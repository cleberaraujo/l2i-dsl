from __future__ import annotations
import copy, hashlib, json, os, subprocess, tempfile, unittest
from pathlib import Path
from l2i.s2_canonical_runner import (S2RunnerError, assignment_from_plan,
    execute_qualification, strict_json, validate_fixture,
    validate_summary_consistency)
ROOT=Path(__file__).resolve().parents[1]
FIXTURE=json.loads((ROOT/"config/phase4r_s2_qualification_fixture.json").read_text())
PLAN_FIXTURE=json.loads((ROOT/"schemas/phase19/fixtures/execution-plan-v1/valid-pilot-rq4-mock-multiconfiguration.json").read_text())["plan"]
class S2CanonicalRunnerTests(unittest.TestCase):
    def run_mode(self,mode,*,backend="mock",fixture=None,eid=None,root=None):
        if root is None:
            temporary=tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            root=Path(temporary.name)/"results"
        summary=execute_qualification(fixture=fixture or FIXTURE,mode=mode,backend=backend,
            profile="phase4r-qualification",repetition=1,execution_id=eid,
            results_root=root,repository=ROOT)
        return summary,json.loads(summary.read_text())
    def test_observation_only_has_zero_post_fault_writes(self):
        _,v=self.run_mode("observation_only")
        self.assertEqual(v["detected_domains"],["C"]); self.assertEqual(v["post_fault_writes"],[])
        self.assertEqual(v["gates"]["zero_post_fault_writes"]["passed"],True)
        self.assertEqual(v["remediation_attempts"],[])
        self.assertEqual(v["readbacks"]["pre_cleanup"],v["readbacks"]["post_fault"])
        self.assertNotEqual(v["readbacks"]["pre_cleanup"],v["readbacks"]["desired"])
    def test_selective_assurance_writes_only_divergent_domain(self):
        _,v=self.run_mode("selective_assurance")
        self.assertEqual({x["domain"] for x in v["post_fault_writes"]},{"C"})
        self.assertEqual([x["attempt"] for x in v["remediation_attempts"]],[1,2])
        self.assertEqual(v["gates"]["zero_post_fault_writes"],{
            "applicable":False,"passed":None,"evidence":{"post_fault_write_count":1}})
        self.assertTrue(v["gates"]["convergence_confirmed"]["passed"])
        self.assertEqual(v["readbacks"]["pre_cleanup"],v["readbacks"]["desired"])
    def test_common_initial_state_and_non_scientific_markers(self):
        _,a=self.run_mode("observation_only"); _,b=self.run_mode("selective_assurance")
        self.assertEqual(a["readbacks"]["initial"],b["readbacks"]["initial"])
        for v in (a,b): self.assertEqual((v["QUALIFICATION_ONLY"],v["SCIENTIFIC_RESULT"],v["CAMPAIGN_MEMBER"]),(True,False,False))
    def test_all_domain_fault_scope(self):
        fixture=dict(FIXTURE,fault_domains=["A","B","C"],reject_first_remediation=False)
        _,v=self.run_mode("selective_assurance",fixture=fixture)
        self.assertEqual({x["domain"] for x in v["post_fault_writes"]},{"A","B","C"})

    def test_non_applicable_gates_are_not_approvals(self):
        _,observation=self.run_mode("observation_only")
        for name in ("selective_write_scope","convergence_confirmed","retry_bounded"):
            self.assertEqual(observation["gates"][name]["applicable"],False)
            self.assertIsNone(observation["gates"][name]["passed"])
        _,selective=self.run_mode("selective_assurance")
        self.assertEqual(selective["gates"]["zero_post_fault_writes"]["applicable"],False)
        self.assertIsNone(selective["gates"]["zero_post_fault_writes"]["passed"])

    def test_cleanup_is_separate_from_observation_and_remediation(self):
        _,v=self.run_mode("observation_only")
        self.assertNotEqual(v["readbacks"]["pre_cleanup"],v["readbacks"]["desired"])
        self.assertEqual(v["readbacks"]["post_cleanup"],v["readbacks"]["desired"])
        self.assertTrue(v["gates"]["post_cleanup_restoration_confirmed"]["passed"])
        self.assertFalse(v["gates"]["convergence_confirmed"]["applicable"])

    def test_summary_consistency_rejects_event_gate_contradictions(self):
        _,observation=self.run_mode("observation_only")
        bad=copy.deepcopy(observation)
        bad["post_fault_writes"]=[{"domain":"C","component":"declarative_state"}]
        with self.assertRaises(S2RunnerError): validate_summary_consistency(bad)
        bad=copy.deepcopy(observation)
        bad["gates"]["convergence_confirmed"]={"applicable":True,"passed":True,"evidence":{"readback":"post_cleanup"}}
        with self.assertRaises(S2RunnerError): validate_summary_consistency(bad)
        _,selective=self.run_mode("selective_assurance")
        bad=copy.deepcopy(selective)
        bad["gates"]["zero_post_fault_writes"]={"applicable":True,"passed":True,"evidence":{"post_fault_write_count":0}}
        with self.assertRaises(S2RunnerError): validate_summary_consistency(bad)
        bad=copy.deepcopy(selective)
        bad["post_fault_writes"][0]["domain"]="A"
        with self.assertRaises(S2RunnerError): validate_summary_consistency(bad)

    @staticmethod
    def directory_digest(path):
        digest=hashlib.sha256()
        for item in sorted(path.rglob("*")):
            if item.is_file():
                digest.update(str(item.relative_to(path)).encode())
                digest.update(item.read_bytes())
        return digest.hexdigest()

    def test_crash_before_running_preserved_and_identity_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"results"; eid="S2-crash-before"
            with self.assertRaisesRegex(S2RunnerError,"INJECTED_CRASH_BEFORE_RUNNING"):
                execute_qualification(fixture=FIXTURE,mode="observation_only",backend="mock",profile="p",repetition=1,
                    execution_id=eid,results_root=root,repository=ROOT,crash_at="before_running")
            run=root/"S2"/eid
            self.assertEqual(json.loads((run/"attempt.json").read_text())["state"],"MATERIALIZED")
            before=self.directory_digest(run)
            with self.assertRaisesRegex(S2RunnerError,"EXECUTION_COLLISION"):
                execute_qualification(fixture=FIXTURE,mode="observation_only",backend="mock",profile="p",repetition=1,
                    execution_id=eid,results_root=root,repository=ROOT)
            self.assertEqual(before,self.directory_digest(run))

    def test_crash_after_running_retry_has_new_identity_and_preserves_previous(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"results"; old="S2-crash-after"; new="S2-retry-new"
            with self.assertRaisesRegex(S2RunnerError,"INJECTED_CRASH_AFTER_RUNNING"):
                execute_qualification(fixture=FIXTURE,mode="selective_assurance",backend="mock",profile="p",repetition=1,
                    execution_id=old,results_root=root,repository=ROOT,crash_at="after_running")
            old_run=root/"S2"/old
            self.assertEqual(json.loads((old_run/"attempt.json").read_text())["state"],"RUNNING")
            before=self.directory_digest(old_run)
            summary=execute_qualification(fixture=FIXTURE,mode="selective_assurance",backend="mock",profile="p",repetition=1,
                execution_id=new,results_root=root,repository=ROOT)
            self.assertEqual(json.loads((summary.parent/"attempt.json").read_text())["state"],"SUCCEEDED")
            self.assertNotEqual(old_run,summary.parent)
            self.assertEqual(before,self.directory_digest(old_run))

    def test_cleanup_failure_is_terminal_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"results"; eid="S2-cleanup-failure"
            with self.assertRaisesRegex(S2RunnerError,"QUALIFICATION_FAILED"):
                execute_qualification(fixture=FIXTURE,mode="observation_only",backend="mock",profile="p",repetition=1,
                    execution_id=eid,results_root=root,repository=ROOT,cleanup_succeeds=False)
            run=root/"S2"/eid
            self.assertEqual(json.loads((run/"attempt.json").read_text())["state"],"FAILED")
            summary=json.loads((run/"summary.json").read_text())
            self.assertFalse(summary["gates"]["post_cleanup_restoration_confirmed"]["passed"])
    def test_unknown_mode_and_baseline_adapt_fail_closed(self):
        for mode in ("baseline","adapt","unknown"):
            with self.assertRaises(S2RunnerError): self.run_mode(mode)
    def test_fixture_unknown_property_and_markers_rejected(self):
        with self.assertRaises(S2RunnerError): validate_fixture(dict(FIXTURE,extra=True))
        with self.assertRaises(S2RunnerError): validate_fixture(dict(FIXTURE,scientific_result=True))
    def test_collision_and_symlink_root_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"results"; self.run_mode("observation_only",eid="S2-fixed",root=root)
            with self.assertRaises(S2RunnerError): self.run_mode("observation_only",eid="S2-fixed",root=root)
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)/"target"; target.mkdir(); link=Path(td)/"link"; link.symlink_to(target,target_is_directory=True)
            with self.assertRaises(S2RunnerError): self.run_mode("observation_only",root=link)
    def test_assignment_v2_has_precedence(self):
        first=PLAN_FIXTURE["run_slots"][0]; assignment=assignment_from_plan(PLAN_FIXTURE,first["run_slot_id"])
        self.assertEqual(assignment["treatment"],"observation_only")
        with self.assertRaises(S2RunnerError): assignment_from_plan(PLAN_FIXTURE,"run-slot-"+"0"*64)
    def test_strict_json_rejects_duplicates_nan_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"x.json"; path.write_text('{"a":1,"a":2}')
            with self.assertRaises(S2RunnerError): strict_json(path)
            path.write_text('{"a":NaN}')
            with self.assertRaises(S2RunnerError): strict_json(path)
            target=Path(td)/"target.json"; target.write_text('{}'); link=Path(td)/"link.json"; link.symlink_to(target)
            with self.assertRaises(S2RunnerError): strict_json(link)
    def test_cli_rejects_cross_dimension_alias(self):
        cp=subprocess.run([os.sys.executable,"-m","scenarios.multidomain_s2","--execution-mode","adapt","--rq4-assurance-mode","baseline",
            "--backend","mock","--profile","p","--repetition","1","--results-root","/tmp/x","--qualification-fixture",str(ROOT/"config/phase4r_s2_qualification_fixture.json"),"--repository",str(ROOT)],capture_output=True,text=True)
        self.assertNotEqual(cp.returncode,0)
if __name__=="__main__": unittest.main()
