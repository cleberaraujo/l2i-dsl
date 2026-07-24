#!/usr/bin/env python3
"""Exercise and validate S2 multicast replication through the BMv2 dataplane.

The source sends timestamped UDP datagrams to an IPv4 multicast group through
BMv2 port 0. Receivers B and C, attached to ports 1 and 2, join the group and
write independent evidence files. Because all namespaces share the same Linux
kernel, ``time.monotonic_ns()`` provides a common clock for one-way delay in
this emulated testbed.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MAGIC = b"L2I2"
HEADER = struct.Struct("!4sIQQ")


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    lower = int(math.floor(index))
    upper = int(math.ceil(index))
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sender(args: argparse.Namespace) -> int:
    payload_bytes = int(args.packet_size)
    if payload_bytes < HEADER.size:
        raise SystemExit(f"packet size must be at least {HEADER.size} bytes")
    if payload_bytes > 1400:
        raise SystemExit("packet size must not exceed 1400 bytes in this smoke test")
    if args.rate_mbps <= 0 or args.duration <= 0:
        raise SystemExit("duration and rate must be greater than zero")

    packets_per_second = (float(args.rate_mbps) * 1_000_000.0) / (8.0 * payload_bytes)
    total_packets = max(1, int(round(float(args.duration) * packets_per_second)))
    interval_ns = max(1, int(1_000_000_000.0 / packets_per_second))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(args.source_ip))
    sock.bind((args.source_ip, 0))

    filler = b"X" * (payload_bytes - HEADER.size)
    start_ns = time.monotonic_ns()
    next_deadline_ns = start_ns
    previous_send_ns: int | None = None
    sent = 0
    send_errors: list[str] = []
    scheduler_lateness_ms: list[float] = []
    inter_send_ms: list[float] = []
    deadline_misses = 0

    for seq in range(total_packets):
        while True:
            remaining = next_deadline_ns - time.monotonic_ns()
            if remaining <= 0:
                break
            time.sleep(min(remaining / 1_000_000_000.0, 0.002))

        send_ns = time.monotonic_ns()
        lateness_ns = max(0, send_ns - next_deadline_ns)
        scheduler_lateness_ms.append(lateness_ns / 1_000_000.0)

        if lateness_ns >= interval_ns:
            deadline_misses += 1

        if previous_send_ns is not None:
            inter_send_ms.append((send_ns - previous_send_ns) / 1_000_000.0)

        datagram = HEADER.pack(MAGIC, seq, send_ns, total_packets) + filler
        try:
            sock.sendto(datagram, (args.group, args.port))
            sent += 1
        except OSError as exc:
            send_errors.append(f"seq={seq}: {exc}")

        previous_send_ns = send_ns

        # Never compensate a scheduler pause by transmitting overdue datagrams
        # back-to-back. The requested rate is treated as an upper bound and the
        # next packet is scheduled at least one full interval after this send.
        next_deadline_ns = send_ns + interval_ns

    end_ns = time.monotonic_ns()
    sock.close()
    elapsed_s = (end_ns - start_ns) / 1_000_000_000.0
    actual_payload_mbps = (
        (sent * payload_bytes * 8.0) / elapsed_s / 1_000_000.0
        if elapsed_s > 0
        else 0.0
    )

    result = {
        "role": "sender",
        "group": args.group,
        "port": args.port,
        "source_ip": args.source_ip,
        "duration_requested_s": args.duration,
        "rate_payload_mbps_requested": args.rate_mbps,
        "packet_size_bytes": payload_bytes,
        "packets_planned": total_packets,
        "packets_sent": sent,
        "send_errors": send_errors,
        "started_monotonic_ns": start_ns,
        "completed_monotonic_ns": end_ns,
        "elapsed_s": elapsed_s,
        "rate_payload_mbps_actual": actual_payload_mbps,
        "pacing": {
            "mode": "minimum_interval_no_catchup",
            "interval_ns": interval_ns,
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
    dump_json(Path(args.output), result)
    print(f"S2_DP_SENDER_PACKETS_SENT={sent}")
    print(f"S2_DP_SENDER_PACKETS_PLANNED={total_packets}")
    print("S2_DP_SENDER_PACING_MODE=minimum_interval_no_catchup")
    print(f"S2_DP_SENDER_DEADLINE_MISSES={deadline_misses}")
    print(f"S2_DP_SENDER_ACTUAL_PAYLOAD_MBPS={actual_payload_mbps:.6f}")
    print(
        "S2_DP_SENDER_MAX_SCHEDULER_LATENESS_MS="
        f"{max(scheduler_lateness_ms) if scheduler_lateness_ms else 0.0:.6f}"
    )
    print(
        "S2_DP_SENDER_MIN_INTER_SEND_MS="
        f"{min(inter_send_ms) if inter_send_ms else 0.0:.6f}"
    )
    print(f"S2_DP_SENDER_OK={sent == total_packets and not send_errors}")
    return 0 if sent == total_packets and not send_errors else 1


def receiver(args: argparse.Namespace) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(("", args.port))
    membership = socket.inet_aton(args.group) + socket.inet_aton(args.interface_ip)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    sock.settimeout(0.2)

    ready_file = Path(args.ready_file) if args.ready_file else None
    stop_file = Path(args.stop_file) if args.stop_file else None
    started_ns = time.monotonic_ns()
    ready_ns = time.monotonic_ns()

    if ready_file is not None:
        dump_json(
            ready_file,
            {
                "label": args.label,
                "group": args.group,
                "port": args.port,
                "interface_ip": args.interface_ip,
                "ready_monotonic_ns": ready_ns,
            },
        )
        print(f"S2_DP_RECEIVER_{args.label}_READY=True", flush=True)

    safety_deadline = time.monotonic() + float(args.duration)
    first_receive_ns: int | None = None
    last_receive_ns: int | None = None
    stop_observed_ns: int | None = None
    termination_reason = "safety_timeout"
    sequences: list[int] = []
    unique: set[int] = set()
    delays_ms: list[float] = []
    duplicates = 0
    malformed = 0
    expected_total: int | None = None

    while True:
        if stop_file is not None and stop_file.exists():
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
        if len(data) < HEADER.size:
            malformed += 1
            continue

        magic, seq, send_ns, packet_total = HEADER.unpack_from(data)
        if magic != MAGIC:
            malformed += 1
            continue

        if expected_total is None:
            expected_total = int(packet_total)
        elif expected_total != int(packet_total):
            malformed += 1
            continue

        if seq in unique:
            duplicates += 1
            continue

        unique.add(seq)
        sequences.append(int(seq))
        delay = max(0.0, (receive_ns - int(send_ns)) / 1_000_000.0)
        delays_ms.append(delay)
        first_receive_ns = receive_ns if first_receive_ns is None else first_receive_ns
        last_receive_ns = receive_ns

    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, membership)
    except OSError:
        pass
    sock.close()
    completed_ns = time.monotonic_ns()

    result = {
        "role": "receiver",
        "label": args.label,
        "group": args.group,
        "port": args.port,
        "interface_ip": args.interface_ip,
        "duration_s": args.duration,
        "lifecycle": {
            "mode": "explicit_ready_and_stop" if stop_file is not None else "fixed_duration",
            "ready_file": str(ready_file) if ready_file is not None else None,
            "stop_file": str(stop_file) if stop_file is not None else None,
            "ready_monotonic_ns": ready_ns,
            "stop_observed_monotonic_ns": stop_observed_ns,
            "termination_reason": termination_reason,
            "runtime_s": (completed_ns - started_ns) / 1_000_000_000.0,
        },
        "expected_total_from_payload": expected_total,
        "unique_received": len(unique),
        "duplicates": duplicates,
        "malformed": malformed,
        "received_sequences": sorted(sequences),
        "first_receive_monotonic_ns": first_receive_ns,
        "last_receive_monotonic_ns": last_receive_ns,
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
    print(f"S2_DP_RECEIVER_{args.label}_UNIQUE={len(unique)}")
    print(f"S2_DP_RECEIVER_{args.label}_DUPLICATES={duplicates}")
    print(f"S2_DP_RECEIVER_{args.label}_MALFORMED={malformed}")
    print(f"S2_DP_RECEIVER_{args.label}_TERMINATION_REASON={termination_reason}")
    return 0 if termination_reason != "socket_error" else 1


def parse_receiver(value: str) -> tuple[str, str, str]:
    parts = value.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError("receiver must use LABEL:NAMESPACE:INTERFACE_IP")
    return parts[0], parts[1], parts[2]


def orchestrate(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    control_dir = output_dir / "control"
    control_dir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    python = sys.executable

    receiver_processes: list[tuple[str, Path, Path, Path, subprocess.Popen[str]]] = []

    for label, namespace, interface_ip in args.receiver:
        output = output_dir / f"receiver_{label}.json"
        ready_file = control_dir / f"receiver_{label}.ready.json"
        stop_file = control_dir / f"receiver_{label}.stop"
        ready_file.unlink(missing_ok=True)
        stop_file.unlink(missing_ok=True)
        command = [
            "ip", "netns", "exec", namespace,
            python, str(script), "receiver",
            "--label", label,
            "--group", args.group,
            "--port", str(args.port),
            "--interface-ip", interface_ip,
            "--duration", str(args.receiver_max_runtime_s),
            "--ready-file", str(ready_file),
            "--stop-file", str(stop_file),
            "--output", str(output),
        ]
        process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        receiver_processes.append((label, output, ready_file, stop_file, process))

    readiness_started = time.monotonic()
    readiness_deadline = readiness_started + float(args.receiver_ready_timeout_s)
    readiness_ok = False

    while time.monotonic() < readiness_deadline:
        readiness_ok = all(ready_file.is_file() for _, _, ready_file, _, _ in receiver_processes)
        if readiness_ok:
            break
        if any(process.poll() is not None for _, _, _, _, process in receiver_processes):
            break
        time.sleep(0.05)

    readiness_wait_s = time.monotonic() - readiness_started
    print(f"S2_DP_RECEIVER_READINESS_OK={readiness_ok}")
    print(f"S2_DP_RECEIVER_READINESS_WAIT_S={readiness_wait_s:.6f}")

    sender_output = output_dir / "sender.json"
    sender_command = [
        "ip", "netns", "exec", args.source_namespace,
        python, str(script), "sender",
        "--group", args.group,
        "--port", str(args.port),
        "--source-ip", args.source_ip,
        "--duration", str(args.duration),
        "--rate-mbps", str(args.rate_mbps),
        "--packet-size", str(args.packet_size),
        "--output", str(sender_output),
    ]

    if readiness_ok:
        sender_completed = subprocess.run(sender_command, text=True, capture_output=True, check=False)
        if sender_completed.stdout:
            print(sender_completed.stdout, end="")
        if sender_completed.stderr:
            print(sender_completed.stderr, end="", file=sys.stderr)
        time.sleep(float(args.receiver_drain_s))
    else:
        sender_completed = subprocess.CompletedProcess(
            sender_command,
            returncode=1,
            stdout="",
            stderr="receiver readiness barrier failed",
        )

    stop_signalled_ns = time.monotonic_ns()
    for _label, _output, _ready_file, stop_file, _process in receiver_processes:
        stop_file.touch()

    worker_logs: dict[str, Any] = {
        "sender": {
            "command": sender_command,
            "returncode": sender_completed.returncode,
            "stdout": sender_completed.stdout,
            "stderr": sender_completed.stderr,
        }
    }

    receiver_returncodes_ok = True
    for label, output, ready_file, stop_file, process in receiver_processes:
        try:
            stdout, stderr = process.communicate(timeout=float(args.receiver_stop_timeout_s))
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
        receiver_returncodes_ok = receiver_returncodes_ok and process.returncode == 0
        if stdout:
            print(stdout, end="")
        if stderr:
            print(stderr, end="", file=sys.stderr)
        worker_logs[f"receiver_{label}"] = {
            "returncode": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "output": str(output),
            "ready_file": str(ready_file),
            "stop_file": str(stop_file),
        }

    if not sender_output.is_file():
        print("S2_DP_SENDER_ARTIFACT_OK=False")
        return 1

    sender_data = json.loads(sender_output.read_text(encoding="utf-8"))
    sent = int(sender_data.get("packets_sent") or 0)
    planned = int(sender_data.get("packets_planned") or 0)
    all_ok = (
        readiness_ok
        and receiver_returncodes_ok
        and sender_completed.returncode == 0
        and sent > 0
        and sent == planned
    )

    receivers_summary: dict[str, Any] = {}
    for label, output, ready_file, stop_file, process in receiver_processes:
        if not output.is_file():
            receivers_summary[label] = {"artifact_present": False}
            all_ok = False
            continue
        data = json.loads(output.read_text(encoding="utf-8"))
        received_sequences = {int(item) for item in data.get("received_sequences", [])}
        valid_received = len({seq for seq in received_sequences if 0 <= seq < planned})
        unexpected = len(received_sequences) - valid_received
        ratio = (valid_received / planned) if planned else 0.0
        lifecycle = data.get("lifecycle") or {}
        receiver_ok = (
            ratio >= float(args.min_delivery)
            and int(data.get("malformed") or 0) == 0
            and unexpected == 0
            and lifecycle.get("termination_reason") == "stop_signal"
            and process.returncode == 0
        )
        all_ok = all_ok and receiver_ok
        receivers_summary[label] = {
            "artifact_present": True,
            "ready_file_present": ready_file.is_file(),
            "stop_file_present": stop_file.is_file(),
            "returncode": process.returncode,
            "valid_received": valid_received,
            "unexpected_sequences": unexpected,
            "delivery_ratio": ratio,
            "duplicates": int(data.get("duplicates") or 0),
            "malformed": int(data.get("malformed") or 0),
            "lifecycle": lifecycle,
            "one_way_delay_ms": data.get("one_way_delay_ms"),
            "ok": receiver_ok,
        }
        print(f"S2_DP_RECEIVER_{label}_DELIVERY_RATIO={ratio:.6f}")
        print(f"S2_DP_RECEIVER_{label}_REPLICATION_OK={receiver_ok}")

    summary = {
        "scenario": "S2_P4_multicast_dataplane_smoke",
        "scope": {
            "multicast_control_plane_programmed": True,
            "multicast_dataplane_exercised": True,
            "qos_contention_exercised": False,
            "recovery_metrics_exercised": False,
            "requested_sender_rate_validated": False,
        },
        "group": args.group,
        "port": args.port,
        "source": {
            "namespace": args.source_namespace,
            "ip": args.source_ip,
        },
        "receiver_lifecycle": {
            "mode": "explicit_readiness_sender_completion_drain_stop",
            "readiness_ok": readiness_ok,
            "readiness_wait_s": readiness_wait_s,
            "ready_timeout_s": args.receiver_ready_timeout_s,
            "drain_s": args.receiver_drain_s,
            "max_runtime_s": args.receiver_max_runtime_s,
            "stop_timeout_s": args.receiver_stop_timeout_s,
            "stop_signalled_monotonic_ns": stop_signalled_ns,
        },
        "receivers": receivers_summary,
        "sender": sender_data,
        "minimum_delivery_ratio": args.min_delivery,
        "worker_logs": worker_logs,
        "passed": bool(all_ok),
    }
    summary_path = output_dir / "summary.json"
    dump_json(summary_path, summary)

    print(f"S2_DP_SUMMARY={summary_path}")
    print("S2_DP_RECEIVER_LIFECYCLE_MODE=explicit_readiness_sender_completion_drain_stop")
    print(f"S2_DP_RECEIVER_STOP_SIGNALLED=True")
    print(f"S2_DP_DATAPLANE_EXERCISED=True")
    print(f"S2_DP_QOS_CONTENTION_EXERCISED=False")
    print(f"S2_DP_RECOVERY_METRICS_EXERCISED=False")
    print(f"S2_DP_REQUESTED_SENDER_RATE_VALIDATED=False")
    if all_ok:
        print("PHASE12_S2_P4_MULTICAST_DATAPLANE_SMOKE_OK")
        return 0
    print("PHASE12_S2_P4_MULTICAST_DATAPLANE_SMOKE_FAILED")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    send = sub.add_parser("sender")
    send.add_argument("--group", required=True)
    send.add_argument("--port", type=int, required=True)
    send.add_argument("--source-ip", required=True)
    send.add_argument("--duration", type=float, required=True)
    send.add_argument("--rate-mbps", type=float, required=True)
    send.add_argument("--packet-size", type=int, default=1200)
    send.add_argument("--output", required=True)

    recv = sub.add_parser("receiver")
    recv.add_argument("--label", required=True)
    recv.add_argument("--group", required=True)
    recv.add_argument("--port", type=int, required=True)
    recv.add_argument("--interface-ip", required=True)
    recv.add_argument("--duration", type=float, required=True)
    recv.add_argument("--ready-file")
    recv.add_argument("--stop-file")
    recv.add_argument("--output", required=True)

    orch = sub.add_parser("orchestrate")
    orch.add_argument("--group", default="239.1.1.1")
    orch.add_argument("--port", type=int, default=5001)
    orch.add_argument("--source-namespace", default="h1")
    orch.add_argument("--source-ip", default="10.0.0.1")
    orch.add_argument("--receiver", action="append", type=parse_receiver, required=True)
    orch.add_argument("--duration", type=float, default=3.0)
    orch.add_argument("--rate-mbps", type=float, default=2.0)
    orch.add_argument("--packet-size", type=int, default=1200)
    orch.add_argument("--min-delivery", type=float, default=0.99)
    orch.add_argument("--receiver-ready-timeout-s", type=float, default=10.0)
    orch.add_argument("--receiver-drain-s", type=float, default=1.0)
    orch.add_argument("--receiver-max-runtime-s", type=float, default=300.0)
    orch.add_argument("--receiver-stop-timeout-s", type=float, default=5.0)
    orch.add_argument("--output-dir", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "sender":
        return sender(args)
    if args.command == "receiver":
        return receiver(args)
    return orchestrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
