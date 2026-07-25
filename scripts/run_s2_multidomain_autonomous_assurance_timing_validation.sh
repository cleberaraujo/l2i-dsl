#!/usr/bin/env bash
set -uo pipefail

# Validate calibrated timing bounds for coordinated Linux/NETCONF/P4 assurance.
#
# The four-run foundation is treated as a calibration sample. Its observed
# maxima are expanded by a fixed 25 percent guard and rounded upward to the
# nearest 250 ms, producing immutable validation bounds of 3000, 3750, and
# 6250 ms. This runner does not recompute or relax those bounds from its own
# results. The mirrored fault-time schedule reduces the risk that execution
# order, host temperature, or transient service state is confused with a
# timing property of the assurance loop.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-${PHASE17_TIMING_VALIDATION_OUTPUT_DIR:-}}"

if [[ -z "$OUTPUT_DIR" ]]; then
  echo "PHASE17_TIMING_VALIDATION_OUTPUT_DIR_REQUIRED" >&2
  exit 2
fi

RUN_TIMEOUT_S="${PHASE17_TIMING_VALIDATION_RUN_TIMEOUT_S:-240}"
MAX_DETECTION_MS="${PHASE17_TIMING_VALIDATION_MAX_DETECTION_MS:-3000}"
MAX_CONTROL_PLANE_RECOVERY_MS="${PHASE17_TIMING_VALIDATION_MAX_CONTROL_PLANE_RECOVERY_MS:-3750}"
MAX_TOTAL_RECONCILIATION_MS="${PHASE17_TIMING_VALIDATION_MAX_TOTAL_RECONCILIATION_MS:-6250}"
PROFILE_ID="${PHASE17_TIMING_VALIDATION_PROFILE_ID:-phase17-s2-multidomain-assurance-calibrated-candidate-v1}"

mkdir -p "$OUTPUT_DIR/runs"

cat > "$OUTPUT_DIR/schedule.tsv" <<'SCHEDULE'
order_index	condition_id	condition_repetition	fault_after_s
1	A	1	3.0
2	B	1	4.0
3	C	1	5.0
4	D	1	6.0
5	D	2	6.0
6	C	2	5.0
7	B	2	4.0
8	A	2	3.0
SCHEDULE

printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
  order_index \
  condition_id \
  condition_repetition \
  fault_after_s \
  output_dir \
  exit_status \
  > "$OUTPUT_DIR/manifest.tsv"

{
  echo "repository=$REPO_DIR"
  echo "output_dir=$OUTPUT_DIR"
  echo "validation_profile_id=$PROFILE_ID"
  echo "validation_type=mirrored_fault_time_matrix"
  echo "expected_run_count=8"
  echo "mirrored_order=A,B,C,D,D,C,B,A"
  echo "calibration_source=phase17_foundation_four_run_sample"
  echo "calibration_guard_factor=1.25"
  echo "calibration_rounding_quantum_ms=250"
  echo "maximum_detection_ms=$MAX_DETECTION_MS"
  echo "maximum_control_plane_recovery_ms=$MAX_CONTROL_PLANE_RECOVERY_MS"
  echo "maximum_total_reconciliation_ms=$MAX_TOTAL_RECONCILIATION_MS"
  echo "poll_interval_s=${PHASE17_TIMING_VALIDATION_POLL_INTERVAL_S:-0.05}"
  echo "drift_confirmations=${PHASE17_TIMING_VALIDATION_DRIFT_CONFIRMATIONS:-3}"
  echo "convergence_confirmations=${PHASE17_TIMING_VALIDATION_CONVERGENCE_CONFIRMATIONS:-2}"
  echo "maximum_remediation_attempts=${PHASE17_TIMING_VALIDATION_MAX_REMEDIATION_ATTEMPTS:-3}"
  echo "head=$(git -C "$REPO_DIR" rev-parse HEAD)"
  echo "branch=$(git -C "$REPO_DIR" branch --show-current)"

  if pgrep -f '[s]imple_switch_grpc' >/dev/null; then
    echo "bmv2_running=1"
  else
    echo "bmv2_running=0"
  fi

  if pgrep -f '[n]etopeer2-server' >/dev/null; then
    echo "netopeer_running=1"
  else
    echo "netopeer_running=0"
  fi

  if ss -H -ltn | grep -Eq ':9559\b'; then
    echo "p4runtime_available=1"
  else
    echo "p4runtime_available=0"
  fi

  if ss -H -ltn | grep -Eq ':830\b'; then
    echo "netconf_available=1"
  else
    echo "netconf_available=0"
  fi
} | tee "$OUTPUT_DIR/preflight.txt"

for marker in \
  bmv2_running=1 \
  netopeer_running=1 \
  p4runtime_available=1 \
  netconf_available=1
do
  if ! grep -Fq "$marker" "$OUTPUT_DIR/preflight.txt"; then
    echo "PHASE17_TIMING_VALIDATION_PREFLIGHT_FAILED=$marker" >&2
    exit 1
  fi
done

operational_failures=0

while IFS=$'\t' read -r \
  order_index \
  condition_id \
  condition_repetition \
  fault_after_s
do
  [[ "$order_index" == "order_index" ]] && continue

  run_name="order${order_index}-${condition_id}-rep${condition_repetition}"
  run_dir="$OUTPUT_DIR/runs/$run_name"
  run_log="$OUTPUT_DIR/runs/${run_name}.log"

  mkdir -p "$run_dir"

  echo "PHASE17_TIMING_VALIDATION_RUN_BEGIN=$run_name" \
    | tee -a "$OUTPUT_DIR/runner.txt"

  timeout "$RUN_TIMEOUT_S" \
    env \
      S2_MULTIDOMAIN_ASSURANCE_OUTPUT_DIR="$run_dir" \
      S2_MULTIDOMAIN_ASSURANCE_PROFILE_ID="$PROFILE_ID" \
      S2_MULTIDOMAIN_ASSURANCE_FAULT_AFTER_S="$fault_after_s" \
      S2_MULTIDOMAIN_ASSURANCE_INJECTOR_COMPLETION_TIMEOUT_S="${PHASE17_TIMING_VALIDATION_INJECTOR_COMPLETION_TIMEOUT_S:-10}" \
      S2_MULTIDOMAIN_ASSURANCE_RECOVERY_TIMEOUT_S="${PHASE17_TIMING_VALIDATION_RECOVERY_TIMEOUT_S:-12}" \
      S2_MULTIDOMAIN_ASSURANCE_POLL_INTERVAL_S="${PHASE17_TIMING_VALIDATION_POLL_INTERVAL_S:-0.05}" \
      S2_MULTIDOMAIN_ASSURANCE_DRIFT_CONFIRMATIONS="${PHASE17_TIMING_VALIDATION_DRIFT_CONFIRMATIONS:-3}" \
      S2_MULTIDOMAIN_ASSURANCE_CONVERGENCE_CONFIRMATIONS="${PHASE17_TIMING_VALIDATION_CONVERGENCE_CONFIRMATIONS:-2}" \
      S2_MULTIDOMAIN_ASSURANCE_MAX_REMEDIATION_ATTEMPTS="${PHASE17_TIMING_VALIDATION_MAX_REMEDIATION_ATTEMPTS:-3}" \
      S2_MULTIDOMAIN_ASSURANCE_INITIAL_BACKOFF_S="${PHASE17_TIMING_VALIDATION_INITIAL_BACKOFF_S:-0.05}" \
      S2_MULTIDOMAIN_ASSURANCE_BACKOFF_MULTIPLIER="${PHASE17_TIMING_VALIDATION_BACKOFF_MULTIPLIER:-2}" \
      S2_MULTIDOMAIN_ASSURANCE_MAX_BACKOFF_S="${PHASE17_TIMING_VALIDATION_MAX_BACKOFF_S:-0.5}" \
      S2_MULTIDOMAIN_ASSURANCE_MAX_DETECTION_MS="$MAX_DETECTION_MS" \
      S2_MULTIDOMAIN_ASSURANCE_MAX_CONTROL_PLANE_RECOVERY_MS="$MAX_CONTROL_PLANE_RECOVERY_MS" \
      S2_MULTIDOMAIN_ASSURANCE_MAX_TOTAL_RECONCILIATION_MS="$MAX_TOTAL_RECONCILIATION_MS" \
      "$REPO_DIR/setup_all.sh" run_s2_multidomain_autonomous_assurance \
      > "$run_log" 2>&1

  status=$?

  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$order_index" \
    "$condition_id" \
    "$condition_repetition" \
    "$fault_after_s" \
    "$run_dir" \
    "$status" \
    >> "$OUTPUT_DIR/manifest.tsv"

  echo "PHASE17_TIMING_VALIDATION_RUN_END=${run_name}:status${status}" \
    | tee -a "$OUTPUT_DIR/runner.txt"

  if [[ "$status" -ne 0 ]]; then
    operational_failures=$((operational_failures + 1))
    tail -n 240 "$run_log" >&2 || true
  fi
done < "$OUTPUT_DIR/schedule.tsv"

echo "PHASE17_TIMING_VALIDATION_OPERATIONAL_FAILURE_COUNT=$operational_failures" \
  | tee -a "$OUTPUT_DIR/runner.txt"

export PHASE17_TIMING_VALIDATION_OUTPUT_DIR="$OUTPUT_DIR"
export PHASE17_TIMING_VALIDATION_OPERATIONAL_FAILURES="$operational_failures"
export PHASE17_TIMING_VALIDATION_MAX_DETECTION_MS="$MAX_DETECTION_MS"
export PHASE17_TIMING_VALIDATION_MAX_CONTROL_PLANE_RECOVERY_MS="$MAX_CONTROL_PLANE_RECOVERY_MS"
export PHASE17_TIMING_VALIDATION_MAX_TOTAL_RECONCILIATION_MS="$MAX_TOTAL_RECONCILIATION_MS"
export PHASE17_TIMING_VALIDATION_PROFILE_ID="$PROFILE_ID"

PYTHONDONTWRITEBYTECODE=1 \
python3 - <<'PY' \
  | tee "$OUTPUT_DIR/timing-validation-analysis.txt"
from __future__ import annotations

import csv
import json
import os
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any


root = Path(
    os.environ["PHASE17_TIMING_VALIDATION_OUTPUT_DIR"]
)

operational_failures = int(
    os.environ[
        "PHASE17_TIMING_VALIDATION_OPERATIONAL_FAILURES"
    ]
)

maximum_detection_ms = float(
    os.environ[
        "PHASE17_TIMING_VALIDATION_MAX_DETECTION_MS"
    ]
)

maximum_control_plane_recovery_ms = float(
    os.environ[
        "PHASE17_TIMING_VALIDATION_MAX_CONTROL_PLANE_RECOVERY_MS"
    ]
)

maximum_total_reconciliation_ms = float(
    os.environ[
        "PHASE17_TIMING_VALIDATION_MAX_TOTAL_RECONCILIATION_MS"
    ]
)

profile_id = os.environ[
    "PHASE17_TIMING_VALIDATION_PROFILE_ID"
]

manifest_path = root / "manifest.tsv"

with manifest_path.open(
    "r",
    encoding="utf-8",
    newline="",
) as handle:
    rows = list(
        csv.DictReader(
            handle,
            delimiter="\t",
        )
    )

expected_order = (
    "A",
    "B",
    "C",
    "D",
    "D",
    "C",
    "B",
    "A",
)

observed_order = tuple(
    row["condition_id"]
    for row in rows
)

condition_counts = Counter(observed_order)

failed: list[str] = []
invalid_rows: list[str] = []
records: list[dict[str, Any]] = []

print(
    "PHASE17_TIMING_VALIDATION_OBSERVED_RUN_COUNT="
    f"{len(rows)}"
)

print(
    "PHASE17_TIMING_VALIDATION_MIRRORED_ORDER_OK="
    f"{observed_order == expected_order}"
)

if len(rows) != 8:
    failed.append("run_count")

if observed_order != expected_order:
    failed.append("mirrored_order")

if condition_counts != Counter(
    {
        "A": 2,
        "B": 2,
        "C": 2,
        "D": 2,
    }
):
    failed.append("condition_coverage")

for row in rows:
    status = int(row["exit_status"])
    run_dir = Path(row["output_dir"])
    summary_path = run_dir / "summary.json"
    identity = (
        f"order{row['order_index']}:"
        f"{row['condition_id']}:"
        f"rep{row['condition_repetition']}"
    )

    if status != 0 or not summary_path.is_file():
        invalid_rows.append(
            f"{identity}:status_or_summary"
        )
        continue

    try:
        summary = json.loads(
            summary_path.read_text(
                encoding="utf-8"
            )
        )
    except Exception as exc:
        invalid_rows.append(
            f"{identity}:json:{type(exc).__name__}"
        )
        continue

    configuration = (
        summary.get("configuration")
        or {}
    )

    scope = summary.get("scope") or {}
    injector = (
        summary.get("fault_injector")
        or {}
    )
    assurance = summary.get("assurance") or {}
    classification = (
        summary.get("classification")
        or {}
    )
    candidate_checks = (
        classification.get("candidate_checks")
        or {}
    )
    continuity = (
        summary.get("service_continuity")
        or {}
    )
    cleanup = summary.get("cleanup") or {}
    operational_checks = (
        summary.get("operational_checks")
        or {}
    )

    run_candidate_found = bool(
        classification.get("candidate_found")
    )

    run_candidate_consistent = (
        bool(candidate_checks)
        and run_candidate_found
        == all(
            bool(value)
            for value in candidate_checks.values()
        )
    )

    content_ok = all(
        (
            summary.get("scenario")
            == "S2_MAD_multidomain_autonomous_assurance",
            summary.get("assurance_profile_id")
            == profile_id,
            summary.get("fault_model")
            == (
                "independent_simultaneous_"
                "linux_netconf_p4_state_deletion"
            ),
            summary.get("passed") is True,
            summary.get("cleanup_ok") is True,
            bool(operational_checks),
            all(
                value is True
                for value in operational_checks.values()
            ),
            float(
                configuration.get("fault_after_s")
                or 0.0
            )
            == float(row["fault_after_s"]),
            float(
                configuration.get("maximum_detection_ms")
                or 0.0
            )
            == maximum_detection_ms,
            float(
                configuration.get(
                    "maximum_control_plane_recovery_ms"
                )
                or 0.0
            )
            == maximum_control_plane_recovery_ms,
            float(
                configuration.get(
                    "maximum_total_reconciliation_ms"
                )
                or 0.0
            )
            == maximum_total_reconciliation_ms,
            scope.get(
                "multi_domain_control_plane_assurance_exercised"
            )
            is True,
            scope.get("linux_tc_domain_exercised")
            is True,
            scope.get("netconf_yang_domain_exercised")
            is True,
            scope.get("p4runtime_domain_exercised")
            is True,
            scope.get(
                "global_cross_domain_convergence_rule_exercised"
            )
            is True,
            scope.get(
                "fault_schedule_shared_with_controller"
            )
            is False,
            scope.get(
                "independent_fault_injector_process_exercised"
            )
            is True,
            scope.get(
                "multi_domain_dataplane_assurance_validated"
            )
            is False,
            scope.get(
                "partial_remediation_failure_validated"
            )
            is False,
            scope.get("cross_domain_rollback_validated")
            is False,
            scope.get(
                "distributed_atomic_transaction_validated"
            )
            is False,
            injector.get("fault_source")
            == "independent_multidomain_subprocess",
            injector.get(
                "controller_notification_sent"
            )
            is False,
            injector.get(
                "all_domain_writes_accepted"
            )
            is True,
            injector.get(
                "all_domain_absence_confirmed"
            )
            is True,
            set(
                (injector.get("domains") or {}).keys()
            )
            == {"A", "B", "C"},
            assurance.get("failure") is None,
            assurance.get("incident_count") == 1,
            assurance.get(
                "successful_convergence_count"
            )
            == 1,
            assurance.get(
                "remediation_attempt_count"
            )
            == 1,
            set(
                classification.get(
                    "observed_drift_domains"
                )
                or []
            )
            == {"A", "B", "C"},
            set(
                classification.get(
                    "remediated_domains"
                )
                or []
            )
            == {"A", "B", "C"},
            run_candidate_found,
            run_candidate_consistent,
            continuity.get("bmv2_pid_continuity")
            is True,
            continuity.get("netopeer_pid_continuity")
            is True,
            continuity.get("linux_device_present")
            is True,
            cleanup.get("linux_state_absent")
            is True,
            cleanup.get("netconf_state_absent")
            is True,
            cleanup.get("p4_state_absent")
            is True,
            cleanup.get("linux_link_absent")
            is True,
            not (cleanup.get("errors") or []),
        )
    )

    if not content_ok:
        invalid_rows.append(
            f"{identity}:content"
        )
        continue

    records.append(summary)


def count(predicate) -> int:
    """Count validation records satisfying one exact property."""

    return sum(
        1
        for record in records
        if predicate(record)
    )


def values(name: str) -> list[float]:
    """Collect one direct timing metric from each valid run."""

    result: list[float] = []

    for record in records:
        timing = record.get("timing_ms") or {}
        value = timing.get(name)

        if value is not None:
            result.append(float(value))

    return result


def med(values_: list[float]) -> float:
    """Return a median or a negative sentinel when evidence is absent."""

    return median(values_) if values_ else -1.0


def maximum(values_: list[float]) -> float:
    """Return a maximum or a negative sentinel when evidence is absent."""

    return max(values_) if values_ else -1.0


detection = values(
    "fault_start_to_drift_confirmation"
)

control_plane = values(
    "remediation_start_to_global_convergence"
)

total = values(
    "fault_start_to_global_convergence"
)

valid_count = len(records)

independent_count = count(
    lambda record: (
        record.get("fault_injector")
        or {}
    ).get("fault_source")
    == "independent_multidomain_subprocess"
)

absence_count = count(
    lambda record: (
        record.get("fault_injector")
        or {}
    ).get("all_domain_absence_confirmed")
    is True
)

exact_drift_count = count(
    lambda record: set(
        (
            record.get("classification")
            or {}
        ).get("observed_drift_domains")
        or []
    )
    == {"A", "B", "C"}
)

exact_remediation_count = count(
    lambda record: set(
        (
            record.get("classification")
            or {}
        ).get("remediated_domains")
        or []
    )
    == {"A", "B", "C"}
)

global_convergence_count = count(
    lambda record: (
        record.get("assurance")
        or {}
    ).get("successful_convergence_count")
    == 1
)

candidate_count = count(
    lambda record: (
        record.get("classification")
        or {}
    ).get("candidate_found")
    is True
)

bmv2_continuity_count = count(
    lambda record: (
        record.get("service_continuity")
        or {}
    ).get("bmv2_pid_continuity")
    is True
)

netopeer_continuity_count = count(
    lambda record: (
        record.get("service_continuity")
        or {}
    ).get("netopeer_pid_continuity")
    is True
)

linux_link_continuity_count = count(
    lambda record: (
        record.get("service_continuity")
        or {}
    ).get("linux_device_present")
    is True
)

observed_detection_max = maximum(detection)
observed_control_plane_max = maximum(control_plane)
observed_total_max = maximum(total)

candidate_found = all(
    (
        operational_failures == 0,
        not invalid_rows,
        valid_count == 8,
        independent_count == 8,
        absence_count == 8,
        exact_drift_count == 8,
        exact_remediation_count == 8,
        global_convergence_count == 8,
        candidate_count == 8,
        bmv2_continuity_count == 8,
        netopeer_continuity_count == 8,
        linux_link_continuity_count == 8,
        0.0
        <= observed_detection_max
        <= maximum_detection_ms,
        0.0
        <= observed_control_plane_max
        <= maximum_control_plane_recovery_ms,
        0.0
        <= observed_total_max
        <= maximum_total_reconciliation_ms,
    )
)

validation_summary = {
    "profile_id": profile_id,
    "validation_type": "mirrored_fault_time_matrix",
    "observed_order": list(observed_order),
    "valid_run_count": valid_count,
    "invalid_rows": invalid_rows,
    "operational_failure_count": operational_failures,
    "timing_bounds_ms": {
        "detection": maximum_detection_ms,
        "control_plane_recovery": (
            maximum_control_plane_recovery_ms
        ),
        "total_reconciliation": (
            maximum_total_reconciliation_ms
        ),
    },
    "timing_observations_ms": {
        "detection_median": med(detection),
        "detection_max": observed_detection_max,
        "control_plane_recovery_median": (
            med(control_plane)
        ),
        "control_plane_recovery_max": (
            observed_control_plane_max
        ),
        "total_reconciliation_median": med(total),
        "total_reconciliation_max": observed_total_max,
    },
    "timing_margins_ms": {
        "detection": (
            maximum_detection_ms
            - observed_detection_max
        ),
        "control_plane_recovery": (
            maximum_control_plane_recovery_ms
            - observed_control_plane_max
        ),
        "total_reconciliation": (
            maximum_total_reconciliation_ms
            - observed_total_max
        ),
    },
    "counts": {
        "independent_injector": independent_count,
        "all_domain_absence": absence_count,
        "exact_drift_domains": exact_drift_count,
        "exact_remediation_domains": exact_remediation_count,
        "global_convergence": global_convergence_count,
        "candidate_found": candidate_count,
        "bmv2_pid_continuity": bmv2_continuity_count,
        "netopeer_pid_continuity": netopeer_continuity_count,
        "linux_link_continuity": linux_link_continuity_count,
    },
    "scope": {
        "multi_domain_control_plane_assurance_validated": (
            candidate_found
        ),
        "multi_domain_dataplane_assurance_validated": False,
        "partial_remediation_failure_validated": False,
        "cross_domain_rollback_validated": False,
        "distributed_atomic_transaction_validated": False,
    },
    "candidate_found": candidate_found,
}

(
    root
    / "timing-validation-summary.json"
).write_text(
    json.dumps(validation_summary, indent=2)
    + "\n",
    encoding="utf-8",
)

print(
    "PHASE17_TIMING_VALIDATION_INVALID_ROW_COUNT="
    f"{len(invalid_rows)}"
)

for invalid_row in invalid_rows:
    print(
        "PHASE17_TIMING_VALIDATION_INVALID_ROW="
        f"{invalid_row}"
    )

print(
    "PHASE17_TIMING_VALIDATION_VALID_RUN_COUNT="
    f"{valid_count}"
)

print(
    "PHASE17_TIMING_VALIDATION_INDEPENDENT_INJECTOR_COUNT="
    f"{independent_count}"
)

print(
    "PHASE17_TIMING_VALIDATION_ALL_DOMAIN_ABSENCE_COUNT="
    f"{absence_count}"
)

print(
    "PHASE17_TIMING_VALIDATION_EXACT_DRIFT_DOMAIN_COUNT="
    f"{exact_drift_count}"
)

print(
    "PHASE17_TIMING_VALIDATION_EXACT_REMEDIATION_DOMAIN_COUNT="
    f"{exact_remediation_count}"
)

print(
    "PHASE17_TIMING_VALIDATION_GLOBAL_CONVERGENCE_COUNT="
    f"{global_convergence_count}"
)

print(
    "PHASE17_TIMING_VALIDATION_CANDIDATE_RUN_COUNT="
    f"{candidate_count}"
)

print(
    "PHASE17_TIMING_VALIDATION_BMV2_PID_CONTINUITY_COUNT="
    f"{bmv2_continuity_count}"
)

print(
    "PHASE17_TIMING_VALIDATION_NETOPEER_PID_CONTINUITY_COUNT="
    f"{netopeer_continuity_count}"
)

print(
    "PHASE17_TIMING_VALIDATION_LINUX_LINK_CONTINUITY_COUNT="
    f"{linux_link_continuity_count}"
)

print(
    "PHASE17_TIMING_VALIDATION_DETECTION_BOUND_MS="
    f"{maximum_detection_ms:.6f}"
)

print(
    "PHASE17_TIMING_VALIDATION_DETECTION_MEDIAN_MS="
    f"{med(detection):.6f}"
)

print(
    "PHASE17_TIMING_VALIDATION_DETECTION_MAX_MS="
    f"{observed_detection_max:.6f}"
)

print(
    "PHASE17_TIMING_VALIDATION_DETECTION_MARGIN_MS="
    f"{maximum_detection_ms - observed_detection_max:.6f}"
)

print(
    "PHASE17_TIMING_VALIDATION_CONTROL_PLANE_RECOVERY_BOUND_MS="
    f"{maximum_control_plane_recovery_ms:.6f}"
)

print(
    "PHASE17_TIMING_VALIDATION_CONTROL_PLANE_RECOVERY_MEDIAN_MS="
    f"{med(control_plane):.6f}"
)

print(
    "PHASE17_TIMING_VALIDATION_CONTROL_PLANE_RECOVERY_MAX_MS="
    f"{observed_control_plane_max:.6f}"
)

print(
    "PHASE17_TIMING_VALIDATION_CONTROL_PLANE_RECOVERY_MARGIN_MS="
    f"{maximum_control_plane_recovery_ms - observed_control_plane_max:.6f}"
)

print(
    "PHASE17_TIMING_VALIDATION_TOTAL_RECONCILIATION_BOUND_MS="
    f"{maximum_total_reconciliation_ms:.6f}"
)

print(
    "PHASE17_TIMING_VALIDATION_TOTAL_RECONCILIATION_MEDIAN_MS="
    f"{med(total):.6f}"
)

print(
    "PHASE17_TIMING_VALIDATION_TOTAL_RECONCILIATION_MAX_MS="
    f"{observed_total_max:.6f}"
)

print(
    "PHASE17_TIMING_VALIDATION_TOTAL_RECONCILIATION_MARGIN_MS="
    f"{maximum_total_reconciliation_ms - observed_total_max:.6f}"
)

print(
    "PHASE17_TIMING_VALIDATION_MULTI_DOMAIN_DATAPLANE_ASSURANCE_VALIDATED=False"
)

print(
    "PHASE17_TIMING_VALIDATION_PARTIAL_REMEDIATION_FAILURE_VALIDATED=False"
)

print(
    "PHASE17_TIMING_VALIDATION_CROSS_DOMAIN_ROLLBACK_VALIDATED=False"
)

print(
    "PHASE17_TIMING_VALIDATION_DISTRIBUTED_ATOMIC_TRANSACTION_VALIDATED=False"
)

print(
    "PHASE17_TIMING_VALIDATION_CANDIDATE_FOUND="
    f"{candidate_found}"
)

if failed or not candidate_found:
    if not candidate_found:
        failed.append("candidate")

    print(
        "PHASE17_TIMING_VALIDATION_ANALYSIS_FAILED="
        + ",".join(failed)
    )
    raise SystemExit(1)

print("PHASE17_TIMING_VALIDATION_ANALYSIS_OK")
PY
analysis_status=${PIPESTATUS[0]}

{
  echo "PHASE17_TIMING_VALIDATION_OPERATIONAL_FAILURE_COUNT=$operational_failures"
  echo "phase17_timing_validation_analysis_exit_status=$analysis_status"

  if [[ "$operational_failures" -eq 0 && "$analysis_status" -eq 0 ]]; then
    echo "PHASE17_TIMING_VALIDATION_RUNNER_OK"
  else
    echo "PHASE17_TIMING_VALIDATION_RUNNER_FAILED"
  fi
} | tee -a "$OUTPUT_DIR/runner.txt"

if [[ "$operational_failures" -ne 0 || "$analysis_status" -ne 0 ]]; then
  exit 1
fi

exit 0
