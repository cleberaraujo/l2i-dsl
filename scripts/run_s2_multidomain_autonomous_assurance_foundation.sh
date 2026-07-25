#!/usr/bin/env bash
set -uo pipefail

# Repeat the coordinated Linux/NETCONF/P4 assurance experiment while keeping
# scientific candidate classification separate from operational execution.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$REPO_DIR/results/S2/multidomain-assurance-foundation-$(date -u +%Y%m%dT%H%M%SZ)}"
REPETITIONS="${PHASE17_FOUNDATION_REPETITIONS:-4}"
RUN_TIMEOUT_S="${PHASE17_FOUNDATION_RUN_TIMEOUT_S:-180}"

mkdir -p "$OUTPUT_DIR/runs"
MANIFEST="$OUTPUT_DIR/manifest.tsv"
PRE_FLIGHT="$OUTPUT_DIR/preflight.txt"
RUNNER_FILE="$OUTPUT_DIR/runner.txt"
ANALYSIS_FILE="$OUTPUT_DIR/foundation-analysis.txt"
SUMMARY_FILE="$OUTPUT_DIR/foundation-summary.json"

printf 'repetition\texit_status\toutput_dir\n' > "$MANIFEST"

{
  echo "branch=$(git -C "$REPO_DIR" branch --show-current)"
  echo "head=$(git -C "$REPO_DIR" rev-parse HEAD)"
  echo "origin_develop=$(git -C "$REPO_DIR" rev-parse origin/develop 2>/dev/null || true)"
  echo "repetitions=$REPETITIONS"
  echo "run_timeout_s=$RUN_TIMEOUT_S"
  echo
  pgrep -af simple_switch_grpc || true
  pgrep -af netopeer2-server || true
  echo
  ss -H -ltn | grep -E ':(830|9559)\b' || true
} > "$PRE_FLIGHT"

operational_failures=0

for repetition in $(seq 1 "$REPETITIONS"); do
  run_dir="$OUTPUT_DIR/rep${repetition}"
  run_log="$OUTPUT_DIR/runs/rep${repetition}.log"
  mkdir -p "$run_dir"

  echo "[Phase17-foundation] repetition=$repetition output=$run_dir"

  timeout "$RUN_TIMEOUT_S" \
  env \
    S2_MULTIDOMAIN_ASSURANCE_OUTPUT_DIR="$run_dir" \
    S2_MULTIDOMAIN_ASSURANCE_PROFILE_ID="phase17-s2-multidomain-assurance-foundation-v1" \
    S2_MULTIDOMAIN_ASSURANCE_FAULT_AFTER_S="${PHASE17_FOUNDATION_FAULT_AFTER_S:-4}" \
    S2_MULTIDOMAIN_ASSURANCE_POLL_INTERVAL_S="${PHASE17_FOUNDATION_POLL_INTERVAL_S:-0.05}" \
    S2_MULTIDOMAIN_ASSURANCE_DRIFT_CONFIRMATIONS="${PHASE17_FOUNDATION_DRIFT_CONFIRMATIONS:-3}" \
    S2_MULTIDOMAIN_ASSURANCE_CONVERGENCE_CONFIRMATIONS="${PHASE17_FOUNDATION_CONVERGENCE_CONFIRMATIONS:-2}" \
    S2_MULTIDOMAIN_ASSURANCE_MAX_REMEDIATION_ATTEMPTS="${PHASE17_FOUNDATION_MAX_REMEDIATION_ATTEMPTS:-3}" \
    S2_MULTIDOMAIN_ASSURANCE_INITIAL_BACKOFF_S="${PHASE17_FOUNDATION_INITIAL_BACKOFF_S:-0.05}" \
    S2_MULTIDOMAIN_ASSURANCE_BACKOFF_MULTIPLIER="${PHASE17_FOUNDATION_BACKOFF_MULTIPLIER:-2}" \
    S2_MULTIDOMAIN_ASSURANCE_MAX_BACKOFF_S="${PHASE17_FOUNDATION_MAX_BACKOFF_S:-0.5}" \
    S2_MULTIDOMAIN_ASSURANCE_MAX_DETECTION_MS="${PHASE17_FOUNDATION_MAX_DETECTION_MS:-1500}" \
    S2_MULTIDOMAIN_ASSURANCE_MAX_CONTROL_PLANE_RECOVERY_MS="${PHASE17_FOUNDATION_MAX_CONTROL_PLANE_RECOVERY_MS:-2500}" \
    S2_MULTIDOMAIN_ASSURANCE_MAX_TOTAL_RECONCILIATION_MS="${PHASE17_FOUNDATION_MAX_TOTAL_RECONCILIATION_MS:-4000}" \
    "$REPO_DIR/setup_all.sh" run_s2_multidomain_autonomous_assurance \
    > "$run_log" 2>&1

  status=$?
  printf '%s\t%s\t%s\n' "$repetition" "$status" "$run_dir" >> "$MANIFEST"
  if [[ "$status" -ne 0 ]]; then
    operational_failures=$((operational_failures + 1))
  fi
done

export PHASE17_FOUNDATION_OUTPUT_DIR="$OUTPUT_DIR"
export PHASE17_FOUNDATION_OPERATIONAL_FAILURES="$operational_failures"

python3 - <<'PY' \
  | tee "$ANALYSIS_FILE"
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from statistics import median
from typing import Any


root = Path(os.environ["PHASE17_FOUNDATION_OUTPUT_DIR"])
manifest_path = root / "manifest.tsv"
summary_path = root / "foundation-summary.json"
operational_failures = int(
    os.environ["PHASE17_FOUNDATION_OPERATIONAL_FAILURES"]
)


def numeric(
    mapping: dict[str, Any],
    key: str,
    default: float = -1.0,
) -> float:
    """Read one numeric field without treating a valid zero as missing."""

    value = mapping.get(key)
    if value is None:
        return default
    return float(value)


with manifest_path.open("r", encoding="utf-8", newline="") as handle:
    rows = list(csv.DictReader(handle, delimiter="\t"))

records: list[dict[str, Any]] = []
invalid_rows: list[str] = []

for row in rows:
    repetition = row["repetition"]
    run_dir = Path(row["output_dir"])
    run_summary = run_dir / "summary.json"
    status = int(row["exit_status"])
    if status != 0 or not run_summary.is_file():
        invalid_rows.append(f"rep{repetition}:status_or_summary")
        continue
    try:
        summary = json.loads(run_summary.read_text(encoding="utf-8"))
    except Exception as exc:
        invalid_rows.append(f"rep{repetition}:json:{type(exc).__name__}")
        continue
    if (
        summary.get("passed") is not True
        or summary.get("cleanup_ok") is not True
    ):
        invalid_rows.append(f"rep{repetition}:operational_or_cleanup_checks")
        continue
    records.append(summary)


def count(predicate) -> int:
    return sum(1 for record in records if predicate(record))


def timing_values(name: str) -> list[float]:
    values: list[float] = []
    for record in records:
        timing = record.get("timing_ms") or {}
        value = timing.get(name)
        if value is not None:
            values.append(float(value))
    return values


def maximum(values: list[float]) -> float:
    return max(values) if values else -1.0


def med(values: list[float]) -> float:
    return median(values) if values else -1.0


valid_count = len(records)
independent_count = count(
    lambda record: (
        (record.get("fault_injector") or {}).get("fault_source")
        == "independent_multidomain_subprocess"
        and (record.get("fault_injector") or {}).get(
            "controller_notification_sent"
        ) is False
    )
)
absence_count = count(
    lambda record: (record.get("fault_injector") or {}).get(
        "all_domain_absence_confirmed"
    ) is True
)
exact_drift_count = count(
    lambda record: set(
        ((record.get("classification") or {}).get("observed_drift_domains") or [])
    ) == {"A", "B", "C"}
)
exact_remediation_count = count(
    lambda record: set(
        ((record.get("classification") or {}).get("remediated_domains") or [])
    ) == {"A", "B", "C"}
)
global_convergence_count = count(
    lambda record: (
        (record.get("assurance") or {}).get("successful_convergence_count")
        == 1
        and (record.get("final_observation") or {}).get("healthy") is True
    )
)
bmv2_continuity_count = count(
    lambda record: (record.get("service_continuity") or {}).get(
        "bmv2_pid_continuity"
    ) is True
)
netopeer_continuity_count = count(
    lambda record: (record.get("service_continuity") or {}).get(
        "netopeer_pid_continuity"
    ) is True
)
linux_link_continuity_count = count(
    lambda record: (record.get("service_continuity") or {}).get(
        "linux_device_present"
    ) is True
)

remediation_attempts = [
    int((record.get("assurance") or {}).get("remediation_attempt_count") or 0)
    for record in records
]

detection = timing_values("fault_start_to_drift_confirmation")
control_plane = timing_values("remediation_start_to_global_convergence")
total = timing_values("fault_start_to_global_convergence")

candidate_values = [
    bool((record.get("classification") or {}).get("candidate_found"))
    for record in records
]
candidate_found = bool(records) and all(candidate_values)
candidate_consistent = (
    len(candidate_values) == valid_count
    and candidate_found == all(candidate_values)
)

scope = {
    "multi_domain_control_plane_assurance_exercised": True,
    "linux_tc_domain_exercised": True,
    "netconf_yang_domain_exercised": True,
    "p4runtime_domain_exercised": True,
    "multi_domain_dataplane_assurance_validated": False,
    "partial_remediation_failure_validated": False,
    "cross_domain_rollback_validated": False,
    "distributed_atomic_transaction_validated": False,
}

foundation = {
    "records": [
        {
            "scenario": record.get("scenario"),
            "candidate_found": (
                record.get("classification") or {}
            ).get("candidate_found"),
            "timing_ms": record.get("timing_ms"),
            "service_continuity": record.get("service_continuity"),
            "summary_path": str(
                Path(row["output_dir"]) / "summary.json"
            ),
        }
        for row, record in zip(
            [item for item in rows if int(item["exit_status"]) == 0],
            records,
        )
    ],
    "operationally_valid": (
        operational_failures == 0 and not invalid_rows and valid_count == 4
    ),
    "candidate_found": candidate_found,
    "candidate_consistent": candidate_consistent,
    "scope": scope,
    "invalid_rows": invalid_rows,
}
summary_path.write_text(
    json.dumps(foundation, indent=2) + "\n",
    encoding="utf-8",
)

print(f"PHASE17_FOUNDATION_INVALID_ROW_COUNT={len(invalid_rows)}")
print(f"PHASE17_FOUNDATION_VALID_RUN_COUNT={valid_count}")
print(f"PHASE17_FOUNDATION_INDEPENDENT_INJECTOR_COUNT={independent_count}")
print(f"PHASE17_FOUNDATION_ALL_DOMAIN_ABSENCE_COUNT={absence_count}")
print(f"PHASE17_FOUNDATION_EXACT_DRIFT_DOMAIN_COUNT={exact_drift_count}")
print(f"PHASE17_FOUNDATION_EXACT_REMEDIATION_DOMAIN_COUNT={exact_remediation_count}")
print(f"PHASE17_FOUNDATION_GLOBAL_CONVERGENCE_COUNT={global_convergence_count}")
print(f"PHASE17_FOUNDATION_BMV2_PID_CONTINUITY_COUNT={bmv2_continuity_count}")
print(f"PHASE17_FOUNDATION_NETOPEER_PID_CONTINUITY_COUNT={netopeer_continuity_count}")
print(f"PHASE17_FOUNDATION_LINUX_LINK_CONTINUITY_COUNT={linux_link_continuity_count}")
print(f"PHASE17_FOUNDATION_DETECTION_MEDIAN_MS={med(detection):.6f}")
print(f"PHASE17_FOUNDATION_DETECTION_MAX_MS={maximum(detection):.6f}")
print(
    "PHASE17_FOUNDATION_CONTROL_PLANE_RECOVERY_MEDIAN_MS="
    f"{med(control_plane):.6f}"
)
print(
    "PHASE17_FOUNDATION_CONTROL_PLANE_RECOVERY_MAX_MS="
    f"{maximum(control_plane):.6f}"
)
print(f"PHASE17_FOUNDATION_TOTAL_RECONCILIATION_MEDIAN_MS={med(total):.6f}")
print(f"PHASE17_FOUNDATION_TOTAL_RECONCILIATION_MAX_MS={maximum(total):.6f}")
print(
    "PHASE17_FOUNDATION_REMEDIATION_ATTEMPTS_MAX="
    f"{max(remediation_attempts) if remediation_attempts else -1}"
)
print(f"PHASE17_FOUNDATION_CANDIDATE_CONSISTENT={candidate_consistent}")
print(f"PHASE17_FOUNDATION_CANDIDATE_FOUND={candidate_found}")
print("PHASE17_FOUNDATION_MULTI_DOMAIN_DATAPLANE_ASSURANCE_VALIDATED=False")
print("PHASE17_FOUNDATION_PARTIAL_REMEDIATION_FAILURE_VALIDATED=False")
print("PHASE17_FOUNDATION_CROSS_DOMAIN_ROLLBACK_VALIDATED=False")
print("PHASE17_FOUNDATION_DISTRIBUTED_ATOMIC_TRANSACTION_VALIDATED=False")

if operational_failures or invalid_rows or valid_count != 4:
    print("PHASE17_FOUNDATION_ANALYSIS_FAILED")
    raise SystemExit(1)

print("PHASE17_FOUNDATION_ANALYSIS_OK")
PY
analysis_status=${PIPESTATUS[0]}

{
  echo "PHASE17_FOUNDATION_OPERATIONAL_FAILURE_COUNT=$operational_failures"
  echo "phase17_foundation_analysis_exit_status=$analysis_status"
  if [[ "$operational_failures" -eq 0 && "$analysis_status" -eq 0 ]]; then
    echo "PHASE17_FOUNDATION_RUNNER_OK"
  else
    echo "PHASE17_FOUNDATION_RUNNER_FAILED"
  fi
} | tee "$RUNNER_FILE"

if [[ "$operational_failures" -ne 0 || "$analysis_status" -ne 0 ]]; then
  exit 1
fi
exit 0
