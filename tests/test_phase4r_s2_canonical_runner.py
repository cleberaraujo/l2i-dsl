from __future__ import annotations
import copy, hashlib, json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
from l2i.s2_canonical_runner import (S2RunnerError, assignment_from_plan,
    binding_from_plan,
    execute_qualification, strict_json, validate_fixture,
    validate_summary_consistency)
from l2i.experiment_contract import sha256_json
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

    def test_real_backend_rejected_before_run_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"results"
            with self.assertRaisesRegex(S2RunnerError,"REAL_BACKEND_NOT_IMPLEMENTED"):
                self.run_mode("observation_only",backend="real",root=root)
            self.assertFalse(root.exists())

    def test_cli_cannot_produce_synthetic_artifact_labeled_real(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"results"
            cp=subprocess.run([os.sys.executable,"-m","scenarios.multidomain_s2","--execution-mode","adapt",
                "--rq4-assurance-mode","observation_only","--backend","real","--profile","p","--repetition","1",
                "--execution-id","S2-forbidden-real","--results-root",str(root),"--qualification-fixture",
                str(ROOT/"config/phase4r_s2_qualification_fixture.json"),"--repository",str(ROOT)],capture_output=True,text=True)
            self.assertNotEqual(cp.returncode,0); self.assertIn("REAL_BACKEND_NOT_IMPLEMENTED",cp.stderr)
            self.assertFalse(root.exists())

    def test_plan_binding_rejects_cli_backend_profile_treatment_and_repository(self):
        slot=PLAN_FIXTURE["run_slots"][0]
        provenance={"commit":PLAN_FIXTURE["repository_commit"],"tree":"a"*40,"branch":"test","worktree_clean":True}
        cases=(
            ({"backend":"real","profile":slot["execution_requirements"]["profile_id"],"mode":slot["assignment"]["treatment"]},"CLI_BACKEND_MISMATCH"),
            ({"backend":"mock","profile":"wrong-profile","mode":slot["assignment"]["treatment"]},"CLI_PROFILE_MISMATCH"),
            ({"backend":"mock","profile":slot["execution_requirements"]["profile_id"],"mode":"selective_assurance"},"ASSIGNMENT_MODE_MISMATCH"),
        )
        for arguments,code in cases:
            with self.subTest(code=code),self.assertRaisesRegex(S2RunnerError,code):
                binding_from_plan(PLAN_FIXTURE,slot["run_slot_id"],repository_provenance=provenance,**arguments)
        with self.assertRaisesRegex(S2RunnerError,"REPOSITORY_COMMIT_MISMATCH"):
            binding_from_plan(PLAN_FIXTURE,slot["run_slot_id"],backend="mock",profile=slot["execution_requirements"]["profile_id"],
                mode=slot["assignment"]["treatment"],repository_provenance=dict(provenance,commit="b"*40))

    def test_plan_binding_rejects_tampered_requirements_scenario_and_external_slot(self):
        invalid_names=("invalid-execution-requirements-scenario.json","invalid-execution-requirements-backend.json")
        slot=PLAN_FIXTURE["run_slots"][0]
        provenance={"commit":PLAN_FIXTURE["repository_commit"]}
        for name in invalid_names:
            invalid=json.loads((ROOT/"schemas/phase19/fixtures/execution-plan-v1"/name).read_text())["plan"]
            with self.subTest(name=name),self.assertRaises(Exception):
                binding_from_plan(invalid,invalid["run_slots"][0]["run_slot_id"],backend="mock",profile="profile-c",
                    mode="observation_only",repository_provenance=provenance)
        with self.assertRaisesRegex(S2RunnerError,"UNKNOWN_RUN_SLOT"):
            binding_from_plan(PLAN_FIXTURE,"run-slot-"+"0"*64,backend="mock",profile=slot["execution_requirements"]["profile_id"],
                mode=slot["assignment"]["treatment"],repository_provenance=provenance)

    def test_manifest_materializes_complete_canonical_binding(self):
        slot=PLAN_FIXTURE["run_slots"][0]
        provenance={"commit":PLAN_FIXTURE["repository_commit"],"tree":"a"*40,"branch":"phase4/s1-s2-technical","worktree_clean":True}
        with tempfile.TemporaryDirectory() as td,mock.patch("l2i.s2_canonical_runner.capture_provenance",return_value=provenance):
            summary=execute_qualification(fixture=FIXTURE,mode=slot["assignment"]["treatment"],backend="mock",
                profile=slot["execution_requirements"]["profile_id"],repetition=1,execution_id="S2-bound",
                results_root=Path(td)/"results",repository=ROOT,plan=PLAN_FIXTURE,run_slot_id=slot["run_slot_id"])
            manifest=json.loads((summary.parent/"manifest.json").read_text()); binding=manifest["plan_binding"]
            self.assertTrue(binding["applicable"])
            self.assertEqual(binding["execution_plan_id"],PLAN_FIXTURE["execution_plan_id"])
            self.assertEqual(binding["execution_plan_sha256"],sha256_json(PLAN_FIXTURE))
            self.assertEqual(binding["run_slot_id"],slot["run_slot_id"])
            self.assertEqual(binding["execution_requirements"],slot["execution_requirements"])
            self.assertEqual(binding["assignment_v2"],slot["assignment"])
            self.assertEqual(binding["assignment_v2_sha256"],sha256_json(slot["assignment"]))

    def test_planless_qualification_is_explicitly_not_applicable(self):
        _,summary=self.run_mode("observation_only")
        self.assertEqual(summary["plan_binding"],{"applicable":False,"justification":"qualification_only_synthetic_fixture"})
        self.assertEqual(summary["harness_scope"],"synthetic_assurance_qualification_only")
        self.assertFalse(summary["operational_s2_qualification"])

    def test_intermediate_symlink_fixture_and_plan_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td); real=base/"real"; real.mkdir(); intermediate=base/"via-link"; intermediate.symlink_to(real,target_is_directory=True)
            (real/"fixture.json").write_text(json.dumps(FIXTURE)); (real/"plan.json").write_text(json.dumps(PLAN_FIXTURE))
            for path in (intermediate/"fixture.json",intermediate/"plan.json"):
                with self.subTest(path=path),self.assertRaisesRegex(S2RunnerError,"UNSAFE_PATH"):
                    strict_json(path)

    def test_results_root_synchronized_substitution_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"results"; root.mkdir(); displaced=Path(td)/"displaced"; replacement=Path(td)/"replacement"; replacement.mkdir()
            def substitute(path):
                path.rename(displaced); path.symlink_to(replacement,target_is_directory=True)
            with self.assertRaisesRegex(S2RunnerError,"RESULTS_ROOT_SUBSTITUTED"):
                execute_qualification(fixture=FIXTURE,mode="observation_only",backend="mock",profile="p",repetition=1,
                    execution_id="S2-substitution",results_root=root,repository=ROOT,after_results_root_open=substitute)
            self.assertFalse((replacement/"S2").exists()); self.assertFalse((displaced/"S2").exists())

    def test_nonregular_and_non_normalized_results_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            regular=Path(td)/"file"; regular.write_text("not a directory")
            with self.assertRaises(S2RunnerError): self.run_mode("observation_only",root=regular)
            with self.assertRaisesRegex(S2RunnerError,"UNSAFE_RESULTS_ROOT"):
                self.run_mode("observation_only",root=Path(td)/"component"/".."/"escape")
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
