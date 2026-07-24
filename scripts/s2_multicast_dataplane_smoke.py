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
    sent = 0
    send_errors: list[str] = []

    for seq in range(total_packets):
        deadline = start_ns + seq * interval_ns
        while True:
            remaining = deadline - time.monotonic_ns()
            if remaining <= 0:
                break
            time.sleep(min(remaining / 1_000_000_000.0, 0.002))

        send_ns = time.monotonic_ns()
        datagram = HEADER.pack(MAGIC, seq, send_ns, total_packets) + filler
        try:
            sock.sendto(datagram, (args.group, args.port))
            sent += 1
        except OSError as exc:
            send_errors.append(f"seq={seq}: {exc}")

    end_ns = time.monotonic_ns()
    sock.close()

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
        "elapsed_s": (end_ns - start_ns) / 1_000_000_000.0,
    }
    dump_json(Path(args.output), result)
    print(f"S2_DP_SENDER_PACKETS_SENT={sent}")
    print(f"S2_DP_SENDER_PACKETS_PLANNED={total_packets}")
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

    deadline = time.monotonic() + float(args.duration)
    first_receive_ns: int | None = None
    last_receive_ns: int | None = None
    sequences: list[int] = []
    unique: set[int] = set()
    delays_ms: list[float] = []
    duplicates = 0
    malformed = 0
    expected_total: int | None = None

    while time.monotonic() < deadline:
        try:
            data, _peer = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
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

    result = {
        "role": "receiver",
        "label": args.label,
        "group": args.group,
        "port": args.port,
        "interface_ip": args.interface_ip,
        "duration_s": args.duration,
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
    return 0


def parse_receiver(value: str) -> tuple[str, str, str]:
    parts = value.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError("receiver must use LABEL:NAMESPACE:INTERFACE_IP")
    return parts[0], parts[1], parts[2]


def orchestrate(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    python = sys.executable

    receiver_duration = float(args.duration) + float(args.receiver_grace_s) + 1.0
    receiver_processes: list[tuple[str, Path, subprocess.Popen[str]]] = []

    for label, namespace, interface_ip in args.receiver:
        output = output_dir / f"receiver_{label}.json"
        command = [
            "ip", "netns", "exec", namespace,
            python, str(script), "receiver",
            "--label", label,
            "--group", args.group,
            "--port", str(args.port),
            "--interface-ip", interface_ip,
            "--duration", str(receiver_duration),
            "--output", str(output),
        ]
        process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        receiver_processes.append((label, output, process))

    time.sleep(float(args.receiver_ready_s))

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
    sender_completed = subprocess.run(sender_command, text=True, capture_output=True, check=False)

    worker_logs: dict[str, Any] = {
        "sender": {
            "command": sender_command,
            "returncode": sender_completed.returncode,
            "stdout": sender_completed.stdout,
            "stderr": sender_completed.stderr,
        }
    }

    for label, output, process in receiver_processes:
        try:
            stdout, stderr = process.communicate(timeout=receiver_duration + 3.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
        worker_logs[f"receiver_{label}"] = {
            "returncode": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "output": str(output),
        }

    if not sender_output.is_file():
        print("S2_DP_SENDER_ARTIFACT_OK=False")
        return 1

    sender_data = json.loads(sender_output.read_text(encoding="utf-8"))
    sent = int(sender_data.get("packets_sent") or 0)
    planned = int(sender_data.get("packets_planned") or 0)
    all_ok = sender_completed.returncode == 0 and sent > 0 and sent == planned

    receivers_summary: dict[str, Any] = {}
    for label, output, _process in receiver_processes:
        if not output.is_file():
            receivers_summary[label] = {"artifact_present": False}
            all_ok = False
            continue
        data = json.loads(output.read_text(encoding="utf-8"))
        received_sequences = {int(item) for item in data.get("received_sequences", [])}
        valid_received = len({seq for seq in received_sequences if 0 <= seq < planned})
        unexpected = len(received_sequences) - valid_received
        ratio = (valid_received / planned) if planned else 0.0
        receiver_ok = (
            ratio >= float(args.min_delivery)
            and int(data.get("malformed") or 0) == 0
            and unexpected == 0
        )
        all_ok = all_ok and receiver_ok
        receivers_summary[label] = {
            "artifact_present": True,
            "valid_received": valid_received,
            "unexpected_sequences": unexpected,
            "delivery_ratio": ratio,
            "duplicates": int(data.get("duplicates") or 0),
            "malformed": int(data.get("malformed") or 0),
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
        },
        "group": args.group,
        "port": args.port,
        "source": {
            "namespace": args.source_namespace,
            "ip": args.source_ip,
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
    print(f"S2_DP_DATAPLANE_EXERCISED=True")
    print(f"S2_DP_QOS_CONTENTION_EXERCISED=False")
    print(f"S2_DP_RECOVERY_METRICS_EXERCISED=False")
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
    orch.add_argument("--receiver-ready-s", type=float, default=1.0)
    orch.add_argument("--receiver-grace-s", type=float, default=1.0)
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
