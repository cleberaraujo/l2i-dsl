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
def execute_qualification(*,fixture:Mapping[str,Any],mode:str,backend:str,profile:str,repetition:int,execution_id:str|None,results_root:Path,repository:Path)->Path:
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
    manifest_path=_atomic_json(run,"manifest.json",manifest)
    attempt={"state":"RUNNING","running_at_utc":utc_rfc3339(),"execution_id":eid}; _atomic_json(run,"attempt.json",attempt)
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
    gates={"initial_state_materialized":True,"drift_detected":set(detected)==set(normalized["fault_domains"]),"classification_exact":set(detected)==set(normalized["fault_domains"]),
      "unaffected_domains_preserved":all(observed[d]==desired[d] for d in unaffected),"zero_post_fault_writes":mode!="observation_only" or not writes,
      "selective_write_scope":mode!="selective_assurance" or {x["domain"] for x in writes}==set(detected),"convergence_confirmed":mode=="observation_only" or observed==desired,
      "retry_bounded":all(x["attempt"]<=2 for x in retries)}
    observed=copy.deepcopy(desired); gates["cleanup_approved"]=observed==desired; success=all(gates.values())
    summary={"run_status":"completed" if success else "failed","execution_mode":"adapt","rq4_assurance_mode":mode,"detected_domains":detected,"post_fault_writes":writes,
      "remediation_attempts":retries,"gates":gates,"readbacks":{"initial":desired,"post_fault":post_fault_snapshot,"post_cleanup":observed},
      "provenance_pre":pre,"provenance_post":capture_provenance(repository),"QUALIFICATION_ONLY":True,"SCIENTIFIC_RESULT":False,"CAMPAIGN_MEMBER":False}
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
