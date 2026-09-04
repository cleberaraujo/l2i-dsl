"""Deterministic temporal observations for the canonical S2 UDP event stream.

This module evaluates only ``PARTIAL_EVALUABLE_S2_RECOVERY_V1``.  It does not
claim bandwidth, priority, or full intent conformance.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any, Iterable, Mapping, Sequence

PREDICATE_ID = "PARTIAL_EVALUABLE_S2_RECOVERY_V1"
SCHEMA_ID = "l2i-s2-recovery-observation-v1"
EXCLUDED_REQUIREMENTS = [
    "bandwidth.min_mbps",
    "bandwidth.max_mbps",
    "priority",
    "full_intent_conformance",
]


class RecoveryObservationError(ValueError):
    """Raised when an observation cannot be safely reconstructed."""


def percentile_type7(values: Sequence[float], probability: float) -> float:
    """Return the Hyndman-Fan type 7 sample quantile."""
    if not values:
        raise RecoveryObservationError("percentile requires at least one value")
    if not 0.0 <= probability <= 1.0:
        raise RecoveryObservationError("probability must be in [0, 1]")
    ordered = sorted(float(value) for value in values)
    if not all(math.isfinite(value) for value in ordered):
        raise RecoveryObservationError("percentile values must be finite")
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise RecoveryObservationError(f"{label} must be an integer")
    try:
        converted = int(value)
    except (TypeError, ValueError) as exc:
        raise RecoveryObservationError(f"{label} must be an integer") from exc
    if converted != value:
        raise RecoveryObservationError(f"{label} must be an integer")
    return converted


def _probe_timestamp(packet: Mapping[str, Any]) -> int | None:
    for name in ("received_ns", "observed_ns"):
        if name in packet:
            return _integer(packet[name], f"probe.{name}")
    return None


def build_recovery_observation(
    raw_events: Iterable[Mapping[str, Any]],
    membership_events: Iterable[Mapping[str, Any]],
    *,
    receivers: Sequence[str],
    latency_max_ms: float,
    recovery_bin_ms: int,
    stable_k_bins: int,
    readbacks_valid: bool,
    probe_packets: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Recompute the authorized recovery observation from canonical events.

    Send opportunities are assigned to half-open windows by ``sent_ns``.
    Received packets inherit the opportunity window of their sequence.  Probe
    packets, which need not carry a sequence, are assigned by their own native
    receive timestamp.
    """
    bin_ms = _integer(recovery_bin_ms, "recovery_bin_ms")
    stable_k = _integer(stable_k_bins, "stable_k_bins")
    if bin_ms < 1 or stable_k < 1:
        raise RecoveryObservationError("bin width and stable K must be positive")
    bin_ns = bin_ms * 1_000_000
    latency_limit = float(latency_max_ms)
    if latency_limit < 0:
        raise RecoveryObservationError("latency_max_ms must be non-negative")

    receiver_set = set(receivers)
    if not receiver_set or len(receiver_set) != len(receivers):
        raise RecoveryObservationError("receivers must be non-empty and unique")

    sends: dict[int, int] = {}
    received: dict[tuple[int, str], list[tuple[int, int]]] = defaultdict(list)
    for row in raw_events:
        event = row.get("event")
        sequence = _integer(row.get("sequence"), "raw.sequence")
        if event == "sent":
            stamp = _integer(row.get("sent_ns"), "raw.sent_ns")
            if sequence in sends and sends[sequence] != stamp:
                raise RecoveryObservationError("conflicting duplicate send event")
            sends[sequence] = stamp
        elif event == "received":
            endpoint = str(row.get("endpoint"))
            if endpoint not in receiver_set:
                continue
            sent_ns = _integer(row.get("sent_ns"), "raw.sent_ns")
            received_ns = _integer(row.get("received_ns"), "raw.received_ns")
            if received_ns < sent_ns:
                raise RecoveryObservationError("negative one-way latency")
            received[(sequence, endpoint)].append((received_ns, sent_ns))

    for (sequence, _endpoint), observations in received.items():
        if sequence not in sends:
            raise RecoveryObservationError("receive event lacks send opportunity")
        if any(sent_ns != sends[sequence] for _, sent_ns in observations):
            raise RecoveryObservationError("receive/send timestamp mismatch")

    membership_by_receiver: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in membership_events:
        endpoint = str(event.get("endpoint"))
        if endpoint in receiver_set:
            membership_by_receiver[endpoint].append(event)
    for events in membership_by_receiver.values():
        events.sort(key=lambda item: _integer(item.get("observed_ns"), "membership.observed_ns"))

    probe_rows = list(probe_packets)
    probe_stamps = sorted(
        stamp for stamp in (_probe_timestamp(packet) for packet in probe_rows)
        if stamp is not None
    )
    probe_timestamps_complete = len(probe_stamps) == len(probe_rows)
    receiver_results: dict[str, Any] = {}
    for receiver in sorted(receiver_set):
        events = membership_by_receiver.get(receiver, [])
        rejoins = [
            _integer(event.get("observed_ns"), "membership_rejoin.observed_ns")
            for event in events if event.get("event") == "membership_rejoin"
        ]
        stop_stamps = [
            _integer(event.get("observed_ns"), "receiver_stop.observed_ns")
            for event in events if event.get("event") == "receiver_stop"
        ]
        episodes: list[dict[str, Any]] = []
        for event_index, rejoin_ns in enumerate(rejoins):
            later_rejoin = rejoins[event_index + 1] if event_index + 1 < len(rejoins) else None
            later_stops = [stamp for stamp in stop_stamps if stamp >= rejoin_ns]
            observation_end_ns = min(later_stops) if later_stops else None
            if later_rejoin is not None:
                observation_end_ns = min(observation_end_ns, later_rejoin) if observation_end_ns else later_rejoin
            windows: list[dict[str, Any]] = []
            if observation_end_ns is not None:
                complete_count = max(0, (observation_end_ns - rejoin_ns) // bin_ns)
                for index in range(complete_count):
                    start_ns = rejoin_ns + index * bin_ns
                    end_ns = start_ns + bin_ns
                    opportunity_sequences = sorted(
                        sequence for sequence, stamp in sends.items()
                        if start_ns <= stamp < end_ns
                    )
                    latencies: list[float] = []
                    unique_received = 0
                    duplicate_count = 0
                    for sequence in opportunity_sequences:
                        observations = sorted(received.get((sequence, receiver), []))
                        if observations:
                            unique_received += 1
                            duplicate_count += len(observations) - 1
                            first_received_ns, sent_ns = observations[0]
                            latencies.append((first_received_ns - sent_ns) / 1_000_000.0)
                    sent_count = len(opportunity_sequences)
                    lost_count = sent_count - unique_received
                    probe_count = sum(start_ns <= stamp < end_ns for stamp in probe_stamps)
                    p99 = percentile_type7(latencies, 0.99) if latencies else None
                    if sent_count == 0:
                        status = "NOT_EVALUABLE"
                        reasons = ["no_send_opportunity"]
                    else:
                        reasons = []
                        if unique_received == 0:
                            reasons.append("no_unique_expected_receiver_reception")
                        if p99 is None or p99 > latency_limit:
                            reasons.append("latency_p99_exceeds_or_is_unavailable")
                        if probe_count:
                            reasons.append("negative_probe_delivery_observed")
                        if not probe_timestamps_complete:
                            reasons.append("negative_probe_timestamp_missing")
                        if not readbacks_valid:
                            reasons.append("required_global_readbacks_invalid")
                        status = "PASS" if not reasons else "FAIL"
                    windows.append({
                        "index": index,
                        "start_ns": start_ns,
                        "end_ns": end_ns,
                        "start_relative_ms": index * bin_ms,
                        "end_relative_ms": (index + 1) * bin_ms,
                        "status": status,
                        "reasons": reasons,
                        "sent_opportunities": sent_count,
                        "unique_received": unique_received,
                        "lost": lost_count,
                        "duplicates": duplicate_count,
                        "delivery_ratio": unique_received / sent_count if sent_count else None,
                        "loss_ratio": lost_count / sent_count if sent_count else None,
                        "latency_p99_one_way_ms": p99,
                        "probe_deliveries": probe_count,
                    })

            first_pass = next((window for window in windows if window["status"] == "PASS"), None)
            stable_window = None
            run_length = 0
            for window in windows:
                run_length = run_length + 1 if window["status"] == "PASS" else 0
                if run_length >= stable_k:
                    stable_window = window
                    break
            if len(windows) < stable_k:
                status = "NOT_EVALUABLE"
                reason = "insufficient_complete_post_rejoin_windows"
            elif stable_window is None:
                status = "FAIL"
                reason = "no_k_consecutive_pass_windows"
            else:
                status = "PASS"
                reason = "k_consecutive_pass_windows_observed"
            episodes.append({
                "membership_rejoin_observed_ns": rejoin_ns,
                "observation_end_ns": observation_end_ns,
                "status": status,
                "reason": reason,
                "time_to_first_observation_pass_ms": (
                    first_pass["end_relative_ms"] if first_pass is not None else None
                ),
                "time_to_stable_observation_ms": (
                    stable_window["end_relative_ms"] if stable_window is not None else None
                ),
                "windows": windows,
            })

        if not rejoins:
            receiver_status = "NOT_EVALUABLE"
            first_time = stable_time = None
            reason = "no_membership_rejoin_event"
        elif any(episode["status"] == "NOT_EVALUABLE" for episode in episodes):
            receiver_status = "NOT_EVALUABLE"
            first_time = stable_time = None
            reason = "at_least_one_episode_not_evaluable"
        elif any(episode["status"] != "PASS" for episode in episodes):
            receiver_status = "FAIL"
            first_time = stable_time = None
            reason = "at_least_one_episode_failed"
        else:
            receiver_status = "PASS"
            first_time = max(episode["time_to_first_observation_pass_ms"] for episode in episodes)
            stable_time = max(episode["time_to_stable_observation_ms"] for episode in episodes)
            reason = "all_rejoin_episodes_passed"
        receiver_results[receiver] = {
            "status": receiver_status,
            "reason": reason,
            "time_to_first_observation_pass_ms": first_time,
            "time_to_stable_observation_ms": stable_time,
            "episodes": episodes,
        }

    statuses = [result["status"] for result in receiver_results.values()]
    if any(status == "NOT_EVALUABLE" for status in statuses):
        aggregate_status = "NOT_EVALUABLE"
    elif any(status != "PASS" for status in statuses):
        aggregate_status = "FAIL"
    else:
        aggregate_status = "PASS"
    aggregate_first = (
        max(result["time_to_first_observation_pass_ms"] for result in receiver_results.values())
        if aggregate_status == "PASS" else None
    )
    aggregate_stable = (
        max(result["time_to_stable_observation_ms"] for result in receiver_results.values())
        if aggregate_status == "PASS" else None
    )
    return {
        "schema": SCHEMA_ID,
        "predicate": PREDICATE_ID,
        "predicate_scope": "temporal_udp_multicast_post_rejoin_observation",
        "observation_status": aggregate_status,
        "excluded_requirements": list(EXCLUDED_REQUIREMENTS),
        "probe_timestamp_evidence_complete": probe_timestamps_complete,
        "parameters": {
            "latency_max_ms": latency_limit,
            "recovery_bin_ms": bin_ms,
            "stable_k_bins": stable_k,
            "native_timestamp_unit": "ns",
            "derived_duration_unit": "ms",
            "quantile_method": "Hyndman-Fan type 7",
            "window_convention": "[start_ns,end_ns)",
        },
        "aggregate": {
            "status": aggregate_status,
            "time_to_first_observation_pass_ms": aggregate_first,
            "time_to_stable_observation_ms": aggregate_stable,
        },
        "receivers": receiver_results,
    }
