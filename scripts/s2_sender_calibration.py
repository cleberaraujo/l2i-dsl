#!/usr/bin/env python3
"""Calibrate user-space UDP pacing for the S2 multicast traffic generator.

This utility intentionally does not program BMv2 and does not create network
namespaces. Each probe sends UDP datagrams to a local loopback sink so that the
measurement includes a real ``sendto()`` system call while remaining isolated
from switch, bridge, receiver, and checksum behavior.

Two pacing implementations are compared:

``current_repeated_sleep``
    Reproduces the sender implementation used at the end of Phase 12. The
    process repeatedly sleeps for at most two milliseconds until the deadline.

``hybrid_sleep_spin``
    Uses one coarse sleep and then a short busy-wait near the deadline. The
    algorithm still forbids catch-up bursts: the next deadline is anchored to
    the actual previous send time, not to the original absolute schedule.

The ``probe`` command produces one JSON evidence file. The ``analyze`` command
aggregates a manifest of independent repetitions, applies explicit calibration
criteria, and identifies the best candidate without changing repository code.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import socket
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


# Linux exposes scheduler policies as integer constants. Keeping human-readable
# names in the evidence makes results easier to audit on another machine.
SCHEDULER_NAMES = {
    getattr(os, "SCHED_OTHER", 0): "SCHED_OTHER",
    getattr(os, "SCHED_FIFO", 1): "SCHED_FIFO",
    getattr(os, "SCHED_RR", 2): "SCHED_RR",
    getattr(os, "SCHED_BATCH", 3): "SCHED_BATCH",
    getattr(os, "SCHED_IDLE", 5): "SCHED_IDLE",
    getattr(os, "SCHED_DEADLINE", 6): "SCHED_DEADLINE",
}


@dataclass(frozen=True)
class ManifestRow:
    """Represent one probe execution listed in the runner manifest."""

    configuration: str
    pacing_mode: str
    scheduling_mode: str
    repetition: int
    output_path: Path
    exit_status: int


def percentile(values: list[float], fraction: float) -> float | None:
    """Return a linearly interpolated percentile for a numeric sample."""

    if not values:
        return None

    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(math.floor(index))
    upper = int(math.ceil(index))

    if lower == upper:
        return ordered[lower]

    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def describe(values: list[float]) -> dict[str, float | int | None]:
    """Build a compact statistical description for an evidence file."""

    return {
        "min": min(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
        "n": len(values),
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write deterministic, human-readable JSON evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def wait_current(deadline_ns: int, _spin_threshold_ns: int) -> None:
    """Reproduce the Phase 12 repeated-sleep pacing implementation."""

    while True:
        remaining_ns = deadline_ns - time.monotonic_ns()

        if remaining_ns <= 0:
            return

        # This intentionally mirrors the sender currently used by the S2 smoke
        # test. Multiple short sleeps may accumulate wake-up latency in a VM.
        time.sleep(min(remaining_ns / 1_000_000_000.0, 0.002))


def wait_hybrid(deadline_ns: int, spin_threshold_ns: int) -> None:
    """Use one coarse sleep followed by a short high-resolution busy wait."""

    while True:
        now_ns = time.monotonic_ns()
        remaining_ns = deadline_ns - now_ns

        if remaining_ns <= 0:
            return

        if remaining_ns > spin_threshold_ns:
            # Leave a small margin for the final busy-wait phase. A single
            # coarse sleep avoids accumulating several scheduler wake-up costs.
            coarse_sleep_ns = remaining_ns - spin_threshold_ns
            time.sleep(coarse_sleep_ns / 1_000_000_000.0)
            continue

        # The final sub-millisecond interval is intentionally busy-waited. This
        # improves deadline precision while bounding CPU use by the configured
        # threshold and packet rate.
        while time.monotonic_ns() < deadline_ns:
            pass
        return


def make_loopback_sockets(packet_size: int) -> tuple[socket.socket, socket.socket, tuple[str, int], bytes]:
    """Create a loopback UDP source and a large-buffer sink for one probe."""

    # Binding a sink prevents ICMP port-unreachable generation. The sink does
    # not need a reader because the receive buffer is sized for the complete
    # short calibration run.
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
    sink.bind(("127.0.0.1", 0))

    source = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    source.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)

    destination = sink.getsockname()
    payload = b"P" * packet_size
    return source, sink, destination, payload


def run_probe(args: argparse.Namespace) -> int:
    """Run one pacing trial and persist all timing evidence."""

    packet_size = int(args.packet_size)
    requested_rate_mbps = float(args.rate_mbps)
    requested_duration_s = float(args.duration)
    spin_threshold_ns = int(float(args.spin_threshold_us) * 1_000.0)

    if packet_size <= 0:
        raise SystemExit("packet size must be greater than zero")

    if requested_rate_mbps <= 0.0:
        raise SystemExit("rate must be greater than zero")

    if requested_duration_s <= 0.0:
        raise SystemExit("duration must be greater than zero")

    if spin_threshold_ns < 0:
        raise SystemExit("spin threshold must not be negative")

    packets_per_second = (
        requested_rate_mbps * 1_000_000.0
    ) / (
        8.0 * packet_size
    )

    total_packets = max(
        1,
        int(round(requested_duration_s * packets_per_second)),
    )

    interval_ns = max(
        1,
        int(round(1_000_000_000.0 / packets_per_second)),
    )

    wait_function: Callable[[int, int], None] = {
        "current_repeated_sleep": wait_current,
        "hybrid_sleep_spin": wait_hybrid,
    }[args.mode]

    source, sink, destination, payload = make_loopback_sockets(packet_size)

    send_timestamps_ns: list[int] = []
    scheduler_lateness_ms: list[float] = []
    deadline_misses = 0
    send_errors: list[str] = []

    wall_start_ns = time.monotonic_ns()
    cpu_start_ns = time.process_time_ns()
    next_deadline_ns = wall_start_ns

    for sequence in range(total_packets):
        wait_function(next_deadline_ns, spin_threshold_ns)

        send_ns = time.monotonic_ns()
        lateness_ns = max(0, send_ns - next_deadline_ns)

        if lateness_ns >= interval_ns:
            deadline_misses += 1

        scheduler_lateness_ms.append(lateness_ns / 1_000_000.0)

        try:
            source.sendto(payload, destination)
        except OSError as exc:
            send_errors.append(f"sequence={sequence}: {exc}")

        send_timestamps_ns.append(send_ns)

        # Preserve the no-catch-up property. Every packet is scheduled at least
        # one full nominal interval after the actual preceding send, so a VM
        # pause cannot produce a compensating burst.
        next_deadline_ns = send_ns + interval_ns

    cpu_end_ns = time.process_time_ns()
    wall_end_ns = time.monotonic_ns()

    source.close()
    sink.close()

    wall_elapsed_s = (wall_end_ns - wall_start_ns) / 1_000_000_000.0
    cpu_elapsed_s = (cpu_end_ns - cpu_start_ns) / 1_000_000_000.0

    inter_send_ms = [
        (current - previous) / 1_000_000.0
        for previous, current in zip(
            send_timestamps_ns,
            send_timestamps_ns[1:],
        )
    ]

    actual_rate_mbps = (
        total_packets * packet_size * 8.0
    ) / (
        wall_elapsed_s * 1_000_000.0
    )

    rate_error_pct = (
        (actual_rate_mbps - requested_rate_mbps)
        / requested_rate_mbps
        * 100.0
    )

    cpu_ratio = (
        cpu_elapsed_s / wall_elapsed_s
        if wall_elapsed_s > 0.0
        else 0.0
    )

    scheduler_policy = os.sched_getscheduler(0)

    result = {
        "run": {
            "configuration": args.configuration,
            "repetition": int(args.repetition),
            "pacing_mode": args.mode,
            "scheduling_mode": args.scheduling_mode,
        },
        "requested": {
            "rate_payload_mbps": requested_rate_mbps,
            "duration_s": requested_duration_s,
            "packet_size_bytes": packet_size,
            "spin_threshold_us": float(args.spin_threshold_us),
        },
        "observed": {
            "packets": total_packets,
            "wall_elapsed_s": wall_elapsed_s,
            "cpu_elapsed_s": cpu_elapsed_s,
            "cpu_ratio": cpu_ratio,
            "rate_payload_mbps": actual_rate_mbps,
            "rate_error_pct": rate_error_pct,
            "deadline_misses": deadline_misses,
            "send_errors": send_errors,
            "inter_send_ms": describe(inter_send_ms),
            "scheduler_lateness_ms": describe(scheduler_lateness_ms),
        },
        "runtime": {
            "pid": os.getpid(),
            "scheduler_policy": scheduler_policy,
            "scheduler_policy_name": SCHEDULER_NAMES.get(
                scheduler_policy,
                f"UNKNOWN_{scheduler_policy}",
            ),
            "cpu_affinity": sorted(os.sched_getaffinity(0)),
            "monotonic_clock_resolution_s": time.get_clock_info("monotonic").resolution,
        },
        "pacing": {
            "nominal_interval_ns": interval_ns,
            "nominal_interval_ms": interval_ns / 1_000_000.0,
            "no_catch_up": True,
        },
    }

    write_json(Path(args.output), result)

    print(f"PHASE13_PROBE_CONFIGURATION={args.configuration}")
    print(f"PHASE13_PROBE_REPETITION={args.repetition}")
    print(f"PHASE13_PROBE_MODE={args.mode}")
    print(f"PHASE13_PROBE_SCHEDULING={args.scheduling_mode}")
    print(f"PHASE13_PROBE_PACKETS={total_packets}")
    print(f"PHASE13_PROBE_ACTUAL_RATE_MBPS={actual_rate_mbps:.9f}")
    print(f"PHASE13_PROBE_RATE_ERROR_PCT={rate_error_pct:.6f}")
    print(f"PHASE13_PROBE_DEADLINE_MISSES={deadline_misses}")
    print(f"PHASE13_PROBE_CPU_RATIO={cpu_ratio:.6f}")
    print(f"PHASE13_PROBE_SCHEDULER_POLICY={result['runtime']['scheduler_policy_name']}")
    print(f"PHASE13_PROBE_OK={not send_errors}")

    return 0 if not send_errors else 1


def load_manifest(path: Path) -> list[ManifestRow]:
    """Load the tab-separated manifest produced by the shell runner."""

    rows: list[ManifestRow] = []

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")

        required_fields = {
            "configuration",
            "pacing_mode",
            "scheduling_mode",
            "repetition",
            "output_path",
            "exit_status",
        }

        if reader.fieldnames is None or not required_fields.issubset(reader.fieldnames):
            raise ValueError("calibration manifest is missing required columns")

        for item in reader:
            rows.append(
                ManifestRow(
                    configuration=item["configuration"],
                    pacing_mode=item["pacing_mode"],
                    scheduling_mode=item["scheduling_mode"],
                    repetition=int(item["repetition"]),
                    output_path=Path(item["output_path"]),
                    exit_status=int(item["exit_status"]),
                )
            )

    return rows


def coefficient_of_variation(values: list[float]) -> float:
    """Return population standard deviation divided by the sample mean."""

    if not values:
        return math.inf

    mean = statistics.fmean(values)

    if mean == 0.0:
        return math.inf

    if len(values) == 1:
        return 0.0

    return statistics.pstdev(values) / mean


def marker_token(value: str) -> str:
    """Convert a configuration label into a stable marker token."""

    return "".join(character if character.isalnum() else "_" for character in value).upper()


def run_analysis(args: argparse.Namespace) -> int:
    """Aggregate probe evidence and classify pacing candidates."""

    manifest_path = Path(args.manifest)
    rows = load_manifest(manifest_path)

    if not rows:
        raise SystemExit("calibration manifest is empty")

    grouped: dict[str, list[tuple[ManifestRow, dict[str, Any]]]] = {}
    invalid_rows: list[str] = []

    for row in rows:
        if row.exit_status != 0:
            invalid_rows.append(
                f"{row.configuration}: repetition {row.repetition} exited with {row.exit_status}"
            )
            continue

        if not row.output_path.is_file():
            invalid_rows.append(
                f"{row.configuration}: missing {row.output_path}"
            )
            continue

        data = json.loads(row.output_path.read_text(encoding="utf-8"))
        grouped.setdefault(row.configuration, []).append((row, data))

    configuration_summaries: dict[str, Any] = {}

    for configuration, records in sorted(grouped.items()):
        rate_values = [
            float(data["observed"]["rate_payload_mbps"])
            for _row, data in records
        ]

        error_values = [
            float(data["observed"]["rate_error_pct"])
            for _row, data in records
        ]

        cpu_values = [
            float(data["observed"]["cpu_ratio"])
            for _row, data in records
        ]

        min_interval_ratios = []
        max_lateness_values = []
        deadline_misses = []

        for _row, data in records:
            nominal_interval_ms = float(data["pacing"]["nominal_interval_ms"])
            minimum_inter_send_ms = float(data["observed"]["inter_send_ms"]["min"])
            min_interval_ratios.append(minimum_inter_send_ms / nominal_interval_ms)
            max_lateness_values.append(
                float(data["observed"]["scheduler_lateness_ms"]["max"])
            )
            deadline_misses.append(int(data["observed"]["deadline_misses"]))

        requested_rate = float(records[0][1]["requested"]["rate_payload_mbps"])
        repetitions_expected = int(args.expected_repetitions)
        repetitions_complete = len(records) == repetitions_expected
        maximum_absolute_error = max(abs(value) for value in error_values)
        rate_cv = coefficient_of_variation(rate_values)
        minimum_interval_ratio = min(min_interval_ratios)
        median_cpu_ratio = statistics.median(cpu_values)

        accuracy_ok = maximum_absolute_error <= float(args.max_abs_rate_error_pct)
        stability_ok = rate_cv <= float(args.max_rate_cv)
        no_microburst_ok = minimum_interval_ratio >= float(args.min_inter_send_ratio)
        cpu_ok = median_cpu_ratio <= float(args.max_cpu_ratio)
        probe_count_ok = repetitions_complete

        passed = (
            accuracy_ok
            and stability_ok
            and no_microburst_ok
            and cpu_ok
            and probe_count_ok
        )

        first_row = records[0][0]
        summary = {
            "configuration": configuration,
            "pacing_mode": first_row.pacing_mode,
            "scheduling_mode": first_row.scheduling_mode,
            "requested_rate_mbps": requested_rate,
            "repetitions_observed": len(records),
            "repetitions_expected": repetitions_expected,
            "median_actual_rate_mbps": statistics.median(rate_values),
            "minimum_actual_rate_mbps": min(rate_values),
            "maximum_actual_rate_mbps": max(rate_values),
            "median_rate_error_pct": statistics.median(error_values),
            "maximum_absolute_rate_error_pct": maximum_absolute_error,
            "rate_coefficient_of_variation": rate_cv,
            "minimum_inter_send_ratio": minimum_interval_ratio,
            "median_cpu_ratio": median_cpu_ratio,
            "maximum_scheduler_lateness_ms": max(max_lateness_values),
            "maximum_deadline_misses": max(deadline_misses),
            "gates": {
                "probe_count_ok": probe_count_ok,
                "accuracy_ok": accuracy_ok,
                "stability_ok": stability_ok,
                "no_microburst_ok": no_microburst_ok,
                "cpu_ok": cpu_ok,
                "passed": passed,
            },
        }

        configuration_summaries[configuration] = summary

        token = marker_token(configuration)
        print(f"PHASE13_CAL_{token}_REPETITIONS={len(records)}")
        print(
            f"PHASE13_CAL_{token}_MEDIAN_RATE_MBPS="
            f"{summary['median_actual_rate_mbps']:.9f}"
        )
        print(
            f"PHASE13_CAL_{token}_MAX_ABS_RATE_ERROR_PCT="
            f"{maximum_absolute_error:.6f}"
        )
        print(f"PHASE13_CAL_{token}_RATE_CV={rate_cv:.9f}")
        print(
            f"PHASE13_CAL_{token}_MIN_INTER_SEND_RATIO="
            f"{minimum_interval_ratio:.9f}"
        )
        print(f"PHASE13_CAL_{token}_MEDIAN_CPU_RATIO={median_cpu_ratio:.9f}")
        print(
            f"PHASE13_CAL_{token}_MAX_SCHEDULER_LATENESS_MS="
            f"{max(max_lateness_values):.6f}"
        )
        print(f"PHASE13_CAL_{token}_ACCURACY_OK={accuracy_ok}")
        print(f"PHASE13_CAL_{token}_STABILITY_OK={stability_ok}")
        print(f"PHASE13_CAL_{token}_NO_MICROBURST_OK={no_microburst_ok}")
        print(f"PHASE13_CAL_{token}_CPU_OK={cpu_ok}")
        print(f"PHASE13_CAL_{token}_PASSED={passed}")

    preference = [
        "hybrid-rt-affinity",
        "hybrid-affinity",
        "hybrid-normal",
        "current-rt-affinity",
        "current-affinity",
        "current-normal",
    ]

    selected_candidate = next(
        (
            configuration
            for configuration in preference
            if configuration in configuration_summaries
            and configuration_summaries[configuration]["gates"]["passed"]
        ),
        None,
    )

    result = {
        "manifest": str(manifest_path),
        "criteria": {
            "expected_repetitions": int(args.expected_repetitions),
            "max_abs_rate_error_pct": float(args.max_abs_rate_error_pct),
            "max_rate_cv": float(args.max_rate_cv),
            "min_inter_send_ratio": float(args.min_inter_send_ratio),
            "max_cpu_ratio": float(args.max_cpu_ratio),
        },
        "invalid_rows": invalid_rows,
        "configurations": configuration_summaries,
        "selected_candidate": selected_candidate,
        "candidate_found": selected_candidate is not None,
    }

    write_json(Path(args.output), result)

    print(f"PHASE13_CALIBRATION_INVALID_ROW_COUNT={len(invalid_rows)}")
    print(f"PHASE13_CALIBRATION_CANDIDATE={selected_candidate or 'NONE'}")
    print(f"PHASE13_CALIBRATION_CANDIDATE_FOUND={selected_candidate is not None}")
    print("PHASE13_CALIBRATION_ANALYSIS_OK")

    # A completed diagnostic analysis returns zero even when no candidate
    # passes. Candidate absence is an experimental result, not a tool failure.
    return 0


def add_probe_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach probe-specific arguments to a subparser."""

    parser.add_argument(
        "--mode",
        choices=(
            "current_repeated_sleep",
            "hybrid_sleep_spin",
        ),
        required=True,
    )
    parser.add_argument("--configuration", required=True)
    parser.add_argument("--scheduling-mode", required=True)
    parser.add_argument("--repetition", type=int, required=True)
    parser.add_argument("--rate-mbps", type=float, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--packet-size", type=int, required=True)
    parser.add_argument("--spin-threshold-us", type=float, default=300.0)
    parser.add_argument("--output", required=True)


def add_analysis_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach analysis criteria to a subparser."""

    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-repetitions", type=int, default=3)
    parser.add_argument("--max-abs-rate-error-pct", type=float, default=5.0)
    parser.add_argument("--max-rate-cv", type=float, default=0.05)
    parser.add_argument("--min-inter-send-ratio", type=float, default=0.98)
    parser.add_argument("--max-cpu-ratio", type=float, default=0.35)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line interface used by the shell runner."""

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe_parser = subparsers.add_parser("probe")
    add_probe_arguments(probe_parser)

    analysis_parser = subparsers.add_parser("analyze")
    add_analysis_arguments(analysis_parser)

    return parser


def main() -> int:
    """Dispatch the requested calibration operation."""

    args = build_parser().parse_args()

    if args.command == "probe":
        return run_probe(args)

    return run_analysis(args)


if __name__ == "__main__":
    raise SystemExit(main())
