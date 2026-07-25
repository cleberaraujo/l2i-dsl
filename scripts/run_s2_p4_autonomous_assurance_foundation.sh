#!/usr/bin/env bash
set -euo pipefail

# Repeat independent multicast-state faults while a persistent MAD assurance
# loop autonomously detects drift, performs bounded remediation, and confirms
# convergence without receiving the injector's schedule or completion signal.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="${1:-}"

if [[ -z "$OUTPUT_DIR" ]]; then
  echo "usage: $0 OUTPUT_DIR" >&2
  exit 2
fi

mkdir -p "$OUTPUT_DIR/runs"
OUTPUT_DIR="$(realpath "$OUTPUT_DIR")"

REPETITIONS="${PHASE16_FOUNDATION_REPETITIONS:-4}"
RUN_TIMEOUT_S="${PHASE16_FOUNDATION_RUN_TIMEOUT_S:-180}"

if ! [[ "$REPETITIONS" =~ ^[1-9][0-9]*$ ]]; then
  echo "PHASE16_FOUNDATION_INVALID_REPETITIONS=$REPETITIONS" >&2
  exit 2
fi

cd "$REPO_DIR"

{
  echo "repository=$REPO_DIR"
  echo "output_dir=$OUTPUT_DIR"
  echo "repetitions=$REPETITIONS"
  echo "duration_s=${PHASE16_FOUNDATION_DURATION_S:-12}"
  echo "fault_after_s=${PHASE16_FOUNDATION_FAULT_AFTER_S:-4}"
  echo "multicast_rate_mbps=${PHASE16_FOUNDATION_MULTICAST_RATE_MBPS:-2}"
  echo "control_rate_mbps=${PHASE16_FOUNDATION_CONTROL_RATE_MBPS:-0.5}"
  echo "assurance_poll_interval_s=${PHASE16_FOUNDATION_POLL_INTERVAL_S:-0.02}"
  echo "assurance_drift_confirmations=${PHASE16_FOUNDATION_DRIFT_CONFIRMATIONS:-3}"
  echo "assurance_convergence_confirmations=${PHASE16_FOUNDATION_CONVERGENCE_CONFIRMATIONS:-2}"
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
  echo "PHASE16_FOUNDATION_BMV2_REQUIRED" >&2
  exit 1
fi

if ! grep -Fq 'p4runtime_available=1' "$OUTPUT_DIR/preflight.txt"; then
  echo "PHASE16_FOUNDATION_P4RUNTIME_REQUIRED" >&2
  exit 1
fi

cpu_count="$(python3 -c 'import os; print(len(os.sched_getaffinity(0)))')"
if (( cpu_count < 2 )); then
  echo "PHASE16_FOUNDATION_DISTINCT_SENDER_CPUS_UNAVAILABLE=$cpu_count" >&2
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

  echo "PHASE16_FOUNDATION_RUN_BEGIN=rep${repetition}" \
    | tee -a "$OUTPUT_DIR/runner.txt"

  set +e
  timeout "$RUN_TIMEOUT_S" \
    env \
      S2_ASSURANCE_OUTPUT_DIR="$run_dir" \
      S2_ASSURANCE_DURATION="${PHASE16_FOUNDATION_DURATION_S:-12}" \
      S2_ASSURANCE_FAULT_AFTER_S="${PHASE16_FOUNDATION_FAULT_AFTER_S:-4}" \
      S2_ASSURANCE_MINIMUM_POST_S="${PHASE16_FOUNDATION_MINIMUM_POST_S:-5}" \
      S2_ASSURANCE_WINDOW_GUARD_S="${PHASE16_FOUNDATION_WINDOW_GUARD_S:-0.25}" \
      S2_ASSURANCE_MULTICAST_RATE_MBPS="${PHASE16_FOUNDATION_MULTICAST_RATE_MBPS:-2}" \
      S2_ASSURANCE_CONTROL_RATE_MBPS="${PHASE16_FOUNDATION_CONTROL_RATE_MBPS:-0.5}" \
      S2_ASSURANCE_PACKET_SIZE="${PHASE16_FOUNDATION_PACKET_SIZE:-1200}" \
      S2_ASSURANCE_SPIN_THRESHOLD_US="${PHASE16_FOUNDATION_SPIN_THRESHOLD_US:-900}" \
      S2_ASSURANCE_POLL_INTERVAL_S="${PHASE16_FOUNDATION_POLL_INTERVAL_S:-0.02}" \
      S2_ASSURANCE_DRIFT_CONFIRMATIONS="${PHASE16_FOUNDATION_DRIFT_CONFIRMATIONS:-3}" \
      S2_ASSURANCE_CONVERGENCE_CONFIRMATIONS="${PHASE16_FOUNDATION_CONVERGENCE_CONFIRMATIONS:-2}" \
      S2_ASSURANCE_MAX_REMEDIATION_ATTEMPTS="${PHASE16_FOUNDATION_MAX_REMEDIATION_ATTEMPTS:-3}" \
      S2_ASSURANCE_INITIAL_BACKOFF_S="${PHASE16_FOUNDATION_INITIAL_BACKOFF_S:-0.01}" \
      S2_ASSURANCE_BACKOFF_MULTIPLIER="${PHASE16_FOUNDATION_BACKOFF_MULTIPLIER:-2}" \
      S2_ASSURANCE_MAX_BACKOFF_S="${PHASE16_FOUNDATION_MAX_BACKOFF_S:-0.10}" \
      S2_ASSURANCE_MAX_DETECTION_MS="${PHASE16_FOUNDATION_MAX_DETECTION_MS:-150}" \
      S2_ASSURANCE_MAX_CONTROL_PLANE_RECOVERY_MS="${PHASE16_FOUNDATION_MAX_CONTROL_PLANE_RECOVERY_MS:-150}" \
      S2_ASSURANCE_MAX_TOTAL_RECONCILIATION_MS="${PHASE16_FOUNDATION_MAX_TOTAL_RECONCILIATION_MS:-250}" \
      S2_ASSURANCE_MAX_FIRST_PACKET_MS="${PHASE16_FOUNDATION_MAX_FIRST_PACKET_MS:-50}" \
      S2_ASSURANCE_MAX_ABS_RATE_ERROR_PCT="${PHASE16_FOUNDATION_MAX_ABS_RATE_ERROR_PCT:-5}" \
      S2_ASSURANCE_MIN_INTER_SEND_RATIO="${PHASE16_FOUNDATION_MIN_INTER_SEND_RATIO:-0.98}" \
      S2_ASSURANCE_MINIMUM_STABLE_DELIVERY="${PHASE16_FOUNDATION_MINIMUM_STABLE_DELIVERY:-0.99}" \
      S2_ASSURANCE_MINIMUM_CONTROL_DELIVERY="${PHASE16_FOUNDATION_MINIMUM_CONTROL_DELIVERY:-0.99}" \
      S2_ASSURANCE_MINIMUM_LOST_PACKETS="${PHASE16_FOUNDATION_MINIMUM_LOST_PACKETS:-1}" \
      ./setup_all.sh run_s2_p4_autonomous_assurance \
      > "$run_log" 2>&1
  status=$?
  set -e

  printf '%s\t%s\t%s\n' \
    "$repetition" \
    "$run_dir" \
    "$status" \
    >> "$OUTPUT_DIR/manifest.tsv"

  echo "PHASE16_FOUNDATION_RUN_END=rep${repetition}:status${status}" \
    | tee -a "$OUTPUT_DIR/runner.txt"

  if (( status != 0 )); then
    operational_failures=$((operational_failures + 1))
    tail -n 220 "$run_log" >&2 || true
  fi
done

echo "PHASE16_FOUNDATION_OPERATIONAL_FAILURE_COUNT=$operational_failures" \
  | tee -a "$OUTPUT_DIR/runner.txt"

export PHASE16_FOUNDATION_OUTPUT_DIR="$OUTPUT_DIR"
export PHASE16_FOUNDATION_REPETITIONS="$REPETITIONS"

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


root = Path(os.environ["PHASE16_FOUNDATION_OUTPUT_DIR"])
repetitions = int(os.environ["PHASE16_FOUNDATION_REPETITIONS"])
manifest_path = root / "manifest.tsv"

with manifest_path.open("r", encoding="utf-8", newline="") as handle:
    rows = list(csv.DictReader(handle, delimiter="\t"))

failed: list[str] = []
invalid_rows: list[str] = []
records: list[dict[str, Any]] = []

print(f"PHASE16_FOUNDATION_EXPECTED_RUN_COUNT={repetitions}")
print(f"PHASE16_FOUNDATION_OBSERVED_RUN_COUNT={len(rows)}")

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
    scope = summary.get("scope") or {}
    assurance = summary.get("assurance") or {}
    injector = summary.get("fault_injector") or {}
    recovery = summary.get("recovery_metrics") or {}
    timing = recovery.get("timing_ms") or {}
    receivers = recovery.get("multicast_receivers") or {}
    control = recovery.get("unicast_control") or {}
    candidate_checks = recovery.get("candidate_checks") or {}
    p4 = summary.get("p4") or {}
    cleanup = p4.get("cleanup") or {}
    operational_checks = summary.get("operational_checks") or {}

    events = assurance.get("events") or []
    event_types = [event.get("event_type") for event in events]
    drift_event = next(
        (
            event
            for event in events
            if event.get("event_type") == "drift_confirmed"
        ),
        {},
    )
    drift_kinds = set((drift_event.get("details") or {}).get("drift_kinds") or [])

    content_ok = (
        summary.get("scenario") == "S2_P4_multicast_autonomous_assurance"
        and summary.get("fault_model")
        == "independent_p4runtime_multicast_state_deletion"
        and summary.get("passed") is True
        and operational_checks
        and all(value is True for value in operational_checks.values())
        and scope.get("persistent_mad_assurance_loop_exercised") is True
        and scope.get("autonomous_mad_detection_exercised") is True
        and scope.get("autonomous_mad_recovery_exercised") is True
        and scope.get("fault_schedule_shared_with_controller") is False
        and scope.get("independent_fault_injector_process_exercised") is True
        and scope.get("idempotent_component_reapply_exercised") is True
        and scope.get("bounded_retry_and_backoff_policy_enabled") is True
        and scope.get("bmv2_process_restart_exercised") is False
        and scope.get("pipeline_reload_exercised") is False
        and scope.get("multi_domain_assurance_validated") is False
        and injector.get("controller_notification_sent") is False
        and injector.get("absence_confirmed") is True
        and assurance.get("incident_count") == 1
        and assurance.get("successful_convergence_count") == 1
        and assurance.get("remediation_attempt_count", 0) >= 1
        and "drift_confirmed" in event_types
        and "remediation_attempt_started" in event_types
        and "convergence_confirmed" in event_types
        and drift_kinds
        >= {
            "missing_pre_multicast_group",
            "missing_multicast_table_entry",
        }
        and p4.get("pid_continuity") is True
        and cleanup.get("multicast_absent") is True
        and cleanup.get("unicast_absent") is True
        and candidate_checks
    )

    for label in ("B", "C"):
        receiver = receivers.get(label) or {}
        windows = receiver.get("windows") or {}
        first = receiver.get("first_recovered_packet") or {}
        gap = receiver.get("crossing_gap") or {}
        content_ok = content_ok and all(
            name in windows
            for name in ("pre_fault", "autonomous_outage", "post_recovery")
        )
        content_ok = content_ok and first.get("found") is True
        content_ok = content_ok and gap.get("found") is True

    control_windows = control.get("windows") or {}
    content_ok = content_ok and all(
        name in control_windows
        for name in ("pre_fault", "autonomous_outage", "post_recovery")
    )

    if not content_ok:
        invalid_rows.append(f"rep{repetition}:content")
        continue

    records.append(
        {
            "repetition": int(repetition),
            "candidate_found": bool(recovery.get("candidate_found")),
            "candidate_checks": candidate_checks,
            "detection_ms": float(
                timing.get("fault_write_to_drift_confirmation") or 0.0
            ),
            "control_plane_recovery_ms": float(
                timing.get("remediation_start_to_convergence") or 0.0
            ),
            "total_reconciliation_ms": float(
                timing.get("fault_write_to_convergence") or 0.0
            ),
            "incident_count": int(assurance.get("incident_count") or 0),
            "remediation_attempt_count": int(
                assurance.get("remediation_attempt_count") or 0
            ),
            "B": receivers["B"],
            "C": receivers["C"],
            "control": control,
        }
    )

print(f"PHASE16_FOUNDATION_INVALID_ROW_COUNT={len(invalid_rows)}")
print(f"PHASE16_FOUNDATION_VALID_RUN_COUNT={len(records)}")

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
    b_post = values(("B", "windows", "post_recovery", "delivery_ratio"))
    c_pre = values(("C", "windows", "pre_fault", "delivery_ratio"))
    c_post = values(("C", "windows", "post_recovery", "delivery_ratio"))
    b_gap = values(("B", "crossing_gap", "sequence_gap"))
    c_gap = values(("C", "crossing_gap", "sequence_gap"))
    b_first = values(
        ("B", "first_recovered_packet", "from_restoration_to_receive_ms")
    )
    c_first = values(
        ("C", "first_recovered_packet", "from_restoration_to_receive_ms")
    )
    control_ratios = [
        float(window["delivery_ratio"])
        for record in records
        for window in record["control"]["windows"].values()
        if int(window.get("expected_packets") or 0) > 0
    ]
    detection_ms = [record["detection_ms"] for record in records]
    recovery_ms = [record["control_plane_recovery_ms"] for record in records]
    total_ms = [record["total_reconciliation_ms"] for record in records]
    attempts = [record["remediation_attempt_count"] for record in records]
else:
    b_pre = b_post = c_pre = c_post = []
    b_gap = c_gap = b_first = c_first = []
    control_ratios = detection_ms = recovery_ms = total_ms = []
    attempts = []

candidate_consistent = all(
    record["candidate_found"]
    == all(bool(value) for value in record["candidate_checks"].values())
    for record in records
)
all_candidates = (
    len(records) == repetitions
    and all(record["candidate_found"] for record in records)
)

print(
    "PHASE16_FOUNDATION_INDEPENDENT_INJECTOR_COUNT="
    f"{sum(bool(record['candidate_checks'].get('independent_fault_injector')) for record in records)}"
)
print(
    "PHASE16_FOUNDATION_AUTONOMOUS_DRIFT_DETECTED_COUNT="
    f"{sum(bool(record['candidate_checks'].get('autonomous_drift_detected')) for record in records)}"
)
print(
    "PHASE16_FOUNDATION_AUTONOMOUS_CONVERGENCE_COUNT="
    f"{sum(bool(record['candidate_checks'].get('autonomous_convergence_confirmed')) for record in records)}"
)
print(
    "PHASE16_FOUNDATION_BMV2_PID_CONTINUITY_COUNT="
    f"{sum(bool(record['candidate_checks'].get('bmv2_process_continuity')) for record in records)}"
)
print(f"PHASE16_FOUNDATION_B_PRE_MIN_RATIO={min(b_pre) if b_pre else 0.0:.9f}")
print(f"PHASE16_FOUNDATION_B_POST_MIN_RATIO={min(b_post) if b_post else 0.0:.9f}")
print(f"PHASE16_FOUNDATION_C_PRE_MIN_RATIO={min(c_pre) if c_pre else 0.0:.9f}")
print(f"PHASE16_FOUNDATION_C_POST_MIN_RATIO={min(c_post) if c_post else 0.0:.9f}")
print(f"PHASE16_FOUNDATION_B_SEQUENCE_GAP_MIN={min(b_gap) if b_gap else 0.0:.0f}")
print(f"PHASE16_FOUNDATION_C_SEQUENCE_GAP_MIN={min(c_gap) if c_gap else 0.0:.0f}")
print(
    "PHASE16_FOUNDATION_CONTROL_MIN_RATIO="
    f"{min(control_ratios) if control_ratios else 0.0:.9f}"
)
print(
    "PHASE16_FOUNDATION_DETECTION_MEDIAN_MS="
    f"{statistics.median(detection_ms) if detection_ms else 0.0:.6f}"
)
print(
    "PHASE16_FOUNDATION_DETECTION_MAX_MS="
    f"{max(detection_ms) if detection_ms else 0.0:.6f}"
)
print(
    "PHASE16_FOUNDATION_CONTROL_PLANE_RECOVERY_MEDIAN_MS="
    f"{statistics.median(recovery_ms) if recovery_ms else 0.0:.6f}"
)
print(
    "PHASE16_FOUNDATION_CONTROL_PLANE_RECOVERY_MAX_MS="
    f"{max(recovery_ms) if recovery_ms else 0.0:.6f}"
)
print(
    "PHASE16_FOUNDATION_TOTAL_RECONCILIATION_MEDIAN_MS="
    f"{statistics.median(total_ms) if total_ms else 0.0:.6f}"
)
print(
    "PHASE16_FOUNDATION_TOTAL_RECONCILIATION_MAX_MS="
    f"{max(total_ms) if total_ms else 0.0:.6f}"
)
print(
    "PHASE16_FOUNDATION_B_FIRST_PACKET_MAX_MS="
    f"{max(b_first) if b_first else 0.0:.6f}"
)
print(
    "PHASE16_FOUNDATION_C_FIRST_PACKET_MAX_MS="
    f"{max(c_first) if c_first else 0.0:.6f}"
)
print(
    "PHASE16_FOUNDATION_REMEDIATION_ATTEMPTS_MAX="
    f"{max(attempts) if attempts else 0}"
)
print(f"PHASE16_FOUNDATION_CANDIDATE_CONSISTENT={candidate_consistent}")
print(f"PHASE16_FOUNDATION_CANDIDATE_FOUND={all_candidates}")
print("PHASE16_FOUNDATION_FAULT_SCHEDULE_SHARED_WITH_CONTROLLER=False")
print("PHASE16_FOUNDATION_PROCESS_RESTART_EXERCISED=False")
print("PHASE16_FOUNDATION_PIPELINE_RELOAD_EXERCISED=False")
print("PHASE16_FOUNDATION_MULTI_DOMAIN_ASSURANCE_VALIDATED=False")

summary = {
    "scenario": "phase16_autonomous_assurance_foundation_analysis",
    "repetitions": repetitions,
    "valid_runs": len(records),
    "invalid_rows": invalid_rows,
    "records": records,
    "candidate_found": all_candidates,
    "candidate_consistent": candidate_consistent,
    "scope": {
        "persistent_mad_assurance_loop_exercised": True,
        "independent_fault_injector_process_exercised": True,
        "fault_schedule_shared_with_controller": False,
        "autonomous_mad_detection_exercised": True,
        "autonomous_mad_recovery_exercised": True,
        "bmv2_process_restart_exercised": False,
        "pipeline_reload_exercised": False,
        "multi_domain_assurance_validated": False,
    },
}
(root / "foundation-summary.json").write_text(
    json.dumps(summary, indent=2) + "\n",
    encoding="utf-8",
)

if not candidate_consistent:
    failed.append("candidate_consistency")

if failed:
    print("PHASE16_FOUNDATION_ANALYSIS_FAILED=" + ",".join(failed))
    raise SystemExit(1)

print("PHASE16_FOUNDATION_ANALYSIS_OK")
PY

analysis_status=${PIPESTATUS[0]}
echo "phase16_foundation_analysis_exit_status=$analysis_status" \
  | tee -a "$OUTPUT_DIR/foundation-analysis.txt"

if (( operational_failures != 0 || analysis_status != 0 )); then
  echo "PHASE16_FOUNDATION_RUNNER_FAILED" | tee -a "$OUTPUT_DIR/runner.txt"
  exit 1
fi

echo "PHASE16_FOUNDATION_RUNNER_OK" | tee -a "$OUTPUT_DIR/runner.txt"
