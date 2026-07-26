#!/usr/bin/env python3
"""Canonical S1: contention-aware unicast QoS across control domains.

This module is the only canonical S1 entrypoint.  It preserves three different
scientific objects instead of merging them into one timing or one success flag:

* the immutable experiment identity and configuration;
* control-plane materialization and readback evidence;
* data-plane observations collected during simultaneous contention.

The measured forwarding path is a Linux bridge/veth testbed.  NETCONF and
P4Runtime are exercised as heterogeneous control-domain materializations, but
their targets are not forwarding this S1 traffic.  Consequently, the scenario
records their apply/readback evidence without attributing throughput or RTT
changes to either target.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from concurrent.futures import ThreadPoolExecutor
import inspect
import json
import math
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET

from l2i.backends.router import get_backends
from l2i.experiment_contract import (
    ExperimentContractError,
    ExperimentIdentity,
    ExperimentRunDirectory,
    RepositoryProvenance,
    atomic_write_json,
    atomic_write_text,
    build_run_manifest,
    generate_execution_id,
    sha256_file,
    utc_now,
    utc_rfc3339,
)


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ID = "S1"
DEFAULT_PROFILE_ID = "s1-canonical-v1"

# The two offered loads originate in different namespaces and converge on the
# same destination.  Both therefore traverse the same B->C egress interface.
SENSITIVE_SOURCE_NS = "h1"
BEST_EFFORT_SOURCE_NS = "h2"
DESTINATION_NS = "h3"
SENSITIVE_SOURCE_IP = "10.0.0.1"
BEST_EFFORT_SOURCE_IP = "10.0.0.2"
DESTINATION_IP = "10.0.0.3"
SENSITIVE_PORT = 5201
BEST_EFFORT_PORT = 5202
BOTTLENECK_INTERFACE = "s1-bc-b"

# Each capacity is materialized on the egress that leads toward the next
# segment.  The B segment must be the unique bottleneck in canonical S1 runs.
ENVIRONMENT_LINKS = {
    "A": "s1-ab-a",
    "B": BOTTLENECK_INTERFACE,
    "C": "h3-eth0-br",
}


class S1ExecutionError(RuntimeError):
    """Raised when one canonical S1 gate fails closed."""


@dataclass(frozen=True)
class S1Intent:
    """Normalized subset of L2i used by the canonical S1 experiment."""

    flow_id: str
    latency_percentile: str
    latency_max_ms: float
    bandwidth_min_mbps: float
    bandwidth_max_mbps: Optional[float]
    priority: str

    def to_backend_intent(self) -> Dict[str, Any]:
        """Return the technology-neutral intent passed to B and C backends."""

        payload: Dict[str, Any] = {
            "class": "prio10",
            "min_mbps": self.bandwidth_min_mbps,
            "priority": self.priority,
        }
        # Absence is semantically meaningful: no intent-level maximum was
        # declared.  Do not silently convert an absent bound into zero.
        if self.bandwidth_max_mbps is not None:
            payload["max_mbps"] = self.bandwidth_max_mbps
        return payload


@dataclass(frozen=True)
class TimedCommand:
    """Observed result and time window for one data-plane command."""

    name: str
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str
    started_offset_s: float
    ended_offset_s: float
    elapsed_s: float

    def evidence(self) -> Dict[str, Any]:
        """Return metadata without duplicating potentially large stdout."""

        return {
            "name": self.name,
            "argv": self.argv,
            "returncode": self.returncode,
            "started_offset_s": self.started_offset_s,
            "ended_offset_s": self.ended_offset_s,
            "elapsed_s": self.elapsed_s,
            "stdout_bytes": len(self.stdout.encode("utf-8")),
            "stderr_bytes": len(self.stderr.encode("utf-8")),
        }


def _mask_secret(
    value: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return a shallow copy with common credential fields redacted."""

    if value is None:
        return None
    masked = dict(value)
    for key in ("password", "pass", "secret"):
        if key in masked and masked[key] is not None:
            masked[key] = "***"
    return masked


def _positive_float(value: Any, label: str) -> float:
    """Parse a strictly positive finite number."""

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise S1ExecutionError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise S1ExecutionError(f"{label} must be greater than zero")
    return number


def _nonnegative_float(value: Any, label: str) -> float:
    """Parse a finite number that may be zero."""

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise S1ExecutionError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise S1ExecutionError(f"{label} must not be negative")
    return number


def _load_specification(path: Path) -> Tuple[Dict[str, Any], S1Intent]:
    """Load and validate the canonical S1 syntax without legacy fallback."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise S1ExecutionError(f"specification does not exist: {resolved}")
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise S1ExecutionError(f"invalid JSON specification: {resolved}") from exc
    if not isinstance(document, dict):
        raise S1ExecutionError("the S1 specification must be a JSON object")

    if document.get("l2i_version") != "0.1":
        raise S1ExecutionError("canonical S1 requires l2i_version='0.1'")
    for required in ("tenant", "scope", "flow", "requirements"):
        if required not in document:
            raise S1ExecutionError(
                f"canonical S1 specification is missing {required!r}"
            )

    flow = document.get("flow")
    if not isinstance(flow, dict) or not str(flow.get("id", "")).strip():
        raise S1ExecutionError("canonical S1 requires flow.id")
    if "flow_id" in document:
        raise S1ExecutionError(
            "legacy flow_id is not allowed when canonical flow.id is used"
        )

    requirements = document.get("requirements")
    if not isinstance(requirements, dict):
        raise S1ExecutionError("requirements must be an object")
    latency = requirements.get("latency")
    bandwidth = requirements.get("bandwidth")
    priority = requirements.get("priority")
    multicast = requirements.get("multicast", {"enabled": False})
    if not isinstance(latency, dict):
        raise S1ExecutionError("S1 requires requirements.latency")
    if not isinstance(bandwidth, dict):
        raise S1ExecutionError("S1 requires requirements.bandwidth")
    if not isinstance(priority, dict):
        raise S1ExecutionError("S1 requires requirements.priority")
    if not isinstance(multicast, dict) or multicast.get("enabled") is not False:
        raise S1ExecutionError("S1 requires multicast.enabled=false")

    percentile = str(latency.get("percentile", "")).upper()
    if percentile not in {"P50", "P95", "P99"}:
        raise S1ExecutionError(
            "latency.percentile must be P50, P95, or P99"
        )
    latency_max_ms = _positive_float(
        latency.get("max_ms"),
        "requirements.latency.max_ms",
    )
    minimum = _positive_float(
        bandwidth.get("min_mbps"),
        "requirements.bandwidth.min_mbps",
    )
    maximum_raw = bandwidth.get("max_mbps")
    maximum = (
        _positive_float(
            maximum_raw,
            "requirements.bandwidth.max_mbps",
        )
        if maximum_raw is not None
        else None
    )
    if maximum is not None and minimum > maximum:
        raise S1ExecutionError(
            "requirements.bandwidth.min_mbps must not exceed max_mbps"
        )

    priority_level = str(priority.get("level", "")).lower()
    if priority_level not in {"critical", "high", "medium", "low"}:
        raise S1ExecutionError(
            "requirements.priority.level is invalid"
        )

    return document, S1Intent(
        flow_id=str(flow["id"]).strip(),
        latency_percentile=percentile,
        latency_max_ms=latency_max_ms,
        bandwidth_min_mbps=minimum,
        bandwidth_max_mbps=maximum,
        priority=priority_level,
    )


def _percentile(values: Sequence[float], quantile: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sequence."""

    if not values:
        raise S1ExecutionError("cannot compute a percentile from no samples")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _percentile_fraction(label: str) -> float:
    """Map one validated L2i percentile label to a numeric fraction."""

    return {"P50": 0.50, "P95": 0.95, "P99": 0.99}[label]


def _namespace_command(namespace: str, argv: Sequence[str]) -> List[str]:
    """Qualify one command for execution inside a network namespace."""

    # Parsing must not depend on the host locale.  iperf3 JSON and iputils
    # packet summaries are therefore generated under the C locale.
    return [
        "ip",
        "netns",
        "exec",
        namespace,
        "env",
        "LC_ALL=C",
        *map(str, argv),
    ]


def _run_capture(
    argv: Sequence[str],
    *,
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    """Run one command without a shell and preserve both output streams."""

    try:
        return subprocess.run(
            list(map(str, argv)),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=list(map(str, argv)),
            returncode=124,
            stdout=str(exc.stdout or ""),
            stderr=str(exc.stderr or "") + "\ncommand timed out",
        )


def _parse_ping_counts(output: str) -> Tuple[int, int]:
    """Extract transmitted and received packet counts from iputils ping."""

    match = re.search(
        r"(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets )?received",
        output,
    )
    if not match:
        raise S1ExecutionError("ping output has no packet-count summary")
    return int(match.group(1)), int(match.group(2))


def _run_preflight(
    *,
    source_namespace: str,
    destination_ip: str,
    artifact: Path,
) -> Dict[str, Any]:
    """Require complete reachability before any backend or traffic action."""

    command = _namespace_command(
        source_namespace,
        ["ping", "-n", "-c", "3", "-W", "1", destination_ip],
    )
    completed = _run_capture(command, timeout_s=10.0)
    combined = (
        f"$ {' '.join(command)}\n"
        f"returncode={completed.returncode}\n"
        f"--- stdout ---\n{completed.stdout}"
        f"\n--- stderr ---\n{completed.stderr}"
    )
    atomic_write_text(artifact, combined)
    transmitted, received = _parse_ping_counts(completed.stdout)
    valid = (
        completed.returncode == 0
        and transmitted == 3
        and received == transmitted
    )
    evidence = {
        "argv": command,
        "returncode": completed.returncode,
        "transmitted": transmitted,
        "received": received,
        "complete": valid,
        "artifact": str(artifact),
    }
    if not valid:
        raise S1ExecutionError(
            f"preflight failed for {source_namespace}->{destination_ip}"
        )
    return evidence


def _backend_failure_message(details: Any) -> str:
    """Extract a useful message from a backend response."""

    if isinstance(details, dict):
        error = details.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)
    return str(details)


def _call_apply_qos(
    module: Any,
    *,
    domain_context: Dict[str, Any],
    intent: Dict[str, Any],
    target: Optional[Dict[str, Any]],
) -> Any:
    """Call historical two- or three-argument backend signatures."""

    function = getattr(module, "apply_qos")
    parameter_count = len(inspect.signature(function).parameters)
    if parameter_count >= 3:
        try:
            return function(domain_context, intent, target)
        except TypeError:
            return function(domain_context.get("name", "X"), intent, target)
    return function(domain_context, intent)


def _normalize_backend_result(raw: Any) -> Tuple[bool, Any]:
    """Normalize the backend result forms retained by the artifact."""

    if (
        isinstance(raw, (list, tuple))
        and len(raw) == 2
        and isinstance(raw[0], bool)
    ):
        return raw[0], raw[1]
    if isinstance(raw, dict) and isinstance(raw.get("applied"), bool):
        return raw["applied"], raw
    return True, raw


def _apply_backend_chain(
    modules: Any,
    *,
    domain_name: str,
    intent: Dict[str, Any],
    target: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Apply every backend in one domain and retain every failure."""

    chain = modules if isinstance(modules, (list, tuple)) else [modules]
    if not chain or any(module is None for module in chain):
        return {
            "applied": False,
            "responses": [],
            "error": "backend router returned an empty module chain",
        }

    responses: List[Dict[str, Any]] = []
    all_applied = True
    for module in chain:
        backend_name = getattr(module, "__name__", str(module))
        try:
            raw = _call_apply_qos(
                module,
                domain_context={"name": domain_name},
                intent=intent,
                target=target,
            )
            applied, response = _normalize_backend_result(raw)
            all_applied = all_applied and bool(applied)
            responses.append(
                {
                    "backend": backend_name,
                    "applied": bool(applied),
                    "response": response,
                }
            )
        except Exception as exc:
            all_applied = False
            responses.append(
                {
                    "backend": backend_name,
                    "applied": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {"applied": all_applied, "responses": responses}


def _load_real_targets() -> Dict[str, Any]:
    """Load real backend endpoints while keeping credentials out of summaries."""

    import yaml

    path = ROOT / "l2i" / "backends" / "backends_real.yaml"
    if not path.is_file():
        raise S1ExecutionError(f"real backend configuration is missing: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise S1ExecutionError("real backend configuration must be a mapping")
    return document


def _readback_is_nonempty(value: Any) -> bool:
    """Reject absent and explicit failure placeholders as readback evidence."""

    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return not any(
        marker in lowered
        for marker in (
            "[no_dump]",
            "[readback_failed]",
            "[error]",
        )
    )


def _netconf_snapshot_matches(
    snapshot: str,
    intent: S1Intent,
) -> bool:
    """Verify that NETCONF readback preserves optional-bound semantics."""

    try:
        root = ET.fromstring(snapshot)
    except ET.ParseError:
        return False

    values: Dict[str, List[str]] = {}
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name in {"class", "min-mbps", "max-mbps"}:
            values.setdefault(local_name, []).append(
                str(element.text or "").strip()
            )

    expected_minimum = str(int(intent.bandwidth_min_mbps))
    if (
        not intent.bandwidth_min_mbps.is_integer()
        or expected_minimum not in values.get("min-mbps", [])
        or "prio10" not in values.get("class", [])
    ):
        return False

    maximum_values = values.get("max-mbps", [])
    if intent.bandwidth_max_mbps is None:
        return not maximum_values
    if not intent.bandwidth_max_mbps.is_integer():
        return False
    return str(int(intent.bandwidth_max_mbps)) in maximum_values


def _gate_control_domain(
    *,
    domain: str,
    backend_mode: str,
    result: Dict[str, Any],
    intent: S1Intent,
) -> Tuple[bool, str, str]:
    """Validate apply and readback evidence for one non-forwarding domain."""

    if not result.get("applied"):
        return False, "", f"{domain} backend application failed"
    responses = result.get("responses")
    if not isinstance(responses, list) or not responses:
        return False, "", f"{domain} backend returned no response"

    for item in responses:
        response = item.get("response")
        if not isinstance(response, dict):
            continue
        if backend_mode == "mock":
            planned = response.get("planned")
            execution = response.get("executed") or response.get("exec")
            if (
                isinstance(planned, dict)
                and isinstance(execution, dict)
                and execution.get("simulated") is True
                and execution.get("ok") is True
            ):
                return (
                    True,
                    json.dumps(response, ensure_ascii=False, indent=2),
                    "mock plan and simulated execution recorded",
                )
        elif domain == "B":
            snapshot = response.get("running_snapshot")
            if (
                _readback_is_nonempty(snapshot)
                and _netconf_snapshot_matches(str(snapshot), intent)
            ):
                return True, str(snapshot), "NETCONF running readback recorded"
        elif domain == "C":
            dump = response.get("readback_dump")
            installed_rule = response.get("installed_rule")
            if (
                _readback_is_nonempty(dump)
                and isinstance(installed_rule, dict)
                and response.get("readback_verified") is True
                and DESTINATION_IP in str(installed_rule.get("match", ""))
                and installed_rule.get("new_dscp") is not None
            ):
                return True, str(dump), "P4 rule and table readback recorded"
    return False, "", f"{domain} readback evidence is incomplete"


def _setup_linux_environment(
    *,
    module: Any,
    capacities: Mapping[str, float],
    delay_ms: float,
    protected_minimum_mbps: float,
) -> Tuple[Dict[str, Any], float]:
    """Materialize identical baseline/adapt link conditions on A, B, and C."""

    started = time.perf_counter()
    segments: Dict[str, Any] = {}
    setup = getattr(module, "setup_environment")
    for segment in ("A", "B", "C"):
        # Reserve exactly the declared sensitive minimum at the shared
        # bottleneck.  The default class may still borrow up to full capacity,
        # so baseline behavior is not artificially capped.
        default_rate = (
            capacities[segment] - protected_minimum_mbps
            if segment == "B"
            else capacities[segment]
        )
        target = {
            "device": ENVIRONMENT_LINKS[segment],
            "default_class": "prio30",
            # HTB class identifiers do not imply priority.  Explicitly assign
            # the lowest scheduler priority to the best-effort class.
            "default_priority": 7,
            "default_rate_mbps": default_rate,
            "default_ceil_mbps": capacities[segment],
            "attach_netem": True,
            "delay_ms": delay_ms,
            "idempotent_cleanup": True,
        }
        ok, details = setup(
            {"name": segment},
            {
                "bw_mbps": capacities[segment],
                "delay_ms": delay_ms,
                "create_default_class": True,
                "default_priority": 7,
                "default_rate_mbps": default_rate,
                "default_ceil_mbps": capacities[segment],
                "attach_netem": True,
            },
            target,
        )
        segments[segment] = details
        if not ok:
            raise S1ExecutionError(
                f"Linux environment setup failed on segment {segment}: "
                + _backend_failure_message(details)
            )
        checks = details.get("readback_checks")
        if not isinstance(checks, dict) or not all(checks.values()):
            raise S1ExecutionError(
                f"Linux environment readback failed on segment {segment}"
            )
    return segments, (time.perf_counter() - started) * 1000.0


def _apply_sensitive_overlay(
    *,
    module: Any,
    intent: S1Intent,
    bottleneck_capacity_mbps: float,
    delay_ms: float,
) -> Tuple[Dict[str, Any], float, float]:
    """Classify sensitive TCP and RTT probes into one protected HTB class."""

    realization_ceil = (
        min(intent.bandwidth_max_mbps, bottleneck_capacity_mbps)
        if intent.bandwidth_max_mbps is not None
        else bottleneck_capacity_mbps
    )
    if realization_ceil < intent.bandwidth_min_mbps:
        raise S1ExecutionError(
            "the bottleneck cannot realize the declared minimum bandwidth"
        )

    target = {
        "device": BOTTLENECK_INTERFACE,
        "default_class": "prio30",
        "htb_priority": 0,
        "priority_delay_ms": delay_ms,
        "attach_priority_netem": True,
        "idempotent_cleanup": True,
        "classifiers": [
            {
                "protocol": "tcp",
                "dst_ip": DESTINATION_IP,
                "dst_port": SENSITIVE_PORT,
                "filter_priority": 1,
            },
            {
                "protocol": "icmp",
                "dst_ip": DESTINATION_IP,
                "filter_priority": 2,
            },
        ],
    }
    realization_intent = {
        "class": "prio10",
        "min_mbps": intent.bandwidth_min_mbps,
        # The physical ceiling is a realization constraint.  It is not written
        # back into the immutable L2i specification as an intent maximum.
        "max_mbps": realization_ceil,
    }

    started = time.perf_counter()
    ok, details = module.apply_qos(
        {"name": "A", "role": "measured-bottleneck"},
        realization_intent,
        target,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if not ok:
        raise S1ExecutionError(
            "Linux sensitive-flow overlay failed: "
            + _backend_failure_message(details)
        )
    checks = details.get("readback_checks")
    if not isinstance(checks, dict) or not all(checks.values()):
        raise S1ExecutionError(
            "Linux sensitive-flow overlay readback is incomplete"
        )
    classifiers = (details.get("intent") or {}).get("classifiers")
    if not isinstance(classifiers, list) or len(classifiers) != 2:
        raise S1ExecutionError(
            "Linux overlay did not preserve both canonical classifiers"
        )
    return details, elapsed_ms, realization_ceil


def _format_tc_dump(
    *,
    environment: Mapping[str, Any],
    overlay: Optional[Mapping[str, Any]],
) -> str:
    """Create one human-readable readback artifact for the Linux data path."""

    sections: List[str] = []
    for segment in ("A", "B", "C"):
        details = environment.get(segment) or {}
        readback = details.get("readback") or {}
        sections.extend(
            [
                f"### segment {segment} device={ENVIRONMENT_LINKS[segment]}",
                "## qdisc",
                str(readback.get("qdisc", "")),
                "## class",
                str(readback.get("class", "")),
                "## filter",
                str(readback.get("filter", "")),
                "",
            ]
        )
    if overlay is not None:
        readback = overlay.get("readback") or {}
        sections.extend(
            [
                "### adapt overlay on shared bottleneck",
                "## qdisc",
                str(readback.get("qdisc", "")),
                "## class",
                str(readback.get("class", "")),
                "## filter",
                str(readback.get("filter", "")),
                "",
            ]
        )
    return "\n".join(sections)


def _server_ready(namespace: str, port: int) -> bool:
    """Check whether one iperf3 server is listening in the destination."""

    completed = _run_capture(
        _namespace_command(namespace, ["ss", "-H", "-ltn"]),
        timeout_s=2.0,
    )
    return completed.returncode == 0 and f":{port}" in completed.stdout


def _start_iperf_server(port: int) -> subprocess.Popen[str]:
    """Start one one-shot iperf3 server for a canonical flow."""

    command = _namespace_command(
        DESTINATION_NS,
        [
            "iperf3",
            "-s",
            "-1",
            "-B",
            DESTINATION_IP,
            "-p",
            str(port),
        ],
    )
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(100):
        if process.poll() is not None:
            break
        if _server_ready(DESTINATION_NS, port):
            return process
        time.sleep(0.05)

    stdout, stderr = process.communicate(timeout=2.0)
    raise S1ExecutionError(
        f"iperf3 server on port {port} did not become ready: "
        f"stdout={stdout!r} stderr={stderr!r}"
    )


def _finish_server(
    process: subprocess.Popen[str],
    *,
    port: int,
) -> Dict[str, Any]:
    """Collect server output and prevent a failed client from leaking a process."""

    try:
        stdout, stderr = process.communicate(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=2.0)
    return {
        "port": port,
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def _run_timed_command(
    *,
    name: str,
    argv: Sequence[str],
    barrier: threading.Barrier,
    reference: float,
    timeout_s: float,
) -> TimedCommand:
    """Launch one command after a shared barrier and observe its actual window."""

    barrier.wait(timeout=10.0)
    started = time.perf_counter()
    completed = _run_capture(argv, timeout_s=timeout_s)
    ended = time.perf_counter()
    return TimedCommand(
        name=name,
        argv=list(map(str, argv)),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        started_offset_s=round(started - reference, 6),
        ended_offset_s=round(ended - reference, 6),
        elapsed_s=round(ended - started, 6),
    )


def _derived_rtt_samples(duration_s: int, interval_ms: int) -> int:
    """Cover the offered-load interval instead of probing only after traffic."""

    return max(2, int(math.ceil(duration_s * 1000.0 / interval_ms)) + 1)


def _parse_iperf_result(result: TimedCommand) -> Dict[str, Any]:
    """Reject missing, failed, or structurally incomplete iperf3 output."""

    if result.returncode != 0:
        raise S1ExecutionError(
            f"{result.name} exited with status {result.returncode}"
        )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise S1ExecutionError(f"{result.name} did not emit JSON") from exc
    if not isinstance(document, dict) or document.get("error"):
        raise S1ExecutionError(
            f"{result.name} reported an iperf3 error: {document.get('error')}"
        )
    try:
        receiver = document["end"]["sum_received"]
        bits_per_second = float(receiver["bits_per_second"])
        seconds = float(receiver["seconds"])
        byte_count = int(receiver["bytes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise S1ExecutionError(
            f"{result.name} receiver summary is incomplete"
        ) from exc
    if (
        not math.isfinite(bits_per_second)
        or bits_per_second <= 0
        or not math.isfinite(seconds)
        or seconds <= 0
        or byte_count <= 0
    ):
        raise S1ExecutionError(
            f"{result.name} receiver summary contains no valid measurement"
        )
    return {
        "throughput_mbps": bits_per_second / 1_000_000.0,
        "receiver_seconds": seconds,
        "receiver_bytes": byte_count,
    }


def _parse_ping_measurement(
    *,
    result: TimedCommand,
    requested_samples: int,
    csv_path: Path,
) -> Dict[str, Any]:
    """Parse RTT while rejecting any empty or incomplete sample set."""

    transmitted, received = _parse_ping_counts(result.stdout)
    sample_pattern = re.compile(r"\btime[=<]([0-9]+(?:\.[0-9]+)?)\s*ms")
    values = [float(match) for match in sample_pattern.findall(result.stdout)]

    rows = ["seq,rtt_ms"]
    rows.extend(
        f"{index},{value:.6f}"
        for index, value in enumerate(values, start=1)
    )
    atomic_write_text(csv_path, "\n".join(rows) + "\n")

    complete = (
        result.returncode == 0
        and transmitted == requested_samples
        and received == requested_samples
        and len(values) == requested_samples
    )
    evidence: Dict[str, Any] = {
        "requested": requested_samples,
        "transmitted": transmitted,
        "received": received,
        "parsed_samples": len(values),
        "delivery_ratio": (
            received / transmitted if transmitted > 0 else 0.0
        ),
        "complete": complete,
    }
    if not values:
        raise S1ExecutionError("RTT measurement contains no samples")

    evidence["rtt_ms"] = {
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "minimum": min(values),
        "maximum": max(values),
    }
    if not complete:
        raise S1ExecutionError(
            "RTT measurement is incomplete; missing probes cannot be "
            "silently excluded from conformance"
        )
    return evidence


def _run_data_plane(
    *,
    duration_s: int,
    flow_mbps: float,
    best_effort_mbps: float,
    rtt_interval_ms: int,
    rtt_samples: Optional[int],
    artifacts: Mapping[str, Path],
) -> Dict[str, Any]:
    """Run both TCP loads and RTT probes over one simultaneous time window."""

    sample_count = (
        rtt_samples
        if rtt_samples is not None
        else _derived_rtt_samples(duration_s, rtt_interval_ms)
    )
    sensitive_server: Optional[subprocess.Popen[str]] = None
    best_effort_server: Optional[subprocess.Popen[str]] = None
    server_evidence: List[Dict[str, Any]] = []

    try:
        sensitive_server = _start_iperf_server(SENSITIVE_PORT)
        best_effort_server = _start_iperf_server(BEST_EFFORT_PORT)

        reference = time.perf_counter()
        barrier = threading.Barrier(4)
        sensitive_command = _namespace_command(
            SENSITIVE_SOURCE_NS,
            [
                "iperf3",
                "-c",
                DESTINATION_IP,
                "-p",
                str(SENSITIVE_PORT),
                "-t",
                str(duration_s),
                "-b",
                f"{flow_mbps}M",
                "--json",
            ],
        )
        best_effort_command = _namespace_command(
            BEST_EFFORT_SOURCE_NS,
            [
                "iperf3",
                "-c",
                DESTINATION_IP,
                "-p",
                str(BEST_EFFORT_PORT),
                "-t",
                str(duration_s),
                "-b",
                f"{best_effort_mbps}M",
                "--json",
            ],
        )
        ping_command = _namespace_command(
            SENSITIVE_SOURCE_NS,
            [
                "ping",
                "-n",
                "-i",
                f"{rtt_interval_ms / 1000.0:.3f}",
                "-c",
                str(sample_count),
                "-W",
                "1",
                DESTINATION_IP,
            ],
        )

        with ThreadPoolExecutor(
            max_workers=3,
            thread_name_prefix="s1-data-plane",
        ) as executor:
            futures = {
                "sensitive_tcp": executor.submit(
                    _run_timed_command,
                    name="sensitive_tcp",
                    argv=sensitive_command,
                    barrier=barrier,
                    reference=reference,
                    timeout_s=duration_s + 30.0,
                ),
                "best_effort_tcp": executor.submit(
                    _run_timed_command,
                    name="best_effort_tcp",
                    argv=best_effort_command,
                    barrier=barrier,
                    reference=reference,
                    timeout_s=duration_s + 30.0,
                ),
                "rtt_probes": executor.submit(
                    _run_timed_command,
                    name="rtt_probes",
                    argv=ping_command,
                    barrier=barrier,
                    reference=reference,
                    timeout_s=duration_s + 30.0,
                ),
            }
            # The main thread is the fourth barrier participant.  No command
            # can begin until all three workers have reached this point.
            barrier.wait(timeout=10.0)
            results = {
                name: future.result()
                for name, future in futures.items()
            }

        # Collect both server processes before parsing metrics so every process
        # exit code participates in the fail-closed execution gate.
        server_evidence.append(
            _finish_server(
                sensitive_server,
                port=SENSITIVE_PORT,
            )
        )
        sensitive_server = None
        server_evidence.append(
            _finish_server(
                best_effort_server,
                port=BEST_EFFORT_PORT,
            )
        )
        best_effort_server = None
        atomic_write_json(
            artifacts["iperf_servers_json"],
            server_evidence,
        )
        if any(
            server.get("returncode") != 0
            for server in server_evidence
        ):
            raise S1ExecutionError(
                "one or more iperf3 servers exited with a non-zero status"
            )

        sensitive = results["sensitive_tcp"]
        best_effort = results["best_effort_tcp"]
        rtt = results["rtt_probes"]

        atomic_write_text(
            artifacts["iperf_sensitive_json"],
            sensitive.stdout,
        )
        atomic_write_text(
            artifacts["iperf_sensitive_stderr"],
            sensitive.stderr,
        )
        atomic_write_text(
            artifacts["iperf_best_effort_json"],
            best_effort.stdout,
        )
        atomic_write_text(
            artifacts["iperf_best_effort_stderr"],
            best_effort.stderr,
        )
        atomic_write_text(artifacts["rtt_stdout"], rtt.stdout)
        atomic_write_text(artifacts["rtt_stderr"], rtt.stderr)

        sensitive_metrics = _parse_iperf_result(sensitive)
        best_effort_metrics = _parse_iperf_result(best_effort)
        rtt_metrics = _parse_ping_measurement(
            result=rtt,
            requested_samples=sample_count,
            csv_path=artifacts["rtt_csv"],
        )

        starts = [
            result.started_offset_s
            for result in results.values()
        ]
        ends = [
            result.ended_offset_s
            for result in results.values()
        ]
        overlap_s = max(0.0, min(ends) - max(starts))
        minimum_overlap_s = duration_s * 0.90
        overlap_complete = overlap_s >= minimum_overlap_s
        if not overlap_complete:
            raise S1ExecutionError(
                "the observed simultaneous window is shorter than 90% "
                "of the configured traffic duration"
            )

        return {
            "commands": {
                name: result.evidence()
                for name, result in results.items()
            },
            "observed_windows": {
                "simultaneous_overlap_s": round(overlap_s, 6),
                "required_overlap_s": round(minimum_overlap_s, 6),
                "complete": overlap_complete,
            },
            "sensitive": sensitive_metrics,
            "best_effort": best_effort_metrics,
            "rtt": rtt_metrics,
            "exit_codes": {
                **{
                    name: result.returncode
                    for name, result in results.items()
                },
                **{
                    f"iperf_server_{server['port']}": server["returncode"]
                    for server in server_evidence
                },
            },
        }
    finally:
        if sensitive_server is not None:
            server_evidence.append(
                _finish_server(
                    sensitive_server,
                    port=SENSITIVE_PORT,
                )
            )
        if best_effort_server is not None:
            server_evidence.append(
                _finish_server(
                    best_effort_server,
                    port=BEST_EFFORT_PORT,
                )
            )
        if server_evidence:
            atomic_write_json(
                artifacts["iperf_servers_json"],
                server_evidence,
            )


def _validate_arguments(args: argparse.Namespace, intent: S1Intent) -> None:
    """Reject configurations that cannot exercise canonical S1 claims."""

    if args.duration < 2:
        raise S1ExecutionError("duration must be at least two seconds")
    if args.repetition < 1:
        raise S1ExecutionError("repetition must be greater than zero")
    if args.rtt_interval_ms < 10:
        raise S1ExecutionError("rtt-interval-ms must be at least 10")
    if args.rtt_samples is not None and args.rtt_samples < 2:
        raise S1ExecutionError("rtt-samples must be at least two")

    capacities = {
        "A": _positive_float(args.bwA, "bwA"),
        "B": _positive_float(args.bwB, "bwB"),
        "C": _positive_float(args.bwC, "bwC"),
    }
    flow_mbps = _positive_float(args.flow_mbps, "flow-mbps")
    best_effort_mbps = _positive_float(args.be_mbps, "be-mbps")
    _nonnegative_float(
        args.bandwidth_tolerance_mbps,
        "bandwidth-tolerance-mbps",
    )
    _nonnegative_float(args.delay_ms, "delay-ms")

    if not (
        capacities["B"] < capacities["A"]
        and capacities["B"] < capacities["C"]
    ):
        raise S1ExecutionError(
            "canonical S1 requires bwB to be the unique shared bottleneck"
        )
    if flow_mbps + best_effort_mbps <= capacities["B"]:
        raise S1ExecutionError(
            "offered loads must exceed bwB to create simultaneous contention"
        )
    if flow_mbps < intent.bandwidth_min_mbps:
        raise S1ExecutionError(
            "flow-mbps must be at least the declared bandwidth minimum"
        )
    if (
        intent.bandwidth_max_mbps is not None
        and flow_mbps > intent.bandwidth_max_mbps
    ):
        raise S1ExecutionError(
            "flow-mbps must not exceed the declared bandwidth maximum"
        )
    if intent.bandwidth_min_mbps >= capacities["B"]:
        raise S1ExecutionError(
            "bwB must exceed the declared bandwidth minimum so best-effort "
            "traffic retains a positive baseline share"
        )


def _require_canonical_provenance(
    provenance: RepositoryProvenance,
) -> None:
    """Require the synchronized, clean develop state used by final evidence."""

    failures: List[str] = []
    if provenance.branch != "develop":
        failures.append(f"branch={provenance.branch!r}")
    if not provenance.worktree_clean:
        failures.append("worktree is dirty")
    if provenance.origin_commit is None:
        failures.append("origin/develop is unavailable")
    elif provenance.origin_commit != provenance.commit:
        failures.append("HEAD differs from origin/develop")
    if failures:
        raise ExperimentContractError(
            "canonical repository provenance gate failed: "
            + "; ".join(failures)
        )


def _artifact_paths(
    run_directory: ExperimentRunDirectory,
) -> Dict[str, Path]:
    """Resolve every artifact inside the collision-safe execution directory."""

    names = {
        "manifest": "manifest.json",
        "summary": "summary.json",
        "domain_a": "domain-A-linux-tc.json",
        "domain_b": "domain-B-netconf.json",
        "domain_c": "domain-C-p4runtime.json",
        "tc_readback": "domain-A-linux-tc-readback.txt",
        "netconf_readback": "domain-B-netconf-readback.txt",
        "p4_readback": "domain-C-p4runtime-readback.txt",
        "preflight_sensitive": "preflight-sensitive.txt",
        "preflight_best_effort": "preflight-best-effort.txt",
        "iperf_sensitive_json": "iperf-sensitive.json",
        "iperf_sensitive_stderr": "iperf-sensitive.stderr.txt",
        "iperf_best_effort_json": "iperf-best-effort.json",
        "iperf_best_effort_stderr": "iperf-best-effort.stderr.txt",
        "iperf_servers_json": "iperf-servers.json",
        "rtt_stdout": "rtt-ping.stdout.txt",
        "rtt_stderr": "rtt-ping.stderr.txt",
        "rtt_csv": "rtt-samples.csv",
        "artifact_hashes": "artifact-hashes.json",
    }
    return {
        key: run_directory.artifact(name)
        for key, name in names.items()
    }


def _configuration(
    *,
    args: argparse.Namespace,
    intent: S1Intent,
) -> Dict[str, Any]:
    """Build the complete immutable configuration hashed by the contract."""

    return {
        "duration_s": args.duration,
        "flow_offered_mbps": args.flow_mbps,
        "best_effort_offered_mbps": args.be_mbps,
        "capacities_mbps": {
            "A": args.bwA,
            "B": args.bwB,
            "C": args.bwC,
        },
        "delay_ms_per_shaped_egress": args.delay_ms,
        "rtt_interval_ms": args.rtt_interval_ms,
        "rtt_samples": args.rtt_samples,
        "bandwidth_tolerance_mbps": args.bandwidth_tolerance_mbps,
        "topology": {
            "sensitive_source": {
                "namespace": SENSITIVE_SOURCE_NS,
                "ip": SENSITIVE_SOURCE_IP,
                "destination_port": SENSITIVE_PORT,
            },
            "best_effort_source": {
                "namespace": BEST_EFFORT_SOURCE_NS,
                "ip": BEST_EFFORT_SOURCE_IP,
                "destination_port": BEST_EFFORT_PORT,
            },
            "destination": {
                "namespace": DESTINATION_NS,
                "ip": DESTINATION_IP,
            },
            "shared_bottleneck_interface": BOTTLENECK_INTERFACE,
            "environment_links": ENVIRONMENT_LINKS,
        },
        "intent_interpretation": asdict(intent),
        "causal_scope": {
            "forwarding_path": "linux-bridge-veth",
            "traffic_effect_backend": "linux-tc",
            "netconf": "materialization-and-readback-only",
            "p4runtime": "materialization-and-readback-only",
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    """Create the canonical command-line interface."""

    parser = argparse.ArgumentParser(
        description="Run canonical S1 with simultaneous contention and RTT.",
    )
    parser.add_argument("--spec", required=True)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--flow-mbps", type=float, default=8.0)
    parser.add_argument("--be-mbps", type=float, default=60.0)
    parser.add_argument(
        "--mode",
        choices=("baseline", "adapt"),
        default="baseline",
    )
    parser.add_argument(
        "--backend",
        choices=("mock", "real"),
        default="mock",
    )
    parser.add_argument("--bwA", type=float, default=100.0)
    parser.add_argument("--bwB", type=float, default=50.0)
    parser.add_argument("--bwC", type=float, default=100.0)
    parser.add_argument("--delay-ms", type=float, default=1.0)
    parser.add_argument("--rtt-interval-ms", type=int, default=50)
    parser.add_argument("--rtt-samples", type=int, default=None)
    parser.add_argument(
        "--bandwidth-tolerance-mbps",
        type=float,
        default=0.25,
    )
    parser.add_argument("--profile-id", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument(
        "--execution-id",
        default=None,
        help="Optional preallocated identity; collisions are rejected.",
    )
    parser.add_argument(
        "--results-root",
        default=str(ROOT / "results"),
    )
    return parser


def _write_artifact_hashes(
    *,
    artifacts: Mapping[str, Path],
) -> None:
    """Hash every completed artifact except the hash list itself."""

    records: Dict[str, str] = {}
    for key, path in sorted(artifacts.items()):
        if key == "artifact_hashes" or not path.is_file():
            continue
        records[path.name] = sha256_file(path)
    atomic_write_json(artifacts["artifact_hashes"], records)


def execute(args: argparse.Namespace) -> Path:
    """Execute one canonical S1 run and return its final summary path."""

    script_started = time.perf_counter()
    started_at = utc_now()

    parse_started = time.perf_counter()
    specification_path = Path(args.spec).expanduser().resolve()
    specification, intent = _load_specification(specification_path)
    _validate_arguments(args, intent)
    parse_ms = (time.perf_counter() - parse_started) * 1000.0

    provenance = RepositoryProvenance.capture(ROOT)
    _require_canonical_provenance(provenance)
    execution_id = args.execution_id or generate_execution_id(SCENARIO_ID)
    identity = ExperimentIdentity(
        scenario_id=SCENARIO_ID,
        execution_id=execution_id,
        profile_id=args.profile_id,
        mode=args.mode,
        backend_mode=args.backend,
        repetition=args.repetition,
    )
    run_directory = ExperimentRunDirectory.create(
        Path(args.results_root),
        identity,
    )
    artifacts = _artifact_paths(run_directory)
    configuration = _configuration(args=args, intent=intent)
    manifest = build_run_manifest(
        identity=identity,
        provenance=provenance,
        specification_path=specification_path,
        configuration=configuration,
        started_at=started_at,
    )
    atomic_write_json(artifacts["manifest"], manifest)

    summary: Dict[str, Any] = {
        "contract_version": manifest["contract_version"],
        "identity": identity.to_dict(),
        "run_status": "running",
        "started_at_utc": utc_rfc3339(started_at),
        "completed_at_utc": None,
        "manifest": {
            "path": str(artifacts["manifest"]),
            "sha256": sha256_file(artifacts["manifest"]),
        },
        "specification": specification,
        "intent": asdict(intent),
        "configuration": configuration,
        "causal_scope": configuration["causal_scope"],
        "gates": {
            "repository_provenance": True,
            "preflight": False,
            "linux_environment_readback": False,
            "linux_overlay_readback": args.mode == "baseline",
            "netconf_apply_readback": args.mode == "baseline",
            "p4runtime_apply_readback": args.mode == "baseline",
            "data_plane_exit_codes": False,
            "measurement_complete": False,
            "simultaneous_window": False,
        },
        "timing": {
            "preparation_ms": None,
            "control_plane_ms": {
                "total": None,
                "linux_environment": None,
                "linux_overlay": 0.0,
                "netconf": 0.0,
                "p4runtime": 0.0,
            },
            "data_plane_ms": None,
            "script_total_ms": None,
            "specification_parse_ms": round(parse_ms, 3),
        },
        "artifacts": {
            key: str(path)
            for key, path in artifacts.items()
        },
    }

    try:
        preflight_started = time.perf_counter()
        preflight_sensitive = _run_preflight(
            source_namespace=SENSITIVE_SOURCE_NS,
            destination_ip=DESTINATION_IP,
            artifact=artifacts["preflight_sensitive"],
        )
        preflight_best_effort = _run_preflight(
            source_namespace=BEST_EFFORT_SOURCE_NS,
            destination_ip=DESTINATION_IP,
            artifact=artifacts["preflight_best_effort"],
        )
        summary["preflight"] = {
            "sensitive": preflight_sensitive,
            "best_effort": preflight_best_effort,
        }
        summary["gates"]["preflight"] = True
        summary["timing"]["preparation_ms"] = round(
            (time.perf_counter() - preflight_started) * 1000.0
            + parse_ms,
            3,
        )

        control_started = time.perf_counter()
        backends = get_backends(args.backend)
        linux_module = backends.get("A")
        if linux_module is None:
            raise S1ExecutionError("backend router did not provide domain A")
        capacities = {
            "A": float(args.bwA),
            "B": float(args.bwB),
            "C": float(args.bwC),
        }
        environment, environment_ms = _setup_linux_environment(
            module=linux_module,
            capacities=capacities,
            delay_ms=float(args.delay_ms),
            protected_minimum_mbps=intent.bandwidth_min_mbps,
        )
        summary["gates"]["linux_environment_readback"] = True

        overlay: Optional[Dict[str, Any]] = None
        overlay_ms = 0.0
        realization_ceil: Optional[float] = None
        if args.mode == "adapt":
            overlay, overlay_ms, realization_ceil = _apply_sensitive_overlay(
                module=linux_module,
                intent=intent,
                bottleneck_capacity_mbps=float(args.bwB),
                delay_ms=float(args.delay_ms),
            )
            summary["gates"]["linux_overlay_readback"] = True

        domain_a = {
            "backend": "linux_tc_local",
            "applied": True,
            "environment": environment,
            "intent_overlay": overlay,
            "intent_overlay_applied": overlay is not None,
            "realization_ceil_mbps": realization_ceil,
            "measured_bottleneck_interface": BOTTLENECK_INTERFACE,
        }
        atomic_write_json(artifacts["domain_a"], domain_a)
        atomic_write_text(
            artifacts["tc_readback"],
            _format_tc_dump(
                environment=environment,
                overlay=overlay,
            ),
        )

        real_targets = (
            _load_real_targets()
            if args.backend == "real"
            else {}
        )
        target_b = (
            (real_targets.get("B") or {}).get("target")
            if args.backend == "real"
            else None
        )
        target_c = (
            (real_targets.get("C") or {}).get("target")
            if args.backend == "real"
            else None
        )
        if isinstance(target_c, dict):
            target_c = dict(target_c)
            target_c["dst_ip"] = DESTINATION_IP

        backend_apply = {
            "A_environment": True,
            "A_intent_overlay": overlay is not None,
            "B": False,
            "C": False,
        }
        if args.mode == "adapt":
            control_intent = intent.to_backend_intent()

            b_started = time.perf_counter()
            domain_b = _apply_backend_chain(
                backends.get("B"),
                domain_name="B",
                intent=control_intent,
                target=target_b,
            )
            b_ms = (time.perf_counter() - b_started) * 1000.0
            b_ok, b_readback, b_message = _gate_control_domain(
                domain="B",
                backend_mode=args.backend,
                result=domain_b,
                intent=intent,
            )
            domain_b.update(
                {
                    "target": _mask_secret(target_b),
                    "gate_message": b_message,
                    "data_plane_role": "materialization-and-readback-only",
                }
            )
            atomic_write_json(artifacts["domain_b"], domain_b)
            atomic_write_text(artifacts["netconf_readback"], b_readback)
            if not b_ok:
                raise S1ExecutionError(b_message)
            summary["gates"]["netconf_apply_readback"] = True
            backend_apply["B"] = True

            c_started = time.perf_counter()
            domain_c = _apply_backend_chain(
                backends.get("C"),
                domain_name="C",
                intent=control_intent,
                target=target_c,
            )
            c_ms = (time.perf_counter() - c_started) * 1000.0
            c_ok, c_readback, c_message = _gate_control_domain(
                domain="C",
                backend_mode=args.backend,
                result=domain_c,
                intent=intent,
            )
            domain_c.update(
                {
                    "target": _mask_secret(target_c),
                    "gate_message": c_message,
                    "data_plane_role": "materialization-and-readback-only",
                }
            )
            atomic_write_json(artifacts["domain_c"], domain_c)
            atomic_write_text(artifacts["p4_readback"], c_readback)
            if not c_ok:
                raise S1ExecutionError(c_message)
            summary["gates"]["p4runtime_apply_readback"] = True
            backend_apply["C"] = True
        else:
            b_ms = 0.0
            c_ms = 0.0
            domain_b = {
                "applied": False,
                "not_applied_reason": "baseline",
                "data_plane_role": "materialization-and-readback-only",
            }
            domain_c = {
                "applied": False,
                "not_applied_reason": "baseline",
                "data_plane_role": "materialization-and-readback-only",
            }
            atomic_write_json(artifacts["domain_b"], domain_b)
            atomic_write_json(artifacts["domain_c"], domain_c)
            atomic_write_text(
                artifacts["netconf_readback"],
                "[baseline] NETCONF materialization was intentionally skipped.\n",
            )
            atomic_write_text(
                artifacts["p4_readback"],
                "[baseline] P4Runtime materialization was intentionally skipped.\n",
            )

        control_ms = (time.perf_counter() - control_started) * 1000.0
        summary["backend_apply"] = backend_apply
        summary["timing"]["control_plane_ms"] = {
            "total": round(control_ms, 3),
            "linux_environment": round(environment_ms, 3),
            "linux_overlay": round(overlay_ms, 3),
            "netconf": round(b_ms, 3),
            "p4runtime": round(c_ms, 3),
        }

        data_started = time.perf_counter()
        data_plane = _run_data_plane(
            duration_s=args.duration,
            flow_mbps=float(args.flow_mbps),
            best_effort_mbps=float(args.be_mbps),
            rtt_interval_ms=args.rtt_interval_ms,
            rtt_samples=args.rtt_samples,
            artifacts=artifacts,
        )
        data_ms = (time.perf_counter() - data_started) * 1000.0
        summary["timing"]["data_plane_ms"] = round(data_ms, 3)
        summary["data_plane"] = data_plane
        summary["gates"]["data_plane_exit_codes"] = all(
            status == 0
            for status in data_plane["exit_codes"].values()
        )
        summary["gates"]["measurement_complete"] = bool(
            data_plane["rtt"]["complete"]
        )
        summary["gates"]["simultaneous_window"] = bool(
            data_plane["observed_windows"]["complete"]
        )

        rtt_key = intent.latency_percentile.lower()
        observed_rtt = float(data_plane["rtt"]["rtt_ms"][rtt_key])
        sensitive_throughput = float(
            data_plane["sensitive"]["throughput_mbps"]
        )
        tolerance = float(args.bandwidth_tolerance_mbps)
        latency_ok = observed_rtt <= intent.latency_max_ms
        bandwidth_min_ok = (
            sensitive_throughput + tolerance
            >= intent.bandwidth_min_mbps
        )
        bandwidth_max_ok = (
            True
            if intent.bandwidth_max_mbps is None
            else sensitive_throughput
            <= intent.bandwidth_max_mbps + tolerance
        )
        bandwidth_ok = bandwidth_min_ok and bandwidth_max_ok
        all_measurement_gates = all(
            summary["gates"][key]
            for key in (
                "data_plane_exit_codes",
                "measurement_complete",
                "simultaneous_window",
            )
        )

        summary["metrics"] = {
            "rtt_percentile": intent.latency_percentile,
            "rtt_percentile_ms": observed_rtt,
            "rtt_ms": data_plane["rtt"]["rtt_ms"],
            "rtt_samples": data_plane["rtt"]["parsed_samples"],
            "delivery_ratio": data_plane["rtt"]["delivery_ratio"],
            "sensitive_throughput_mbps": sensitive_throughput,
            "best_effort_throughput_mbps": float(
                data_plane["best_effort"]["throughput_mbps"]
            ),
            "simultaneous_overlap_s": data_plane[
                "observed_windows"
            ]["simultaneous_overlap_s"],
        }
        summary["conformance"] = {
            "measurement_valid": all_measurement_gates,
            "latency_ok": latency_ok,
            "bandwidth_min_ok": bandwidth_min_ok,
            "bandwidth_max_declared": (
                intent.bandwidth_max_mbps is not None
            ),
            "bandwidth_max_ok": bandwidth_max_ok,
            "bandwidth_ok": bandwidth_ok,
            "intent_ok": (
                all_measurement_gates
                and latency_ok
                and bandwidth_ok
            ),
            "bandwidth_tolerance_mbps": tolerance,
        }
        summary["run_status"] = "completed"
        summary["completed_at_utc"] = utc_rfc3339()
        summary["timing"]["script_total_ms"] = round(
            (time.perf_counter() - script_started) * 1000.0,
            3,
        )
        atomic_write_json(artifacts["summary"], summary)
        _write_artifact_hashes(artifacts=artifacts)
        return artifacts["summary"]
    except BaseException as exc:
        summary["run_status"] = "failed"
        summary["completed_at_utc"] = utc_rfc3339()
        summary["timing"]["script_total_ms"] = round(
            (time.perf_counter() - script_started) * 1000.0,
            3,
        )
        summary["failure"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        atomic_write_json(artifacts["summary"], summary)
        _write_artifact_hashes(artifacts=artifacts)
        raise S1ExecutionError(
            f"{exc}; failure summary: {artifacts['summary']}"
        ) from exc


def main() -> int:
    """CLI wrapper with stable success/failure markers."""

    args = _build_parser().parse_args()
    try:
        summary_path = execute(args)
    except BaseException as exc:
        print("PHASE19_S1_RUN_OK=False")
        print(f"PHASE19_S1_FAILURE={type(exc).__name__}: {exc}")
        return 1

    document = json.loads(summary_path.read_text(encoding="utf-8"))
    print(f"PHASE19_S1_EXECUTION_ID={document['identity']['execution_id']}")
    print(f"PHASE19_S1_SUMMARY={summary_path}")
    print("PHASE19_S1_RUN_OK=True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
