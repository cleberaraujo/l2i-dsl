from __future__ import annotations

import argparse
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from l2i.s2_recovery_observation import (
    EXCLUDED_REQUIREMENTS,
    PREDICATE_ID,
    SCHEMA_ID,
    build_recovery_observation,
    percentile_type7,
)
from scenarios.multidomain_s2 import (
    CHILD_LIFETIME_MARGIN_S,
    GROUP,
    P4_FUNCTIONAL_TOKEN,
    READINESS_TIMEOUT_S,
    build_parser,
    cleanup_processes,
    collect_required_readbacks,
    emit_success,
    evaluate_readbacks,
    expected_packet_count,
    finalize_runtime,
    finalize_or_raise,
    main as s2_main,
    new_p4_apply_state,
    normalize_timeout_text,
    parse_spec_json,
    positive_float,
    program_p4,
    reserve_output_directory,
    resolve_output_directory,
    source_address_evidence,
    validate_cli_parameters,
    validate_execution_id,
    validate_spec,
    validate_terminal_artifacts,
    wait_for_required_readbacks,
)

ROOT = Path(__file__).resolve().parents[1]
RECEIVERS = ["B:h3", "C:h4"]
HISTORICAL_HASHES = {
    "scenarios/multicast_s2_recovery_stable5.py": "7784549668708bd717123a3145e72eadd3c01682df7bd9f3f5a86bae3f2508f1",
    "scenarios/multicast_s2.py": "e6c6caae1cfe5871ebf40e62d93a8f8d4dfc39047c0afae3709a191116694322",
    "scenarios/multicast_s2_with_phases.py": "3753bedd418ebb2ba063102b9ee5cea829a391058bcedc7a0fbf69f4a2634634",
    "scenarios/multicast_s2_with_recovery.py": "efd595cd105bb3fc5d4804f27f6ecf132bfc1b36ac061108c904c4e6a1b51649",
    "scenarios/multicast_s2_with_recovery_stable.py": "159c94c992007dd771239f42f107d6f4217fbeeeb07651723e782898de08a96a",
    "scenarios/multicast_s2_with_recovery_stable2.py": "dc90e311c77d914112663e0f5bd831862a67ca05aac1590d01ec8e9e40c140d4",
    "scenarios/multicast_s2_with_recovery_stable3.py": "c1fc1232ccec4a574478965e1718d42ee4bf848994bf826cb3a5324efb660173",
}


def valid_spec() -> dict:
    return {
        "flow_id": "S2_SourceOrientedMulticast",
        "endpoints": {
            "source": {"domain": "A", "host": "h1"},
            "receivers": [
                {"domain": "B", "host": "h3"},
                {"domain": "C", "host": "h4"},
            ],
        },
        "multicast": {"enabled": True, "group": "G1", "tree": "SPT"},
        "bandwidth": {"min_mbps": 2, "max_mbps": 5},
        "priority": "medium",
        "latency": {"max_ms": 40, "percentile": "P99"},
    }


def recovery_fixture(
    *, stop_offset_ms: int = 350, duplicate: bool = False,
    omit_sequence: int | None = None, include_rejoin: bool = True,
):
    origin = 1_000_000_000
    raw = []
    for index, offset_ms in enumerate((10, 20, 110, 120, 210, 220), start=1):
        sent_ns = origin + offset_ms * 1_000_000
        raw.append({"event": "sent", "sequence": index, "sent_ns": sent_ns})
        for receiver in RECEIVERS:
            if index == omit_sequence and receiver == "B:h3":
                continue
            row = {
                "event": "received", "sequence": index, "endpoint": receiver,
                "sent_ns": sent_ns, "received_ns": sent_ns + 1_000_000,
            }
            raw.append(row)
            if duplicate and index == 1 and receiver == "B:h3":
                raw.append(dict(row, received_ns=sent_ns + 2_000_000))
    membership = []
    for receiver in RECEIVERS:
        if include_rejoin:
            membership.append({"event": "membership_rejoin", "endpoint": receiver, "observed_ns": origin})
        membership.append({
            "event": "receiver_stop", "endpoint": receiver,
            "observed_ns": origin + stop_offset_ms * 1_000_000,
        })
    return raw, membership


def complete_readback(**changes):
    running = {
        name: {"state": "running", "returncode": None}
        for name in ("receiver:B:h3", "receiver:C:h4", "probe:h2")
    }
    source_address_json = json.dumps([{
        "ifindex": 2, "ifname": "h1-eth0", "flags": ["BROADCAST", "UP", "LOWER_UP"],
        "addr_info": [{"family": "inet", "local": "10.0.0.1", "prefixlen": 24, "scope": "global"}],
    }])
    command_results = {
        "source_address": {"argv": ["address"], "returncode": 0, "stdout": source_address_json, "stderr": "", "timed_out": False, "error": None},
        "receiver:B:h3": {"argv": ["maddr", "h3"], "returncode": 0, "stdout": f"1: lo\n    inet 224.0.0.1\n2: eth0\n    inet {GROUP} users 1\n", "stderr": "", "timed_out": False, "error": None},
        "receiver:C:h4": {"argv": ["maddr", "h4"], "returncode": 0, "stdout": f"2: eth0\n    inet {GROUP}\n", "stderr": "", "timed_out": False, "error": None},
        "probe:h2": {"argv": ["maddr", "h2"], "returncode": 0, "stdout": "1: lo\n    inet 224.0.0.1\n", "stderr": "", "timed_out": False, "error": None},
    }
    values = {
        "source_address_json": source_address_json,
        "receiver_membership": {
            "B:h3": f"1: lo\n    inet 224.0.0.1\n2: eth0\n    inet {GROUP} users 1\n",
            "C:h4": f"2: eth0\n    inet {GROUP}\n",
        },
        "probe_membership": "1: lo\n    inet 224.0.0.1\n",
        "backend": "real",
        "p4_stdout": P4_FUNCTIONAL_TOKEN + "\n",
        "process_liveness_before": copy.deepcopy(running),
        "process_liveness_after": copy.deepcopy(running),
        "readback_window_started_ns": 100,
        "readback_window_completed_ns": 200,
        "command_results": command_results,
    }
    values.update(changes)
    result = evaluate_readbacks(**values)
    result["readiness"] = {
        "clock": "monotonic", "deadline_seconds": 3.0,
        "poll_interval_seconds": 0.05, "attempt_count": 1,
        "attempts": [{
            "attempt": 1, "started_monotonic": 10.0,
            "completed_monotonic": 10.1, "valid": result["valid"],
            "checks": copy.deepcopy(result["checks"]),
        }],
        "ready": result["valid"], "timed_out": not result["valid"],
    }
    return result


class FakeProcess:
    def __init__(self, *, running=True, stubborn=False, timeout_once=False, cleanup_error=None):
        self.returncode = None if running else 0
        self.stubborn = stubborn
        self.timeout_once = timeout_once
        self.cleanup_error = cleanup_error
        self.actions = []

    def poll(self):
        self.actions.append("poll")
        return self.returncode

    def terminate(self):
        self.actions.append("terminate")
        if not self.stubborn:
            self.returncode = -15

    def kill(self):
        self.actions.append("kill")
        self.returncode = -9

    def communicate(self, timeout=None):
        self.actions.append(("communicate", timeout))
        if self.cleanup_error is not None:
            raise self.cleanup_error
        if self.timeout_once:
            self.timeout_once = False
            raise subprocess.TimeoutExpired("fake", timeout)
        if self.returncode is None:
            self.returncode = 0
        return ("captured stdout", "captured stderr")


class SequencedProcess:
    def __init__(self, returncodes):
        self.returncodes = list(returncodes)
        self.index = 0

    def poll(self):
        value = self.returncodes[min(self.index, len(self.returncodes) - 1)]
        self.index += 1
        return value


class FiniteCliAndSpecTests(unittest.TestCase):
    def test_default_cardinality_and_one_ms_units(self):
        self.assertEqual(expected_packet_count(30, 50), 600)
        self.assertEqual(expected_packet_count(1, 1), 1000)
        self.assertEqual(expected_packet_count(1.001, 1), 1001)

    def test_nonfinite_cli_duration_rejected(self):
        for value in ("nan", "NaN", "inf", "-inf", "Infinity", "1e9999"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                positive_float(value)

    def test_cli_defaults_zero_negative_and_invalid_combination(self):
        parser = build_parser()
        common = [
            "--spec", "s.json", "--execution-id", "x", "--repetition", "1",
            "--results-root", "r", "--backend", "mock", "--mode", "adapt",
            "--duration", "30",
        ]
        args = parser.parse_args(common)
        self.assertEqual((args.packet_interval_ms, args.recovery_bin_ms, args.stable_k_bins), (50, 500, 3))
        validate_cli_parameters(args)
        for flag in ("--packet-interval-ms", "--recovery-bin-ms", "--stable-k-bins"):
            for value in ("0", "-1"):
                with self.assertRaises(SystemExit):
                    parser.parse_args(common + [flag, value])
        invalid = parser.parse_args(common + ["--packet-interval-ms", "100", "--recovery-bin-ms", "50"])
        with self.assertRaises(ValueError):
            validate_cli_parameters(invalid)

    def test_decimal_overflow_is_controlled(self):
        for value in (float("inf"), 1e308):
            with self.assertRaises(ValueError):
                expected_packet_count(value, 1)

    def test_nonstandard_json_constants_rejected(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaises(ValueError):
                parse_spec_json('{"value": ' + constant + "}")

    def test_spec_nonfinite_values_and_strings_rejected(self):
        fields = [
            (("latency", "max_ms"), float("nan")),
            (("latency", "max_ms"), "Infinity"),
            (("bandwidth", "min_mbps"), float("-inf")),
            (("bandwidth", "max_mbps"), "1e9999"),
            (("priority",), float("inf")),
            (("priority",), "NaN"),
        ]
        for path, value in fields:
            document = copy.deepcopy(valid_spec())
            target = document
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path, value=value), self.assertRaises(ValueError):
                validate_spec(document)

    def test_boolean_numeric_priority_rejected(self):
        document = valid_spec()
        document["priority"] = True
        with self.assertRaises(ValueError):
            validate_spec(document)

    def test_canonical_real_json_spec_from_stage(self):
        path = ROOT / "specs/valid/s2_multicast_source_oriented.json"
        document = parse_spec_json(path.read_text(encoding="utf-8"))
        validated = validate_spec(document)
        self.assertEqual(validated["bandwidth"], {"min_mbps": 2.0, "max_mbps": 5.0})
        self.assertEqual(validated["latency"], {"percentile": "P99", "max_ms": 40.0})

    def test_binding_is_fail_closed(self):
        bad = valid_spec()
        bad["endpoints"]["receivers"][1]["host"] = "h5"
        with self.assertRaises(ValueError):
            validate_spec(bad)


class ExactReadbackTests(unittest.TestCase):
    def test_timeout_text_normalizes_none_and_preserves_str(self):
        self.assertEqual(normalize_timeout_text(None), "")
        value = "texto"
        self.assertIs(normalize_timeout_text(value), value)

    def test_timeout_text_decodes_utf8_bytes(self):
        self.assertEqual(normalize_timeout_text("texto ç".encode()), "texto ç")

    def test_timeout_text_replaces_invalid_utf8(self):
        normalized = normalize_timeout_text(b"before\xffafter")
        self.assertEqual(normalized, "before\ufffdafter")

    def test_timeout_text_unexpected_type_fails_closed(self):
        with self.assertRaisesRegex(TypeError, "str, bytes, or None"):
            normalize_timeout_text(42)

    def test_complete_exact_readback_passes_and_is_structured(self):
        result = complete_readback()
        self.assertTrue(result["valid"])
        self.assertEqual(result["schema"], "l2i-s2-readbacks-v3")
        self.assertTrue(result["collected_while_receivers_active"])
        self.assertTrue(result["checks"]["all_receivers_have_exact_group"])

    def test_nonempty_output_without_group_fails(self):
        result = complete_readback(receiver_membership={"B:h3": "inet 224.0.0.1\n", "C:h4": "inet 224.0.0.1\n"})
        self.assertFalse(result["valid"])

    def test_only_one_receiver_with_group_fails(self):
        result = complete_readback(receiver_membership={"B:h3": f"inet {GROUP}\n", "C:h4": "inet 224.0.0.1\n"})
        self.assertFalse(result["valid"])

    def test_group_present_on_probe_fails(self):
        result = complete_readback(probe_membership=f"inet {GROUP}\n")
        self.assertFalse(result["valid"])

    def test_real_backend_without_p4_token_fails(self):
        result = complete_readback(p4_stdout="nonempty but wrong")
        self.assertFalse(result["valid"])

    def test_temporally_late_collection_fails(self):
        exited = copy.deepcopy(complete_readback()["process_liveness_after"])
        exited["probe:h2"] = {"state": "exited", "returncode": 0}
        result = complete_readback(process_liveness_after=exited)
        self.assertFalse(result["valid"])

    def test_p4_token_exact_stripped_line_passes(self):
        self.assertTrue(complete_readback(p4_stdout=f"noise\n  {P4_FUNCTIONAL_TOKEN}  \nmore")["valid"])

    def test_p4_token_substring_variants_fail(self):
        for text in (
            "NOT_" + P4_FUNCTIONAL_TOKEN,
            P4_FUNCTIONAL_TOKEN + "_EXTRA",
            "sentence " + P4_FUNCTIONAL_TOKEN + " suffix",
            "",
        ):
            with self.subTest(text=text):
                self.assertFalse(complete_readback(p4_stdout=text)["valid"])

    def test_mock_backend_explicitly_bypasses_p4_token(self):
        self.assertTrue(complete_readback(backend="mock", p4_stdout="")["valid"])

    def test_liveness_before_exit_fails(self):
        exited = copy.deepcopy(complete_readback()["process_liveness_before"])
        exited["receiver:B:h3"] = {"state": "exited", "returncode": 7}
        self.assertFalse(complete_readback(process_liveness_before=exited)["valid"])

    def test_transition_during_readbacks_fails(self):
        processes = {
            "receiver:B:h3": SequencedProcess([None, None]),
            "receiver:C:h4": SequencedProcess([None, None]),
            "probe:h2": SequencedProcess([None, 0]),
        }
        responses = [
            subprocess.CompletedProcess([], 0, complete_readback()["raw"]["source_address_json"], ""),
            subprocess.CompletedProcess([], 0, f"inet {GROUP}\n", ""),
            subprocess.CompletedProcess([], 0, f"inet {GROUP}\n", ""),
            subprocess.CompletedProcess([], 0, "inet 224.0.0.1\n", ""),
        ]
        result = collect_required_readbacks(
            processes=processes, backend="mock", p4_stdout="",
            command_runner=lambda *args, **kwargs: responses.pop(0),
            clock=iter((100, 200)).__next__,
        )
        self.assertFalse(result["valid"])
        self.assertEqual(result["process_liveness_after"]["probe:h2"]["state"], "exited")

    def test_all_active_both_sides_and_commands_preserved(self):
        processes = {name: SequencedProcess([None, None]) for name in (
            "receiver:B:h3", "receiver:C:h4", "probe:h2")}
        outputs = iter((complete_readback()["raw"]["source_address_json"], f"inet {GROUP}\n", f"inet {GROUP}\n", "inet 224.0.0.1\n"))
        result = collect_required_readbacks(
            processes=processes, backend="mock", p4_stdout="",
            command_runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, next(outputs), ""),
            clock=iter((10, 20)).__next__, timeout_s=0.25,
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["readback_window_started_ns"], 10)
        self.assertEqual(result["readback_window_completed_ns"], 20)
        self.assertEqual(len(result["raw"]["commands"]), 4)

    def test_readback_timeout_is_recorded_and_fails(self):
        processes = {name: SequencedProcess([None, None]) for name in (
            "receiver:B:h3", "receiver:C:h4", "probe:h2")}
        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output="partial", stderr="late")
        result = collect_required_readbacks(
            processes=processes, backend="mock", p4_stdout="", command_runner=timeout,
            clock=iter((10, 20)).__next__, timeout_s=0.01,
        )
        self.assertFalse(result["valid"])
        self.assertTrue(result["raw"]["commands"]["source_address"]["timed_out"])
        self.assertEqual(result["raw"]["commands"]["source_address"]["stdout"], "partial")

    def test_partial_timeout_bytes_returns_structured_invalid_readback_and_cleans_up(self):
        processes = {name: FakeProcess() for name in (
            "receiver:B:h3", "receiver:C:h4", "probe:h2")}

        def command_runner(argv, **kwargs):
            if "h3" in argv:
                raise subprocess.TimeoutExpired(
                    cmd=["ip", "-n", "h3", "maddr", "show"], timeout=0.01,
                    output=b"partial\n", stderr=b"late\n",
                )
            if argv[-3:] == ["-j", "address", "show"]:
                stdout = complete_readback()["raw"]["source_address_json"]
            elif "h4" in argv:
                stdout = f"inet {GROUP}\n"
            else:
                stdout = "inet 224.0.0.1\n"
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        readbacks = collect_required_readbacks(
            processes=processes, backend="mock", p4_stdout="",
            command_runner=command_runner, clock=iter((10, 20)).__next__, timeout_s=0.01,
        )
        timeout_row = readbacks["raw"]["commands"]["receiver:B:h3"]
        self.assertIsInstance(readbacks, dict)
        self.assertFalse(readbacks["valid"])
        self.assertTrue(timeout_row["timed_out"])
        self.assertIsInstance(timeout_row["stdout"], str)
        self.assertIsInstance(timeout_row["stderr"], str)
        self.assertIn("partial", timeout_row["stdout"])
        self.assertIn("late", timeout_row["stderr"])
        self.assertIn("TimeoutExpired", timeout_row["error"])
        success_json = []
        self.assertEqual(success_json, [])
        self.assertTrue(cleanup_processes(processes)["valid"])

    def test_local_timeout_partial_output_can_be_bytes(self):
        code = (
            "import sys,time;"
            "sys.stdout.buffer.write(b'partial\\n');sys.stdout.flush();"
            "sys.stderr.buffer.write(b'late\\n');sys.stderr.flush();"
            "time.sleep(2)"
        )
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True,
                timeout=0.5, check=False,
            )
        self.assertIsInstance(caught.exception.output, bytes)
        self.assertIsInstance(caught.exception.stderr, bytes)

    def test_source_address_json_is_structurally_authenticated(self):
        raw = complete_readback()["raw"]["source_address_json"]
        evidence = source_address_evidence(raw)
        self.assertEqual(evidence, {
            "valid": True, "interface": "h1-eth0", "address": "10.0.0.1",
            "family": "inet", "prefixlen": 24, "scope": "global", "reason": None,
        })

    def test_source_address_text_substring_and_malformed_json_fail(self):
        for raw in ('10.0.0.1', '{"note":"10.0.0.1"}', '[invalid'):
            with self.subTest(raw=raw):
                self.assertFalse(source_address_evidence(raw)["valid"])
                self.assertFalse(complete_readback(source_address_json=raw)["valid"])

    def test_source_address_absent_or_duplicated_fails(self):
        valid_interface = json.loads(complete_readback()["raw"]["source_address_json"])[0]
        for document in ([], [valid_interface, copy.deepcopy(valid_interface)]):
            raw = json.dumps(document)
            with self.subTest(document=document):
                self.assertFalse(source_address_evidence(raw)["valid"])
                self.assertFalse(complete_readback(source_address_json=raw)["valid"])

    def test_source_address_loopback_wrong_family_and_down_fail(self):
        mutations = (
            {"ifname": "lo", "flags": ["LOOPBACK", "UP"]},
            {"flags": ["BROADCAST", "UP"], "addr_info": [{
                "family": "inet6", "local": "10.0.0.1", "prefixlen": 24, "scope": "global"}]},
            {"flags": ["BROADCAST"]},
        )
        original = json.loads(complete_readback()["raw"]["source_address_json"])[0]
        for mutation in mutations:
            interface = copy.deepcopy(original)
            interface.update(mutation)
            raw = json.dumps([interface])
            with self.subTest(mutation=mutation):
                self.assertFalse(source_address_evidence(raw)["valid"])

    def test_missing_receiver_process_fails_closed(self):
        before = copy.deepcopy(complete_readback()["process_liveness_before"])
        after = copy.deepcopy(complete_readback()["process_liveness_after"])
        before.pop("receiver:B:h3")
        after.pop("receiver:B:h3")
        self.assertFalse(complete_readback(
            process_liveness_before=before, process_liveness_after=after)["valid"])

    def test_nonzero_and_exception_source_commands_fail_closed(self):
        processes = {name: SequencedProcess([None, None]) for name in (
            "receiver:B:h3", "receiver:C:h4", "probe:h2")}
        for failure in ("nonzero", "exception"):
            def runner(argv, **kwargs):
                if argv[-3:] == ["-j", "address", "show"]:
                    if failure == "exception":
                        raise OSError("readback unavailable")
                    return subprocess.CompletedProcess(argv, 7, "[]", "failed")
                return subprocess.CompletedProcess(argv, 0, "inet 224.0.0.1\n", "")
            with self.subTest(failure=failure):
                result = collect_required_readbacks(
                    processes=processes, backend="mock", p4_stdout="",
                    command_runner=runner, clock=iter((10, 20)).__next__)
                self.assertFalse(result["valid"])

    def test_bounded_polling_records_every_attempt_and_becomes_ready(self):
        processes = {name: SequencedProcess([None, None, None, None]) for name in (
            "receiver:B:h3", "receiver:C:h4", "probe:h2")}
        calls = 0
        source_json = complete_readback()["raw"]["source_address_json"]
        def runner(argv, **kwargs):
            nonlocal calls
            attempt = calls // 4
            calls += 1
            if argv[-3:] == ["-j", "address", "show"]:
                stdout = source_json
            elif "h3" in argv:
                stdout = f"inet {GROUP}\n" if attempt else "inet 224.0.0.1\n"
            elif "h4" in argv:
                stdout = f"inet {GROUP}\n"
            else:
                stdout = "inet 224.0.0.1\n"
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        sleeps = []
        result = wait_for_required_readbacks(
            processes=processes, backend="mock", p4_stdout="",
            command_runner=runner, wall_clock=iter((10, 20, 30, 40)).__next__,
            monotonic_clock=iter((0.0, 0.0, 0.1, 0.2, 0.3)).__next__,
            sleeper=sleeps.append, deadline_s=1.0, poll_interval_s=0.05)
        self.assertTrue(result["valid"])
        self.assertEqual(result["readiness"]["attempt_count"], 2)
        self.assertEqual([row["valid"] for row in result["readiness"]["attempts"]], [False, True])
        self.assertEqual(sleeps, [0.05])

    def test_monotonic_deadline_stops_unready_polling(self):
        processes = {name: SequencedProcess([None, None]) for name in (
            "receiver:B:h3", "receiver:C:h4", "probe:h2")}
        def runner(argv, **kwargs):
            stdout = "[]" if argv[-3:] == ["-j", "address", "show"] else ""
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        result = wait_for_required_readbacks(
            processes=processes, backend="mock", p4_stdout="",
            command_runner=runner, wall_clock=iter((10, 20)).__next__,
            monotonic_clock=iter((0.0, 0.0, 1.0)).__next__,
            sleeper=lambda _: self.fail("deadline-exhausted poll must not sleep"),
            deadline_s=0.5, poll_interval_s=0.05)
        self.assertFalse(result["valid"])
        self.assertTrue(result["readiness"]["timed_out"])
        self.assertEqual(result["readiness"]["attempt_count"], 1)

    def test_readback_precedes_blocking_sender_and_lifetime_margin_is_explicit(self):
        source = inspect.getsource(s2_main)
        self.assertLess(source.index("readbacks = wait_for_required_readbacks("),
                        source.index("sender = run("))
        self.assertIn("child_duration = args.duration + READINESS_TIMEOUT_S + CHILD_LIFETIME_MARGIN_S", source)
        self.assertIn("str(count), str(interval)", source)
        self.assertGreater(READINESS_TIMEOUT_S, 0)
        self.assertGreater(CHILD_LIFETIME_MARGIN_S, 0)


class ObservationTests(unittest.TestCase):
    def observe(self, raw, membership, **kwargs):
        return build_recovery_observation(
            raw, membership, receivers=RECEIVERS, latency_max_ms=40,
            recovery_bin_ms=kwargs.pop("recovery_bin_ms", 100),
            stable_k_bins=kwargs.pop("stable_k_bins", 2),
            readbacks_valid=kwargs.pop("readbacks_valid", True), **kwargs,
        )

    def test_dedup_loss_and_duplicate_count(self):
        raw, membership = recovery_fixture(duplicate=True, omit_sequence=2)
        first = self.observe(raw, membership)["receivers"]["B:h3"]["episodes"][0]["windows"][0]
        self.assertEqual((first["sent_opportunities"], first["unique_received"]), (2, 1))
        self.assertEqual((first["lost"], first["duplicates"]), (1, 1))

    def test_type7_p99(self):
        self.assertAlmostEqual(percentile_type7(list(range(1, 101)), .99), 99.01)
        with self.assertRaises(ValueError):
            percentile_type7([1, float("nan")], .99)

    def test_empty_window_is_not_evaluable(self):
        raw, membership = recovery_fixture()
        moved = []
        for row in raw:
            item = dict(row, sent_ns=row["sent_ns"] + 100_000_000)
            if "received_ns" in row:
                item["received_ns"] = row["received_ns"] + 100_000_000
            moved.append(item)
        first = self.observe(moved, membership, stable_k_bins=1)["receivers"]["B:h3"]["episodes"][0]["windows"][0]
        self.assertEqual(first["status"], "NOT_EVALUABLE")

    def test_adapt_k_bins_consecutive_passes(self):
        raw, membership = recovery_fixture()
        result = self.observe(raw, membership)
        self.assertEqual(result["observation_status"], "PASS")
        self.assertEqual(result["aggregate"]["time_to_stable_observation_ms"], 200)

    def test_k_bins_must_be_consecutive(self):
        raw, membership = recovery_fixture()
        result = self.observe(raw, membership, probe_packets=[{"received_ns": 1_150_000_000}])
        episode = result["receivers"]["B:h3"]["episodes"][0]
        self.assertEqual([window["status"] for window in episode["windows"]], ["PASS", "FAIL", "PASS"])
        self.assertEqual(episode["status"], "FAIL")

    def test_baseline_without_rejoin_is_not_evaluable(self):
        raw, membership = recovery_fixture(include_rejoin=False)
        self.assertEqual(self.observe(raw, membership)["observation_status"], "NOT_EVALUABLE")

    def test_insufficient_post_event_time_is_not_evaluable(self):
        raw, membership = recovery_fixture(stop_offset_ms=150)
        self.assertEqual(self.observe(raw, membership)["observation_status"], "NOT_EVALUABLE")

    def test_probe_without_timestamp_cannot_produce_success(self):
        raw, membership = recovery_fixture()
        result = self.observe(raw, membership, probe_packets=[{"bytes": 42}])
        self.assertFalse(result["probe_timestamp_evidence_complete"])
        self.assertEqual(result["observation_status"], "FAIL")

    def test_predicate_exclusions_and_deterministic_recomputation(self):
        raw, membership = recovery_fixture(duplicate=True)
        first = self.observe(raw, membership)
        second = self.observe(raw, membership)
        self.assertEqual(first, second)
        self.assertEqual(first["predicate"], PREDICATE_ID)
        self.assertEqual(first["excluded_requirements"], EXCLUDED_REQUIREMENTS)
        rendered = json.dumps(first, sort_keys=True)
        for forbidden in ('"full_conformance":', '"intent_conformance":', '"bandwidth_pass":'):
            self.assertNotIn(forbidden, rendered)


class ExecutionIdTests(unittest.TestCase):
    def test_valid_ids_and_direct_child_resolution(self):
        for identifier in ("s2-001", "A_b.c", "9"):
            self.assertEqual(validate_execution_id(identifier), identifier)
        with tempfile.TemporaryDirectory() as temporary:
            output = resolve_output_directory(temporary, "s2-001")
            self.assertEqual(output.parent, Path(temporary).resolve())

    def test_unsafe_ids_rejected(self):
        for identifier in ("", ".", "..", "/absolute", "a/b", "a\\b", "../x", "x\ncontrol", " x"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                validate_execution_id(identifier)

    def test_collision_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            reserve_output_directory(temporary, "same")
            with self.assertRaises(FileExistsError):
                reserve_output_directory(temporary, "same")

    def test_setup_default_is_unique_safe_and_override_is_retained(self):
        source = (ROOT / "setup_all.sh").read_text(encoding="utf-8").splitlines()
        start = next(index for index, line in enumerate(source) if line.startswith("s2_operational_execution_id()"))
        end = next(index for index in range(start + 1, len(source)) if source[index] == "}")
        function = "\n".join(source[start:end + 1])
        command = function + "\na=$(s2_operational_execution_id)\nb=$(s2_operational_execution_id)\nprintf '%s\\n%s\\n' \"$a\" \"$b\""
        completed = subprocess.run(["bash", "-c", command], text=True, capture_output=True, check=True)
        first, second = completed.stdout.splitlines()
        self.assertNotEqual(first, second)
        validate_execution_id(first)
        validate_execution_id(second)
        self.assertIn("[[ -v S2_EXECUTION_ID ]]", function)


class CleanupAndTerminalTests(unittest.TestCase):
    EXECUTION_ID = "terminal-fixture"

    def make_terminal_files(self, root: Path, *, backend="mock"):
        digest = "a" * 64
        raw = [
            {"schema": "phase4sc-operational-raw-v3", "scenario": "S2", "event": "sent",
             "sequence": 1, "endpoint": "A:h1", "traffic_class": "multicast",
             "payload_sha256": digest, "sent_ns": 100, "received_ns": 0, "bytes": 10, "status": "sent"},
            {"schema": "phase4sc-operational-raw-v3", "scenario": "S2", "event": "received",
             "sequence": 1, "endpoint": "B:h3", "traffic_class": "multicast",
             "payload_sha256": digest, "sent_ns": 100, "received_ns": 110, "bytes": 10, "status": "received"},
            {"schema": "phase4sc-operational-raw-v3", "scenario": "S2", "event": "received",
             "sequence": 1, "endpoint": "C:h4", "traffic_class": "multicast",
             "payload_sha256": digest, "sent_ns": 100, "received_ns": 111, "bytes": 10, "status": "received"},
        ]
        membership = [
            {"event": "membership_join", "endpoint": receiver, "observed_ns": 90}
            for receiver in RECEIVERS
        ] + [
            {"event": "receiver_stop", "endpoint": receiver, "observed_ns": 200}
            for receiver in RECEIVERS
        ]
        readbacks = complete_readback(backend=backend, p4_stdout=(P4_FUNCTIONAL_TOKEN if backend == "real" else ""))
        observation_path = root / "recovery-observation.json"
        observation = {
            "schema": SCHEMA_ID, "predicate": PREDICATE_ID,
            "observation_status": "NOT_EVALUABLE",
            "parameters": {"latency_max_ms": 40.0, "recovery_bin_ms": 500, "stable_k_bins": 3},
            "aggregate": {"status": "NOT_EVALUABLE"},
            "receivers": {receiver: {"status": "NOT_EVALUABLE"} for receiver in RECEIVERS},
        }
        summary = {
            "scenario": "S2", "execution_id": self.EXECUTION_ID, "backend": backend, "mode": "adapt",
            "readbacks_valid": True, "raw_recomputable": True, "synthetic_metrics": False,
            "metrics": {"sent": 1, "delivered": 2},
            "workload_parameters": {"expected_packet_count": 1},
            "raw_artifacts": {"recovery_observation": str(observation_path)},
            "recovery_observation": {"artifact": str(observation_path), "schema": SCHEMA_ID,
                                     "observation_status": "NOT_EVALUABLE"},
        }
        state = ({"attempted": True, "completed": True, "returncode_or_error": 0}
                 if backend == "real" else new_p4_apply_state())
        for name, value in (("readbacks.json", readbacks), ("summary.json", summary),
                            ("recovery-observation.json", observation)):
            (root / name).write_text(json.dumps(value) + "\n", encoding="utf-8")
        for name, rows in (("raw-events.jsonl", raw), ("membership-events.jsonl", membership)):
            (root / name).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        if backend == "mock":
            finalize_runtime(root, backend=backend, p4_apply_state=state, processes={})
        else:
            successful = subprocess.CompletedProcess(["cleanup"], 0, stdout="ok", stderr="")
            with patch("scenarios.multidomain_s2.run", return_value=successful):
                finalize_runtime(root, backend=backend, p4_apply_state=state, processes={})

    def test_failure_before_sender_cleans_started_receiver(self):
        receiver = FakeProcess()
        result = cleanup_processes({"receiver:B:h3": receiver})
        self.assertTrue(result["valid"])
        self.assertIn("terminate", receiver.actions)

    def test_sender_failure_cleanup_covers_all_children(self):
        children = {name: FakeProcess() for name in ("receiver:B:h3", "receiver:C:h4", "probe:h2")}
        result = cleanup_processes(children)
        self.assertTrue(result["valid"])
        self.assertTrue(all("terminate" in process.actions for process in children.values()))

    def test_receiver_timeout_escalates_to_kill(self):
        receiver = FakeProcess(stubborn=True, timeout_once=True)
        result = cleanup_processes({"receiver:B:h3": receiver}, timeout_s=0.01)
        self.assertTrue(result["valid"])
        self.assertIn("kill", receiver.actions)
        self.assertEqual(result["processes"]["receiver:B:h3"]["returncode"], -9)

    def test_cleanup_error_is_recorded(self):
        receiver = FakeProcess(cleanup_error=RuntimeError("secondary"))
        result = cleanup_processes({"receiver:B:h3": receiver})
        self.assertFalse(result["valid"])
        self.assertIn("secondary", result["processes"]["receiver:B:h3"]["cleanup_error"])

    def test_success_emitted_once_after_valid_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.make_terminal_files(output)
            events = []
            emit_success(output, self.EXECUTION_ID, printer=lambda value: events.append(json.loads(value)))
            self.assertEqual(len([item for item in events if isinstance(item, dict) and item.get("valid")]), 1)

    def test_terminal_validator_accepts_complete_real_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.make_terminal_files(output, backend="real")
            self.assertTrue(validate_terminal_artifacts(output, self.EXECUTION_ID)["valid"])

    def test_p4_cleanup_failure_never_emits_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.make_terminal_files(output)
            failed = subprocess.CompletedProcess(["cleanup"], 1, stdout="bad", stderr="failure")
            state = {"attempted": True, "completed": True, "returncode_or_error": 0}
            with patch("scenarios.multidomain_s2.run", return_value=failed):
                rollback = finalize_runtime(output, backend="real", p4_apply_state=state, processes={})
            emitted = []
            with self.assertRaises(ValueError):
                emit_success(output, self.EXECUTION_ID, printer=emitted.append)
            self.assertFalse(rollback["valid"])
            self.assertEqual(emitted, [])

    def test_main_has_single_terminal_success_call_after_finalize(self):
        source = (ROOT / "scenarios/multidomain_s2.py").read_text(encoding="utf-8")
        main_source = source[source.index("def main()") :]
        self.assertEqual(main_source.count("emit_success(output, args.execution_id)"), 1)
        self.assertLess(main_source.index("finalize_or_raise("), main_source.index("emit_success(output, args.execution_id)"))

    def test_empty_json_placeholders_rejected_without_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.make_terminal_files(output)
            (output / "summary.json").write_text("{}\n", encoding="utf-8")
            emitted = []
            with self.assertRaises(ValueError):
                emit_success(output, self.EXECUTION_ID, printer=emitted.append)
            self.assertEqual(emitted, [])

    def test_each_terminal_artifact_semantic_mutation_is_rejected(self):
        mutations = {
            "raw-events.jsonl": lambda value: value.update(schema="wrong"),
            "membership-events.jsonl": lambda value: value.update(endpoint="D:h9"),
            "readbacks.json": lambda value: value.update(valid=False),
            "summary.json": lambda value: value.update(execution_id="wrong"),
            "recovery-observation.json": lambda value: value.update(predicate="wrong"),
            "rollback.json": lambda value: value.update(valid=False),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                self.make_terminal_files(output)
                path = output / name
                if name.endswith(".jsonl"):
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                    mutate(rows[0])
                    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                else:
                    value = json.loads(path.read_text())
                    mutate(value)
                    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
                emitted = []
                with self.assertRaises(ValueError):
                    emit_success(output, self.EXECUTION_ID, printer=emitted.append)
                self.assertEqual(emitted, [])

    def test_invalid_json_is_rejected_without_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.make_terminal_files(output)
            (output / "recovery-observation.json").write_text("{not-json}\n", encoding="utf-8")
            emitted = []
            with self.assertRaises(json.JSONDecodeError):
                emit_success(output, self.EXECUTION_ID, printer=emitted.append)
            self.assertEqual(emitted, [])

    def test_raw_summary_cardinality_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.make_terminal_files(output)
            summary = json.loads((output / "summary.json").read_text())
            summary["metrics"]["sent"] = 99
            (output / "summary.json").write_text(json.dumps(summary) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_terminal_artifacts(output, self.EXECUTION_ID)

    def test_prohibited_claim_key_nested_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.make_terminal_files(output)
            summary = json.loads((output / "summary.json").read_text())
            summary["nested"] = {"full_" + "conformance": True}
            (output / "summary.json").write_text(json.dumps(summary) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_terminal_artifacts(output, self.EXECUTION_ID)

    def test_readback_command_timeout_never_emits_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.make_terminal_files(output)
            readbacks = json.loads((output / "readbacks.json").read_text())
            readbacks["raw"]["commands"]["source_address"]["timed_out"] = True
            (output / "readbacks.json").write_text(json.dumps(readbacks) + "\n", encoding="utf-8")
            emitted = []
            with self.assertRaises(ValueError):
                emit_success(output, self.EXECUTION_ID, printer=emitted.append)
            self.assertEqual(emitted, [])

    def test_p4_apply_success_state(self):
        state = new_p4_apply_state()
        completed = subprocess.CompletedProcess([], 0, P4_FUNCTIONAL_TOKEN + "\n", "")
        self.assertIs(program_p4(state, command_runner=lambda *a, **k: completed), completed)
        self.assertEqual(state, {"attempted": True, "completed": True, "returncode_or_error": 0})

    def test_p4_apply_nonzero_state_requires_cleanup(self):
        state = new_p4_apply_state()
        completed = subprocess.CompletedProcess([], 7, "", "apply rejected")
        with self.assertRaises(RuntimeError):
            program_p4(state, command_runner=lambda *a, **k: completed)
        self.assertEqual(state, {"attempted": True, "completed": True, "returncode_or_error": 7})
        with tempfile.TemporaryDirectory() as temporary:
            cleanup = subprocess.CompletedProcess([], 0, "clean", "")
            with patch("scenarios.multidomain_s2.run", return_value=cleanup):
                rollback = finalize_runtime(Path(temporary), backend="real", p4_apply_state=state, processes={})
        self.assertTrue(rollback["p4_cleanup_required"])
        self.assertTrue(rollback["p4_cleanup"]["attempted"])

    def test_p4_apply_exception_state_requires_cleanup(self):
        for failure in (OSError("transport unavailable"), subprocess.TimeoutExpired("apply", 1)):
            with self.subTest(failure=type(failure).__name__):
                state = new_p4_apply_state()
                def fail(*args, **kwargs):
                    raise failure
                with self.assertRaises(type(failure)):
                    program_p4(state, command_runner=fail)
                self.assertTrue(state["attempted"])
                self.assertFalse(state["completed"])
                self.assertIn(type(failure).__name__, state["returncode_or_error"])
                with tempfile.TemporaryDirectory() as temporary:
                    cleanup = subprocess.CompletedProcess([], 0, "clean", "")
                    with patch("scenarios.multidomain_s2.run", return_value=cleanup):
                        rollback = finalize_runtime(Path(temporary), backend="real", p4_apply_state=state, processes={})
                self.assertTrue(rollback["p4_cleanup_required"])

    def test_primary_apply_error_is_preserved_after_cleanup(self):
        primary = RuntimeError("primary apply failure")
        state = {"attempted": True, "completed": False, "returncode_or_error": "transport"}
        with tempfile.TemporaryDirectory() as temporary:
            cleanup = subprocess.CompletedProcess([], 0, "clean", "")
            with patch("scenarios.multidomain_s2.run", return_value=cleanup):
                with self.assertRaises(RuntimeError) as caught:
                    finalize_or_raise(Path(temporary), backend="real", p4_apply_state=state,
                                      processes={}, primary_error=primary)
        self.assertIs(caught.exception, primary)

    def test_cleanup_failure_cannot_mask_primary_error(self):
        primary = RuntimeError("primary")
        state = {"attempted": True, "completed": False, "returncode_or_error": "transport"}
        with tempfile.TemporaryDirectory() as temporary:
            failed = subprocess.CompletedProcess([], 9, "", "cleanup failed")
            with patch("scenarios.multidomain_s2.run", return_value=failed):
                with self.assertRaises(RuntimeError) as caught:
                    finalize_or_raise(Path(temporary), backend="real", p4_apply_state=state,
                                      processes={}, primary_error=primary)
        self.assertIs(caught.exception, primary)

    def test_mock_backend_never_requests_p4_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            rollback = finalize_runtime(Path(temporary), backend="mock",
                                        p4_apply_state=new_p4_apply_state(), processes={})
        self.assertFalse(rollback["p4_cleanup_required"])
        self.assertFalse(rollback["p4_cleanup"]["attempted"])


class CandidateStaticTests(unittest.TestCase):
    def test_historical_files_are_byte_identical(self):
        for relative, expected in HISTORICAL_HASHES.items():
            self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), expected, relative)

    def test_no_historical_dispatch_or_mtime_selection(self):
        active = [
            ROOT / "setup_all.sh", ROOT / "scripts/setup_all.sh", ROOT / "docs/experiments.md",
            ROOT / "scripts/s2_compare.py", ROOT / "scripts/sweep_s2.py",
        ]
        for path in active:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("scenarios.multicast_s2_recovery_stable5", text, path)
            self.assertNotIn("-m scenarios.multicast_s2 ", text, path)
            self.assertNotIn("st_mtime", text, path)
            self.assertNotIn("getmtime", text, path)

    def test_s2_surface_taxonomy_is_complete(self):
        source = (ROOT / "setup_all.sh").read_text(encoding="utf-8")
        functions = set(re.findall(r"^(run_s2_[A-Za-z0-9_]+)\(\)", source, re.MULTILINE))
        rows = re.findall(r"^# S2_TAXONOMY (run_s2_[A-Za-z0-9_]+) (\w+)$", source, re.MULTILINE)
        taxonomy = dict(rows)
        self.assertEqual(set(taxonomy), functions)
        self.assertEqual(taxonomy["run_s2_real"], "canonical_scenario")
        self.assertEqual(sum(value == "canonical_scenario" for value in taxonomy.values()), 1)
        self.assertTrue(all(value in {"canonical_scenario", "specialized_validation_profile"} for value in taxonomy.values()))
        workflow = (ROOT / "docs/s2_operational_workflow.md").read_text(encoding="utf-8")
        for name, classification in taxonomy.items():
            self.assertIn(f"`{name}`", workflow)
            self.assertIn(f"`{classification}`", workflow)

    def test_legacy_metrics_both_documented(self):
        engine = (ROOT / "scenarios/multidomain_s2.py").read_text(encoding="utf-8")
        workflow = (ROOT / "docs/s2_operational_workflow.md").read_text(encoding="utf-8")
        for metric in ("metrics.recovery_ms", "metrics.stability"):
            self.assertIn(metric, engine)
            self.assertIn(metric, workflow)

    def test_no_prohibited_claim_keys(self):
        sources = [ROOT / "scenarios/multidomain_s2.py", ROOT / "l2i/s2_recovery_observation.py"]
        rendered = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        for forbidden in ('"full_conformance":', '"intent_conformance":', '"bandwidth_pass":'):
            self.assertNotIn(forbidden, rendered)


class CertifiedNetconfBootstrapTests(unittest.TestCase):
    def lifecycle_source(self):
        source = (ROOT / "setup_all.sh").read_text(encoding="utf-8")
        return source[source.index('NETCONF_SYSTEMD_UNIT="netopeer2-server.service"'):
                      source.index("# -------------------------------\n# p4", source.index('NETCONF_SYSTEMD_UNIT='))]

    def run_harness(self, scenario="positive", action="start"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = root / "key"
            key.write_text("private fixture\n", encoding="utf-8")
            log = root / "commands.log"
            bindir = root / "bin"
            bindir.mkdir()
            for name in ("sysrepocfg", "sysrepoctl", "ss"):
                command = bindir / name
                command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                command.chmod(0o755)
            script = r'''
source ./setup_all.sh
SCENARIO=${HARNESS_SCENARIO}
UNIT_STATE=inactive
[[ "$SCENARIO" == unitactive ]] && UNIT_STATE=active
PID_CALLS=0
: > "${HARNESS_LOG}"
printf '0\n' > "${HARNESS_CLOCK}"
id(){ return 0; }
ps(){ printf '%s\n' netopeer2-serv; }
systemctl(){
  case "$1" in
    is-active) printf '%s\n' "$UNIT_STATE"; [[ "$UNIT_STATE" == active ]] ;;
    show)
      case "$*" in
        *LoadState*) printf '%s\n' loaded ;;
        *ActiveState*) printf '%s\n' "$UNIT_STATE" ;;
        *SubState*) [[ "$UNIT_STATE" == active ]] && printf '%s\n' running || printf '%s\n' dead ;;
        *MainPID*) [[ "$UNIT_STATE" == active ]] && printf '%s\n' 4242 || printf '%s\n' 0 ;;
        *ControlGroup*) printf '%s\n' /system.slice/netopeer2-server.service ;;
      esac ;;
    *) return 0 ;;
  esac
}
sudo(){
  [[ "${1:-}" == -n ]] && shift
  case "${1:-}:${2:-}" in
    true:*) [[ "$SCENARIO" == sudounavailable ]] && return 1; return 0 ;;
    systemctl:start)
      printf '%s\n' start >> "${HARNESS_LOG}"
      [[ "$SCENARIO" == startfail ]] && return 7
      if [[ "$SCENARIO" == partialstartfail ]]; then UNIT_STATE=active; return 7; fi
      UNIT_STATE=active; return 0 ;;
    systemctl:stop)
      printf '%s\n' stop >> "${HARNESS_LOG}"
      UNIT_STATE=inactive; return 0 ;;
    sysrepocfg:-C)
      local module="${*: -1}"
      printf 'copy:%s\n' "$module" >> "${HARNESS_LOG}"
      [[ "$SCENARIO" == "copyfail-$module" ]] && return 8
      return 0 ;;
    sysrepoctl:*) return 0 ;;
    kill:-0) return 0 ;;
    kill:*) printf 'signal:%s\n' "$*" >> "${HARNESS_LOG}"; return 0 ;;
    tee:*) cat >/dev/null; return 0 ;;
    rm:*) printf '%s\n' pidfile-remove >> "${HARNESS_LOG}"; return 0 ;;
    journalctl:*) return 0 ;;
    *) return 0 ;;
  esac
}
netconf_module_installed(){ [[ "$SCENARIO" != "modulemissing-$1" ]]; }
netconf_python_ready(){ return 0; }
netconf_expected_executable(){ printf '%s\n' /usr/local/sbin/netopeer2-server; }
netconf_residual_pids(){
  case "$SCENARIO" in
    residual|deleted-canonical-residual) printf '%s\n' 4343 ;;
    residual-error) return 7 ;;
  esac
  return 0
}
netconf_main_pid(){ [[ "$UNIT_STATE" == active ]] && printf '%s\n' 4242 || printf '%s\n' 0; }
netconf_unit_active_running(){
  if [[ "$SCENARIO" == activetimeout || "$SCENARIO" == pidinvalid ]]; then
    return 1
  fi
  [[ "$UNIT_STATE" == active ]]
}
netconf_pid_valid(){
  [[ "$SCENARIO" == pidinvalid ]] && return 1
  if [[ "$SCENARIO" == piddies ]]; then
    PID_CALLS=$((PID_CALLS + 1))
    if (( PID_CALLS > 1 )); then return 1; fi
  fi
  [[ "${1:-}" == 4242 ]]
}
netconf_process_identity(){ printf '%s\n' "${1:-}|/usr/local/sbin/netopeer2-server|/system.slice/netopeer2-server.service"; }
netconf_monotonic_ns(){
  local value
  read -r value < "${HARNESS_CLOCK}"
  printf '%s\n' "$value"
  printf '%s\n' "$((value + 1000000000))" > "${HARNESS_CLOCK}"
}
netconf_poll_sleep(){ :; }
port_listening(){
  [[ "$SCENARIO" == portoccupied ]] && return 0
  [[ "$UNIT_STATE" == active ]] || return 1
  if [[ "$SCENARIO" == portoscillates || "$SCENARIO" == portabsent ]]; then
    return 1
  fi
  return 0
}
netconf_snapshot(){
  local module="$1" datastore="$2" raw norm
  case "$module" in
    ietf-keystore) raw=b44726600d70ff3b653d92c764d5bbc8a03a94dba2d5e702641639283e736f25 ;;
    ietf-netconf-acm) raw=0a8a1b531211ecc7765e104e2a64fc8843444b1051533398990aa961a4201252 ;;
    ietf-netconf-server) raw=48dbb26be547f3b246769f156fe007feec0cd1e545ace4e5a256352f1d7eb77b ;;
    l2i-qos) raw=qos-raw ;;
  esac
  if [[ "$datastore" == startup && "$SCENARIO" == "certifiedhash-$module" ]]; then raw=bad-certified-hash; fi
  norm="normalized-$module"
  if [[ "$datastore" == running && "$SCENARIO" == "diverge-$module" ]]; then norm=divergent; fi
  if [[ "$datastore" == startup && "$SCENARIO" == startupaltered ]] \
     && grep -q '^copy:ietf-netconf-server$' "${HARNESS_LOG}"; then raw=altered; norm=altered; fi
  if [[ "$module" == l2i-qos && "$SCENARIO" == qosaltered ]] \
     && grep -q '^copy:ietf-netconf-server$' "${HARNESS_LOG}"; then raw=altered; norm=altered; fi
  printf '%s %s\n' "$raw" "$norm"
}
netconf_read_only_probe(){
  printf 'get-config:running\n' >> "${HARNESS_LOG}"
  case "$SCENARIO" in authrefused|gettimeout|invalidreply) return 1 ;; esac
  return 0
}
netconf_limited_diagnostics(){ printf '%s\n' diagnostics >> "${HARNESS_LOG}"; }
set +e
if [[ "${HARNESS_ACTION}" == stop ]]; then stop_netconf; else start_netconf; fi
rc=$?
set -e
printf 'RC=%s\n' "$rc"
cat "${HARNESS_LOG}"
'''
            env = dict(os.environ)
            env.update({
                "L2I_REPO_DIR": str(ROOT), "L2I_SETUP_LIB_ONLY": "1",
                "PYTHON_BIN": sys.executable, "NETCONF_KEY": str(key),
                "HARNESS_LOG": str(log), "HARNESS_SCENARIO": scenario,
                "HARNESS_ACTION": action,
                "HARNESS_CLOCK": str(root / "clock"),
                "NETCONF_EXPECTED_EXECUTABLE": "/usr/local/sbin/netopeer2-server",
            })
            env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
            return subprocess.run(
                ["bash", "-c", script], cwd=ROOT, env=env,
                text=True, capture_output=True, timeout=8, check=False,
            )

    def test_positive_path_has_exact_temporal_order_and_counts(self):
        completed = self.run_harness()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        events = [line for line in completed.stdout.splitlines()
                  if line in {"start", "copy:ietf-keystore", "copy:ietf-netconf-acm",
                              "copy:ietf-netconf-server", "get-config:running", "stop"}]
        self.assertEqual(events, ["start", "copy:ietf-keystore", "copy:ietf-netconf-acm",
                                  "copy:ietf-netconf-server", "get-config:running"])

    def test_start_contract_is_single_systemd_only(self):
        source = self.lifecycle_source()
        self.assertEqual(source.count("sudo -n systemctl start netopeer2-server.service"), 1)
        for forbidden in ("nohup", "pkill", "systemctl restart", "netopeer_bin", "launcher_pid"):
            self.assertNotIn(forbidden, source)

    def test_copy_contract_is_looped_once_and_forbids_extra_modules(self):
        source = self.lifecycle_source()
        self.assertEqual(source.count("sudo -n sysrepocfg -C startup -d running"), 1)
        self.assertIn("NETCONF_BOOTSTRAP_MODULES=(ietf-keystore ietf-netconf-acm ietf-netconf-server)", source)
        self.assertNotRegex(source, r"-C startup -d running -m (l2i-qos|ietf-truststore)")

    def test_start_and_active_pid_failures_cleanup_once(self):
        for scenario in ("startfail", "partialstartfail", "activetimeout", "pidinvalid"):
            with self.subTest(scenario=scenario):
                completed = self.run_harness(scenario)
                self.assertIn("RC=", completed.stdout)
                self.assertNotIn("RC=0\n", completed.stdout)
                self.assertEqual(completed.stdout.splitlines().count("start"), 1)
                self.assertEqual(completed.stdout.splitlines().count("stop"), 1)

    def test_prestart_failures_never_mutate_service_resources(self):
        scenarios = (
            "unitactive", "portoccupied", "residual", "residual-error",
            "modulemissing-ietf-keystore",
            "certifiedhash-ietf-keystore", "sudounavailable",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                completed = self.run_harness(scenario)
                lines = completed.stdout.splitlines()
                self.assertNotIn("RC=0", lines)
                self.assertNotIn("start", lines)
                self.assertNotIn("stop", lines)
                self.assertFalse(any(line.startswith("signal:") for line in lines))
                self.assertNotIn("pidfile-remove", lines)

    def test_partial_activation_failure_is_owned_and_cleaned_once(self):
        completed = self.run_harness("partialstartfail")
        lines = completed.stdout.splitlines()
        self.assertNotIn("RC=0", lines)
        self.assertEqual(lines.count("start"), 1)
        self.assertEqual(lines.count("stop"), 1)
        self.assertEqual(lines.count("pidfile-remove"), 1)
        self.assertIn("Falha no start único", completed.stderr)
        self.assertIn("Falha primária preservada", completed.stderr)

    def test_each_copy_failure_stops_and_preserves_order(self):
        modules = ("ietf-keystore", "ietf-netconf-acm", "ietf-netconf-server")
        for index, module in enumerate(modules):
            with self.subTest(module=module):
                completed = self.run_harness("copyfail-" + module)
                lines = completed.stdout.splitlines()
                self.assertNotIn("RC=0", lines)
                self.assertEqual(lines.count("start"), 1)
                self.assertEqual(lines.count("stop"), 1)
                self.assertEqual([x for x in lines if x.startswith("copy:")],
                                 ["copy:" + x for x in modules[:index + 1]])

    def test_running_divergence_startup_and_qos_mutation_fail_closed(self):
        scenarios = ("diverge-ietf-keystore", "diverge-ietf-netconf-acm",
                     "diverge-ietf-netconf-server", "startupaltered", "qosaltered")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                completed = self.run_harness(scenario)
                self.assertNotIn("RC=0", completed.stdout.splitlines())
                self.assertEqual(completed.stdout.splitlines().count("stop"), 1)

    def test_port_pid_and_authenticated_probe_failures_cleanup(self):
        for scenario in ("portabsent", "portoscillates", "piddies",
                         "authrefused", "gettimeout", "invalidreply"):
            with self.subTest(scenario=scenario):
                completed = self.run_harness(scenario)
                self.assertNotIn("RC=0", completed.stdout.splitlines())
                self.assertEqual(completed.stdout.splitlines().count("stop"), 1)

    def test_readiness_and_rpc_are_bounded_read_only_and_closed(self):
        source = self.lifecycle_source()
        self.assertIn("time.monotonic_ns()", source)
        self.assertIn("NETCONF_STABILITY_NS=2000000000", source)
        self.assertNotIn("SECONDS", source)
        self.assertIn('session.get_config(source="running")', source)
        self.assertIn("session.close_session()", source)
        self.assertNotIn("edit_config", source)
        self.assertNotIn("dispatch", source)

    def test_failure_path_preserves_primary_and_has_limited_diagnostics(self):
        completed = self.run_harness("startfail")
        self.assertIn("Falha primária preservada", completed.stderr)
        source = self.lifecycle_source()
        self.assertIn('primary="${NETCONF_PRIMARY_ERROR', source)
        self.assertEqual(source.count("netconf_systemd_stop_once \"$unit_pid\" || true"), 1)

    def test_stop_is_idempotent_systemd_scoped_and_no_broad_kill(self):
        completed = self.run_harness(action="stop")
        self.assertIn("RC=0", completed.stdout)
        self.assertEqual(completed.stdout.splitlines().count("stop"), 1)
        source = self.lifecycle_source()
        self.assertIn('systemctl stop "$NETCONF_SYSTEMD_UNIT"', source)
        self.assertNotIn("pkill", source)
        self.assertNotIn("killall", source)
        self.assertIn('netconf_capture_process_fingerprint "$unit_pid"', source)
        self.assertIn('netconf_process_fingerprint_valid "$fingerprint"', source)
        self.assertIn("netconf_pid_start_time", source)

    def test_preflight_contract_and_certified_hashes_are_explicit(self):
        source = self.lifecycle_source()
        for token in ("sudo -n true", "sysrepoctl", "NETCONF_KEY", "NETCONF_USER", "import ncclient",
                      "port_listening", "netconf_residual_pids", "/proc",
                      "b44726600d70ff3b653d92c764d5bbc8a03a94dba2d5e702641639283e736f25",
                      "0a8a1b531211ecc7765e104e2a64fc8843444b1051533398990aa961a4201252",
                      "48dbb26be547f3b246769f156fe007feec0cd1e545ace4e5a256352f1d7eb77b"):
            self.assertIn(token, source)
        for forbidden in ("pgrep -x netopeer2-server", "pgrep -f", "ps -p", " comm=", "pkill"):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("netconf_residual_pids 2>/dev/null || true", source)
        self.assertIn('residuals="$(netconf_residual_pids 2>/dev/null)"', source)

    def test_preflight_requires_zero_status_and_empty_residual_output(self):
        error = self.run_harness("residual-error")
        self.assertNotIn("RC=0", error.stdout.splitlines())
        self.assertIn("Falha ao verificar processos Netopeer2 residuais", error.stderr)
        self.assertNotIn("start", error.stdout.splitlines())
        residual = self.run_harness("residual")
        self.assertNotIn("RC=0", residual.stdout.splitlines())
        self.assertIn("Processo Netopeer2 residual detectado", residual.stderr)
        self.assertNotIn("start", residual.stdout.splitlines())
        empty = self.run_harness("positive")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertIn("start", empty.stdout.splitlines())

    def test_cleanup_final_state_requires_unit_pid_port_and_residual_absence(self):
        source = self.lifecycle_source()
        self.assertIn("netconf_wait_absent", source)
        self.assertIn('active="$(systemctl show "$NETCONF_SYSTEMD_UNIT" --property=ActiveState', source)
        self.assertIn('netconf_main_pid_absent "$pid"', source)
        self.assertIn('residuals="$(netconf_residual_pids)" || return 1', source)
        self.assertIn('&& [[ -z "$residuals" ]]', source)

    def test_post_stop_fingerprint_is_birth_bound_and_main_pid_independent(self):
        source = self.lifecycle_source()
        capture = source[source.index("netconf_capture_process_fingerprint()"):
                         source.index("netconf_process_fingerprint_valid()")]
        validator = source[source.index("netconf_process_fingerprint_valid()"):
                           source.index("netconf_unit_active_running()")]
        self.assertIn('start_time="$(netconf_pid_start_time "$pid")"', capture)
        self.assertIn('current_start_time="$(netconf_pid_start_time "$pid")"', validator)
        self.assertIn('[[ "$current_start_time" == "$start_time" ]]', validator)
        self.assertNotIn("netconf_main_pid", validator)

    def run_wait_absent_harness(self, residual_mode):
        with tempfile.TemporaryDirectory() as temporary:
            clock = Path(temporary) / "clock"
            clock.write_text("0\n", encoding="utf-8")
            script = r'''
source ./setup_all.sh
systemctl(){ [[ "$*" == *ActiveState* ]] && printf '%s\n' inactive; }
netconf_main_pid(){ printf '%s\n' 0; }
port_listening(){ return 1; }
netconf_residual_pids(){
  case "${RESIDUAL_MODE}" in
    present) printf '%s\n' 7777 ;;
    error) return 7 ;;
  esac
}
netconf_monotonic_ns(){
  local value
  read -r value < "${CLOCK_FILE}"
  printf '%s\n' "$value"
  printf '%s\n' "$((value + 6000000000))" > "${CLOCK_FILE}"
}
netconf_poll_sleep(){ :; }
set +e
netconf_wait_absent
rc=$?
set -e
printf 'WAIT_ABSENT_RC=%s\n' "$rc"
'''
            env = dict(os.environ)
            env.update({
                "L2I_REPO_DIR": str(ROOT), "L2I_SETUP_LIB_ONLY": "1",
                "CLOCK_FILE": str(clock), "RESIDUAL_MODE": residual_mode,
            })
            return subprocess.run(
                ["bash", "-c", script], cwd=ROOT, env=env,
                text=True, capture_output=True, timeout=4, check=False,
            )

    def test_wait_absent_rejects_residual_with_zero_main_pid(self):
        completed = self.run_wait_absent_harness("present")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("WAIT_ABSENT_RC=1", completed.stdout)

    def test_wait_absent_accepts_only_complete_absence(self):
        completed = self.run_wait_absent_harness("absent")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("WAIT_ABSENT_RC=0", completed.stdout)

    def test_wait_absent_fails_closed_on_residual_detection_error(self):
        completed = self.run_wait_absent_harness("error")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("WAIT_ABSENT_RC=1", completed.stdout)

    def run_residual_scan_harness(self, mode):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / "proc"
            proc.mkdir()
            executable = root / "usr" / "sbin" / "netopeer2-server"
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture\n", encoding="utf-8")
            executable.chmod(0o755)
            other_executable = root / "usr" / "sbin" / "other-server"
            other_executable.write_text("fixture\n", encoding="utf-8")
            other_executable.chmod(0o755)
            same_basename = root / "opt" / "netopeer2-server"
            same_basename.parent.mkdir(parents=True)
            same_basename.write_text("fixture\n", encoding="utf-8")
            same_basename.chmod(0o755)

            def write_stat(pid_dir, pid, start_time):
                fields = ["S"] + ["0"] * 18 + [str(start_time)]
                (pid_dir / "stat").write_text(
                    f"{pid} (fixture process) " + " ".join(fields) + "\n",
                    encoding="utf-8",
                )

            first = proc / "4242"
            first.mkdir()
            write_stat(first, 4242, 123456)
            if mode in {"root-noncanonical", "status-root-only", "noncanonical-then-residual"}:
                target = other_executable
            elif mode in {"deleted-noncanonical", "deleted-noncanonical-then-residual"}:
                target = Path(str(other_executable) + " (deleted)")
            elif mode == "deleted-same-basename":
                target = Path(str(same_basename) + " (deleted)")
            elif mode == "same-basename":
                target = same_basename
            elif mode == "deleted-canonical":
                target = Path(str(executable) + " (deleted)")
            else:
                target = executable
            (first / "exe").symlink_to(target)
            status_values = {
                "kernel-thread": "Kthread:\t1\n",
                "kernel-then-residual": "Kthread:\t1\n",
                "root-noncanonical": "Kthread:\t0\n",
                "root-canonical": "Kthread:\t0\n",
                "root-unreadable-exe": "Kthread:\t0\n",
                "root-empty-exe": "Kthread:\t0\n",
                "root-relative-exe": "Kthread:\t0\n",
                "deleted-canonical": "Kthread:\t0\n",
                "deleted-noncanonical": "Kthread:\t0\n",
                "deleted-noncanonical-then-residual": "Kthread:\t0\n",
                "deleted-same-basename": "Kthread:\t0\n",
                "status-root-only": "Kthread:\t0\n",
                "status-malformed": "Kthread: 0\n",
                "status-invalid": "Kthread:\t2\n",
                "status-duplicate": "Kthread:\t0\nKthread:\t0\n",
            }
            if mode != "status-absent":
                (first / "status").write_text(
                    status_values.get(mode, "Kthread:\t0\n"), encoding="utf-8"
                )
            if mode in {"disappear-then-residual", "kernel-then-residual",
                        "deleted-noncanonical-then-residual", "noncanonical-then-residual"}:
                later = proc / "4343"
                later.mkdir()
                write_stat(later, 4343, 654321)
                (later / "exe").symlink_to(executable)
                (later / "status").write_text("Kthread:\t0\n", encoding="utf-8")

            script = r'''
source ./setup_all.sh
NETCONF_PROC_ROOT=${SCAN_PROC_ROOT}
NETCONF_EXPECTED_EXECUTABLE=${SCAN_EXECUTABLE}
PYTHON_BIN=${SCAN_PYTHON}
NETCONF_RESIDUAL_PID_TIMEOUT_NS=10000000
NETCONF_RESIDUAL_PID_POLL_INTERVAL=0.001
readlink(){
  if [[ "${*: -1}" == "${NETCONF_PROC_ROOT}/4242/exe" ]]; then
    case "${SCAN_MODE}" in
      disappear|disappear-then-residual)
      /bin/rm -rf -- "${NETCONF_PROC_ROOT}/4242"
      return 7
      ;;
      kernel-thread|kernel-then-residual|root-noncanonical|root-canonical|root-unreadable-exe|root-empty-exe|root-relative-exe|status-root-only|status-unreadable|status-absent|status-malformed|status-invalid|status-duplicate)
        return 7
      ;;
    esac
  fi
  command readlink "$@"
}
cat(){
  if [[ "${*: -1}" == "${NETCONF_PROC_ROOT}/4242/status" ]]; then
    case "${SCAN_MODE}" in
      status-root-only|status-unreadable) return 7 ;;
    esac
  fi
  command cat "$@"
}
sudo(){
  [[ "${1:-}" == -n ]] && shift
  case "${1:-}" in
    cat)
      [[ "${SCAN_MODE}" == status-unreadable ]] && return 7
      command cat "${@:2}"
      ;;
    readlink)
      [[ "${SCAN_MODE}" == root-unreadable-exe ]] && return 7
      [[ "${SCAN_MODE}" == root-empty-exe ]] && return 0
      if [[ "${SCAN_MODE}" == root-relative-exe ]]; then
        printf '%s\n' relative/netopeer2-server
        return 0
      fi
      command readlink "${@:2}"
      ;;
    *) return 97 ;;
  esac
}
set +e
output="$(netconf_residual_pids)"
rc=$?
set -e
printf 'SCAN_RC=%s\n' "$rc"
printf 'SCAN_OUTPUT=%s\n' "$output"
'''
            env = dict(os.environ)
            env.update({
                "L2I_REPO_DIR": str(ROOT), "L2I_SETUP_LIB_ONLY": "1",
                "SCAN_PROC_ROOT": str(proc), "SCAN_EXECUTABLE": str(executable),
                "SCAN_MODE": mode, "SCAN_PYTHON": sys.executable,
            })
            return subprocess.run(
                ["bash", "-c", script], cwd=ROOT, env=env,
                text=True, capture_output=True, timeout=4, check=False,
            )

    def run_bounded_residual_harness(self, mode):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / "proc"
            pid_dir = proc / "4242"
            pid_dir.mkdir(parents=True)
            executable = root / "usr" / "sbin" / "netopeer2-server"
            other = root / "opt" / "other-server"
            executable.parent.mkdir(parents=True)
            other.parent.mkdir(parents=True)
            executable.write_text("fixture\n", encoding="utf-8")
            other.write_text("fixture\n", encoding="utf-8")
            executable.chmod(0o755)
            other.chmod(0o755)
            (pid_dir / "exe").symlink_to(executable)
            (pid_dir / "status").write_text("Kthread:\t0\n", encoding="utf-8")
            (pid_dir / "stat").write_text("fixture\n", encoding="utf-8")
            clock = root / "clock"
            start_calls = root / "start-calls"
            incarnation = root / "incarnation"
            poll_log = root / "poll.log"
            output = root / "output"
            clock.write_text("0\n", encoding="utf-8")
            start_calls.write_text("0\n", encoding="utf-8")
            incarnation.write_text("unreadable\n", encoding="utf-8")
            script = r'''
source ./setup_all.sh
NETCONF_PROC_ROOT=${BOUNDED_PROC_ROOT}
NETCONF_EXPECTED_EXECUTABLE=${BOUNDED_EXECUTABLE}
NETCONF_RESIDUAL_PID_TIMEOUT_NS=30
NETCONF_RESIDUAL_PID_POLL_INTERVAL=0
netconf_monotonic_ns(){
  local value
  read -r value < "${BOUNDED_CLOCK}"
  printf '%s\n' "$((value + 10))" > "${BOUNDED_CLOCK}"
  printf '%s\n' "$value"
}
netconf_residual_poll_sleep(){
  printf '%s\n' poll >> "${BOUNDED_POLL_LOG}"
  case "${BOUNDED_MODE}" in
    exe-unreadable-then-gone|status-unreadable-then-gone)
      /bin/rm -rf -- "${NETCONF_PROC_ROOT}/4242" ;;
  esac
}
netconf_pid_start_time(){
  local calls
  read -r calls < "${BOUNDED_START_CALLS}"
  calls=$((calls + 1))
  printf '%s\n' "$calls" > "${BOUNDED_START_CALLS}"
  case "${BOUNDED_MODE}" in
    malformed-starttime) return 1 ;;
    reuse-canonical|reuse-noncanonical)
      if ((calls == 1)); then
        printf '%s\n' 100
      else
        if ((calls == 2)); then
          printf '%s\n' "${BOUNDED_MODE#reuse-}" > "${BOUNDED_INCARNATION}"
        fi
        printf '%s\n' 200
      fi
      ;;
    *) printf '%s\n' 100 ;;
  esac
}
netconf_pid_kthread_state(){
  case "${BOUNDED_MODE}" in
    disappear-during-status)
      /bin/rm -rf -- "${NETCONF_PROC_ROOT}/4242"
      return 2
      ;;
    status-unreadable-then-gone) return 1 ;;
    *) printf '%s\n' 0 ;;
  esac
}
netconf_pid_executable(){
  local incarnation
  case "${BOUNDED_MODE}" in
    exe-unreadable-then-gone|persistent-unreadable) return 1 ;;
    proc-artifact) printf '%s\n' "${NETCONF_PROC_ROOT}/4242/exe" ;;
    reuse-canonical|reuse-noncanonical)
      read -r incarnation < "${BOUNDED_INCARNATION}"
      case "$incarnation" in
        canonical) printf '%s\n' "${BOUNDED_EXECUTABLE}" ;;
        noncanonical) printf '%s\n' "${BOUNDED_OTHER}" ;;
        *) return 1 ;;
      esac
      ;;
    *) printf '%s\n' "${BOUNDED_EXECUTABLE}" ;;
  esac
}
set +e
netconf_residual_pids > "${BOUNDED_OUTPUT}"
rc=$?
set -e
printf 'SCAN_RC=%s\n' "$rc"
printf 'SCAN_OUTPUT=%s\n' "$(paste -sd, - < "${BOUNDED_OUTPUT}")"
printf 'METRICS=%s,%s,%s,%s,%s,%s,%s,%s\n' \
  "$NETCONF_RESIDUAL_ENUMERATED_COUNT" "$NETCONF_RESIDUAL_CANONICAL_COUNT" \
  "$NETCONF_RESIDUAL_NONCANONICAL_COUNT" "$NETCONF_RESIDUAL_KERNEL_THREAD_COUNT" \
  "$NETCONF_RESIDUAL_GONE_COUNT" "$NETCONF_RESIDUAL_UNRESOLVED_COUNT" \
  "$NETCONF_RESIDUAL_REEVALUATED_PID_COUNT" "$NETCONF_RESIDUAL_REEVALUATION_COUNT"
printf 'POLL_COUNT=%s\n' "$(wc -l < "${BOUNDED_POLL_LOG}" 2>/dev/null || printf 0)"
printf 'CLOCK_FINAL=%s\n' "$(cat "${BOUNDED_CLOCK}")"
'''
            env = dict(os.environ)
            env.update({
                "L2I_REPO_DIR": str(ROOT), "L2I_SETUP_LIB_ONLY": "1",
                "BOUNDED_PROC_ROOT": str(proc), "BOUNDED_EXECUTABLE": str(executable),
                "BOUNDED_OTHER": str(other), "BOUNDED_CLOCK": str(clock),
                "BOUNDED_START_CALLS": str(start_calls),
                "BOUNDED_INCARNATION": str(incarnation),
                "BOUNDED_POLL_LOG": str(poll_log), "BOUNDED_OUTPUT": str(output),
                "BOUNDED_MODE": mode,
            })
            return subprocess.run(
                ["bash", "-c", script], cwd=ROOT, env=env,
                text=True, capture_output=True, timeout=4, check=False,
            )

    def test_bounded_scan_accepts_only_observed_disappearance(self):
        for mode in ("disappear-during-status", "exe-unreadable-then-gone",
                     "status-unreadable-then-gone"):
            with self.subTest(mode=mode):
                completed = self.run_bounded_residual_harness(mode)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("SCAN_RC=0", completed.stdout)
                self.assertIn("SCAN_OUTPUT=\n", completed.stdout)
                self.assertIn("METRICS=1,0,0,0,1,0", completed.stdout)

    def test_bounded_scan_fails_closed_when_user_identity_stays_unreadable(self):
        completed = self.run_bounded_residual_harness("persistent-unreadable")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=1", completed.stdout)
        self.assertIn("METRICS=1,0,0,0,0,1,1,1", completed.stdout)
        self.assertIn("POLL_COUNT=1", completed.stdout)
        self.assertIn("CLOCK_FINAL=40", completed.stdout)

    def test_bounded_scan_reclassifies_reused_pid_as_canonical(self):
        completed = self.run_bounded_residual_harness("reuse-canonical")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=0", completed.stdout)
        self.assertIn("SCAN_OUTPUT=4242", completed.stdout)
        self.assertIn("METRICS=1,1,0,0,0,0,1,1", completed.stdout)

    def test_bounded_scan_reclassifies_reused_pid_as_noncanonical(self):
        completed = self.run_bounded_residual_harness("reuse-noncanonical")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=0", completed.stdout)
        self.assertIn("SCAN_OUTPUT=\n", completed.stdout)
        self.assertIn("METRICS=1,0,1,0,0,0,1,1", completed.stdout)

    def test_bounded_scan_rejects_malformed_starttime_and_proc_artifact(self):
        for mode in ("malformed-starttime", "proc-artifact"):
            with self.subTest(mode=mode):
                completed = self.run_bounded_residual_harness(mode)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("SCAN_RC=1", completed.stdout)
                self.assertIn("METRICS=1,0,0,0,0,1,1,1", completed.stdout)

    def test_residual_deadline_is_created_once_and_never_restarted(self):
        source = self.lifecycle_source()
        assignment = "deadline_ns=$((start_ns + NETCONF_RESIDUAL_PID_TIMEOUT_NS))"
        self.assertEqual(source.count(assignment), 1)
        classifier = source[source.index("netconf_classify_residual_pid()"):
                            source.index("netconf_residual_pids()")]
        self.assertLess(classifier.index(assignment), classifier.index("while :; do"))
        self.assertIn("NETCONF_RESIDUAL_PID_POLL_INTERVAL=0.05", source)

    def test_residual_scan_ignores_proven_kernel_thread_without_executable(self):
        completed = self.run_residual_scan_harness("kernel-thread")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=0", completed.stdout)
        self.assertIn("SCAN_OUTPUT=\n", completed.stdout)

    def test_residual_scan_uses_root_for_noncanonical_user_process(self):
        completed = self.run_residual_scan_harness("root-noncanonical")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=0", completed.stdout)
        self.assertIn("SCAN_OUTPUT=\n", completed.stdout)

    def test_residual_scan_uses_root_and_reports_canonical_process(self):
        completed = self.run_residual_scan_harness("root-canonical")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=0", completed.stdout)
        self.assertIn("SCAN_OUTPUT=4242", completed.stdout)

    def test_residual_scan_reports_live_canonical_process(self):
        completed = self.run_residual_scan_harness("live-canonical")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=0", completed.stdout)
        self.assertIn("SCAN_OUTPUT=4242", completed.stdout)

    def test_real_readlink_semantics_expose_exact_deleted_canonical_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "usr" / "sbin" / "netopeer2-server"
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture\n", encoding="utf-8")
            link = root / "exe"
            deleted = Path(str(executable) + " (deleted)")
            link.symlink_to(deleted)
            strict = subprocess.run(
                ["readlink", "-e", "--", str(link)], text=True,
                capture_output=True, check=False,
            )
            fallback = subprocess.run(
                ["readlink", "-f", "--", str(link)], text=True,
                capture_output=True, check=False,
            )
            self.assertNotEqual(strict.returncode, 0)
            self.assertEqual(fallback.returncode, 0, fallback.stderr)
            self.assertEqual(fallback.stdout, str(deleted) + "\n")

    def test_residual_scan_reports_exact_deleted_canonical_process(self):
        completed = self.run_residual_scan_harness("deleted-canonical")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=0", completed.stdout)
        self.assertIn("SCAN_OUTPUT=4242", completed.stdout)

    def test_residual_scan_ignores_deleted_noncanonical_paths(self):
        for mode in ("deleted-noncanonical", "deleted-same-basename"):
            with self.subTest(mode=mode):
                completed = self.run_residual_scan_harness(mode)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("SCAN_RC=0", completed.stdout)
                self.assertIn("SCAN_OUTPUT=\n", completed.stdout)

    def test_residual_scan_fails_closed_on_empty_or_relative_privileged_result(self):
        for mode in ("root-empty-exe", "root-relative-exe"):
            with self.subTest(mode=mode):
                completed = self.run_residual_scan_harness(mode)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("SCAN_RC=1", completed.stdout)
                self.assertIn("SCAN_OUTPUT=\n", completed.stdout)

    def test_residual_scan_continues_after_deleted_noncanonical_process(self):
        for mode in ("deleted-noncanonical-then-residual", "noncanonical-then-residual"):
            with self.subTest(mode=mode):
                completed = self.run_residual_scan_harness(mode)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("SCAN_RC=0", completed.stdout)
                self.assertIn("SCAN_OUTPUT=4343", completed.stdout)

    def test_residual_scan_rejects_same_basename_at_different_path(self):
        completed = self.run_residual_scan_harness("same-basename")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=0", completed.stdout)
        self.assertIn("SCAN_OUTPUT=\n", completed.stdout)

    def test_preflight_blocks_deleted_canonical_residual(self):
        completed = self.run_harness("deleted-canonical-residual")
        self.assertNotIn("RC=0", completed.stdout.splitlines())
        self.assertIn("Processo Netopeer2 residual detectado", completed.stderr)
        self.assertNotIn("start", completed.stdout.splitlines())

    def test_residual_scan_fails_when_root_cannot_resolve_user_executable(self):
        completed = self.run_residual_scan_harness("root-unreadable-exe")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=1", completed.stdout)
        self.assertIn("SCAN_OUTPUT=\n", completed.stdout)

    def test_residual_scan_fails_on_unclassifiable_or_malformed_status(self):
        for mode in ("status-unreadable", "status-absent", "status-malformed",
                     "status-invalid", "status-duplicate"):
            with self.subTest(mode=mode):
                completed = self.run_residual_scan_harness(mode)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("SCAN_RC=1", completed.stdout)
                self.assertIn("SCAN_OUTPUT=\n", completed.stdout)

    def test_residual_scan_uses_root_to_classify_other_user_status(self):
        completed = self.run_residual_scan_harness("status-root-only")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=0", completed.stdout)
        self.assertIn("SCAN_OUTPUT=\n", completed.stdout)

    def test_residual_scan_accepts_pid_disappearance_during_read(self):
        completed = self.run_residual_scan_harness("disappear")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=0", completed.stdout)
        self.assertIn("SCAN_OUTPUT=\n", completed.stdout)

    def test_residual_scan_continues_after_race_and_finds_later_process(self):
        completed = self.run_residual_scan_harness("disappear-then-residual")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=0", completed.stdout)
        self.assertIn("SCAN_OUTPUT=4343", completed.stdout)

    def test_residual_scan_continues_after_kernel_thread_and_finds_later_process(self):
        completed = self.run_residual_scan_harness("kernel-then-residual")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SCAN_RC=0", completed.stdout)
        self.assertIn("SCAN_OUTPUT=4343", completed.stdout)

    def test_residual_scan_uses_only_exact_kthread_status_field(self):
        source = self.lifecycle_source()
        self.assertIn("$'Kthread:\\t0'", source)
        self.assertIn("$'Kthread:\\t1'", source)
        self.assertIn('sudo -n cat -- "$status_path"', source)
        self.assertIn('sudo -n readlink -f -- "$link"', source)
        self.assertIn('"$executable" == "$expected (deleted)"', source)
        self.assertIn('"$executable" == /*', source)
        self.assertIn('"$executable" != "$NETCONF_PROC_ROOT/"*', source)
        for forbidden in ("/comm", "/cmdline", "pgrep ", " pid == 2", " pid == 13"):
            self.assertNotIn(forbidden, source)

    def run_cleanup_fingerprint_harness(self, scenario):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / "proc"
            executable = root / "usr" / "sbin" / "netopeer2-server"
            other_executable = root / "usr" / "sbin" / "other-server"
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture\n", encoding="utf-8")
            other_executable.write_text("fixture\n", encoding="utf-8")
            executable.chmod(0o755)
            other_executable.chmod(0o755)

            def create_pid(pid, start_time, group):
                pid_dir = proc / str(pid)
                pid_dir.mkdir(parents=True)
                (pid_dir / "exe").symlink_to(executable)
                (pid_dir / "status").write_text("Kthread:\t0\n", encoding="utf-8")
                (pid_dir / "cgroup").write_text(f"0::{group}\n", encoding="utf-8")
                fields = ["S"] + ["0"] * 18 + [str(start_time)]
                (pid_dir / "stat").write_text(
                    f"{pid} (netopeer2 server) " + " ".join(fields) + "\n",
                    encoding="utf-8",
                )

            create_pid(4242, 123456, "/system.slice/netopeer2-server.service")
            if scenario == "additional-residual":
                create_pid(4343, 654321, "/user.slice/unrelated.service")
            clock = root / "clock"
            clock.write_text("0\n", encoding="utf-8")
            log = root / "commands.log"
            log.write_text("", encoding="utf-8")
            script = r'''
source ./setup_all.sh
NETCONF_PROC_ROOT=${FINGERPRINT_PROC_ROOT}
NETCONF_EXPECTED_EXECUTABLE=${FINGERPRINT_EXECUTABLE}
UNIT_STATE=active
MAIN_PID=4242
systemctl(){
  case "$*" in
    *LoadState*) printf '%s\n' loaded ;;
    *ControlGroup*) printf '%s\n' /system.slice/netopeer2-server.service ;;
    *ActiveState*) printf '%s\n' "$UNIT_STATE" ;;
    *MainPID*) printf '%s\n' "$MAIN_PID" ;;
  esac
}
netconf_main_pid(){ printf '%s\n' "$MAIN_PID"; }
port_listening(){ return 1; }
netconf_monotonic_ns(){
  local value
  read -r value < "${FINGERPRINT_CLOCK}"
  printf '%s\n' "$value"
  printf '%s\n' "$((value + 6000000000))" > "${FINGERPRINT_CLOCK}"
}
netconf_poll_sleep(){ :; }
sleep(){ :; }
sudo(){
  [[ "${1:-}" == -n ]] && shift
  case "${1:-}:${2:-}" in
    systemctl:stop)
      printf '%s\n' stop >> "${FINGERPRINT_LOG}"
      UNIT_STATE=inactive
      MAIN_PID=0
      case "${FINGERPRINT_SCENARIO}" in
        pid-reused)
          printf '%s\n' '4242 (netopeer2 server) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 999999' > "${NETCONF_PROC_ROOT}/4242/stat" ;;
        executable-changed)
          /bin/rm -f -- "${NETCONF_PROC_ROOT}/4242/exe"
          /bin/ln -s -- "${FINGERPRINT_OTHER_EXECUTABLE}" "${NETCONF_PROC_ROOT}/4242/exe" ;;
        cgroup-changed)
          printf '%s\n' '0::/user.slice/adversary.service' > "${NETCONF_PROC_ROOT}/4242/cgroup" ;;
        stat-read-error)
          /bin/rm -f -- "${NETCONF_PROC_ROOT}/4242/stat" ;;
        exe-read-error)
          /bin/rm -f -- "${NETCONF_PROC_ROOT}/4242/exe"
          /bin/ln -s -- "${FINGERPRINT_OTHER_EXECUTABLE}.missing" "${NETCONF_PROC_ROOT}/4242/exe" ;;
        cgroup-read-error)
          /bin/rm -f -- "${NETCONF_PROC_ROOT}/4242/cgroup" ;;
      esac
      return 0 ;;
    kill:-0)
      [[ -d "${NETCONF_PROC_ROOT}/${3:-}" ]] ;;
    kill:-TERM)
      printf 'signal:TERM:%s\n' "${3:-}" >> "${FINGERPRINT_LOG}"
      if [[ "${FINGERPRINT_SCENARIO}" == dies-after-term ]]; then
        /bin/rm -rf -- "${NETCONF_PROC_ROOT}/${3:-}"
      fi ;;
    kill:-KILL)
      printf 'signal:KILL:%s\n' "${3:-}" >> "${FINGERPRINT_LOG}"
      /bin/rm -rf -- "${NETCONF_PROC_ROOT}/${3:-}" ;;
    rm:*) return 0 ;;
    *) return 0 ;;
  esac
}
if [[ "${FINGERPRINT_SCENARIO}" == residual-read-error ]]; then
  netconf_residual_pids(){ return 7; }
fi
set +e
netconf_systemd_stop_once 4242
rc=$?
set -e
printf 'CLEANUP_RC=%s\n' "$rc"
cat "${FINGERPRINT_LOG}"
'''
            env = dict(os.environ)
            env.update({
                "L2I_REPO_DIR": str(ROOT), "L2I_SETUP_LIB_ONLY": "1",
                "FINGERPRINT_PROC_ROOT": str(proc),
                "FINGERPRINT_EXECUTABLE": str(executable),
                "FINGERPRINT_OTHER_EXECUTABLE": str(other_executable),
                "FINGERPRINT_CLOCK": str(clock),
                "FINGERPRINT_LOG": str(log),
                "FINGERPRINT_SCENARIO": scenario,
            })
            return subprocess.run(
                ["bash", "-c", script], cwd=ROOT, env=env,
                text=True, capture_output=True, timeout=4, check=False,
            )

    def test_authenticated_process_persists_after_stop_and_reaches_term_kill(self):
        completed = self.run_cleanup_fingerprint_harness("persistent")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertIn("CLEANUP_RC=0", lines)
        self.assertEqual(lines.count("stop"), 1)
        self.assertEqual(lines.count("signal:TERM:4242"), 1)
        self.assertEqual(lines.count("signal:KILL:4242"), 1)

    def test_process_disappearing_after_term_is_not_killed(self):
        completed = self.run_cleanup_fingerprint_harness("dies-after-term")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertIn("CLEANUP_RC=0", lines)
        self.assertEqual(lines.count("signal:TERM:4242"), 1)
        self.assertNotIn("signal:KILL:4242", lines)

    def test_pid_reuse_is_never_signalled_and_cleanup_fails(self):
        completed = self.run_cleanup_fingerprint_harness("pid-reused")
        lines = completed.stdout.splitlines()
        self.assertIn("CLEANUP_RC=90", lines)
        self.assertFalse(any(line.startswith("signal:") for line in lines))
        self.assertIn("NETCONF_CLEANUP_DIAGNOSTIC_BEGIN", completed.stdout)
        self.assertIn("NETCONF_CLEANUP_DIAGNOSTIC_END", completed.stdout)

    def test_executable_or_cgroup_change_is_never_signalled(self):
        for scenario in ("executable-changed", "cgroup-changed"):
            with self.subTest(scenario=scenario):
                completed = self.run_cleanup_fingerprint_harness(scenario)
                lines = completed.stdout.splitlines()
                self.assertIn("CLEANUP_RC=90", lines)
                self.assertFalse(any(line.startswith("signal:") for line in lines))

    def test_additional_residual_prevents_any_fallback_signal(self):
        completed = self.run_cleanup_fingerprint_harness("additional-residual")
        lines = completed.stdout.splitlines()
        self.assertIn("CLEANUP_RC=90", lines)
        self.assertFalse(any(line.startswith("signal:") for line in lines))

    def test_proc_and_residual_read_failures_are_closed_without_signal(self):
        scenarios = ("stat-read-error", "exe-read-error", "cgroup-read-error", "residual-read-error")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                completed = self.run_cleanup_fingerprint_harness(scenario)
                lines = completed.stdout.splitlines()
                self.assertIn("CLEANUP_RC=90", lines)
                self.assertFalse(any(line.startswith("signal:") for line in lines))

    def run_identity_harness(self, mode):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / "proc"
            executable = root / "usr" / "sbin" / "netopeer2-server"
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture\n", encoding="utf-8")
            executable.chmod(0o755)
            pid_dir = proc / "4242"
            pid_dir.mkdir(parents=True)
            target = executable
            group = "/system.slice/netopeer2-server.service"
            if mode == "forged-command-line":
                target = root / "usr" / "bin" / "python3"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("fixture\n", encoding="utf-8")
                target.chmod(0o755)
                group = "/user.slice/adversary.service"
            elif mode == "outside-unit":
                group = "/user.slice/unrelated.service"
            (pid_dir / "exe").symlink_to(target)
            (pid_dir / "status").write_text("Kthread:\t0\n", encoding="utf-8")
            fields = ["S"] + ["0"] * 18 + ["123456"]
            (pid_dir / "stat").write_text(
                "4242 (fixture process) " + " ".join(fields) + "\n", encoding="utf-8"
            )
            (pid_dir / "cgroup").write_text(f"0::{group}\n", encoding="utf-8")
            decoy_dir = proc / "4343"
            decoy_dir.mkdir()
            decoy = root / "usr" / "sbin" / "netopeer2-server-helper"
            decoy.write_text("fixture\n", encoding="utf-8")
            decoy.chmod(0o755)
            (decoy_dir / "exe").symlink_to(decoy)
            (decoy_dir / "status").write_text("Kthread:\t0\n", encoding="utf-8")
            fields[-1] = "654321"
            (decoy_dir / "stat").write_text(
                "4343 (fixture helper) " + " ".join(fields) + "\n", encoding="utf-8"
            )
            (decoy_dir / "cgroup").write_text(
                "0::/system.slice/netopeer2-server.service\n", encoding="utf-8"
            )
            script = r'''
source ./setup_all.sh
NETCONF_PROC_ROOT=${IDENTITY_PROC_ROOT}
NETCONF_EXPECTED_EXECUTABLE=${IDENTITY_EXECUTABLE}
PYTHON_BIN=${IDENTITY_PYTHON}
systemctl(){
  case "$*" in
    *LoadState*) printf '%s\n' loaded ;;
    *MainPID*) printf '%s\n' 4242 ;;
    *ControlGroup*) printf '%s\n' /system.slice/netopeer2-server.service ;;
  esac
}
sudo(){ [[ "${1:-}" == -n ]] && shift; [[ "${1:-}:${2:-}" == kill:-0 ]]; }
ps(){ printf '%s\n' netopeer2-serv; printf '%s\n' PS_CALLED >> "${IDENTITY_LOG}"; }
set +e
netconf_pid_valid 4242
rc=$?
set -e
printf 'VALID_RC=%s\n' "$rc"
printf 'RESIDUALS='
netconf_residual_pids | paste -sd, -
test ! -s "${IDENTITY_LOG}" && printf 'PS_UNUSED=True\n'
'''
            env = dict(os.environ)
            env.update({
                "L2I_REPO_DIR": str(ROOT), "L2I_SETUP_LIB_ONLY": "1",
                "IDENTITY_PROC_ROOT": str(proc),
                "IDENTITY_EXECUTABLE": str(executable),
                "IDENTITY_LOG": str(root / "ps.log"),
                "IDENTITY_PYTHON": sys.executable,
            })
            return subprocess.run(
                ["bash", "-c", script], cwd=ROOT, env=env,
                text=True, capture_output=True, timeout=4, check=False,
            )

    def test_identity_accepts_real_executable_despite_truncated_comm(self):
        completed = self.run_identity_harness("valid")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("VALID_RC=0", completed.stdout)
        self.assertIn("RESIDUALS=4242", completed.stdout)
        self.assertIn("PS_UNUSED=True", completed.stdout)

    def test_forged_command_line_and_correct_executable_outside_unit_are_rejected(self):
        for mode in ("forged-command-line", "outside-unit"):
            with self.subTest(mode=mode):
                completed = self.run_identity_harness(mode)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("VALID_RC=1", completed.stdout)
                self.assertIn("PS_UNUSED=True", completed.stdout)
                if mode == "forged-command-line":
                    self.assertIn("RESIDUALS=", completed.stdout)
                    self.assertNotIn("RESIDUALS=4242", completed.stdout)

    def run_pid_executable_reader_harness(self, mode):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / "proc"
            pid_dir = proc / "4242"
            pid_dir.mkdir(parents=True)
            executable = root / "usr" / "sbin" / "netopeer2-server"
            other = root / "opt" / "other-server"
            executable.parent.mkdir(parents=True)
            other.parent.mkdir(parents=True)
            executable.write_text("fixture\n", encoding="utf-8")
            other.write_text("fixture\n", encoding="utf-8")
            executable.chmod(0o755)
            other.chmod(0o755)
            target = other if mode == "different" else executable
            if mode == "deleted":
                target = Path(str(executable) + " (deleted)")
            (pid_dir / "exe").symlink_to(target)
            log = root / "sudo.log"
            log.write_text("", encoding="utf-8")
            script = r'''
source ./setup_all.sh
NETCONF_PROC_ROOT=${READER_PROC_ROOT}
readlink(){
  if [[ "${*: -1}" == "${NETCONF_PROC_ROOT}/4242/exe" ]]; then
    case "${READER_MODE}" in
      unprivileged|different) command readlink "$@" ;;
      disappear-before) /bin/rm -rf -- "${NETCONF_PROC_ROOT}/4242"; return 7 ;;
      *) return 7 ;;
    esac
    return
  fi
  command readlink "$@"
}
sudo(){
  [[ "${1:-}" == -n ]] && shift
  printf 'sudo:%s\n' "$*" >> "${READER_LOG}"
  [[ "${1:-}" == readlink ]] || return 97
  case "${READER_MODE}" in
    privileged|deleted) command readlink "${@:2}" ;;
    empty) return 0 ;;
    relative) printf '%s\n' relative/netopeer2-server ;;
    multiline) printf '%s\n%s\n' "${READER_EXECUTABLE}" /another/path ;;
    nul) printf '%s\0\n' "${READER_EXECUTABLE}" ;;
    disappear-after)
      printf '%s\n' "${READER_EXECUTABLE}"
      /bin/rm -rf -- "${NETCONF_PROC_ROOT}/4242"
      ;;
    *) return 7 ;;
  esac
}
set +e
value="$(netconf_pid_executable 4242)"
rc=$?
set -e
printf 'READER_RC=%s\n' "$rc"
printf 'READER_VALUE=%s\n' "$value"
printf 'SUDO_CALLS=%s\n' "$(wc -l < "${READER_LOG}")"
'''
            env = dict(os.environ)
            env.update({
                "L2I_REPO_DIR": str(ROOT), "L2I_SETUP_LIB_ONLY": "1",
                "READER_PROC_ROOT": str(proc), "READER_MODE": mode,
                "READER_EXECUTABLE": str(executable), "READER_LOG": str(log),
            })
            return subprocess.run(
                ["bash", "-c", script], cwd=ROOT, env=env,
                text=True, capture_output=True, timeout=4, check=False,
            )

    def test_pid_executable_reader_privileged_contract(self):
        expectations = {
            "unprivileged": ("0", "0"),
            "privileged": ("0", "1"),
            "sudo-fail": ("1", "1"),
            "empty": ("1", "1"),
            "relative": ("1", "1"),
            "multiline": ("1", "1"),
            "nul": ("1", "1"),
            "disappear-before": ("2", "0"),
            "disappear-after": ("2", "1"),
            "unreadable": ("1", "1"),
            "different": ("0", "0"),
            "deleted": ("0", "1"),
        }
        for mode, (rc, sudo_calls) in expectations.items():
            with self.subTest(mode=mode):
                completed = self.run_pid_executable_reader_harness(mode)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn(f"READER_RC={rc}", completed.stdout)
                self.assertIn(f"SUDO_CALLS={sudo_calls}", completed.stdout)

    def run_privileged_mainpid_harness(self, target_mode):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / "proc"
            pid_dir = proc / "4242"
            pid_dir.mkdir(parents=True)
            executable = root / "usr" / "sbin" / "netopeer2-server"
            other = root / "opt" / "other-server"
            executable.parent.mkdir(parents=True)
            other.parent.mkdir(parents=True)
            executable.write_text("fixture\n", encoding="utf-8")
            other.write_text("fixture\n", encoding="utf-8")
            executable.chmod(0o755)
            other.chmod(0o755)
            target = executable
            if target_mode == "different":
                target = other
            elif target_mode == "deleted":
                target = Path(str(executable) + " (deleted)")
            (pid_dir / "exe").symlink_to(target)
            (pid_dir / "cgroup").write_text(
                "0::/system.slice/netopeer2-server.service\n", encoding="utf-8"
            )
            fields = ["S"] + ["0"] * 18 + ["123456"]
            (pid_dir / "stat").write_text(
                "4242 (netopeer2 server) " + " ".join(fields) + "\n",
                encoding="utf-8",
            )
            script = r'''
source ./setup_all.sh
NETCONF_PROC_ROOT=${MAINPID_PROC_ROOT}
NETCONF_EXPECTED_EXECUTABLE=${MAINPID_EXECUTABLE}
systemctl(){
  case "$*" in
    *LoadState*) printf '%s\n' loaded ;;
    *MainPID*) printf '%s\n' 4242 ;;
    *ControlGroup*) printf '%s\n' /system.slice/netopeer2-server.service ;;
  esac
}
readlink(){
  if [[ "${*: -1}" == "${NETCONF_PROC_ROOT}/4242/exe" && "${1:-}" == -e ]]; then
    return 7
  fi
  command readlink "$@"
}
sudo(){
  [[ "${1:-}" == -n ]] && shift
  case "${1:-}:${2:-}" in
    kill:-0) return 0 ;;
    readlink:-f) command readlink "${@:2}" ;;
    *) return 97 ;;
  esac
}
set +e
netconf_pid_valid 4242
valid_rc=$?
fingerprint="$(netconf_capture_process_fingerprint 4242)"
fingerprint_rc=$?
set -e
printf 'VALID_RC=%s\n' "$valid_rc"
printf 'FINGERPRINT_RC=%s\n' "$fingerprint_rc"
printf 'FINGERPRINT_FIELDS=%s\n' "$(printf '%s\n' "$fingerprint" | wc -l)"
'''
            env = dict(os.environ)
            env.update({
                "L2I_REPO_DIR": str(ROOT), "L2I_SETUP_LIB_ONLY": "1",
                "MAINPID_PROC_ROOT": str(proc),
                "MAINPID_EXECUTABLE": str(executable),
            })
            return subprocess.run(
                ["bash", "-c", script], cwd=ROOT, env=env,
                text=True, capture_output=True, timeout=4, check=False,
            )

    def test_privileged_mainpid_readiness_requires_exact_live_executable(self):
        valid = self.run_privileged_mainpid_harness("canonical")
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertIn("VALID_RC=0", valid.stdout)
        self.assertIn("FINGERPRINT_RC=0", valid.stdout)
        self.assertIn("FINGERPRINT_FIELDS=4", valid.stdout)
        for mode in ("different", "deleted"):
            with self.subTest(mode=mode):
                rejected = self.run_privileged_mainpid_harness(mode)
                self.assertEqual(rejected.returncode, 0, rejected.stderr)
                self.assertIn("VALID_RC=1", rejected.stdout)
                self.assertIn("FINGERPRINT_RC=1", rejected.stdout)

    def run_stability_harness(self, clocks, pids, ports):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clock_file = root / "clocks"
            pid_file = root / "pids"
            port_file = root / "ports"
            clock_file.write_text("\n".join(str(value) for value in clocks) + "\n", encoding="utf-8")
            pid_file.write_text("\n".join(str(value) for value in pids) + "\n", encoding="utf-8")
            port_file.write_text("\n".join("1" if value else "0" for value in ports) + "\n", encoding="utf-8")
            script = r'''
source ./setup_all.sh
queue_pop(){
  local file="$1" value
  value="$(head -n 1 "$file")"
  tail -n +2 "$file" > "$file.next"
  mv "$file.next" "$file"
  printf '%s\n' "$value"
}
netconf_monotonic_ns(){ queue_pop "${CLOCK_FILE}"; }
netconf_main_pid(){ queue_pop "${PID_FILE}"; }
port_listening(){ [[ "$(queue_pop "${PORT_FILE}")" == 1 ]]; }
netconf_unit_active_running(){ return 0; }
netconf_pid_valid(){ return 0; }
netconf_process_identity(){ printf '%s|/usr/local/sbin/netopeer2-server|/system.slice/netopeer2-server.service\n' "$1"; }
netconf_poll_sleep(){ :; }
set +e
netconf_stability_gate
rc=$?
set -e
printf 'STABILITY_RC=%s\n' "$rc"
'''
            env = dict(os.environ)
            env.update({
                "L2I_REPO_DIR": str(ROOT), "L2I_SETUP_LIB_ONLY": "1",
                "CLOCK_FILE": str(clock_file), "PID_FILE": str(pid_file),
                "PORT_FILE": str(port_file),
            })
            return subprocess.run(
                ["bash", "-c", script], cwd=ROOT, env=env,
                text=True, capture_output=True, timeout=4, check=False,
            )

    def test_monotonic_boundary_rejects_1999_and_accepts_2000_ms(self):
        rejected = self.run_stability_harness(
            (0, 0, 1_999_000_000, 60_000_000_001),
            (4242, 4242, 4242), (True, True, True),
        )
        accepted = self.run_stability_harness(
            (0, 0, 2_000_000_000), (4242, 4242), (True, True),
        )
        self.assertIn("STABILITY_RC=1", rejected.stdout, rejected.stderr)
        self.assertIn("STABILITY_RC=0", accepted.stdout, accepted.stderr)

    def test_main_pid_change_and_port_oscillation_reset_entire_window(self):
        pid_changed = self.run_stability_harness(
            (0, 0, 1_000_000_000, 2_999_000_000, 60_000_000_001),
            (4242, 4343, 4343, 4343), (True, True, True, True),
        )
        port_oscillated = self.run_stability_harness(
            (0, 0, 1_000_000_000, 2_000_000_000, 3_999_000_000, 60_000_000_001),
            (4242, 4242, 4242, 4242, 4242), (True, False, True, True, True),
        )
        accepted_after_reset = self.run_stability_harness(
            (0, 0, 1_000_000_000, 3_000_000_000),
            (4242, 4343, 4343), (True, True, True),
        )
        self.assertIn("STABILITY_RC=1", pid_changed.stdout, pid_changed.stderr)
        self.assertIn("STABILITY_RC=1", port_oscillated.stdout, port_oscillated.stderr)
        self.assertIn("STABILITY_RC=0", accepted_after_reset.stdout, accepted_after_reset.stderr)


if __name__ == "__main__":
    unittest.main()
