#!/usr/bin/env python3
"""Exercise coordinated autonomous assurance across Linux, NETCONF, and P4.

The experiment materializes one shared S2 desired state in three real control
planes: a Linux HTB hierarchy, the ``l2i-qos`` NETCONF/YANG datastore, and the
P4Runtime multicast PRE/table state. A subprocess that receives only a file
barrier and its private delay removes the three states. It never notifies the
MAD controller. One aggregate assurance loop then confirms cross-domain drift,
invokes only the divergent domain adapters, and declares global convergence
only after every domain is healthy in the same readback cycle.

This foundation validates coordinated multi-domain *control-plane* assurance.
It does not claim atomic distributed transactions, rollback, partial-remediation
failure handling, multi-domain dataplane recovery, BMv2 restart recovery, or
pipeline reload.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Mapping, NoReturn
import xml.etree.ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ncclient import manager
from p4.v1 import p4runtime_pb2

from l2i.assurance import (
    AssurancePolicy,
    MADAssuranceController,
    RemediationOutcome,
    StateObservation,
)
from l2i.backends import _shim_linux_tc_local as linux_tc
from l2i.backends import _shim_real_netconf as real_netconf
from l2i.multidomain_assurance import (
    DomainAssuranceBinding,
    MultiDomainAssuranceAdapter,
)
from l2i.third_party.p4rt_min.client import P4RTClient
from scripts.p4_program_s2 import (
    build_mcast_entry,
    build_pre_entity,
    find_required_objects as find_mcast_objects,
    load_p4info,
    table_entity as mcast_table_entity,
)
from scripts.s2_p4_autonomous_assurance import (
    P4MulticastAssuranceAdapter,
    write_upsert_entity,
)
from scripts.s2_p4_state_recovery import (
    multicast_state_readback,
    process_ids,
    tcp_port_available,
    update_message,
)


QOS_NAMESPACE = "urn:l2i:qos"
NETCONF_BASE_NAMESPACE = "urn:ietf:params:xml:ns:netconf:base:1.0"


def fail(message: str) -> NoReturn:
    """Terminate with a stable Phase 17 diagnostic marker."""

    print(
        f"PHASE17_MULTIDOMAIN_ASSURANCE_FAILED: {message}",
        file=sys.stderr,
    )
    raise SystemExit(1)


def dump_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write deterministic UTF-8 JSON evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def wait_for_barrier(path: Path, timeout_s: float) -> bool:
    """Wait for an orchestration file without signalling the controller."""

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        time.sleep(0.01)
    return path.is_file()


def run_command(
    argv: list[str],
    *,
    check: bool = True,
) -> dict[str, Any]:
    """Execute one local command and preserve a JSON-safe record."""

    started_ns = time.monotonic_ns()
    completed = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        check=False,
    )
    completed_ns = time.monotonic_ns()
    record = {
        "argv": argv,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "started_monotonic_ns": started_ns,
        "completed_monotonic_ns": completed_ns,
        "elapsed_ms": (completed_ns - started_ns) / 1_000_000.0,
    }
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(argv)}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return record


def normalize_backend_result(raw: Any) -> tuple[bool, dict[str, Any]]:
    """Normalize the tuple contract used by the current real backends."""

    if isinstance(raw, tuple) and len(raw) == 2:
        ok, details = raw
        return bool(ok), dict(details or {})
    raise TypeError(f"unsupported backend result: {type(raw).__name__}")


def parse_tc_rate_mbps(token: str | None) -> float | None:
    """Parse the compact units emitted by ``tc class show``."""

    if token is None:
        return None
    match = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)([KMG]?)(?:bit|bps)",
        token.strip(),
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    scale = {
        "": 1.0 / 1_000_000.0,
        "K": 1.0 / 1_000.0,
        "M": 1.0,
        "G": 1_000.0,
    }[unit]
    return value * scale


def tc_class_rates(
    class_text: str,
    classid: str,
) -> tuple[float | None, float | None]:
    """Extract one HTB class rate and ceil from text readback."""

    for line in class_text.splitlines():
        if not re.search(rf"\b{re.escape(classid)}\b", line):
            continue
        match = re.search(
            r"\brate\s+(\S+)\s+ceil\s+(\S+)",
            line,
        )
        if match is not None:
            return (
                parse_tc_rate_mbps(match.group(1)),
                parse_tc_rate_mbps(match.group(2)),
            )
    return None, None


class LinuxTCAssuranceAdapter:
    """Map one Linux HTB desired state to generic assurance callbacks."""

    def __init__(
        self,
        *,
        device: str,
        capacity_mbps: float,
        minimum_mbps: float,
        maximum_mbps: float,
        multicast_group: str,
        multicast_port: int,
    ) -> None:
        self.device = device
        self.environment = {
            "bw_mbps": capacity_mbps,
            "create_default_class": False,
            "default_class": "30",
            "r2q": 10,
        }
        self.intent = {
            "class": "prio10",
            "min_mbps": minimum_mbps,
            "max_mbps": maximum_mbps,
        }
        self.target = {
            "device": device,
            "namespace": None,
            "dry_run": False,
            "filter_protocol": "udp",
            "filter_priority": 1,
            "dst_ip": f"{multicast_group}/32",
            "dst_port": multicast_port,
            "attach_priority_netem": False,
            "idempotent_cleanup": True,
            "remove_leaf_qdisc": True,
            "default_class": "30",
        }
        self.desired_state = {
            "technology": "linux_tc_htb",
            "device": device,
            "root_qdisc": "htb 1:",
            "parent_classid": "1:1",
            "priority_classid": "1:10",
            "filter_flowid": "1:10",
            "minimum_mbps": minimum_mbps,
            "maximum_mbps": maximum_mbps,
            "multicast_group": multicast_group,
            "multicast_port": multicast_port,
        }

    def observe(self) -> StateObservation:
        """Read qdisc, class, and filter state without modifying the device."""

        observed_ns = time.monotonic_ns()
        details = linux_tc.inspect_state(
            {"name": "A"},
            self.target,
        )
        readback = dict(details.get("readback") or {})
        qdisc = str(readback.get("qdisc") or "")
        classes = str(readback.get("class") or "")
        filters = str(readback.get("filter") or "")
        minimum, maximum = tc_class_rates(classes, "1:10")
        tolerance = 1e-6

        checks = {
            "root_htb": "htb 1:" in qdisc,
            "parent_class": "1:1" in classes,
            "priority_class": "1:10" in classes,
            "priority_filter": "flowid 1:10" in filters,
            "minimum_rate": (
                minimum is not None
                and abs(minimum - float(self.intent["min_mbps"])) <= tolerance
            ),
            "maximum_rate": (
                maximum is not None
                and abs(maximum - float(self.intent["max_mbps"])) <= tolerance
            ),
        }
        drift_map = {
            "root_htb": "missing_root_htb_qdisc",
            "parent_class": "missing_parent_htb_class",
            "priority_class": "missing_priority_htb_class",
            "priority_filter": "missing_priority_filter",
            "minimum_rate": "priority_minimum_rate_mismatch",
            "maximum_rate": "priority_maximum_rate_mismatch",
        }
        drifts = tuple(
            drift_map[name]
            for name, value in checks.items()
            if not value
        )
        read_ok = details.get("error") is None
        healthy = read_ok and all(checks.values())
        return StateObservation(
            read_ok=read_ok,
            healthy=healthy,
            drift_kinds=drifts,
            observed={
                "device": self.device,
                "checks": checks,
                "observed_minimum_mbps": minimum,
                "observed_maximum_mbps": maximum,
                "backend": details,
            },
            observed_monotonic_ns=observed_ns,
            message=(
                "Linux TC state is converged"
                if healthy
                else "Linux TC state diverged"
            ),
        )

    def materialize(self) -> dict[str, Any]:
        """Rebuild the managed HTB environment and priority overlay."""

        env_ok, environment = normalize_backend_result(
            linux_tc.setup_environment(
                {"name": "A"},
                self.environment,
                self.target,
            )
        )
        qos_ok = False
        qos: dict[str, Any] = {}
        if env_ok:
            qos_ok, qos = normalize_backend_result(
                linux_tc.apply_qos(
                    {"name": "A"},
                    self.intent,
                    self.target,
                )
            )
        return {
            "accepted": bool(env_ok and qos_ok),
            "environment": environment,
            "qos": qos,
        }

    def remediate(
        self,
        observation: StateObservation,
        incident_id: int,
        attempt: int,
    ) -> RemediationOutcome:
        """Rebuild only the Linux domain after confirmed aggregate drift."""

        started_ns = time.monotonic_ns()
        result = self.materialize()
        completed_ns = time.monotonic_ns()
        return RemediationOutcome(
            accepted=result["accepted"],
            details={
                "domain": "A",
                "incident_id": incident_id,
                "attempt": attempt,
                "drift_kinds": list(observation.drift_kinds),
                "materialization": result,
                "started_monotonic_ns": started_ns,
                "completed_monotonic_ns": completed_ns,
            },
            completed_monotonic_ns=completed_ns,
        )

    def cleanup(self) -> dict[str, Any]:
        """Remove the managed HTB root idempotently."""

        ok, details = normalize_backend_result(
            linux_tc.cleanup_environment(
                {"name": "A"},
                self.target,
            )
        )
        return {"accepted": ok, "details": details}


class NetconfQosAssuranceAdapter:
    """Map the local ``l2i-qos`` datastore to assurance callbacks."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        key_filename: str,
        timeout_s: float,
        qos_class: str,
        minimum_mbps: int,
        maximum_mbps: int,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.key_filename = os.path.expanduser(key_filename)
        self.timeout_s = timeout_s
        self.intent = {
            "class": qos_class,
            "min_mbps": minimum_mbps,
            "max_mbps": maximum_mbps,
        }
        self.target = {
            "host": host,
            "port": port,
            "user": username,
            "key_filename": self.key_filename,
            "timeout": max(1, int(timeout_s)),
        }
        self.desired_state = {
            "technology": "netconf_yang",
            "datastore": "running",
            "model_namespace": QOS_NAMESPACE,
            **self.intent,
        }

    def _connect(self):
        """Create one short-lived NETCONF session for isolated evidence."""

        return manager.connect(
            host=self.host,
            port=self.port,
            username=self.username,
            key_filename=self.key_filename,
            hostkey_verify=False,
            allow_agent=False,
            look_for_keys=False,
            timeout=self.timeout_s,
        )

    @staticmethod
    def _parse_snapshot(xml_text: str) -> dict[str, Any]:
        """Extract the three leaves from a NETCONF reply XML document."""

        root = ET.fromstring(xml_text)
        qos = root.find(f".//{{{QOS_NAMESPACE}}}qos")
        if qos is None:
            return {
                "qos_present": False,
                "class": None,
                "min_mbps": None,
                "max_mbps": None,
            }

        def leaf(name: str) -> str | None:
            element = qos.find(f"{{{QOS_NAMESPACE}}}{name}")
            if element is None or element.text is None:
                return None
            return element.text.strip()

        minimum = leaf("min-mbps")
        maximum = leaf("max-mbps")
        return {
            "qos_present": True,
            "class": leaf("class"),
            "min_mbps": int(minimum) if minimum is not None else None,
            "max_mbps": int(maximum) if maximum is not None else None,
        }

    def read_state(self) -> dict[str, Any]:
        """Read the running datastore through a fresh real NETCONF session."""

        started_ns = time.monotonic_ns()
        try:
            with self._connect() as session:
                reply = session.get_config(
                    source="running",
                    filter=("subtree", f'<qos xmlns="{QOS_NAMESPACE}"/>'),
                )
                snapshot = self._parse_snapshot(reply.xml)
            completed_ns = time.monotonic_ns()
            return {
                "read_ok": True,
                "snapshot": snapshot,
                "raw_xml": reply.xml,
                "observed_monotonic_ns": completed_ns,
                "elapsed_ms": (completed_ns - started_ns) / 1_000_000.0,
            }
        except Exception as exc:
            completed_ns = time.monotonic_ns()
            return {
                "read_ok": False,
                "snapshot": {
                    "qos_present": False,
                    "class": None,
                    "min_mbps": None,
                    "max_mbps": None,
                },
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "observed_monotonic_ns": completed_ns,
                "elapsed_ms": (completed_ns - started_ns) / 1_000_000.0,
            }

    def observe(self) -> StateObservation:
        """Compare exact running datastore leaves with the desired values."""

        readback = self.read_state()
        snapshot = dict(readback.get("snapshot") or {})
        checks = {
            "qos_present": snapshot.get("qos_present") is True,
            "class": snapshot.get("class") == self.intent["class"],
            "min_mbps": snapshot.get("min_mbps") == self.intent["min_mbps"],
            "max_mbps": snapshot.get("max_mbps") == self.intent["max_mbps"],
        }
        drift_map = {
            "qos_present": "missing_qos_container",
            "class": "qos_class_mismatch",
            "min_mbps": "qos_minimum_rate_mismatch",
            "max_mbps": "qos_maximum_rate_mismatch",
        }
        drifts = tuple(
            drift_map[name]
            for name, value in checks.items()
            if not value
        )
        if readback.get("read_ok") is not True:
            drifts = ("readback_error", *drifts)
        healthy = bool(
            readback.get("read_ok") is True
            and all(checks.values())
        )
        return StateObservation(
            read_ok=readback.get("read_ok") is True,
            healthy=healthy,
            drift_kinds=drifts,
            observed={
                "checks": checks,
                "readback": readback,
            },
            observed_monotonic_ns=int(
                readback.get("observed_monotonic_ns")
                or time.monotonic_ns()
            ),
            message=(
                "NETCONF/YANG state is converged"
                if healthy
                else "NETCONF/YANG state diverged"
            ),
        )

    def materialize(self) -> dict[str, Any]:
        """Apply the exact desired leaves through the production shim."""

        ok, details = normalize_backend_result(
            real_netconf.apply_qos(
                {"name": "B"},
                self.intent,
                self.target,
            )
        )
        return {"accepted": ok, "details": details}

    def remediate(
        self,
        observation: StateObservation,
        incident_id: int,
        attempt: int,
    ) -> RemediationOutcome:
        """Reapply the NETCONF desired state after confirmed drift."""

        started_ns = time.monotonic_ns()
        result = self.materialize()
        completed_ns = time.monotonic_ns()
        return RemediationOutcome(
            accepted=result["accepted"],
            details={
                "domain": "B",
                "incident_id": incident_id,
                "attempt": attempt,
                "drift_kinds": list(observation.drift_kinds),
                "materialization": result,
                "started_monotonic_ns": started_ns,
                "completed_monotonic_ns": completed_ns,
            },
            completed_monotonic_ns=completed_ns,
        )

    def remove(self) -> dict[str, Any]:
        """Remove the QoS container idempotently with ``nc:operation=remove``."""

        xml = f"""
<config xmlns="{NETCONF_BASE_NAMESPACE}">
  <qos xmlns="{QOS_NAMESPACE}"
       xmlns:nc="{NETCONF_BASE_NAMESPACE}"
       nc:operation="remove"/>
</config>
""".strip()
        started_ns = time.monotonic_ns()
        try:
            with self._connect() as session:
                reply = session.edit_config(
                    target="running",
                    config=xml,
                )
            completed_ns = time.monotonic_ns()
            return {
                "accepted": True,
                "reply_xml": reply.xml,
                "started_monotonic_ns": started_ns,
                "completed_monotonic_ns": completed_ns,
                "elapsed_ms": (completed_ns - started_ns) / 1_000_000.0,
            }
        except Exception as exc:
            completed_ns = time.monotonic_ns()
            return {
                "accepted": False,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "started_monotonic_ns": started_ns,
                "completed_monotonic_ns": completed_ns,
                "elapsed_ms": (completed_ns - started_ns) / 1_000_000.0,
            }


class P4Context:
    """Hold immutable P4Info objects and desired/delete entities."""

    def __init__(self, args: argparse.Namespace) -> None:
        p4info = load_p4info(
            Path(args.p4_outdir) / "l2i_minimal.p4info.txtpb"
        )
        table, match_def, action, param = find_mcast_objects(p4info)
        self.table = table
        self.match_def = match_def
        self.action = action
        self.param = param
        self.state_kwargs = {
            "table": table,
            "match_def": match_def,
            "action": action,
            "param": param,
            "group_id": args.group_id,
            "ports": list(args.multicast_ports),
            "dst_ip": args.group,
        }
        self.pre_entity = build_pre_entity(
            args.group_id,
            args.multicast_ports,
            include_replicas=True,
        )
        self.pre_delete = build_pre_entity(
            args.group_id,
            args.multicast_ports,
            include_replicas=False,
        )
        full_entry = build_mcast_entry(
            table=table,
            match_def=match_def,
            action=action,
            param=param,
            dst_ip=args.group,
            group_id=args.group_id,
            include_action=True,
        )
        delete_entry = build_mcast_entry(
            table=table,
            match_def=match_def,
            action=action,
            param=param,
            dst_ip=args.group,
            group_id=args.group_id,
            include_action=False,
        )
        self.table_entity = mcast_table_entity(full_entry)
        self.table_delete = mcast_table_entity(delete_entry)
        self.desired_state = {
            "technology": "p4runtime",
            "group_id": args.group_id,
            "replica_ports": list(args.multicast_ports),
            "multicast_destination": args.group,
            "components": [
                "pre_multicast_group",
                "multicast_table_entry",
            ],
        }


def p4_write_state(
    args: argparse.Namespace,
    context: P4Context,
    *,
    present: bool,
    election_low: int,
) -> dict[str, Any]:
    """Materialize or remove the P4 multicast desired state idempotently."""

    client = P4RTClient(
        address=args.p4_addr,
        device_id=args.device_id,
        election_id=(0, election_low),
        timeout_s=args.p4_timeout_s,
    )
    try:
        client.connect()
        arbitration = client.ensure_primary(wait_s=2.0)
        if not arbitration.ok or not arbitration.is_primary:
            return {
                "accepted": False,
                "arbitration_ok": arbitration.ok,
                "is_primary": arbitration.is_primary,
                "message": arbitration.message,
            }

        before = multicast_state_readback(
            client,
            **context.state_kwargs,
        )
        operations: list[dict[str, Any]] = []
        if present:
            operations.append(
                {
                    "component": "pre_multicast_group",
                    **write_upsert_entity(client, context.pre_entity),
                }
            )
            operations.append(
                {
                    "component": "multicast_table_entry",
                    **write_upsert_entity(client, context.table_entity),
                }
            )
            write_ok = all(
                operation.get("accepted") is True
                for operation in operations
            )
        else:
            updates: list[p4runtime_pb2.Update] = []
            if before.get("entry_present") is True:
                updates.append(
                    update_message(
                        p4runtime_pb2.Update.DELETE,
                        context.table_delete,
                    )
                )
            if before.get("group_present") is True:
                updates.append(
                    update_message(
                        p4runtime_pb2.Update.DELETE,
                        context.pre_delete,
                    )
                )
            if updates:
                write_ok, message = client.write(updates)
            else:
                write_ok, message = True, "state already absent"
            operations.append(
                {
                    "component": "multicast_state_cleanup",
                    "accepted": write_ok,
                    "message": message,
                    "update_count": len(updates),
                }
            )

        after = multicast_state_readback(
            client,
            **context.state_kwargs,
        )
        converged = (
            after.get("complete_present") is True
            if present
            else after.get("complete_absent") is True
        )
        return {
            "accepted": bool(write_ok and converged),
            "arbitration_ok": arbitration.ok,
            "is_primary": arbitration.is_primary,
            "before": before,
            "operations": operations,
            "after": after,
            "expected_present": present,
            "converged": converged,
        }
    finally:
        client.close()


def setup_linux_link(device: str, peer: str) -> dict[str, Any]:
    """Create a dedicated veth used only by the Linux assurance domain."""

    cleanup_linux_link(device, peer)
    create = run_command(
        ["ip", "link", "add", device, "type", "veth", "peer", "name", peer]
    )
    up_device = run_command(["ip", "link", "set", device, "up"])
    up_peer = run_command(["ip", "link", "set", peer, "up"])
    return {
        "device": device,
        "peer": peer,
        "commands": [create, up_device, up_peer],
        "device_present": run_command(
            ["ip", "link", "show", "dev", device],
            check=False,
        )["returncode"] == 0,
        "peer_present": run_command(
            ["ip", "link", "show", "dev", peer],
            check=False,
        )["returncode"] == 0,
    }


def cleanup_linux_link(device: str, peer: str) -> dict[str, Any]:
    """Delete the dedicated veth idempotently."""

    deletion = run_command(
        ["ip", "link", "del", device],
        check=False,
    )
    device_absent = run_command(
        ["ip", "link", "show", "dev", device],
        check=False,
    )["returncode"] != 0
    peer_absent = run_command(
        ["ip", "link", "show", "dev", peer],
        check=False,
    )["returncode"] != 0
    return {
        "deletion": deletion,
        "device_absent": device_absent,
        "peer_absent": peer_absent,
    }


def build_adapters(
    args: argparse.Namespace,
    context: P4Context,
) -> tuple[
    LinuxTCAssuranceAdapter,
    NetconfQosAssuranceAdapter,
    P4MulticastAssuranceAdapter,
]:
    """Construct all three real domain adapters from one argument set."""

    linux = LinuxTCAssuranceAdapter(
        device=args.linux_device,
        capacity_mbps=args.capacity_mbps,
        minimum_mbps=args.minimum_mbps,
        maximum_mbps=args.maximum_mbps,
        multicast_group=args.group,
        multicast_port=args.multicast_port,
    )
    netconf = NetconfQosAssuranceAdapter(
        host=args.netconf_host,
        port=args.netconf_port,
        username=args.netconf_username,
        key_filename=args.netconf_key,
        timeout_s=args.netconf_timeout_s,
        qos_class=args.qos_class,
        minimum_mbps=int(args.minimum_mbps),
        maximum_mbps=int(args.maximum_mbps),
    )
    p4 = P4MulticastAssuranceAdapter(
        p4_addr=args.p4_addr,
        device_id=args.device_id,
        p4_timeout_s=args.p4_timeout_s,
        observer_election_low=args.observer_election_low,
        remediation_election_low=args.remediation_election_low,
        table=context.table,
        match_def=context.match_def,
        action=context.action,
        param=context.param,
        group_id=args.group_id,
        ports=list(args.multicast_ports),
        dst_ip=args.group,
        pre_entity=context.pre_entity,
        table_entity=context.table_entity,
    )
    return linux, netconf, p4


def inject_fault(args: argparse.Namespace) -> int:
    """Delete all three domain states from an independent subprocess."""

    barrier = Path(args.start_barrier)
    output = Path(args.output)
    if not wait_for_barrier(barrier, args.barrier_timeout_s):
        fail("fault injector did not observe the start barrier")

    barrier_ns = time.monotonic_ns()
    time.sleep(args.delay_s)
    context = P4Context(args)
    netconf = NetconfQosAssuranceAdapter(
        host=args.netconf_host,
        port=args.netconf_port,
        username=args.netconf_username,
        key_filename=args.netconf_key,
        timeout_s=args.netconf_timeout_s,
        qos_class=args.qos_class,
        minimum_mbps=int(args.minimum_mbps),
        maximum_mbps=int(args.maximum_mbps),
    )

    def fault_linux() -> dict[str, Any]:
        started_ns = time.monotonic_ns()
        command = run_command(
            ["tc", "qdisc", "del", "dev", args.linux_device, "root"],
            check=False,
        )
        inspection = linux_tc.inspect_state(
            {"name": "A"},
            {"device": args.linux_device, "dry_run": False},
        )
        absent = "htb 1:" not in str(
            (inspection.get("readback") or {}).get("qdisc") or ""
        )
        completed_ns = time.monotonic_ns()
        return {
            "domain": "A",
            "technology": "linux_tc_htb",
            "started_monotonic_ns": started_ns,
            "completed_monotonic_ns": completed_ns,
            "write_accepted": command["returncode"] == 0,
            "absence_confirmed": absent,
            "command": command,
            "readback": inspection,
        }

    def fault_netconf() -> dict[str, Any]:
        started_ns = time.monotonic_ns()
        removal = netconf.remove()
        readback = netconf.read_state()
        absent = (
            readback.get("read_ok") is True
            and (readback.get("snapshot") or {}).get("qos_present") is False
        )
        completed_ns = time.monotonic_ns()
        return {
            "domain": "B",
            "technology": "netconf_yang",
            "started_monotonic_ns": started_ns,
            "completed_monotonic_ns": completed_ns,
            "write_accepted": removal.get("accepted") is True,
            "absence_confirmed": absent,
            "removal": removal,
            "readback": readback,
        }

    def fault_p4() -> dict[str, Any]:
        started_ns = time.monotonic_ns()
        removal = p4_write_state(
            args,
            context,
            present=False,
            election_low=args.injector_election_low,
        )
        completed_ns = time.monotonic_ns()
        return {
            "domain": "C",
            "technology": "p4runtime",
            "started_monotonic_ns": started_ns,
            "completed_monotonic_ns": completed_ns,
            "write_accepted": removal.get("accepted") is True,
            "absence_confirmed": (
                (removal.get("after") or {}).get("complete_absent") is True
            ),
            "removal": removal,
        }

    started_ns = time.monotonic_ns()
    results: dict[str, dict[str, Any]] = {}
    functions = {
        "A": fault_linux,
        "B": fault_netconf,
        "C": fault_p4,
    }
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_map = {
            executor.submit(function): domain
            for domain, function in functions.items()
        }
        for future in as_completed(future_map):
            domain = future_map[future]
            try:
                results[domain] = future.result()
            except Exception as exc:
                results[domain] = {
                    "domain": domain,
                    "write_accepted": False,
                    "absence_confirmed": False,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "completed_monotonic_ns": time.monotonic_ns(),
                }
    completed_ns = time.monotonic_ns()
    all_write_accepted = all(
        result.get("write_accepted") is True
        for result in results.values()
    )
    all_absence_confirmed = all(
        result.get("absence_confirmed") is True
        for result in results.values()
    )
    evidence = {
        "fault_source": "independent_multidomain_subprocess",
        "controller_notification_sent": False,
        "fault_schedule_shared_with_controller": False,
        "start_barrier": str(barrier),
        "barrier_observed_monotonic_ns": barrier_ns,
        "configured_delay_s": args.delay_s,
        "fault_started_monotonic_ns": started_ns,
        "fault_completed_monotonic_ns": completed_ns,
        "domains": results,
        "all_domain_writes_accepted": all_write_accepted,
        "all_domain_absence_confirmed": all_absence_confirmed,
    }
    dump_json(output, evidence)
    print(
        "PHASE17_FAULT_INJECTOR_ALL_WRITES_ACCEPTED="
        f"{all_write_accepted}"
    )
    print(
        "PHASE17_FAULT_INJECTOR_ALL_ABSENCE_CONFIRMED="
        f"{all_absence_confirmed}"
    )
    print("PHASE17_FAULT_INJECTOR_CONTROLLER_NOTIFICATION_SENT=False")
    if not all_write_accepted or not all_absence_confirmed:
        return 1
    print("PHASE17_INDEPENDENT_MULTIDOMAIN_FAULT_INJECTOR_OK")
    return 0


def event_by_type(
    events: list[dict[str, Any]],
    event_type: str,
) -> dict[str, Any] | None:
    """Return the first event with the requested type."""

    return next(
        (
            event
            for event in events
            if event.get("event_type") == event_type
        ),
        None,
    )


def orchestrate(args: argparse.Namespace) -> int:
    """Run initial materialization, independent drift, and reconciliation."""

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    injector_output = output_dir / "fault-injector.json"
    start_barrier = output_dir / "assurance-start.barrier"
    start_barrier.unlink(missing_ok=True)

    if not Path(args.netconf_key).expanduser().is_file():
        fail(f"NETCONF private key is missing: {args.netconf_key}")
    if not tcp_port_available(args.p4_host, args.p4_port):
        fail("P4Runtime port is unavailable")
    if not tcp_port_available(args.netconf_host, args.netconf_port):
        fail("NETCONF port is unavailable")

    initial_bmv2_pids = process_ids("[s]imple_switch_grpc")
    initial_netopeer_pids = process_ids("[n]etopeer2-server")
    if not initial_bmv2_pids:
        fail("BMv2 process was not found")
    if not initial_netopeer_pids:
        fail("Netopeer2 process was not found")

    first_remediation_election_low = (
        args.remediation_election_low
        + 101
    )
    election_plan = (
        args.observer_election_low,
        args.injector_election_low,
        args.initial_cleanup_election_low,
        args.initial_program_election_low,
        first_remediation_election_low,
        args.cleanup_election_low,
    )
    election_plan_is_strictly_increasing = (
        election_plan == tuple(sorted(election_plan))
        and len(set(election_plan)) == len(election_plan)
    )
    if not election_plan_is_strictly_increasing:
        fail(
            "invalid P4Runtime election plan: expected strictly increasing "
            "observer, injector, initial-cleanup, initial-program, "
            "first-remediation, cleanup IDs"
        )

    context = P4Context(args)
    linux: LinuxTCAssuranceAdapter | None = None
    netconf: NetconfQosAssuranceAdapter | None = None
    p4: P4MulticastAssuranceAdapter | None = None
    aggregate: MultiDomainAssuranceAdapter | None = None
    controller: MADAssuranceController | None = None
    controller_thread: threading.Thread | None = None
    stop_event = threading.Event()
    injector_process: subprocess.Popen[str] | None = None
    initial_materialization: dict[str, Any] = {}
    cleanup: dict[str, Any] = {}
    cleanup_performed = False
    summary_written = False

    try:
        link_setup = setup_linux_link(
            args.linux_device,
            args.linux_peer,
        )
        if not link_setup["device_present"] or not link_setup["peer_present"]:
            fail("dedicated Linux assurance veth was not created")

        linux, netconf, p4 = build_adapters(args, context)

        # Start from an explicitly absent state in all three domains. This makes
        # the initial materialization evidence independent from previous runs.
        initial_cleanup = {
            "A": linux.cleanup(),
            "B": netconf.remove(),
            "C": p4_write_state(
                args,
                context,
                present=False,
                election_low=args.initial_cleanup_election_low,
            ),
        }
        initial_materialization = {
            "cleanup": initial_cleanup,
            "A": linux.materialize(),
            "B": netconf.materialize(),
            "C": p4_write_state(
                args,
                context,
                present=True,
                election_low=args.initial_program_election_low,
            ),
        }

        # Preserve per-domain evidence before enforcing the aggregate gate. This
        # keeps deterministic startup failures attributable to the exact domain
        # instead of collapsing them into one generic orchestration message.
        dump_json(
            output_dir / "initial-materialization.json",
            initial_materialization,
        )

        failed_initial_domains = [
            domain
            for domain in ("A", "B", "C")
            if (
                (initial_materialization[domain] or {}).get("accepted")
                is not True
            )
        ]

        for domain in ("A", "B", "C"):
            accepted = (
                (initial_materialization[domain] or {}).get("accepted") is True
            )
            print(
                f"PHASE17_INITIAL_MATERIALIZATION_{domain}_ACCEPTED={accepted}"
            )

        if failed_initial_domains:
            fail(
                "initial multi-domain materialization failed: domains="
                + ",".join(failed_initial_domains)
            )

        bindings = (
            DomainAssuranceBinding(
                domain_id="A",
                technology="linux_tc_htb",
                desired_state=linux.desired_state,
                observe_fn=linux.observe,
                remediate_fn=linux.remediate,
            ),
            DomainAssuranceBinding(
                domain_id="B",
                technology="netconf_yang",
                desired_state=netconf.desired_state,
                observe_fn=netconf.observe,
                remediate_fn=netconf.remediate,
            ),
            DomainAssuranceBinding(
                domain_id="C",
                technology="p4runtime",
                desired_state=context.desired_state,
                observe_fn=p4.observe,
                remediate_fn=p4.remediate,
            ),
        )
        aggregate = MultiDomainAssuranceAdapter(bindings)
        controller = MADAssuranceController(
            controller_id="phase17-mad-multidomain-assurance",
            desired_state=aggregate.desired_state,
            observe_fn=aggregate.observe,
            remediate_fn=aggregate.remediate,
            policy=AssurancePolicy(
                poll_interval_s=args.assurance_poll_interval_s,
                drift_confirmations=args.assurance_drift_confirmations,
                convergence_confirmations=(
                    args.assurance_convergence_confirmations
                ),
                maximum_remediation_attempts=(
                    args.assurance_maximum_remediation_attempts
                ),
                initial_backoff_s=args.assurance_initial_backoff_s,
                backoff_multiplier=args.assurance_backoff_multiplier,
                maximum_backoff_s=args.assurance_maximum_backoff_s,
                maximum_consecutive_observation_errors=(
                    args.assurance_maximum_consecutive_observation_errors
                ),
            ),
        )
        controller_thread = threading.Thread(
            target=controller.run,
            args=(stop_event,),
            kwargs={"maximum_runtime_s": args.assurance_maximum_runtime_s},
            name="phase17-mad-multidomain-assurance",
        )
        controller_thread.start()
        if not controller.initial_convergence_event.wait(
            args.assurance_initial_convergence_timeout_s
        ):
            fail("initial aggregate convergence was not confirmed")

        injector_command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "inject-fault",
            "--output", str(injector_output),
            "--start-barrier", str(start_barrier),
            "--barrier-timeout-s", str(args.injector_barrier_timeout_s),
            "--delay-s", str(args.fault_after_s),
            "--linux-device", args.linux_device,
            "--netconf-host", args.netconf_host,
            "--netconf-port", str(args.netconf_port),
            "--netconf-username", args.netconf_username,
            "--netconf-key", args.netconf_key,
            "--netconf-timeout-s", str(args.netconf_timeout_s),
            "--qos-class", args.qos_class,
            "--capacity-mbps", str(args.capacity_mbps),
            "--minimum-mbps", str(args.minimum_mbps),
            "--maximum-mbps", str(args.maximum_mbps),
            "--multicast-port", str(args.multicast_port),
            "--p4-addr", args.p4_addr,
            "--p4-host", args.p4_host,
            "--p4-port", str(args.p4_port),
            "--device-id", str(args.device_id),
            "--p4-outdir", args.p4_outdir,
            "--p4-timeout-s", str(args.p4_timeout_s),
            "--group", args.group,
            "--group-id", str(args.group_id),
            "--multicast-ports",
            *[str(port) for port in args.multicast_ports],
            "--injector-election-low", str(args.injector_election_low),
        ]
        injector_process = subprocess.Popen(
            injector_command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        start_barrier.write_text(
            f"{time.monotonic_ns()}\n",
            encoding="utf-8",
        )

        if not controller.first_recovery_event.wait(
            args.assurance_recovery_timeout_s
        ):
            fail("autonomous cross-domain convergence was not confirmed")

        injector_stdout, injector_stderr = injector_process.communicate(
            timeout=args.injector_completion_timeout_s
        )
        if injector_process.returncode != 0:
            fail(
                "independent fault injector failed: "
                f"stdout={injector_stdout} stderr={injector_stderr}"
            )
        if not injector_output.is_file():
            fail("independent fault injector evidence is missing")
        injector = json.loads(
            injector_output.read_text(encoding="utf-8")
        )

        # Preserve a short stable period after convergence so the controller
        # performs additional aggregate observations before it is stopped.
        time.sleep(args.post_convergence_observation_s)
        stop_event.set()
        controller_thread.join(args.assurance_stop_timeout_s)
        if controller_thread.is_alive():
            fail("assurance controller did not stop")

        controller_snapshot = controller.snapshot()
        aggregate_snapshot = aggregate.snapshot()
        final_observation = aggregate.observe()
        events = list(controller_snapshot.get("events") or [])
        drift_event = event_by_type(events, "drift_confirmed")
        remediation_started = event_by_type(
            events,
            "remediation_attempt_started",
        )
        remediation_completed = event_by_type(
            events,
            "remediation_attempt_completed",
        )
        convergence_event = event_by_type(events, "convergence_confirmed")
        if not all(
            event is not None
            for event in (
                drift_event,
                remediation_started,
                remediation_completed,
                convergence_event,
            )
        ):
            fail("required assurance events are missing")

        fault_started_ns = int(injector["fault_started_monotonic_ns"])
        fault_completed_ns = int(injector["fault_completed_monotonic_ns"])
        drift_ns = int(drift_event["monotonic_ns"])
        remediation_started_ns = int(remediation_started["monotonic_ns"])
        remediation_completed_ns = int(remediation_completed["monotonic_ns"])
        convergence_ns = int(convergence_event["monotonic_ns"])
        timing_ms = {
            "fault_start_to_drift_confirmation": (
                drift_ns - fault_started_ns
            ) / 1_000_000.0,
            "fault_completion_to_drift_confirmation": (
                drift_ns - fault_completed_ns
            ) / 1_000_000.0,
            "remediation_start_to_completion": (
                remediation_completed_ns - remediation_started_ns
            ) / 1_000_000.0,
            "remediation_start_to_global_convergence": (
                convergence_ns - remediation_started_ns
            ) / 1_000_000.0,
            "fault_start_to_global_convergence": (
                convergence_ns - fault_started_ns
            ) / 1_000_000.0,
        }

        observed_drifts = set(
            (drift_event.get("details") or {}).get("drift_kinds") or []
        )
        expected_drift_prefixes = {"A:", "B:", "C:"}
        drift_domains = {
            drift.split(":", 1)[0]
            for drift in observed_drifts
            if ":" in drift
        }
        remediation_details = dict(
            remediation_completed.get("details") or {}
        )
        remediation_record = dict(
            remediation_details.get("details") or {}
        )
        remediated_domains = set(
            remediation_record.get("requested_domains") or []
        )
        service_state = {
            "initial_bmv2_pids": initial_bmv2_pids,
            "final_bmv2_pids": process_ids("[s]imple_switch_grpc"),
            "initial_netopeer_pids": initial_netopeer_pids,
            "final_netopeer_pids": process_ids("[n]etopeer2-server"),
            "p4runtime_available": tcp_port_available(
                args.p4_host,
                args.p4_port,
            ),
            "netconf_available": tcp_port_available(
                args.netconf_host,
                args.netconf_port,
            ),
            "linux_device_present": run_command(
                ["ip", "link", "show", "dev", args.linux_device],
                check=False,
            )["returncode"] == 0,
        }
        service_state["bmv2_pid_continuity"] = (
            service_state["final_bmv2_pids"] == initial_bmv2_pids
        )
        service_state["netopeer_pid_continuity"] = (
            service_state["final_netopeer_pids"] == initial_netopeer_pids
        )

        candidate_checks = {
            "independent_injector": (
                injector.get("fault_source")
                == "independent_multidomain_subprocess"
                and injector.get("controller_notification_sent") is False
                and injector.get("fault_schedule_shared_with_controller") is False
            ),
            "all_fault_writes_accepted": (
                injector.get("all_domain_writes_accepted") is True
            ),
            "all_absence_confirmed": (
                injector.get("all_domain_absence_confirmed") is True
            ),
            "one_cross_domain_incident": (
                controller_snapshot.get("incident_count") == 1
            ),
            "one_successful_global_convergence": (
                controller_snapshot.get("successful_convergence_count") == 1
            ),
            "exact_drifted_domains": (
                drift_domains == {"A", "B", "C"}
                and all(
                    any(drift.startswith(prefix) for drift in observed_drifts)
                    for prefix in expected_drift_prefixes
                )
            ),
            "exact_remediated_domains": (
                remediated_domains == {"A", "B", "C"}
            ),
            "single_remediation_attempt": (
                controller_snapshot.get("remediation_attempt_count") == 1
            ),
            "final_global_convergence": final_observation.healthy,
            "detection_bound": (
                0.0
                <= timing_ms["fault_start_to_drift_confirmation"]
                <= args.maximum_detection_ms
            ),
            "control_plane_recovery_bound": (
                0.0
                <= timing_ms["remediation_start_to_global_convergence"]
                <= args.maximum_control_plane_recovery_ms
            ),
            "total_reconciliation_bound": (
                0.0
                <= timing_ms["fault_start_to_global_convergence"]
                <= args.maximum_total_reconciliation_ms
            ),
            "bmv2_pid_continuity": service_state["bmv2_pid_continuity"],
            "netopeer_pid_continuity": service_state["netopeer_pid_continuity"],
            "p4runtime_available": service_state["p4runtime_available"],
            "netconf_available": service_state["netconf_available"],
            "linux_link_continuity": service_state["linux_device_present"],
        }
        candidate_found = all(candidate_checks.values())

        # Final cleanup is part of the operational result, not an afterthought.
        # Each domain is independently read back after removal, and the Linux
        # test link is removed before the summary is classified as successful.
        if p4 is not None:
            p4.close()
            p4 = None
        cleanup["A"] = linux.cleanup()
        cleanup["B"] = netconf.remove()
        cleanup["B_readback"] = netconf.read_state()
        cleanup["C"] = p4_write_state(
            args,
            context,
            present=False,
            election_low=args.cleanup_election_low,
        )
        cleanup["linux_link"] = cleanup_linux_link(
            args.linux_device,
            args.linux_peer,
        )
        cleanup["errors"] = []
        cleanup["linux_state_absent"] = (
            cleanup["A"].get("accepted") is True
        )
        cleanup["netconf_state_absent"] = (
            cleanup["B"].get("accepted") is True
            and cleanup["B_readback"].get("read_ok") is True
            and (
                cleanup["B_readback"].get("snapshot") or {}
            ).get("qos_present") is False
        )
        cleanup["p4_state_absent"] = (
            cleanup["C"].get("accepted") is True
            and (cleanup["C"].get("after") or {}).get(
                "complete_absent"
            ) is True
        )
        cleanup["linux_link_absent"] = (
            cleanup["linux_link"].get("device_absent") is True
            and cleanup["linux_link"].get("peer_absent") is True
        )
        cleanup_ok = all(
            cleanup[name] is True
            for name in (
                "linux_state_absent",
                "netconf_state_absent",
                "p4_state_absent",
                "linux_link_absent",
            )
        )
        cleanup_performed = True

        operational_checks = {
            "initial_linux_materialization": (
                initial_materialization["A"].get("accepted") is True
            ),
            "initial_netconf_materialization": (
                initial_materialization["B"].get("accepted") is True
            ),
            "initial_p4_materialization": (
                initial_materialization["C"].get("accepted") is True
            ),
            "initial_global_convergence": (
                controller.initial_convergence_event.is_set()
            ),
            "controller_not_failed": controller.failed_event.is_set() is False,
            "controller_stopped": controller_snapshot.get("state") == "stopped",
            "injector_exit_ok": injector_process.returncode == 0,
            "final_observation_read_ok": final_observation.read_ok,
            "final_cleanup_ok": cleanup_ok,
        }
        operational_ok = all(operational_checks.values())

        summary = {
            "scenario": "S2_MAD_multidomain_autonomous_assurance",
            "assurance_profile_id": args.assurance_profile_id,
            "fault_model": "independent_simultaneous_linux_netconf_p4_state_deletion",
            "scope": {
                "persistent_mad_assurance_loop_exercised": True,
                "multi_domain_control_plane_assurance_exercised": True,
                "linux_tc_domain_exercised": True,
                "netconf_yang_domain_exercised": True,
                "p4runtime_domain_exercised": True,
                "global_cross_domain_convergence_rule_exercised": True,
                "selective_domain_remediation_policy_enabled": True,
                "fault_schedule_shared_with_controller": False,
                "independent_fault_injector_process_exercised": True,
                "multi_domain_dataplane_assurance_validated": False,
                "partial_remediation_failure_validated": False,
                "cross_domain_rollback_validated": False,
                "distributed_atomic_transaction_validated": False,
                "bmv2_process_restart_exercised": False,
                "pipeline_reload_exercised": False,
                "netopeer_process_restart_exercised": False,
                "intent_recompilation_validated": False,
                "policy_conflict_resolution_validated": False,
            },
            "configuration": {
                "fault_after_s": args.fault_after_s,
                "linux_device": args.linux_device,
                "linux_peer": args.linux_peer,
                "capacity_mbps": args.capacity_mbps,
                "minimum_mbps": args.minimum_mbps,
                "maximum_mbps": args.maximum_mbps,
                "qos_class": args.qos_class,
                "multicast_group": args.group,
                "multicast_group_id": args.group_id,
                "multicast_ports": list(args.multicast_ports),
                "multicast_port": args.multicast_port,
                "assurance_policy": controller_snapshot.get("policy"),
                "maximum_detection_ms": args.maximum_detection_ms,
                "maximum_control_plane_recovery_ms": (
                    args.maximum_control_plane_recovery_ms
                ),
                "maximum_total_reconciliation_ms": (
                    args.maximum_total_reconciliation_ms
                ),
            },
            "desired_state": aggregate.desired_state,
            "initial_link_setup": link_setup,
            "initial_materialization": initial_materialization,
            "fault_injector": {
                **injector,
                "returncode": injector_process.returncode,
                "stdout": injector_stdout,
                "stderr": injector_stderr,
            },
            "assurance": controller_snapshot,
            "multidomain_adapter": aggregate_snapshot,
            "final_observation": final_observation.to_dict(),
            "timing_ms": timing_ms,
            "classification": {
                "observed_drift_kinds": sorted(observed_drifts),
                "observed_drift_domains": sorted(drift_domains),
                "remediated_domains": sorted(remediated_domains),
                "candidate_checks": candidate_checks,
                "candidate_found": candidate_found,
            },
            "service_continuity": service_state,
            "cleanup": cleanup,
            "cleanup_ok": cleanup_ok,
            "operational_checks": operational_checks,
            "passed": operational_ok,
        }
        dump_json(output_dir / "summary.json", summary)
        summary_written = True

        print(
            "PHASE17_MULTIDOMAIN_PROFILE_ID="
            f"{args.assurance_profile_id}"
        )
        print("PHASE17_MULTIDOMAIN_FAULT_SCHEDULE_SHARED=False")
        print(
            "PHASE17_MULTIDOMAIN_DRIFT_DOMAINS="
            + ",".join(sorted(drift_domains))
        )
        print(
            "PHASE17_MULTIDOMAIN_REMEDIATED_DOMAINS="
            + ",".join(sorted(remediated_domains))
        )
        print(
            "PHASE17_MULTIDOMAIN_DETECTION_MS="
            f"{timing_ms['fault_start_to_drift_confirmation']:.6f}"
        )
        print(
            "PHASE17_MULTIDOMAIN_CONTROL_PLANE_RECOVERY_MS="
            f"{timing_ms['remediation_start_to_global_convergence']:.6f}"
        )
        print(
            "PHASE17_MULTIDOMAIN_TOTAL_RECONCILIATION_MS="
            f"{timing_ms['fault_start_to_global_convergence']:.6f}"
        )
        print(
            "PHASE17_MULTIDOMAIN_REMEDIATION_ATTEMPTS="
            f"{controller_snapshot.get('remediation_attempt_count')}"
        )
        print(
            "PHASE17_MULTIDOMAIN_CANDIDATE_FOUND="
            f"{candidate_found}"
        )
        if not operational_ok:
            return 1
        print("PHASE17_MULTIDOMAIN_ASSURANCE_RUN_OK")
        return 0
    finally:
        stop_event.set()
        if controller_thread is not None and controller_thread.is_alive():
            controller_thread.join(timeout=2.0)
        if injector_process is not None and injector_process.poll() is None:
            injector_process.terminate()
            try:
                injector_process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                injector_process.kill()
        cleanup_errors: list[str] = []
        if not cleanup_performed:
            if linux is not None:
                try:
                    cleanup["A"] = linux.cleanup()
                except Exception as exc:
                    cleanup_errors.append(f"A:{type(exc).__name__}:{exc}")
            if netconf is not None:
                try:
                    cleanup["B"] = netconf.remove()
                except Exception as exc:
                    cleanup_errors.append(f"B:{type(exc).__name__}:{exc}")
            try:
                cleanup["C"] = p4_write_state(
                    args,
                    context,
                    present=False,
                    election_low=args.cleanup_election_low,
                )
            except Exception as exc:
                cleanup_errors.append(f"C:{type(exc).__name__}:{exc}")
            try:
                cleanup["linux_link"] = cleanup_linux_link(
                    args.linux_device,
                    args.linux_peer,
                )
            except Exception as exc:
                cleanup_errors.append(f"link:{type(exc).__name__}:{exc}")
        if p4 is not None:
            try:
                p4.close()
            except Exception as exc:
                cleanup_errors.append(f"p4-close:{type(exc).__name__}:{exc}")
        cleanup.setdefault("errors", []).extend(cleanup_errors)
        dump_json(output_dir / "cleanup.json", cleanup)
        if summary_written and cleanup_errors:
            summary_path = output_dir / "summary.json"
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                summary["cleanup"] = cleanup
                summary["cleanup_ok"] = False
                summary["passed"] = False
                dump_json(summary_path, summary)
            except Exception:
                pass


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Add shared Linux, NETCONF, P4Runtime, and desired-state arguments."""

    parser.add_argument("--linux-device", default="l2i-md-a0")
    parser.add_argument("--linux-peer", default="l2i-md-a1")
    parser.add_argument("--netconf-host", default="127.0.0.1")
    parser.add_argument("--netconf-port", type=int, default=830)
    parser.add_argument("--netconf-username", default="netconf")
    parser.add_argument("--netconf-key", default="~/.ssh/l2i_netconf_key")
    parser.add_argument("--netconf-timeout-s", type=float, default=5.0)
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
    parser.add_argument("--qos-class", default="prio10")
    parser.add_argument("--capacity-mbps", type=float, default=3.0)
    parser.add_argument("--minimum-mbps", type=float, default=2.0)
    parser.add_argument("--maximum-mbps", type=float, default=3.0)
    parser.add_argument("--injector-election-low", type=int, default=17110)


def build_parser() -> argparse.ArgumentParser:
    """Build the independent injector and orchestrator command line."""

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    injector = subparsers.add_parser(
        "inject-fault",
        help="delete Linux, NETCONF, and P4 state from an independent process",
    )
    injector.add_argument("--output", required=True)
    injector.add_argument("--start-barrier", required=True)
    injector.add_argument("--barrier-timeout-s", type=float, default=10.0)
    injector.add_argument("--delay-s", type=float, default=4.0)
    add_common_arguments(injector)

    orchestrator = subparsers.add_parser(
        "orchestrate",
        help="run coordinated autonomous multi-domain assurance",
    )
    orchestrator.add_argument("--output-dir", required=True)
    orchestrator.add_argument(
        "--assurance-profile-id",
        default="phase17-s2-multidomain-assurance-foundation-v1",
    )
    add_common_arguments(orchestrator)
    orchestrator.add_argument("--fault-after-s", type=float, default=4.0)
    orchestrator.add_argument("--injector-barrier-timeout-s", type=float, default=10.0)
    orchestrator.add_argument("--injector-completion-timeout-s", type=float, default=10.0)
    orchestrator.add_argument("--post-convergence-observation-s", type=float, default=0.4)
    orchestrator.add_argument("--maximum-detection-ms", type=float, default=1_500.0)
    orchestrator.add_argument(
        "--maximum-control-plane-recovery-ms",
        type=float,
        default=2_500.0,
    )
    orchestrator.add_argument(
        "--maximum-total-reconciliation-ms",
        type=float,
        default=4_000.0,
    )
    orchestrator.add_argument("--assurance-poll-interval-s", type=float, default=0.05)
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
    orchestrator.add_argument("--assurance-initial-backoff-s", type=float, default=0.05)
    orchestrator.add_argument("--assurance-backoff-multiplier", type=float, default=2.0)
    orchestrator.add_argument("--assurance-maximum-backoff-s", type=float, default=0.5)
    orchestrator.add_argument(
        "--assurance-maximum-consecutive-observation-errors",
        type=int,
        default=3,
    )
    orchestrator.add_argument(
        "--assurance-initial-convergence-timeout-s",
        type=float,
        default=5.0,
    )
    orchestrator.add_argument("--assurance-recovery-timeout-s", type=float, default=8.0)
    orchestrator.add_argument("--assurance-stop-timeout-s", type=float, default=3.0)
    orchestrator.add_argument("--assurance-maximum-runtime-s", type=float, default=30.0)
    orchestrator.add_argument("--initial-cleanup-election-low", type=int, default=17180)
    orchestrator.add_argument("--initial-program-election-low", type=int, default=17190)
    orchestrator.add_argument("--observer-election-low", type=int, default=17100)
    orchestrator.add_argument("--remediation-election-low", type=int, default=17120)
    orchestrator.add_argument("--cleanup-election-low", type=int, default=17990)
    return parser


def main() -> int:
    """Dispatch the independent injector or orchestrator."""

    args = build_parser().parse_args()
    args.netconf_key = os.path.expanduser(args.netconf_key)
    if args.command == "inject-fault":
        return inject_fault(args)
    return orchestrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
