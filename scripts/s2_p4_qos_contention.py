#!/usr/bin/env python3
"""Exercise multicast QoS under a measured shared-egress bottleneck.

The experiment keeps the responsibilities of the emulated domains explicit:

* BMv2/P4Runtime performs multicast replication from port 0 to ports 1 and 2.
* A P4 unicast rule forwards background UDP traffic from port 3 to port 1.
* Linux ``tc`` shapes the receiver-B attachment egress (``s2b-h3``), where the
  multicast replica and the background flow demonstrably share one queueing
  resource.
* Receiver C remains outside the bottleneck and acts as an internal control.

The script does not claim that the current P4 pipeline implements queue
selection or bandwidth guarantees. QoS differentiation is materialized by the
Linux egress scheduler after BMv2 forwarding, and every summary records that
scope explicitly.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import socket
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, NoReturn, Sequence

# Allow direct execution from the repository without installation as a package.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.s2_multicast_dataplane_smoke import (
    dump_json,
    percentile,
    wait_repeated_sleep_spin,
)


BACKGROUND_MAGIC = b"L2IB"
BACKGROUND_HEADER = struct.Struct("!4sIQQ")


def fail(message: str) -> NoReturn:
    """Terminate with a stable marker suitable for archived evidence."""

    print(f"PHASE14_CONTENTION_FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def command_record(
    argv: Sequence[str],
    *,
    timeout_s: float = 15.0,
    check: bool = False,
) -> dict[str, Any]:
    """Execute one command and preserve its complete audit record."""

    started_ns = time.monotonic_ns()
    try:
        completed = subprocess.run(
            [str(item) for item in argv],
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        record = {
            "argv": [str(item) for item in argv],
            "returncode": int(completed.returncode),
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
            "elapsed_ms": (time.monotonic_ns() - started_ns) / 1_000_000.0,
        }
    except subprocess.TimeoutExpired as exc:
        record = {
            "argv": [str(item) for item in argv],
            "returncode": 124,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "command timed out",
            "elapsed_ms": (time.monotonic_ns() - started_ns) / 1_000_000.0,
        }
    except OSError as exc:
        record = {
            "argv": [str(item) for item in argv],
            "returncode": 127,
            "stdout": "",
            "stderr": str(exc),
            "elapsed_ms": (time.monotonic_ns() - started_ns) / 1_000_000.0,
        }

    if check and record["returncode"] != 0:
        raise RuntimeError(
            f"command failed with rc={record['returncode']}: "
            + " ".join(record["argv"])
            + f"\n{record['stderr']}"
        )
    return record


def allowed_cpus() -> list[int]:
    """Return the process affinity mask without assuming contiguous CPU IDs."""

    cpus = sorted(os.sched_getaffinity(0))
    if not cpus:
        fail("the process affinity mask does not expose any CPU")
    return cpus


def resolve_cpu(value: str, *, role: str, reserved: Iterable[int] = ()) -> int:
    """Resolve an explicit or automatic CPU while avoiding reserved CPUs."""

    normalized = value.strip().lower()
    cpus = allowed_cpus()
    reserved_set = set(int(item) for item in reserved)
    available = [cpu for cpu in cpus if cpu not in reserved_set]

    if normalized in {"auto", "auto-distinct"}:
        if not available:
            fail(f"no CPU remains available for {role}; allowed={cpus}")
        return available[-1]

    try:
        cpu = int(normalized)
    except ValueError as exc:
        raise SystemExit(
            f"{role} CPU must be 'auto', 'auto-distinct', or an integer"
        ) from exc

    if cpu not in cpus:
        fail(f"{role} CPU {cpu} is outside the allowed affinity mask {cpus}")
    if cpu in reserved_set:
        fail(f"{role} CPU {cpu} conflicts with reserved CPUs {sorted(reserved_set)}")
    return cpu


def sender_metrics(
    *,
    sent: int,
    planned: int,
    packet_size: int,
    requested_rate_mbps: float,
    started_ns: int,
    completed_ns: int,
    process_started_ns: int,
    process_completed_ns: int,
    interval_ns: int,
    spin_threshold_us: float,
    scheduler_lateness_ms: list[float],
    inter_send_ms: list[float],
    deadline_misses: int,
    send_errors: list[str],
) -> dict[str, Any]:
    """Build the common sender evidence block used by the background flow."""

    elapsed_s = (completed_ns - started_ns) / 1_000_000_000.0
    actual_rate_mbps = (
        (sent * packet_size * 8.0) / elapsed_s / 1_000_000.0
        if elapsed_s > 0.0
        else 0.0
    )
    rate_error_pct = (
        ((actual_rate_mbps - requested_rate_mbps) / requested_rate_mbps) * 100.0
        if requested_rate_mbps > 0.0
        else float("inf")
    )
    process_cpu_s = (
        process_completed_ns - process_started_ns
    ) / 1_000_000_000.0
    cpu_ratio = process_cpu_s / elapsed_s if elapsed_s > 0.0 else 0.0

    return {
        "packets_planned": planned,
        "packets_sent": sent,
        "packet_size_bytes": packet_size,
        "rate_payload_mbps_requested": requested_rate_mbps,
        "rate_payload_mbps_actual": actual_rate_mbps,
        "rate_error_pct": rate_error_pct,
        "elapsed_s": elapsed_s,
        "process_cpu_s": process_cpu_s,
        "cpu_ratio": cpu_ratio,
        "send_errors": send_errors,
        "pacing": {
            "mode": "repeated_sleep_spin",
            "interval_ns": interval_ns,
            "spin_threshold_us": spin_threshold_us,
            "no_catch_up": True,
            "deadline_misses": deadline_misses,
            "scheduler_lateness_ms": {
                "min": min(scheduler_lateness_ms) if scheduler_lateness_ms else None,
                "mean": statistics.fmean(scheduler_lateness_ms) if scheduler_lateness_ms else None,
                "p95": percentile(scheduler_lateness_ms, 0.95),
                "p99": percentile(scheduler_lateness_ms, 0.99),
                "max": max(scheduler_lateness_ms) if scheduler_lateness_ms else None,
                "n": len(scheduler_lateness_ms),
            },
            "inter_send_ms": {
                "min": min(inter_send_ms) if inter_send_ms else None,
                "mean": statistics.fmean(inter_send_ms) if inter_send_ms else None,
                "p50": percentile(inter_send_ms, 0.50),
                "p95": percentile(inter_send_ms, 0.95),
                "p99": percentile(inter_send_ms, 0.99),
                "max": max(inter_send_ms) if inter_send_ms else None,
                "n": len(inter_send_ms),
            },
        },
    }


def background_sender(args: argparse.Namespace) -> int:
    """Send paced unicast UDP traffic through BMv2 port 3 toward receiver B."""

    packet_size = int(args.packet_size)
    if packet_size < BACKGROUND_HEADER.size:
        fail(f"packet size must be at least {BACKGROUND_HEADER.size} bytes")
    if packet_size > 1400:
        fail("packet size must not exceed 1400 bytes")
    if args.rate_mbps <= 0.0 or args.duration <= 0.0:
        fail("duration and rate must be greater than zero")

    packets_per_second = (
        float(args.rate_mbps) * 1_000_000.0
    ) / (8.0 * packet_size)
    planned = max(1, int(round(float(args.duration) * packets_per_second)))
    interval_ns = max(1, int(1_000_000_000.0 / packets_per_second))
    spin_threshold_ns = int(round(float(args.spin_threshold_us) * 1_000.0))

    if spin_threshold_ns <= 0 or spin_threshold_ns >= interval_ns:
        fail("spin threshold must be positive and smaller than the packet interval")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    sock.bind((args.source_ip, 0))

    filler = b"B" * (packet_size - BACKGROUND_HEADER.size)
    process_started_ns = time.process_time_ns()
    started_ns = time.monotonic_ns()
    next_deadline_ns = started_ns
    previous_send_ns: int | None = None
    sent = 0
    send_errors: list[str] = []
    scheduler_lateness_ms: list[float] = []
    inter_send_ms: list[float] = []
    deadline_misses = 0

    for sequence in range(planned):
        wait_repeated_sleep_spin(next_deadline_ns, spin_threshold_ns)
        send_ns = time.monotonic_ns()
        lateness_ns = max(0, send_ns - next_deadline_ns)
        scheduler_lateness_ms.append(lateness_ns / 1_000_000.0)

        if lateness_ns >= interval_ns:
            deadline_misses += 1
        if previous_send_ns is not None:
            inter_send_ms.append((send_ns - previous_send_ns) / 1_000_000.0)

        datagram = (
            BACKGROUND_HEADER.pack(BACKGROUND_MAGIC, sequence, send_ns, planned)
            + filler
        )
        try:
            sock.sendto(datagram, (args.destination_ip, args.port))
            sent += 1
        except OSError as exc:
            send_errors.append(f"seq={sequence}: {exc}")

        previous_send_ns = send_ns

        # Anchor every new deadline to the actual send time. This prevents a
        # scheduler pause from being compensated by a burst of overdue packets.
        next_deadline_ns = send_ns + interval_ns

    completed_ns = time.monotonic_ns()
    process_completed_ns = time.process_time_ns()
    sock.close()

    result = {
        "role": "background_sender",
        "source_ip": args.source_ip,
        "destination_ip": args.destination_ip,
        "port": args.port,
        **sender_metrics(
            sent=sent,
            planned=planned,
            packet_size=packet_size,
            requested_rate_mbps=float(args.rate_mbps),
            started_ns=started_ns,
            completed_ns=completed_ns,
            process_started_ns=process_started_ns,
            process_completed_ns=process_completed_ns,
            interval_ns=interval_ns,
            spin_threshold_us=float(args.spin_threshold_us),
            scheduler_lateness_ms=scheduler_lateness_ms,
            inter_send_ms=inter_send_ms,
            deadline_misses=deadline_misses,
            send_errors=send_errors,
        ),
    }
    dump_json(Path(args.output), result)

    print(f"PHASE14_BG_SENDER_PACKETS_SENT={sent}")
    print(f"PHASE14_BG_SENDER_PACKETS_PLANNED={planned}")
    print(
        "PHASE14_BG_SENDER_ACTUAL_PAYLOAD_MBPS="
        f"{result['rate_payload_mbps_actual']:.6f}"
    )
    print(f"PHASE14_BG_SENDER_CPU_RATIO={result['cpu_ratio']:.6f}")
    print(f"PHASE14_BG_SENDER_OK={sent == planned and not send_errors}")
    return 0 if sent == planned and not send_errors else 1


def background_receiver(args: argparse.Namespace) -> int:
    """Receive the unicast background flow and preserve sequence evidence."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind((args.interface_ip, args.port))
    sock.settimeout(0.2)

    ready_file = Path(args.ready_file)
    stop_file = Path(args.stop_file)
    ready_file.unlink(missing_ok=True)
    stop_file.unlink(missing_ok=True)

    started_ns = time.monotonic_ns()
    ready_ns = time.monotonic_ns()
    dump_json(
        ready_file,
        {
            "interface_ip": args.interface_ip,
            "port": args.port,
            "ready_monotonic_ns": ready_ns,
        },
    )
    print("PHASE14_BG_RECEIVER_READY=True", flush=True)

    safety_deadline = time.monotonic() + float(args.max_runtime_s)
    stop_observed_ns: int | None = None
    termination_reason = "safety_timeout"
    unique: set[int] = set()
    delays_ms: list[float] = []
    duplicates = 0
    malformed = 0
    expected_total: int | None = None
    first_receive_ns: int | None = None
    last_receive_ns: int | None = None

    while True:
        if stop_file.exists():
            stop_observed_ns = time.monotonic_ns()
            termination_reason = "stop_signal"
            break
        if time.monotonic() >= safety_deadline:
            break

        try:
            data, _peer = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            termination_reason = "socket_error"
            break

        receive_ns = time.monotonic_ns()
        if len(data) < BACKGROUND_HEADER.size:
            malformed += 1
            continue

        magic, sequence, send_ns, packet_total = BACKGROUND_HEADER.unpack_from(data)
        if magic != BACKGROUND_MAGIC:
            malformed += 1
            continue
        if expected_total is None:
            expected_total = int(packet_total)
        elif expected_total != int(packet_total):
            malformed += 1
            continue
        if sequence in unique:
            duplicates += 1
            continue

        unique.add(int(sequence))
        delays_ms.append(max(0.0, (receive_ns - int(send_ns)) / 1_000_000.0))
        first_receive_ns = receive_ns if first_receive_ns is None else first_receive_ns
        last_receive_ns = receive_ns

    sock.close()
    completed_ns = time.monotonic_ns()

    result = {
        "role": "background_receiver",
        "interface_ip": args.interface_ip,
        "port": args.port,
        "expected_total_from_payload": expected_total,
        "unique_received": len(unique),
        "duplicates": duplicates,
        "malformed": malformed,
        "received_sequences": sorted(unique),
        "first_receive_monotonic_ns": first_receive_ns,
        "last_receive_monotonic_ns": last_receive_ns,
        "lifecycle": {
            "ready_file": str(ready_file),
            "stop_file": str(stop_file),
            "ready_monotonic_ns": ready_ns,
            "stop_observed_monotonic_ns": stop_observed_ns,
            "termination_reason": termination_reason,
            "runtime_s": (completed_ns - started_ns) / 1_000_000_000.0,
        },
        "one_way_delay_ms": {
            "min": min(delays_ms) if delays_ms else None,
            "mean": statistics.fmean(delays_ms) if delays_ms else None,
            "p50": percentile(delays_ms, 0.50),
            "p95": percentile(delays_ms, 0.95),
            "p99": percentile(delays_ms, 0.99),
            "max": max(delays_ms) if delays_ms else None,
            "n": len(delays_ms),
        },
    }
    dump_json(Path(args.output), result)

    print(f"PHASE14_BG_RECEIVER_UNIQUE={len(unique)}")
    print(f"PHASE14_BG_RECEIVER_MALFORMED={malformed}")
    print(f"PHASE14_BG_RECEIVER_TERMINATION_REASON={termination_reason}")
    return 0 if termination_reason != "socket_error" else 1


def tc_command(device: str, *arguments: str, check: bool = True) -> dict[str, Any]:
    """Execute one ``tc`` command against the shared receiver-B egress."""

    return command_record(["tc", *arguments, "dev", device], check=check)


def expected_tc_rate_accounting(args: argparse.Namespace) -> dict[str, Any]:
    """Translate payload rates into the byte domain observed by Linux ``tc``.

    The sender reports application payload throughput, while the egress qdisc
    accounts the complete packet presented by the kernel. The explicit overhead
    parameter prevents a payload-rate intent from being copied directly into an
    HTB class whose accounting domain is larger.
    """

    packet_size = int(args.packet_size)
    overhead_bytes = int(args.tc_overhead_bytes)

    if packet_size <= 0:
        fail("packet size must be greater than zero")
    if overhead_bytes < 0:
        fail("tc overhead bytes must not be negative")

    tc_packet_size = packet_size + overhead_bytes
    multicast_payload_rate = float(args.multicast_rate_mbps)
    background_payload_rate = float(args.background_rate_mbps)
    multicast_reserved_rate = float(args.multicast_reserved_mbps)

    multicast_tc_rate = (
        multicast_payload_rate * tc_packet_size / packet_size
    )
    background_tc_rate = (
        background_payload_rate * tc_packet_size / packet_size
    )
    reservation_margin = multicast_reserved_rate - multicast_tc_rate
    reservation_covers = reservation_margin >= -1e-9

    return {
        "payload_packet_size_bytes": packet_size,
        "tc_overhead_bytes": overhead_bytes,
        "tc_accounted_packet_size_bytes": tc_packet_size,
        "multicast_payload_rate_mbps": multicast_payload_rate,
        "background_payload_rate_mbps": background_payload_rate,
        "expected_multicast_tc_rate_mbps": multicast_tc_rate,
        "expected_background_tc_rate_mbps": background_tc_rate,
        "expected_aggregate_tc_rate_mbps": multicast_tc_rate + background_tc_rate,
        "configured_multicast_reserved_mbps": multicast_reserved_rate,
        "configured_reservation_margin_mbps": reservation_margin,
        "configured_multicast_reservation_covers_expected_load": reservation_covers,
        "accounting_domain": "linux_tc_egress_packet_bytes",
    }


def configure_bottleneck(args: argparse.Namespace) -> dict[str, Any]:
    """Create the baseline or differentiated HTB hierarchy."""

    device = args.bottleneck_device
    capacity = float(args.capacity_mbps)
    multicast_rate = float(args.multicast_reserved_mbps)
    background_rate = capacity - multicast_rate
    rate_accounting = expected_tc_rate_accounting(args)

    if capacity <= 0.0:
        fail("bottleneck capacity must be greater than zero")
    if not 0.0 < multicast_rate < capacity:
        fail("multicast reserved rate must be between zero and the capacity")
    if int(args.queue_limit_packets) <= 0:
        fail("queue limit must be greater than zero")
    if (
        args.mode == "adapt"
        and not rate_accounting[
            "configured_multicast_reservation_covers_expected_load"
        ]
    ):
        fail(
            "multicast HTB reservation does not cover the payload rate after "
            "translation into the Linux tc accounting domain"
        )

    limit_bytes = (
        int(args.queue_limit_packets)
        * int(rate_accounting["tc_accounted_packet_size_bytes"])
    )
    commands: list[dict[str, Any]] = []

    # A clean root guarantees that every run starts with empty class counters.
    commands.append(
        command_record(
            ["tc", "qdisc", "del", "dev", device, "root"],
            check=False,
        )
    )
    commands.append(
        command_record(
            [
                "tc", "qdisc", "add", "dev", device,
                "root", "handle", "1:", "htb", "default", "30", "r2q", "10",
            ],
            check=True,
        )
    )
    commands.append(
        command_record(
            [
                "tc", "class", "add", "dev", device,
                "parent", "1:", "classid", "1:1", "htb",
                "rate", f"{capacity:g}mbit", "ceil", f"{capacity:g}mbit",
                "quantum", "1514",
            ],
            check=True,
        )
    )

    if args.mode == "baseline":
        # Baseline places both flows in one FIFO class. Any protection observed
        # in adapt mode must therefore come from the explicit class separation.
        commands.append(
            command_record(
                [
                    "tc", "class", "add", "dev", device,
                    "parent", "1:1", "classid", "1:30", "htb",
                    "rate", f"{capacity:g}mbit", "ceil", f"{capacity:g}mbit",
                    "prio", "7", "quantum", "1514",
                ],
                check=True,
            )
        )
        commands.append(
            command_record(
                [
                    "tc", "qdisc", "add", "dev", device,
                    "parent", "1:30", "handle", "30:",
                    "bfifo", "limit", str(limit_bytes),
                ],
                check=True,
            )
        )
    else:
        # Adapt reserves the requested multicast rate and limits background
        # traffic to the remaining share. HTB priority only controls borrowing;
        # the rate values are the primary isolation mechanism.
        commands.append(
            command_record(
                [
                    "tc", "class", "add", "dev", device,
                    "parent", "1:1", "classid", "1:10", "htb",
                    "rate", f"{multicast_rate:g}mbit", "ceil", f"{capacity:g}mbit",
                    "prio", "0", "quantum", "1514",
                ],
                check=True,
            )
        )
        commands.append(
            command_record(
                [
                    "tc", "class", "add", "dev", device,
                    "parent", "1:1", "classid", "1:30", "htb",
                    "rate", f"{background_rate:g}mbit", "ceil", f"{capacity:g}mbit",
                    "prio", "7", "quantum", "1514",
                ],
                check=True,
            )
        )
        for classid, handle in (("1:10", "10:"), ("1:30", "30:")):
            commands.append(
                command_record(
                    [
                        "tc", "qdisc", "add", "dev", device,
                        "parent", classid, "handle", handle,
                        "bfifo", "limit", str(limit_bytes),
                    ],
                    check=True,
                )
            )

        commands.append(
            command_record(
                [
                    "tc", "filter", "add", "dev", device,
                    "protocol", "ip", "parent", "1:", "prio", "1", "u32",
                    "match", "ip", "protocol", "17", "0xff",
                    "match", "ip", "dst", f"{args.group}/32",
                    "match", "ip", "dport", str(args.multicast_port), "0xffff",
                    "flowid", "1:10",
                ],
                check=True,
            )
        )
        commands.append(
            command_record(
                [
                    "tc", "filter", "add", "dev", device,
                    "protocol", "ip", "parent", "1:", "prio", "2", "u32",
                    "match", "ip", "protocol", "17", "0xff",
                    "match", "ip", "dport", str(args.background_port), "0xffff",
                    "flowid", "1:30",
                ],
                check=True,
            )
        )

    readback = read_tc_state(device)
    class_text = readback["class_text"]["stdout"]
    filter_text = readback["filter_text"]["stdout"]
    hierarchy_ok = "1:1" in class_text and "1:30" in class_text
    if args.mode == "adapt":
        hierarchy_ok = hierarchy_ok and "1:10" in class_text
        hierarchy_ok = hierarchy_ok and "flowid 1:10" in filter_text
        hierarchy_ok = hierarchy_ok and "flowid 1:30" in filter_text

    return {
        "mode": args.mode,
        "device": device,
        "capacity_mbps": capacity,
        "multicast_reserved_mbps": multicast_rate if args.mode == "adapt" else None,
        "background_reserved_mbps": background_rate if args.mode == "adapt" else None,
        "queue_limit_packets": int(args.queue_limit_packets),
        "queue_limit_bytes": limit_bytes,
        "rate_accounting": rate_accounting,
        "commands": commands,
        "readback": readback,
        "hierarchy_ok": hierarchy_ok,
    }


def read_tc_state(device: str) -> dict[str, Any]:
    """Capture text and JSON views of qdiscs, classes, and filters."""

    state: dict[str, Any] = {}
    for label, noun in (("qdisc", "qdisc"), ("class", "class"), ("filter", "filter")):
        text_record = command_record(
            ["tc", "-s", noun, "show", "dev", device],
            check=False,
        )
        json_record = command_record(
            ["tc", "-j", "-s", noun, "show", "dev", device],
            check=False,
        )
        state[f"{label}_text"] = text_record
        state[f"{label}_json"] = json_record
    return state


def recursive_number(value: Any, names: set[str]) -> int:
    """Find and sum numeric counter fields in a possibly nested tc JSON item."""

    if isinstance(value, dict):
        total = 0
        for key, item in value.items():
            if key in names and isinstance(item, (int, float)):
                total += int(item)
            elif isinstance(item, (dict, list)):
                total += recursive_number(item, names)
        return total
    if isinstance(value, list):
        return sum(recursive_number(item, names) for item in value)
    return 0


def parse_class_counters(state: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Extract class counters from JSON with a text fallback for older tc."""

    counters: dict[str, dict[str, int]] = {}
    json_stdout = str((state.get("class_json") or {}).get("stdout") or "")
    try:
        payload = json.loads(json_stdout)
    except json.JSONDecodeError:
        payload = []

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            classid = str(item.get("classid") or "")
            if not classid:
                continue
            counters[classid] = {
                "bytes": recursive_number(item.get("stats") or {}, {"bytes"}),
                "packets": recursive_number(item.get("stats") or {}, {"packets", "packets64"}),
                "drops": recursive_number(item.get("stats") or {}, {"drops", "drop_overlimit"}),
                "overlimits": recursive_number(item.get("stats") or {}, {"overlimits"}),
            }

    if counters:
        return counters

    # Older iproute2 versions may not expose class statistics in JSON. Parse the
    # stable two-line text form: "class ... 1:10" followed by "Sent ...".
    current_class: str | None = None
    text_stdout = str((state.get("class_text") or {}).get("stdout") or "")
    for line in text_stdout.splitlines():
        class_match = re.search(r"\bclass\s+\S+\s+(\S+)", line)
        if class_match:
            current_class = class_match.group(1)
            counters.setdefault(
                current_class,
                {"bytes": 0, "packets": 0, "drops": 0, "overlimits": 0},
            )
            continue
        sent_match = re.search(
            r"Sent\s+(\d+)\s+bytes\s+(\d+)\s+pkt\s+\(dropped\s+(\d+),\s+overlimits\s+(\d+)",
            line,
        )
        if current_class and sent_match:
            counters[current_class] = {
                "bytes": int(sent_match.group(1)),
                "packets": int(sent_match.group(2)),
                "drops": int(sent_match.group(3)),
                "overlimits": int(sent_match.group(4)),
            }
    return counters


def observed_tc_rate_accounting(
    *,
    mode: str,
    class_counters: dict[str, dict[str, int]],
    multicast_sender: dict[str, Any],
    background_sender: dict[str, Any],
    configuration: dict[str, Any],
) -> dict[str, Any]:
    """Derive the actual qdisc accounting rates from archived class counters."""

    packet_size = int(multicast_sender.get("packet_size_bytes") or 0)
    multicast_payload_rate = float(
        multicast_sender.get("rate_payload_mbps_actual") or 0.0
    )
    background_payload_rate = float(
        background_sender.get("rate_payload_mbps_actual") or 0.0
    )

    def class_measurement(classid: str, payload_rate: float) -> dict[str, Any]:
        counter = class_counters.get(classid) or {}
        packets = int(counter.get("packets") or 0)
        byte_count = int(counter.get("bytes") or 0)
        bytes_per_packet = byte_count / packets if packets > 0 else 0.0
        required_tc_rate = (
            payload_rate * bytes_per_packet / packet_size
            if packet_size > 0 and bytes_per_packet > 0.0
            else 0.0
        )
        return {
            "classid": classid,
            "packets": packets,
            "bytes": byte_count,
            "bytes_per_packet": bytes_per_packet,
            "required_tc_rate_mbps": required_tc_rate,
            "drops": int(counter.get("drops") or 0),
            "overlimits": int(counter.get("overlimits") or 0),
        }

    if mode == "adapt":
        multicast = class_measurement("1:10", multicast_payload_rate)
        background = class_measurement("1:30", background_payload_rate)
        reserved = float(configuration.get("multicast_reserved_mbps") or 0.0)
        margin = reserved - float(multicast["required_tc_rate_mbps"])
        covers = (
            multicast["packets"] > 0
            and multicast["bytes"] > 0
            and margin >= -1e-9
        )
    else:
        multicast = None
        background = None
        reserved = None
        margin = None
        covers = None

    return {
        "mode": mode,
        "multicast_class": multicast,
        "background_class": background,
        "configured_multicast_reserved_mbps": reserved,
        "observed_multicast_reservation_margin_mbps": margin,
        "observed_multicast_reservation_covers_load": covers,
    }


def wait_for_file(path: Path, processes: Sequence[subprocess.Popen[str]], timeout_s: float) -> bool:
    """Wait for one readiness artifact while aborting on early worker exit."""

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        if any(process.poll() is not None for process in processes):
            return False
        time.sleep(0.05)
    return path.is_file()


def read_json(path: Path) -> dict[str, Any]:
    """Read one required JSON artifact with a useful failure message."""

    if not path.is_file():
        fail(f"required JSON artifact is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def orchestrate(args: argparse.Namespace) -> int:
    """Run one baseline or adapt contention experiment."""

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    control_dir = output_dir / "control"
    control_dir.mkdir(parents=True, exist_ok=True)

    if args.mode not in {"baseline", "adapt"}:
        fail("mode must be baseline or adapt")

    # The two paced senders must not compete for the same virtual CPU. This
    # preserves the Phase 13 source profile while keeping background generation
    # independent from the multicast source process.
    multicast_cpu = resolve_cpu(args.multicast_sender_cpu, role="multicast sender")
    background_cpu = resolve_cpu(
        args.background_sender_cpu,
        role="background sender",
        reserved=(multicast_cpu,),
    )

    h3_mac_record = command_record(
        [
            "ip", "netns", "exec", args.background_receiver_namespace,
            "cat", f"/sys/class/net/{args.background_receiver_device}/address",
        ],
        check=True,
    )
    h3_mac = h3_mac_record["stdout"].strip()
    if not re.fullmatch(r"[0-9a-fA-F:]{17}", h3_mac):
        fail(f"invalid receiver-B MAC address: {h3_mac!r}")

    neighbor_record = command_record(
        [
            "ip", "-n", args.background_source_namespace,
            "neigh", "replace", args.background_receiver_ip,
            "lladdr", h3_mac, "nud", "permanent",
            "dev", args.background_source_device,
        ],
        check=True,
    )

    tc_configuration = configure_bottleneck(args)
    if not tc_configuration["hierarchy_ok"]:
        fail("tc readback does not match the requested hierarchy")

    bg_ready = control_dir / "background.ready.json"
    bg_stop = control_dir / "background.stop"
    bg_receiver_output = output_dir / "background_receiver.json"
    bg_sender_output = output_dir / "background_sender.json"
    bg_ready.unlink(missing_ok=True)
    bg_stop.unlink(missing_ok=True)

    script = Path(__file__).resolve()
    python = sys.executable
    receiver_command = [
        "ip", "netns", "exec", args.background_receiver_namespace,
        python, str(script), "background-receiver",
        "--interface-ip", args.background_receiver_ip,
        "--port", str(args.background_port),
        "--max-runtime-s", str(args.worker_max_runtime_s),
        "--ready-file", str(bg_ready),
        "--stop-file", str(bg_stop),
        "--output", str(bg_receiver_output),
    ]
    bg_receiver_process = subprocess.Popen(
        receiver_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    bg_ready_ok = wait_for_file(
        bg_ready,
        [bg_receiver_process],
        timeout_s=float(args.worker_ready_timeout_s),
    )
    print(f"PHASE14_BG_RECEIVER_READINESS_OK={bg_ready_ok}")

    background_command = [
        "ip", "netns", "exec", args.background_source_namespace,
        "taskset", "-c", str(background_cpu),
        python, str(script), "background-sender",
        "--source-ip", args.background_source_ip,
        "--destination-ip", args.background_receiver_ip,
        "--port", str(args.background_port),
        "--duration", str(args.background_duration),
        "--rate-mbps", str(args.background_rate_mbps),
        "--packet-size", str(args.packet_size),
        "--spin-threshold-us", str(args.spin_threshold_us),
        "--output", str(bg_sender_output),
    ]

    if bg_ready_ok:
        bg_sender_process = subprocess.Popen(
            background_command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    else:
        bg_sender_process = None

    time.sleep(float(args.background_prefill_s))

    multicast_output_dir = output_dir / "multicast"
    multicast_command = [
        python,
        str(REPO_ROOT / "scripts" / "s2_multicast_dataplane_smoke.py"),
        "orchestrate",
        "--group", args.group,
        "--port", str(args.multicast_port),
        "--source-namespace", args.multicast_source_namespace,
        "--source-ip", args.multicast_source_ip,
        "--receiver", f"B:{args.background_receiver_namespace}:{args.background_receiver_ip}",
        "--receiver", f"C:{args.control_receiver_namespace}:{args.control_receiver_ip}",
        "--duration", str(args.duration),
        "--rate-mbps", str(args.multicast_rate_mbps),
        "--packet-size", str(args.packet_size),
        "--pacing-mode", "repeated_sleep_spin",
        "--spin-threshold-us", str(args.spin_threshold_us),
        "--sender-profile-id", args.sender_profile_id,
        "--sender-cpu", str(multicast_cpu),
        "--max-abs-rate-error-pct", str(args.max_abs_rate_error_pct),
        "--min-inter-send-ratio", str(args.min_inter_send_ratio),
        "--require-rate-validation",
        # The foundation must observe natural baseline loss rather than turn it
        # into an operational harness failure. Scientific gates are calculated
        # only after both baseline and adapt runs have been collected.
        "--min-delivery", "0.0",
        "--receiver-ready-timeout-s", str(args.worker_ready_timeout_s),
        "--receiver-drain-s", str(args.multicast_receiver_drain_s),
        "--receiver-max-runtime-s", str(args.worker_max_runtime_s),
        "--receiver-stop-timeout-s", str(args.worker_stop_timeout_s),
        "--output-dir", str(multicast_output_dir),
    ]

    if bg_ready_ok and bg_sender_process is not None:
        multicast_completed = subprocess.run(
            multicast_command,
            text=True,
            capture_output=True,
            check=False,
        )
    else:
        multicast_completed = subprocess.CompletedProcess(
            multicast_command,
            returncode=1,
            stdout="",
            stderr="background readiness failed",
        )

    if multicast_completed.stdout:
        print(multicast_completed.stdout, end="")
    if multicast_completed.stderr:
        print(multicast_completed.stderr, end="", file=sys.stderr)

    if bg_sender_process is not None:
        try:
            bg_sender_stdout, bg_sender_stderr = bg_sender_process.communicate(
                timeout=float(args.worker_max_runtime_s)
            )
        except subprocess.TimeoutExpired:
            bg_sender_process.terminate()
            bg_sender_stdout, bg_sender_stderr = bg_sender_process.communicate(timeout=2.0)
    else:
        bg_sender_stdout = ""
        bg_sender_stderr = "background sender was not started"

    if bg_sender_stdout:
        print(bg_sender_stdout, end="")
    if bg_sender_stderr:
        print(bg_sender_stderr, end="", file=sys.stderr)

    time.sleep(float(args.background_drain_s))
    bg_stop.touch()

    try:
        bg_receiver_stdout, bg_receiver_stderr = bg_receiver_process.communicate(
            timeout=float(args.worker_stop_timeout_s)
        )
    except subprocess.TimeoutExpired:
        bg_receiver_process.terminate()
        try:
            bg_receiver_stdout, bg_receiver_stderr = bg_receiver_process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            bg_receiver_process.kill()
            bg_receiver_stdout, bg_receiver_stderr = bg_receiver_process.communicate()

    if bg_receiver_stdout:
        print(bg_receiver_stdout, end="")
    if bg_receiver_stderr:
        print(bg_receiver_stderr, end="", file=sys.stderr)

    tc_after = read_tc_state(args.bottleneck_device)
    class_counters = parse_class_counters(tc_after)

    multicast_summary = read_json(multicast_output_dir / "summary.json")
    background_sender_data = read_json(bg_sender_output)
    background_receiver_data = read_json(bg_receiver_output)

    mcast_sender = multicast_summary.get("sender") or {}
    mcast_rate_validation = multicast_summary.get("rate_validation") or {}
    receivers = multicast_summary.get("receivers") or {}
    receiver_b = receivers.get("B") or {}
    receiver_c = receivers.get("C") or {}

    bg_planned = int(background_sender_data.get("packets_planned") or 0)
    bg_sent = int(background_sender_data.get("packets_sent") or 0)
    bg_unique = int(background_receiver_data.get("unique_received") or 0)
    bg_delivery_ratio = (bg_unique / bg_planned) if bg_planned > 0 else 0.0
    bg_pacing = background_sender_data.get("pacing") or {}
    bg_inter_send = bg_pacing.get("inter_send_ms") or {}
    bg_interval_ms = float(bg_pacing.get("interval_ns") or 0) / 1_000_000.0
    bg_min_inter_send_ratio = (
        float(bg_inter_send.get("min") or 0.0) / bg_interval_ms
        if bg_interval_ms > 0.0
        else 0.0
    )
    bg_rate_validated = (
        bg_sent == bg_planned
        and bg_planned > 0
        and abs(float(background_sender_data.get("rate_error_pct") or 0.0))
        <= float(args.max_abs_rate_error_pct)
        and bg_min_inter_send_ratio >= float(args.min_inter_send_ratio)
    )

    observed_rate_accounting = observed_tc_rate_accounting(
        mode=args.mode,
        class_counters=class_counters,
        multicast_sender=mcast_sender,
        background_sender=background_sender_data,
        configuration=tc_configuration,
    )
    configured_rate_accounting = tc_configuration.get("rate_accounting") or {}
    configured_accounting_ok = (
        args.mode == "baseline"
        or configured_rate_accounting.get(
            "configured_multicast_reservation_covers_expected_load"
        )
        is True
    )
    observed_accounting_ok = (
        args.mode == "baseline"
        or observed_rate_accounting.get(
            "observed_multicast_reservation_covers_load"
        )
        is True
    )

    expected_classes = {"1:30"} if args.mode == "baseline" else {"1:10", "1:30"}
    class_activity_ok = all(
        int((class_counters.get(classid) or {}).get("packets") or 0) > 0
        for classid in expected_classes
    )

    receiver_lifecycle_ok = all(
        ((receiver.get("lifecycle") or {}).get("termination_reason") == "stop_signal")
        for receiver in (receiver_b, receiver_c)
    )
    receiver_integrity_ok = all(
        int(receiver.get("malformed") or 0) == 0
        for receiver in (receiver_b, receiver_c)
    )
    background_receiver_ok = (
        bg_receiver_process.returncode == 0
        and int(background_receiver_data.get("malformed") or 0) == 0
        and (background_receiver_data.get("lifecycle") or {}).get("termination_reason")
        == "stop_signal"
        and bg_unique > 0
    )
    background_sender_returncode = (
        bg_sender_process.returncode if bg_sender_process is not None else 1
    )

    operational_ok = all(
        (
            bg_ready_ok,
            tc_configuration["hierarchy_ok"],
            multicast_completed.returncode == 0,
            multicast_summary.get("passed") is True,
            mcast_rate_validation.get("validated") is True,
            int(mcast_sender.get("packets_sent") or 0)
            == int(mcast_sender.get("packets_planned") or 0),
            background_sender_returncode == 0,
            bg_rate_validated,
            background_receiver_ok,
            receiver_lifecycle_ok,
            receiver_integrity_ok,
            class_activity_ok,
            configured_accounting_ok,
            observed_accounting_ok,
        )
    )

    summary = {
        "scenario": "S2_P4_multicast_qos_contention_foundation",
        "mode": args.mode,
        "scope": {
            "multicast_control_plane_programmed": True,
            "unicast_background_control_plane_programmed": True,
            "multicast_dataplane_exercised": True,
            "shared_receiver_b_egress_exercised": class_activity_ok,
            "linux_tc_egress_qos_exercised": True,
            "linux_tc_qos_differentiation_applied": args.mode == "adapt",
            "p4_internal_queueing_exercised": False,
            "requested_multicast_rate_validated": bool(
                mcast_rate_validation.get("validated")
            ),
            "requested_background_rate_validated": bg_rate_validated,
            "tc_rate_accounting_model_applied": True,
            "tc_multicast_reservation_exercised": args.mode == "adapt",
            "tc_multicast_reservation_covers_observed_load": (
                observed_rate_accounting.get(
                    "observed_multicast_reservation_covers_load"
                )
                if args.mode == "adapt"
                else None
            ),
            "sustained_qos_isolation_validated": False,
            "baseline_adapt_comparison_validated": False,
            "recovery_metrics_exercised": False,
        },
        "topology": {
            "multicast_source": {
                "namespace": args.multicast_source_namespace,
                "ip": args.multicast_source_ip,
                "p4_ingress_port": 0,
            },
            "background_source": {
                "namespace": args.background_source_namespace,
                "ip": args.background_source_ip,
                "p4_ingress_port": 3,
                "static_neighbor_mac": h3_mac,
            },
            "receiver_b": {
                "namespace": args.background_receiver_namespace,
                "ip": args.background_receiver_ip,
                "p4_egress_port": 1,
                "shared_egress_device": args.bottleneck_device,
            },
            "receiver_c": {
                "namespace": args.control_receiver_namespace,
                "ip": args.control_receiver_ip,
                "p4_egress_port": 2,
                "control_path": True,
            },
        },
        "traffic": {
            "multicast": {
                "group": args.group,
                "port": args.multicast_port,
                "duration_s": args.duration,
                "rate_mbps": args.multicast_rate_mbps,
                "sender_cpu": multicast_cpu,
                "summary": multicast_summary,
            },
            "background": {
                "destination_ip": args.background_receiver_ip,
                "port": args.background_port,
                "duration_s": args.background_duration,
                "rate_mbps": args.background_rate_mbps,
                "sender_cpu": background_cpu,
                "sender": background_sender_data,
                "receiver": background_receiver_data,
                "delivery_ratio": bg_delivery_ratio,
                "minimum_inter_send_ratio": bg_min_inter_send_ratio,
                "rate_validated": bg_rate_validated,
            },
        },
        "bottleneck": {
            "configuration": tc_configuration,
            "state_after": tc_after,
            "class_counters": class_counters,
            "rate_accounting": {
                "configured": configured_rate_accounting,
                "observed": observed_rate_accounting,
            },
            "expected_active_classes": sorted(expected_classes),
            "class_activity_ok": class_activity_ok,
        },
        "worker_logs": {
            "multicast": {
                "command": multicast_command,
                "returncode": multicast_completed.returncode,
                "stdout": multicast_completed.stdout,
                "stderr": multicast_completed.stderr,
            },
            "background_sender": {
                "command": background_command,
                "returncode": background_sender_returncode,
                "stdout": bg_sender_stdout,
                "stderr": bg_sender_stderr,
            },
            "background_receiver": {
                "command": receiver_command,
                "returncode": bg_receiver_process.returncode,
                "stdout": bg_receiver_stdout,
                "stderr": bg_receiver_stderr,
            },
            "neighbor": neighbor_record,
        },
        "operational_checks": {
            "background_readiness_ok": bg_ready_ok,
            "tc_hierarchy_ok": tc_configuration["hierarchy_ok"],
            "multicast_harness_ok": multicast_completed.returncode == 0,
            "multicast_rate_ok": mcast_rate_validation.get("validated") is True,
            "background_rate_ok": bg_rate_validated,
            "background_receiver_ok": background_receiver_ok,
            "receiver_lifecycle_ok": receiver_lifecycle_ok,
            "receiver_integrity_ok": receiver_integrity_ok,
            "shared_class_activity_ok": class_activity_ok,
            "configured_rate_accounting_ok": configured_accounting_ok,
            "observed_rate_accounting_ok": observed_accounting_ok,
        },
        "passed": bool(operational_ok),
    }
    summary_path = output_dir / "summary.json"
    dump_json(summary_path, summary)

    receiver_b_ratio = float(receiver_b.get("delivery_ratio") or 0.0)
    receiver_c_ratio = float(receiver_c.get("delivery_ratio") or 0.0)
    receiver_b_delay = receiver_b.get("one_way_delay_ms") or {}
    receiver_c_delay = receiver_c.get("one_way_delay_ms") or {}

    print(f"PHASE14_CONTENTION_MODE={args.mode}")
    print(f"PHASE14_CONTENTION_MULTICAST_CPU={multicast_cpu}")
    print(f"PHASE14_CONTENTION_BACKGROUND_CPU={background_cpu}")
    print(f"PHASE14_CONTENTION_RECEIVER_B_RATIO={receiver_b_ratio:.6f}")
    print(f"PHASE14_CONTENTION_RECEIVER_C_RATIO={receiver_c_ratio:.6f}")
    print(
        "PHASE14_CONTENTION_RECEIVER_B_P95_MS="
        f"{float(receiver_b_delay.get('p95') or 0.0):.6f}"
    )
    print(
        "PHASE14_CONTENTION_RECEIVER_C_P95_MS="
        f"{float(receiver_c_delay.get('p95') or 0.0):.6f}"
    )
    print(f"PHASE14_CONTENTION_BACKGROUND_DELIVERY_RATIO={bg_delivery_ratio:.6f}")
    print(f"PHASE14_CONTENTION_CLASS_ACTIVITY_OK={class_activity_ok}")
    print(
        "PHASE14_CONTENTION_CONFIGURED_RATE_ACCOUNTING_OK="
        f"{configured_accounting_ok}"
    )
    print(
        "PHASE14_CONTENTION_OBSERVED_RATE_ACCOUNTING_OK="
        f"{observed_accounting_ok}"
    )
    if args.mode == "adapt":
        observed_multicast = (
            observed_rate_accounting.get("multicast_class") or {}
        )
        print(
            "PHASE14_CONTENTION_OBSERVED_MCAST_TC_BYTES_PER_PACKET="
            f"{float(observed_multicast.get('bytes_per_packet') or 0.0):.6f}"
        )
        print(
            "PHASE14_CONTENTION_OBSERVED_MCAST_TC_RATE_REQUIRED_MBPS="
            f"{float(observed_multicast.get('required_tc_rate_mbps') or 0.0):.9f}"
        )
        print(
            "PHASE14_CONTENTION_MCAST_RESERVATION_MARGIN_MBPS="
            f"{float(observed_rate_accounting.get('observed_multicast_reservation_margin_mbps') or 0.0):.9f}"
        )
    print("PHASE14_CONTENTION_P4_INTERNAL_QUEUEING_EXERCISED=False")
    print("PHASE14_CONTENTION_LINUX_TC_EGRESS_QOS_EXERCISED=True")
    print(f"PHASE14_CONTENTION_SUMMARY={summary_path}")

    if operational_ok:
        print("PHASE14_S2_P4_QOS_CONTENTION_RUN_OK")
        return 0

    print("PHASE14_S2_P4_QOS_CONTENTION_RUN_FAILED")
    return 1


def build_parser() -> argparse.ArgumentParser:
    """Build worker and orchestrator subcommands."""

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    send = subparsers.add_parser("background-sender")
    send.add_argument("--source-ip", required=True)
    send.add_argument("--destination-ip", required=True)
    send.add_argument("--port", type=int, required=True)
    send.add_argument("--duration", type=float, required=True)
    send.add_argument("--rate-mbps", type=float, required=True)
    send.add_argument("--packet-size", type=int, default=1200)
    send.add_argument("--spin-threshold-us", type=float, default=900.0)
    send.add_argument("--output", required=True)

    receive = subparsers.add_parser("background-receiver")
    receive.add_argument("--interface-ip", required=True)
    receive.add_argument("--port", type=int, required=True)
    receive.add_argument("--max-runtime-s", type=float, default=120.0)
    receive.add_argument("--ready-file", required=True)
    receive.add_argument("--stop-file", required=True)
    receive.add_argument("--output", required=True)

    orchestrator = subparsers.add_parser("orchestrate")
    orchestrator.add_argument("--mode", choices=("baseline", "adapt"), required=True)
    orchestrator.add_argument("--output-dir", required=True)
    orchestrator.add_argument("--bottleneck-device", default="s2b-h3")
    orchestrator.add_argument("--capacity-mbps", type=float, default=3.0)
    orchestrator.add_argument("--multicast-reserved-mbps", type=float, default=2.1)
    orchestrator.add_argument("--queue-limit-packets", type=int, default=64)
    orchestrator.add_argument("--group", default="239.1.1.1")
    orchestrator.add_argument("--multicast-port", type=int, default=5001)
    orchestrator.add_argument("--background-port", type=int, default=6001)
    orchestrator.add_argument("--duration", type=float, default=3.0)
    orchestrator.add_argument("--background-duration", type=float, default=7.0)
    orchestrator.add_argument("--background-prefill-s", type=float, default=0.5)
    orchestrator.add_argument("--background-drain-s", type=float, default=0.5)
    orchestrator.add_argument("--multicast-receiver-drain-s", type=float, default=1.0)
    orchestrator.add_argument("--multicast-rate-mbps", type=float, default=2.0)
    orchestrator.add_argument("--background-rate-mbps", type=float, default=2.0)
    orchestrator.add_argument("--packet-size", type=int, default=1200)
    orchestrator.add_argument("--tc-overhead-bytes", type=int, default=42)
    orchestrator.add_argument("--spin-threshold-us", type=float, default=900.0)
    orchestrator.add_argument(
        "--sender-profile-id",
        default="phase13-tailspin-900us-affinity-v1",
    )
    orchestrator.add_argument("--multicast-sender-cpu", default="auto")
    orchestrator.add_argument("--background-sender-cpu", default="auto-distinct")
    orchestrator.add_argument("--max-abs-rate-error-pct", type=float, default=5.0)
    orchestrator.add_argument("--min-inter-send-ratio", type=float, default=0.98)
    orchestrator.add_argument("--worker-ready-timeout-s", type=float, default=10.0)
    orchestrator.add_argument("--worker-stop-timeout-s", type=float, default=5.0)
    orchestrator.add_argument("--worker-max-runtime-s", type=float, default=120.0)
    orchestrator.add_argument("--multicast-source-namespace", default="h1")
    orchestrator.add_argument("--multicast-source-ip", default="10.0.0.1")
    orchestrator.add_argument("--background-source-namespace", default="h2")
    orchestrator.add_argument("--background-source-ip", default="10.0.0.2")
    orchestrator.add_argument("--background-source-device", default="h2-eth0")
    orchestrator.add_argument("--background-receiver-namespace", default="h3")
    orchestrator.add_argument("--background-receiver-ip", default="10.0.0.3")
    orchestrator.add_argument("--background-receiver-device", default="h3-eth0")
    orchestrator.add_argument("--control-receiver-namespace", default="h4")
    orchestrator.add_argument("--control-receiver-ip", default="10.0.0.4")
    return parser


def main() -> int:
    """Dispatch the requested worker or complete experiment."""

    args = build_parser().parse_args()
    if args.command == "background-sender":
        return background_sender(args)
    if args.command == "background-receiver":
        return background_receiver(args)
    return orchestrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
