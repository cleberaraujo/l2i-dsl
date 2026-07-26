#!/usr/bin/env python3
"""Exercise selective multidomain assurance and bounded synthetic rejection.

The scenario reuses the certified Phase 17 Linux TC, NETCONF/YANG, P4Runtime,
MAD controller, and multidomain aggregation primitives. An independent
subprocess can delete any selected subset of domains or execute a no-fault
control. The orchestration then verifies exact drift classification, exact
selective remediation, preservation of unaffected domains, bounded retry, and
aggregate convergence.

Synthetic rejection is injected above a backend callback and is explicitly a
test mechanism. It does not claim a real NETCONF, Linux, or P4Runtime backend
failure. Cross-domain rollback, distributed atomicity, and multidomain
dataplane assurance remain outside this scenario's scope.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Mapping, NoReturn

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from l2i.assurance import AssurancePolicy, MADAssuranceController
from l2i.multidomain_assurance import (
    DomainAssuranceBinding,
    MultiDomainAssuranceAdapter,
)
from scripts import s2_multidomain_autonomous_assurance as phase17
from scripts.s2_p4_autonomous_assurance import P4MulticastAssuranceAdapter
from scripts.s2_p4_state_recovery import process_ids, tcp_port_available


DOMAIN_ORDER = ("A", "B", "C")
DOMAIN_TECHNOLOGY = {
    "A": "linux_tc_htb",
    "B": "netconf_yang",
    "C": "p4runtime",
}


def fail(message: str) -> NoReturn:
    """Terminate with one stable Phase 18 diagnostic marker."""

    print(
        f"PHASE18_SELECTIVE_ASSURANCE_FAILED: {message}",
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


def parse_fault_domains(raw: str) -> tuple[str, ...]:
    """Normalize a comma-separated domain subset in stable domain order."""

    tokens = [
        token.strip().upper()
        for token in str(raw or "").split(",")
        if token.strip()
    ]
    unknown = sorted(set(tokens) - set(DOMAIN_ORDER))
    if unknown:
        raise ValueError(
            "unknown fault domains: " + ",".join(unknown)
        )
    if len(tokens) != len(set(tokens)):
        raise ValueError("fault domains cannot contain duplicates")
    selected = set(tokens)
    return tuple(domain for domain in DOMAIN_ORDER if domain in selected)


def event_by_type(
    events: list[dict[str, Any]],
    event_type: str,
) -> dict[str, Any] | None:
    """Return the first controller event with one exact type."""

    return next(
        (
            event
            for event in events
            if event.get("event_type") == event_type
        ),
        None,
    )


def events_by_type(
    events: list[dict[str, Any]],
    event_type: str,
) -> list[dict[str, Any]]:
    """Return all controller events with one exact type."""

    return [
        event
        for event in events
        if event.get("event_type") == event_type
    ]


def p4_presence_observation(
    args: argparse.Namespace,
    context: phase17.P4Context,
) -> dict[str, Any]:
    """Read P4 multicast state from the independent subprocess."""

    probe = P4MulticastAssuranceAdapter(
        p4_addr=args.p4_addr,
        device_id=args.device_id,
        p4_timeout_s=args.p4_timeout_s,
        observer_election_low=args.injector_election_low,
        remediation_election_low=args.injector_election_low + 1,
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
    try:
        observation = probe.observe()
    finally:
        probe.close()
    return {
        "domain": "C",
        "technology": DOMAIN_TECHNOLOGY["C"],
        "presence_confirmed": observation.healthy,
        "observation": observation.to_dict(),
    }


def inject_fault(args: argparse.Namespace) -> int:
    """Delete only selected domains from an independent subprocess."""

    try:
        selected_domains = parse_fault_domains(args.fault_domains)
    except ValueError as exc:
        fail(str(exc))

    unaffected_domains = tuple(
        domain
        for domain in DOMAIN_ORDER
        if domain not in selected_domains
    )
    barrier = Path(args.start_barrier)
    output = Path(args.output)
    if not phase17.wait_for_barrier(barrier, args.barrier_timeout_s):
        fail("fault injector did not observe the start barrier")

    barrier_ns = time.monotonic_ns()
    time.sleep(args.delay_s)
    context = phase17.P4Context(args)
    linux = phase17.LinuxTCAssuranceAdapter(
        device=args.linux_device,
        capacity_mbps=args.capacity_mbps,
        minimum_mbps=args.minimum_mbps,
        maximum_mbps=args.maximum_mbps,
        multicast_group=args.group,
        multicast_port=args.multicast_port,
    )
    netconf = phase17.NetconfQosAssuranceAdapter(
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
        command = phase17.run_command(
            ["tc", "qdisc", "del", "dev", args.linux_device, "root"],
            check=False,
        )
        observation = linux.observe()
        checks = dict(observation.observed.get("checks") or {})
        completed_ns = time.monotonic_ns()
        return {
            "domain": "A",
            "technology": DOMAIN_TECHNOLOGY["A"],
            "started_monotonic_ns": started_ns,
            "completed_monotonic_ns": completed_ns,
            "write_accepted": command["returncode"] == 0,
            "absence_confirmed": (
                observation.read_ok is True
                and checks.get("root_htb") is False
            ),
            "command": command,
            "readback": observation.to_dict(),
        }

    def fault_netconf() -> dict[str, Any]:
        started_ns = time.monotonic_ns()
        removal = netconf.remove()
        observation = netconf.observe()
        readback = dict(observation.observed.get("readback") or {})
        snapshot = dict(readback.get("snapshot") or {})
        completed_ns = time.monotonic_ns()
        return {
            "domain": "B",
            "technology": DOMAIN_TECHNOLOGY["B"],
            "started_monotonic_ns": started_ns,
            "completed_monotonic_ns": completed_ns,
            "write_accepted": removal.get("accepted") is True,
            "absence_confirmed": (
                observation.read_ok is True
                and snapshot.get("qos_present") is False
            ),
            "removal": removal,
            "readback": observation.to_dict(),
        }

    def fault_p4() -> dict[str, Any]:
        started_ns = time.monotonic_ns()
        removal = phase17.p4_write_state(
            args,
            context,
            present=False,
            election_low=args.injector_election_low,
        )
        completed_ns = time.monotonic_ns()
        return {
            "domain": "C",
            "technology": DOMAIN_TECHNOLOGY["C"],
            "started_monotonic_ns": started_ns,
            "completed_monotonic_ns": completed_ns,
            "write_accepted": removal.get("accepted") is True,
            "absence_confirmed": (
                (removal.get("after") or {}).get("complete_absent") is True
            ),
            "removal": removal,
        }

    fault_functions = {
        "A": fault_linux,
        "B": fault_netconf,
        "C": fault_p4,
    }
    fault_started_ns = time.monotonic_ns()
    selected_results: dict[str, dict[str, Any]] = {}
    if selected_domains:
        with ThreadPoolExecutor(
            max_workers=len(selected_domains)
        ) as executor:
            future_map = {
                executor.submit(fault_functions[domain]): domain
                for domain in selected_domains
            }
            for future in as_completed(future_map):
                domain = future_map[future]
                try:
                    selected_results[domain] = future.result()
                except Exception as exc:
                    selected_results[domain] = {
                        "domain": domain,
                        "technology": DOMAIN_TECHNOLOGY[domain],
                        "write_accepted": False,
                        "absence_confirmed": False,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "completed_monotonic_ns": time.monotonic_ns(),
                    }

    # The independent process also proves that every non-selected domain remains
    # present. This readback is separate from the controller's classification.
    unaffected_presence: dict[str, dict[str, Any]] = {}
    for domain in unaffected_domains:
        try:
            if domain == "A":
                observation = linux.observe()
                record = {
                    "domain": "A",
                    "technology": DOMAIN_TECHNOLOGY["A"],
                    "presence_confirmed": observation.healthy,
                    "observation": observation.to_dict(),
                }
            elif domain == "B":
                observation = netconf.observe()
                record = {
                    "domain": "B",
                    "technology": DOMAIN_TECHNOLOGY["B"],
                    "presence_confirmed": observation.healthy,
                    "observation": observation.to_dict(),
                }
            else:
                record = p4_presence_observation(args, context)
        except Exception as exc:
            record = {
                "domain": domain,
                "technology": DOMAIN_TECHNOLOGY[domain],
                "presence_confirmed": False,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
        unaffected_presence[domain] = record

    fault_completed_ns = time.monotonic_ns()
    selected_writes_accepted = all(
        selected_results.get(domain, {}).get("write_accepted") is True
        for domain in selected_domains
    )
    selected_absence_confirmed = all(
        selected_results.get(domain, {}).get("absence_confirmed") is True
        for domain in selected_domains
    )
    unaffected_presence_confirmed = all(
        unaffected_presence.get(domain, {}).get("presence_confirmed") is True
        for domain in unaffected_domains
    )
    all_domains_selected = set(selected_domains) == set(DOMAIN_ORDER)
    evidence = {
        "fault_source": "independent_selective_multidomain_subprocess",
        "controller_notification_sent": False,
        "fault_schedule_shared_with_controller": False,
        "fault_performed": bool(selected_domains),
        "control_condition": not selected_domains,
        "start_barrier": str(barrier),
        "barrier_observed_monotonic_ns": barrier_ns,
        "configured_delay_s": args.delay_s,
        "fault_started_monotonic_ns": fault_started_ns,
        "fault_completed_monotonic_ns": fault_completed_ns,
        "selected_fault_domains": list(selected_domains),
        "unaffected_domains": list(unaffected_domains),
        "domains": selected_results,
        "unaffected_presence": unaffected_presence,
        "all_selected_writes_accepted": selected_writes_accepted,
        "all_selected_absence_confirmed": selected_absence_confirmed,
        "all_unaffected_presence_confirmed": unaffected_presence_confirmed,
        # Compatibility fields remain meaningful for the original all-domain
        # Phase 17 condition while selective Phase 18 gates use the explicit
        # selected/unaffected fields above.
        "all_domain_writes_accepted": (
            all_domains_selected and selected_writes_accepted
        ),
        "all_domain_absence_confirmed": (
            all_domains_selected and selected_absence_confirmed
        ),
    }
    dump_json(output, evidence)
    print(
        "PHASE18_INJECTOR_SELECTED_DOMAINS="
        + ",".join(selected_domains)
    )
    print(
        "PHASE18_INJECTOR_UNAFFECTED_DOMAINS="
        + ",".join(unaffected_domains)
    )
    print(
        "PHASE18_INJECTOR_SELECTED_WRITES_ACCEPTED="
        f"{selected_writes_accepted}"
    )
    print(
        "PHASE18_INJECTOR_SELECTED_ABSENCE_CONFIRMED="
        f"{selected_absence_confirmed}"
    )
    print(
        "PHASE18_INJECTOR_UNAFFECTED_PRESENCE_CONFIRMED="
        f"{unaffected_presence_confirmed}"
    )
    print("PHASE18_INJECTOR_CONTROLLER_NOTIFICATION_SENT=False")
    if not all(
        (
            selected_writes_accepted,
            selected_absence_confirmed,
            unaffected_presence_confirmed,
        )
    ):
        return 1
    print("PHASE18_SELECTIVE_INJECTOR_OK")
    return 0


def build_bindings(
    linux: phase17.LinuxTCAssuranceAdapter,
    netconf: phase17.NetconfQosAssuranceAdapter,
    p4: P4MulticastAssuranceAdapter,
    context: phase17.P4Context,
) -> tuple[DomainAssuranceBinding, ...]:
    """Build the certified heterogeneous domain bindings."""

    return (
        DomainAssuranceBinding(
            domain_id="A",
            technology=DOMAIN_TECHNOLOGY["A"],
            desired_state=linux.desired_state,
            observe_fn=linux.observe,
            remediate_fn=linux.remediate,
        ),
        DomainAssuranceBinding(
            domain_id="B",
            technology=DOMAIN_TECHNOLOGY["B"],
            desired_state=netconf.desired_state,
            observe_fn=netconf.observe,
            remediate_fn=netconf.remediate,
        ),
        DomainAssuranceBinding(
            domain_id="C",
            technology=DOMAIN_TECHNOLOGY["C"],
            desired_state=context.desired_state,
            observe_fn=p4.observe,
            remediate_fn=p4.remediate,
        ),
    )


def service_continuity(
    args: argparse.Namespace,
    *,
    initial_bmv2_pids: list[int],
    initial_netopeer_pids: list[int],
) -> dict[str, Any]:
    """Read process, port, and Linux-link continuity without modification."""

    state = {
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
        "linux_device_present": phase17.run_command(
            ["ip", "link", "show", "dev", args.linux_device],
            check=False,
        )["returncode"] == 0,
    }
    state["bmv2_pid_continuity"] = (
        state["final_bmv2_pids"] == initial_bmv2_pids
    )
    state["netopeer_pid_continuity"] = (
        state["final_netopeer_pids"] == initial_netopeer_pids
    )
    return state


def parse_remediation_event(
    event: dict[str, Any],
) -> dict[str, Any]:
    """Extract the multidomain remediation record from a controller event."""

    event_details = dict(event.get("details") or {})
    return dict(event_details.get("details") or {})


def find_preservation_observation(
    controller_snapshot: dict[str, Any],
    remediation_events: list[dict[str, Any]],
    synthetic_domain: str | None,
) -> dict[str, Any] | None:
    """Find evidence that converged domains stayed healthy before retry."""

    if synthetic_domain is None or len(remediation_events) < 2:
        return None
    started_events = events_by_type(
        list(controller_snapshot.get("events") or []),
        "remediation_attempt_started",
    )
    if len(started_events) < 2:
        return None
    first_completed_ns = int(remediation_events[0]["monotonic_ns"])
    second_started = started_events[1]
    second_started_ns = int(second_started["monotonic_ns"])
    expected_healthy = sorted(
        domain for domain in DOMAIN_ORDER if domain != synthetic_domain
    )
    for observation in controller_snapshot.get("observations") or []:
        observed_ns = int(observation.get("observed_monotonic_ns") or 0)
        aggregate = dict(observation.get("observed") or {})
        healthy_domains = sorted(aggregate.get("healthy_domains") or [])
        drifted_domains = sorted(aggregate.get("drifted_domains") or [])
        if (
            first_completed_ns < observed_ns < second_started_ns
            and healthy_domains == expected_healthy
            and drifted_domains == [synthetic_domain]
        ):
            return dict(observation)
    return None


def orchestrate(args: argparse.Namespace) -> int:
    """Run one no-fault, selective-drift, or synthetic-retry condition."""

    try:
        selected_domains = parse_fault_domains(args.fault_domains)
    except ValueError as exc:
        fail(str(exc))
    selected_set = set(selected_domains)
    unaffected_domains = tuple(
        domain for domain in DOMAIN_ORDER if domain not in selected_set
    )
    synthetic_domain = (
        args.synthetic_rejection_domain.strip().upper() or None
    )
    if synthetic_domain is not None and synthetic_domain not in DOMAIN_ORDER:
        fail("synthetic rejection domain must be A, B, or C")
    if args.synthetic_rejection_count < 0:
        fail("synthetic rejection count cannot be negative")
    if args.synthetic_rejection_count > 0 and synthetic_domain is None:
        fail("synthetic rejection count requires a domain")
    if synthetic_domain is not None and synthetic_domain not in selected_set:
        fail("synthetic rejection domain must also be faulted")
    if not selected_domains and args.synthetic_rejection_count:
        fail("no-fault control cannot request synthetic rejection")
    if args.timing_candidate_policy not in {"enforce", "observe-only"}:
        fail("invalid timing candidate policy")

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

    first_remediation_election_low = args.remediation_election_low + 101
    last_remediation_election_low = (
        args.remediation_election_low
        + 100
        + args.assurance_maximum_remediation_attempts
    )
    election_plan = (
        args.observer_election_low,
        args.injector_election_low,
        args.initial_cleanup_election_low,
        args.initial_program_election_low,
        first_remediation_election_low,
        last_remediation_election_low,
        args.cleanup_election_low,
    )
    if (
        election_plan != tuple(sorted(election_plan))
        or len(set(election_plan)) != len(election_plan)
    ):
        fail("invalid strictly increasing P4Runtime election plan")

    context = phase17.P4Context(args)
    linux: phase17.LinuxTCAssuranceAdapter | None = None
    netconf: phase17.NetconfQosAssuranceAdapter | None = None
    p4: P4MulticastAssuranceAdapter | None = None
    aggregate: MultiDomainAssuranceAdapter | None = None
    controller: MADAssuranceController | None = None
    controller_thread: threading.Thread | None = None
    injector_process: subprocess.Popen[str] | None = None
    stop_event = threading.Event()
    cleanup: dict[str, Any] = {}
    cleanup_performed = False
    summary_written = False

    try:
        link_setup = phase17.setup_linux_link(
            args.linux_device,
            args.linux_peer,
        )
        if not link_setup["device_present"] or not link_setup["peer_present"]:
            fail("dedicated Linux assurance veth was not created")

        linux, netconf, p4 = phase17.build_adapters(args, context)
        initial_cleanup = {
            "A": linux.cleanup(),
            "B": netconf.remove(),
            "C": phase17.p4_write_state(
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
            "C": phase17.p4_write_state(
                args,
                context,
                present=True,
                election_low=args.initial_program_election_low,
            ),
        }
        dump_json(
            output_dir / "initial-materialization.json",
            initial_materialization,
        )
        failed_initial_domains = [
            domain
            for domain in DOMAIN_ORDER
            if initial_materialization[domain].get("accepted") is not True
        ]
        for domain in DOMAIN_ORDER:
            print(
                f"PHASE18_INITIAL_MATERIALIZATION_{domain}_ACCEPTED="
                f"{initial_materialization[domain].get('accepted') is True}"
            )
        if failed_initial_domains:
            fail(
                "initial multidomain materialization failed: "
                + ",".join(failed_initial_domains)
            )

        rejection_budget = (
            {synthetic_domain: args.synthetic_rejection_count}
            if synthetic_domain is not None
            else {}
        )
        aggregate = MultiDomainAssuranceAdapter(
            build_bindings(linux, netconf, p4, context),
            synthetic_rejection_budget=rejection_budget,
        )
        controller = MADAssuranceController(
            controller_id="phase18-mad-selective-multidomain-assurance",
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
            name="phase18-mad-selective-multidomain-assurance",
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
            "--fault-domains", ",".join(selected_domains),
            "--linux-device", args.linux_device,
            "--linux-peer", args.linux_peer,
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
        injector_wait_timeout_s = (
            args.fault_after_s + args.injector_completion_timeout_s
        )
        try:
            injector_stdout, injector_stderr = injector_process.communicate(
                timeout=injector_wait_timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            fail(
                "independent injector did not complete within "
                f"{injector_wait_timeout_s:.3f}s: {exc}"
            )
        if injector_process.returncode != 0:
            fail(
                "independent injector failed: "
                f"stdout={injector_stdout} stderr={injector_stderr}"
            )
        if not injector_output.is_file():
            fail("independent injector evidence is missing")
        injector = json.loads(
            injector_output.read_text(encoding="utf-8")
        )
        injector_ok = all(
            (
                injector.get("all_selected_writes_accepted") is True,
                injector.get("all_selected_absence_confirmed") is True,
                injector.get("all_unaffected_presence_confirmed") is True,
                set(injector.get("selected_fault_domains") or [])
                == selected_set,
                set(injector.get("unaffected_domains") or [])
                == set(unaffected_domains),
            )
        )
        if not injector_ok:
            fail("independent injector evidence did not match the condition")
        print("PHASE18_INJECTOR_VERIFIED_BEFORE_ASSURANCE_WAIT=True")

        if selected_domains:
            wait_outcome = phase17.wait_for_recovery_or_failure(
                controller,
                args.assurance_recovery_timeout_s,
            )
            if wait_outcome != "recovered":
                diagnostic = {
                    "outcome": wait_outcome,
                    "selected_fault_domains": list(selected_domains),
                    "controller": controller.snapshot(),
                    "multidomain_adapter": aggregate.snapshot(),
                    "final_observation": aggregate.observe().to_dict(),
                    "injector": injector,
                }
                dump_json(
                    output_dir / "recovery-wait-diagnostic.json",
                    diagnostic,
                )
                if wait_outcome == "controller_failed":
                    fail("assurance controller failed before convergence")
                fail("selective multidomain convergence was not confirmed")
            time.sleep(args.post_convergence_observation_s)
        else:
            control_deadline = time.monotonic() + args.no_fault_observation_s
            while time.monotonic() < control_deadline:
                if controller.failed_event.is_set():
                    fail("controller failed during no-fault observation")
                time.sleep(min(0.05, args.no_fault_observation_s))

        stop_event.set()
        controller_thread.join(args.assurance_stop_timeout_s)
        if controller_thread.is_alive():
            fail("assurance controller did not stop")

        controller_snapshot = controller.snapshot()
        aggregate_snapshot = aggregate.snapshot()
        final_observation = aggregate.observe()
        events = list(controller_snapshot.get("events") or [])
        drift_events = events_by_type(events, "drift_confirmed")
        remediation_started_events = events_by_type(
            events,
            "remediation_attempt_started",
        )
        remediation_completed_events = events_by_type(
            events,
            "remediation_attempt_completed",
        )
        convergence_events = events_by_type(
            events,
            "convergence_confirmed",
        )
        backoff_events = events_by_type(
            events,
            "remediation_backoff_started",
        )

        timing_ms: dict[str, float | None] = {
            "fault_start_to_drift_confirmation": None,
            "remediation_start_to_global_convergence": None,
            "fault_start_to_global_convergence": None,
            "control_observation_duration": (
                args.no_fault_observation_s * 1000.0
                if not selected_domains
                else None
            ),
        }
        observed_drifts: set[str] = set()
        drift_domains: set[str] = set()
        remediation_request_sequence: list[list[str]] = []
        remediated_domains: set[str] = set()
        preservation_observation: dict[str, Any] | None = None

        if selected_domains:
            if not drift_events or not remediation_started_events:
                fail("required selective drift/remediation events are missing")
            if not remediation_completed_events or not convergence_events:
                fail("required selective convergence events are missing")
            drift_event = drift_events[0]
            convergence_event = convergence_events[0]
            fault_started_ns = int(
                injector["fault_started_monotonic_ns"]
            )
            drift_ns = int(drift_event["monotonic_ns"])
            first_remediation_ns = int(
                remediation_started_events[0]["monotonic_ns"]
            )
            convergence_ns = int(convergence_event["monotonic_ns"])
            timing_ms.update(
                {
                    "fault_start_to_drift_confirmation": (
                        drift_ns - fault_started_ns
                    ) / 1_000_000.0,
                    "remediation_start_to_global_convergence": (
                        convergence_ns - first_remediation_ns
                    ) / 1_000_000.0,
                    "fault_start_to_global_convergence": (
                        convergence_ns - fault_started_ns
                    ) / 1_000_000.0,
                }
            )
            observed_drifts = set(
                (drift_event.get("details") or {}).get("drift_kinds")
                or []
            )
            drift_domains = {
                drift.split(":", 1)[0]
                for drift in observed_drifts
                if ":" in drift
            }
            for event in remediation_completed_events:
                record = parse_remediation_event(event)
                requested = list(record.get("requested_domains") or [])
                remediation_request_sequence.append(requested)
                remediated_domains.update(requested)
            preservation_observation = find_preservation_observation(
                controller_snapshot,
                remediation_completed_events,
                synthetic_domain,
            )

        continuity = service_continuity(
            args,
            initial_bmv2_pids=initial_bmv2_pids,
            initial_netopeer_pids=initial_netopeer_pids,
        )
        domain_attempt_counts = dict(
            aggregate_snapshot.get("domain_remediation_counts") or {}
        )
        backend_counts = dict(
            aggregate_snapshot.get("domain_backend_remediation_counts")
            or {}
        )
        synthetic_counts = dict(
            aggregate_snapshot.get("domain_synthetic_rejection_counts")
            or {}
        )

        candidate_checks: dict[str, bool] = {
            "independent_subprocess": (
                injector.get("fault_source")
                == "independent_selective_multidomain_subprocess"
                and injector.get("controller_notification_sent") is False
                and injector.get("fault_schedule_shared_with_controller")
                is False
            ),
            "exact_selected_fault_domains": (
                set(injector.get("selected_fault_domains") or [])
                == selected_set
            ),
            "selected_writes_accepted": (
                injector.get("all_selected_writes_accepted") is True
            ),
            "selected_absence_confirmed": (
                injector.get("all_selected_absence_confirmed") is True
            ),
            "unaffected_presence_confirmed": (
                injector.get("all_unaffected_presence_confirmed") is True
            ),
            "final_global_convergence": final_observation.healthy,
            "bmv2_pid_continuity": continuity["bmv2_pid_continuity"],
            "netopeer_pid_continuity": continuity[
                "netopeer_pid_continuity"
            ],
            "p4runtime_available": continuity["p4runtime_available"],
            "netconf_available": continuity["netconf_available"],
            "linux_link_continuity": continuity["linux_device_present"],
        }

        if not selected_domains:
            candidate_checks.update(
                {
                    "no_fault_control": (
                        injector.get("fault_performed") is False
                        and injector.get("control_condition") is True
                    ),
                    "zero_incidents": (
                        controller_snapshot.get("incident_count") == 0
                    ),
                    "zero_successful_recoveries": (
                        controller_snapshot.get(
                            "successful_convergence_count"
                        )
                        == 0
                    ),
                    "zero_remediation_attempts": (
                        controller_snapshot.get(
                            "remediation_attempt_count"
                        )
                        == 0
                    ),
                    "no_drift_confirmed_event": not drift_events,
                    "no_remediation_events": (
                        not remediation_started_events
                        and not remediation_completed_events
                    ),
                    "zero_domain_remediation_counts": all(
                        int(domain_attempt_counts.get(domain, 0)) == 0
                        for domain in DOMAIN_ORDER
                    ),
                }
            )
        else:
            expected_attempts = 1 + args.synthetic_rejection_count
            expected_domain_attempts = {
                domain: (
                    expected_attempts
                    if domain == synthetic_domain
                    else (1 if domain in selected_set else 0)
                )
                for domain in DOMAIN_ORDER
            }
            expected_backend_counts = {
                domain: (1 if domain in selected_set else 0)
                for domain in DOMAIN_ORDER
            }
            candidate_checks.update(
                {
                    "fault_performed": injector.get("fault_performed") is True,
                    "one_incident": (
                        controller_snapshot.get("incident_count") == 1
                    ),
                    "one_successful_global_convergence": (
                        controller_snapshot.get(
                            "successful_convergence_count"
                        )
                        == 1
                    ),
                    "exact_drift_domains": drift_domains == selected_set,
                    "exact_remediated_domains": (
                        remediated_domains == selected_set
                    ),
                    "exact_total_remediation_attempts": (
                        controller_snapshot.get(
                            "remediation_attempt_count"
                        )
                        == expected_attempts
                    ),
                    "exact_domain_attempt_counts": all(
                        int(domain_attempt_counts.get(domain, 0))
                        == expected_domain_attempts[domain]
                        for domain in DOMAIN_ORDER
                    ),
                    "exact_backend_callback_counts": all(
                        int(backend_counts.get(domain, 0))
                        == expected_backend_counts[domain]
                        for domain in DOMAIN_ORDER
                    ),
                    "unaffected_domains_not_remediated": all(
                        int(domain_attempt_counts.get(domain, 0)) == 0
                        for domain in unaffected_domains
                    ),
                    "exact_synthetic_rejection_counts": all(
                        int(synthetic_counts.get(domain, 0))
                        == (
                            args.synthetic_rejection_count
                            if domain == synthetic_domain
                            else 0
                        )
                        for domain in DOMAIN_ORDER
                    ),
                }
            )
            if args.synthetic_rejection_count:
                expected_first = list(selected_domains)
                candidate_checks.update(
                    {
                        "synthetic_rejection_exercised": (
                            synthetic_domain is not None
                            and synthetic_counts.get(synthetic_domain)
                            == args.synthetic_rejection_count
                        ),
                        "retry_request_sequence": (
                            remediation_request_sequence
                            == [expected_first, [synthetic_domain]]
                        ),
                        "backoff_exercised": (
                            len(backoff_events)
                            == args.synthetic_rejection_count
                        ),
                        "converged_domains_preserved_before_retry": (
                            preservation_observation is not None
                        ),
                    }
                )
            else:
                candidate_checks.update(
                    {
                        "single_request_sequence": (
                            remediation_request_sequence
                            == [list(selected_domains)]
                        ),
                        "no_backoff": not backoff_events,
                        "no_synthetic_rejection": all(
                            int(value) == 0
                            for value in synthetic_counts.values()
                        ),
                    }
                )
            if args.timing_candidate_policy == "enforce":
                candidate_checks.update(
                    {
                        "detection_bound": (
                            0.0
                            <= float(
                                timing_ms[
                                    "fault_start_to_drift_confirmation"
                                ]
                            )
                            <= args.maximum_detection_ms
                        ),
                        "control_plane_recovery_bound": (
                            0.0
                            <= float(
                                timing_ms[
                                    "remediation_start_to_global_convergence"
                                ]
                            )
                            <= args.maximum_control_plane_recovery_ms
                        ),
                        "total_reconciliation_bound": (
                            0.0
                            <= float(
                                timing_ms[
                                    "fault_start_to_global_convergence"
                                ]
                            )
                            <= args.maximum_total_reconciliation_ms
                        ),
                    }
                )

        candidate_found = all(candidate_checks.values())

        # Final cleanup remains an operational gate for every condition.
        if p4 is not None:
            p4.close()
            p4 = None
        cleanup["A"] = linux.cleanup()
        cleanup["B"] = netconf.remove()
        cleanup["B_readback"] = netconf.read_state()
        cleanup["C"] = phase17.p4_write_state(
            args,
            context,
            present=False,
            election_low=args.cleanup_election_low,
        )
        cleanup["linux_link"] = phase17.cleanup_linux_link(
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
            and (cleanup["C"].get("after") or {}).get("complete_absent")
            is True
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
            "scenario": "S2_MAD_multidomain_selective_assurance",
            "assurance_profile_id": args.assurance_profile_id,
            "condition_id": args.condition_id,
            "fault_model": (
                "independent_no_fault_control"
                if not selected_domains
                else "independent_selective_multidomain_state_deletion"
            ),
            "scope": {
                "persistent_mad_assurance_loop_exercised": True,
                "multi_domain_control_plane_assurance_exercised": True,
                "selective_domain_fault_injection_exercised": bool(
                    selected_domains
                ),
                "selective_domain_remediation_policy_exercised": True,
                "no_fault_control_exercised": not selected_domains,
                "synthetic_partial_remediation_rejection_exercised": (
                    args.synthetic_rejection_count > 0
                ),
                "selective_retry_after_synthetic_rejection_exercised": (
                    args.synthetic_rejection_count > 0
                ),
                "fault_schedule_shared_with_controller": False,
                "independent_fault_injector_process_exercised": True,
                "multi_domain_dataplane_assurance_validated": False,
                "real_backend_partial_remediation_failure_validated": False,
                "partial_remediation_failure_validated": False,
                "cross_domain_rollback_validated": False,
                "distributed_atomic_transaction_validated": False,
                "intent_recompilation_validated": False,
                "policy_conflict_resolution_validated": False,
            },
            "configuration": {
                "condition_id": args.condition_id,
                "fault_domains": list(selected_domains),
                "unaffected_domains": list(unaffected_domains),
                "fault_after_s": args.fault_after_s,
                "no_fault_observation_s": args.no_fault_observation_s,
                "synthetic_rejection_domain": synthetic_domain,
                "synthetic_rejection_count": args.synthetic_rejection_count,
                "timing_candidate_policy": args.timing_candidate_policy,
                "maximum_detection_ms": args.maximum_detection_ms,
                "maximum_control_plane_recovery_ms": (
                    args.maximum_control_plane_recovery_ms
                ),
                "maximum_total_reconciliation_ms": (
                    args.maximum_total_reconciliation_ms
                ),
                "assurance_policy": controller_snapshot.get("policy"),
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
                "expected_fault_domains": list(selected_domains),
                "observed_drift_kinds": sorted(observed_drifts),
                "observed_drift_domains": sorted(drift_domains),
                "remediated_domains": sorted(remediated_domains),
                "remediation_request_sequence": remediation_request_sequence,
                "preservation_observation": preservation_observation,
                "candidate_checks": candidate_checks,
                "candidate_found": candidate_found,
            },
            "service_continuity": continuity,
            "cleanup": cleanup,
            "cleanup_ok": cleanup_ok,
            "operational_checks": operational_checks,
            "passed": operational_ok,
        }
        dump_json(output_dir / "summary.json", summary)
        summary_written = True

        print(f"PHASE18_SELECTIVE_CONDITION_ID={args.condition_id}")
        print(
            "PHASE18_SELECTIVE_EXPECTED_FAULT_DOMAINS="
            + ",".join(selected_domains)
        )
        print(
            "PHASE18_SELECTIVE_OBSERVED_DRIFT_DOMAINS="
            + ",".join(sorted(drift_domains))
        )
        print(
            "PHASE18_SELECTIVE_REMEDIATED_DOMAINS="
            + ",".join(sorted(remediated_domains))
        )
        print(
            "PHASE18_SELECTIVE_REMEDIATION_REQUEST_SEQUENCE="
            + ";".join(
                ",".join(request) for request in remediation_request_sequence
            )
        )
        print(
            "PHASE18_SELECTIVE_DOMAIN_ATTEMPT_COUNTS="
            + ",".join(
                f"{domain}:{domain_attempt_counts.get(domain, 0)}"
                for domain in DOMAIN_ORDER
            )
        )
        print(
            "PHASE18_SELECTIVE_SYNTHETIC_REJECTION_COUNTS="
            + ",".join(
                f"{domain}:{synthetic_counts.get(domain, 0)}"
                for domain in DOMAIN_ORDER
            )
        )
        print(
            "PHASE18_SELECTIVE_REMEDIATION_ATTEMPTS="
            f"{controller_snapshot.get('remediation_attempt_count')}"
        )
        print(
            "PHASE18_SELECTIVE_BACKOFF_EVENT_COUNT="
            f"{len(backoff_events)}"
        )
        print(
            "PHASE18_SELECTIVE_PRESERVATION_OBSERVED="
            f"{preservation_observation is not None}"
        )
        print(f"PHASE18_SELECTIVE_CANDIDATE_FOUND={candidate_found}")
        if not operational_ok:
            return 1
        print("PHASE18_SELECTIVE_ASSURANCE_RUN_OK")
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
                cleanup["C"] = phase17.p4_write_state(
                    args,
                    context,
                    present=False,
                    election_low=args.cleanup_election_low,
                )
            except Exception as exc:
                cleanup_errors.append(f"C:{type(exc).__name__}:{exc}")
            try:
                cleanup["linux_link"] = phase17.cleanup_linux_link(
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
    """Add the certified Phase 17 domain and desired-state arguments."""

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
    parser.add_argument("--injector-election-low", type=int, default=18110)
    parser.add_argument("--fault-domains", default="A,B,C")


def build_parser() -> argparse.ArgumentParser:
    """Build the independent injector and selective orchestrator CLI."""

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    injector = subparsers.add_parser("inject-fault")
    injector.add_argument("--output", required=True)
    injector.add_argument("--start-barrier", required=True)
    injector.add_argument("--barrier-timeout-s", type=float, default=10.0)
    injector.add_argument("--delay-s", type=float, default=4.0)
    add_common_arguments(injector)

    orchestrator = subparsers.add_parser("orchestrate")
    orchestrator.add_argument("--output-dir", required=True)
    orchestrator.add_argument("--condition-id", required=True)
    orchestrator.add_argument(
        "--assurance-profile-id",
        default="phase18-s2-multidomain-selective-assurance-foundation-v1",
    )
    add_common_arguments(orchestrator)
    orchestrator.add_argument("--fault-after-s", type=float, default=4.0)
    orchestrator.add_argument(
        "--synthetic-rejection-domain",
        default="",
    )
    orchestrator.add_argument(
        "--synthetic-rejection-count",
        type=int,
        default=0,
    )
    orchestrator.add_argument(
        "--timing-candidate-policy",
        choices=("enforce", "observe-only"),
        default="observe-only",
    )
    orchestrator.add_argument("--no-fault-observation-s", type=float, default=3.0)
    orchestrator.add_argument("--injector-barrier-timeout-s", type=float, default=10.0)
    orchestrator.add_argument("--injector-completion-timeout-s", type=float, default=12.0)
    orchestrator.add_argument("--post-convergence-observation-s", type=float, default=0.4)
    orchestrator.add_argument("--maximum-detection-ms", type=float, default=4_000.0)
    orchestrator.add_argument(
        "--maximum-control-plane-recovery-ms",
        type=float,
        default=3_750.0,
    )
    orchestrator.add_argument(
        "--maximum-total-reconciliation-ms",
        type=float,
        default=6_250.0,
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
    orchestrator.add_argument("--assurance-recovery-timeout-s", type=float, default=18.0)
    orchestrator.add_argument("--assurance-stop-timeout-s", type=float, default=3.0)
    orchestrator.add_argument("--assurance-maximum-runtime-s", type=float, default=45.0)
    orchestrator.add_argument("--initial-cleanup-election-low", type=int, default=18180)
    orchestrator.add_argument("--initial-program-election-low", type=int, default=18190)
    orchestrator.add_argument("--observer-election-low", type=int, default=18100)
    orchestrator.add_argument("--remediation-election-low", type=int, default=18120)
    orchestrator.add_argument("--cleanup-election-low", type=int, default=18990)
    return parser


def main() -> int:
    """Dispatch the selective injector or orchestrator."""

    args = build_parser().parse_args()
    args.netconf_key = os.path.expanduser(args.netconf_key)
    if args.command == "inject-fault":
        return inject_fault(args)
    return orchestrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
