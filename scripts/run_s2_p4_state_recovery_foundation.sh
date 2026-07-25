#!/usr/bin/env bash
set -euo pipefail

# Run repeated multicast-state deletion and rematerialization experiments while
# an independent unicast flow verifies BMv2 dataplane continuity.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="${1:-}"

if [[ -z "$OUTPUT_DIR" ]]; then
  echo "usage: $0 OUTPUT_DIR" >&2
  exit 2
fi

mkdir -p "$OUTPUT_DIR/runs"
OUTPUT_DIR="$(realpath "$OUTPUT_DIR")"

REPETITIONS="${PHASE15_FOUNDATION_REPETITIONS:-4}"
RUN_TIMEOUT_S="${PHASE15_FOUNDATION_RUN_TIMEOUT_S:-180}"

if ! [[ "$REPETITIONS" =~ ^[1-9][0-9]*$ ]]; then
  echo "PHASE15_FOUNDATION_INVALID_REPETITIONS=$REPETITIONS" >&2
  exit 2
fi

cd "$REPO_DIR"

{
  echo "repository=$REPO_DIR"
  echo "output_dir=$OUTPUT_DIR"
  echo "repetitions=$REPETITIONS"
  echo "duration_s=${PHASE15_FOUNDATION_DURATION_S:-14}"
  echo "fault_after_s=${PHASE15_FOUNDATION_FAULT_AFTER_S:-4}"
  echo "fault_hold_s=${PHASE15_FOUNDATION_FAULT_HOLD_S:-2}"
  echo "multicast_rate_mbps=${PHASE15_FOUNDATION_MULTICAST_RATE_MBPS:-2}"
  echo "control_rate_mbps=${PHASE15_FOUNDATION_CONTROL_RATE_MBPS:-0.5}"
  echo "packet_size=${PHASE15_FOUNDATION_PACKET_SIZE:-1200}"
  echo "spin_threshold_us=${PHASE15_FOUNDATION_SPIN_THRESHOLD_US:-900}"
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
  echo "PHASE15_FOUNDATION_BMV2_REQUIRED" >&2
  exit 1
fi

if ! grep -Fq 'p4runtime_available=1' "$OUTPUT_DIR/preflight.txt"; then
  echo "PHASE15_FOUNDATION_P4RUNTIME_REQUIRED" >&2
  exit 1
fi

cpu_count="$(python3 -c 'import os; print(len(os.sched_getaffinity(0)))')"
if (( cpu_count < 2 )); then
  echo "PHASE15_FOUNDATION_DISTINCT_SENDER_CPUS_UNAVAILABLE=$cpu_count" >&2
  exit 1
fi

printf '%s\t%s\t%s\n' \
  repetition \
  output_dir \
  exit_status \
  > "$OUTPUT_DIR/manifest.tsv"

operational_failures=0

for repetition in $(seq 1 "$REPETITIONS"); do
  run_dir="$OUTPUT_DIR/runs/rep${repetition}"
  run_log="$OUTPUT_DIR/runs/rep${repetition}.log"
  mkdir -p "$run_dir"

  echo "PHASE15_FOUNDATION_RUN_BEGIN=rep${repetition}" \
    | tee -a "$OUTPUT_DIR/runner.txt"

  set +e
  timeout "$RUN_TIMEOUT_S" \
    env \
      S2_RECOVERY_OUTPUT_DIR="$run_dir" \
      S2_RECOVERY_DURATION="${PHASE15_FOUNDATION_DURATION_S:-14}" \
      S2_RECOVERY_FAULT_AFTER_S="${PHASE15_FOUNDATION_FAULT_AFTER_S:-4}" \
      S2_RECOVERY_FAULT_HOLD_S="${PHASE15_FOUNDATION_FAULT_HOLD_S:-2}" \
      S2_RECOVERY_MINIMUM_POST_S="${PHASE15_FOUNDATION_MINIMUM_POST_S:-5}" \
      S2_RECOVERY_WINDOW_GUARD_S="${PHASE15_FOUNDATION_WINDOW_GUARD_S:-0.25}" \
      S2_RECOVERY_MULTICAST_RATE_MBPS="${PHASE15_FOUNDATION_MULTICAST_RATE_MBPS:-2}" \
      S2_RECOVERY_CONTROL_RATE_MBPS="${PHASE15_FOUNDATION_CONTROL_RATE_MBPS:-0.5}" \
      S2_RECOVERY_PACKET_SIZE="${PHASE15_FOUNDATION_PACKET_SIZE:-1200}" \
      S2_RECOVERY_SPIN_THRESHOLD_US="${PHASE15_FOUNDATION_SPIN_THRESHOLD_US:-900}" \
      S2_RECOVERY_MAX_ABS_RATE_ERROR_PCT="${PHASE15_FOUNDATION_MAX_ABS_RATE_ERROR_PCT:-5}" \
      S2_RECOVERY_MIN_INTER_SEND_RATIO="${PHASE15_FOUNDATION_MIN_INTER_SEND_RATIO:-0.98}" \
      S2_RECOVERY_MINIMUM_STABLE_DELIVERY="${PHASE15_FOUNDATION_MINIMUM_STABLE_DELIVERY:-0.99}" \
      S2_RECOVERY_MINIMUM_CONTROL_DELIVERY="${PHASE15_FOUNDATION_MINIMUM_CONTROL_DELIVERY:-0.99}" \
      S2_RECOVERY_MAXIMUM_FAULT_DELIVERY="${PHASE15_FOUNDATION_MAXIMUM_FAULT_DELIVERY:-0.05}" \
      S2_RECOVERY_MAXIMUM_FIRST_PACKET_MS="${PHASE15_FOUNDATION_MAXIMUM_FIRST_PACKET_MS:-250}" \
      ./setup_all.sh run_s2_p4_state_recovery \
      > "$run_log" 2>&1
  status=$?
  set -e

  printf '%s\t%s\t%s\n' \
    "$repetition" \
    "$run_dir" \
    "$status" \
    >> "$OUTPUT_DIR/manifest.tsv"

  echo "PHASE15_FOUNDATION_RUN_END=rep${repetition}:status${status}" \
    | tee -a "$OUTPUT_DIR/runner.txt"

  if (( status != 0 )); then
    operational_failures=$((operational_failures + 1))
    tail -n 180 "$run_log" >&2 || true
  fi
done

echo "PHASE15_FOUNDATION_OPERATIONAL_FAILURE_COUNT=$operational_failures" \
  | tee -a "$OUTPUT_DIR/runner.txt"

export PHASE15_FOUNDATION_OUTPUT_DIR="$OUTPUT_DIR"
export PHASE15_FOUNDATION_REPETITIONS="$REPETITIONS"

PYTHONDONTWRITEBYTECODE=1 \
python3 - <<'PY' \
  | tee "$OUTPUT_DIR/foundation-analysis.txt"
from __future__ import annotations

import csv
import json
import os
import statistics
from pathlib import Path
from typing import Any


root = Path(os.environ["PHASE15_FOUNDATION_OUTPUT_DIR"])
repetitions = int(os.environ["PHASE15_FOUNDATION_REPETITIONS"])
manifest_path = root / "manifest.tsv"

with manifest_path.open("r", encoding="utf-8", newline="") as handle:
    rows = list(csv.DictReader(handle, delimiter="\t"))

failed: list[str] = []
invalid_rows: list[str] = []
records: list[dict[str, Any]] = []

print(f"PHASE15_FOUNDATION_EXPECTED_RUN_COUNT={repetitions}")
print(f"PHASE15_FOUNDATION_OBSERVED_RUN_COUNT={len(rows)}")

if len(rows) != repetitions:
    failed.append("run_count")

for row in rows:
    repetition = row["repetition"]
    status = int(row["exit_status"])
    summary_path = Path(row["output_dir"]) / "summary.json"

    if status != 0 or not summary_path.is_file():
        invalid_rows.append(f"rep{repetition}:status{status}")
        continue

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    operational_checks = summary.get("operational_checks") or {}
    recovery = summary.get("recovery_metrics") or {}
    candidate_checks = recovery.get("candidate_checks") or {}
    receivers = recovery.get("multicast_receivers") or {}
    control = recovery.get("unicast_control") or {}
    p4 = summary.get("p4") or {}
    state_events = p4.get("state_events") or {}
    timing = state_events.get("timing_ms") or {}
    scope = summary.get("scope") or {}

    content_ok = (
        summary.get("scenario")
        == "S2_P4_multicast_state_recovery_foundation"
        and summary.get("fault_model")
        == "p4runtime_multicast_table_and_pre_state_deletion"
        and summary.get("passed") is True
        and operational_checks
        and all(value is True for value in operational_checks.values())
        and scope.get("multicast_state_loss_injected") is True
        and scope.get("p4runtime_absence_detection_exercised") is True
        and scope.get("desired_state_rematerialization_exercised") is True
        and scope.get("unicast_dataplane_continuity_control_exercised") is True
        and scope.get("bmv2_process_restart_exercised") is False
        and scope.get("pipeline_reload_exercised") is False
        and scope.get("autonomous_mad_detection_validated") is False
        and scope.get("autonomous_mad_recovery_validated") is False
        and scope.get("test_harness_triggered_recovery") is True
        and p4.get("pid_continuity") is True
        and (p4.get("cleanup") or {}).get("multicast_absent") is True
        and (p4.get("cleanup") or {}).get("unicast_absent") is True
        and candidate_checks
    )

    for label in ("B", "C"):
        receiver = receivers.get(label) or {}
        windows = receiver.get("windows") or {}
        first = receiver.get("first_recovered_packet") or {}
        content_ok = content_ok and all(
            name in windows
            for name in ("pre_fault", "fault_absent", "post_recovery")
        )
        content_ok = content_ok and first.get("found") is True

    control_windows = control.get("windows") or {}
    content_ok = content_ok and all(
        name in control_windows
        for name in ("pre_fault", "fault_absent", "post_recovery")
    )

    if not content_ok:
        invalid_rows.append(f"rep{repetition}:content")
        continue

    records.append(
        {
            "repetition": int(repetition),
            "candidate_found": bool(recovery.get("candidate_found")),
            "candidate_checks": candidate_checks,
            "absence_detection_ms": float(
                timing.get("absence_detection_from_fault_start") or 0.0
            ),
            "control_plane_recovery_ms": float(
                timing.get("control_plane_recovery_from_remediation_start") or 0.0
            ),
            "B": receivers["B"],
            "C": receivers["C"],
            "control": control,
        }
    )

print(f"PHASE15_FOUNDATION_INVALID_ROW_COUNT={len(invalid_rows)}")
print(f"PHASE15_FOUNDATION_VALID_RUN_COUNT={len(records)}")

if invalid_rows:
    failed.append("invalid_rows")


def values(path: tuple[str, ...]) -> list[float]:
    result: list[float] = []
    for record in records:
        item: Any = record
        for key in path:
            item = item[key]
        result.append(float(item))
    return result


if records:
    b_pre = values(("B", "windows", "pre_fault", "delivery_ratio"))
    b_fault = values(("B", "windows", "fault_absent", "delivery_ratio"))
    b_post = values(("B", "windows", "post_recovery", "delivery_ratio"))
    c_pre = values(("C", "windows", "pre_fault", "delivery_ratio"))
    c_fault = values(("C", "windows", "fault_absent", "delivery_ratio"))
    c_post = values(("C", "windows", "post_recovery", "delivery_ratio"))
    control_fault = values(("control", "windows", "fault_absent", "delivery_ratio"))
    b_first = values(("B", "first_recovered_packet", "from_restoration_to_receive_ms"))
    c_first = values(("C", "first_recovered_packet", "from_restoration_to_receive_ms"))
    absence_ms = [record["absence_detection_ms"] for record in records]
    recovery_ms = [record["control_plane_recovery_ms"] for record in records]
else:
    b_pre = b_fault = b_post = []
    c_pre = c_fault = c_post = []
    control_fault = b_first = c_first = []
    absence_ms = recovery_ms = []

candidate_consistent = all(
    record["candidate_found"]
    == all(bool(value) for value in record["candidate_checks"].values())
    for record in records
)
all_candidates = (
    len(records) == repetitions
    and all(record["candidate_found"] for record in records)
)

state_absence_count = sum(
    bool(record["candidate_checks"].get("state_absence_confirmed"))
    for record in records
)
state_restoration_count = sum(
    bool(record["candidate_checks"].get("state_restoration_confirmed"))
    for record in records
)
bmv2_continuity_count = sum(
    bool(record["candidate_checks"].get("bmv2_process_continuity"))
    for record in records
)

print(f"PHASE15_FOUNDATION_STATE_ABSENCE_CONFIRMED_COUNT={state_absence_count}")
print(f"PHASE15_FOUNDATION_STATE_RESTORATION_CONFIRMED_COUNT={state_restoration_count}")
print(f"PHASE15_FOUNDATION_BMV2_PID_CONTINUITY_COUNT={bmv2_continuity_count}")
print(f"PHASE15_FOUNDATION_B_PRE_MIN_RATIO={min(b_pre) if b_pre else 0.0:.9f}")
print(f"PHASE15_FOUNDATION_B_FAULT_MAX_RATIO={max(b_fault) if b_fault else 0.0:.9f}")
print(f"PHASE15_FOUNDATION_B_POST_MIN_RATIO={min(b_post) if b_post else 0.0:.9f}")
print(f"PHASE15_FOUNDATION_C_PRE_MIN_RATIO={min(c_pre) if c_pre else 0.0:.9f}")
print(f"PHASE15_FOUNDATION_C_FAULT_MAX_RATIO={max(c_fault) if c_fault else 0.0:.9f}")
print(f"PHASE15_FOUNDATION_C_POST_MIN_RATIO={min(c_post) if c_post else 0.0:.9f}")
print(
    "PHASE15_FOUNDATION_CONTROL_FAULT_MIN_RATIO="
    f"{min(control_fault) if control_fault else 0.0:.9f}"
)
print(
    "PHASE15_FOUNDATION_ABSENCE_DETECTION_MEDIAN_MS="
    f"{statistics.median(absence_ms) if absence_ms else 0.0:.6f}"
)
print(
    "PHASE15_FOUNDATION_ABSENCE_DETECTION_MAX_MS="
    f"{max(absence_ms) if absence_ms else 0.0:.6f}"
)
print(
    "PHASE15_FOUNDATION_CONTROL_PLANE_RECOVERY_MEDIAN_MS="
    f"{statistics.median(recovery_ms) if recovery_ms else 0.0:.6f}"
)
print(
    "PHASE15_FOUNDATION_CONTROL_PLANE_RECOVERY_MAX_MS="
    f"{max(recovery_ms) if recovery_ms else 0.0:.6f}"
)
print(
    "PHASE15_FOUNDATION_B_FIRST_PACKET_RECOVERY_MEDIAN_MS="
    f"{statistics.median(b_first) if b_first else 0.0:.6f}"
)
print(
    "PHASE15_FOUNDATION_B_FIRST_PACKET_RECOVERY_MAX_MS="
    f"{max(b_first) if b_first else 0.0:.6f}"
)
print(
    "PHASE15_FOUNDATION_C_FIRST_PACKET_RECOVERY_MEDIAN_MS="
    f"{statistics.median(c_first) if c_first else 0.0:.6f}"
)
print(
    "PHASE15_FOUNDATION_C_FIRST_PACKET_RECOVERY_MAX_MS="
    f"{max(c_first) if c_first else 0.0:.6f}"
)
print(f"PHASE15_FOUNDATION_CANDIDATE_CONSISTENT={candidate_consistent}")
print(f"PHASE15_FOUNDATION_CANDIDATE_FOUND={all_candidates}")
print("PHASE15_FOUNDATION_PROCESS_RESTART_EXERCISED=False")
print("PHASE15_FOUNDATION_AUTONOMOUS_MAD_RECOVERY_VALIDATED=False")

summary = {
    "scenario": "phase15_state_recovery_foundation_analysis",
    "repetitions": repetitions,
    "valid_runs": len(records),
    "invalid_rows": invalid_rows,
    "records": records,
    "candidate_found": all_candidates,
    "candidate_consistent": candidate_consistent,
    "scope": {
        "p4runtime_multicast_state_deletion_exercised": True,
        "unicast_dataplane_continuity_control_exercised": True,
        "bmv2_process_restart_exercised": False,
        "pipeline_reload_exercised": False,
        "autonomous_mad_recovery_validated": False,
        "test_harness_triggered_recovery": True,
    },
}
(root / "foundation-summary.json").write_text(
    json.dumps(summary, indent=2) + "\n",
    encoding="utf-8",
)

if not candidate_consistent:
    failed.append("candidate_consistency")

if failed:
    print("PHASE15_FOUNDATION_ANALYSIS_FAILED=" + ",".join(failed))
    raise SystemExit(1)

print("PHASE15_FOUNDATION_ANALYSIS_OK")
PY

analysis_status=${PIPESTATUS[0]}
echo "phase15_foundation_analysis_exit_status=$analysis_status" \
  | tee -a "$OUTPUT_DIR/foundation-analysis.txt"

if (( operational_failures != 0 || analysis_status != 0 )); then
  echo "PHASE15_FOUNDATION_RUNNER_FAILED" | tee -a "$OUTPUT_DIR/runner.txt"
  exit 1
fi

echo "PHASE15_FOUNDATION_RUNNER_OK" | tee -a "$OUTPUT_DIR/runner.txt"
