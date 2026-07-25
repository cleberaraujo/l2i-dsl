#!/usr/bin/env bash
set -euo pipefail

# Validate autonomous assurance with a mirrored matrix covering a no-fault
# control, selective PRE drift, selective table drift, and one bounded
# remediation rejection that must exercise retry and exponential backoff.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="${1:-}"
RUN_TIMEOUT_S="${PHASE16_VALIDATION_RUN_TIMEOUT_S:-180}"

if [[ -z "$OUTPUT_DIR" ]]; then
  echo "usage: $0 OUTPUT_DIR" >&2
  exit 2
fi

mkdir -p "$OUTPUT_DIR/runs"
OUTPUT_DIR="$(realpath "$OUTPUT_DIR")"
cd "$REPO_DIR"

cat > "$OUTPUT_DIR/schedule.tsv" <<'SCHEDULE'
order_index	condition_id	condition_repetition	fault_kind	forced_rejections	fault_after_s	duration_s
1	A	1	none	0	4.0	10.0
2	B	1	pre_only	0	3.5	12.0
3	C	1	table_only	0	4.5	12.0
4	D	1	both	1	4.0	13.0
5	D	2	both	1	4.0	13.0
6	C	2	table_only	0	4.5	12.0
7	B	2	pre_only	0	3.5	12.0
8	A	2	none	0	4.0	10.0
SCHEDULE

{
  echo "repository=$REPO_DIR"
  echo "output_dir=$OUTPUT_DIR"
  echo "expected_run_count=8"
  echo "mirrored_order=A,B,C,D,D,C,B,A"
  echo "poll_interval_s=${PHASE16_VALIDATION_POLL_INTERVAL_S:-0.02}"
  echo "drift_confirmations=${PHASE16_VALIDATION_DRIFT_CONFIRMATIONS:-3}"
  echo "convergence_confirmations=${PHASE16_VALIDATION_CONVERGENCE_CONFIRMATIONS:-2}"
  echo "maximum_remediation_attempts=${PHASE16_VALIDATION_MAX_REMEDIATION_ATTEMPTS:-3}"
  echo "head=$(git rev-parse HEAD)"
  echo "branch=$(git branch --show-current)"
  echo "allowed_cpus=$(python3 -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0)))))')"

  if pgrep -f '[s]imple_switch_grpc' >/dev/null; then
    echo "bmv2_running=1"
  else
    echo "bmv2_running=0"
  fi

  if ss -H -ltn | grep -Eq ':9559\b'; then
    echo "p4runtime_available=1"
  else
    echo "p4runtime_available=0"
  fi
} | tee "$OUTPUT_DIR/preflight.txt"

if ! grep -Fq 'bmv2_running=1' "$OUTPUT_DIR/preflight.txt"; then
  echo "PHASE16_VALIDATION_BMV2_REQUIRED" >&2
  exit 1
fi

if ! grep -Fq 'p4runtime_available=1' "$OUTPUT_DIR/preflight.txt"; then
  echo "PHASE16_VALIDATION_P4RUNTIME_REQUIRED" >&2
  exit 1
fi

cpu_count="$(python3 -c 'import os; print(len(os.sched_getaffinity(0)))')"
if (( cpu_count < 2 )); then
  echo "PHASE16_VALIDATION_DISTINCT_SENDER_CPUS_UNAVAILABLE=$cpu_count" >&2
  exit 1
fi

printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  order_index \
  condition_id \
  condition_repetition \
  fault_kind \
  forced_rejections \
  fault_after_s \
  output_dir \
  exit_status \
  > "$OUTPUT_DIR/manifest.tsv"

operational_failures=0

while IFS=$'\t' read -r \
  order_index \
  condition_id \
  condition_repetition \
  fault_kind \
  forced_rejections \
  fault_after_s \
  duration_s
do
  [[ "$order_index" == "order_index" ]] && continue

  run_name="order${order_index}-${condition_id}-rep${condition_repetition}"
  run_dir="$OUTPUT_DIR/runs/$run_name"
  run_log="$OUTPUT_DIR/runs/${run_name}.log"
  mkdir -p "$run_dir"

  echo "PHASE16_VALIDATION_RUN_BEGIN=$run_name" \
    | tee -a "$OUTPUT_DIR/runner.txt"

  set +e
  timeout "$RUN_TIMEOUT_S" \
    env \
      S2_ASSURANCE_OUTPUT_DIR="$run_dir" \
      S2_ASSURANCE_PROFILE_ID="phase16-s2-p4-autonomous-assurance-validation-v1" \
      S2_ASSURANCE_DURATION="$duration_s" \
      S2_ASSURANCE_FAULT_AFTER_S="$fault_after_s" \
      S2_ASSURANCE_FAULT_KIND="$fault_kind" \
      S2_ASSURANCE_FORCED_REMEDIATION_REJECTIONS="$forced_rejections" \
      S2_ASSURANCE_MINIMUM_POST_S="${PHASE16_VALIDATION_MINIMUM_POST_S:-5}" \
      S2_ASSURANCE_WINDOW_GUARD_S="${PHASE16_VALIDATION_WINDOW_GUARD_S:-0.25}" \
      S2_ASSURANCE_MULTICAST_RATE_MBPS="${PHASE16_VALIDATION_MULTICAST_RATE_MBPS:-2}" \
      S2_ASSURANCE_CONTROL_RATE_MBPS="${PHASE16_VALIDATION_CONTROL_RATE_MBPS:-0.5}" \
      S2_ASSURANCE_PACKET_SIZE="${PHASE16_VALIDATION_PACKET_SIZE:-1200}" \
      S2_ASSURANCE_SPIN_THRESHOLD_US="${PHASE16_VALIDATION_SPIN_THRESHOLD_US:-900}" \
      S2_ASSURANCE_POLL_INTERVAL_S="${PHASE16_VALIDATION_POLL_INTERVAL_S:-0.02}" \
      S2_ASSURANCE_DRIFT_CONFIRMATIONS="${PHASE16_VALIDATION_DRIFT_CONFIRMATIONS:-3}" \
      S2_ASSURANCE_CONVERGENCE_CONFIRMATIONS="${PHASE16_VALIDATION_CONVERGENCE_CONFIRMATIONS:-2}" \
      S2_ASSURANCE_MAX_REMEDIATION_ATTEMPTS="${PHASE16_VALIDATION_MAX_REMEDIATION_ATTEMPTS:-3}" \
      S2_ASSURANCE_INITIAL_BACKOFF_S="${PHASE16_VALIDATION_INITIAL_BACKOFF_S:-0.01}" \
      S2_ASSURANCE_BACKOFF_MULTIPLIER="${PHASE16_VALIDATION_BACKOFF_MULTIPLIER:-2}" \
      S2_ASSURANCE_MAX_BACKOFF_S="${PHASE16_VALIDATION_MAX_BACKOFF_S:-0.10}" \
      S2_ASSURANCE_MAX_DETECTION_MS="${PHASE16_VALIDATION_MAX_DETECTION_MS:-150}" \
      S2_ASSURANCE_MAX_CONTROL_PLANE_RECOVERY_MS="${PHASE16_VALIDATION_MAX_CONTROL_PLANE_RECOVERY_MS:-250}" \
      S2_ASSURANCE_MAX_TOTAL_RECONCILIATION_MS="${PHASE16_VALIDATION_MAX_TOTAL_RECONCILIATION_MS:-400}" \
      S2_ASSURANCE_MAX_FIRST_PACKET_MS="${PHASE16_VALIDATION_MAX_FIRST_PACKET_MS:-50}" \
      S2_ASSURANCE_MAX_ABS_RATE_ERROR_PCT="${PHASE16_VALIDATION_MAX_ABS_RATE_ERROR_PCT:-5}" \
      S2_ASSURANCE_MIN_INTER_SEND_RATIO="${PHASE16_VALIDATION_MIN_INTER_SEND_RATIO:-0.98}" \
      S2_ASSURANCE_MINIMUM_STABLE_DELIVERY="${PHASE16_VALIDATION_MINIMUM_STABLE_DELIVERY:-0.99}" \
      S2_ASSURANCE_MINIMUM_CONTROL_DELIVERY="${PHASE16_VALIDATION_MINIMUM_CONTROL_DELIVERY:-0.99}" \
      S2_ASSURANCE_MINIMUM_LOST_PACKETS="${PHASE16_VALIDATION_MINIMUM_LOST_PACKETS:-1}" \
      ./setup_all.sh run_s2_p4_autonomous_assurance \
      > "$run_log" 2>&1
  status=$?
  set -e

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$order_index" \
    "$condition_id" \
    "$condition_repetition" \
    "$fault_kind" \
    "$forced_rejections" \
    "$fault_after_s" \
    "$run_dir" \
    "$status" \
    >> "$OUTPUT_DIR/manifest.tsv"

  echo "PHASE16_VALIDATION_RUN_END=${run_name}:status${status}" \
    | tee -a "$OUTPUT_DIR/runner.txt"

  if (( status != 0 )); then
    operational_failures=$((operational_failures + 1))
    tail -n 240 "$run_log" >&2 || true
  fi
done < "$OUTPUT_DIR/schedule.tsv"

echo "PHASE16_VALIDATION_OPERATIONAL_FAILURE_COUNT=$operational_failures" \
  | tee -a "$OUTPUT_DIR/runner.txt"

export PHASE16_VALIDATION_OUTPUT_DIR="$OUTPUT_DIR"

PYTHONDONTWRITEBYTECODE=1 \
python3 - <<'PY' \
  | tee "$OUTPUT_DIR/validation-analysis.txt"
from __future__ import annotations

import csv
import json
import os
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


root = Path(os.environ["PHASE16_VALIDATION_OUTPUT_DIR"])
manifest_path = root / "manifest.tsv"

with manifest_path.open("r", encoding="utf-8", newline="") as handle:
    rows = list(csv.DictReader(handle, delimiter="\t"))

expected_order = ("A", "B", "C", "D", "D", "C", "B", "A")
observed_order = tuple(row["condition_id"] for row in rows)
counts = Counter(observed_order)

failed: list[str] = []
invalid_rows: list[str] = []
records: list[dict[str, Any]] = []

print(f"PHASE16_VALIDATION_EXPECTED_RUN_COUNT=8")
print(f"PHASE16_VALIDATION_OBSERVED_RUN_COUNT={len(rows)}")
print(
    "PHASE16_VALIDATION_MIRRORED_ORDER_OK="
    f"{observed_order == expected_order}"
)

if len(rows) != 8:
    failed.append("run_count")
if observed_order != expected_order:
    failed.append("mirrored_order")
if counts != Counter({"A": 2, "B": 2, "C": 2, "D": 2}):
    failed.append("condition_counts")

expected_by_condition = {
    "A": {
        "fault_kind": "none",
        "forced_rejections": 0,
        "expected_drift": set(),
        "expected_components": set(),
    },
    "B": {
        "fault_kind": "pre_only",
        "forced_rejections": 0,
        "expected_drift": {"missing_pre_multicast_group"},
        "expected_components": {"pre_multicast_group"},
    },
    "C": {
        "fault_kind": "table_only",
        "forced_rejections": 0,
        "expected_drift": {"missing_multicast_table_entry"},
        "expected_components": {"multicast_table_entry"},
    },
    "D": {
        "fault_kind": "both",
        "forced_rejections": 1,
        "expected_drift": {
            "missing_pre_multicast_group",
            "missing_multicast_table_entry",
        },
        "expected_components": {
            "pre_multicast_group",
            "multicast_table_entry",
        },
    },
}

for row in rows:
    run_name = (
        f"order{row['order_index']}-"
        f"{row['condition_id']}-"
        f"rep{row['condition_repetition']}"
    )
    status = int(row["exit_status"])
    summary_path = Path(row["output_dir"]) / "summary.json"

    if status != 0 or not summary_path.is_file():
        invalid_rows.append(f"{run_name}:status_or_summary")
        continue

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    condition = expected_by_condition[row["condition_id"]]
    config = summary.get("configuration") or {}
    scope = summary.get("scope") or {}
    injector = summary.get("fault_injector") or {}
    assurance = summary.get("assurance") or {}
    recovery = summary.get("recovery_metrics") or {}
    multicast = recovery.get("multicast_receivers") or {}
    control = recovery.get("unicast_control") or {}
    candidate_checks = recovery.get("candidate_checks") or {}
    events = assurance.get("events") or []

    event_types = [event.get("event_type") for event in events]
    fault_kind = condition["fault_kind"]
    fault_expected = fault_kind != "none"

    run_candidate_found = bool(recovery.get("candidate_found"))
    run_candidate_consistent = bool(candidate_checks) and (
        run_candidate_found
        == all(bool(value) for value in candidate_checks.values())
    )

    common_ok = bool(
        summary.get("scenario") == "S2_P4_multicast_autonomous_assurance"
        and summary.get("fault_model")
        == "independent_p4runtime_multicast_state_deletion"
        and summary.get("assurance_profile_id")
        == "phase16-s2-p4-autonomous-assurance-validation-v1"
        and summary.get("passed") is True
        and run_candidate_consistent
        and config.get("fault_kind") == fault_kind
        and int(config.get("forced_remediation_rejections") or 0)
        == condition["forced_rejections"]
        and injector.get("fault_kind") == fault_kind
        and injector.get("controller_notification_sent") is False
        and scope.get("persistent_mad_assurance_loop_exercised") is True
        and scope.get("fault_schedule_shared_with_controller") is False
        and scope.get("independent_fault_injector_process_exercised") is True
        and scope.get("bmv2_process_restart_exercised") is False
        and scope.get("pipeline_reload_exercised") is False
        and scope.get("multi_domain_assurance_validated") is False
        and (summary.get("p4") or {}).get("pid_continuity") is True
    )

    expected_drift = condition["expected_drift"]
    expected_components = condition["expected_components"]
    observed_drift = set(recovery.get("observed_drift_kinds") or [])
    remediated_components = set(
        recovery.get("remediated_components") or []
    )

    b_metrics = multicast.get("B") or {}
    c_metrics = multicast.get("C") or {}
    control_windows = control.get("windows") or {}

    if fault_expected:
        timing = recovery.get("timing_ms") or {}
        structural_ok = bool(
            injector.get("fault_injected") is True
            and "incident_count" in assurance
            and "successful_convergence_count" in assurance
            and "remediation_attempt_count" in assurance
            and set(recovery.get("expected_drift_kinds") or [])
            == expected_drift
            and set(recovery.get("expected_remediation_components") or [])
            == expected_components
            and all(
                timing.get(name) is not None
                for name in (
                    "fault_write_to_drift_confirmation",
                    "remediation_start_to_convergence",
                    "fault_write_to_convergence",
                )
            )
            and all(
                bool((multicast.get(label) or {}).get("windows"))
                and "crossing_gap" in (multicast.get(label) or {})
                and "first_recovered_packet" in (multicast.get(label) or {})
                for label in ("B", "C")
            )
            and all(
                name in control_windows
                for name in (
                    "pre_fault",
                    "autonomous_outage",
                    "post_recovery",
                )
            )
        )
        content_ok = common_ok and structural_ok
    else:
        structural_ok = bool(
            injector.get("fault_injected") is False
            and set(recovery.get("expected_drift_kinds") or []) == set()
            and set(recovery.get("expected_remediation_components") or [])
            == set()
            and all(
                "stable_full"
                in ((multicast.get(label) or {}).get("windows") or {})
                for label in ("B", "C")
            )
            and "stable_full" in control_windows
            and scope.get("no_fault_false_positive_control_exercised") is True
        )
        content_ok = common_ok and structural_ok

    if not content_ok:
        invalid_rows.append(f"{run_name}:content")
        continue

    records.append(
        {
            "run_name": run_name,
            "condition_id": row["condition_id"],
            "fault_kind": fault_kind,
            "forced_rejections": condition["forced_rejections"],
            "summary_path": str(summary_path),
            "summary": summary,
        }
    )

print(f"PHASE16_VALIDATION_INVALID_ROW_COUNT={len(invalid_rows)}")
print(f"PHASE16_VALIDATION_VALID_RUN_COUNT={len(records)}")

if invalid_rows:
    failed.append("invalid_rows")

no_fault_records = [item for item in records if item["condition_id"] == "A"]
fault_records = [item for item in records if item["condition_id"] != "A"]
pre_records = [item for item in records if item["condition_id"] == "B"]
table_records = [item for item in records if item["condition_id"] == "C"]
retry_records = [item for item in records if item["condition_id"] == "D"]


def recovery(item: dict[str, Any]) -> dict[str, Any]:
    return item["summary"].get("recovery_metrics") or {}


def assurance(item: dict[str, Any]) -> dict[str, Any]:
    return item["summary"].get("assurance") or {}


def receiver(item: dict[str, Any], label: str) -> dict[str, Any]:
    return (
        (recovery(item).get("multicast_receivers") or {}).get(label)
        or {}
    )


def timing_values(key: str) -> list[float]:
    values: list[float] = []
    for item in fault_records:
        value = (recovery(item).get("timing_ms") or {}).get(key)
        if value is not None:
            values.append(float(value))
    return values


def fault_ratios(window_name: str) -> list[float]:
    values: list[float] = []
    for item in fault_records:
        for label in ("B", "C"):
            windows = receiver(item, label).get("windows") or {}
            values.append(
                float(
                    (windows.get(window_name) or {}).get("delivery_ratio")
                    or 0.0
                )
            )
    return values


no_false_positive_count = sum(
    1
    for item in no_fault_records
    if int(assurance(item).get("incident_count") or 0) != 0
    or int(assurance(item).get("remediation_attempt_count") or 0) != 0
)
selective_exact_count = sum(
    1
    for item in fault_records
    if set(recovery(item).get("observed_drift_kinds") or [])
    == expected_by_condition[item["condition_id"]]["expected_drift"]
    and set(recovery(item).get("remediated_components") or [])
    == expected_by_condition[item["condition_id"]]["expected_components"]
)
forced_rejection_count = sum(
    int(recovery(item).get("forced_rejection_event_count") or 0)
    for item in records
)
backoff_event_count = sum(
    int(recovery(item).get("backoff_event_count") or 0)
    for item in records
)
autonomous_convergence_count = sum(
    int(assurance(item).get("successful_convergence_count") or 0)
    for item in fault_records
)
remediation_attempts = [
    int(assurance(item).get("remediation_attempt_count") or 0)
    for item in records
]
sequence_gaps = {
    label: [
        int((receiver(item, label).get("crossing_gap") or {}).get("sequence_gap") or 0)
        for item in fault_records
    ]
    for label in ("B", "C")
}
control_ratios: list[float] = []
for item in records:
    windows = (
        (recovery(item).get("unicast_control") or {}).get("windows")
        or {}
    )
    for payload in windows.values():
        control_ratios.append(float(payload.get("delivery_ratio") or 0.0))

pre_ratios = fault_ratios("pre_fault")
post_ratios = fault_ratios("post_recovery")
stable_ratios: list[float] = []
for item in no_fault_records:
    for label in ("B", "C"):
        stable_ratios.append(
            float(
                ((receiver(item, label).get("windows") or {}).get("stable_full") or {}).get(
                    "delivery_ratio"
                )
                or 0.0
            )
        )

detection = timing_values("fault_write_to_drift_confirmation")
control_plane = timing_values("remediation_start_to_convergence")
total = timing_values("fault_write_to_convergence")

candidate_checks = {
    "mirrored_order": observed_order == expected_order,
    "all_runs_valid": len(records) == 8 and not invalid_rows,
    "no_fault_controls_valid": len(no_fault_records) == 2,
    "no_false_positive_incidents": no_false_positive_count == 0,
    "pre_only_classification": len(pre_records) == 2,
    "table_only_classification": len(table_records) == 2,
    "combined_retry_condition": len(retry_records) == 2,
    "selective_classification_and_reapply": selective_exact_count == 6,
    "forced_rejection_exercised": forced_rejection_count == 2,
    "backoff_exercised": backoff_event_count >= 2,
    "autonomous_convergence": autonomous_convergence_count == 6,
    "remediation_attempt_bound": max(remediation_attempts, default=0) == 2,
    "bounded_detection": max(detection, default=1e9) <= 150.0,
    "bounded_control_plane_recovery": max(control_plane, default=1e9) <= 250.0,
    "bounded_total_reconciliation": max(total, default=1e9) <= 400.0,
    "fault_effect_visible": min(sequence_gaps["B"], default=0) >= 1
    and min(sequence_gaps["C"], default=0) >= 1,
    "pre_fault_delivery": min(pre_ratios, default=0.0) >= 0.99,
    "post_recovery_delivery": min(post_ratios, default=0.0) >= 0.99,
    "no_fault_stable_delivery": min(stable_ratios, default=0.0) >= 0.99,
    "unicast_control_stable": min(control_ratios, default=0.0) >= 0.99,
}

candidate_found = all(candidate_checks.values())
operationally_valid = not failed

print(f"PHASE16_VALIDATION_NO_FAULT_VALID_RUN_COUNT={len(no_fault_records)}")
print(f"PHASE16_VALIDATION_NO_FALSE_POSITIVE_INCIDENT_COUNT={no_false_positive_count}")
print(f"PHASE16_VALIDATION_PRE_ONLY_VALID_RUN_COUNT={len(pre_records)}")
print(f"PHASE16_VALIDATION_TABLE_ONLY_VALID_RUN_COUNT={len(table_records)}")
print(f"PHASE16_VALIDATION_RETRY_VALID_RUN_COUNT={len(retry_records)}")
print(f"PHASE16_VALIDATION_SELECTIVE_REMEDIATION_EXACT_COUNT={selective_exact_count}")
print(f"PHASE16_VALIDATION_FORCED_REJECTION_EVENT_COUNT={forced_rejection_count}")
print(f"PHASE16_VALIDATION_BACKOFF_EVENT_COUNT={backoff_event_count}")
print(f"PHASE16_VALIDATION_AUTONOMOUS_CONVERGENCE_COUNT={autonomous_convergence_count}")
print(f"PHASE16_VALIDATION_REMEDIATION_ATTEMPTS_MAX={max(remediation_attempts, default=0)}")
print(f"PHASE16_VALIDATION_B_SEQUENCE_GAP_MIN={min(sequence_gaps['B'], default=0)}")
print(f"PHASE16_VALIDATION_C_SEQUENCE_GAP_MIN={min(sequence_gaps['C'], default=0)}")
print(f"PHASE16_VALIDATION_NO_FAULT_DELIVERY_MIN={min(stable_ratios, default=0.0):.9f}")
print(f"PHASE16_VALIDATION_PRE_FAULT_DELIVERY_MIN={min(pre_ratios, default=0.0):.9f}")
print(f"PHASE16_VALIDATION_POST_RECOVERY_DELIVERY_MIN={min(post_ratios, default=0.0):.9f}")
print(f"PHASE16_VALIDATION_CONTROL_MIN_RATIO={min(control_ratios, default=0.0):.9f}")
print(f"PHASE16_VALIDATION_DETECTION_MEDIAN_MS={statistics.median(detection) if detection else 0.0:.6f}")
print(f"PHASE16_VALIDATION_DETECTION_MAX_MS={max(detection, default=0.0):.6f}")
print(f"PHASE16_VALIDATION_CONTROL_PLANE_RECOVERY_MEDIAN_MS={statistics.median(control_plane) if control_plane else 0.0:.6f}")
print(f"PHASE16_VALIDATION_CONTROL_PLANE_RECOVERY_MAX_MS={max(control_plane, default=0.0):.6f}")
print(f"PHASE16_VALIDATION_TOTAL_RECONCILIATION_MEDIAN_MS={statistics.median(total) if total else 0.0:.6f}")
print(f"PHASE16_VALIDATION_TOTAL_RECONCILIATION_MAX_MS={max(total, default=0.0):.6f}")
print(f"PHASE16_VALIDATION_CANDIDATE_FOUND={candidate_found}")
print("PHASE16_VALIDATION_FAULT_SCHEDULE_SHARED_WITH_CONTROLLER=False")
print("PHASE16_VALIDATION_PROCESS_RESTART_EXERCISED=False")
print("PHASE16_VALIDATION_PIPELINE_RELOAD_EXERCISED=False")
print("PHASE16_VALIDATION_MULTI_DOMAIN_ASSURANCE_VALIDATED=False")

summary = {
    "phase": 16,
    "scenario": "S2_P4_autonomous_assurance_validation",
    "operationally_valid": operationally_valid,
    "candidate_found": candidate_found,
    "candidate_checks": candidate_checks,
    "mirrored_order": list(observed_order),
    "records": [
        {
            key: value
            for key, value in item.items()
            if key != "summary"
        }
        for item in records
    ],
    "aggregates": {
        "no_false_positive_incident_count": no_false_positive_count,
        "selective_remediation_exact_count": selective_exact_count,
        "forced_rejection_event_count": forced_rejection_count,
        "backoff_event_count": backoff_event_count,
        "autonomous_convergence_count": autonomous_convergence_count,
        "remediation_attempts_max": max(remediation_attempts, default=0),
        "receiver_b_sequence_gap_min": min(sequence_gaps["B"], default=0),
        "receiver_c_sequence_gap_min": min(sequence_gaps["C"], default=0),
        "no_fault_delivery_min": min(stable_ratios, default=0.0),
        "pre_fault_delivery_min": min(pre_ratios, default=0.0),
        "post_recovery_delivery_min": min(post_ratios, default=0.0),
        "control_min_ratio": min(control_ratios, default=0.0),
        "detection_max_ms": max(detection, default=0.0),
        "control_plane_recovery_max_ms": max(control_plane, default=0.0),
        "total_reconciliation_max_ms": max(total, default=0.0),
    },
    "scope": {
        "no_fault_false_positive_control_exercised": True,
        "component_selective_drift_exercised": True,
        "bounded_retry_and_backoff_exercised": True,
        "independent_fault_injector_process_exercised": True,
        "fault_schedule_shared_with_controller": False,
        "bmv2_process_restart_exercised": False,
        "pipeline_reload_exercised": False,
        "multi_domain_assurance_validated": False,
    },
    "invalid_rows": invalid_rows,
    "failures": failed,
}

(root / "validation-summary.json").write_text(
    json.dumps(summary, indent=2) + "\n",
    encoding="utf-8",
)

if failed:
    print("PHASE16_VALIDATION_ANALYSIS_FAILED=" + ",".join(failed))
    raise SystemExit(1)

print("PHASE16_VALIDATION_ANALYSIS_OK")
PY

analysis_status=${PIPESTATUS[0]}
echo "phase16_validation_analysis_exit_status=$analysis_status" \
  | tee -a "$OUTPUT_DIR/validation-analysis.txt"

if (( operational_failures != 0 || analysis_status != 0 )); then
  echo "PHASE16_VALIDATION_RUNNER_FAILED" | tee -a "$OUTPUT_DIR/runner.txt"
  exit 1
fi

echo "PHASE16_VALIDATION_RUNNER_OK" | tee -a "$OUTPUT_DIR/runner.txt"
