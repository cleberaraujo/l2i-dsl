#!/usr/bin/env python3
"""Validate the Phase 19.5a canonical S1 implementation without traffic."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from l2i.backends import _shim_linux_tc_local as linux_tc
from l2i.backends import _shim_mock_netconf as mock_netconf
from l2i.backends import _shim_mock_p4 as mock_p4
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
    topology_cleanup_path = ROOT / "scripts" / "s1_topology_cleanup.sh"
    dispatcher_path = ROOT / "setup_all.sh"
    netconf_backend_path = (
        ROOT / "l2i" / "backends" / "_shim_real_netconf.py"
    )
    p4_backend_path = ROOT / "l2i" / "backends" / "_shim_real_p4.py"
    specification_path = ROOT / "specs" / "valid" / "s1_unicast_qos.json"

    source = scenario_path.read_text(encoding="utf-8")
    topology = topology_path.read_text(encoding="utf-8")
    topology_cleanup = topology_cleanup_path.read_text(encoding="utf-8")
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
        and intent.delivery_min_ratio == 0.99
        and "max_mbps" not in intent.to_backend_intent(),
    )
    control_intent = intent.to_backend_intent()
    mock_netconf_applied, mock_netconf_response = mock_netconf.apply_qos(
        {"name": "B"},
        control_intent,
        None,
    )
    mock_p4_applied, mock_p4_response = mock_p4.apply_qos(
        {"name": "C"},
        control_intent,
        None,
    )
    mock_netconf_gate, _, _ = s1._gate_control_domain(
        domain="B",
        backend_mode="mock",
        result={
            "applied": mock_netconf_applied,
            "responses": [{"response": mock_netconf_response}],
        },
        intent=intent,
    )
    mock_p4_gate, _, _ = s1._gate_control_domain(
        domain="C",
        backend_mode="mock",
        result={
            "applied": mock_p4_applied,
            "responses": [{"response": mock_p4_response}],
        },
        intent=intent,
    )
    corrupted_mock_p4 = json.loads(json.dumps(mock_p4_response))
    corrupted_mock_p4["request"]["normalized_intent"]["priority"] = (
        "best_effort"
    )
    corrupted_mock_gate, _, _ = s1._gate_control_domain(
        domain="C",
        backend_mode="mock",
        result={
            "applied": True,
            "responses": [{"response": corrupted_mock_p4}],
        },
        intent=intent,
    )
    corrupted_mock_netconf = json.loads(json.dumps(mock_netconf_response))
    corrupted_mock_netconf["request"]["xml"] = (
        corrupted_mock_netconf["request"]["xml"].replace(
            "<min-mbps>4</min-mbps>",
            "<min-mbps>5</min-mbps>",
        )
    )
    corrupted_netconf_gate, _, _ = s1._gate_control_domain(
        domain="B",
        backend_mode="mock",
        result={
            "applied": True,
            "responses": [{"response": corrupted_mock_netconf}],
        },
        intent=intent,
    )
    corrupted_mock_dscp = json.loads(json.dumps(mock_p4_response))
    corrupted_mock_dscp["planned"]["materialized_projection"]["new_dscp"] = 0
    corrupted_dscp_gate, _, _ = s1._gate_control_domain(
        domain="C",
        backend_mode="mock",
        result={
            "applied": True,
            "responses": [{"response": corrupted_mock_dscp}],
        },
        intent=intent,
    )
    netconf_xml = mock_netconf_response.get("request", {}).get("xml", "")
    require(
        "MOCK_INTENT_SEMANTICS_FAIL_CLOSED",
        mock_netconf_gate
        and mock_p4_gate
        and not corrupted_mock_gate
        and not corrupted_netconf_gate
        and not corrupted_dscp_gate
        and "<class>prio10</class>" in netconf_xml
        and "<min-mbps>4</min-mbps>" in netconf_xml
        and "<max-mbps>" not in netconf_xml
        and (
            mock_p4_response.get("planned", {})
            .get("materialized_projection", {})
            .get("new_dscp")
            == 8
        )
        and mock_p4_response.get("planned", {}).get("not_materialized")
        == ["min_mbps"],
    )
    retained_s2_intent = {
        "class": "prio20",
        "min_mbps": 2,
        "max_mbps": 6,
    }
    retained_s1_timing_intent = {
        "latency_pctl": "P99",
        "latency_max_ms": 30,
        "bandwidth_min_mbps": 4,
        "bandwidth_max_mbps": 0,
        "priority_level": "high",
    }
    retained_s2_netconf_ok, retained_s2_netconf = mock_netconf.apply_qos(
        {"name": "B"},
        retained_s2_intent,
        None,
    )
    retained_s2_p4_ok, retained_s2_p4 = mock_p4.apply_qos(
        {"name": "C"},
        retained_s2_intent,
        None,
    )
    retained_timing_netconf_ok, retained_timing_netconf = (
        mock_netconf.apply_qos(
            {"name": "B"},
            retained_s1_timing_intent,
            None,
        )
    )
    retained_timing_p4_ok, retained_timing_p4 = mock_p4.apply_qos(
        {"name": "C"},
        retained_s1_timing_intent,
        None,
    )
    require(
        "MOCK_ACTUAL_RETAINED_SHAPES_SUPPORTED",
        retained_s2_netconf_ok
        and retained_s2_p4_ok
        and retained_timing_netconf_ok
        and retained_timing_p4_ok
        and (
            retained_s2_netconf.get("request", {})
            .get("normalized_intent", {})
            .get("priority")
            == "medium"
        )
        and (
            retained_s2_p4.get("planned", {})
            .get("materialized_projection", {})
            .get("new_dscp")
            == 16
        )
        and (
            retained_timing_netconf.get("request", {})
            .get("normalized_intent")
            == {
                "class": "prio10",
                "min_mbps": 4,
                "priority": "high",
            }
        )
        and (
            retained_timing_p4.get("request", {})
            .get("normalized_intent")
            == {
                "class": "prio10",
                "min_mbps": 4,
                "priority": "high",
            }
        ),
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
                            "new_dscp": 8,
                        },
                    }
                }
            ],
        },
        intent=intent,
    )
    mismatched_p4_gate_ok, _, _ = s1._gate_control_domain(
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
                            "new_dscp": 8,
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
        and not mismatched_p4_gate_ok
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

        accounted_loss_result = s1.TimedCommand(
            name="rtt_probes",
            argv=["ping"],
            returncode=0,
            stdout=(
                "64 bytes from 10.0.0.3: icmp_seq=1 time=1.000 ms\n"
                "2 packets transmitted, 1 received, 50% packet loss\n"
            ),
            stderr="",
            started_offset_s=0.0,
            ended_offset_s=1.0,
            elapsed_s=1.0,
        )
        accounted_loss = s1._parse_ping_measurement(
            result=accounted_loss_result,
            requested_samples=2,
            csv_path=temporary_root / "accounted-loss.csv",
        )
        require(
            "PACKET_LOSS_ACCOUNTED",
            accounted_loss["complete"] is True
            and accounted_loss["received"] == 1
            and accounted_loss["lost"] == 1
            and accounted_loss["missing_sequences"] == [2]
            and accounted_loss["delivery_ratio"] == 0.5
            and (
                temporary_root / "accounted-loss.csv"
            ).read_text(encoding="utf-8").endswith("2,\n"),
        )
        require(
            "DELIVERY_REQUIREMENT_EXPLICIT",
            intent.delivery_min_ratio == 0.99
            and accounted_loss["delivery_ratio"]
            < intent.delivery_min_ratio,
        )
        loss_metrics, loss_conformance = s1._evaluate_conformance(
            intent=intent,
            data_plane={
                "rtt": accounted_loss,
                "sensitive": {"throughput_mbps": 4.0},
                "best_effort": {"throughput_mbps": 30.0},
                "observed_windows": {
                    "simultaneous_overlap_s": 2.9,
                },
            },
            measurement_valid=True,
            bandwidth_tolerance_mbps=0.25,
        )
        require(
            "LOSS_AFFECTS_OVERALL_CONFORMANCE",
            loss_metrics["rtt_lost_probes"] == 1
            and loss_conformance["measurement_valid"] is True
            and loss_conformance["delivery_ok"] is False
            and loss_conformance["intent_ok"] is False,
        )

        inconsistent_result = s1.TimedCommand(
            name="rtt_probes",
            argv=["ping"],
            returncode=2,
            stdout=(
                "64 bytes from 10.0.0.3: icmp_seq=1 time=1.000 ms\n"
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
                result=inconsistent_result,
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
    synthetic_tc_classes = "\n".join(
        (
            "class htb 1:1 root rate 50Mbit ceil 50Mbit",
            "class htb 1:30 parent 1:1 prio 7 rate 46Mbit ceil 50Mbit",
            "class htb 1:10 parent 1:1 prio 0 rate 4000Kbit ceil 0.05Gbit",
        )
    )
    require(
        "TC_READBACK_VALUES_EXPLICIT",
        linux_tc._class_readback_matches(
            synthetic_tc_classes,
            classid="1:30",
            rate_mbps=46,
            ceil_mbps=50,
            priority=7,
        )
        and linux_tc._class_readback_matches(
            synthetic_tc_classes,
            classid="1:10",
            rate_mbps=4,
            ceil_mbps=50,
            priority=0,
        )
        and not linux_tc._class_readback_matches(
            synthetic_tc_classes,
            classid="1:10",
            rate_mbps=5,
            ceil_mbps=50,
            priority=0,
        ),
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
    class NeverReadyServer:
        """Minimal process double for the failed readiness cleanup path."""

        def __init__(self, *, survives_terminate: bool) -> None:
            self.terminated = False
            self.killed = False
            self.survives_terminate = survives_terminate
            self.communicate_calls = 0

        def poll(self) -> None:
            return None

        def communicate(self, timeout: float) -> tuple[str, str]:
            self.communicate_calls += 1
            if self.killed:
                return "", "not ready"
            if not self.terminated or self.survives_terminate:
                raise s1.subprocess.TimeoutExpired(["iperf3"], timeout)
            return "", "not ready"

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    startup_results = []
    for survives_terminate in (False, True):
        never_ready = NeverReadyServer(
            survives_terminate=survives_terminate,
        )
        startup_failure_raised = False
        with (
            patch.object(s1.subprocess, "Popen", return_value=never_ready),
            patch.object(s1, "_server_ready", return_value=False),
            patch.object(s1.time, "sleep", return_value=None),
        ):
            try:
                s1._start_iperf_server(s1.SENSITIVE_PORT)
            except s1.S1ExecutionError:
                startup_failure_raised = True
        startup_results.append(
            (
                startup_failure_raised
                and never_ready.terminated
                and never_ready.communicate_calls >= 2
                and (
                    never_ready.killed
                    if survives_terminate
                    else not never_ready.killed
                )
            )
        )
    require(
        "IPERF_STARTUP_FAILURE_REAPED",
        all(startup_results),
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
        "TOPOLOGY_RUNTIME_HYGIENE",
        "ethtool -K" in topology
        and "PHASE19_S1_TOPOLOGY_OFFLOADS_DISABLED=True" in topology
        and "ip netns pids" in topology
        and "ip netns pids" in topology_cleanup
        and "graphviz ethtool" in dispatcher,
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
