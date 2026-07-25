#!/usr/bin/env python3
"""Exercise autonomous MAD assurance for S2 multicast P4Runtime state.

The experiment separates the fault source from the assurance controller. An
independent subprocess deletes the multicast table entry and PRE group after a
private delay. The MAD assurance loop receives neither the delay nor a fault
notification. It continuously reads P4Runtime state, confirms drift across
multiple observations, classifies the missing desired-state components,
performs bounded idempotent remediation, and confirms convergence by readback.

A paced unicast flow and its P4 rule remain active throughout the experiment.
They demonstrate that a multicast interruption is caused by selective state
loss rather than by a BMv2 restart, pipeline reload, or link failure.

This foundation validates an autonomous MAD-side reconciliation prototype for
one P4Runtime multicast desired state. It does not claim multi-domain assurance,
BMv2 process-restart recovery, pipeline reload, or production-scale policy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable, NoReturn, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from p4.v1 import p4runtime_pb2

from l2i.assurance import (
    AssurancePolicy,
    MADAssuranceController,
    RemediationOutcome,
    StateObservation,
)
from l2i.third_party.p4rt_min.client import P4RTClient
from scripts.p4_program_s2 import (
    build_mcast_entry,
    build_pre_entity,
    find_required_objects as find_mcast_objects,
    load_p4info,
    table_entity as mcast_table_entity,
)
from scripts.p4_program_unicast import (
    build_entry as build_unicast_entry,
    find_required_objects as find_unicast_objects,
    table_entity as unicast_table_entity,
)
from scripts.s2_p4_state_recovery import (
    allowed_cpus,
    command_record,
    communicate_worker,
    crossing_gap,
    first_recovered_packet,
    multicast_state_readback,
    process_ids,
    resolve_cpu,
    sender_rate_validated,
    tcp_port_available,
    unicast_state_readback,
    update_message,
    wait_for_files,
    wait_for_multicast_state,
    window_metrics,
)


def fail(message: str) -> NoReturn:
    """Terminate with a stable Phase 16 marker."""

    print(f"PHASE16_AUTONOMOUS_ASSURANCE_FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    """Write deterministic UTF-8 JSON evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def wait_for_barrier(path: Path, timeout_s: float) -> bool:
    """Wait for a file barrier without consuming any controller event."""

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        time.sleep(0.01)
    return path.is_file()


def write_upsert_entity(
    client: P4RTClient,
    entity: p4runtime_pb2.Entity,
) -> dict[str, Any]:
    """Apply one entity idempotently using INSERT followed by MODIFY."""

    insert = update_message(p4runtime_pb2.Update.INSERT, entity)
    started_ns = time.monotonic_ns()
    insert_ok, insert_message = client.write([insert])
    if insert_ok:
        return {
            "accepted": True,
            "mode": "INSERT",
            "insert_message": insert_message,
            "modify_message": None,
            "completed_monotonic_ns": time.monotonic_ns(),
            "elapsed_ms": (time.monotonic_ns() - started_ns) / 1_000_000.0,
        }

    modify = update_message(p4runtime_pb2.Update.MODIFY, entity)
    modify_ok, modify_message = client.write([modify])
    completed_ns = time.monotonic_ns()
    return {
        "accepted": bool(modify_ok),
        "mode": "MODIFY" if modify_ok else "FAILED",
        "insert_message": insert_message,
        "modify_message": modify_message,
        "completed_monotonic_ns": completed_ns,
        "elapsed_ms": (completed_ns - started_ns) / 1_000_000.0,
    }


class P4MulticastAssuranceAdapter:
    """Adapt S2 P4Runtime readback and upsert operations to the MAD loop."""

    def __init__(
        self,
        *,
        p4_addr: str,
        device_id: int,
        p4_timeout_s: float,
        observer_election_low: int,
        remediation_election_low: int,
        table: Any,
        match_def: Any,
        action: Any,
        param: Any,
        group_id: int,
        ports: list[int],
        dst_ip: str,
        pre_entity: p4runtime_pb2.Entity,
        table_entity: p4runtime_pb2.Entity,
        forced_remediation_rejections: int = 0,
    ) -> None:
        self.p4_addr = p4_addr
        self.device_id = device_id
        self.p4_timeout_s = p4_timeout_s
        self.remediation_election_low = remediation_election_low
        self.table = table
        self.match_def = match_def
        self.action = action
        self.param = param
        self.group_id = group_id
        self.ports = list(ports)
        self.dst_ip = dst_ip
        self.pre_entity = pre_entity
        self.table_entity = table_entity
        if forced_remediation_rejections < 0:
            raise ValueError("forced_remediation_rejections cannot be negative")
        self.forced_remediation_rejections = int(forced_remediation_rejections)
        self._remaining_forced_rejections = int(forced_remediation_rejections)
        self.observer = P4RTClient(
            address=p4_addr,
            device_id=device_id,
            election_id=(0, observer_election_low),
            timeout_s=p4_timeout_s,
        )
        self.observer.connect()
        self.observer_arbitration = self.observer.ensure_primary(wait_s=2.0)

    def close(self) -> None:
        """Close the long-lived observation session."""

        self.observer.close()

    def observe(self) -> StateObservation:
        """Read multicast state and classify every missing desired component."""

        readback = multicast_state_readback(
            self.observer,
            table=self.table,
            match_def=self.match_def,
            action=self.action,
            param=self.param,
            group_id=self.group_id,
            ports=self.ports,
            dst_ip=self.dst_ip,
        )
        drifts: list[str] = []
        if readback.get("read_ok") is not True:
            drifts.append("readback_error")
        if readback.get("group_present") is not True:
            drifts.append("missing_pre_multicast_group")
        if readback.get("entry_present") is not True:
            drifts.append("missing_multicast_table_entry")

        healthy = bool(
            readback.get("read_ok") is True
            and readback.get("complete_present") is True
        )
        return StateObservation(
            read_ok=readback.get("read_ok") is True,
            healthy=healthy,
            drift_kinds=tuple(drifts),
            observed=readback,
            observed_monotonic_ns=int(
                readback.get("observed_monotonic_ns") or time.monotonic_ns()
            ),
            message=(
                "desired multicast state is converged"
                if healthy
                else "desired multicast state diverged"
            ),
        )

    def remediate(
        self,
        observation: StateObservation,
        incident_id: int,
        attempt: int,
    ) -> RemediationOutcome:
        """Reapply only divergent components through a fresh primary session."""

        election_low = (
            self.remediation_election_low
            + incident_id * 100
            + attempt
        )

        # Validation can request a bounded synthetic backend rejection. This
        # exercises the generic retry and backoff state machine without sharing
        # the independent fault schedule with the assurance controller.
        if self._remaining_forced_rejections > 0:
            self._remaining_forced_rejections -= 1
            return RemediationOutcome(
                accepted=False,
                details={
                    "election_low": election_low,
                    "forced_test_rejection": True,
                    "remaining_forced_rejections": (
                        self._remaining_forced_rejections
                    ),
                    "incident_id": incident_id,
                    "attempt": attempt,
                    "drift_kinds": list(observation.drift_kinds),
                },
            )
        client = P4RTClient(
            address=self.p4_addr,
            device_id=self.device_id,
            election_id=(0, election_low),
            timeout_s=self.p4_timeout_s,
        )
        started_ns = time.monotonic_ns()
        try:
            client.connect()
            arbitration = client.ensure_primary(wait_s=2.0)
            if not arbitration.ok or not arbitration.is_primary:
                return RemediationOutcome(
                    accepted=False,
                    details={
                        "election_low": election_low,
                        "arbitration_ok": arbitration.ok,
                        "is_primary": arbitration.is_primary,
                        "arbitration_message": arbitration.message,
                    },
                )

            observed = dict(observation.observed)
            operations: list[dict[str, Any]] = []

            if observed.get("group_present") is not True:
                group_result = write_upsert_entity(client, self.pre_entity)
                operations.append(
                    {"component": "pre_multicast_group", **group_result}
                )

            if observed.get("entry_present") is not True:
                table_result = write_upsert_entity(client, self.table_entity)
                operations.append(
                    {"component": "multicast_table_entry", **table_result}
                )

            accepted = bool(operations) and all(
                item.get("accepted") is True
                for item in operations
            )
            completed_ns = time.monotonic_ns()
            return RemediationOutcome(
                accepted=accepted,
                completed_monotonic_ns=completed_ns,
                details={
                    "election_low": election_low,
                    "arbitration_ok": arbitration.ok,
                    "is_primary": arbitration.is_primary,
                    "drift_kinds": list(observation.drift_kinds),
                    "operations": operations,
                    "elapsed_ms": (completed_ns - started_ns) / 1_000_000.0,
                    "idempotent_component_upsert": True,
                },
            )
        finally:
            client.close()


def program_initial_state(
    *,
    args: argparse.Namespace,
    mcast_state_kwargs: dict[str, Any],
    pre_entity: p4runtime_pb2.Entity,
    pre_delete_key: p4runtime_pb2.Entity,
    table_entity: p4runtime_pb2.Entity,
    table_delete_key: p4runtime_pb2.Entity,
    unicast_entry_entity: p4runtime_pb2.Entity,
    unicast_delete_entity: p4runtime_pb2.Entity,
    unicast_state_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Remove stale test state, upsert desired state, and verify convergence."""

    client = P4RTClient(
        address=args.p4_addr,
        device_id=args.device_id,
        election_id=(0, args.initial_program_election_low),
        timeout_s=args.p4_timeout_s,
    )
    try:
        client.connect()
        arbitration = client.ensure_primary(wait_s=2.0)
        if not arbitration.ok or not arbitration.is_primary:
            fail(f"initial P4Runtime arbitration failed: {arbitration.message}")

        existing_mcast = multicast_state_readback(client, **mcast_state_kwargs)
        existing_unicast = unicast_state_readback(client, **unicast_state_kwargs)
        stale_updates: list[p4runtime_pb2.Update] = []
        if existing_mcast.get("entry_present") is True:
            stale_updates.append(
                update_message(p4runtime_pb2.Update.DELETE, table_delete_key)
            )
        if existing_mcast.get("group_present") is True:
            stale_updates.append(
                update_message(p4runtime_pb2.Update.DELETE, pre_delete_key)
            )
        if existing_unicast.get("present") is True:
            stale_updates.append(
                update_message(p4runtime_pb2.Update.DELETE, unicast_delete_entity)
            )
        stale_ok = True
        stale_message = "no stale state"
        if stale_updates:
            stale_ok, stale_message = client.write(stale_updates)
        if not stale_ok:
            fail(f"stale-state cleanup failed: {stale_message}")

        writes = {
            "pre": write_upsert_entity(client, pre_entity),
            "multicast_table": write_upsert_entity(client, table_entity),
            "unicast_table": write_upsert_entity(client, unicast_entry_entity),
        }
        if not all(item.get("accepted") is True for item in writes.values()):
            fail("initial desired-state programming failed")

        multicast = multicast_state_readback(client, **mcast_state_kwargs)
        unicast = unicast_state_readback(client, **unicast_state_kwargs)
        converged = bool(
            multicast.get("complete_present") is True
            and unicast.get("present") is True
        )
        if not converged:
            fail("initial desired state was not confirmed by readback")

        return {
            "arbitration": {
                "ok": arbitration.ok,
                "is_primary": arbitration.is_primary,
                "message": arbitration.message,
            },
            "stale_cleanup": {
                "update_count": len(stale_updates),
                "ok": stale_ok,
                "message": stale_message,
            },
            "writes": writes,
            "multicast_readback": multicast,
            "unicast_readback": unicast,
            "converged": converged,
        }
    finally:
        client.close()


def expected_fault_state(fault_kind: str) -> dict[str, bool]:
    """Return the expected multicast component presence after one fault."""

    states = {
        "none": {
            "group_present": True,
            "entry_present": True,
        },
        "pre_only": {
            "group_present": False,
            "entry_present": True,
        },
        "table_only": {
            "group_present": True,
            "entry_present": False,
        },
        "both": {
            "group_present": False,
            "entry_present": False,
        },
    }
    try:
        return states[fault_kind]
    except KeyError as exc:
        raise ValueError(f"unsupported fault kind: {fault_kind}") from exc


def expected_drift_kinds(fault_kind: str) -> set[str]:
    """Map one selective state fault to its exact assurance classification."""

    mapping = {
        "none": set(),
        "pre_only": {"missing_pre_multicast_group"},
        "table_only": {"missing_multicast_table_entry"},
        "both": {
            "missing_pre_multicast_group",
            "missing_multicast_table_entry",
        },
    }
    try:
        return set(mapping[fault_kind])
    except KeyError as exc:
        raise ValueError(f"unsupported fault kind: {fault_kind}") from exc


def expected_remediation_components(fault_kind: str) -> set[str]:
    """Map one fault to the component upserts required for convergence."""

    mapping = {
        "none": set(),
        "pre_only": {"pre_multicast_group"},
        "table_only": {"multicast_table_entry"},
        "both": {
            "pre_multicast_group",
            "multicast_table_entry",
        },
    }
    try:
        return set(mapping[fault_kind])
    except KeyError as exc:
        raise ValueError(f"unsupported fault kind: {fault_kind}") from exc


def wait_for_fault_state(
    client: P4RTClient,
    *,
    fault_kind: str,
    timeout_s: float,
    poll_interval_s: float,
    state_kwargs: dict[str, Any],
) -> tuple[bool, dict[str, Any], int]:
    """Poll until the exact component-selective fault state is observed."""

    expected = expected_fault_state(fault_kind)
    deadline = time.monotonic() + timeout_s
    polls = 0
    last: dict[str, Any] = {}

    while time.monotonic() < deadline:
        polls += 1
        last = multicast_state_readback(client, **state_kwargs)
        if (
            last.get("read_ok") is True
            and bool(last.get("group_present"))
            is expected["group_present"]
            and bool(last.get("entry_present"))
            is expected["entry_present"]
        ):
            return True, last, polls
        time.sleep(poll_interval_s)

    polls += 1
    last = multicast_state_readback(client, **state_kwargs)
    matched = bool(
        last.get("read_ok") is True
        and bool(last.get("group_present"))
        is expected["group_present"]
        and bool(last.get("entry_present"))
        is expected["entry_present"]
    )
    return matched, last, polls


def inject_fault(args: argparse.Namespace) -> int:
    """Apply one independent component-selective fault after a file barrier."""

    output_path = Path(args.output)
    barrier_path = Path(args.start_barrier)
    if not wait_for_barrier(barrier_path, args.barrier_timeout_s):
        fail("fault injector did not observe the traffic-start barrier")

    barrier_observed_ns = time.monotonic_ns()
    time.sleep(args.delay_s)

    p4info = load_p4info(
        Path(args.p4_outdir) / "l2i_minimal.p4info.txtpb"
    )
    table, match_def, action, param = find_mcast_objects(p4info)
    table_delete = mcast_table_entity(
        build_mcast_entry(
            table=table,
            match_def=match_def,
            action=action,
            param=param,
            dst_ip=args.group,
            group_id=args.group_id,
            include_action=False,
        )
    )
    pre_delete = build_pre_entity(
        args.group_id,
        args.multicast_ports,
        include_replicas=False,
    )
    state_kwargs = {
        "table": table,
        "match_def": match_def,
        "action": action,
        "param": param,
        "group_id": args.group_id,
        "ports": args.multicast_ports,
        "dst_ip": args.group,
    }

    client = P4RTClient(
        address=args.p4_addr,
        device_id=args.device_id,
        election_id=(0, args.election_low),
        timeout_s=args.p4_timeout_s,
    )
    try:
        client.connect()
        arbitration = client.ensure_primary(wait_s=2.0)
        if not arbitration.ok or not arbitration.is_primary:
            fail(f"fault injector arbitration failed: {arbitration.message}")

        pre_readback = multicast_state_readback(client, **state_kwargs)
        updates: list[p4runtime_pb2.Update] = []
        deleted_components: list[str] = []

        if args.fault_kind in {"table_only", "both"}:
            if pre_readback.get("entry_present") is not True:
                fail("multicast table entry was absent before fault injection")
            updates.append(
                update_message(p4runtime_pb2.Update.DELETE, table_delete)
            )
            deleted_components.append("multicast_table_entry")

        if args.fault_kind in {"pre_only", "both"}:
            if pre_readback.get("group_present") is not True:
                fail("PRE multicast group was absent before fault injection")
            updates.append(
                update_message(p4runtime_pb2.Update.DELETE, pre_delete)
            )
            deleted_components.append("pre_multicast_group")

        write_started_ns = time.monotonic_ns()
        if updates:
            write_ok, write_message = client.write(updates)
        else:
            write_ok, write_message = True, "no-fault control: no P4 write"
        write_completed_ns = time.monotonic_ns()
        if not write_ok:
            fail(f"fault injection write failed: {write_message}")

        effect_ok, effect_readback, effect_polls = wait_for_fault_state(
            client,
            fault_kind=args.fault_kind,
            timeout_s=args.state_readback_timeout_s,
            poll_interval_s=args.state_poll_interval_s,
            state_kwargs=state_kwargs,
        )
        effect_confirmed_ns = int(
            effect_readback.get("observed_monotonic_ns")
            or time.monotonic_ns()
        )
        absence_confirmed = bool(
            args.fault_kind != "none" and effect_ok
        )
        evidence = {
            "fault_source": "independent_p4runtime_subprocess",
            "fault_kind": args.fault_kind,
            "fault_injected": args.fault_kind != "none",
            "controller_notification_sent": False,
            "start_barrier": str(barrier_path),
            "barrier_observed_monotonic_ns": barrier_observed_ns,
            "configured_delay_s": args.delay_s,
            "pre_fault_readback": pre_readback,
            "deleted_components": deleted_components,
            "write_update_count": len(updates),
            "write_started_monotonic_ns": write_started_ns,
            "write_completed_monotonic_ns": write_completed_ns,
            "write_ok": write_ok,
            "write_message": write_message,
            "fault_effect_confirmed": effect_ok,
            "fault_effect_confirmed_monotonic_ns": effect_confirmed_ns,
            "fault_effect_poll_count": effect_polls,
            "fault_effect_readback": effect_readback,
            # Preserve the original foundation fields for backward-compatible
            # evidence validation when the default combined fault is used.
            "absence_confirmed": absence_confirmed,
            "absence_confirmed_monotonic_ns": effect_confirmed_ns,
            "absence_poll_count": effect_polls,
            "absence_readback": effect_readback,
            "election_low": args.election_low,
        }
        dump_json(output_path, evidence)
        print(f"PHASE16_FAULT_INJECTOR_KIND={args.fault_kind}")
        print(f"PHASE16_FAULT_INJECTOR_WRITE_OK={write_ok}")
        print(f"PHASE16_FAULT_INJECTOR_EFFECT_CONFIRMED={effect_ok}")
        print(
            "PHASE16_FAULT_INJECTOR_ABSENCE_CONFIRMED="
            f"{absence_confirmed}"
        )
        print("PHASE16_FAULT_INJECTOR_CONTROLLER_NOTIFICATION_SENT=False")
        print(f"PHASE16_FAULT_INJECTOR_EVIDENCE={output_path}")
        if not effect_ok:
            return 1
        print("PHASE16_INDEPENDENT_FAULT_INJECTOR_OK")
        return 0
    finally:
        client.close()


def event_by_type(
    controller_snapshot: dict[str, Any],
    event_type: str,
) -> dict[str, Any] | None:
    """Return the first controller event with one exact type."""

    return next(
        (
            event
            for event in controller_snapshot.get("events") or []
            if event.get("event_type") == event_type
        ),
        None,
    )


def orchestrate(args: argparse.Namespace) -> int:
    """Run one independent fault and autonomous reconciliation experiment."""

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    control_dir = output_dir / "control"
    control_dir.mkdir(parents=True, exist_ok=True)

    fault_expected = args.fault_kind != "none"
    if fault_expected and args.duration <= args.fault_after_s + args.minimum_post_s:
        fail("duration must leave the configured minimum post-recovery interval")
    if args.fault_after_s <= args.window_guard_s:
        fail("fault-after interval must exceed the measurement guard")
    if args.assurance_forced_remediation_rejections < 0:
        fail("forced remediation rejections cannot be negative")
    if (
        not fault_expected
        and args.assurance_forced_remediation_rejections != 0
    ):
        fail("no-fault control cannot request remediation rejection")

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

    p4info = load_p4info(
        Path(args.p4_outdir) / "l2i_minimal.p4info.txtpb"
    )
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
    mcast_delete = build_mcast_entry(
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
    pre_delete = build_pre_entity(
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
    unicast_delete = build_unicast_entry(
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
    unicast_state_kwargs = {
        "table": unicast_table,
        "match_def": unicast_match,
        "action": unicast_action,
        "param": unicast_param,
        "ingress_port": args.control_ingress_port,
        "egress_port": args.control_egress_port,
    }

    initial_pids = process_ids("[s]imple_switch_grpc")
    initial_port_ok = tcp_port_available(args.p4_host, args.p4_port)
    if not initial_pids or not initial_port_ok:
        fail("BMv2 process and P4Runtime listener must already be active")

    initial_state = program_initial_state(
        args=args,
        mcast_state_kwargs=mcast_state_kwargs,
        pre_entity=pre_entity,
        pre_delete_key=pre_delete,
        table_entity=mcast_table_entity(mcast_entry),
        table_delete_key=mcast_table_entity(mcast_delete),
        unicast_entry_entity=unicast_table_entity(unicast_entry),
        unicast_delete_entity=unicast_table_entity(unicast_delete),
        unicast_state_kwargs=unicast_state_kwargs,
    )

    adapter = P4MulticastAssuranceAdapter(
        p4_addr=args.p4_addr,
        device_id=args.device_id,
        p4_timeout_s=args.p4_timeout_s,
        observer_election_low=args.observer_election_low,
        remediation_election_low=args.remediation_election_low,
        table=mcast_table,
        match_def=mcast_match,
        action=mcast_action,
        param=mcast_param,
        group_id=args.group_id,
        ports=args.multicast_ports,
        dst_ip=args.group,
        pre_entity=pre_entity,
        table_entity=mcast_table_entity(mcast_entry),
        forced_remediation_rejections=(
            args.assurance_forced_remediation_rejections
        ),
    )
    policy = AssurancePolicy(
        poll_interval_s=args.assurance_poll_interval_s,
        drift_confirmations=args.assurance_drift_confirmations,
        convergence_confirmations=args.assurance_convergence_confirmations,
        maximum_remediation_attempts=args.assurance_maximum_remediation_attempts,
        initial_backoff_s=args.assurance_initial_backoff_s,
        backoff_multiplier=args.assurance_backoff_multiplier,
        maximum_backoff_s=args.assurance_maximum_backoff_s,
        maximum_consecutive_observation_errors=(
            args.assurance_maximum_consecutive_observation_errors
        ),
    )
    desired_state = {
        "kind": "p4runtime_multicast_state",
        "destination": args.group,
        "group_id": args.group_id,
        "egress_ports": list(args.multicast_ports),
        "table": str(mcast_table.preamble.name),
        "source": "s2_intent_derived_parameters",
    }
    controller = MADAssuranceController(
        controller_id="mad-s2-p4-multicast-assurance-v1",
        desired_state=desired_state,
        observe_fn=adapter.observe,
        remediate_fn=adapter.remediate,
        policy=policy,
    )
    assurance_stop = threading.Event()
    assurance_thread = threading.Thread(
        target=controller.run,
        kwargs={
            "stop_event": assurance_stop,
            "maximum_runtime_s": args.assurance_maximum_runtime_s,
        },
        name="mad_s2_assurance_loop",
        daemon=True,
    )

    receiver_processes: list[tuple[str, Path, Path, Path, subprocess.Popen[str]]] = []
    sender_processes: dict[str, subprocess.Popen[str]] = {}
    injector_process: subprocess.Popen[str] | None = None
    injector_stdout = ""
    injector_stderr = ""
    controller_snapshot: dict[str, Any] = {}
    cleanup_state: dict[str, Any] = {}

    python = str(Path(sys.executable))
    smoke_script = REPO_ROOT / "scripts" / "s2_multicast_dataplane_smoke.py"
    qos_script = REPO_ROOT / "scripts" / "s2_p4_qos_contention.py"
    this_script = Path(__file__).resolve()

    try:
        assurance_thread.start()
        if not controller.initial_convergence_event.wait(
            args.assurance_initial_convergence_timeout_s
        ):
            fail("MAD assurance did not confirm initial desired state")

        for label, namespace, interface_ip in (
            ("B", args.receiver_b_namespace, args.receiver_b_ip),
            ("C", args.receiver_c_namespace, args.receiver_c_ip),
        ):
            output = output_dir / f"multicast_receiver_{label}.json"
            ready = control_dir / f"multicast_receiver_{label}.ready.json"
            stop = control_dir / f"multicast_receiver_{label}.stop"
            ready.unlink(missing_ok=True)
            stop.unlink(missing_ok=True)
            process = subprocess.Popen(
                [
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
                ],
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
        control_receiver_process = subprocess.Popen(
            [
                "ip", "netns", "exec", args.control_receiver_namespace,
                python, str(qos_script), "background-receiver",
                "--interface-ip", args.control_receiver_ip,
                "--port", str(args.control_port),
                "--max-runtime-s", str(args.worker_max_runtime_s),
                "--ready-file", str(control_ready),
                "--stop-file", str(control_stop),
                "--output", str(control_receiver_output),
            ],
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

        traffic_barrier = control_dir / "traffic-start.barrier"
        traffic_barrier.unlink(missing_ok=True)
        injector_output = output_dir / "fault-injector.json"
        injector_process = subprocess.Popen(
            [
                python, str(this_script), "inject-fault",
                "--output", str(injector_output),
                "--start-barrier", str(traffic_barrier),
                "--barrier-timeout-s", str(args.worker_ready_timeout_s),
                "--delay-s", str(args.fault_after_s),
                "--fault-kind", args.fault_kind,
                "--p4-addr", args.p4_addr,
                "--device-id", str(args.device_id),
                "--p4-outdir", args.p4_outdir,
                "--p4-timeout-s", str(args.p4_timeout_s),
                "--group", args.group,
                "--group-id", str(args.group_id),
                "--multicast-ports",
                *[str(port) for port in args.multicast_ports],
                "--election-low", str(args.injector_election_low),
                "--state-readback-timeout-s", str(args.state_readback_timeout_s),
                "--state-poll-interval-s", str(args.state_poll_interval_s),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        multicast_sender_output = output_dir / "multicast_sender.json"
        control_sender_output = output_dir / "control_sender.json"
        sender_processes["control"] = subprocess.Popen(
            [
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
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sender_processes["multicast"] = subprocess.Popen(
            [
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
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        traffic_processes_started_ns = time.monotonic_ns()
        traffic_barrier.touch()

        injector_stdout, injector_stderr = communicate_worker(
            injector_process,
            args.worker_max_runtime_s,
        )
        if injector_stdout:
            print(injector_stdout, end="")
        if injector_stderr:
            print(injector_stderr, end="", file=sys.stderr)
        if injector_process.returncode != 0:
            fail("independent fault injector failed")

        if fault_expected and not controller.first_recovery_event.wait(
            args.assurance_recovery_timeout_s
        ):
            snapshot = controller.snapshot()
            fail(
                "MAD assurance did not autonomously restore desired state; "
                f"state={snapshot.get('state')} failure={snapshot.get('failure')}"
            )

        if not fault_expected:
            # Keep observing after the independent no-op injector completes so
            # the control condition can detect false-positive incidents.
            stable_observation_s = min(
                max(args.assurance_poll_interval_s * 5.0, 0.10),
                max(args.duration - args.fault_after_s, 0.10),
            )
            time.sleep(stable_observation_s)

        unicast_during_incident = unicast_state_readback(
            adapter.observer,
            **unicast_state_kwargs,
        )
        pids_during_incident = process_ids("[s]imple_switch_grpc")
        port_during_incident = tcp_port_available(args.p4_host, args.p4_port)

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

        assurance_stop.set()
        assurance_thread.join(timeout=args.assurance_stop_timeout_s)
        if assurance_thread.is_alive():
            fail("MAD assurance loop did not stop within its deadline")
        controller_snapshot = controller.snapshot()
        dump_json(output_dir / "assurance-controller.json", controller_snapshot)

        required_outputs = (
            multicast_sender_output,
            control_sender_output,
            output_dir / "multicast_receiver_B.json",
            output_dir / "multicast_receiver_C.json",
            control_receiver_output,
            injector_output,
        )
        if not all(path.is_file() for path in required_outputs):
            fail("one or more traffic or injector evidence files are missing")

        multicast_sender = json.loads(
            multicast_sender_output.read_text(encoding="utf-8")
        )
        control_sender = json.loads(
            control_sender_output.read_text(encoding="utf-8")
        )
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
        injector = json.loads(injector_output.read_text(encoding="utf-8"))

        events = controller_snapshot.get("events") or []
        drift_events = [
            event
            for event in events
            if event.get("event_type") == "drift_confirmed"
        ]
        remediation_started_events = [
            event
            for event in events
            if event.get("event_type") == "remediation_attempt_started"
        ]
        remediation_completed_events = [
            event
            for event in events
            if event.get("event_type") == "remediation_attempt_completed"
        ]
        convergence_events = [
            event
            for event in events
            if event.get("event_type") == "convergence_confirmed"
        ]
        backoff_events = [
            event
            for event in events
            if event.get("event_type") == "remediation_backoff_started"
        ]
        forced_rejection_events = [
            event
            for event in remediation_completed_events
            if (event.get("details") or {}).get("accepted") is False
            and (
                (event.get("details") or {}).get("details")
                or {}
            ).get("forced_test_rejection") is True
        ]
        accepted_remediation_events = [
            event
            for event in remediation_completed_events
            if (event.get("details") or {}).get("accepted") is True
        ]

        expected_drift = expected_drift_kinds(args.fault_kind)
        expected_components = expected_remediation_components(args.fault_kind)
        observed_drift = set()
        remediated_components: set[str] = set()

        if drift_events:
            observed_drift = set(
                (drift_events[0].get("details") or {}).get("drift_kinds")
                or []
            )

        for event in accepted_remediation_events:
            outcome_details = (
                (event.get("details") or {}).get("details")
                or {}
            )
            for operation in outcome_details.get("operations") or []:
                component = operation.get("component")
                if component:
                    remediated_components.add(str(component))

        multicast_sender_timeline = multicast_sender.get("packet_send_timeline") or []
        control_sender_timeline = control_sender.get("packet_send_timeline") or []
        first_mcast_send_ns = min(
            int(item["send_monotonic_ns"])
            for item in multicast_sender_timeline
            if bool(item.get("sent"))
        )
        last_mcast_send_ns = max(
            int(item["send_monotonic_ns"])
            for item in multicast_sender_timeline
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

        timing_ms: dict[str, float | None]
        multicast_windows: dict[str, tuple[int, int]]
        control_windows: dict[str, tuple[int, int]]
        convergence_confirmed_ns: int | None = None
        remediation_started_ns: int | None = None
        fault_effect_confirmed_ns = int(
            injector.get("fault_effect_confirmed_monotonic_ns")
            or injector.get("absence_confirmed_monotonic_ns")
            or time.monotonic_ns()
        )
        fault_write_completed_ns = int(
            injector.get("write_completed_monotonic_ns")
            or time.monotonic_ns()
        )

        if fault_expected:
            if (
                len(drift_events) != 1
                or not remediation_started_events
                or len(convergence_events) != 1
            ):
                fail("controller transition evidence is incomplete")

            drift_confirmed_ns = int(drift_events[0]["monotonic_ns"])
            remediation_started_ns = int(
                remediation_started_events[0]["monotonic_ns"]
            )
            convergence_confirmed_ns = int(
                convergence_events[0]["monotonic_ns"]
            )
            timing_ms = {
                "fault_write_to_drift_confirmation": (
                    drift_confirmed_ns - fault_write_completed_ns
                )
                / 1_000_000.0,
                "drift_confirmation_to_remediation_start": (
                    remediation_started_ns - drift_confirmed_ns
                )
                / 1_000_000.0,
                "remediation_start_to_convergence": (
                    convergence_confirmed_ns - remediation_started_ns
                )
                / 1_000_000.0,
                "fault_write_to_convergence": (
                    convergence_confirmed_ns - fault_write_completed_ns
                )
                / 1_000_000.0,
            }
            multicast_windows = {
                "pre_fault": (
                    first_mcast_send_ns + guard_ns,
                    int(injector["write_started_monotonic_ns"]),
                ),
                "autonomous_outage": (
                    fault_effect_confirmed_ns,
                    convergence_confirmed_ns,
                ),
                "post_recovery": (
                    convergence_confirmed_ns + guard_ns,
                    last_mcast_send_ns + 1,
                ),
            }
            control_windows = {
                "pre_fault": (
                    first_control_send_ns + guard_ns,
                    int(injector["write_started_monotonic_ns"]),
                ),
                "autonomous_outage": (
                    fault_effect_confirmed_ns,
                    convergence_confirmed_ns,
                ),
                "post_recovery": (
                    convergence_confirmed_ns + guard_ns,
                    last_control_send_ns + 1,
                ),
            }
        else:
            timing_ms = {
                "fault_write_to_drift_confirmation": None,
                "drift_confirmation_to_remediation_start": None,
                "remediation_start_to_convergence": None,
                "fault_write_to_convergence": None,
            }
            multicast_windows = {
                "stable_full": (
                    first_mcast_send_ns + guard_ns,
                    last_mcast_send_ns + 1,
                ),
            }
            control_windows = {
                "stable_full": (
                    first_control_send_ns + guard_ns,
                    last_control_send_ns + 1,
                ),
            }

        multicast_metrics: dict[str, Any] = {}
        for label, receiver in multicast_receivers.items():
            receiver_timeline = receiver.get("packet_receive_timeline") or []
            metrics: dict[str, Any] = {
                "windows": {
                    name: window_metrics(
                        multicast_sender_timeline,
                        receiver_timeline,
                        start_ns=start_ns,
                        end_ns=end_ns,
                    )
                    for name, (start_ns, end_ns) in multicast_windows.items()
                },
                "malformed": int(receiver.get("malformed") or 0),
                "duplicates": int(receiver.get("duplicates") or 0),
                "lifecycle": receiver.get("lifecycle") or {},
            }
            if fault_expected:
                metrics["first_recovered_packet"] = first_recovered_packet(
                    receiver_timeline,
                    int(convergence_confirmed_ns),
                    remediation_started_ns=int(remediation_started_ns),
                    fault_injection_started_ns=fault_write_completed_ns,
                )
                metrics["crossing_gap"] = crossing_gap(
                    receiver_timeline,
                    absence_confirmed_ns=fault_effect_confirmed_ns,
                    restoration_confirmed_ns=int(convergence_confirmed_ns),
                )
            else:
                metrics["first_recovered_packet"] = {
                    "found": False,
                    "not_applicable": True,
                }
                metrics["crossing_gap"] = {
                    "found": False,
                    "not_applicable": True,
                    "sequence_gap": 0,
                }
            multicast_metrics[label] = metrics

        control_receiver_timeline = control_receiver.get(
            "packet_receive_timeline"
        ) or []
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
        bmv2_pid_continuity = bool(
            initial_pids == pids_during_incident == final_pids
            and initial_pids
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
            for process in [
                *sender_processes.values(),
                *[item[4] for item in receiver_processes],
            ]
        )

        common_candidate_checks = {
            "independent_fault_injector": bool(
                injector.get("fault_source")
                == "independent_p4runtime_subprocess"
                and injector.get("controller_notification_sent") is False
                and injector.get("fault_kind") == args.fault_kind
            ),
            "fault_effect_confirmed": (
                injector.get("fault_effect_confirmed") is True
            ),
            "unicast_rule_continuity": (
                unicast_during_incident.get("present") is True
            ),
            "bmv2_process_continuity": bmv2_pid_continuity,
            "p4runtime_continuity": bool(
                initial_port_ok and port_during_incident and final_port_ok
            ),
        }

        if fault_expected:
            candidate_checks = {
                **common_candidate_checks,
                "state_absence_confirmed": (
                    injector.get("absence_confirmed") is True
                ),
                "autonomous_drift_detected": bool(
                    controller_snapshot.get("incident_count") == 1
                    and controller.drift_detected_event.is_set()
                ),
                "drift_classification_exact": (
                    observed_drift == expected_drift
                ),
                "component_selective_remediation_exact": (
                    remediated_components == expected_components
                ),
                "forced_rejection_count_exact": (
                    len(forced_rejection_events)
                    == args.assurance_forced_remediation_rejections
                ),
                "remediation_attempt_count_exact": (
                    controller_snapshot.get("remediation_attempt_count")
                    == 1 + args.assurance_forced_remediation_rejections
                ),
                "retry_backoff_behavior_exact": (
                    (
                        args.assurance_forced_remediation_rejections == 0
                        and len(backoff_events) == 0
                    )
                    or (
                        args.assurance_forced_remediation_rejections > 0
                        and len(backoff_events)
                        >= args.assurance_forced_remediation_rejections
                    )
                ),
                "autonomous_convergence_confirmed": bool(
                    controller_snapshot.get("successful_convergence_count") == 1
                    and controller.first_recovery_event.is_set()
                ),
                "bounded_detection": (
                    float(timing_ms["fault_write_to_drift_confirmation"])
                    <= args.maximum_detection_ms
                ),
                "bounded_control_plane_recovery": (
                    float(timing_ms["remediation_start_to_convergence"])
                    <= args.maximum_control_plane_recovery_ms
                ),
                "bounded_total_reconciliation": (
                    float(timing_ms["fault_write_to_convergence"])
                    <= args.maximum_total_reconciliation_ms
                ),
                "multicast_pre_fault_delivery": all(
                    multicast_metrics[label]["windows"]["pre_fault"][
                        "delivery_ratio"
                    ]
                    >= args.minimum_stable_delivery
                    for label in ("B", "C")
                ),
                "multicast_fault_effect_observed": all(
                    multicast_metrics[label]["crossing_gap"].get("found") is True
                    and int(
                        multicast_metrics[label]["crossing_gap"].get(
                            "sequence_gap"
                        )
                        or 0
                    )
                    >= args.minimum_lost_packets
                    for label in ("B", "C")
                ),
                "multicast_post_recovery_delivery": all(
                    multicast_metrics[label]["windows"]["post_recovery"][
                        "delivery_ratio"
                    ]
                    >= args.minimum_stable_delivery
                    for label in ("B", "C")
                ),
                "first_packet_recovered": all(
                    multicast_metrics[label]["first_recovered_packet"].get(
                        "found"
                    )
                    is True
                    and float(
                        multicast_metrics[label]["first_recovered_packet"].get(
                            "from_restoration_to_receive_ms"
                        )
                        if multicast_metrics[label]["first_recovered_packet"].get(
                            "from_restoration_to_receive_ms"
                        )
                        is not None
                        else float("inf")
                    )
                    <= args.maximum_first_packet_recovery_ms
                    for label in ("B", "C")
                ),
                "unicast_control_stable": all(
                    control_metrics["windows"][name]["expected_packets"] >= 1
                    and control_metrics["windows"][name]["delivery_ratio"]
                    >= args.minimum_control_delivery
                    for name in (
                        "pre_fault",
                        "autonomous_outage",
                        "post_recovery",
                    )
                ),
            }
        else:
            candidate_checks = {
                **common_candidate_checks,
                "no_fault_state_remained_converged": bool(
                    injector.get("fault_injected") is False
                    and (
                        injector.get("fault_effect_readback")
                        or {}
                    ).get("complete_present")
                    is True
                ),
                "no_false_positive_incident": bool(
                    controller_snapshot.get("incident_count") == 0
                    and controller_snapshot.get("remediation_attempt_count") == 0
                    and controller_snapshot.get("successful_convergence_count") == 0
                    and not drift_events
                    and not remediation_started_events
                    and not convergence_events
                    and not backoff_events
                ),
                "stable_multicast_delivery": all(
                    multicast_metrics[label]["windows"]["stable_full"][
                        "delivery_ratio"
                    ]
                    >= args.minimum_stable_delivery
                    for label in ("B", "C")
                ),
                "stable_unicast_control": bool(
                    control_metrics["windows"]["stable_full"][
                        "expected_packets"
                    ]
                    >= 1
                    and control_metrics["windows"]["stable_full"][
                        "delivery_ratio"
                    ]
                    >= args.minimum_control_delivery
                ),
            }

        candidate_found = all(candidate_checks.values())

        cleanup_client = P4RTClient(
            address=args.p4_addr,
            device_id=args.device_id,
            election_id=(0, args.cleanup_election_low),
            timeout_s=args.p4_timeout_s,
        )
        try:
            cleanup_client.connect()
            cleanup_arbitration = cleanup_client.ensure_primary(wait_s=2.0)
            cleanup_write_ok, cleanup_write_message = cleanup_client.write(
                [
                    update_message(
                        p4runtime_pb2.Update.DELETE,
                        mcast_table_entity(mcast_delete),
                    ),
                    update_message(p4runtime_pb2.Update.DELETE, pre_delete),
                    update_message(
                        p4runtime_pb2.Update.DELETE,
                        unicast_table_entity(unicast_delete),
                    ),
                ]
            )
            cleanup_mcast = multicast_state_readback(
                cleanup_client,
                **mcast_state_kwargs,
            )
            cleanup_unicast = unicast_state_readback(
                cleanup_client,
                **unicast_state_kwargs,
            )
            cleanup_state = {
                "arbitration_ok": cleanup_arbitration.ok,
                "is_primary": cleanup_arbitration.is_primary,
                "write_ok": cleanup_write_ok,
                "write_message": cleanup_write_message,
                "multicast_absent": cleanup_mcast.get("complete_absent") is True,
                "unicast_absent": cleanup_unicast.get("present") is False,
                "multicast_readback": cleanup_mcast,
                "unicast_readback": cleanup_unicast,
            }
        finally:
            cleanup_client.close()

        operational_checks = {
            "initial_state_converged": initial_state.get("converged") is True,
            "controller_initial_convergence": (
                controller.initial_convergence_event.is_set()
            ),
            "controller_not_failed": controller.failed_event.is_set() is False,
            "controller_stopped": controller_snapshot.get("state") == "stopped",
            "injector_exit_ok": injector_process.returncode == 0,
            "receiver_readiness_ok": readiness_ok,
            "worker_returncodes_ok": worker_returncodes_ok,
            "receiver_lifecycle_ok": receiver_lifecycle_ok,
            "receiver_integrity_ok": receiver_integrity_ok,
            "multicast_sender_rate_ok": multicast_rate_ok,
            "control_sender_rate_ok": control_rate_ok,
            "cleanup_write_ok": cleanup_state.get("write_ok") is True,
            "cleanup_multicast_absent": cleanup_state.get("multicast_absent") is True,
            "cleanup_unicast_absent": cleanup_state.get("unicast_absent") is True,
        }
        operational_ok = all(operational_checks.values())

        summary = {
            "scenario": "S2_P4_multicast_autonomous_assurance",
            "assurance_profile_id": args.assurance_profile_id,
            "fault_model": "independent_p4runtime_multicast_state_deletion",
            "scope": {
                "desired_state_supplied_by_s2_orchestration": True,
                "persistent_mad_assurance_loop_exercised": True,
                "autonomous_mad_detection_exercised": fault_expected,
                "autonomous_mad_recovery_exercised": fault_expected,
                "no_fault_false_positive_control_exercised": (
                    not fault_expected
                ),
                "component_selective_drift_classification_exercised": (
                    args.fault_kind in {"pre_only", "table_only"}
                ),
                "synthetic_remediation_rejection_exercised": (
                    args.assurance_forced_remediation_rejections > 0
                ),
                "retry_and_backoff_execution_exercised": (
                    bool(backoff_events)
                ),
                "fault_schedule_shared_with_controller": False,
                "independent_fault_injector_process_exercised": True,
                "p4runtime_readback_assurance_exercised": True,
                "idempotent_component_reapply_exercised": fault_expected,
                "bounded_retry_and_backoff_policy_enabled": True,
                "unicast_dataplane_continuity_control_exercised": True,
                "bmv2_process_restart_exercised": False,
                "pipeline_reload_exercised": False,
                "multi_domain_assurance_validated": False,
                "production_scale_validated": False,
            },
            "configuration": {
                "duration_s": args.duration,
                "fault_after_s": args.fault_after_s,
                "fault_kind": args.fault_kind,
                "fault_expected": fault_expected,
                "forced_remediation_rejections": (
                    args.assurance_forced_remediation_rejections
                ),
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
                "assurance_policy": controller_snapshot.get("policy"),
            },
            "desired_state": desired_state,
            "initial_state": initial_state,
            "fault_injector": {
                **injector,
                "returncode": injector_process.returncode,
                "stdout": injector_stdout,
                "stderr": injector_stderr,
            },
            "assurance": controller_snapshot,
            "p4": {
                "address": args.p4_addr,
                "initial_process_ids": initial_pids,
                "process_ids_during_incident": pids_during_incident,
                "final_process_ids": final_pids,
                "pid_continuity": bmv2_pid_continuity,
                "initial_port_available": initial_port_ok,
                "port_available_during_incident": port_during_incident,
                "final_port_available": final_port_ok,
                "unicast_rule_during_incident": unicast_during_incident,
                "cleanup": cleanup_state,
            },
            "traffic": {
                "multicast_sender": multicast_sender,
                "control_sender": control_sender,
                "multicast_receivers": multicast_receivers,
                "control_receiver": control_receiver,
            },
            "recovery_metrics": {
                "timing_ms": timing_ms,
                "expected_drift_kinds": sorted(expected_drift),
                "observed_drift_kinds": sorted(observed_drift),
                "expected_remediation_components": sorted(expected_components),
                "remediated_components": sorted(remediated_components),
                "forced_rejection_event_count": len(forced_rejection_events),
                "backoff_event_count": len(backoff_events),
                "accepted_remediation_event_count": (
                    len(accepted_remediation_events)
                ),
                "multicast_receivers": multicast_metrics,
                "unicast_control": control_metrics,
                "candidate_checks": candidate_checks,
                "candidate_found": candidate_found,
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

        print(f"PHASE16_ASSURANCE_PROFILE_ID={args.assurance_profile_id}")
        print(f"PHASE16_ASSURANCE_FAULT_KIND={args.fault_kind}")
        print("PHASE16_ASSURANCE_FAULT_SCHEDULE_SHARED=False")
        print(
            "PHASE16_ASSURANCE_AUTONOMOUS_DETECTION_EXERCISED="
            f"{fault_expected}"
        )
        print(
            "PHASE16_ASSURANCE_AUTONOMOUS_RECOVERY_EXERCISED="
            f"{fault_expected}"
        )
        print(
            "PHASE16_ASSURANCE_REMEDIATION_ATTEMPTS="
            f"{controller_snapshot.get('remediation_attempt_count')}"
        )
        print(
            "PHASE16_ASSURANCE_FORCED_REJECTION_EVENTS="
            f"{len(forced_rejection_events)}"
        )
        print(
            "PHASE16_ASSURANCE_BACKOFF_EVENTS="
            f"{len(backoff_events)}"
        )
        if fault_expected:
            print(
                "PHASE16_ASSURANCE_DETECTION_MS="
                f"{float(timing_ms['fault_write_to_drift_confirmation']):.6f}"
            )
            print(
                "PHASE16_ASSURANCE_CONTROL_PLANE_RECOVERY_MS="
                f"{float(timing_ms['remediation_start_to_convergence']):.6f}"
            )
            print(
                "PHASE16_ASSURANCE_TOTAL_RECONCILIATION_MS="
                f"{float(timing_ms['fault_write_to_convergence']):.6f}"
            )
        else:
            print("PHASE16_ASSURANCE_DETECTION_MS=0.000000")
            print("PHASE16_ASSURANCE_CONTROL_PLANE_RECOVERY_MS=0.000000")
            print("PHASE16_ASSURANCE_TOTAL_RECONCILIATION_MS=0.000000")

        for label in ("B", "C"):
            metrics = multicast_metrics[label]
            if fault_expected:
                pre_ratio = metrics["windows"]["pre_fault"]["delivery_ratio"]
                post_ratio = metrics["windows"]["post_recovery"][
                    "delivery_ratio"
                ]
                sequence_gap = int(
                    metrics["crossing_gap"].get("sequence_gap") or 0
                )
                first_packet_ms = metrics["first_recovered_packet"].get(
                    "from_restoration_to_receive_ms"
                )
            else:
                pre_ratio = metrics["windows"]["stable_full"]["delivery_ratio"]
                post_ratio = pre_ratio
                sequence_gap = 0
                first_packet_ms = 0.0
            print(
                f"PHASE16_ASSURANCE_{label}_PRE_RATIO="
                f"{float(pre_ratio):.9f}"
            )
            print(
                f"PHASE16_ASSURANCE_{label}_POST_RATIO="
                f"{float(post_ratio):.9f}"
            )
            print(
                f"PHASE16_ASSURANCE_{label}_SEQUENCE_GAP="
                f"{sequence_gap}"
            )
            print(
                f"PHASE16_ASSURANCE_{label}_FIRST_PACKET_MS="
                f"{float(first_packet_ms or 0.0):.6f}"
            )
        print(f"PHASE16_ASSURANCE_CANDIDATE_FOUND={candidate_found}")
        print(f"PHASE16_ASSURANCE_SUMMARY={summary_path}")

        if operational_ok:
            print("PHASE16_S2_P4_AUTONOMOUS_ASSURANCE_RUN_OK")
            return 0
        print("PHASE16_S2_P4_AUTONOMOUS_ASSURANCE_RUN_FAILED")
        return 1
    finally:
        assurance_stop.set()
        if assurance_thread.is_alive():
            assurance_thread.join(timeout=2.0)
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
        if injector_process is not None and injector_process.poll() is None:
            injector_process.terminate()
        adapter.close()


def add_common_p4_arguments(parser: argparse.ArgumentParser) -> None:
    """Add shared P4Runtime state arguments to one subcommand."""

    parser.add_argument("--p4-addr", default="127.0.0.1:9559")
    parser.add_argument("--p4-host", default="127.0.0.1")
    parser.add_argument("--p4-port", type=int, default=9559)
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--p4-outdir", default="/tmp/l2i_minimal")
    parser.add_argument("--p4-timeout-s", type=float, default=5.0)
    parser.add_argument("--group", default="239.1.1.1")
    parser.add_argument("--group-id", type=int, default=1)
    parser.add_argument("--multicast-ports", type=int, nargs="+", default=[1, 2])
    parser.add_argument("--state-readback-timeout-s", type=float, default=2.0)
    parser.add_argument("--state-poll-interval-s", type=float, default=0.01)


def build_parser() -> argparse.ArgumentParser:
    """Build the autonomous assurance command line."""

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    injector = subparsers.add_parser(
        "inject-fault",
        help="delete multicast state from an independent subprocess",
    )
    injector.add_argument("--output", required=True)
    injector.add_argument("--start-barrier", required=True)
    injector.add_argument("--barrier-timeout-s", type=float, default=10.0)
    injector.add_argument("--delay-s", type=float, default=4.0)
    injector.add_argument(
        "--fault-kind",
        choices=("none", "pre_only", "table_only", "both"),
        default="both",
    )
    injector.add_argument("--election-low", type=int, default=16110)
    add_common_p4_arguments(injector)

    orchestrator = subparsers.add_parser(
        "orchestrate",
        help="run traffic, an independent fault, and autonomous MAD assurance",
    )
    orchestrator.add_argument("--output-dir", required=True)
    orchestrator.add_argument(
        "--assurance-profile-id",
        default="phase16-s2-p4-autonomous-assurance-foundation-v1",
    )
    add_common_p4_arguments(orchestrator)
    orchestrator.add_argument("--multicast-port", type=int, default=5001)
    orchestrator.add_argument("--control-port", type=int, default=6001)
    orchestrator.add_argument("--multicast-source-namespace", default="h1")
    orchestrator.add_argument("--multicast-source-ip", default="10.0.0.1")
    orchestrator.add_argument("--receiver-b-namespace", default="h3")
    orchestrator.add_argument("--receiver-b-ip", default="10.0.0.3")
    orchestrator.add_argument("--receiver-c-namespace", default="h4")
    orchestrator.add_argument("--receiver-c-ip", default="10.0.0.4")
    orchestrator.add_argument("--control-source-namespace", default="h2")
    orchestrator.add_argument("--control-source-ip", default="10.0.0.2")
    orchestrator.add_argument("--control-source-device", default="h2-eth0")
    orchestrator.add_argument("--control-receiver-namespace", default="h3")
    orchestrator.add_argument("--control-receiver-ip", default="10.0.0.3")
    orchestrator.add_argument("--control-receiver-device", default="h3-eth0")
    orchestrator.add_argument("--control-ingress-port", type=int, default=3)
    orchestrator.add_argument("--control-egress-port", type=int, default=1)
    orchestrator.add_argument("--duration", type=float, default=12.0)
    orchestrator.add_argument("--fault-after-s", type=float, default=4.0)
    orchestrator.add_argument(
        "--fault-kind",
        choices=("none", "pre_only", "table_only", "both"),
        default="both",
    )
    orchestrator.add_argument("--minimum-post-s", type=float, default=5.0)
    orchestrator.add_argument("--window-guard-s", type=float, default=0.25)
    orchestrator.add_argument("--multicast-rate-mbps", type=float, default=2.0)
    orchestrator.add_argument("--control-rate-mbps", type=float, default=0.5)
    orchestrator.add_argument("--packet-size", type=int, default=1200)
    orchestrator.add_argument("--spin-threshold-us", type=float, default=900.0)
    orchestrator.add_argument("--multicast-sender-cpu", default="auto")
    orchestrator.add_argument("--control-sender-cpu", default="auto-distinct")
    orchestrator.add_argument("--max-abs-rate-error-pct", type=float, default=5.0)
    orchestrator.add_argument("--min-inter-send-ratio", type=float, default=0.98)
    orchestrator.add_argument("--minimum-stable-delivery", type=float, default=0.99)
    orchestrator.add_argument("--minimum-control-delivery", type=float, default=0.99)
    orchestrator.add_argument("--minimum-lost-packets", type=int, default=1)
    orchestrator.add_argument("--maximum-first-packet-recovery-ms", type=float, default=50.0)
    orchestrator.add_argument("--maximum-detection-ms", type=float, default=150.0)
    orchestrator.add_argument(
        "--maximum-control-plane-recovery-ms",
        type=float,
        default=150.0,
    )
    orchestrator.add_argument(
        "--maximum-total-reconciliation-ms",
        type=float,
        default=250.0,
    )
    orchestrator.add_argument("--assurance-poll-interval-s", type=float, default=0.02)
    orchestrator.add_argument("--assurance-drift-confirmations", type=int, default=3)
    orchestrator.add_argument(
        "--assurance-convergence-confirmations",
        type=int,
        default=2,
    )
    orchestrator.add_argument(
        "--assurance-maximum-remediation-attempts",
        type=int,
        default=3,
    )
    orchestrator.add_argument(
        "--assurance-forced-remediation-rejections",
        type=int,
        default=0,
        help=(
            "synthetically reject a bounded number of initial remediation "
            "attempts to validate retry and backoff behavior"
        ),
    )
    orchestrator.add_argument("--assurance-initial-backoff-s", type=float, default=0.01)
    orchestrator.add_argument("--assurance-backoff-multiplier", type=float, default=2.0)
    orchestrator.add_argument("--assurance-maximum-backoff-s", type=float, default=0.10)
    orchestrator.add_argument(
        "--assurance-maximum-consecutive-observation-errors",
        type=int,
        default=3,
    )
    orchestrator.add_argument(
        "--assurance-initial-convergence-timeout-s",
        type=float,
        default=3.0,
    )
    orchestrator.add_argument("--assurance-recovery-timeout-s", type=float, default=3.0)
    orchestrator.add_argument("--assurance-stop-timeout-s", type=float, default=3.0)
    orchestrator.add_argument("--assurance-maximum-runtime-s", type=float, default=30.0)
    orchestrator.add_argument("--initial-program-election-low", type=int, default=16090)
    orchestrator.add_argument("--observer-election-low", type=int, default=16100)
    orchestrator.add_argument("--injector-election-low", type=int, default=16110)
    orchestrator.add_argument("--remediation-election-low", type=int, default=16120)
    orchestrator.add_argument("--cleanup-election-low", type=int, default=16990)
    orchestrator.add_argument("--receiver-drain-s", type=float, default=1.0)
    orchestrator.add_argument("--worker-ready-timeout-s", type=float, default=10.0)
    orchestrator.add_argument("--worker-stop-timeout-s", type=float, default=5.0)
    orchestrator.add_argument("--worker-max-runtime-s", type=float, default=120.0)
    return parser


def main() -> int:
    """Dispatch the independent injector or the autonomous orchestrator."""

    args = build_parser().parse_args()
    if args.command == "inject-fault":
        return inject_fault(args)
    return orchestrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
