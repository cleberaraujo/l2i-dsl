#!/usr/bin/env bash
# Validate P4Runtime multicast-state recovery across a mirrored timing matrix.
#
# The matrix varies both fault injection time and fault hold duration. Its
# mirrored order reduces the risk that host drift or thermal state is confused
# with a recovery property. Every row uses the same paced multicast and unicast
# continuity-control traffic profiles established by the earlier phases.

set -u
set -o pipefail

OUTPUT_DIR="${1:-${PHASE15_VALIDATION_OUTPUT_DIR:-}}"
if [[ -z "$OUTPUT_DIR" ]]; then
  echo "PHASE15_VALIDATION_OUTPUT_DIR_REQUIRED" >&2
  exit 2
fi

RUN_TIMEOUT_S="${PHASE15_VALIDATION_RUN_TIMEOUT_S:-180}"
MAX_ABSENCE_MS="${PHASE15_VALIDATION_MAX_ABSENCE_MS:-50}"
MAX_CONTROL_PLANE_RECOVERY_MS="${PHASE15_VALIDATION_MAX_CONTROL_PLANE_RECOVERY_MS:-50}"
MAX_FIRST_PACKET_MS="${PHASE15_VALIDATION_MAX_FIRST_PACKET_MS:-50}"
MAX_REMEDIATION_TO_RECEIVE_MS="${PHASE15_VALIDATION_MAX_REMEDIATION_TO_RECEIVE_MS:-75}"
MAX_OUTAGE_EXCESS_MS="${PHASE15_VALIDATION_MAX_OUTAGE_EXCESS_MS:-100}"

mkdir -p "$OUTPUT_DIR/runs"

cat > "$OUTPUT_DIR/schedule.tsv" <<'EOF'
order_index	condition_id	condition_repetition	fault_after_s	fault_hold_s	duration_s
1	A	1	3.0	0.5	12.0
2	B	1	4.5	1.0	14.0
3	C	1	5.0	2.0	15.0
4	D	1	4.0	3.0	15.0
5	D	2	4.0	3.0	15.0
6	C	2	5.0	2.0	15.0
7	B	2	4.5	1.0	14.0
8	A	2	3.0	0.5	12.0
EOF

printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  order_index \
  condition_id \
  condition_repetition \
  fault_after_s \
  fault_hold_s \
  duration_s \
  output_dir \
  exit_status \
  > "$OUTPUT_DIR/manifest.tsv"

{
  echo "recovery_profile_id=${PHASE15_VALIDATION_PROFILE_ID:-phase15-s2-p4-state-recovery-v1}"
  echo "validation_type=mirrored_fault_timing_and_hold_matrix"
  echo "expected_run_count=8"
  echo "maximum_absence_detection_ms=$MAX_ABSENCE_MS"
  echo "maximum_control_plane_recovery_ms=$MAX_CONTROL_PLANE_RECOVERY_MS"
  echo "maximum_first_packet_recovery_ms=$MAX_FIRST_PACKET_MS"
  echo "maximum_remediation_to_receive_ms=$MAX_REMEDIATION_TO_RECEIVE_MS"
  echo "maximum_outage_excess_ms=$MAX_OUTAGE_EXCESS_MS"
  echo "multicast_rate_mbps=${PHASE15_VALIDATION_MULTICAST_RATE_MBPS:-2}"
  echo "control_rate_mbps=${PHASE15_VALIDATION_CONTROL_RATE_MBPS:-0.5}"
  echo "packet_size=${PHASE15_VALIDATION_PACKET_SIZE:-1200}"
  echo "minimum_post_s=${PHASE15_VALIDATION_MINIMUM_POST_S:-5}"
  echo "window_guard_s=${PHASE15_VALIDATION_WINDOW_GUARD_S:-0.25}"
} | tee "$OUTPUT_DIR/preflight.txt"

if ! pgrep -f '[s]imple_switch_grpc' >/dev/null; then
  echo "PHASE15_VALIDATION_BMV2_REQUIRED" >&2
  exit 1
fi

if ! ss -H -ltn | grep -Eq ':9559\b'; then
  echo "PHASE15_VALIDATION_P4RUNTIME_REQUIRED" >&2
  exit 1
fi

cpu_count="$(${PYTHON_BIN:-python3} - <<'PY'
import os
print(len(os.sched_getaffinity(0)))
PY
)"
if (( cpu_count < 2 )); then
  echo "PHASE15_VALIDATION_DISTINCT_SENDER_CPUS_UNAVAILABLE=$cpu_count" >&2
  exit 1
fi

operational_failures=0

while IFS=$'\t' read -r \
  order_index \
  condition_id \
  condition_repetition \
  fault_after_s \
  fault_hold_s \
  duration_s
do
  [[ "$order_index" == "order_index" ]] && continue

  run_name="order${order_index}-${condition_id}-rep${condition_repetition}"
  run_dir="$OUTPUT_DIR/runs/$run_name"
  run_log="$OUTPUT_DIR/runs/${run_name}.log"
  mkdir -p "$run_dir"

  echo "PHASE15_VALIDATION_RUN_BEGIN=$run_name" \
    | tee -a "$OUTPUT_DIR/runner.txt"

  timeout "$RUN_TIMEOUT_S" \
    env \
      S2_RECOVERY_OUTPUT_DIR="$run_dir" \
      S2_RECOVERY_PROFILE_ID="${PHASE15_VALIDATION_PROFILE_ID:-phase15-s2-p4-state-recovery-v1}" \
      S2_RECOVERY_DURATION="$duration_s" \
      S2_RECOVERY_FAULT_AFTER_S="$fault_after_s" \
      S2_RECOVERY_FAULT_HOLD_S="$fault_hold_s" \
      S2_RECOVERY_MINIMUM_POST_S="${PHASE15_VALIDATION_MINIMUM_POST_S:-5}" \
      S2_RECOVERY_WINDOW_GUARD_S="${PHASE15_VALIDATION_WINDOW_GUARD_S:-0.25}" \
      S2_RECOVERY_MULTICAST_RATE_MBPS="${PHASE15_VALIDATION_MULTICAST_RATE_MBPS:-2}" \
      S2_RECOVERY_CONTROL_RATE_MBPS="${PHASE15_VALIDATION_CONTROL_RATE_MBPS:-0.5}" \
      S2_RECOVERY_PACKET_SIZE="${PHASE15_VALIDATION_PACKET_SIZE:-1200}" \
      S2_RECOVERY_SPIN_THRESHOLD_US="${PHASE15_VALIDATION_SPIN_THRESHOLD_US:-900}" \
      S2_RECOVERY_MAX_ABS_RATE_ERROR_PCT="${PHASE15_VALIDATION_MAX_ABS_RATE_ERROR_PCT:-5}" \
      S2_RECOVERY_MIN_INTER_SEND_RATIO="${PHASE15_VALIDATION_MIN_INTER_SEND_RATIO:-0.98}" \
      S2_RECOVERY_MINIMUM_STABLE_DELIVERY="${PHASE15_VALIDATION_MINIMUM_STABLE_DELIVERY:-0.99}" \
      S2_RECOVERY_MINIMUM_CONTROL_DELIVERY="${PHASE15_VALIDATION_MINIMUM_CONTROL_DELIVERY:-0.99}" \
      S2_RECOVERY_MAXIMUM_FAULT_DELIVERY="${PHASE15_VALIDATION_MAXIMUM_FAULT_DELIVERY:-0.05}" \
      S2_RECOVERY_MAXIMUM_FIRST_PACKET_MS="$MAX_FIRST_PACKET_MS" \
    ./setup_all.sh run_s2_p4_state_recovery \
    > "$run_log" 2>&1

  status=$?

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$order_index" \
    "$condition_id" \
    "$condition_repetition" \
    "$fault_after_s" \
    "$fault_hold_s" \
    "$duration_s" \
    "$run_dir" \
    "$status" \
    >> "$OUTPUT_DIR/manifest.tsv"

  echo "PHASE15_VALIDATION_RUN_END=${run_name}:status${status}" \
    | tee -a "$OUTPUT_DIR/runner.txt"

  if (( status != 0 )); then
    operational_failures=$((operational_failures + 1))
    tail -n 180 "$run_log" >&2 || true
  fi
done < "$OUTPUT_DIR/schedule.tsv"

echo "PHASE15_VALIDATION_OPERATIONAL_FAILURE_COUNT=$operational_failures" \
  | tee -a "$OUTPUT_DIR/runner.txt"

export PHASE15_VALIDATION_OUTPUT_DIR="$OUTPUT_DIR"
export PHASE15_VALIDATION_MAX_ABSENCE_MS="$MAX_ABSENCE_MS"
export PHASE15_VALIDATION_MAX_CONTROL_PLANE_RECOVERY_MS="$MAX_CONTROL_PLANE_RECOVERY_MS"
export PHASE15_VALIDATION_MAX_FIRST_PACKET_MS="$MAX_FIRST_PACKET_MS"
export PHASE15_VALIDATION_MAX_REMEDIATION_TO_RECEIVE_MS="$MAX_REMEDIATION_TO_RECEIVE_MS"
export PHASE15_VALIDATION_MAX_OUTAGE_EXCESS_MS="$MAX_OUTAGE_EXCESS_MS"

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


root = Path(os.environ["PHASE15_VALIDATION_OUTPUT_DIR"])
max_absence_ms = float(os.environ["PHASE15_VALIDATION_MAX_ABSENCE_MS"])
max_control_plane_ms = float(
    os.environ["PHASE15_VALIDATION_MAX_CONTROL_PLANE_RECOVERY_MS"]
)
max_first_packet_ms = float(os.environ["PHASE15_VALIDATION_MAX_FIRST_PACKET_MS"])
max_remediation_receive_ms = float(
    os.environ["PHASE15_VALIDATION_MAX_REMEDIATION_TO_RECEIVE_MS"]
)
max_outage_excess_ms = float(os.environ["PHASE15_VALIDATION_MAX_OUTAGE_EXCESS_MS"])

manifest_path = root / "manifest.tsv"
with manifest_path.open("r", encoding="utf-8", newline="") as handle:
    rows = list(csv.DictReader(handle, delimiter="\t"))

expected_order = ("A", "B", "C", "D", "D", "C", "B", "A")
observed_order = tuple(row["condition_id"] for row in rows)
condition_counts = Counter(observed_order)

failed: list[str] = []
invalid_rows: list[str] = []
records: list[dict[str, Any]] = []

print(f"PHASE15_VALIDATION_OBSERVED_RUN_COUNT={len(rows)}")
print(f"PHASE15_VALIDATION_MIRRORED_ORDER_OK={observed_order == expected_order}")

if len(rows) != 8:
    failed.append("run_count")
if observed_order != expected_order:
    failed.append("order")
if condition_counts != Counter({"A": 2, "B": 2, "C": 2, "D": 2}):
    failed.append("condition_coverage")

for row in rows:
    status = int(row["exit_status"])
    run_dir = Path(row["output_dir"])
    summary_path = run_dir / "summary.json"
    identity = f"order{row['order_index']}:{row['condition_id']}"

    if status != 0 or not summary_path.is_file():
        invalid_rows.append(f"{identity}:status{status}")
        continue

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    config = summary.get("configuration") or {}
    p4 = summary.get("p4") or {}
    state_events = p4.get("state_events") or {}
    timing = state_events.get("timing_ms") or {}
    recovery = summary.get("recovery_metrics") or {}
    receivers = recovery.get("multicast_receivers") or {}
    control = recovery.get("unicast_control") or {}
    candidate_checks = recovery.get("candidate_checks") or {}
    scope = summary.get("scope") or {}
    operational_checks = summary.get("operational_checks") or {}

    schedule_ok = (
        float(config.get("fault_after_s") or 0.0) == float(row["fault_after_s"])
        and float(config.get("fault_hold_s") or 0.0) == float(row["fault_hold_s"])
        and float(config.get("duration_s") or 0.0) == float(row["duration_s"])
    )

    content_ok = (
        summary.get("scenario") == "S2_P4_multicast_state_recovery"
        and summary.get("recovery_profile_id")
        == "phase15-s2-p4-state-recovery-v1"
        and summary.get("fault_model")
        == "p4runtime_multicast_table_and_pre_state_deletion"
        and summary.get("passed") is True
        and operational_checks
        and all(value is True for value in operational_checks.values())
        and schedule_ok
        and p4.get("pid_continuity") is True
        and scope.get("multicast_state_loss_injected") is True
        and scope.get("p4runtime_absence_detection_exercised") is True
        and scope.get("desired_state_rematerialization_exercised") is True
        and scope.get("unicast_dataplane_continuity_control_exercised") is True
        and scope.get("bmv2_process_restart_exercised") is False
        and scope.get("pipeline_reload_exercised") is False
        and scope.get("autonomous_mad_detection_validated") is False
        and scope.get("autonomous_mad_recovery_validated") is False
        and scope.get("test_harness_triggered_recovery") is True
        and candidate_checks
        and all(bool(value) for value in candidate_checks.values())
    )

    receiver_records: dict[str, Any] = {}
    for label in ("B", "C"):
        receiver = receivers.get(label) or {}
        windows = receiver.get("windows") or {}
        first = receiver.get("first_recovered_packet") or {}
        gap = receiver.get("crossing_gap") or {}

        first_restoration_ms = float(
            first.get("from_restoration_to_receive_ms")
            if first.get("from_restoration_to_receive_ms") is not None
            else float("inf")
        )
        first_remediation_ms = float(
            first.get("from_remediation_start_to_receive_ms")
            if first.get("from_remediation_start_to_receive_ms") is not None
            else float("inf")
        )
        receive_gap_ms = float(
            gap.get("receive_gap_ms")
            if gap.get("receive_gap_ms") is not None
            else float("inf")
        )
        outage_excess_ms = receive_gap_ms - float(row["fault_hold_s"]) * 1000.0

        receiver_records[label] = {
            "pre_ratio": float((windows.get("pre_fault") or {}).get("delivery_ratio") or 0.0),
            "fault_ratio": float((windows.get("fault_absent") or {}).get("delivery_ratio") or 0.0),
            "post_ratio": float((windows.get("post_recovery") or {}).get("delivery_ratio") or 0.0),
            "first_from_restoration_ms": first_restoration_ms,
            "first_from_remediation_ms": first_remediation_ms,
            "receive_gap_ms": receive_gap_ms,
            "outage_excess_ms": outage_excess_ms,
            "sequence_gap": int(gap.get("sequence_gap") or 0),
        }

        content_ok = content_ok and (
            first.get("found") is True
            and gap.get("found") is True
            and 0.0 <= outage_excess_ms <= max_outage_excess_ms
        )

    control_windows = control.get("windows") or {}
    control_min_ratio = min(
        float((control_windows.get(name) or {}).get("delivery_ratio") or 0.0)
        for name in ("pre_fault", "fault_absent", "post_recovery")
    )

    if not content_ok:
        invalid_rows.append(f"{identity}:content")
        continue

    records.append(
        {
            "order_index": int(row["order_index"]),
            "condition_id": row["condition_id"],
            "condition_repetition": int(row["condition_repetition"]),
            "fault_after_s": float(row["fault_after_s"]),
            "fault_hold_s": float(row["fault_hold_s"]),
            "duration_s": float(row["duration_s"]),
            "absence_detection_ms": float(
                timing.get("absence_detection_from_fault_start") or 0.0
            ),
            "control_plane_recovery_ms": float(
                timing.get("control_plane_recovery_from_remediation_start") or 0.0
            ),
            "B": receiver_records["B"],
            "C": receiver_records["C"],
            "control_min_ratio": control_min_ratio,
            "candidate_found": bool(recovery.get("candidate_found")),
        }
    )

print(f"PHASE15_VALIDATION_INVALID_ROW_COUNT={len(invalid_rows)}")
print(f"PHASE15_VALIDATION_VALID_RUN_COUNT={len(records)}")
if invalid_rows:
    failed.append("invalid_rows")


def values(*path: str) -> list[float]:
    result: list[float] = []
    for record in records:
        item: Any = record
        for key in path:
            item = item[key]
        result.append(float(item))
    return result


absence = values("absence_detection_ms")
control_plane = values("control_plane_recovery_ms")
b_first = values("B", "first_from_restoration_ms")
c_first = values("C", "first_from_restoration_ms")
b_remediation = values("B", "first_from_remediation_ms")
c_remediation = values("C", "first_from_remediation_ms")
b_excess = values("B", "outage_excess_ms")
c_excess = values("C", "outage_excess_ms")
b_pre = values("B", "pre_ratio")
b_fault = values("B", "fault_ratio")
b_post = values("B", "post_ratio")
c_pre = values("C", "pre_ratio")
c_fault = values("C", "fault_ratio")
c_post = values("C", "post_ratio")
control_min = values("control_min_ratio")

candidate_checks = {
    "all_rows_valid": len(records) == 8 and not invalid_rows,
    "mirrored_schedule": observed_order == expected_order,
    "state_absence_bounded": bool(absence) and max(absence) <= max_absence_ms,
    "control_plane_recovery_bounded": (
        bool(control_plane) and max(control_plane) <= max_control_plane_ms
    ),
    "first_packet_bounded": (
        bool(b_first)
        and bool(c_first)
        and max(b_first) <= max_first_packet_ms
        and max(c_first) <= max_first_packet_ms
    ),
    "remediation_to_dataplane_bounded": (
        bool(b_remediation)
        and bool(c_remediation)
        and max(b_remediation) <= max_remediation_receive_ms
        and max(c_remediation) <= max_remediation_receive_ms
    ),
    "fault_effect_complete": (
        bool(b_fault)
        and bool(c_fault)
        and max(b_fault) <= 0.05
        and max(c_fault) <= 0.05
    ),
    "pre_and_post_delivery_stable": (
        bool(b_pre)
        and bool(c_pre)
        and bool(b_post)
        and bool(c_post)
        and min(b_pre) >= 0.99
        and min(c_pre) >= 0.99
        and min(b_post) >= 0.99
        and min(c_post) >= 0.99
    ),
    "unicast_control_stable": bool(control_min) and min(control_min) >= 0.99,
    "outage_tracks_hold_duration": (
        bool(b_excess)
        and bool(c_excess)
        and min(b_excess) >= 0.0
        and min(c_excess) >= 0.0
        and max(b_excess) <= max_outage_excess_ms
        and max(c_excess) <= max_outage_excess_ms
    ),
    "all_per_run_candidates": (
        len(records) == 8 and all(record["candidate_found"] for record in records)
    ),
}

candidate_found = all(candidate_checks.values())

for name, value in candidate_checks.items():
    print(f"PHASE15_VALIDATION_CHECK_{name.upper()}={value}")

print(f"PHASE15_VALIDATION_B_PRE_MIN_RATIO={min(b_pre) if b_pre else 0.0:.9f}")
print(f"PHASE15_VALIDATION_B_FAULT_MAX_RATIO={max(b_fault) if b_fault else 0.0:.9f}")
print(f"PHASE15_VALIDATION_B_POST_MIN_RATIO={min(b_post) if b_post else 0.0:.9f}")
print(f"PHASE15_VALIDATION_C_PRE_MIN_RATIO={min(c_pre) if c_pre else 0.0:.9f}")
print(f"PHASE15_VALIDATION_C_FAULT_MAX_RATIO={max(c_fault) if c_fault else 0.0:.9f}")
print(f"PHASE15_VALIDATION_C_POST_MIN_RATIO={min(c_post) if c_post else 0.0:.9f}")
print(f"PHASE15_VALIDATION_CONTROL_MIN_RATIO={min(control_min) if control_min else 0.0:.9f}")
print(f"PHASE15_VALIDATION_ABSENCE_DETECTION_MEDIAN_MS={statistics.median(absence) if absence else 0.0:.6f}")
print(f"PHASE15_VALIDATION_ABSENCE_DETECTION_MAX_MS={max(absence) if absence else 0.0:.6f}")
print(f"PHASE15_VALIDATION_CONTROL_PLANE_RECOVERY_MEDIAN_MS={statistics.median(control_plane) if control_plane else 0.0:.6f}")
print(f"PHASE15_VALIDATION_CONTROL_PLANE_RECOVERY_MAX_MS={max(control_plane) if control_plane else 0.0:.6f}")
print(f"PHASE15_VALIDATION_B_FIRST_PACKET_MAX_MS={max(b_first) if b_first else 0.0:.6f}")
print(f"PHASE15_VALIDATION_C_FIRST_PACKET_MAX_MS={max(c_first) if c_first else 0.0:.6f}")
print(f"PHASE15_VALIDATION_B_REMEDIATION_TO_RECEIVE_MAX_MS={max(b_remediation) if b_remediation else 0.0:.6f}")
print(f"PHASE15_VALIDATION_C_REMEDIATION_TO_RECEIVE_MAX_MS={max(c_remediation) if c_remediation else 0.0:.6f}")
print(f"PHASE15_VALIDATION_B_OUTAGE_EXCESS_MAX_MS={max(b_excess) if b_excess else 0.0:.6f}")
print(f"PHASE15_VALIDATION_C_OUTAGE_EXCESS_MAX_MS={max(c_excess) if c_excess else 0.0:.6f}")
print(f"PHASE15_VALIDATION_CANDIDATE_FOUND={candidate_found}")
print("PHASE15_VALIDATION_PROCESS_RESTART_EXERCISED=False")
print("PHASE15_VALIDATION_PIPELINE_RELOAD_EXERCISED=False")
print("PHASE15_VALIDATION_AUTONOMOUS_MAD_RECOVERY_VALIDATED=False")

summary = {
    "scenario": "phase15_state_recovery_timing_validation",
    "recovery_profile_id": "phase15-s2-p4-state-recovery-v1",
    "validation_design": {
        "run_count": 8,
        "mirrored_condition_order": list(expected_order),
        "conditions": {
            "A": {"fault_after_s": 3.0, "fault_hold_s": 0.5, "duration_s": 12.0},
            "B": {"fault_after_s": 4.5, "fault_hold_s": 1.0, "duration_s": 14.0},
            "C": {"fault_after_s": 5.0, "fault_hold_s": 2.0, "duration_s": 15.0},
            "D": {"fault_after_s": 4.0, "fault_hold_s": 3.0, "duration_s": 15.0},
        },
        "thresholds_ms": {
            "maximum_absence_detection": max_absence_ms,
            "maximum_control_plane_recovery": max_control_plane_ms,
            "maximum_first_packet_after_restoration": max_first_packet_ms,
            "maximum_remediation_to_receive": max_remediation_receive_ms,
            "maximum_outage_excess": max_outage_excess_ms,
        },
    },
    "records": records,
    "aggregates": {
        "absence_detection_median_ms": statistics.median(absence) if absence else 0.0,
        "absence_detection_max_ms": max(absence) if absence else 0.0,
        "control_plane_recovery_median_ms": statistics.median(control_plane) if control_plane else 0.0,
        "control_plane_recovery_max_ms": max(control_plane) if control_plane else 0.0,
        "b_first_packet_max_ms": max(b_first) if b_first else 0.0,
        "c_first_packet_max_ms": max(c_first) if c_first else 0.0,
        "b_remediation_to_receive_max_ms": max(b_remediation) if b_remediation else 0.0,
        "c_remediation_to_receive_max_ms": max(c_remediation) if c_remediation else 0.0,
        "b_outage_excess_max_ms": max(b_excess) if b_excess else 0.0,
        "c_outage_excess_max_ms": max(c_excess) if c_excess else 0.0,
        "b_pre_min_ratio": min(b_pre) if b_pre else 0.0,
        "b_fault_max_ratio": max(b_fault) if b_fault else 0.0,
        "b_post_min_ratio": min(b_post) if b_post else 0.0,
        "c_pre_min_ratio": min(c_pre) if c_pre else 0.0,
        "c_fault_max_ratio": max(c_fault) if c_fault else 0.0,
        "c_post_min_ratio": min(c_post) if c_post else 0.0,
        "control_min_ratio": min(control_min) if control_min else 0.0,
    },
    "candidate_checks": candidate_checks,
    "candidate_found": candidate_found,
    "operationally_valid": len(records) == 8 and not invalid_rows,
    "scope": {
        "p4runtime_multicast_state_deletion_exercised": True,
        "desired_state_rematerialization_exercised": True,
        "unicast_dataplane_continuity_control_exercised": True,
        "fault_timing_variation_exercised": True,
        "fault_hold_duration_variation_exercised": True,
        "bmv2_process_restart_exercised": False,
        "pipeline_reload_exercised": False,
        "autonomous_mad_detection_validated": False,
        "autonomous_mad_recovery_validated": False,
        "test_harness_triggered_recovery": True,
    },
    "invalid_rows": invalid_rows,
}

(root / "validation-summary.json").write_text(
    json.dumps(summary, indent=2) + "\n",
    encoding="utf-8",
)

if failed:
    print("PHASE15_VALIDATION_ANALYSIS_FAILED=" + ",".join(failed))
    raise SystemExit(1)

print("PHASE15_VALIDATION_ANALYSIS_OK")
PY

analysis_status=${PIPESTATUS[0]}
echo "phase15_validation_analysis_exit_status=$analysis_status" \
  | tee -a "$OUTPUT_DIR/validation-analysis.txt"

if (( operational_failures != 0 || analysis_status != 0 )); then
  echo "PHASE15_VALIDATION_RUNNER_FAILED" | tee -a "$OUTPUT_DIR/runner.txt"
  exit 1
fi

echo "PHASE15_VALIDATION_RUNNER_OK" | tee -a "$OUTPUT_DIR/runner.txt"
