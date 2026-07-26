#!/usr/bin/env python3
"""Validate the Phase 19.5a canonical S1 implementation without traffic."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from l2i.backends import _shim_linux_tc_local as linux_tc
from scenarios import multidomain_s1 as s1


ROOT = Path(__file__).resolve().parents[1]


def require(name: str, condition: bool) -> None:
    """Emit one stable marker and stop immediately on a failed invariant."""

    print(f"PHASE19_S1_STATIC_CHECK_{name}={condition}")
    if not condition:
        raise SystemExit(1)


def _canonical_arguments(specification: Path) -> argparse.Namespace:
    """Build a representative configuration without invoking the CLI."""

    return argparse.Namespace(
        spec=str(specification),
        duration=30,
        flow_mbps=8.0,
        be_mbps=60.0,
        mode="adapt",
        backend="mock",
        bwA=100.0,
        bwB=50.0,
        bwC=100.0,
        delay_ms=1.0,
        rtt_interval_ms=50,
        rtt_samples=None,
        bandwidth_tolerance_mbps=0.25,
        profile_id=s1.DEFAULT_PROFILE_ID,
        repetition=1,
        execution_id=None,
        results_root=str(ROOT / "results"),
    )


def main() -> None:
    """Exercise syntax, semantics, dry-run tc plans, and static boundaries."""

    scenario_path = ROOT / "scenarios" / "multidomain_s1.py"
    topology_path = ROOT / "scripts" / "s1_topology_setup.sh"
    dispatcher_path = ROOT / "setup_all.sh"
    netconf_backend_path = (
        ROOT / "l2i" / "backends" / "_shim_real_netconf.py"
    )
    p4_backend_path = ROOT / "l2i" / "backends" / "_shim_real_p4.py"
    specification_path = ROOT / "specs" / "valid" / "s1_unicast_qos.json"

    source = scenario_path.read_text(encoding="utf-8")
    topology = topology_path.read_text(encoding="utf-8")
    dispatcher = dispatcher_path.read_text(encoding="utf-8")
    netconf_backend = netconf_backend_path.read_text(encoding="utf-8")
    p4_backend = p4_backend_path.read_text(encoding="utf-8")
    specification = json.loads(specification_path.read_text(encoding="utf-8"))

    require(
        "ENTRYPOINT_CANONICAL",
        scenario_path.is_file()
        and "SCENARIO_ID = \"S1\"" in source
        and "multidomain_s1_timing" not in dispatcher,
    )
    require(
        "CONTRACT_INTEGRATED",
        all(
            token in source
            for token in (
                "ExperimentIdentity",
                "ExperimentRunDirectory",
                "RepositoryProvenance",
                "build_run_manifest",
                "atomic_write_json",
            )
        ),
    )
    require(
        "SPECIFICATION_CANONICAL",
        isinstance(specification.get("flow"), dict)
        and specification["flow"].get("id") == "S1_UnicastQoS"
        and "flow_id" not in specification,
    )

    _, intent = s1._load_specification(specification_path)
    require(
        "OPTIONAL_MAX_SEMANTICS",
        intent.bandwidth_min_mbps == 4.0
        and intent.bandwidth_max_mbps is None
        and "max_mbps" not in intent.to_backend_intent(),
    )
    readback_without_maximum = """
<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <data>
    <qos xmlns="urn:l2i:qos">
      <class>prio10</class>
      <min-mbps>4</min-mbps>
    </qos>
  </data>
</rpc-reply>
""".strip()
    stale_readback = readback_without_maximum.replace(
        "</qos>",
        "<max-mbps>7</max-mbps></qos>",
    )
    require(
        "NETCONF_OPTIONAL_MAX_REPLACED",
        'nc:operation="replace"' in netconf_backend
        and 'if max_mbps is not None:' in netconf_backend
        and s1._netconf_snapshot_matches(readback_without_maximum, intent)
        and not s1._netconf_snapshot_matches(stale_readback, intent),
    )
    require(
        "P4_READBACK_VERIFICATION_EXPLICIT",
        '"readback_verified": False' in p4_backend
        and 'info["readback_verified"] = True' in p4_backend
        and 'response.get("readback_verified") is True' in source,
    )
    netconf_gate_ok, _, _ = s1._gate_control_domain(
        domain="B",
        backend_mode="real",
        result={
            "applied": True,
            "responses": [
                {
                    "response": {
                        "running_snapshot": readback_without_maximum,
                    }
                }
            ],
        },
        intent=intent,
    )
    stale_netconf_gate_ok, _, _ = s1._gate_control_domain(
        domain="B",
        backend_mode="real",
        result={
            "applied": True,
            "responses": [
                {
                    "response": {
                        "running_snapshot": stale_readback,
                    }
                }
            ],
        },
        intent=intent,
    )
    p4_gate_ok, _, _ = s1._gate_control_domain(
        domain="C",
        backend_mode="real",
        result={
            "applied": True,
            "responses": [
                {
                    "response": {
                        "readback_dump": "# entries=1",
                        "readback_verified": True,
                        "installed_rule": {
                            "match": (
                                "Ingress.ipv4_qos:"
                                f"{s1.DESTINATION_IP}"
                            ),
                            "new_dscp": 46,
                        },
                    }
                }
            ],
        },
        intent=intent,
    )
    unverified_p4_gate_ok, _, _ = s1._gate_control_domain(
        domain="C",
        backend_mode="real",
        result={
            "applied": True,
            "responses": [
                {
                    "response": {
                        "readback_dump": "# entries=1",
                        "readback_verified": False,
                        "installed_rule": {
                            "match": (
                                "Ingress.ipv4_qos:"
                                f"{s1.DESTINATION_IP}"
                            ),
                            "new_dscp": 46,
                        },
                    }
                }
            ],
        },
        intent=intent,
    )
    require(
        "REAL_READBACK_GATES_FAIL_CLOSED",
        netconf_gate_ok
        and not stale_netconf_gate_ok
        and p4_gate_ok
        and not unverified_p4_gate_ok,
    )

    with tempfile.TemporaryDirectory(prefix="phase19-s1-static-") as temporary:
        temporary_root = Path(temporary)
        bounded_specification = json.loads(
            specification_path.read_text(encoding="utf-8")
        )
        bounded_specification["requirements"]["bandwidth"]["max_mbps"] = 7
        bounded_path = temporary_root / "bounded.json"
        bounded_path.write_text(
            json.dumps(bounded_specification),
            encoding="utf-8",
        )
        _, bounded_intent = s1._load_specification(bounded_path)

        invalid_specification = json.loads(
            specification_path.read_text(encoding="utf-8")
        )
        invalid_specification["requirements"]["bandwidth"]["max_mbps"] = 3
        invalid_path = temporary_root / "invalid.json"
        invalid_path.write_text(
            json.dumps(invalid_specification),
            encoding="utf-8",
        )
        invalid_rejected = False
        try:
            s1._load_specification(invalid_path)
        except s1.S1ExecutionError:
            invalid_rejected = True

        require(
            "MIN_MAX_VALIDATION",
            bounded_intent.bandwidth_max_mbps == 7.0
            and invalid_rejected,
        )

        incomplete_result = s1.TimedCommand(
            name="rtt_probes",
            argv=["ping"],
            returncode=0,
            stdout=(
                "64 bytes from 10.0.0.3: time=1.000 ms\n"
                "2 packets transmitted, 1 received, 50% packet loss\n"
            ),
            stderr="",
            started_offset_s=0.0,
            ended_offset_s=1.0,
            elapsed_s=1.0,
        )
        incomplete_rejected = False
        try:
            s1._parse_ping_measurement(
                result=incomplete_result,
                requested_samples=2,
                csv_path=temporary_root / "incomplete.csv",
            )
        except s1.S1ExecutionError:
            incomplete_rejected = True
        require("INCOMPLETE_MEASUREMENT_REJECTED", incomplete_rejected)

    arguments = _canonical_arguments(specification_path)
    s1._validate_arguments(arguments, intent)
    require(
        "SHARED_BOTTLENECK_CONFIGURATION",
        arguments.bwB < arguments.bwA
        and arguments.bwB < arguments.bwC
        and arguments.flow_mbps + arguments.be_mbps > arguments.bwB
        and s1.BOTTLENECK_INTERFACE == "s1-bc-b",
    )
    require(
        "RTT_COVERS_TRAFFIC_WINDOW",
        s1._derived_rtt_samples(30, 50) == 601,
    )

    environment_ok, environment = linux_tc.setup_environment(
        {"name": "B"},
        {
            "bw_mbps": 50,
            "delay_ms": 1,
            "create_default_class": True,
            "default_priority": 7,
            "default_rate_mbps": 46,
            "default_ceil_mbps": 50,
        },
        {
            "device": s1.BOTTLENECK_INTERFACE,
            "dry_run": True,
            "default_priority": 7,
            "default_rate_mbps": 46,
            "default_ceil_mbps": 50,
            "attach_netem": True,
        },
    )
    environment_commands = environment.get("planned", {}).get("cmds", [])
    require(
        "BEST_EFFORT_PRIORITY_EXPLICIT",
        environment_ok
        and any("classid 1:30" in command and "prio 7" in command
                for command in environment_commands),
    )

    overlay_ok, overlay = linux_tc.apply_qos(
        {"name": "A"},
        {"class": "prio10", "min_mbps": 4, "max_mbps": 50},
        {
            "device": s1.BOTTLENECK_INTERFACE,
            "dry_run": True,
            "default_class": "prio30",
            "htb_priority": 0,
            "classifiers": [
                {
                    "protocol": "tcp",
                    "dst_ip": s1.DESTINATION_IP,
                    "dst_port": s1.SENSITIVE_PORT,
                    "filter_priority": 1,
                },
                {
                    "protocol": "icmp",
                    "dst_ip": s1.DESTINATION_IP,
                    "filter_priority": 2,
                },
            ],
        },
    )
    overlay_commands = overlay.get("planned", {}).get("cmds", [])
    tcp_classified = any(
        "match ip protocol 6 0xff" in command
        and "match ip dport 5201 0xffff" in command
        and "flowid 1:10" in command
        for command in overlay_commands
    )
    icmp_classified = any(
        "match ip protocol 1 0xff" in command
        and "flowid 1:10" in command
        for command in overlay_commands
    )
    require(
        "TCP_AND_RTT_CLASSIFIED",
        overlay_ok and tcp_classified and icmp_classified,
    )
    require(
        "SCHEDULER_PRIORITY_EXPLICIT",
        any(
            "classid 1:10" in command and "prio 0" in command
            for command in overlay_commands
        ),
    )

    require(
        "CONCURRENT_TRAFFIC_MODEL",
        "ThreadPoolExecutor" in source
        and "threading.Barrier(4)" in source
        and "sensitive_tcp" in source
        and "best_effort_tcp" in source
        and "rtt_probes" in source,
    )
    require(
        "BASELINE_ADAPT_DIFFERENTIATED",
        'if args.mode == "adapt":' in source
        and '"A_intent_overlay": overlay is not None' in source,
    )
    require(
        "TIMING_PLANES_SEPARATED",
        '"control_plane_ms"' in source
        and '"data_plane_ms"' in source
        and '"simultaneous_overlap_s"' in source,
    )
    require(
        "BACKEND_GATES_FAIL_CLOSED",
        "_gate_control_domain" in source
        and "readback evidence is incomplete" in source
        and 'raise S1ExecutionError(b_message)' in source
        and 'raise S1ExecutionError(c_message)' in source,
    )
    require(
        "CONTROL_DOMAIN_SCOPE_LIMITED",
        "materialization-and-readback-only" in source
        and '"forwarding_path": "linux-bridge-veth"' in source,
    )
    require(
        "TOPOLOGY_THREE_SEGMENTS",
        all(name in topology for name in ("brA", "brB", "brC"))
        and "create_host h1 10.0.0.1 brA" in topology
        and "create_host h2 10.0.0.2 brA" in topology
        and "create_host h3 10.0.0.3 brC" in topology
        and "create_interdomain_link s1-bc-b brB s1-bc-c brC" in topology,
    )
    require(
        "ATOMIC_FAILURE_SUMMARY",
        'summary["run_status"] = "failed"' in source
        and 'atomic_write_json(artifacts["summary"], summary)' in source,
    )
    require(
        "DISPATCHER_REGISTERED",
        "validate_phase19_s1_static()" in dispatcher
        and (
            "validate_phase19_s1_static) "
            "validate_phase19_s1_static ;;"
        ) in dispatcher,
    )

    print("PHASE19_S1_STATIC_VALIDATION_OK")


if __name__ == "__main__":
    main()
