from __future__ import annotations

import copy
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from jsonschema import Draft202012Validator, FormatChecker

from l2i.execution_plan import materialize_execution_plan_v1
from l2i.experiment_plan import materialize_randomization_manifest_v1
from l2i.runtime_adapter import (
    RuntimeAdapterError,
    build_s1_argv,
    dry_run_description,
    materialize_paired_unit,
    reserve_attempt,
    run_attempt_with_executor,
    transition_attempt,
    validate_materialized_unit,
    validate_results_root,
)


COMMIT = "1" * 40
SPEC_PATH = "zzz/canonical-scenario.data"
SPEC_BYTES = b'{"kind":"phase19-test-scenario-spec"}\n'
SPEC_SHA256 = hashlib.sha256(SPEC_BYTES).hexdigest()
AUXILIARY_PATH = "aaa/looks-like-scenario.yaml"
RUNTIME = {
    "spec_path": SPEC_PATH,
    "duration_s": 2,
    "flow_mbps": 8,
    "best_effort_mbps": 60,
    "bandwidths_mbps": {"A": 100, "B": 50, "C": 100},
    "delay_ms": 1,
    "rtt_interval_ms": 50,
    "rtt_samples": 40,
    "bandwidth_tolerance_mbps": 0.25,
}


def source(role: str, path: str, digest: str) -> dict[str, str]:
    return {"role": role, "path": path, "sha256": digest}


def make_plan(
    *,
    stage: str = "pilot",
    rq: str = "RQ3",
    scenario: str = "S1",
    backend: str = "mock",
    sources: list[dict[str, str]] | None = None,
) -> dict:
    request = {
        "request_version": "phase19-randomization-request-v1",
        "manifest_id": "adapter-test-manifest",
        "campaign_id": "adapter-test-campaign",
        "campaign_stage": stage,
        "rq_id": rq,
        "scenario_id": scenario,
        "backend_mode": backend,
        "repository_commit": COMMIT,
        "seed_hex": "12" * 32,
        "configurations": [
            {
                "configuration_id": "adapter-test-configuration",
                "profile_id": "adapter-test-profile",
                "block_count": 2,
                "source_artifacts": sources
                if sources is not None
                else [
                    source("scenario_spec", SPEC_PATH, SPEC_SHA256),
                    source("profile_definition", AUXILIARY_PATH, "2" * 64),
                ],
            }
        ],
    }
    return materialize_execution_plan_v1(materialize_randomization_manifest_v1(request))


def provenance() -> dict:
    return {
        "commit": COMMIT,
        "tree": "3" * 40,
        "branch": "develop",
        "worktree_clean": True,
        "origin_commit": COMMIT,
        "hostname": "test-host",
        "user": "test-user",
    }


def fixed_now() -> dt.datetime:
    return dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)


class RuntimeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source_root = Path(self.temporary.name, "source")
        self.results_root = Path(self.temporary.name, "results")
        self.source_root.mkdir()
        self.results_root.mkdir()
        spec = self.source_root / SPEC_PATH
        spec.parent.mkdir(parents=True)
        spec.write_bytes(SPEC_BYTES)
        schema_target = self.source_root / "schemas/phase19"
        schema_target.mkdir(parents=True)
        repository = Path(__file__).resolve().parents[1]
        schema_target.joinpath("execution-attempt-v1.schema.json").write_bytes(
            repository.joinpath("schemas/phase19/execution-attempt-v1.schema.json").read_bytes()
        )
        self.plan = make_plan()
        self.by_order = {
            block["order"]: block["block_id"]
            for block in self.plan["randomization_manifest"]["blocks"]
        }

    def unit(self, order: str = "AB", *, plan: dict | None = None, runtime: dict | None = None):
        selected_plan = plan or self.plan
        by_order = {
            block["order"]: block["block_id"]
            for block in selected_plan["randomization_manifest"]["blocks"]
        }
        return materialize_paired_unit(
            selected_plan,
            block_id=by_order[order],
            repetition=1,
            runtime_parameters=runtime or RUNTIME,
        )

    def reserve(self, unit, slot_index: int = 0, *, execution_id: str = "S1-test-execution") -> Path:
        return reserve_attempt(
            unit,
            self.plan,
            run_slot_id=unit["slots"][slot_index]["run_slot_id"],
            results_root=self.results_root,
            source_root=self.source_root,
            provenance=provenance(),
            execution_id_factory=lambda _scenario: execution_id,
            now=fixed_now,
        )

    def preflight(self, sidecar: Path) -> None:
        transition_attempt(sidecar, "PREFLIGHT_PASSED", results_root=self.results_root, now=fixed_now)

    def inert_run(self, sidecar: Path, unit, executor):
        return run_attempt_with_executor(
            sidecar,
            executor=executor,
            cwd=self.source_root,
            plan=self.plan,
            unit=unit,
            results_root=self.results_root,
            source_root=self.source_root,
            provenance_provider=lambda _cwd: provenance(),
        )

    def reservation_path(self, execution_id: str) -> Path:
        return self.results_root / f"phase19-execution-id-{execution_id}"

    def assert_reservation_rejected(self, sidecar: Path, unit) -> None:
        before = sidecar.read_bytes()
        executor = mock.Mock()
        with self.assertRaisesRegex(RuntimeAdapterError, "INVALID_ATTEMPT_RESERVATION"):
            self.inert_run(sidecar, unit, executor)
        executor.assert_not_called()
        self.assertEqual(sidecar.read_bytes(), before)
        persisted = json.loads(before)
        self.assertEqual(persisted["state"], "PREFLIGHT_PASSED")
        self.assertIsNone(persisted["outcome"])

    def post_binding_tamper(self, sidecar: Path, unit, mutator):
        observed_fds = []
        executor = mock.Mock()
        from l2i import runtime_adapter as adapter_module

        original = adapter_module._bind_verified_descriptors

        def tampering_binding(logical_invocation, **kwargs):
            bound = original(logical_invocation, **kwargs)
            observed_fds.extend((kwargs["spec_fd"], kwargs["results_fd"]))
            record = json.loads(sidecar.read_text(encoding="utf-8"))
            mutator(record)
            sidecar.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
            return bound

        with mock.patch.object(adapter_module, "_bind_verified_descriptors", tampering_binding):
            with self.assertRaises(RuntimeAdapterError):
                self.inert_run(sidecar, unit, executor)
        return executor, observed_fds

    def test_nominal_scenario_spec_is_role_selected_and_order_independent(self):
        unit = self.unit()
        matches = [item for item in unit["source_artifacts"] if item["role"] == "scenario_spec"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["path"], SPEC_PATH)
        self.assertEqual(unit["runtime_parameters"]["spec_path"], SPEC_PATH)
        reversed_plan = make_plan(sources=list(reversed([
            source("scenario_spec", SPEC_PATH, SPEC_SHA256),
            source("profile_definition", AUXILIARY_PATH, "2" * 64),
        ])))
        self.assertEqual(unit, self.unit(plan=reversed_plan))

    def test_filename_and_extension_do_not_confer_authority(self):
        unit = self.unit()
        self.assertEqual(
            [item["path"] for item in unit["source_artifacts"] if item["role"] == "scenario_spec"],
            [SPEC_PATH],
        )
        self.assertIn(AUXILIARY_PATH, [item["path"] for item in unit["source_artifacts"]])

    def test_role_swap_changes_authority_and_runtime_assertion_must_follow(self):
        swapped = make_plan(sources=[
            source("profile_definition", SPEC_PATH, SPEC_SHA256),
            source("scenario_spec", AUXILIARY_PATH, "2" * 64),
        ])
        with self.assertRaisesRegex(RuntimeAdapterError, "SCENARIO_SPEC_ASSERTION_MISMATCH"):
            self.unit(plan=swapped)

    def test_invalid_role_and_cardinality_are_rejected(self):
        cases = [
            [source("profile_definition", SPEC_PATH, SPEC_SHA256)],
            [source("scenario_spec", SPEC_PATH, SPEC_SHA256), source("scenario_spec", AUXILIARY_PATH, "2" * 64)],
            [{"path": SPEC_PATH, "sha256": SPEC_SHA256}],
            [source("", SPEC_PATH, SPEC_SHA256)],
            [source("../scenario_spec", SPEC_PATH, SPEC_SHA256)],
        ]
        for sources in cases:
            with self.subTest(sources=sources), self.assertRaises(Exception):
                make_plan(sources=sources)
        request_plan = make_plan()
        broken = copy.deepcopy(request_plan)
        broken["randomization_manifest"]["configurations"].append(
            {
                "configuration_id": "second-configuration",
                "profile_id": "second-profile",
                "block_count": 2,
                "source_artifacts": [source("scenario_spec", "other/spec.bin", "4" * 64)],
            }
        )
        with self.assertRaises(Exception):
            materialize_paired_unit(
                broken,
                block_id=broken["run_slots"][0]["assignment"]["block_id"],
                repetition=1,
                runtime_parameters=RUNTIME,
            )

    def test_spec_assertion_hash_and_paths_fail_closed(self):
        changed = dict(RUNTIME, spec_path="other/spec.data")
        with self.assertRaisesRegex(RuntimeAdapterError, "SCENARIO_SPEC_ASSERTION_MISMATCH"):
            self.unit(runtime=changed)
        self.source_root.joinpath(SPEC_PATH).write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeAdapterError, "SCENARIO_SPEC_HASH_MISMATCH"):
            self.reserve(self.unit())
        self.assertEqual(list(self.results_root.iterdir()), [])

    def test_symlink_spec_and_intermediate_component_are_rejected(self):
        real = self.source_root / "real-spec"
        real.write_bytes(SPEC_BYTES)
        self.source_root.joinpath(SPEC_PATH).unlink()
        self.source_root.joinpath(SPEC_PATH).symlink_to(real)
        with self.assertRaisesRegex(RuntimeAdapterError, "SCENARIO_SPEC_OPEN_FAILED"):
            self.reserve(self.unit())
        self.source_root.joinpath(SPEC_PATH).unlink()
        self.source_root.joinpath("zzz").rmdir()
        external = Path(self.temporary.name, "external")
        external.mkdir()
        external.joinpath("canonical-scenario.data").write_bytes(SPEC_BYTES)
        self.source_root.joinpath("zzz").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeAdapterError, "SCENARIO_SPEC_OPEN_FAILED"):
            self.reserve(self.unit(), execution_id="S1-second")
        self.assertEqual(list(self.results_root.iterdir()), [])

    def test_results_root_symlink_component_is_rejected_without_external_write(self):
        external = Path(self.temporary.name, "external-results")
        external.mkdir()
        link = Path(self.temporary.name, "results-link")
        link.symlink_to(external, target_is_directory=True)
        with self.assertRaises(OSError):
            validate_results_root(link)
        self.assertEqual(list(external.iterdir()), [])
        campaign_link = self.results_root / "adapter-test-campaign"
        campaign_link.symlink_to(external, target_is_directory=True)
        with self.assertRaises(OSError):
            self.reserve(self.unit(), execution_id="S1-intermediate-symlink")
        self.assertEqual(list(external.iterdir()), [])

    def test_materialized_unit_is_deeply_immutable_and_deterministic(self):
        unit = self.unit()
        duplicate = self.unit()
        self.assertEqual(unit, duplicate)
        with self.assertRaises(RuntimeAdapterError):
            unit["campaign_id"] = "changed"
        with self.assertRaises(RuntimeAdapterError):
            unit["slots"][0]["arm"] = "B"
        plan_copy = copy.deepcopy(self.plan)
        self.plan["campaign_id"] = "later-mutation"
        self.assertEqual(unit, materialize_paired_unit(plan_copy, block_id=self.by_order["AB"], repetition=1, runtime_parameters=RUNTIME))

    def test_orders_and_fail_closed_tuple(self):
        for order, arms in (("AB", ["A", "B"]), ("BA", ["B", "A"])):
            unit = self.unit(order)
            self.assertEqual([slot["period"] for slot in unit["slots"]], [1, 2])
            self.assertEqual([slot["arm"] for slot in unit["slots"]], arms)
        for overrides in ({"scenario": "S2"}, {"rq": "RQ4", "scenario": "S2"}, {"backend": "real"}):
            plan = make_plan(**overrides)
            with self.assertRaises(RuntimeAdapterError):
                self.unit(plan=plan)

    def test_dry_run_has_no_process_or_filesystem_effect(self):
        unit = self.unit()
        before = sorted(str(path.relative_to(self.results_root)) for path in self.results_root.rglob("*"))
        with mock.patch("subprocess.run") as run:
            description = dry_run_description(unit, self.plan)
        run.assert_not_called()
        self.assertTrue(description["dry_run"])
        self.assertEqual(before, sorted(str(path.relative_to(self.results_root)) for path in self.results_root.rglob("*")))

    def test_reservation_transition_and_sealed_spec_executor(self):
        unit = self.unit()
        sidecar = self.reserve(unit)
        self.preflight(sidecar)
        observed_fds = []

        def executor(argv, **kwargs):
            running = json.loads(sidecar.read_text())
            self.assertEqual(running["state"], "RUNNING")
            self.assertIn("pass_fds", kwargs)
            self.assertEqual(len(kwargs["pass_fds"]), 2)
            observed_fds.extend(kwargs["pass_fds"])
            spec_value = argv[argv.index("--spec") + 1]
            results_value = argv[argv.index("--results-root") + 1]
            self.assertTrue(spec_value.startswith("/proc/self/fd/"))
            self.assertTrue(results_value.startswith("/proc/self/fd/"))
            self.assertEqual(Path(spec_value).read_bytes(), SPEC_BYTES)
            Path(results_value, "inert-artifact.txt").write_text("inert", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="out", stderr="")

        record = self.inert_run(sidecar, unit, executor)
        self.assertEqual(record["state"], "SUCCEEDED")
        self.assertEqual(record["outcome"]["return_code"], 0)
        self.assertTrue(Path(record["native_results_root"], "inert-artifact.txt").is_file())
        for descriptor in observed_fds:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_native_results_descriptor_resists_synchronized_name_substitution(self):
        unit = self.unit()
        sidecar = self.reserve(unit, execution_id="S1-results-capability")
        self.preflight(sidecar)
        original_native = Path(json.loads(sidecar.read_text())["native_results_root"])
        authorized_object = original_native.with_name("native-authorized-object")
        external = Path(self.temporary.name, "external-redirection-target")
        external.mkdir()
        ready = threading.Barrier(2)
        replaced = threading.Barrier(2)
        observed = {}

        def attacker():
            ready.wait(timeout=5)
            original_native.rename(authorized_object)
            original_native.symlink_to(external, target_is_directory=True)
            replaced.wait(timeout=5)

        attacker_thread = threading.Thread(target=attacker)
        attacker_thread.start()

        def executor(argv, **kwargs):
            observed["pass_fds"] = tuple(kwargs["pass_fds"])
            observed["results_argument"] = argv[argv.index("--results-root") + 1]
            ready.wait(timeout=5)
            replaced.wait(timeout=5)
            Path(observed["results_argument"], "authorized.txt").write_text(
                "authorized", encoding="utf-8"
            )
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        record = self.inert_run(sidecar, unit, executor)
        attacker_thread.join(timeout=5)
        self.assertFalse(attacker_thread.is_alive())
        self.assertEqual(record["state"], "SUCCEEDED")
        self.assertTrue(observed["results_argument"].startswith("/proc/self/fd/"))
        self.assertIn(int(observed["results_argument"].rsplit("/", 1)[1]), observed["pass_fds"])
        self.assertTrue(authorized_object.joinpath("authorized.txt").is_file())
        self.assertEqual(list(external.iterdir()), [])
        persisted = json.loads(sidecar.with_name("attempt.json").read_text())
        self.assertIn("<VERIFIED_NATIVE_RESULTS_ROOT_FD>", persisted["invocation"])
        self.assertNotIn(str(original_native), persisted["invocation"])

    def test_native_results_descriptor_closes_after_executor_exception(self):
        unit = self.unit()
        sidecar = self.reserve(unit, execution_id="S1-results-exception")
        self.preflight(sidecar)
        observed = []

        def executor(_argv, **kwargs):
            observed.extend(kwargs["pass_fds"])
            raise RuntimeError("inert executor failure")

        record = self.inert_run(sidecar, unit, executor)
        self.assertEqual(record["state"], "FAILED")
        self.assertEqual(len(observed), 2)
        for descriptor in observed:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_attempt_timestamps_are_strict_utc_before_running(self):
        invalid_values = (
            "not-a-timestamp",
            "2026-01-02T03:04:05.000000",
            "2026-02-30T03:04:05.000000Z",
            "2026-01-02T03:04:05+00:00",
        )
        for index, invalid in enumerate(invalid_values):
            with self.subTest(value=invalid):
                root = Path(self.temporary.name, f"timestamp-results-{index}")
                root.mkdir()
                old = self.results_root
                self.results_root = root
                unit = self.unit()
                sidecar = self.reserve(unit, execution_id=f"S1-invalid-time-{index}")
                self.preflight(sidecar)
                record = json.loads(sidecar.read_text())
                record["timestamps"]["materialized_at_utc"] = invalid
                sidecar.write_text(json.dumps(record), encoding="utf-8")
                executor = mock.Mock()
                with self.assertRaisesRegex(RuntimeAdapterError, "INVALID_ATTEMPT_TIMESTAMP"):
                    self.inert_run(sidecar, unit, executor)
                executor.assert_not_called()
                self.assertEqual(json.loads(sidecar.read_text())["state"], "PREFLIGHT_PASSED")
                self.results_root = old

    def test_every_non_null_timestamp_is_validated_on_reread(self):
        unit = self.unit()
        cases = (
            ("preflight_passed_at_utc", "PREFLIGHT_PASSED"),
            ("running_at_utc", "RUNNING"),
            ("completed_at_utc", "FAILED"),
        )
        for index, (field, target_state) in enumerate(cases):
            with self.subTest(field=field):
                root = Path(self.temporary.name, f"timestamp-reread-{index}")
                root.mkdir()
                old = self.results_root
                self.results_root = root
                sidecar = self.reserve(unit, execution_id=f"S1-reread-time-{index}")
                self.preflight(sidecar)
                if target_state in {"RUNNING", "FAILED"}:
                    from l2i import runtime_adapter as adapter_module

                    validated = adapter_module._read_attempt(sidecar, root)
                    with adapter_module._verified_attempt_reservation(
                        root, validated["execution_id"]
                    ) as authority:
                        adapter_module._transition_validated_attempt(
                            validated,
                            sidecar,
                            "RUNNING",
                            results_root=root,
                            now=fixed_now,
                            running_authority=authority,
                        )
                if target_state == "FAILED":
                    transition_attempt(
                        sidecar, "FAILED", results_root=root, return_code=1, now=fixed_now
                    )
                record = json.loads(sidecar.read_text())
                record["timestamps"][field] = "2026-13-99T25:61:61.000000Z"
                sidecar.write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeAdapterError, "INVALID_ATTEMPT_TIMESTAMP"):
                    transition_attempt(
                        sidecar, "SUCCEEDED", results_root=root, return_code=0, now=fixed_now
                    )
                self.results_root = old

    def test_source_substitution_after_reservation_fails_before_running(self):
        unit = self.unit()
        sidecar = self.reserve(unit)
        self.preflight(sidecar)
        self.source_root.joinpath(SPEC_PATH).write_bytes(b"substituted")
        executor = mock.Mock()
        with self.assertRaisesRegex(RuntimeAdapterError, "SCENARIO_SPEC_HASH_MISMATCH"):
            self.inert_run(sidecar, unit, executor)
        executor.assert_not_called()
        self.assertEqual(json.loads(sidecar.read_text())["state"], "PREFLIGHT_PASSED")

    def test_tampered_sidecar_fields_fail_before_running_and_executor(self):
        fields = ("invocation", "materialized_unit_sha256", "campaign_id", "results_directory")
        for index, field in enumerate(fields):
            with self.subTest(field=field):
                root = Path(self.temporary.name, f"results-{index}")
                root.mkdir()
                old = self.results_root
                self.results_root = root
                unit = self.unit()
                sidecar = self.reserve(unit, execution_id=f"S1-tamper-{index}")
                self.preflight(sidecar)
                record = json.loads(sidecar.read_text())
                record[field] = ["tampered"] if field == "invocation" else "tampered"
                sidecar.write_text(json.dumps(record))
                executor = mock.Mock()
                with self.assertRaises(RuntimeAdapterError):
                    self.inert_run(sidecar, unit, executor)
                executor.assert_not_called()
                self.assertEqual(json.loads(sidecar.read_text())["state"], "PREFLIGHT_PASSED")
                self.results_root = old

    def test_post_binding_invocation_tamper_is_rejected(self):
        unit = self.unit()
        sidecar = self.reserve(unit, execution_id="S1-post-binding-invocation")
        self.preflight(sidecar)
        executor, _fds = self.post_binding_tamper(
            sidecar, unit, lambda record: record["invocation"].append("--tampered")
        )
        executor.assert_not_called()
        self.assertEqual(json.loads(sidecar.read_text())["state"], "PREFLIGHT_PASSED")

    def test_post_binding_provenance_tamper_is_rejected(self):
        unit = self.unit()
        sidecar = self.reserve(unit, execution_id="S1-post-binding-provenance")
        self.preflight(sidecar)
        executor, _fds = self.post_binding_tamper(
            sidecar, unit,
            lambda record: record["provenance"].__setitem__("hostname", "tampered-host"),
        )
        executor.assert_not_called()
        self.assertEqual(json.loads(sidecar.read_text())["state"], "PREFLIGHT_PASSED")

    def test_final_checkpoint_failure_closes_descriptors(self):
        unit = self.unit()
        sidecar = self.reserve(unit, execution_id="S1-post-binding-identity")
        self.preflight(sidecar)
        executor, observed_fds = self.post_binding_tamper(
            sidecar, unit,
            lambda record: record.__setitem__("execution_id", "S1-divergent-identity"),
        )
        executor.assert_not_called()
        self.assertEqual(len(observed_fds), 2)
        for descriptor in observed_fds:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        persisted = json.loads(sidecar.read_text())
        self.assertEqual(persisted["state"], "PREFLIGHT_PASSED")
        self.assertIsNone(persisted["outcome"])

    def test_final_checkpoint_nominal_transitions_running_once(self):
        unit = self.unit()
        sidecar = self.reserve(unit, execution_id="S1-final-checkpoint-nominal")
        self.preflight(sidecar)
        executor = mock.Mock(
            return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
        )
        from l2i import runtime_adapter as adapter_module

        observed_states = []
        original = adapter_module._transition_validated_attempt

        def observing_transition(record, *args, **kwargs):
            observed_states.append(kwargs.get("new_state", args[1] if len(args) > 1 else None))
            return original(record, *args, **kwargs)

        with mock.patch.object(
            adapter_module, "_transition_validated_attempt", observing_transition
        ):
            final = self.inert_run(sidecar, unit, executor)
        self.assertEqual(final["state"], "SUCCEEDED")
        self.assertEqual(observed_states.count("RUNNING"), 1)
        executor.assert_called_once()
        self.assertEqual(len(executor.call_args.kwargs["pass_fds"]), 2)

    def test_public_transition_rejects_zero_attempt_without_rewrite(self):
        unit = self.unit()
        sidecar = self.reserve(unit, execution_id="S1-public-zero-attempt")
        self.preflight(sidecar)
        record = json.loads(sidecar.read_text())
        record["attempt_number"] = 0
        sidecar.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        before = sidecar.read_bytes()
        with self.assertRaisesRegex(RuntimeAdapterError, "INVALID_ATTEMPT_SCHEMA"):
            transition_attempt(sidecar, "RUNNING", results_root=self.results_root)
        self.assertEqual(sidecar.read_bytes(), before)
        self.assertEqual(json.loads(before)["state"], "PREFLIGHT_PASSED")

    def test_public_transition_rejects_empty_invocation_without_rewrite(self):
        unit = self.unit()
        sidecar = self.reserve(unit, execution_id="S1-public-empty-invocation")
        self.preflight(sidecar)
        record = json.loads(sidecar.read_text())
        record["invocation"] = []
        sidecar.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        before = sidecar.read_bytes()
        with self.assertRaisesRegex(RuntimeAdapterError, "INVALID_ATTEMPT_SCHEMA"):
            transition_attempt(sidecar, "RUNNING", results_root=self.results_root)
        self.assertEqual(sidecar.read_bytes(), before)
        self.assertEqual(json.loads(before)["state"], "PREFLIGHT_PASSED")

    def test_public_transition_nominal_validates_and_persists_once(self):
        unit = self.unit()
        sidecar = self.reserve(unit, execution_id="S1-public-nominal")
        from l2i import runtime_adapter as adapter_module

        writes = []
        original = adapter_module._atomic_write_json_at

        def observing_write(directory_fd, filename, value, **kwargs):
            writes.append(copy.deepcopy(value))
            return original(directory_fd, filename, value, **kwargs)

        with mock.patch.object(adapter_module, "_atomic_write_json_at", observing_write):
            preflight = transition_attempt(
                sidecar, "PREFLIGHT_PASSED", results_root=self.results_root, now=fixed_now
            )
        self.assertEqual(len(writes), 1)
        self.assertEqual(preflight["state"], "PREFLIGHT_PASSED")
        self.assertEqual(preflight["attempt_number"], 1)
        schema = json.loads(
            Path(__file__).resolve().parents[1]
            .joinpath("schemas/phase19/execution-attempt-v1.schema.json")
            .read_text()
        )
        self.assertEqual(
            list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(preflight)),
            [],
        )
        adapter_module._validate_attempt_record(copy.deepcopy(preflight))

    def test_dispatch_rejects_structural_sidecar_without_effect(self):
        unit = self.unit()
        sidecar = self.reserve(unit, execution_id="S1-dispatch-structural-invalid")
        self.preflight(sidecar)
        record = json.loads(sidecar.read_text())
        record["attempt_number"] = 0
        sidecar.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        before = sidecar.read_bytes()
        executor = mock.Mock()
        with self.assertRaisesRegex(RuntimeAdapterError, "INVALID_ATTEMPT_SCHEMA"):
            self.inert_run(sidecar, unit, executor)
        executor.assert_not_called()
        self.assertEqual(sidecar.read_bytes(), before)
        self.assertEqual(json.loads(before)["state"], "PREFLIGHT_PASSED")

    def test_legacy_attempt_is_rejected_before_effect(self):
        unit = self.unit()
        sidecar = self.reserve(unit)
        self.preflight(sidecar)
        record = json.loads(sidecar.read_text())
        record.pop("materialized_unit_sha256")
        sidecar.write_text(json.dumps(record))
        executor = mock.Mock()
        with self.assertRaises(RuntimeAdapterError):
            self.inert_run(sidecar, unit, executor)
        executor.assert_not_called()

    def test_tampered_plan_and_unit_fail_before_running(self):
        unit = self.unit()
        for kind in ("plan", "unit"):
            root = Path(self.temporary.name, f"context-{kind}")
            root.mkdir()
            old = self.results_root
            self.results_root = root
            sidecar = self.reserve(unit, execution_id=f"S1-{kind}-tamper")
            self.preflight(sidecar)
            executor = mock.Mock()
            candidate_plan = copy.deepcopy(self.plan)
            candidate_unit = unit.to_dict()
            if kind == "plan":
                candidate_plan["randomization_manifest_sha256"] = "0" * 64
            else:
                candidate_unit["slots"][0]["assignment_sha256"] = "0" * 64
            with self.assertRaises(Exception):
                run_attempt_with_executor(
                    sidecar,
                    executor=executor,
                    cwd=self.source_root,
                    plan=candidate_plan,
                    unit=candidate_unit,
                    results_root=self.results_root,
                    source_root=self.source_root,
                    provenance_provider=lambda _cwd: provenance(),
                )
            executor.assert_not_called()
            self.assertEqual(json.loads(sidecar.read_text())["state"], "PREFLIGHT_PASSED")
            self.results_root = old

    def test_executor_failure_and_interrupt_are_terminal(self):
        unit = self.unit()
        cases = (("nonzero", 9, "FAILED"), ("exception", None, "FAILED"), ("interrupt", None, "INTERRUPTED"))
        for index, (kind, rc, expected) in enumerate(cases):
            root = Path(self.temporary.name, f"terminal-{index}")
            root.mkdir()
            old = self.results_root
            self.results_root = root
            sidecar = self.reserve(unit, execution_id=f"S1-terminal-{index}")
            self.preflight(sidecar)

            def executor(argv, **_kwargs):
                if kind == "exception":
                    raise RuntimeError("inert failure")
                if kind == "interrupt":
                    raise KeyboardInterrupt
                return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

            record = self.inert_run(sidecar, unit, executor)
            self.assertEqual(record["state"], expected)
            self.assertNotEqual(record["outcome"]["return_code"], 0)
            self.results_root = old

    def test_global_execution_id_reservation_is_atomic_across_slots(self):
        unit = self.unit()
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def factory(_scenario):
            barrier.wait(timeout=5)
            return "S1-global-collision"

        def worker(slot_index):
            try:
                reserve_attempt(
                    unit,
                    self.plan,
                    run_slot_id=unit["slots"][slot_index]["run_slot_id"],
                    results_root=self.results_root,
                    source_root=self.source_root,
                    provenance=provenance(),
                    execution_id_factory=factory,
                    now=fixed_now,
                )
                outcomes.append("won")
            except RuntimeAdapterError:
                outcomes.append("lost")

        threads = [threading.Thread(target=worker, args=(index,)) for index in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(outcomes), ["lost", "won"])
        self.assertEqual(len(list(self.results_root.glob("phase19-execution-id-S1-global-collision"))), 1)
        self.assertEqual(len(list(self.results_root.rglob("S1-global-collision"))), 1)

    def test_schema_states_and_persisted_fixtures(self):
        repository = Path(__file__).resolve().parents[1]
        cases = (
            ("materialized-experimental-unit-v1", "unit"),
            ("execution-attempt-v1", "attempt"),
        )
        for name, key in cases:
            schema = json.loads(repository.joinpath(f"schemas/phase19/{name}.schema.json").read_text())
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            directory = repository / f"schemas/phase19/fixtures/{name}"
            positive = json.loads(next(directory.glob("valid-*.json")).read_text())[key]
            self.assertEqual(list(validator.iter_errors(positive)), [])
            negative = json.loads(next(directory.glob("invalid-*.json")).read_text())
            mutated = {key: copy.deepcopy(positive)}
            segments = negative["mutation"]["path"].strip("/").split("/")[1:]
            target = mutated[key]
            for segment in segments[:-1]:
                target = target[int(segment)] if isinstance(target, list) else target[segment]
            final = segments[-1]
            if negative["mutation"]["operation"] == "remove":
                target.pop(int(final) if isinstance(target, list) else final)
            else:
                target[int(final) if isinstance(target, list) else final] = negative["mutation"]["value"]
            self.assertNotEqual(list(validator.iter_errors(mutated[key])), [])
        unit = self.unit().to_dict()
        fixture_unit = json.loads(repository.joinpath("schemas/phase19/fixtures/materialized-experimental-unit-v1/valid-pilot-rq3-s1-mock-ab.json").read_text())["unit"]
        self.assertEqual(fixture_unit, unit)
        attempt_schema = json.loads(repository.joinpath("schemas/phase19/execution-attempt-v1.schema.json").read_text())
        validator = Draft202012Validator(attempt_schema, format_checker=FormatChecker())
        materialized = json.loads(repository.joinpath("schemas/phase19/fixtures/execution-attempt-v1/valid-materialized-attempt.json").read_text())["attempt"]
        invalid_states = []
        with_outcome = copy.deepcopy(materialized)
        with_outcome["outcome"] = {"return_code": 0, "artifact_hashes": {}, "readbacks": []}
        invalid_states.append(with_outcome)
        succeeded_bad_rc = copy.deepcopy(materialized)
        succeeded_bad_rc["state"] = "SUCCEEDED"
        succeeded_bad_rc["timestamps"].update({
            "preflight_passed_at_utc": "2026-01-02T03:04:06.000000Z",
            "running_at_utc": "2026-01-02T03:04:07.000000Z",
            "completed_at_utc": "2026-01-02T03:04:08.000000Z",
        })
        succeeded_bad_rc["outcome"] = {"return_code": 2, "artifact_hashes": {}, "readbacks": []}
        invalid_states.append(succeeded_bad_rc)
        for candidate in invalid_states:
            self.assertNotEqual(list(validator.iter_errors(candidate)), [])

    def test_attempt_fixture_matches_canonical_record(self):
        unit = self.unit()
        sidecar = self.reserve(unit, execution_id="S1-fixture-execution")
        actual = json.loads(sidecar.read_text())
        root = str(self.results_root)
        for field in ("results_directory", "native_results_root"):
            actual[field] = actual[field].replace(root, "/RESULTS_ROOT")
        actual["invocation"] = [value.replace(root, "/RESULTS_ROOT") for value in actual["invocation"]]
        repository = Path(__file__).resolve().parents[1]
        expected = json.loads(repository.joinpath("schemas/phase19/fixtures/execution-attempt-v1/valid-materialized-attempt.json").read_text())["attempt"]
        self.assertEqual(actual, expected)

    def test_dispatch_rejects_missing_global_reservation(self):
        unit = self.unit()
        execution_id = "S1-reservation-missing"
        sidecar = self.reserve(unit, execution_id=execution_id)
        self.preflight(sidecar)
        self.reservation_path(execution_id).unlink()
        self.assert_reservation_rejected(sidecar, unit)

    def test_dispatch_rejects_divergent_global_reservation_content(self):
        unit = self.unit()
        execution_id = "S1-reservation-divergent"
        sidecar = self.reserve(unit, execution_id=execution_id)
        self.preflight(sidecar)
        self.reservation_path(execution_id).write_bytes(b"different-execution\n")
        self.assert_reservation_rejected(sidecar, unit)

    def test_dispatch_rejects_symlink_global_reservation(self):
        unit = self.unit()
        execution_id = "S1-reservation-symlink"
        sidecar = self.reserve(unit, execution_id=execution_id)
        self.preflight(sidecar)
        marker = self.reservation_path(execution_id)
        marker.unlink()
        target = self.results_root / "reservation-target"
        target.write_text(execution_id + "\n", encoding="ascii")
        marker.symlink_to(target)
        self.assert_reservation_rejected(sidecar, unit)

    def test_dispatch_rejects_nonregular_global_reservation(self):
        unit = self.unit()
        execution_id = "S1-reservation-directory"
        sidecar = self.reserve(unit, execution_id=execution_id)
        self.preflight(sidecar)
        marker = self.reservation_path(execution_id)
        marker.unlink()
        marker.mkdir()
        self.assert_reservation_rejected(sidecar, unit)

    def test_dispatch_rejects_synchronized_reservation_replacement(self):
        unit = self.unit()
        execution_id = "S1-reservation-replaced"
        sidecar = self.reserve(unit, execution_id=execution_id)
        self.preflight(sidecar)
        marker = self.reservation_path(execution_id)
        displaced = marker.with_name(marker.name + "-displaced")
        executor = mock.Mock()
        observed_fds = []
        from l2i import runtime_adapter as adapter_module

        original_validate = adapter_module._validate_reservation_capability
        original_bind = adapter_module._bind_verified_descriptors

        def observing_validate(directory_fd, reservation_fd, name, expected):
            observed_fds.append(reservation_fd)
            return original_validate(directory_fd, reservation_fd, name, expected)

        def replacing_bind(logical_invocation, **kwargs):
            bound = original_bind(logical_invocation, **kwargs)
            marker.rename(displaced)
            marker.write_text(execution_id + "\n", encoding="ascii")
            return bound

        before = sidecar.read_bytes()
        with mock.patch.object(
            adapter_module, "_validate_reservation_capability", observing_validate
        ), mock.patch.object(adapter_module, "_bind_verified_descriptors", replacing_bind):
            with self.assertRaisesRegex(RuntimeAdapterError, "INVALID_ATTEMPT_RESERVATION"):
                self.inert_run(sidecar, unit, executor)
        executor.assert_not_called()
        self.assertEqual(sidecar.read_bytes(), before)
        self.assertGreaterEqual(len(observed_fds), 2)
        for descriptor in set(observed_fds):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_reservation_capability_lives_through_nominal_dispatch_and_closes(self):
        unit = self.unit()
        execution_id = "S1-reservation-lifecycle"
        sidecar = self.reserve(unit, execution_id=execution_id)
        self.preflight(sidecar)
        observed_fds = []
        from l2i import runtime_adapter as adapter_module

        original = adapter_module._validate_reservation_capability

        def observing_validate(directory_fd, reservation_fd, name, expected):
            observed_fds.append(reservation_fd)
            return original(directory_fd, reservation_fd, name, expected)

        def executor(argv, **kwargs):
            self.assertEqual(json.loads(sidecar.read_text())["state"], "RUNNING")
            reservation_fd = observed_fds[-1]
            self.assertTrue(os.fstat(reservation_fd))
            self.assertNotIn(reservation_fd, kwargs["pass_fds"])
            self.assertEqual(len(kwargs["pass_fds"]), 2)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(
            adapter_module, "_validate_reservation_capability", observing_validate
        ):
            final = self.inert_run(sidecar, unit, executor)
        self.assertEqual(final["state"], "SUCCEEDED")
        self.assertGreaterEqual(len(observed_fds), 2)
        for descriptor in set(observed_fds):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_reservation_capability_closes_after_inert_executor_failure(self):
        unit = self.unit()
        execution_id = "S1-reservation-failure"
        sidecar = self.reserve(unit, execution_id=execution_id)
        self.preflight(sidecar)
        observed_fds = []
        executor_calls = []
        from l2i import runtime_adapter as adapter_module

        original = adapter_module._validate_reservation_capability

        def observing_validate(directory_fd, reservation_fd, name, expected):
            observed_fds.append(reservation_fd)
            return original(directory_fd, reservation_fd, name, expected)

        def executor(_argv, **kwargs):
            executor_calls.append(1)
            self.assertTrue(os.fstat(observed_fds[-1]))
            self.assertNotIn(observed_fds[-1], kwargs["pass_fds"])
            raise RuntimeError("controlled inert failure")

        with mock.patch.object(
            adapter_module, "_validate_reservation_capability", observing_validate
        ):
            final = self.inert_run(sidecar, unit, executor)
        self.assertEqual(executor_calls, [1])
        self.assertEqual(final["state"], "FAILED")
        self.assertEqual(final["outcome"]["return_code"], 1)
        for descriptor in set(observed_fds):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_public_transition_rejects_running_without_reservation_authority(self):
        unit = self.unit()
        execution_id = "S1-public-running-no-reservation"
        sidecar = self.reserve(unit, execution_id=execution_id)
        self.preflight(sidecar)
        self.reservation_path(execution_id).unlink()
        before = sidecar.read_bytes()
        with self.assertRaisesRegex(
            RuntimeAdapterError, "RUNNING_REQUIRES_DISPATCH_AUTHORITY"
        ):
            transition_attempt(sidecar, "RUNNING", results_root=self.results_root)
        self.assertEqual(sidecar.read_bytes(), before)
        self.assertFalse(self.reservation_path(execution_id).exists())

    def test_public_transition_rejects_running_with_valid_reservation(self):
        unit = self.unit()
        execution_id = "S1-public-running-valid-reservation"
        sidecar = self.reserve(unit, execution_id=execution_id)
        self.preflight(sidecar)
        marker = self.reservation_path(execution_id)
        before_sidecar = sidecar.read_bytes()
        before_marker = marker.read_bytes()
        with self.assertRaisesRegex(
            RuntimeAdapterError, "RUNNING_REQUIRES_DISPATCH_AUTHORITY"
        ):
            transition_attempt(sidecar, "RUNNING", results_root=self.results_root)
        self.assertEqual(sidecar.read_bytes(), before_sidecar)
        self.assertEqual(marker.read_bytes(), before_marker)

    def test_internal_running_transition_rejects_invalid_authority_variants(self):
        unit = self.unit()
        from l2i import runtime_adapter as adapter_module

        variants = ("missing", "forged", "closed", "other-attempt")
        for index, variant in enumerate(variants):
            with self.subTest(variant=variant):
                root = Path(self.temporary.name, f"authority-variant-{index}")
                root.mkdir()
                old = self.results_root
                self.results_root = root
                try:
                    execution_id = f"S1-authority-target-{index}"
                    sidecar = self.reserve(unit, execution_id=execution_id)
                    self.preflight(sidecar)
                    validated = adapter_module._read_attempt(sidecar, root)
                    before = sidecar.read_bytes()

                    if variant == "missing":
                        authority = None
                        context = contextlib.nullcontext()
                    elif variant == "forged":
                        authority = object.__new__(adapter_module._RunningDispatchAuthority)
                        context = contextlib.nullcontext()
                    elif variant == "closed":
                        with adapter_module._verified_attempt_reservation(
                            root, execution_id
                        ) as closed_authority:
                            pass
                        authority = closed_authority
                        context = contextlib.nullcontext()
                    else:
                        other_sidecar = self.reserve(
                            unit, slot_index=1, execution_id=f"S1-authority-other-{index}"
                        )
                        other_record = json.loads(other_sidecar.read_text())
                        context = adapter_module._verified_attempt_reservation(
                            root, other_record["execution_id"]
                        )
                        authority = None

                    with context as active_authority:
                        selected = active_authority if active_authority is not None else authority
                        with self.assertRaisesRegex(
                            RuntimeAdapterError, "RUNNING_REQUIRES_DISPATCH_AUTHORITY"
                        ):
                            adapter_module._transition_validated_attempt(
                                validated,
                                sidecar,
                                "RUNNING",
                                results_root=root,
                                now=fixed_now,
                                running_authority=selected,
                            )
                    self.assertEqual(sidecar.read_bytes(), before)
                    self.assertEqual(json.loads(before)["state"], "PREFLIGHT_PASSED")
                finally:
                    self.results_root = old

    def test_executor_is_sole_authorized_running_route(self):
        unit = self.unit()
        execution_id = "S1-sole-running-route"
        sidecar = self.reserve(unit, execution_id=execution_id)
        self.preflight(sidecar)
        from l2i import runtime_adapter as adapter_module

        transitions = []
        reservation_fds = []
        executor_calls = []
        original_transition = adapter_module._transition_validated_attempt
        original_reservation_validation = adapter_module._validate_reservation_capability

        def observing_transition(record, sidecar_path, new_state, **kwargs):
            transitions.append(new_state)
            return original_transition(record, sidecar_path, new_state, **kwargs)

        def observing_reservation(directory_fd, reservation_fd, name, expected):
            reservation_fds.append(reservation_fd)
            return original_reservation_validation(
                directory_fd, reservation_fd, name, expected
            )

        def executor(argv, **kwargs):
            executor_calls.append(1)
            self.assertEqual(json.loads(sidecar.read_text())["state"], "RUNNING")
            self.assertEqual(transitions.count("RUNNING"), 1)
            self.assertTrue(os.fstat(reservation_fds[-1]))
            self.assertNotIn(reservation_fds[-1], kwargs["pass_fds"])
            self.assertEqual(len(kwargs["pass_fds"]), 2)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(
            adapter_module, "_transition_validated_attempt", observing_transition
        ), mock.patch.object(
            adapter_module, "_validate_reservation_capability", observing_reservation
        ):
            final = self.inert_run(sidecar, unit, executor)
        self.assertEqual(executor_calls, [1])
        self.assertEqual(transitions, ["RUNNING", "SUCCEEDED"])
        self.assertEqual(final["state"], "SUCCEEDED")
        self.assertEqual(final["outcome"]["return_code"], 0)
        for descriptor in set(reservation_fds):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_no_scenario_router_or_backend_imports(self):
        probe = (
            "import json,sys; import l2i.runtime_adapter; "
            "print(json.dumps(sorted(n for n in sys.modules if "
            "n == 'scenarios' or n.startswith('scenarios.') or "
            "n == 'l2i.backends' or n.startswith('l2i.backends.'))))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), [])


if __name__ == "__main__":
    unittest.main()
