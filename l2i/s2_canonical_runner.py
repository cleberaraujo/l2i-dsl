"""Canonical fail-closed S2/RQ4 technical runner (Phase 4R)."""
from __future__ import annotations
import argparse, copy, json, os, re, stat, subprocess
from pathlib import Path
from typing import Any, Mapping, NoReturn
from l2i.execution_plan import validate_execution_plan_v1
from l2i.experiment_contract import (ExperimentAssignmentV2, ExperimentContractError,
    generate_execution_id, sha256_file, sha256_json, utc_rfc3339)

DOMAINS=("A","B","C"); RQ4_MODES=("observation_only","selective_assurance")
IDENTIFIER=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"); MAX_JSON=1024*1024
class S2RunnerError(ExperimentContractError): pass
def fail(code:str,message:str)->NoReturn: raise S2RunnerError(f"{code}: {message}")
def _duplicates(pairs):
    out={}
    for key,value in pairs:
        if key in out: fail("INVALID_JSON",f"duplicate key: {key}")
        out[key]=value
    return out
def _constant(value): fail("INVALID_JSON",f"non-finite constant: {value}")
def strict_json(path:Path)->Any:
    if not path.is_absolute(): fail("UNSAFE_PATH","JSON path must be absolute")
    try: fd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
    except OSError as exc: fail("UNSAFE_PATH",str(exc))
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_JSON: fail("INVALID_JSON_FILE","bounded regular file required")
        payload=b""
        while True:
            chunk=os.read(fd,65536)
            if not chunk: break
            payload+=chunk
            if len(payload)>MAX_JSON: fail("INVALID_JSON_FILE","too large")
    finally: os.close(fd)
    try: return json.loads(payload.decode(),object_pairs_hook=_duplicates,parse_constant=_constant)
    except (UnicodeDecodeError,json.JSONDecodeError) as exc: fail("INVALID_JSON",str(exc))
def _identifier(value,field):
    if not isinstance(value,str) or IDENTIFIER.fullmatch(value) is None: fail("INVALID_IDENTIFIER",field)
    return value
def capture_provenance(repository:Path)->dict[str,Any]:
    def git(*args):
        cp=subprocess.run(["git","-C",str(repository),*args],capture_output=True,text=True)
        if cp.returncode: fail("PROVENANCE_FAILED",cp.stderr.strip())
        return cp.stdout.strip()
    return {"commit":git("rev-parse","HEAD"),"tree":git("rev-parse","HEAD^{tree}"),
            "branch":git("branch","--show-current") or "DETACHED","worktree_clean":git("status","--porcelain")==""}
def _safe_root(path:Path)->Path:
    if not path.is_absolute() or path!=Path(os.path.normpath(path)): fail("UNSAFE_RESULTS_ROOT","absolute normalized path required")
    current=Path(path.anchor)
    for part in path.parts[1:]:
        current/=part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir(): fail("UNSAFE_RESULTS_ROOT",f"unsafe component: {current}")
        else: current.mkdir(mode=0o700)
    return path
def _atomic_json(directory:Path,name:str,value:Any,*,replace=False)->Path:
    if "/" in name or name in {"",".",".."}: fail("UNSAFE_ARTIFACT",name)
    dfd=os.open(directory,os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_NOFOLLOW",0))
    temp=f".{name}.tmp-{os.getpid()}-{os.urandom(6).hex()}"
    try:
        if not replace:
            try: os.stat(name,dir_fd=dfd,follow_symlinks=False)
            except FileNotFoundError: pass
            else: fail("ARTIFACT_COLLISION",name)
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600,dir_fd=dfd)
        try:
            payload=(json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+"\n").encode(); offset=0
            while offset<len(payload): offset+=os.write(fd,payload[offset:])
            os.fsync(fd)
        finally: os.close(fd)
        os.rename(temp,name,src_dir_fd=dfd,dst_dir_fd=dfd); os.fsync(dfd)
    finally:
        try: os.unlink(temp,dir_fd=dfd)
        except FileNotFoundError: pass
        os.close(dfd)
    return directory/name
def validate_fixture(value:Any)->dict[str,Any]:
    fields={"fixture_version","qualification_only","scientific_result","campaign_member","fixture_id","fault_domains","reject_first_remediation","desired_state"}
    if not isinstance(value,dict) or set(value)!=fields: fail("INVALID_FIXTURE","unknown or missing properties")
    if value["fixture_version"]!="phase4r-s2-qualification-fixture-v1": fail("INVALID_FIXTURE","version")
    if (value["qualification_only"],value["scientific_result"],value["campaign_member"])!=(True,False,False): fail("INVALID_FIXTURE","non-scientific markers")
    _identifier(value["fixture_id"],"fixture_id"); faults=value["fault_domains"]
    if not isinstance(faults,list) or not faults or len(faults)!=len(set(faults)) or any(x not in DOMAINS for x in faults): fail("INVALID_FIXTURE","fault domains")
    if not isinstance(value["reject_first_remediation"],bool): fail("INVALID_FIXTURE","retry flag")
    desired=value["desired_state"]
    if not isinstance(desired,dict) or set(desired)!=set(DOMAINS) or any(not isinstance(desired[d],dict) or not desired[d] for d in DOMAINS): fail("INVALID_FIXTURE","desired state")
    return copy.deepcopy(value)
def assignment_from_plan(plan:Any,run_slot_id:str)->dict[str,Any]:
    validate_execution_plan_v1(plan)
    if plan["rq_id"]!="RQ4" or plan["scenario_id"]!="S2": fail("INCOMPATIBLE_PLAN","RQ4/S2 required")
    matches=[s for s in plan["run_slots"] if s["run_slot_id"]==run_slot_id]
    if len(matches)!=1: fail("UNKNOWN_RUN_SLOT","external slot has no authority")
    assignment=matches[0]["assignment"]; ExperimentAssignmentV2(**assignment); return copy.deepcopy(assignment)
def _gate(*, applicable: bool, passed: bool | None, evidence: Any) -> dict[str, Any]:
    """Build a gate without representing non-applicability as approval."""
    if not applicable and passed is not None:
        fail("INVALID_GATE", "a non-applicable gate cannot have a pass result")
    if applicable and not isinstance(passed, bool):
        fail("INVALID_GATE", "an applicable gate requires a boolean result")
    return {"applicable": applicable, "passed": passed, "evidence": evidence}


def validate_summary_consistency(summary: Mapping[str, Any]) -> None:
    """Reject summaries whose gates contradict events or readbacks."""
    mode = summary.get("rq4_assurance_mode")
    gates = summary.get("gates")
    writes = summary.get("post_fault_writes")
    readbacks = summary.get("readbacks")
    detected = summary.get("detected_domains")
    if mode not in RQ4_MODES or not isinstance(gates, dict):
        fail("CONTRADICTORY_SUMMARY", "mode or gates")
    if not isinstance(writes, list) or not isinstance(readbacks, dict) or not isinstance(detected, list):
        fail("CONTRADICTORY_SUMMARY", "events or readbacks")
    required = {
        "initial_state_materialized", "drift_detected", "classification_exact",
        "unaffected_domains_preserved", "zero_post_fault_writes",
        "selective_write_scope", "convergence_confirmed", "retry_bounded",
        "post_cleanup_restoration_confirmed",
    }
    if set(gates) != required:
        fail("CONTRADICTORY_SUMMARY", "unknown or missing gates")
    for name, gate in gates.items():
        if not isinstance(gate, dict) or set(gate) != {"applicable", "passed", "evidence"}:
            fail("CONTRADICTORY_SUMMARY", f"malformed gate: {name}")
        if gate["applicable"] is False and gate["passed"] is not None:
            fail("CONTRADICTORY_SUMMARY", f"non-applicable gate approved: {name}")
        if gate["applicable"] is True and not isinstance(gate["passed"], bool):
            fail("CONTRADICTORY_SUMMARY", f"applicable gate has no result: {name}")
    zero = gates["zero_post_fault_writes"]
    scope = gates["selective_write_scope"]
    convergence = gates["convergence_confirmed"]
    desired = readbacks.get("desired")
    initial = readbacks.get("initial")
    post_fault = readbacks.get("post_fault")
    post_cleanup = readbacks.get("post_cleanup")
    if not all(isinstance(value, dict) for value in (desired, initial, post_fault, post_cleanup)):
        fail("CONTRADICTORY_SUMMARY", "missing state readback")
    derived_detected = [domain for domain in DOMAINS if post_fault.get(domain) != desired.get(domain)]
    if detected != derived_detected:
        fail("CONTRADICTORY_SUMMARY", "detected domains disagree with readback")
    if gates["initial_state_materialized"]["passed"] != (initial == desired):
        fail("CONTRADICTORY_SUMMARY", "initial materialization gate")
    if gates["drift_detected"]["passed"] != bool(derived_detected):
        fail("CONTRADICTORY_SUMMARY", "drift detection gate")
    expected = gates["classification_exact"]["evidence"].get("expected_domains")
    if gates["classification_exact"]["passed"] != (set(detected) == set(expected or ())):
        fail("CONTRADICTORY_SUMMARY", "classification gate")
    unaffected = [domain for domain in DOMAINS if domain not in detected]
    if gates["unaffected_domains_preserved"]["passed"] != all(readbacks.get("pre_cleanup", {}).get(domain) == desired.get(domain) for domain in unaffected):
        fail("CONTRADICTORY_SUMMARY", "unaffected-domain gate")
    if gates["post_cleanup_restoration_confirmed"]["passed"] != (post_cleanup == desired):
        fail("CONTRADICTORY_SUMMARY", "cleanup restoration gate")
    if mode == "observation_only":
        if zero != _gate(applicable=True, passed=not writes, evidence={"post_fault_write_count": len(writes)}):
            fail("CONTRADICTORY_SUMMARY", "observation write gate")
        if any(gates[name]["applicable"] for name in ("selective_write_scope", "convergence_confirmed", "retry_bounded")):
            fail("CONTRADICTORY_SUMMARY", "selective gates applied to observation")
        if readbacks.get("pre_cleanup") == readbacks.get("desired"):
            fail("CONTRADICTORY_SUMMARY", "observation incorrectly reconverged during window")
    else:
        if zero["applicable"] or zero["passed"] is not None:
            fail("CONTRADICTORY_SUMMARY", "zero-write gate applied to selective assurance")
        if not all(gates[name]["applicable"] for name in ("selective_write_scope", "convergence_confirmed", "retry_bounded")):
            fail("CONTRADICTORY_SUMMARY", "selective gate not applicable")
        write_domains = [entry.get("domain") for entry in writes]
        if scope["passed"] != (set(write_domains) == set(detected) and len(write_domains) == len(set(write_domains))):
            fail("CONTRADICTORY_SUMMARY", "selective write scope")
        if convergence["passed"] != (readbacks.get("pre_cleanup") == readbacks.get("desired")):
            fail("CONTRADICTORY_SUMMARY", "pre-cleanup convergence")
        attempts = summary.get("remediation_attempts")
        if not isinstance(attempts, list):
            fail("CONTRADICTORY_SUMMARY", "remediation attempts")
        bounded = all(isinstance(item, dict) and isinstance(item.get("attempt"), int) and item["attempt"] <= 2 for item in attempts)
        if gates["retry_bounded"]["passed"] != bounded:
            fail("CONTRADICTORY_SUMMARY", "retry bound gate")
        if not writes:
            fail("CONTRADICTORY_SUMMARY", "selective assurance recorded no remediation")


def execute_qualification(*,fixture:Mapping[str,Any],mode:str,backend:str,profile:str,repetition:int,execution_id:str|None,results_root:Path,repository:Path,
                          crash_at: str | None = None, cleanup_succeeds: bool = True)->Path:
    if mode not in RQ4_MODES: fail("UNSUPPORTED_RQ4_MODE","no fallback")
    if backend not in {"mock","real"}: fail("UNSUPPORTED_BACKEND",backend)
    _identifier(profile,"profile")
    if not isinstance(repetition,int) or isinstance(repetition,bool) or repetition<1: fail("INVALID_REPETITION",str(repetition))
    normalized=validate_fixture(dict(fixture)); eid=_identifier(execution_id,"execution_id") if execution_id else generate_execution_id("S2")
    root=_safe_root(results_root); scenario=root/"S2"; scenario.mkdir(mode=0o700,exist_ok=True)
    if scenario.is_symlink(): fail("UNSAFE_RESULTS_ROOT","scenario symlink")
    run=scenario/eid
    try: run.mkdir(mode=0o700,exist_ok=False)
    except FileExistsError: fail("EXECUTION_COLLISION",str(run))
    pre=capture_provenance(repository); desired=copy.deepcopy(normalized["desired_state"]); observed=copy.deepcopy(desired)
    manifest={"contract_version":"phase4r-s2-canonical-run-v1","identity":{"execution_id":eid,"profile":profile,"backend":backend,"repetition":repetition},
      "execution_mode":"adapt","rq4_assurance_mode":mode,"fixture":normalized,"fixture_sha256":sha256_json(normalized),"desired_state_sha256":sha256_json(desired),
      "provenance_pre":pre,"QUALIFICATION_ONLY":True,"SCIENTIFIC_RESULT":False,"CAMPAIGN_MEMBER":False}
    if crash_at not in {None, "before_running", "after_running"}:
        fail("INVALID_CRASH_POINT", str(crash_at))
    if not isinstance(cleanup_succeeds, bool):
        fail("INVALID_CLEANUP_CONTROL", "boolean required")
    manifest_path=_atomic_json(run,"manifest.json",manifest)
    attempt={"state":"MATERIALIZED","materialized_at_utc":utc_rfc3339(),"execution_id":eid}
    _atomic_json(run,"attempt.json",attempt)
    if crash_at == "before_running":
        fail("INJECTED_CRASH_BEFORE_RUNNING", str(run))
    attempt.update({"state":"RUNNING","running_at_utc":utc_rfc3339()})
    _atomic_json(run,"attempt.json",attempt,replace=True)
    if crash_at == "after_running":
        fail("INJECTED_CRASH_AFTER_RUNNING", str(run))
    # Independent injector changes only observed state and never invokes assurance.
    for domain in normalized["fault_domains"]: observed[domain]={"phase4r_drift":True}
    post_fault_snapshot=copy.deepcopy(observed); detected=[d for d in DOMAINS if observed[d]!=desired[d]]
    writes=[]; retries=[]
    if mode=="selective_assurance":
        for domain in detected:
            number=1
            if normalized["reject_first_remediation"]: retries.append({"domain":domain,"attempt":1,"result":"synthetic_rejection"}); number=2
            observed[domain]=copy.deepcopy(desired[domain]); writes.append({"domain":domain,"component":"declarative_state"}); retries.append({"domain":domain,"attempt":number,"result":"applied"})
    unaffected=[d for d in DOMAINS if d not in detected]
    pre_cleanup=copy.deepcopy(observed)
    exact=set(detected)==set(normalized["fault_domains"])
    write_domains=[x["domain"] for x in writes]
    gates={
      "initial_state_materialized":_gate(applicable=True,passed=True,evidence={"desired_state_sha256":sha256_json(desired)}),
      "drift_detected":_gate(applicable=True,passed=exact,evidence={"detected_domains":detected}),
      "classification_exact":_gate(applicable=True,passed=exact,evidence={"expected_domains":normalized["fault_domains"],"detected_domains":detected}),
      "unaffected_domains_preserved":_gate(applicable=True,passed=all(pre_cleanup[d]==desired[d] for d in unaffected),evidence={"unaffected_domains":unaffected}),
      "zero_post_fault_writes":_gate(applicable=mode=="observation_only",passed=not writes if mode=="observation_only" else None,evidence={"post_fault_write_count":len(writes)}),
      "selective_write_scope":_gate(applicable=mode=="selective_assurance",passed=(set(write_domains)==set(detected) and len(write_domains)==len(set(write_domains))) if mode=="selective_assurance" else None,evidence={"detected_domains":detected,"written_domains":write_domains}),
      "convergence_confirmed":_gate(applicable=mode=="selective_assurance",passed=pre_cleanup==desired if mode=="selective_assurance" else None,evidence={"readback":"pre_cleanup"}),
      "retry_bounded":_gate(applicable=mode=="selective_assurance",passed=all(x["attempt"]<=2 for x in retries) if mode=="selective_assurance" else None,evidence={"maximum_attempt":max((x["attempt"] for x in retries),default=None),"limit":2}),
    }
    if cleanup_succeeds:
        observed=copy.deepcopy(desired)
    gates["post_cleanup_restoration_confirmed"]=_gate(applicable=True,passed=observed==desired,evidence={"readback":"post_cleanup"})
    success=all(gate["passed"] is True for gate in gates.values() if gate["applicable"])
    summary={"run_status":"completed" if success else "failed","execution_mode":"adapt","rq4_assurance_mode":mode,"detected_domains":detected,"post_fault_writes":writes,
      "remediation_attempts":retries,"gates":gates,"readbacks":{"desired":desired,"initial":desired,"post_fault":post_fault_snapshot,"pre_cleanup":pre_cleanup,"post_cleanup":observed},
      "provenance_pre":pre,"provenance_post":capture_provenance(repository),"QUALIFICATION_ONLY":True,"SCIENTIFIC_RESULT":False,"CAMPAIGN_MEMBER":False}
    validate_summary_consistency(summary)
    summary_path=_atomic_json(run,"summary.json",summary); hashes={p.name:sha256_file(p) for p in (manifest_path,summary_path)}; _atomic_json(run,"artifact-hashes.json",hashes)
    attempt.update({"state":"SUCCEEDED" if success else "FAILED","completed_at_utc":utc_rfc3339(),"artifact_hashes":hashes}); _atomic_json(run,"attempt.json",attempt,replace=True)
    if not success: fail("QUALIFICATION_FAILED",str(gates))
    return summary_path
def build_parser():
    p=argparse.ArgumentParser(description="Canonical S2/RQ4 technical runner",allow_abbrev=False)
    p.add_argument("--execution-mode",required=True,choices=("adapt",)); p.add_argument("--rq4-assurance-mode",required=True,choices=RQ4_MODES)
    p.add_argument("--backend",required=True,choices=("mock","real")); p.add_argument("--profile",required=True); p.add_argument("--repetition",required=True,type=int)
    p.add_argument("--execution-id"); p.add_argument("--results-root",required=True,type=Path); p.add_argument("--qualification-fixture",required=True,type=Path)
    p.add_argument("--repository",required=True,type=Path); p.add_argument("--plan",type=Path); p.add_argument("--run-slot-id"); return p
def main(argv=None):
    args=build_parser().parse_args(argv)
    try:
        fixture=strict_json(args.qualification_fixture)
        if (args.plan is None)!=(args.run_slot_id is None): fail("INCOMPLETE_PLAN_BINDING","plan and slot are paired")
        if args.plan is not None:
            assignment=assignment_from_plan(strict_json(args.plan),args.run_slot_id)
            if assignment["treatment"]!=args.rq4_assurance_mode: fail("ASSIGNMENT_MODE_MISMATCH","AssignmentV2 has precedence")
        summary=execute_qualification(fixture=fixture,mode=args.rq4_assurance_mode,backend=args.backend,profile=args.profile,repetition=args.repetition,execution_id=args.execution_id,results_root=args.results_root,repository=args.repository)
        print(f"S2_CANONICAL_SUMMARY={summary}\nS2_CANONICAL_RUN_OK=True"); return 0
    except (S2RunnerError,ExperimentContractError) as exc:
        print(f"S2_CANONICAL_RUN_OK=False\nS2_CANONICAL_FAILURE={exc}",file=os.sys.stderr); return 2
if __name__=="__main__": raise SystemExit(main())
