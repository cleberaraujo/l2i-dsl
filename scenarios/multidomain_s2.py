#!/usr/bin/env python3
"""Prospective canonical S2 operational engine with raw multicast lineage.

This is an operational engine, not a campaign, fixture, or qualification helper. It sends
real UDP multicast packets through the selected testbed, records sender and
receiver timestamps, observes receiver membership transitions, verifies the
P4Runtime PRE state in the real implementation, and rolls every transient
resource back before returning.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, DecimalException, ROUND_FLOOR
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from typing import Any

from l2i.s2_recovery_observation import (
    EXCLUDED_REQUIREMENTS,
    PREDICATE_ID,
    SCHEMA_ID,
    build_recovery_observation,
    percentile_type7,
)

GROUP = "239.1.1.1"
PORT = 5001
SOURCE = "A:h1"
RECEIVERS = {"B:h3": ("h3", "10.0.0.3"), "C:h4": ("h4", "10.0.0.4")}
SOURCE_NS = "h1"
SOURCE_IP = "10.0.0.1"
PROBE_NS = "h2"
DEFAULT_PACKET_INTERVAL_MS = 50
DEFAULT_RECOVERY_BIN_MS = 500
DEFAULT_STABLE_K_BINS = 3
READINESS_TIMEOUT_S = 3.0
READINESS_POLL_INTERVAL_S = 0.05
READBACK_COMMAND_TIMEOUT_S = 0.5
CHILD_LIFETIME_MARGIN_S = 1.0
P4_FUNCTIONAL_TOKEN = "P4_S2_PROGRAM_FUNCTIONAL_OK"
EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
GROUP_MEMBERSHIP_RE = re.compile(r"(?m)^\s*inet\s+239\.1\.1\.1(?:\s|$)")


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    os.replace(temporary, path)


def run(
    argv: list[str], *, check: bool = True, timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(argv, text=True, capture_output=True, shell=False, timeout=timeout)
    if check and cp.returncode:
        raise RuntimeError(f"command failed ({cp.returncode}): {argv!r}: {cp.stderr}")
    return cp


RECEIVER_CODE = r'''
import json, socket, struct, sys, time
group, port, interface, endpoint, mode, duration = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], float(sys.argv[6])
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(("",port));s.settimeout(.30)
mreq=socket.inet_aton(group)+socket.inet_aton(interface);s.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,mreq)
events=[{"event":"membership_join","endpoint":endpoint,"observed_ns":time.time_ns()}];packets=[];deadline=time.monotonic()+duration+1.0;cycled=False
while time.monotonic()<deadline:
  try:data,addr=s.recvfrom(65535)
  except socket.timeout:continue
  received=time.time_ns();doc=json.loads(data);packets.append({"sequence":doc["sequence"],"payload":doc["payload"],"bytes":len(data),"sent_ns":doc["sent_ns"],"received_ns":received,"source_ip":addr[0]})
  if mode=="adapt" and len(packets)==5 and not cycled:
    events.append({"event":"membership_leave","endpoint":endpoint,"observed_ns":time.time_ns()});s.setsockopt(socket.IPPROTO_IP,socket.IP_DROP_MEMBERSHIP,mreq);time.sleep(.20);s.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,mreq);events.append({"event":"membership_rejoin","endpoint":endpoint,"observed_ns":time.time_ns()});cycled=True
events.append({"event":"receiver_stop","endpoint":endpoint,"observed_ns":time.time_ns()});print(json.dumps({"endpoint":endpoint,"packets":packets,"events":events},sort_keys=True))
'''

PROBE_CODE = r'''
import json,socket,sys,time
port,duration=int(sys.argv[1]),float(sys.argv[2]);s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(("",port));s.settimeout(.2);packets=[];deadline=time.monotonic()+duration+1
while time.monotonic()<deadline:
  try:data,addr=s.recvfrom(65535);packets.append({"bytes":len(data),"source_ip":addr[0],"received_ns":time.time_ns()})
  except socket.timeout:pass
print(json.dumps({"membership":False,"packets":packets},sort_keys=True))
'''

SENDER_CODE = r'''
import json,socket,sys,time
group,port,interface,count,interval=sys.argv[1],int(sys.argv[2]),sys.argv[3],int(sys.argv[4]),float(sys.argv[5]);s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP);s.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_IF,socket.inet_aton(interface));s.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_TTL,4);rows=[]
for seq in range(1,count+1):
 sent=time.time_ns();payload=hashlib.sha256(("s2-r4-%d"%seq).encode()).hexdigest();body=json.dumps({"sequence":seq,"sent_ns":sent,"payload":payload},sort_keys=True).encode();s.sendto(body,(group,port));rows.append({"sequence":seq,"sent_ns":sent,"payload":payload,"bytes":len(body)});time.sleep(interval)
print(json.dumps({"packets":rows},sort_keys=True))
'''.replace("import json,socket,sys,time", "import hashlib,json,socket,sys,time")


def raw_event(sequence: int, event: str, endpoint: str, payload: str,
              sent_ns: int, received_ns: int, size: int) -> dict[str, Any]:
    return {
        "schema": "phase4sc-operational-raw-v3", "scenario": "S2",
        "event": event, "sequence": sequence, "endpoint": endpoint,
        "traffic_class": "multicast", "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "sent_ns": sent_ns, "received_ns": received_ns, "bytes": size,
        "status": "sent" if event == "sent" else "received",
    }


def positive_int(value: str) -> int:
    converted = int(value)
    if converted < 1:
        raise argparse.ArgumentTypeError("must be an integer >= 1")
    return converted


def positive_float(value: str) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero") from exc
    if not math.isfinite(converted):
        raise argparse.ArgumentTypeError("must be finite")
    if converted <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return converted


def expected_packet_count(duration_s: float, packet_interval_ms: int) -> int:
    duration = finite_number(duration_s, "duration")
    if duration <= 0:
        raise ValueError("duration must be finite and greater than zero")
    try:
        duration_ms = Decimal(str(duration)) * Decimal(1000)
        count = int((duration_ms / Decimal(packet_interval_ms)).to_integral_value(rounding=ROUND_FLOOR))
    except (DecimalException, OverflowError, ValueError) as exc:
        raise ValueError("duration cannot be represented safely") from exc
    if count < 1:
        raise ValueError("duration must contain at least one packet opportunity")
    if count > sys.maxsize:
        raise ValueError("packet opportunity count exceeds supported range")
    return count


def finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must not be boolean")
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be numeric and finite") from exc
    if not math.isfinite(converted):
        raise ValueError(f"{label} must be finite")
    return converted


def parse_spec_json(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON numeric constant rejected: {value}")

    return json.loads(text, parse_constant=reject_constant)


def validate_spec(spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("S2 spec must be a JSON object")
    requirements = spec.get("requirements", spec)
    if not isinstance(requirements, dict):
        raise ValueError("S2 requirements must be an object")
    multicast = requirements.get("multicast")
    endpoints = requirements.get("endpoints")
    latency = requirements.get("latency")
    bandwidth = requirements.get("bandwidth")
    if not all(isinstance(section, dict) for section in (multicast, endpoints, latency, bandwidth)):
        raise ValueError("S2 spec lacks multicast/endpoints/latency/bandwidth objects")
    source = endpoints.get("source")
    receivers = endpoints.get("receivers")
    expected_source = {"domain": "A", "host": "h1"}
    expected_receivers = [{"domain": "B", "host": "h3"}, {"domain": "C", "host": "h4"}]
    if multicast.get("enabled") is not True:
        raise ValueError("multicast.enabled must be true")
    if multicast.get("group") not in {"G1", GROUP} or multicast.get("tree") != "SPT":
        raise ValueError("multicast group/tree is incompatible with frozen G1/SPT binding")
    if source != expected_source or receivers != expected_receivers:
        raise ValueError("logical endpoint binding is incompatible with frozen A:h1/B:h3/C:h4 topology")
    if str(latency.get("percentile", "")).upper() != "P99":
        raise ValueError("latency.percentile must be P99")
    try:
        latency_max_ms = finite_number(latency["max_ms"], "latency.max_ms")
        minimum_mbps = finite_number(bandwidth["min_mbps"], "bandwidth.min_mbps")
        maximum_mbps = finite_number(bandwidth["max_mbps"], "bandwidth.max_mbps")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("latency and bandwidth bounds must be numeric") from exc
    if latency_max_ms < 0 or minimum_mbps < 0 or maximum_mbps < minimum_mbps:
        raise ValueError("latency/bandwidth bounds are invalid")
    if "priority" in requirements:
        priority = requirements["priority"]
        if isinstance(priority, bool) or not isinstance(priority, (str, int, float)):
            raise ValueError("priority, when present, must be a non-boolean scalar")
        if isinstance(priority, (int, float)):
            finite_number(priority, "priority")
        else:
            try:
                numeric_priority = float(priority)
            except ValueError:
                numeric_priority = None
            if numeric_priority is not None and not math.isfinite(numeric_priority):
                raise ValueError("priority numeric string must be finite")
    return {
        "multicast": {
            "enabled": True,
            "group": multicast["group"],
            "tree": multicast["tree"],
        },
        "source": source,
        "receivers": receivers,
        "latency": {"percentile": "P99", "max_ms": latency_max_ms},
        "bandwidth": {"min_mbps": minimum_mbps, "max_mbps": maximum_mbps},
        "priority": requirements.get("priority"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Canonical operational S2 UDP multicast engine")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--repetition", type=positive_int, required=True)
    parser.add_argument("--results-root", required=True)
    parser.add_argument("--backend", choices=("mock", "real"), required=True)
    parser.add_argument("--mode", choices=("baseline", "adapt"), required=True)
    parser.add_argument("--duration", type=positive_float, required=True)
    parser.add_argument("--packet-interval-ms", type=positive_int, default=DEFAULT_PACKET_INTERVAL_MS)
    parser.add_argument("--recovery-bin-ms", type=positive_int, default=DEFAULT_RECOVERY_BIN_MS)
    parser.add_argument("--stable-k-bins", type=positive_int, default=DEFAULT_STABLE_K_BINS)
    return parser


def validate_cli_parameters(args: argparse.Namespace) -> None:
    if args.recovery_bin_ms < args.packet_interval_ms:
        raise ValueError("--recovery-bin-ms must be >= --packet-interval-ms")
    expected_packet_count(args.duration, args.packet_interval_ms)


def validate_execution_id(value: str) -> str:
    if not isinstance(value, str) or value in {"", ".", ".."} or not EXECUTION_ID_RE.fullmatch(value):
        raise ValueError("execution-id must be a safe single path component")
    return value


def resolve_output_directory(results_root: str | Path, execution_id: str) -> Path:
    identifier = validate_execution_id(execution_id)
    root = Path(results_root).expanduser().resolve()
    output = (root / identifier).resolve()
    if output.parent != root:
        raise ValueError("execution output must be a direct child of results-root")
    return output


def reserve_output_directory(results_root: str | Path, execution_id: str) -> Path:
    output = resolve_output_directory(results_root, execution_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)
    return output


def source_address_evidence(stdout: str) -> dict[str, Any]:
    """Authenticate the exact IPv4 source address from structured ``ip -j`` output."""
    try:
        document = parse_spec_json(stdout)
    except (TypeError, ValueError) as exc:
        return {"valid": False, "interface": None, "address": None,
                "reason": f"invalid_json:{type(exc).__name__}"}
    if not isinstance(document, list):
        return {"valid": False, "interface": None, "address": None,
                "reason": "root_not_array"}

    matches: list[tuple[str, dict[str, Any]]] = []
    for interface in document:
        if not isinstance(interface, dict):
            return {"valid": False, "interface": None, "address": None,
                    "reason": "interface_not_object"}
        ifname = interface.get("ifname")
        flags = interface.get("flags")
        addresses = interface.get("addr_info")
        if not isinstance(ifname, str) or not isinstance(flags, list) \
                or not all(isinstance(flag, str) for flag in flags) \
                or not isinstance(addresses, list):
            return {"valid": False, "interface": None, "address": None,
                    "reason": "interface_shape_invalid"}
        for address in addresses:
            if not isinstance(address, dict):
                return {"valid": False, "interface": None, "address": None,
                        "reason": "address_not_object"}
            if address.get("local") != SOURCE_IP:
                continue
            prefixlen = address.get("prefixlen")
            if (ifname == "lo" or "LOOPBACK" in flags or "UP" not in flags
                    or address.get("family") != "inet"
                    or address.get("scope") != "global"
                    or isinstance(prefixlen, bool) or not isinstance(prefixlen, int)
                    or not 1 <= prefixlen <= 32):
                return {"valid": False, "interface": None, "address": None,
                        "reason": "source_address_not_send_capable"}
            matches.append((ifname, address))
    if len(matches) != 1:
        return {"valid": False, "interface": None, "address": None,
                "reason": "source_address_missing" if not matches else "source_address_duplicated"}
    ifname, address = matches[0]
    return {
        "valid": True,
        "interface": ifname,
        "address": SOURCE_IP,
        "family": "inet",
        "prefixlen": address["prefixlen"],
        "scope": "global",
        "reason": None,
    }


def membership_has_group(stdout: str) -> bool:
    return bool(GROUP_MEMBERSHIP_RE.search(stdout))


def p4_functional_token_present(stdout: str) -> bool:
    return any(line.strip() == P4_FUNCTIONAL_TOKEN for line in stdout.splitlines())


def _process_liveness(processes: dict[str, Any]) -> dict[str, Any]:
    return {
        name: {"state": "running" if (returncode := process.poll()) is None else "exited",
               "returncode": returncode}
        for name, process in sorted(processes.items())
    }


def normalize_timeout_text(value: str | bytes | None) -> str:
    """Return timeout output as text; unexpected types fail closed with TypeError."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"timeout output must be str, bytes, or None, not {type(value).__name__}")


def evaluate_readbacks(
    *,
    source_address_json: str,
    receiver_membership: dict[str, str],
    probe_membership: str,
    backend: str,
    p4_stdout: str,
    process_liveness_before: dict[str, Any],
    process_liveness_after: dict[str, Any],
    readback_window_started_ns: int,
    readback_window_completed_ns: int,
    command_results: dict[str, Any],
) -> dict[str, Any]:
    receiver_checks = {
        endpoint: membership_has_group(receiver_membership.get(endpoint, ""))
        for endpoint in sorted(RECEIVERS)
    }
    source_evidence = source_address_evidence(source_address_json)
    checks = {
        "source_address_exact": source_evidence["valid"],
        "receiver_group_membership": receiver_checks,
        "all_receivers_have_exact_group": all(receiver_checks.values()),
        "probe_exact_group_absent": not membership_has_group(probe_membership),
        "p4_functional_token": backend == "mock" or p4_functional_token_present(p4_stdout),
        "readback_commands_succeeded": all(
            row.get("returncode") == 0 and row.get("timed_out") is False
            and row.get("error") is None for row in command_results.values()
        ),
        "readback_window_coherent": (
            isinstance(readback_window_started_ns, int)
            and not isinstance(readback_window_started_ns, bool)
            and isinstance(readback_window_completed_ns, int)
            and not isinstance(readback_window_completed_ns, bool)
            and readback_window_completed_ns >= readback_window_started_ns
        ),
        "processes_running_before": all(
            row.get("state") == "running" and row.get("returncode") is None
            for row in process_liveness_before.values()
        ),
        "processes_running_after": all(
            row.get("state") == "running" and row.get("returncode") is None
            for row in process_liveness_after.values()
        ),
    }
    expected_process_names = {"receiver:B:h3", "receiver:C:h4", "probe:h2"}
    checks["required_processes_observed"] = (
        set(process_liveness_before) == expected_process_names
        and set(process_liveness_after) == expected_process_names
    )
    collected_while_receivers_active = all([
        checks["required_processes_observed"], checks["processes_running_before"],
        checks["processes_running_after"], checks["readback_window_coherent"],
    ])
    valid = all([
        checks["source_address_exact"],
        checks["all_receivers_have_exact_group"],
        checks["probe_exact_group_absent"],
        checks["p4_functional_token"],
        checks["readback_commands_succeeded"],
        collected_while_receivers_active,
    ])
    return {
        "schema": "l2i-s2-readbacks-v3",
        "raw": {
            "source_address_json": source_address_json,
            "source_address": source_evidence,
            "receiver_membership": {key: receiver_membership.get(key, "") for key in sorted(RECEIVERS)},
            "probe_membership": probe_membership,
            "p4runtime_pre": p4_stdout if backend == "real" else "mock-control-plane-not-applied",
            "commands": command_results,
        },
        "checks": checks,
        "process_liveness_before": process_liveness_before,
        "process_liveness_after": process_liveness_after,
        "readback_window_started_ns": readback_window_started_ns,
        "readback_window_completed_ns": readback_window_completed_ns,
        "collected_while_receivers_active": bool(collected_while_receivers_active),
        "valid": valid,
    }


def collect_required_readbacks(
    *,
    processes: dict[str, Any],
    backend: str,
    p4_stdout: str,
    command_runner: Any = run,
    clock: Any = time.time_ns,
    timeout_s: float = READBACK_COMMAND_TIMEOUT_S,
) -> dict[str, Any]:
    started_ns = clock()
    before = _process_liveness(processes)
    commands = {
        "source_address": ["ip", "-n", SOURCE_NS, "-j", "address", "show"],
        "receiver:B:h3": ["ip", "-n", RECEIVERS["B:h3"][0], "maddr", "show"],
        "receiver:C:h4": ["ip", "-n", RECEIVERS["C:h4"][0], "maddr", "show"],
        "probe:h2": ["ip", "-n", PROBE_NS, "maddr", "show"],
    }
    results: dict[str, Any] = {}
    for name, argv in commands.items():
        try:
            completed = command_runner(argv, check=False, timeout=timeout_s)
            results[name] = {
                "argv": argv, "returncode": completed.returncode,
                "stdout": completed.stdout or "", "stderr": completed.stderr or "",
                "timed_out": False, "error": None,
            }
        except subprocess.TimeoutExpired as exc:
            results[name] = {
                "argv": argv, "returncode": None,
                "stdout": normalize_timeout_text(exc.stdout),
                "stderr": normalize_timeout_text(exc.stderr),
                "timed_out": True, "error": f"TimeoutExpired: {exc}",
            }
        except BaseException as exc:
            results[name] = {
                "argv": argv, "returncode": None, "stdout": "", "stderr": "",
                "timed_out": False, "error": f"{type(exc).__name__}: {exc}",
            }
    after = _process_liveness(processes)
    completed_ns = clock()
    return evaluate_readbacks(
        source_address_json=results["source_address"]["stdout"],
        receiver_membership={
            "B:h3": results["receiver:B:h3"]["stdout"],
            "C:h4": results["receiver:C:h4"]["stdout"],
        },
        probe_membership=results["probe:h2"]["stdout"],
        backend=backend,
        p4_stdout=p4_stdout,
        process_liveness_before=before,
        process_liveness_after=after,
        readback_window_started_ns=started_ns,
        readback_window_completed_ns=completed_ns,
        command_results=results,
    )


def wait_for_required_readbacks(
    *,
    processes: dict[str, Any],
    backend: str,
    p4_stdout: str,
    command_runner: Any = run,
    wall_clock: Any = time.time_ns,
    monotonic_clock: Any = time.monotonic,
    sleeper: Any = time.sleep,
    deadline_s: float = READINESS_TIMEOUT_S,
    poll_interval_s: float = READINESS_POLL_INTERVAL_S,
    command_timeout_s: float = READBACK_COMMAND_TIMEOUT_S,
) -> dict[str, Any]:
    """Poll bounded readiness and retain a summary of every observed attempt."""
    deadline_value = finite_number(deadline_s, "readiness deadline")
    interval_value = finite_number(poll_interval_s, "readiness poll interval")
    if deadline_value <= 0 or interval_value <= 0 or interval_value > deadline_value:
        raise ValueError("readiness timing bounds invalid")
    started = monotonic_clock()
    deadline = started + deadline_value
    attempts: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None
    while True:
        attempt_started = monotonic_clock()
        result = collect_required_readbacks(
            processes=processes,
            backend=backend,
            p4_stdout=p4_stdout,
            command_runner=command_runner,
            clock=wall_clock,
            timeout_s=command_timeout_s,
        )
        attempt_completed = monotonic_clock()
        attempts.append({
            "attempt": len(attempts) + 1,
            "started_monotonic": attempt_started,
            "completed_monotonic": attempt_completed,
            "valid": result["valid"],
            "checks": result["checks"],
        })
        if result["valid"]:
            result["readiness"] = {
                "clock": "monotonic",
                "deadline_seconds": deadline_value,
                "poll_interval_seconds": interval_value,
                "attempt_count": len(attempts),
                "attempts": attempts,
                "ready": True,
                "timed_out": False,
            }
            return result
        if attempt_completed >= deadline:
            break
        sleeper(min(interval_value, deadline - attempt_completed))
    assert result is not None
    result["readiness"] = {
        "clock": "monotonic",
        "deadline_seconds": deadline_value,
        "poll_interval_seconds": interval_value,
        "attempt_count": len(attempts),
        "attempts": attempts,
        "ready": False,
        "timed_out": True,
    }
    result["valid"] = False
    return result


def cleanup_processes(processes: dict[str, Any], timeout_s: float = 1.0) -> dict[str, Any]:
    results: dict[str, Any] = {}
    overall_valid = True
    for name, process in processes.items():
        terminated = killed = False
        stdout = stderr = ""
        error = None
        try:
            if process.poll() is None:
                process.terminate()
                terminated = True
            try:
                stdout, stderr = process.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                process.kill()
                killed = True
                stdout, stderr = process.communicate(timeout=timeout_s)
            returncode = process.returncode
            valid = returncode is not None
        except BaseException as exc:
            returncode = getattr(process, "returncode", None)
            valid = False
            error = f"{type(exc).__name__}: {exc}"
        overall_valid = overall_valid and valid
        results[name] = {
            "terminated": terminated,
            "killed": killed,
            "returncode": returncode,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "cleanup_error": error,
            "valid": valid,
        }
    return {"valid": overall_valid, "processes": results}


def new_p4_apply_state() -> dict[str, Any]:
    return {"attempted": False, "completed": False, "returncode_or_error": None}


def program_p4(
    state: dict[str, Any], *, command_runner: Any = run, timeout_s: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    state["attempted"] = True
    argv = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "scripts/p4_program_s2.py"),
        "--addr", "127.0.0.1:9559", "--ports", "1", "2",
    ]
    try:
        completed = command_runner(argv, check=False, timeout=timeout_s)
    except BaseException as exc:
        state["completed"] = False
        state["returncode_or_error"] = f"{type(exc).__name__}: {exc}"
        raise
    state["completed"] = True
    state["returncode_or_error"] = completed.returncode
    if completed.returncode != 0:
        raise RuntimeError(f"P4_APPLY_FAILED: returncode={completed.returncode}: {completed.stderr}")
    return completed


def finalize_runtime(
    output: Path,
    *,
    backend: str,
    p4_apply_state: dict[str, Any],
    processes: dict[str, Any],
) -> dict[str, Any]:
    process_cleanup = cleanup_processes(processes)
    p4_cleanup_doc: dict[str, Any]
    cleanup_required = backend == "real" and p4_apply_state.get("attempted") is True
    if cleanup_required:
        try:
            cleanup = run([
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "scripts/p4_program_s2.py"),
                "--addr", "127.0.0.1:9559", "--ports", "1", "2", "--cleanup",
            ], check=False)
            p4_cleanup_doc = {
                "attempted": True, "returncode": cleanup.returncode,
                "stdout": cleanup.stdout, "stderr": cleanup.stderr,
                "valid": cleanup.returncode == 0,
            }
        except BaseException as exc:
            p4_cleanup_doc = {
                "attempted": True, "returncode": None, "stdout": "", "stderr": "",
                "cleanup_error": f"{type(exc).__name__}: {exc}", "valid": False,
            }
    else:
        p4_cleanup_doc = {"attempted": False, "valid": True, "reason": "no-real-control-state"}
    rollback = {
        "schema": "l2i-s2-rollback-v3",
        "backend": backend,
        "p4_apply_attempted": p4_apply_state.get("attempted") is True,
        "p4_apply_completed": p4_apply_state.get("completed") is True,
        "p4_apply_returncode_or_error": p4_apply_state.get("returncode_or_error"),
        "p4_cleanup_required": cleanup_required,
        "process_cleanup": process_cleanup,
        "p4_cleanup": p4_cleanup_doc,
        "valid": process_cleanup["valid"] and p4_cleanup_doc["valid"],
    }
    dump(output / "rollback.json", rollback)
    return rollback


FORBIDDEN_CLAIM_KEYS = frozenset((
    "full_" + "conformance", "intent_" + "conformance", "bandwidth_" + "pass",
))


def _read_terminal_json(path: Path) -> dict[str, Any]:
    value = parse_spec_json(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _read_terminal_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"{path.name}:{number}: blank line")
        value = parse_spec_json(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}:{number}: JSON object required")
        rows.append(value)
    if not rows:
        raise ValueError(f"{path.name}: at least one record required")
    return rows


def _contains_forbidden_claim(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(FORBIDDEN_CLAIM_KEYS.intersection(value)) or any(
            _contains_forbidden_claim(child) for child in value.values()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_claim(child) for child in value)
    return False


def _terminal_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def validate_terminal_artifacts(output: Path, execution_id: str) -> dict[str, Any]:
    """Validate the complete terminal evidence set without rewriting any artifact."""
    expected_files = (
        "raw-events.jsonl", "membership-events.jsonl", "readbacks.json",
        "summary.json", "recovery-observation.json", "rollback.json",
    )
    missing = [name for name in expected_files if not (output / name).is_file()]
    if missing:
        raise ValueError(f"missing terminal artifacts: {missing}")
    raw = _read_terminal_jsonl(output / "raw-events.jsonl")
    membership = _read_terminal_jsonl(output / "membership-events.jsonl")
    readbacks = _read_terminal_json(output / "readbacks.json")
    summary = _read_terminal_json(output / "summary.json")
    observation = _read_terminal_json(output / "recovery-observation.json")
    rollback = _read_terminal_json(output / "rollback.json")
    if any(_contains_forbidden_claim(document) for document in (
            raw, membership, readbacks, summary, observation, rollback)):
        raise ValueError("prohibited scientific/conformance claim key present")

    sends: dict[int, dict[str, Any]] = {}
    received: list[dict[str, Any]] = []
    for index, row in enumerate(raw):
        if row.get("schema") != "phase4sc-operational-raw-v3" or row.get("scenario") != "S2":
            raise ValueError(f"raw[{index}] schema/scenario invalid")
        sequence = _terminal_int(row.get("sequence"), f"raw[{index}].sequence", minimum=1)
        sent_ns = _terminal_int(row.get("sent_ns"), f"raw[{index}].sent_ns")
        event = row.get("event")
        if event == "sent":
            if row.get("endpoint") != SOURCE or row.get("status") != "sent" or sequence in sends:
                raise ValueError(f"raw[{index}] sent semantics invalid")
            sends[sequence] = row
        elif event == "received":
            if row.get("endpoint") not in RECEIVERS or row.get("status") != "received":
                raise ValueError(f"raw[{index}] receive endpoint/status invalid")
            received_ns = _terminal_int(row.get("received_ns"), f"raw[{index}].received_ns")
            if received_ns < sent_ns:
                raise ValueError(f"raw[{index}] negative latency")
            received.append(row)
        else:
            raise ValueError(f"raw[{index}] event invalid")
        if (not isinstance(row.get("payload_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", row["payload_sha256"]) is None):
            raise ValueError(f"raw[{index}] payload digest invalid")
        _terminal_int(row.get("bytes"), f"raw[{index}].bytes", minimum=1)
    if not sends:
        raise ValueError("raw send set empty")
    for index, row in enumerate(received):
        sequence = row["sequence"]
        if sequence not in sends or row["sent_ns"] != sends[sequence]["sent_ns"]:
            raise ValueError(f"received[{index}] lacks matching send")

    stops = set()
    for index, event in enumerate(membership):
        if event.get("endpoint") not in RECEIVERS or not isinstance(event.get("event"), str):
            raise ValueError(f"membership[{index}] endpoint/event invalid")
        _terminal_int(event.get("observed_ns"), f"membership[{index}].observed_ns")
        if event["event"] not in {"membership_join", "membership_leave", "membership_rejoin", "receiver_stop"}:
            raise ValueError(f"membership[{index}] event invalid")
        if event["event"] == "receiver_stop":
            stops.add(event["endpoint"])
    if stops != set(RECEIVERS):
        raise ValueError("membership receiver_stop coverage invalid")

    expected_processes = {"receiver:B:h3", "receiver:C:h4", "probe:h2"}
    required_checks = {
        "source_address_exact", "all_receivers_have_exact_group",
        "probe_exact_group_absent", "p4_functional_token", "readback_commands_succeeded",
        "readback_window_coherent", "processes_running_before", "processes_running_after",
        "required_processes_observed",
    }
    if readbacks.get("schema") != "l2i-s2-readbacks-v3" or readbacks.get("valid") is not True:
        raise ValueError("readbacks schema/valid invalid")
    if readbacks.get("collected_while_receivers_active") is not True:
        raise ValueError("readbacks not bounded by active processes")
    checks = readbacks.get("checks")
    if not isinstance(checks, dict) or not all(checks.get(name) is True for name in required_checks):
        raise ValueError("readback required checks invalid")
    for phase in ("process_liveness_before", "process_liveness_after"):
        state = readbacks.get(phase)
        if not isinstance(state, dict) or set(state) != expected_processes:
            raise ValueError(f"{phase} coverage invalid")
        if any(row != {"state": "running", "returncode": None} for row in state.values()):
            raise ValueError(f"{phase} state invalid")
    started = _terminal_int(readbacks.get("readback_window_started_ns"), "readback start")
    completed = _terminal_int(readbacks.get("readback_window_completed_ns"), "readback completion")
    if completed < started:
        raise ValueError("readback time window inverted")
    readiness = readbacks.get("readiness")
    if (not isinstance(readiness, dict) or readiness.get("clock") != "monotonic"
            or readiness.get("ready") is not True or readiness.get("timed_out") is not False):
        raise ValueError("readback readiness proof invalid")
    attempts = readiness.get("attempts")
    attempt_count = readiness.get("attempt_count")
    deadline_seconds = readiness.get("deadline_seconds")
    poll_interval_seconds = readiness.get("poll_interval_seconds")
    if (isinstance(attempt_count, bool) or not isinstance(attempt_count, int)
            or attempt_count < 1 or not isinstance(attempts, list)
            or len(attempts) != attempt_count
            or isinstance(deadline_seconds, bool)
            or not isinstance(deadline_seconds, (int, float))
            or not math.isfinite(deadline_seconds) or deadline_seconds <= 0
            or isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0
            or poll_interval_seconds > deadline_seconds):
        raise ValueError("readback readiness attempts invalid")
    for number, attempt in enumerate(attempts, start=1):
        if (not isinstance(attempt, dict) or attempt.get("attempt") != number
                or isinstance(attempt.get("started_monotonic"), bool)
                or not isinstance(attempt.get("started_monotonic"), (int, float))
                or isinstance(attempt.get("completed_monotonic"), bool)
                or not isinstance(attempt.get("completed_monotonic"), (int, float))
                or not math.isfinite(attempt["started_monotonic"])
                or not math.isfinite(attempt["completed_monotonic"])
                or attempt["completed_monotonic"] < attempt["started_monotonic"]
                or not isinstance(attempt.get("checks"), dict)
                or not isinstance(attempt.get("valid"), bool)):
            raise ValueError("readback readiness attempt malformed")
    if attempts[-1].get("valid") is not True:
        raise ValueError("readback readiness terminal attempt invalid")
    if attempts[-1].get("checks") != checks:
        raise ValueError("readback readiness terminal checks mismatch")
    commands = readbacks.get("raw", {}).get("commands")
    expected_commands = {"source_address", "receiver:B:h3", "receiver:C:h4", "probe:h2"}
    if not isinstance(commands, dict) or set(commands) != expected_commands:
        raise ValueError("readback command evidence incomplete")
    for name, row in commands.items():
        if (not isinstance(row, dict) or row.get("returncode") != 0
                or row.get("timed_out") is not False or row.get("error") is not None
                or not isinstance(row.get("argv"), list)
                or not isinstance(row.get("stdout"), str) or not isinstance(row.get("stderr"), str)):
            raise ValueError(f"readback command {name} invalid")
    raw_readbacks = readbacks.get("raw")
    if not isinstance(raw_readbacks, dict):
        raise ValueError("readback raw evidence absent")
    if (raw_readbacks.get("source_address_json") != commands["source_address"]["stdout"]
            or raw_readbacks.get("receiver_membership") != {
                "B:h3": commands["receiver:B:h3"]["stdout"],
                "C:h4": commands["receiver:C:h4"]["stdout"],
            }
            or raw_readbacks.get("probe_membership") != commands["probe:h2"]["stdout"]):
        raise ValueError("readback raw/command evidence mismatch")
    exact_receiver_checks = {
        endpoint: membership_has_group(raw_readbacks["receiver_membership"][endpoint])
        for endpoint in sorted(RECEIVERS)
    }
    source_evidence = source_address_evidence(raw_readbacks["source_address_json"])
    if raw_readbacks.get("source_address") != source_evidence:
        raise ValueError("source address structural evidence mismatch")
    recomputed_checks = {
        "source_address_exact": source_evidence["valid"],
        "receiver_group_membership": exact_receiver_checks,
        "all_receivers_have_exact_group": all(exact_receiver_checks.values()),
        "probe_exact_group_absent": not membership_has_group(raw_readbacks["probe_membership"]),
        "p4_functional_token": (
            summary.get("backend") == "mock"
            or p4_functional_token_present(str(raw_readbacks.get("p4runtime_pre", "")))
        ),
    }
    if any(checks.get(name) != value for name, value in recomputed_checks.items()):
        raise ValueError("readback exact checks disagree with raw evidence")

    statuses = {"PASS", "FAIL", "NOT_EVALUABLE"}
    if (observation.get("schema") != SCHEMA_ID or observation.get("predicate") != PREDICATE_ID
            or observation.get("observation_status") not in statuses
            or observation.get("aggregate", {}).get("status") != observation.get("observation_status")):
        raise ValueError("recovery observation identity/status invalid")
    parameters = observation.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("recovery parameters absent")
    latency_limit = finite_number(parameters.get("latency_max_ms"), "latency_max_ms")
    if latency_limit < 0:
        raise ValueError("latency_max_ms negative")
    _terminal_int(parameters.get("recovery_bin_ms"), "recovery_bin_ms", minimum=1)
    _terminal_int(parameters.get("stable_k_bins"), "stable_k_bins", minimum=1)
    receiver_observations = observation.get("receivers")
    if not isinstance(receiver_observations, dict) or set(receiver_observations) != set(RECEIVERS):
        raise ValueError("recovery receiver coverage invalid")
    if any(row.get("status") not in statuses for row in receiver_observations.values()):
        raise ValueError("recovery receiver status invalid")

    if (summary.get("scenario") != "S2" or summary.get("execution_id") != execution_id
            or summary.get("backend") not in {"mock", "real"}
            or summary.get("mode") not in {"baseline", "adapt"}
            or summary.get("readbacks_valid") is not True
            or summary.get("raw_recomputable") is not True
            or summary.get("synthetic_metrics") is not False):
        raise ValueError("summary identity/control semantics invalid")
    metrics = summary.get("metrics")
    workload = summary.get("workload_parameters")
    if not isinstance(metrics, dict) or not isinstance(workload, dict):
        raise ValueError("summary metric/workload objects absent")
    if (metrics.get("sent") != len(sends) or metrics.get("delivered") != len(received)
            or workload.get("expected_packet_count") != len(sends)):
        raise ValueError("summary/raw cardinality mismatch")
    observation_reference = summary.get("recovery_observation", {})
    raw_reference = summary.get("raw_artifacts", {}).get("recovery_observation")
    if (observation_reference.get("schema") != SCHEMA_ID
            or observation_reference.get("observation_status") != observation["observation_status"]
            or Path(str(observation_reference.get("artifact", ""))).resolve() != (output / "recovery-observation.json").resolve()
            or Path(str(raw_reference or "")).resolve() != (output / "recovery-observation.json").resolve()):
        raise ValueError("summary/recovery observation linkage invalid")

    backend = summary["backend"]
    if (rollback.get("schema") != "l2i-s2-rollback-v3" or rollback.get("backend") != backend
            or rollback.get("valid") is not True
            or rollback.get("process_cleanup", {}).get("valid") is not True
            or rollback.get("p4_cleanup", {}).get("valid") is not True):
        raise ValueError("rollback identity/validity invalid")
    if backend == "real":
        if (rollback.get("p4_apply_attempted") is not True
                or rollback.get("p4_apply_completed") is not True
                or rollback.get("p4_apply_returncode_or_error") != 0
                or rollback.get("p4_cleanup_required") is not True
                or rollback["p4_cleanup"].get("attempted") is not True):
            raise ValueError("real-backend apply/rollback lifecycle invalid")
    elif (rollback.get("p4_apply_attempted") is not False
          or rollback.get("p4_apply_completed") is not False
          or rollback.get("p4_apply_returncode_or_error") is not None
          or rollback.get("p4_cleanup_required") is not False
          or rollback["p4_cleanup"].get("attempted") is not False):
        raise ValueError("mock-backend apply/rollback lifecycle invalid")
    return {
        "valid": True, "execution_id": execution_id, "raw_events": len(raw),
        "sent": len(sends), "received": len(received), "membership_events": len(membership),
    }


def emit_success(output: Path, execution_id: str, *, printer: Any = print) -> None:
    validation = validate_terminal_artifacts(output, execution_id)
    printer(json.dumps({
        "valid": True, "summary": str(output / "summary.json"),
        "terminal_validation": validation,
    }, sort_keys=True))


def finalize_or_raise(
    output: Path, *, backend: str, p4_apply_state: dict[str, Any],
    processes: dict[str, Any], primary_error: BaseException | None,
) -> dict[str, Any]:
    try:
        rollback = finalize_runtime(
            output, backend=backend, p4_apply_state=p4_apply_state, processes=processes,
        )
    except BaseException as cleanup_error:
        if primary_error is not None:
            if hasattr(primary_error, "add_note"):
                primary_error.add_note(f"secondary cleanup exception: {cleanup_error}")
            raise primary_error from cleanup_error
        raise
    if primary_error is not None:
        if not rollback["valid"] and hasattr(primary_error, "add_note"):
            primary_error.add_note("secondary runtime cleanup failure recorded in rollback.json")
        raise primary_error
    if not rollback["valid"]:
        raise RuntimeError("S2_ROLLBACK_INVALID")
    return rollback


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        validate_cli_parameters(args)
        resolve_output_directory(args.results_root, args.execution_id)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        spec = parse_spec_json(Path(args.spec).read_text(encoding="utf-8"))
        intent_requirements = validate_spec(spec)
        count = expected_packet_count(args.duration, args.packet_interval_ms)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"S2_SPEC_CONTRACT_INVALID: {exc}") from exc
    try:
        output = reserve_output_directory(args.results_root, args.execution_id)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"S2_OUTPUT_CONTRACT_INVALID: {exc}") from exc
    interval = args.packet_interval_ms / 1000.0
    p4_apply: subprocess.CompletedProcess[str] | None = None
    p4_apply_state = new_p4_apply_state()
    processes: dict[str, Any] = {}
    primary_error: BaseException | None = None
    try:
        if args.backend == "real":
            p4_apply = program_p4(p4_apply_state)
        receiver_processes = {}
        child_duration = args.duration + READINESS_TIMEOUT_S + CHILD_LIFETIME_MARGIN_S
        for endpoint, (namespace, address) in RECEIVERS.items():
            process = subprocess.Popen(
                ["ip", "netns", "exec", namespace, sys.executable, "-c", RECEIVER_CODE,
                 GROUP, str(PORT), address, endpoint, args.mode, str(child_duration)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            receiver_processes[endpoint] = process
            processes[f"receiver:{endpoint}"] = process
        probe = subprocess.Popen(
            ["ip", "netns", "exec", PROBE_NS, sys.executable, "-c", PROBE_CODE,
             str(PORT), str(child_duration)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        processes["probe:h2"] = probe
        readbacks = wait_for_required_readbacks(
            processes=processes, backend=args.backend,
            p4_stdout=p4_apply.stdout if p4_apply else "",
        )
        dump(output / "readbacks.json", readbacks)
        if not readbacks["valid"]:
            raise RuntimeError("S2_REQUIRED_READBACK_INVALID")
        sender = run(["ip", "netns", "exec", SOURCE_NS, sys.executable, "-c", SENDER_CODE,
                      GROUP, str(PORT), SOURCE_IP, str(count), str(interval)])
        sender_doc = json.loads(sender.stdout); receiver_docs = {}
        for endpoint, process in receiver_processes.items():
            stdout, stderr = process.communicate(timeout=args.duration + 4)
            if process.returncode or not stdout.strip(): raise RuntimeError(f"receiver {endpoint} failed: {stderr}")
            receiver_docs[endpoint] = json.loads(stdout)
        probe_out, probe_err = probe.communicate(timeout=args.duration + 4)
        if probe.returncode: raise RuntimeError(f"probe failed: {probe_err}")
        probe_doc = json.loads(probe_out)
        sent = {int(p["sequence"]): p for p in sender_doc["packets"]}; rows = []
        for sequence, packet in sorted(sent.items()):
            rows.append(raw_event(sequence, "sent", SOURCE, packet["payload"], packet["sent_ns"], 0, packet["bytes"]))
        for endpoint, document in receiver_docs.items():
            for packet in document["packets"]:
                rows.append(raw_event(int(packet["sequence"]), "received", endpoint, packet["payload"],
                                      int(packet["sent_ns"]), int(packet["received_ns"]), int(packet["bytes"])))
        raw_path = output / "raw-events.jsonl"
        raw_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows))
        event_path = output / "membership-events.jsonl"
        membership = [event for doc in receiver_docs.values() for event in doc["events"]]
        event_path.write_text("".join(json.dumps(event, sort_keys=True) + "\n" for event in membership))
        delivered = [row for row in rows if row["event"] == "received"]
        denominator = len(sent) * len(RECEIVERS); latencies = [(row["received_ns"] - row["sent_ns"]) / 1e6 for row in delivered]
        recovery = []
        for endpoint, document in receiver_docs.items():
            joins = [e["observed_ns"] for e in document["events"] if e["event"] in {"membership_join", "membership_rejoin"}]
            received = [p["received_ns"] for p in document["packets"]]
            for joined in joins:
                later = [stamp for stamp in received if stamp >= joined]
                if later: recovery.append((min(later) - joined) / 1e6)
        dump(output / "sender.json", sender_doc); dump(output / "receivers.json", receiver_docs); dump(output / "probe.json", probe_doc)
        observation = build_recovery_observation(
            rows,
            membership,
            receivers=sorted(RECEIVERS),
            latency_max_ms=intent_requirements["latency"]["max_ms"],
            recovery_bin_ms=args.recovery_bin_ms,
            stable_k_bins=args.stable_k_bins,
            readbacks_valid=readbacks["valid"],
            probe_packets=probe_doc["packets"],
        )
        observation_path = output / "recovery-observation.json"
        dump(observation_path, observation)
        summary = {
            "scenario": "S2", "execution_id": args.execution_id, "mode": args.mode, "backend": args.backend,
            "repetition": args.repetition,
            "source_oriented_multicast": True, "source": SOURCE, "group": GROUP,
            "receivers": sorted(RECEIVERS), "probe_membership": False, "probe_packets": len(probe_doc["packets"]),
            "raw_artifacts": {"events": str(raw_path), "membership_events": str(event_path),
                              "recovery_observation": str(observation_path)},
            "metrics": {"sent": len(sent), "delivered": len(delivered), "lost": denominator - len(delivered),
                        "delivery_ratio": len(delivered) / denominator, "loss_ratio": (denominator - len(delivered)) / denominator,
                        "recovery_ms": max(recovery) if recovery else None,
                        "stability": int(bool(delivered) and not probe_doc["packets"]),
                        "latency_p50_ms": percentile_type7(latencies, .50) if latencies else None,
                        "latency_p95_ms": percentile_type7(latencies, .95) if latencies else None,
                        "latency_p99_ms": percentile_type7(latencies, .99) if latencies else None},
            "legacy_metric_semantics": {
                "metrics.recovery_ms": "legacy first-reception aggregate; not K-bin stability",
                "metrics.stability": "legacy coarse delivery/probe flag; not K-bin stability or conformance",
            },
            "intent_requirements": intent_requirements,
            "resolved_bindings": {"source": SOURCE, "receivers": sorted(RECEIVERS), "group_symbol": "G1", "group_address": GROUP},
            "workload_parameters": {"duration_s": args.duration, "packet_interval_ms": args.packet_interval_ms,
                                    "expected_packet_count": count},
            "environment_parameters": {"backend": args.backend, "mode": args.mode, "repetition": args.repetition},
            "evaluable_predicates": [PREDICATE_ID],
            "non_evaluable_predicates": list(EXCLUDED_REQUIREMENTS),
            "recovery_observation": {"artifact": str(observation_path), "schema": observation["schema"],
                                     "observation_status": observation["observation_status"]},
            "readbacks_valid": readbacks["valid"],
            "raw_recomputable": True, "synthetic_metrics": False,
        }
        dump(output / "summary.json", summary)
    except BaseException as exc:
        primary_error = exc
    finalize_or_raise(
        output, backend=args.backend, p4_apply_state=p4_apply_state,
        processes=processes, primary_error=primary_error,
    )
    emit_success(output, args.execution_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
