#!/usr/bin/env python3
"""Inject and recover loss of S2 multicast P4Runtime state under traffic.

The fault model is deliberately narrow and observable: while paced multicast
traffic is active, the harness removes the multicast-table entry and the PRE
multicast group from the running BMv2 process. It confirms absence through
P4Runtime readback, holds the fault for a configured interval, rematerializes
the same desired state, and confirms restoration through a second readback.

A low-rate unicast flow remains programmed and active throughout the fault. It
acts as a dataplane continuity control, demonstrating that a multicast outage
is caused by the deleted multicast state rather than by a BMv2 restart, link
failure, or process-wide interruption.

Recovery is triggered by this test harness after explicit readback detection.
The experiment therefore validates state-loss detection and desired-state
rematerialization mechanics, but it does not claim autonomous MAD recovery or
BMv2 process-restart recovery.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, NoReturn, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from p4.v1 import p4runtime_pb2

from l2i.third_party.p4rt_min.client import P4RTClient
from scripts.p4_program_s2 import (
    build_mcast_entry,
    build_pre_entity,
    find_required_objects as find_mcast_objects,
    load_p4info,
    mcast_entry_matches,
    pre_group_matches,
    table_entity as mcast_table_entity,
    write_upsert as write_mcast_upsert,
)
from scripts.p4_program_unicast import (
    build_entry as build_unicast_entry,
    entry_matches as unicast_entry_matches,
    find_required_objects as find_unicast_objects,
    table_entity as unicast_table_entity,
    write_upsert as write_unicast_upsert,
)


def fail(message: str) -> NoReturn:
    """Terminate with a stable marker suitable for archived evidence."""

    print(f"PHASE15_RECOVERY_FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    """Write deterministic UTF-8 JSON evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def command_record(
    argv: Sequence[str],
    *,
    timeout_s: float = 10.0,
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
    """Return the current affinity mask without assuming contiguous IDs."""

    cpus = sorted(os.sched_getaffinity(0))
    if not cpus:
        fail("the process affinity mask does not expose any CPU")
    return cpus


def resolve_cpu(value: str, *, role: str, reserved: Iterable[int] = ()) -> int:
    """Resolve an explicit or automatic CPU while avoiding reserved CPUs."""

    normalized = value.strip().lower()
    cpus = allowed_cpus()
    reserved_set = {int(item) for item in reserved}
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


def process_ids(pattern: str) -> list[int]:
    """Return sorted process IDs matching one exact pgrep expression."""

    completed = subprocess.run(
        ["pgrep", "-f", pattern],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        return []
    return sorted(
        int(line)
        for line in completed.stdout.splitlines()
        if line.strip().isdigit()
    )


def tcp_port_available(host: str, port: int) -> bool:
    """Probe a TCP listener without changing its protocol state."""

    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def wait_for_files(
    paths: Sequence[Path],
    processes: Sequence[subprocess.Popen[str]],
    timeout_s: float,
) -> bool:
    """Wait for readiness files while aborting on an early worker exit."""

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if all(path.is_file() for path in paths):
            return True
        if any(process.poll() is not None for process in processes):
            return False
        time.sleep(0.05)
    return all(path.is_file() for path in paths)


def communicate_worker(
    process: subprocess.Popen[str],
    timeout_s: float,
) -> tuple[str, str]:
    """Collect one worker and force termination only after its deadline."""

    try:
        return process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            return process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.communicate()


def update_message(
    update_type: int,
    entity: p4runtime_pb2.Entity,
) -> p4runtime_pb2.Update:
    """Build one P4Runtime update message."""

    update = p4runtime_pb2.Update()
    update.type = update_type
    update.entity.CopyFrom(entity)
    return update


def read_table_entries_quiet(
    client: P4RTClient,
    table_id: int,
) -> tuple[bool, str, list[Any]]:
    """Read one table without emitting per-poll console output."""

    query = p4runtime_pb2.Entity()
    query.table_entry.table_id = int(table_id)
    ok, message, responses = client.read([query])
    if not ok:
        return False, message, []

    entries: list[Any] = []
    for response in responses:
        for entity in response.entities:
            if entity.HasField("table_entry"):
                entries.append(entity.table_entry)
    return True, message, entries


def read_pre_groups_quiet(
    client: P4RTClient,
    group_id: int,
) -> tuple[bool, str, list[Any]]:
    """Read one PRE group while treating an absent group as an empty result."""

    query = p4runtime_pb2.Entity()
    (
        query.packet_replication_engine_entry
        .multicast_group_entry
        .multicast_group_id
    ) = int(group_id)

    ok, message, responses = client.read([query])
    if not ok:
        if "NOT_FOUND" in message or "does not exist" in message:
            return True, message, []
        return False, message, []

    groups: list[Any] = []
    for response in responses:
        for entity in response.entities:
            if not entity.HasField("packet_replication_engine_entry"):
                continue
            pre = entity.packet_replication_engine_entry
            if pre.HasField("multicast_group_entry"):
                groups.append(pre.multicast_group_entry)
    return True, message, groups


def multicast_state_readback(
    client: P4RTClient,
    *,
    table: Any,
    match_def: Any,
    action: Any,
    param: Any,
    group_id: int,
    ports: list[int],
    dst_ip: str,
) -> dict[str, Any]:
    """Read and classify the complete multicast state."""

    pre_ok, pre_message, groups = read_pre_groups_quiet(client, group_id)
    table_ok, table_message, entries = read_table_entries_quiet(
        client,
        int(table.preamble.id),
    )

    group_present = any(
        pre_group_matches(group, group_id, ports)
        for group in groups
    )
    entry_present = any(
        mcast_entry_matches(
            entry,
            table=table,
            match_def=match_def,
            action=action,
            param=param,
            dst_ip=dst_ip,
            group_id=group_id,
        )
        for entry in entries
    )

    return {
        "read_ok": bool(pre_ok and table_ok),
        "pre_read_ok": pre_ok,
        "pre_message": pre_message,
        "table_read_ok": table_ok,
        "table_message": table_message,
        "group_present": group_present,
        "entry_present": entry_present,
        "complete_present": bool(group_present and entry_present),
        "complete_absent": bool(not group_present and not entry_present),
        "observed_monotonic_ns": time.monotonic_ns(),
    }


def unicast_state_readback(
    client: P4RTClient,
    *,
    table: Any,
    match_def: Any,
    action: Any,
    param: Any,
    ingress_port: int,
    egress_port: int,
) -> dict[str, Any]:
    """Read and classify the independent unicast control rule."""

    read_ok, message, entries = read_table_entries_quiet(
        client,
        int(table.preamble.id),
    )
    present = any(
        unicast_entry_matches(
            entry,
            table=table,
            match_def=match_def,
            action=action,
            param=param,
            ingress_port=ingress_port,
            egress_port=egress_port,
        )
        for entry in entries
    )
    return {
        "read_ok": read_ok,
        "message": message,
        "present": present,
        "observed_monotonic_ns": time.monotonic_ns(),
    }


def wait_for_multicast_state(
    client: P4RTClient,
    *,
    expected_present: bool,
    timeout_s: float,
    poll_interval_s: float,
    state_kwargs: dict[str, Any],
) -> tuple[bool, dict[str, Any], int]:
    """Poll readback until the complete multicast state reaches one condition."""

    deadline = time.monotonic() + timeout_s
    polls = 0
    last: dict[str, Any] = {}

    while time.monotonic() < deadline:
        polls += 1
        last = multicast_state_readback(client, **state_kwargs)
        reached = (
            last.get("complete_present") is True
            if expected_present
            else last.get("complete_absent") is True
        )
        if last.get("read_ok") is True and reached:
            return True, last, polls
        time.sleep(poll_interval_s)

    return False, last, polls


def window_metrics(
    sender_timeline: list[dict[str, Any]],
    receiver_timeline: list[dict[str, Any]],
    *,
    start_ns: int,
    end_ns: int,
) -> dict[str, Any]:
    """Calculate delivery for packets sent inside one monotonic-time window."""

    expected = {
        int(item["sequence"])
        for item in sender_timeline
        if bool(item.get("sent"))
        and start_ns <= int(item.get("send_monotonic_ns") or 0) < end_ns
    }
    received = {
        int(item["sequence"])
        for item in receiver_timeline
        if item.get("sequence") is not None
        and int(item["sequence"]) in expected
    }
    ratio = len(received) / len(expected) if expected else 0.0
    return {
        "start_monotonic_ns": int(start_ns),
        "end_monotonic_ns": int(end_ns),
        "expected_packets": len(expected),
        "received_packets": len(received),
        "missing_packets": len(expected - received),
        "delivery_ratio": ratio,
    }


def first_recovered_packet(
    receiver_timeline: list[dict[str, Any]],
    restoration_confirmed_ns: int,
    *,
    remediation_started_ns: int,
    fault_injection_started_ns: int,
) -> dict[str, Any]:
    """Find the first packet sent after restoration and observed by a receiver.

    The three latency origins separate control-plane and dataplane effects. The
    restoration-confirmed origin measures only dataplane resumption after a
    successful readback, while the remediation and fault origins retain the
    complete harness-observed recovery timeline.
    """

    candidates = [
        item
        for item in receiver_timeline
        if int(item.get("send_monotonic_ns") or 0) >= restoration_confirmed_ns
    ]
    if not candidates:
        return {
            "found": False,
            "sequence": None,
            "send_monotonic_ns": None,
            "receive_monotonic_ns": None,
            "from_restoration_to_receive_ms": None,
            "from_remediation_start_to_receive_ms": None,
            "from_fault_start_to_receive_ms": None,
        }

    first = min(candidates, key=lambda item: int(item["receive_monotonic_ns"]))
    return {
        "found": True,
        "sequence": int(first["sequence"]),
        "send_monotonic_ns": int(first["send_monotonic_ns"]),
        "receive_monotonic_ns": int(first["receive_monotonic_ns"]),
        "from_restoration_to_receive_ms": (
            int(first["receive_monotonic_ns"]) - restoration_confirmed_ns
        ) / 1_000_000.0,
        "from_remediation_start_to_receive_ms": (
            int(first["receive_monotonic_ns"]) - remediation_started_ns
        ) / 1_000_000.0,
        "from_fault_start_to_receive_ms": (
            int(first["receive_monotonic_ns"]) - fault_injection_started_ns
        ) / 1_000_000.0,
    }


def crossing_gap(
    receiver_timeline: list[dict[str, Any]],
    *,
    absence_confirmed_ns: int,
    restoration_confirmed_ns: int,
) -> dict[str, Any]:
    """Measure the observed receive gap spanning the deleted-state interval."""

    before = [
        item
        for item in receiver_timeline
        if int(item.get("send_monotonic_ns") or 0) < absence_confirmed_ns
    ]
    after = [
        item
        for item in receiver_timeline
        if int(item.get("send_monotonic_ns") or 0) >= restoration_confirmed_ns
    ]
    if not before or not after:
        return {
            "found": False,
            "last_before_sequence": None,
            "first_after_sequence": None,
            "receive_gap_ms": None,
            "sequence_gap": None,
        }

    last_before = max(before, key=lambda item: int(item["receive_monotonic_ns"]))
    first_after = min(after, key=lambda item: int(item["receive_monotonic_ns"]))
    return {
        "found": True,
        "last_before_sequence": int(last_before["sequence"]),
        "first_after_sequence": int(first_after["sequence"]),
        "receive_gap_ms": (
            int(first_after["receive_monotonic_ns"])
            - int(last_before["receive_monotonic_ns"])
        ) / 1_000_000.0,
        "sequence_gap": int(first_after["sequence"]) - int(last_before["sequence"]) - 1,
    }


def sender_rate_validated(
    sender: dict[str, Any],
    *,
    maximum_error_pct: float,
    minimum_inter_send_ratio: float,
) -> bool:
    """Apply the Phase 13 sender-rate gates to one sender artifact."""

    planned = int(sender.get("packets_planned") or 0)
    sent = int(sender.get("packets_sent") or 0)
    error = abs(float(sender.get("rate_error_pct") or 0.0))
    pacing = sender.get("pacing") or {}
    interval_ms = float(pacing.get("interval_ns") or 0) / 1_000_000.0
    minimum_ms = float((pacing.get("inter_send_ms") or {}).get("min") or 0.0)
    ratio = minimum_ms / interval_ms if interval_ms > 0.0 else 0.0
    return (
        planned > 0
        and sent == planned
        and error <= maximum_error_pct
        and ratio >= minimum_inter_send_ratio
    )


def orchestrate(args: argparse.Namespace) -> int:
    """Run one multicast-state deletion and rematerialization experiment."""

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    control_dir = output_dir / "control"
    control_dir.mkdir(parents=True, exist_ok=True)

    if args.duration <= args.fault_after_s + args.fault_hold_s + args.minimum_post_s:
        fail("duration must leave the configured minimum post-recovery interval")
    if args.fault_after_s <= args.window_guard_s:
        fail("fault-after interval must exceed the measurement guard")

    multicast_cpu = resolve_cpu(args.multicast_sender_cpu, role="multicast sender")
    control_cpu = resolve_cpu(
        args.control_sender_cpu,
        role="unicast control sender",
        reserved=(multicast_cpu,),
    )

    h3_mac_record = command_record(
        [
            "ip", "netns", "exec", args.control_receiver_namespace,
            "cat", f"/sys/class/net/{args.control_receiver_device}/address",
        ],
        check=True,
    )
    h3_mac = h3_mac_record["stdout"].strip()
    if not re.fullmatch(r"[0-9a-fA-F:]{17}", h3_mac):
        fail(f"invalid control receiver MAC address: {h3_mac!r}")

    neighbor_record = command_record(
        [
            "ip", "-n", args.control_source_namespace,
            "neigh", "replace", args.control_receiver_ip,
            "lladdr", h3_mac, "nud", "permanent",
            "dev", args.control_source_device,
        ],
        check=True,
    )

    p4info_path = Path(args.p4_outdir) / "l2i_minimal.p4info.txtpb"
    p4info = load_p4info(p4info_path)
    mcast_table, mcast_match, mcast_action, mcast_param = find_mcast_objects(p4info)
    unicast_table, unicast_match, unicast_action, unicast_param = (
        find_unicast_objects(p4info)
    )

    mcast_entry = build_mcast_entry(
        table=mcast_table,
        match_def=mcast_match,
        action=mcast_action,
        param=mcast_param,
        dst_ip=args.group,
        group_id=args.group_id,
        include_action=True,
    )
    mcast_delete_key = build_mcast_entry(
        table=mcast_table,
        match_def=mcast_match,
        action=mcast_action,
        param=mcast_param,
        dst_ip=args.group,
        group_id=args.group_id,
        include_action=False,
    )
    pre_entity = build_pre_entity(
        args.group_id,
        args.multicast_ports,
        include_replicas=True,
    )
    pre_delete_key = build_pre_entity(
        args.group_id,
        args.multicast_ports,
        include_replicas=False,
    )
    unicast_entry = build_unicast_entry(
        table=unicast_table,
        match_def=unicast_match,
        action=unicast_action,
        param=unicast_param,
        ingress_port=args.control_ingress_port,
        egress_port=args.control_egress_port,
        include_action=True,
    )
    unicast_delete_key = build_unicast_entry(
        table=unicast_table,
        match_def=unicast_match,
        action=unicast_action,
        param=unicast_param,
        ingress_port=args.control_ingress_port,
        egress_port=args.control_egress_port,
        include_action=False,
    )

    mcast_state_kwargs = {
        "table": mcast_table,
        "match_def": mcast_match,
        "action": mcast_action,
        "param": mcast_param,
        "group_id": args.group_id,
        "ports": args.multicast_ports,
        "dst_ip": args.group,
    }

    initial_pids = process_ids("[s]imple_switch_grpc")
    initial_port_ok = tcp_port_available(args.p4_host, args.p4_port)
    if not initial_pids or not initial_port_ok:
        fail("BMv2 process and P4Runtime listener must already be active")

    client = P4RTClient(
        address=args.p4_addr,
        device_id=args.device_id,
        election_id=(0, 1501),
        timeout_s=args.p4_timeout_s,
    )

    receiver_processes: list[tuple[str, Path, Path, Path, subprocess.Popen[str]]] = []
    sender_processes: dict[str, subprocess.Popen[str]] = {}
    worker_logs: dict[str, Any] = {}
    state_events: dict[str, Any] = {}
    cleanup_state: dict[str, Any] = {}

    smoke_script = REPO_ROOT / "scripts" / "s2_multicast_dataplane_smoke.py"
    qos_script = REPO_ROOT / "scripts" / "s2_p4_qos_contention.py"
    python = sys.executable

    try:
        client.connect()
        arbitration = client.ensure_primary(wait_s=2.0)
        if not arbitration.ok or not arbitration.is_primary:
            fail("primary P4Runtime arbitration was not granted")

        # Remove stale state from an interrupted prior run before programming
        # the desired baseline. This keeps repeated experiments idempotent.
        existing_mcast = multicast_state_readback(client, **mcast_state_kwargs)
        existing_unicast = unicast_state_readback(
            client,
            table=unicast_table,
            match_def=unicast_match,
            action=unicast_action,
            param=unicast_param,
            ingress_port=args.control_ingress_port,
            egress_port=args.control_egress_port,
        )
        stale_updates: list[p4runtime_pb2.Update] = []
        if existing_mcast.get("entry_present") is True:
            stale_updates.append(
                update_message(
                    p4runtime_pb2.Update.DELETE,
                    mcast_table_entity(mcast_delete_key),
                )
            )
        if existing_mcast.get("group_present") is True:
            stale_updates.append(
                update_message(p4runtime_pb2.Update.DELETE, pre_delete_key)
            )
        if existing_unicast.get("present") is True:
            stale_updates.append(
                update_message(
                    p4runtime_pb2.Update.DELETE,
                    unicast_table_entity(unicast_delete_key),
                )
            )
        if stale_updates:
            stale_ok, stale_message = client.write(stale_updates)
            if not stale_ok:
                fail(f"stale-state cleanup failed: {stale_message}")

        write_mcast_upsert(client, pre_entity, "PHASE15_INITIAL_PRE")
        write_mcast_upsert(
            client,
            mcast_table_entity(mcast_entry),
            "PHASE15_INITIAL_MCAST_TABLE",
        )
        write_unicast_upsert(client, unicast_table_entity(unicast_entry))

        initial_mcast = multicast_state_readback(client, **mcast_state_kwargs)
        initial_unicast = unicast_state_readback(
            client,
            table=unicast_table,
            match_def=unicast_match,
            action=unicast_action,
            param=unicast_param,
            ingress_port=args.control_ingress_port,
            egress_port=args.control_egress_port,
        )
        initial_state_ok = (
            initial_mcast.get("complete_present") is True
            and initial_unicast.get("present") is True
        )
        if not initial_state_ok:
            fail("initial multicast or unicast state was not confirmed")

        # Start multicast receivers B and C.
        for label, namespace, interface_ip in (
            ("B", args.receiver_b_namespace, args.receiver_b_ip),
            ("C", args.receiver_c_namespace, args.receiver_c_ip),
        ):
            output = output_dir / f"multicast_receiver_{label}.json"
            ready = control_dir / f"multicast_receiver_{label}.ready.json"
            stop = control_dir / f"multicast_receiver_{label}.stop"
            ready.unlink(missing_ok=True)
            stop.unlink(missing_ok=True)
            command = [
                "ip", "netns", "exec", namespace,
                python, str(smoke_script), "receiver",
                "--label", label,
                "--group", args.group,
                "--port", str(args.multicast_port),
                "--interface-ip", interface_ip,
                "--duration", str(args.worker_max_runtime_s),
                "--ready-file", str(ready),
                "--stop-file", str(stop),
                "--output", str(output),
            ]
            process = subprocess.Popen(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            receiver_processes.append((label, output, ready, stop, process))

        control_receiver_output = output_dir / "control_receiver.json"
        control_ready = control_dir / "control_receiver.ready.json"
        control_stop = control_dir / "control_receiver.stop"
        control_ready.unlink(missing_ok=True)
        control_stop.unlink(missing_ok=True)
        control_receiver_command = [
            "ip", "netns", "exec", args.control_receiver_namespace,
            python, str(qos_script), "background-receiver",
            "--interface-ip", args.control_receiver_ip,
            "--port", str(args.control_port),
            "--max-runtime-s", str(args.worker_max_runtime_s),
            "--ready-file", str(control_ready),
            "--stop-file", str(control_stop),
            "--output", str(control_receiver_output),
        ]
        control_receiver_process = subprocess.Popen(
            control_receiver_command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        receiver_processes.append(
            (
                "CONTROL",
                control_receiver_output,
                control_ready,
                control_stop,
                control_receiver_process,
            )
        )

        readiness_ok = wait_for_files(
            [item[2] for item in receiver_processes],
            [item[4] for item in receiver_processes],
            timeout_s=args.worker_ready_timeout_s,
        )
        if not readiness_ok:
            fail("receiver readiness barrier failed")

        multicast_sender_output = output_dir / "multicast_sender.json"
        multicast_sender_command = [
            "ip", "netns", "exec", args.multicast_source_namespace,
            "taskset", "-c", str(multicast_cpu),
            python, str(smoke_script), "sender",
            "--group", args.group,
            "--port", str(args.multicast_port),
            "--source-ip", args.multicast_source_ip,
            "--duration", str(args.duration),
            "--rate-mbps", str(args.multicast_rate_mbps),
            "--packet-size", str(args.packet_size),
            "--pacing-mode", "repeated_sleep_spin",
            "--spin-threshold-us", str(args.spin_threshold_us),
            "--output", str(multicast_sender_output),
        ]

        control_sender_output = output_dir / "control_sender.json"
        control_sender_command = [
            "ip", "netns", "exec", args.control_source_namespace,
            "taskset", "-c", str(control_cpu),
            python, str(qos_script), "background-sender",
            "--source-ip", args.control_source_ip,
            "--destination-ip", args.control_receiver_ip,
            "--port", str(args.control_port),
            "--duration", str(args.duration),
            "--rate-mbps", str(args.control_rate_mbps),
            "--packet-size", str(args.packet_size),
            "--spin-threshold-us", str(args.spin_threshold_us),
            "--output", str(control_sender_output),
        ]

        sender_processes["control"] = subprocess.Popen(
            control_sender_command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sender_processes["multicast"] = subprocess.Popen(
            multicast_sender_command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        traffic_processes_started_ns = time.monotonic_ns()

        fault_deadline = time.monotonic() + args.fault_after_s
        while time.monotonic() < fault_deadline:
            if any(process.poll() is not None for process in sender_processes.values()):
                fail("a sender exited before fault injection")
            time.sleep(0.02)

        fault_injection_started_ns = time.monotonic_ns()
        fault_updates = [
            update_message(
                p4runtime_pb2.Update.DELETE,
                mcast_table_entity(mcast_delete_key),
            ),
            update_message(p4runtime_pb2.Update.DELETE, pre_delete_key),
        ]
        fault_write_started_ns = time.monotonic_ns()
        fault_write_ok, fault_write_message = client.write(fault_updates)
        fault_write_completed_ns = time.monotonic_ns()
        if not fault_write_ok:
            fail(f"multicast state deletion failed: {fault_write_message}")

        absence_ok, absence_readback, absence_polls = wait_for_multicast_state(
            client,
            expected_present=False,
            timeout_s=args.state_readback_timeout_s,
            poll_interval_s=args.state_poll_interval_s,
            state_kwargs=mcast_state_kwargs,
        )
        fault_absence_confirmed_ns = int(
            absence_readback.get("observed_monotonic_ns") or time.monotonic_ns()
        )

        unicast_during_fault = unicast_state_readback(
            client,
            table=unicast_table,
            match_def=unicast_match,
            action=unicast_action,
            param=unicast_param,
            ingress_port=args.control_ingress_port,
            egress_port=args.control_egress_port,
        )
        pids_during_fault = process_ids("[s]imple_switch_grpc")
        port_during_fault = tcp_port_available(args.p4_host, args.p4_port)

        if not absence_ok:
            fail("multicast state absence was not confirmed by readback")

        hold_deadline = time.monotonic() + args.fault_hold_s
        while time.monotonic() < hold_deadline:
            if any(process.poll() is not None for process in sender_processes.values()):
                fail("a sender exited during the configured fault hold")
            time.sleep(0.02)

        remediation_started_ns = time.monotonic_ns()
        restore_updates = [
            update_message(p4runtime_pb2.Update.INSERT, pre_entity),
            update_message(
                p4runtime_pb2.Update.INSERT,
                mcast_table_entity(mcast_entry),
            ),
        ]
        restore_write_started_ns = time.monotonic_ns()
        restore_write_ok, restore_write_message = client.write(restore_updates)
        restore_write_completed_ns = time.monotonic_ns()
        if not restore_write_ok:
            fail(f"multicast state rematerialization failed: {restore_write_message}")

        restoration_ok, restoration_readback, restoration_polls = (
            wait_for_multicast_state(
                client,
                expected_present=True,
                timeout_s=args.state_readback_timeout_s,
                poll_interval_s=args.state_poll_interval_s,
                state_kwargs=mcast_state_kwargs,
            )
        )
        restoration_confirmed_ns = int(
            restoration_readback.get("observed_monotonic_ns")
            or time.monotonic_ns()
        )

        unicast_after_restoration = unicast_state_readback(
            client,
            table=unicast_table,
            match_def=unicast_match,
            action=unicast_action,
            param=unicast_param,
            ingress_port=args.control_ingress_port,
            egress_port=args.control_egress_port,
        )
        if not restoration_ok:
            fail("multicast state restoration was not confirmed by readback")

        sender_logs: dict[str, Any] = {}
        for label, process in sender_processes.items():
            stdout, stderr = communicate_worker(process, args.worker_max_runtime_s)
            if stdout:
                print(stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)
            sender_logs[label] = {
                "returncode": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }

        time.sleep(args.receiver_drain_s)
        for _label, _output, _ready, stop, _process in receiver_processes:
            stop.touch()

        receiver_logs: dict[str, Any] = {}
        for label, output, ready, stop, process in receiver_processes:
            stdout, stderr = communicate_worker(process, args.worker_stop_timeout_s)
            if stdout:
                print(stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)
            receiver_logs[label] = {
                "returncode": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "output": str(output),
                "ready_file": str(ready),
                "stop_file": str(stop),
            }

        required_outputs = (
            multicast_sender_output,
            control_sender_output,
            output_dir / "multicast_receiver_B.json",
            output_dir / "multicast_receiver_C.json",
            control_receiver_output,
        )
        if not all(path.is_file() for path in required_outputs):
            fail("one or more traffic evidence files are missing")

        multicast_sender = json.loads(
            multicast_sender_output.read_text(encoding="utf-8")
        )
        control_sender = json.loads(control_sender_output.read_text(encoding="utf-8"))
        multicast_receivers = {
            label: json.loads(
                (output_dir / f"multicast_receiver_{label}.json").read_text(
                    encoding="utf-8"
                )
            )
            for label in ("B", "C")
        }
        control_receiver = json.loads(
            control_receiver_output.read_text(encoding="utf-8")
        )

        mcast_sender_timeline = multicast_sender.get("packet_send_timeline") or []
        control_sender_timeline = control_sender.get("packet_send_timeline") or []
        first_mcast_send_ns = min(
            int(item["send_monotonic_ns"])
            for item in mcast_sender_timeline
            if bool(item.get("sent"))
        )
        last_mcast_send_ns = max(
            int(item["send_monotonic_ns"])
            for item in mcast_sender_timeline
            if bool(item.get("sent"))
        )
        first_control_send_ns = min(
            int(item["send_monotonic_ns"])
            for item in control_sender_timeline
            if bool(item.get("sent"))
        )
        last_control_send_ns = max(
            int(item["send_monotonic_ns"])
            for item in control_sender_timeline
            if bool(item.get("sent"))
        )

        guard_ns = int(round(args.window_guard_s * 1_000_000_000.0))
        mcast_windows = {
            "pre_fault": (
                first_mcast_send_ns + guard_ns,
                fault_injection_started_ns,
            ),
            "fault_absent": (
                fault_absence_confirmed_ns,
                remediation_started_ns,
            ),
            "post_recovery": (
                restoration_confirmed_ns + guard_ns,
                last_mcast_send_ns + 1,
            ),
        }
        control_windows = {
            "pre_fault": (
                first_control_send_ns + guard_ns,
                fault_injection_started_ns,
            ),
            "fault_absent": (
                fault_absence_confirmed_ns,
                remediation_started_ns,
            ),
            "post_recovery": (
                restoration_confirmed_ns + guard_ns,
                last_control_send_ns + 1,
            ),
        }

        multicast_metrics: dict[str, Any] = {}
        for label, receiver in multicast_receivers.items():
            receiver_timeline = receiver.get("packet_receive_timeline") or []
            windows = {
                name: window_metrics(
                    mcast_sender_timeline,
                    receiver_timeline,
                    start_ns=start_ns,
                    end_ns=end_ns,
                )
                for name, (start_ns, end_ns) in mcast_windows.items()
            }
            multicast_metrics[label] = {
                "windows": windows,
                "first_recovered_packet": first_recovered_packet(
                    receiver_timeline,
                    restoration_confirmed_ns,
                    remediation_started_ns=remediation_started_ns,
                    fault_injection_started_ns=fault_injection_started_ns,
                ),
                "crossing_gap": crossing_gap(
                    receiver_timeline,
                    absence_confirmed_ns=fault_absence_confirmed_ns,
                    restoration_confirmed_ns=restoration_confirmed_ns,
                ),
                "malformed": int(receiver.get("malformed") or 0),
                "duplicates": int(receiver.get("duplicates") or 0),
                "lifecycle": receiver.get("lifecycle") or {},
            }

        control_receiver_timeline = control_receiver.get("packet_receive_timeline") or []
        control_metrics = {
            "windows": {
                name: window_metrics(
                    control_sender_timeline,
                    control_receiver_timeline,
                    start_ns=start_ns,
                    end_ns=end_ns,
                )
                for name, (start_ns, end_ns) in control_windows.items()
            },
            "malformed": int(control_receiver.get("malformed") or 0),
            "duplicates": int(control_receiver.get("duplicates") or 0),
            "lifecycle": control_receiver.get("lifecycle") or {},
        }

        final_pids = process_ids("[s]imple_switch_grpc")
        final_port_ok = tcp_port_available(args.p4_host, args.p4_port)
        bmv2_pid_continuity = (
            initial_pids == pids_during_fault == final_pids
            and bool(initial_pids)
        )

        multicast_rate_ok = sender_rate_validated(
            multicast_sender,
            maximum_error_pct=args.max_abs_rate_error_pct,
            minimum_inter_send_ratio=args.min_inter_send_ratio,
        )
        control_rate_ok = sender_rate_validated(
            control_sender,
            maximum_error_pct=args.max_abs_rate_error_pct,
            minimum_inter_send_ratio=args.min_inter_send_ratio,
        )

        receiver_lifecycle_ok = all(
            (item.get("lifecycle") or {}).get("termination_reason") == "stop_signal"
            for item in [*multicast_receivers.values(), control_receiver]
        )
        receiver_integrity_ok = all(
            int(item.get("malformed") or 0) == 0
            for item in [*multicast_receivers.values(), control_receiver]
        )
        worker_returncodes_ok = all(
            process.returncode == 0
            for process in [*sender_processes.values(), *[item[4] for item in receiver_processes]]
        )

        state_events = {
            "initial": {
                "multicast": initial_mcast,
                "unicast": initial_unicast,
            },
            "traffic_processes_started_monotonic_ns": traffic_processes_started_ns,
            "fault": {
                "injection_started_monotonic_ns": fault_injection_started_ns,
                "write_started_monotonic_ns": fault_write_started_ns,
                "write_completed_monotonic_ns": fault_write_completed_ns,
                "write_ok": fault_write_ok,
                "write_message": fault_write_message,
                "absence_confirmed_monotonic_ns": fault_absence_confirmed_ns,
                "absence_confirmed": absence_ok,
                "absence_readback": absence_readback,
                "absence_poll_count": absence_polls,
                "configured_hold_s": args.fault_hold_s,
                "unicast_rule_during_fault": unicast_during_fault,
                "bmv2_pids_during_fault": pids_during_fault,
                "p4runtime_available_during_fault": port_during_fault,
            },
            "remediation": {
                "started_monotonic_ns": remediation_started_ns,
                "write_started_monotonic_ns": restore_write_started_ns,
                "write_completed_monotonic_ns": restore_write_completed_ns,
                "write_ok": restore_write_ok,
                "write_message": restore_write_message,
                "restoration_confirmed_monotonic_ns": restoration_confirmed_ns,
                "restoration_confirmed": restoration_ok,
                "restoration_readback": restoration_readback,
                "restoration_poll_count": restoration_polls,
                "unicast_rule_after_restoration": unicast_after_restoration,
            },
            "timing_ms": {
                "fault_write": (
                    fault_write_completed_ns - fault_write_started_ns
                ) / 1_000_000.0,
                "absence_detection_from_fault_start": (
                    fault_absence_confirmed_ns - fault_injection_started_ns
                ) / 1_000_000.0,
                "restore_write": (
                    restore_write_completed_ns - restore_write_started_ns
                ) / 1_000_000.0,
                "control_plane_recovery_from_remediation_start": (
                    restoration_confirmed_ns - remediation_started_ns
                ) / 1_000_000.0,
            },
        }

        candidate_checks = {
            "state_absence_confirmed": absence_ok,
            "state_restoration_confirmed": restoration_ok,
            "bmv2_process_continuity": bmv2_pid_continuity,
            "p4runtime_continuity": bool(
                initial_port_ok and port_during_fault and final_port_ok
            ),
            "unicast_control_rule_continuity": bool(
                unicast_during_fault.get("present") is True
                and unicast_after_restoration.get("present") is True
            ),
            "multicast_pre_fault_delivery": all(
                multicast_metrics[label]["windows"]["pre_fault"]["delivery_ratio"]
                >= args.minimum_stable_delivery
                for label in ("B", "C")
            ),
            "multicast_fault_effect_observed": all(
                multicast_metrics[label]["windows"]["fault_absent"]["expected_packets"]
                >= args.minimum_window_packets
                and multicast_metrics[label]["windows"]["fault_absent"]["delivery_ratio"]
                <= args.maximum_fault_delivery
                for label in ("B", "C")
            ),
            "multicast_post_recovery_delivery": all(
                multicast_metrics[label]["windows"]["post_recovery"]["delivery_ratio"]
                >= args.minimum_stable_delivery
                for label in ("B", "C")
            ),
            "first_packet_recovered": all(
                multicast_metrics[label]["first_recovered_packet"]["found"] is True
                and float(
                    multicast_metrics[label]["first_recovered_packet"][
                        "from_restoration_to_receive_ms"
                    ]
                    if multicast_metrics[label]["first_recovered_packet"][
                        "from_restoration_to_receive_ms"
                    ] is not None
                    else float("inf")
                )
                <= args.maximum_first_packet_recovery_ms
                for label in ("B", "C")
            ),
            "unicast_control_stable": all(
                control_metrics["windows"][name]["expected_packets"]
                >= args.minimum_window_packets
                and control_metrics["windows"][name]["delivery_ratio"]
                >= args.minimum_control_delivery
                for name in ("pre_fault", "fault_absent", "post_recovery")
            ),
        }
        recovery_candidate = all(candidate_checks.values())

        # Remove both test states after all readbacks and traffic evidence have
        # been collected. The cleanup is part of the operational result.
        cleanup_updates = [
            update_message(
                p4runtime_pb2.Update.DELETE,
                mcast_table_entity(mcast_delete_key),
            ),
            update_message(p4runtime_pb2.Update.DELETE, pre_delete_key),
            update_message(
                p4runtime_pb2.Update.DELETE,
                unicast_table_entity(unicast_delete_key),
            ),
        ]
        cleanup_write_ok, cleanup_write_message = client.write(cleanup_updates)
        cleanup_mcast = multicast_state_readback(client, **mcast_state_kwargs)
        cleanup_unicast = unicast_state_readback(
            client,
            table=unicast_table,
            match_def=unicast_match,
            action=unicast_action,
            param=unicast_param,
            ingress_port=args.control_ingress_port,
            egress_port=args.control_egress_port,
        )
        cleanup_state = {
            "write_ok": cleanup_write_ok,
            "write_message": cleanup_write_message,
            "multicast_absent": cleanup_mcast.get("complete_absent") is True,
            "unicast_absent": cleanup_unicast.get("present") is False,
            "multicast_readback": cleanup_mcast,
            "unicast_readback": cleanup_unicast,
        }

        operational_checks = {
            "initial_state_ok": initial_state_ok,
            "receiver_readiness_ok": readiness_ok,
            "worker_returncodes_ok": worker_returncodes_ok,
            "receiver_lifecycle_ok": receiver_lifecycle_ok,
            "receiver_integrity_ok": receiver_integrity_ok,
            "multicast_sender_rate_ok": multicast_rate_ok,
            "control_sender_rate_ok": control_rate_ok,
            "fault_write_ok": fault_write_ok,
            "state_absence_confirmed": absence_ok,
            "restore_write_ok": restore_write_ok,
            "state_restoration_confirmed": restoration_ok,
            "unicast_rule_during_fault": unicast_during_fault.get("present") is True,
            "unicast_rule_after_restoration": (
                unicast_after_restoration.get("present") is True
            ),
            "bmv2_process_continuity": bmv2_pid_continuity,
            "p4runtime_continuity": bool(
                initial_port_ok and port_during_fault and final_port_ok
            ),
            "cleanup_write_ok": cleanup_write_ok,
            "cleanup_multicast_absent": cleanup_state["multicast_absent"],
            "cleanup_unicast_absent": cleanup_state["unicast_absent"],
        }
        operational_ok = all(operational_checks.values())

        summary = {
            "scenario": "S2_P4_multicast_state_recovery_foundation",
            "fault_model": "p4runtime_multicast_table_and_pre_state_deletion",
            "scope": {
                "multicast_state_loss_injected": True,
                "p4runtime_absence_detection_exercised": True,
                "desired_state_rematerialization_exercised": True,
                "unicast_dataplane_continuity_control_exercised": True,
                "bmv2_process_restart_exercised": False,
                "pipeline_reload_exercised": False,
                "autonomous_mad_detection_validated": False,
                "autonomous_mad_recovery_validated": False,
                "test_harness_triggered_recovery": True,
                "qos_contention_exercised": False,
            },
            "configuration": {
                "duration_s": args.duration,
                "fault_after_s": args.fault_after_s,
                "fault_hold_s": args.fault_hold_s,
                "minimum_post_s": args.minimum_post_s,
                "window_guard_s": args.window_guard_s,
                "multicast_payload_rate_mbps": args.multicast_rate_mbps,
                "control_payload_rate_mbps": args.control_rate_mbps,
                "packet_size_bytes": args.packet_size,
                "spin_threshold_us": args.spin_threshold_us,
                "multicast_sender_cpu": multicast_cpu,
                "control_sender_cpu": control_cpu,
                "group": args.group,
                "group_id": args.group_id,
                "multicast_ports": args.multicast_ports,
                "control_ingress_port": args.control_ingress_port,
                "control_egress_port": args.control_egress_port,
            },
            "p4": {
                "address": args.p4_addr,
                "initial_process_ids": initial_pids,
                "process_ids_during_fault": pids_during_fault,
                "final_process_ids": final_pids,
                "pid_continuity": bmv2_pid_continuity,
                "initial_port_available": initial_port_ok,
                "port_available_during_fault": port_during_fault,
                "final_port_available": final_port_ok,
                "state_events": state_events,
                "cleanup": cleanup_state,
            },
            "traffic": {
                "multicast_sender": multicast_sender,
                "control_sender": control_sender,
                "multicast_receivers": multicast_receivers,
                "control_receiver": control_receiver,
            },
            "recovery_metrics": {
                "multicast_receivers": multicast_metrics,
                "unicast_control": control_metrics,
                "candidate_checks": candidate_checks,
                "candidate_found": recovery_candidate,
            },
            "worker_logs": {
                "senders": sender_logs,
                "receivers": receiver_logs,
                "neighbor": neighbor_record,
                "receiver_b_mac": h3_mac_record,
            },
            "operational_checks": operational_checks,
            "passed": operational_ok,
        }
        summary_path = output_dir / "summary.json"
        dump_json(summary_path, summary)

        print("PHASE15_RECOVERY_FAULT_MODEL=p4runtime_multicast_state_deletion")
        print(f"PHASE15_RECOVERY_STATE_ABSENCE_CONFIRMED={absence_ok}")
        print(f"PHASE15_RECOVERY_STATE_RESTORATION_CONFIRMED={restoration_ok}")
        print(f"PHASE15_RECOVERY_BMV2_PID_CONTINUITY={bmv2_pid_continuity}")
        print(
            "PHASE15_RECOVERY_CONTROL_PLANE_RECOVERY_MS="
            f"{state_events['timing_ms']['control_plane_recovery_from_remediation_start']:.6f}"
        )
        for label in ("B", "C"):
            metrics = multicast_metrics[label]
            print(
                f"PHASE15_RECOVERY_{label}_PRE_RATIO="
                f"{metrics['windows']['pre_fault']['delivery_ratio']:.9f}"
            )
            print(
                f"PHASE15_RECOVERY_{label}_FAULT_RATIO="
                f"{metrics['windows']['fault_absent']['delivery_ratio']:.9f}"
            )
            print(
                f"PHASE15_RECOVERY_{label}_POST_RATIO="
                f"{metrics['windows']['post_recovery']['delivery_ratio']:.9f}"
            )
            first_latency = metrics["first_recovered_packet"].get(
                "from_restoration_to_receive_ms"
            )
            print(
                f"PHASE15_RECOVERY_{label}_FIRST_PACKET_MS="
                f"{float(first_latency or 0.0):.6f}"
            )
        print(
            "PHASE15_RECOVERY_CONTROL_FAULT_RATIO="
            f"{control_metrics['windows']['fault_absent']['delivery_ratio']:.9f}"
        )
        print(f"PHASE15_RECOVERY_CANDIDATE_FOUND={recovery_candidate}")
        print(f"PHASE15_RECOVERY_SUMMARY={summary_path}")

        if operational_ok:
            print("PHASE15_S2_P4_STATE_RECOVERY_RUN_OK")
            return 0
        print("PHASE15_S2_P4_STATE_RECOVERY_RUN_FAILED")
        return 1
    finally:
        for _label, _output, _ready, stop, process in receiver_processes:
            if process.poll() is None:
                try:
                    stop.touch()
                except OSError:
                    pass
                process.terminate()
        for process in sender_processes.values():
            if process.poll() is None:
                process.terminate()
        try:
            client.close()
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    """Build the recovery orchestrator command line."""

    parser = argparse.ArgumentParser(
        description="Inject and recover S2 multicast P4Runtime state loss."
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--p4-addr", default="127.0.0.1:9559")
    parser.add_argument("--p4-host", default="127.0.0.1")
    parser.add_argument("--p4-port", type=int, default=9559)
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--p4-outdir", default="/tmp/l2i_minimal")
    parser.add_argument("--p4-timeout-s", type=float, default=5.0)
    parser.add_argument("--group", default="239.1.1.1")
    parser.add_argument("--group-id", type=int, default=1)
    parser.add_argument("--multicast-ports", type=int, nargs="+", default=[1, 2])
    parser.add_argument("--multicast-port", type=int, default=5001)
    parser.add_argument("--control-port", type=int, default=6001)
    parser.add_argument("--multicast-source-namespace", default="h1")
    parser.add_argument("--multicast-source-ip", default="10.0.0.1")
    parser.add_argument("--receiver-b-namespace", default="h3")
    parser.add_argument("--receiver-b-ip", default="10.0.0.3")
    parser.add_argument("--receiver-c-namespace", default="h4")
    parser.add_argument("--receiver-c-ip", default="10.0.0.4")
    parser.add_argument("--control-source-namespace", default="h2")
    parser.add_argument("--control-source-ip", default="10.0.0.2")
    parser.add_argument("--control-source-device", default="h2-eth0")
    parser.add_argument("--control-receiver-namespace", default="h3")
    parser.add_argument("--control-receiver-ip", default="10.0.0.3")
    parser.add_argument("--control-receiver-device", default="h3-eth0")
    parser.add_argument("--control-ingress-port", type=int, default=3)
    parser.add_argument("--control-egress-port", type=int, default=1)
    parser.add_argument("--duration", type=float, default=14.0)
    parser.add_argument("--fault-after-s", type=float, default=4.0)
    parser.add_argument("--fault-hold-s", type=float, default=2.0)
    parser.add_argument("--minimum-post-s", type=float, default=5.0)
    parser.add_argument("--window-guard-s", type=float, default=0.25)
    parser.add_argument("--multicast-rate-mbps", type=float, default=2.0)
    parser.add_argument("--control-rate-mbps", type=float, default=0.5)
    parser.add_argument("--packet-size", type=int, default=1200)
    parser.add_argument("--spin-threshold-us", type=float, default=900.0)
    parser.add_argument("--multicast-sender-cpu", default="auto")
    parser.add_argument("--control-sender-cpu", default="auto-distinct")
    parser.add_argument("--max-abs-rate-error-pct", type=float, default=5.0)
    parser.add_argument("--min-inter-send-ratio", type=float, default=0.98)
    parser.add_argument("--minimum-stable-delivery", type=float, default=0.99)
    parser.add_argument("--minimum-control-delivery", type=float, default=0.99)
    parser.add_argument("--maximum-fault-delivery", type=float, default=0.05)
    parser.add_argument("--minimum-window-packets", type=int, default=20)
    parser.add_argument("--maximum-first-packet-recovery-ms", type=float, default=250.0)
    parser.add_argument("--state-readback-timeout-s", type=float, default=2.0)
    parser.add_argument("--state-poll-interval-s", type=float, default=0.02)
    parser.add_argument("--receiver-drain-s", type=float, default=1.0)
    parser.add_argument("--worker-ready-timeout-s", type=float, default=10.0)
    parser.add_argument("--worker-stop-timeout-s", type=float, default=5.0)
    parser.add_argument("--worker-max-runtime-s", type=float, default=120.0)
    return parser


def main() -> int:
    """Parse arguments and run the recovery experiment."""

    return orchestrate(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
