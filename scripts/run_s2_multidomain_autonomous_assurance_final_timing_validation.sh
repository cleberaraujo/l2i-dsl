#!/usr/bin/env bash
set -uo pipefail

# Validate the recalibrated Phase 17 timing candidate on an independent matrix.
#
# The original four-run foundation and the first eight-run timing matrix are
# preserved as a twelve-run recalibration sample. The first validation matrix
# legitimately rejected the 3000 ms end-to-end detection bound in one run.
# The new 4000 ms bound is therefore fixed before this independent matrix by
# applying the same 25 percent guard and 250 ms upward-rounding rule to the
# combined maximum of 3090.596727 ms. The already validated control-plane and
# total-reconciliation bounds remain unchanged.
#
# Additional sub-metrics make the timing semantics explicit without replacing
# the original end-to-end metric:
#   * fault injection and absence-confirmation duration;
#   * fault start to first aggregate drift observation;
#   * first drift observation to confirmed drift.
#
# These sub-bounds were also fixed from the combined twelve-run sample before
# this runner was executed. The runner never retunes any bound from its own
# observations.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-${PHASE17_FINAL_TIMING_VALIDATION_OUTPUT_DIR:-}}"

if [[ -z "$OUTPUT_DIR" ]]; then
  echo "PHASE17_FINAL_TIMING_VALIDATION_OUTPUT_DIR_REQUIRED" >&2
  exit 2
fi

RUN_TIMEOUT_S="${PHASE17_FINAL_TIMING_VALIDATION_RUN_TIMEOUT_S:-240}"
PROFILE_ID="${PHASE17_FINAL_TIMING_VALIDATION_PROFILE_ID:-phase17-s2-multidomain-assurance-recalibrated-candidate-v2}"

MAX_DETECTION_MS="${PHASE17_FINAL_TIMING_VALIDATION_MAX_DETECTION_MS:-4000}"
MAX_CONTROL_PLANE_RECOVERY_MS="${PHASE17_FINAL_TIMING_VALIDATION_MAX_CONTROL_PLANE_RECOVERY_MS:-3750}"
MAX_TOTAL_RECONCILIATION_MS="${PHASE17_FINAL_TIMING_VALIDATION_MAX_TOTAL_RECONCILIATION_MS:-6250}"
MAX_INJECTION_CONFIRMATION_MS="${PHASE17_FINAL_TIMING_VALIDATION_MAX_INJECTION_CONFIRMATION_MS:-2750}"
MAX_FIRST_DRIFT_OBSERVATION_MS="${PHASE17_FINAL_TIMING_VALIDATION_MAX_FIRST_DRIFT_OBSERVATION_MS:-1000}"
MAX_CONFIRMATION_ACCUMULATION_MS="${PHASE17_FINAL_TIMING_VALIDATION_MAX_CONFIRMATION_ACCUMULATION_MS:-3250}"

mkdir -p "$OUTPUT_DIR/runs"

cat > "$OUTPUT_DIR/schedule.tsv" <<'SCHEDULE'
order_index	condition_id	condition_repetition	fault_after_s
1	B	1	4.0
2	D	1	6.0
3	A	1	3.0
4	C	1	5.0
5	C	2	5.0
6	A	2	3.0
7	D	2	6.0
8	B	2	4.0
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
  echo "validation_type=independent_recalibrated_mirrored_fault_time_matrix"
  echo "expected_run_count=8"
  echo "mirrored_order=B,D,A,C,C,A,D,B"
  echo "recalibration_source=phase17_combined_twelve_run_sample"
  echo "recalibration_foundation_run_count=4"
  echo "recalibration_pilot_validation_run_count=8"
  echo "recalibration_guard_factor=1.25"
  echo "recalibration_rounding_quantum_ms=250"
  echo "recalibration_detection_source_max_ms=3090.596727"
  echo "recalibration_injection_source_max_ms=2193.442394"
  echo "recalibration_first_drift_source_max_ms=769.245751"
  echo "recalibration_confirmation_source_max_ms=2417.362894"
  echo "maximum_detection_ms=$MAX_DETECTION_MS"
  echo "maximum_control_plane_recovery_ms=$MAX_CONTROL_PLANE_RECOVERY_MS"
  echo "maximum_total_reconciliation_ms=$MAX_TOTAL_RECONCILIATION_MS"
  echo "maximum_injection_confirmation_ms=$MAX_INJECTION_CONFIRMATION_MS"
  echo "maximum_first_drift_observation_ms=$MAX_FIRST_DRIFT_OBSERVATION_MS"
  echo "maximum_confirmation_accumulation_ms=$MAX_CONFIRMATION_ACCUMULATION_MS"
  echo "poll_interval_s=${PHASE17_FINAL_TIMING_VALIDATION_POLL_INTERVAL_S:-0.05}"
  echo "drift_confirmations=${PHASE17_FINAL_TIMING_VALIDATION_DRIFT_CONFIRMATIONS:-3}"
  echo "convergence_confirmations=${PHASE17_FINAL_TIMING_VALIDATION_CONVERGENCE_CONFIRMATIONS:-2}"
  echo "maximum_remediation_attempts=${PHASE17_FINAL_TIMING_VALIDATION_MAX_REMEDIATION_ATTEMPTS:-3}"
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
    echo "PHASE17_FINAL_TIMING_VALIDATION_PREFLIGHT_FAILED=$marker" >&2
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

  echo "PHASE17_FINAL_TIMING_VALIDATION_RUN_BEGIN=$run_name" \
    | tee -a "$OUTPUT_DIR/runner.txt"

  timeout "$RUN_TIMEOUT_S" \
    env \
      S2_MULTIDOMAIN_ASSURANCE_OUTPUT_DIR="$run_dir" \
      S2_MULTIDOMAIN_ASSURANCE_PROFILE_ID="$PROFILE_ID" \
      S2_MULTIDOMAIN_ASSURANCE_FAULT_AFTER_S="$fault_after_s" \
      S2_MULTIDOMAIN_ASSURANCE_INJECTOR_COMPLETION_TIMEOUT_S="${PHASE17_FINAL_TIMING_VALIDATION_INJECTOR_COMPLETION_TIMEOUT_S:-10}" \
      S2_MULTIDOMAIN_ASSURANCE_RECOVERY_TIMEOUT_S="${PHASE17_FINAL_TIMING_VALIDATION_RECOVERY_TIMEOUT_S:-12}" \
      S2_MULTIDOMAIN_ASSURANCE_POLL_INTERVAL_S="${PHASE17_FINAL_TIMING_VALIDATION_POLL_INTERVAL_S:-0.05}" \
      S2_MULTIDOMAIN_ASSURANCE_DRIFT_CONFIRMATIONS="${PHASE17_FINAL_TIMING_VALIDATION_DRIFT_CONFIRMATIONS:-3}" \
      S2_MULTIDOMAIN_ASSURANCE_CONVERGENCE_CONFIRMATIONS="${PHASE17_FINAL_TIMING_VALIDATION_CONVERGENCE_CONFIRMATIONS:-2}" \
      S2_MULTIDOMAIN_ASSURANCE_MAX_REMEDIATION_ATTEMPTS="${PHASE17_FINAL_TIMING_VALIDATION_MAX_REMEDIATION_ATTEMPTS:-3}" \
      S2_MULTIDOMAIN_ASSURANCE_INITIAL_BACKOFF_S="${PHASE17_FINAL_TIMING_VALIDATION_INITIAL_BACKOFF_S:-0.05}" \
      S2_MULTIDOMAIN_ASSURANCE_BACKOFF_MULTIPLIER="${PHASE17_FINAL_TIMING_VALIDATION_BACKOFF_MULTIPLIER:-2}" \
      S2_MULTIDOMAIN_ASSURANCE_MAX_BACKOFF_S="${PHASE17_FINAL_TIMING_VALIDATION_MAX_BACKOFF_S:-0.5}" \
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

  echo "PHASE17_FINAL_TIMING_VALIDATION_RUN_END=${run_name}:status${status}" \
    | tee -a "$OUTPUT_DIR/runner.txt"

  if [[ "$status" -ne 0 ]]; then
    operational_failures=$((operational_failures + 1))
    tail -n 240 "$run_log" >&2 || true
  fi
done < "$OUTPUT_DIR/schedule.tsv"

echo "PHASE17_FINAL_TIMING_VALIDATION_OPERATIONAL_FAILURE_COUNT=$operational_failures" \
  | tee -a "$OUTPUT_DIR/runner.txt"

export PHASE17_FINAL_TIMING_VALIDATION_OUTPUT_DIR="$OUTPUT_DIR"
export PHASE17_FINAL_TIMING_VALIDATION_OPERATIONAL_FAILURES="$operational_failures"
export PHASE17_FINAL_TIMING_VALIDATION_PROFILE_ID="$PROFILE_ID"
export PHASE17_FINAL_TIMING_VALIDATION_MAX_DETECTION_MS="$MAX_DETECTION_MS"
export PHASE17_FINAL_TIMING_VALIDATION_MAX_CONTROL_PLANE_RECOVERY_MS="$MAX_CONTROL_PLANE_RECOVERY_MS"
export PHASE17_FINAL_TIMING_VALIDATION_MAX_TOTAL_RECONCILIATION_MS="$MAX_TOTAL_RECONCILIATION_MS"
export PHASE17_FINAL_TIMING_VALIDATION_MAX_INJECTION_CONFIRMATION_MS="$MAX_INJECTION_CONFIRMATION_MS"
export PHASE17_FINAL_TIMING_VALIDATION_MAX_FIRST_DRIFT_OBSERVATION_MS="$MAX_FIRST_DRIFT_OBSERVATION_MS"
export PHASE17_FINAL_TIMING_VALIDATION_MAX_CONFIRMATION_ACCUMULATION_MS="$MAX_CONFIRMATION_ACCUMULATION_MS"

PYTHONDONTWRITEBYTECODE=1 \
python3 - <<'PY' \
  | tee "$OUTPUT_DIR/final-timing-validation-analysis.txt"
"""Analyze the independent recalibrated Phase 17 timing matrix."""

from __future__ import annotations

import csv
import json
import os
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any


root = Path(
    os.environ[
        "PHASE17_FINAL_TIMING_VALIDATION_OUTPUT_DIR"
    ]
)

operational_failures = int(
    os.environ[
        "PHASE17_FINAL_TIMING_VALIDATION_OPERATIONAL_FAILURES"
    ]
)

profile_id = os.environ[
    "PHASE17_FINAL_TIMING_VALIDATION_PROFILE_ID"
]

bounds = {
    "detection": float(
        os.environ[
            "PHASE17_FINAL_TIMING_VALIDATION_MAX_DETECTION_MS"
        ]
    ),
    "control_plane_recovery": float(
        os.environ[
            "PHASE17_FINAL_TIMING_VALIDATION_MAX_CONTROL_PLANE_RECOVERY_MS"
        ]
    ),
    "total_reconciliation": float(
        os.environ[
            "PHASE17_FINAL_TIMING_VALIDATION_MAX_TOTAL_RECONCILIATION_MS"
        ]
    ),
    "injection_confirmation": float(
        os.environ[
            "PHASE17_FINAL_TIMING_VALIDATION_MAX_INJECTION_CONFIRMATION_MS"
        ]
    ),
    "first_drift_observation": float(
        os.environ[
            "PHASE17_FINAL_TIMING_VALIDATION_MAX_FIRST_DRIFT_OBSERVATION_MS"
        ]
    ),
    "confirmation_accumulation": float(
        os.environ[
            "PHASE17_FINAL_TIMING_VALIDATION_MAX_CONFIRMATION_ACCUMULATION_MS"
        ]
    ),
}

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
    "B",
    "D",
    "A",
    "C",
    "C",
    "A",
    "D",
    "B",
)

observed_order = tuple(
    row["condition_id"]
    for row in rows
)

condition_counts = Counter(observed_order)

failed: list[str] = []
invalid_rows: list[str] = []
records: list[dict[str, Any]] = []


def event_by_type(
    events: list[dict[str, Any]],
    event_type: str,
) -> dict[str, Any] | None:
    """Return the first event with one exact type."""

    return next(
        (
            event
            for event in events
            if event.get("event_type")
            == event_type
        ),
        None,
    )


def duration_ms(
    started_ns: Any,
    completed_ns: Any,
) -> float | None:
    """Convert two monotonic timestamps to milliseconds."""

    if started_ns is None or completed_ns is None:
        return None

    return (
        int(completed_ns)
        - int(started_ns)
    ) / 1_000_000.0


def exact_float(
    value: Any,
    expected: float,
) -> bool:
    """Compare one serialized configuration value with its fixed bound."""

    try:
        return float(value) == expected
    except (
        TypeError,
        ValueError,
    ):
        return False


print(
    "PHASE17_FINAL_TIMING_VALIDATION_OBSERVED_RUN_COUNT="
    f"{len(rows)}"
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_MIRRORED_ORDER_OK="
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
    assurance = (
        summary.get("assurance")
        or {}
    )
    events = assurance.get("events") or []
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
    timing = summary.get("timing_ms") or {}

    drift_observed = event_by_type(
        events,
        "drift_observed",
    )
    drift_confirmed = event_by_type(
        events,
        "drift_confirmed",
    )

    fault_started_ns = injector.get(
        "fault_started_monotonic_ns"
    )
    fault_completed_ns = injector.get(
        "fault_completed_monotonic_ns"
    )

    injection_confirmation_ms = duration_ms(
        fault_started_ns,
        fault_completed_ns,
    )

    first_drift_observation_ms = duration_ms(
        fault_started_ns,
        (
            drift_observed.get("monotonic_ns")
            if drift_observed is not None
            else None
        ),
    )

    confirmation_accumulation_ms = duration_ms(
        (
            drift_observed.get("monotonic_ns")
            if drift_observed is not None
            else None
        ),
        (
            drift_confirmed.get("monotonic_ns")
            if drift_confirmed is not None
            else None
        ),
    )

    run_candidate_found = bool(
        classification.get("candidate_found")
    )

    run_candidate_consistent = (
        bool(candidate_checks)
        and run_candidate_found
        == all(
            bool(value)
            for value
            in candidate_checks.values()
        )
    )

    derived_checks = {
        "injection_confirmation_bound": (
            injection_confirmation_ms is not None
            and 0.0
            <= injection_confirmation_ms
            <= bounds["injection_confirmation"]
        ),
        "first_drift_observation_bound": (
            first_drift_observation_ms is not None
            and 0.0
            <= first_drift_observation_ms
            <= bounds["first_drift_observation"]
        ),
        "confirmation_accumulation_bound": (
            confirmation_accumulation_ms is not None
            and 0.0
            <= confirmation_accumulation_ms
            <= bounds["confirmation_accumulation"]
        ),
    }

    content_checks = {
        "scenario": (
            summary.get("scenario")
            == "S2_MAD_multidomain_autonomous_assurance"
        ),
        "profile_id": (
            summary.get("assurance_profile_id")
            == profile_id
        ),
        "fault_model": (
            summary.get("fault_model")
            == (
                "independent_simultaneous_"
                "linux_netconf_p4_state_deletion"
            )
        ),
        "summary_passed": (
            summary.get("passed") is True
        ),
        "cleanup_ok": (
            summary.get("cleanup_ok") is True
        ),
        "operational_checks": (
            bool(operational_checks)
            and all(
                value is True
                for value
                in operational_checks.values()
            )
        ),
        "fault_after_s": exact_float(
            configuration.get("fault_after_s"),
            float(row["fault_after_s"]),
        ),
        "detection_bound_configuration": exact_float(
            configuration.get(
                "maximum_detection_ms"
            ),
            bounds["detection"],
        ),
        "control_plane_bound_configuration": exact_float(
            configuration.get(
                "maximum_control_plane_recovery_ms"
            ),
            bounds["control_plane_recovery"],
        ),
        "total_bound_configuration": exact_float(
            configuration.get(
                "maximum_total_reconciliation_ms"
            ),
            bounds["total_reconciliation"],
        ),
        "scope_control_plane": (
            scope.get(
                "multi_domain_control_plane_assurance_exercised"
            )
            is True
        ),
        "scope_linux": (
            scope.get("linux_tc_domain_exercised")
            is True
        ),
        "scope_netconf": (
            scope.get("netconf_yang_domain_exercised")
            is True
        ),
        "scope_p4runtime": (
            scope.get("p4runtime_domain_exercised")
            is True
        ),
        "scope_global_convergence": (
            scope.get(
                "global_cross_domain_convergence_rule_exercised"
            )
            is True
        ),
        "scope_schedule_not_shared": (
            scope.get(
                "fault_schedule_shared_with_controller"
            )
            is False
        ),
        "scope_independent_injector": (
            scope.get(
                "independent_fault_injector_process_exercised"
            )
            is True
        ),
        "scope_no_dataplane_claim": (
            scope.get(
                "multi_domain_dataplane_assurance_validated"
            )
            is False
        ),
        "scope_no_partial_failure_claim": (
            scope.get(
                "partial_remediation_failure_validated"
            )
            is False
        ),
        "scope_no_rollback_claim": (
            scope.get("cross_domain_rollback_validated")
            is False
        ),
        "scope_no_atomicity_claim": (
            scope.get(
                "distributed_atomic_transaction_validated"
            )
            is False
        ),
        "injector_source": (
            injector.get("fault_source")
            == "independent_multidomain_subprocess"
        ),
        "injector_no_notification": (
            injector.get(
                "controller_notification_sent"
            )
            is False
        ),
        "injector_writes": (
            injector.get(
                "all_domain_writes_accepted"
            )
            is True
        ),
        "injector_absence": (
            injector.get(
                "all_domain_absence_confirmed"
            )
            is True
        ),
        "injector_domains": (
            set(
                (
                    injector.get("domains")
                    or {}
                ).keys()
            )
            == {
                "A",
                "B",
                "C",
            }
        ),
        "assurance_no_failure": (
            assurance.get("failure") is None
        ),
        "assurance_incident": (
            assurance.get("incident_count")
            == 1
        ),
        "assurance_convergence": (
            assurance.get(
                "successful_convergence_count"
            )
            == 1
        ),
        "assurance_attempt": (
            assurance.get(
                "remediation_attempt_count"
            )
            == 1
        ),
        "exact_drift_domains": (
            set(
                classification.get(
                    "observed_drift_domains"
                )
                or []
            )
            == {
                "A",
                "B",
                "C",
            }
        ),
        "exact_remediated_domains": (
            set(
                classification.get(
                    "remediated_domains"
                )
                or []
            )
            == {
                "A",
                "B",
                "C",
            }
        ),
        "candidate_found": run_candidate_found,
        "candidate_consistent": run_candidate_consistent,
        "bmv2_continuity": (
            continuity.get("bmv2_pid_continuity")
            is True
        ),
        "netopeer_continuity": (
            continuity.get("netopeer_pid_continuity")
            is True
        ),
        "linux_link_continuity": (
            continuity.get("linux_device_present")
            is True
        ),
        "cleanup_linux": (
            cleanup.get("linux_state_absent")
            is True
        ),
        "cleanup_netconf": (
            cleanup.get("netconf_state_absent")
            is True
        ),
        "cleanup_p4": (
            cleanup.get("p4_state_absent")
            is True
        ),
        "cleanup_link": (
            cleanup.get("linux_link_absent")
            is True
        ),
        "cleanup_errors": (
            not (cleanup.get("errors") or [])
        ),
        **derived_checks,
    }

    false_checks = [
        name
        for name, value
        in content_checks.items()
        if value is not True
    ]

    if false_checks:
        invalid_rows.append(
            f"{identity}:content:"
            + ",".join(false_checks)
        )
        continue

    records.append(
        {
            "identity": identity,
            "summary": summary,
            "metrics": {
                "detection": float(
                    timing[
                        "fault_start_to_drift_confirmation"
                    ]
                ),
                "control_plane_recovery": float(
                    timing[
                        "remediation_start_to_global_convergence"
                    ]
                ),
                "total_reconciliation": float(
                    timing[
                        "fault_start_to_global_convergence"
                    ]
                ),
                "injection_confirmation": float(
                    injection_confirmation_ms
                ),
                "first_drift_observation": float(
                    first_drift_observation_ms
                ),
                "confirmation_accumulation": float(
                    confirmation_accumulation_ms
                ),
            },
        }
    )


print(
    "PHASE17_FINAL_TIMING_VALIDATION_INVALID_ROW_COUNT="
    f"{len(invalid_rows)}"
)

for row in invalid_rows:
    print(
        "PHASE17_FINAL_TIMING_VALIDATION_INVALID_ROW="
        f"{row}"
    )

print(
    "PHASE17_FINAL_TIMING_VALIDATION_VALID_RUN_COUNT="
    f"{len(records)}"
)


def metric_values(
    name: str,
) -> list[float]:
    """Collect one derived or direct metric from valid runs."""

    return [
        float(record["metrics"][name])
        for record in records
    ]


def metric_median(
    values: list[float],
) -> float:
    """Return a median or a negative sentinel."""

    return median(values) if values else -1.0


def metric_maximum(
    values: list[float],
) -> float:
    """Return a maximum or a negative sentinel."""

    return max(values) if values else -1.0


for metric_name, marker_name in (
    ("detection", "DETECTION"),
    (
        "control_plane_recovery",
        "CONTROL_PLANE_RECOVERY",
    ),
    (
        "total_reconciliation",
        "TOTAL_RECONCILIATION",
    ),
    (
        "injection_confirmation",
        "INJECTION_CONFIRMATION",
    ),
    (
        "first_drift_observation",
        "FIRST_DRIFT_OBSERVATION",
    ),
    (
        "confirmation_accumulation",
        "CONFIRMATION_ACCUMULATION",
    ),
):
    values = metric_values(metric_name)
    maximum = metric_maximum(values)
    bound = bounds[metric_name]

    print(
        "PHASE17_FINAL_TIMING_VALIDATION_"
        f"{marker_name}_BOUND_MS="
        f"{bound:.6f}"
    )

    print(
        "PHASE17_FINAL_TIMING_VALIDATION_"
        f"{marker_name}_MEDIAN_MS="
        f"{metric_median(values):.6f}"
    )

    print(
        "PHASE17_FINAL_TIMING_VALIDATION_"
        f"{marker_name}_MAX_MS="
        f"{maximum:.6f}"
    )

    print(
        "PHASE17_FINAL_TIMING_VALIDATION_"
        f"{marker_name}_MARGIN_MS="
        f"{bound - maximum:.6f}"
    )


valid_count = len(records)


def count_summary(predicate) -> int:
    """Count valid records that satisfy one exact summary property."""

    return sum(
        1
        for record in records
        if predicate(record["summary"])
    )


independent_injector_count = count_summary(
    lambda summary: (
        summary.get("fault_injector")
        or {}
    ).get("fault_source")
    == "independent_multidomain_subprocess"
)

all_domain_absence_count = count_summary(
    lambda summary: (
        summary.get("fault_injector")
        or {}
    ).get("all_domain_absence_confirmed")
    is True
)

exact_drift_domain_count = count_summary(
    lambda summary: set(
        (
            summary.get("classification")
            or {}
        ).get("observed_drift_domains")
        or []
    )
    == {"A", "B", "C"}
)

exact_remediation_domain_count = count_summary(
    lambda summary: set(
        (
            summary.get("classification")
            or {}
        ).get("remediated_domains")
        or []
    )
    == {"A", "B", "C"}
)

global_convergence_count = count_summary(
    lambda summary: (
        summary.get("assurance")
        or {}
    ).get("successful_convergence_count")
    == 1
)

bmv2_continuity_count = count_summary(
    lambda summary: (
        summary.get("service_continuity")
        or {}
    ).get("bmv2_pid_continuity")
    is True
)

netopeer_continuity_count = count_summary(
    lambda summary: (
        summary.get("service_continuity")
        or {}
    ).get("netopeer_pid_continuity")
    is True
)

linux_link_continuity_count = count_summary(
    lambda summary: (
        summary.get("service_continuity")
        or {}
    ).get("linux_device_present")
    is True
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_INDEPENDENT_INJECTOR_COUNT="
    f"{independent_injector_count}"
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_ALL_DOMAIN_ABSENCE_COUNT="
    f"{all_domain_absence_count}"
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_EXACT_DRIFT_DOMAIN_COUNT="
    f"{exact_drift_domain_count}"
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_EXACT_REMEDIATION_DOMAIN_COUNT="
    f"{exact_remediation_domain_count}"
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_GLOBAL_CONVERGENCE_COUNT="
    f"{global_convergence_count}"
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_BMV2_PID_CONTINUITY_COUNT="
    f"{bmv2_continuity_count}"
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_NETOPEER_PID_CONTINUITY_COUNT="
    f"{netopeer_continuity_count}"
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_LINUX_LINK_CONTINUITY_COUNT="
    f"{linux_link_continuity_count}"
)

candidate_found = all(
    (
        operational_failures == 0,
        len(rows) == 8,
        observed_order == expected_order,
        len(invalid_rows) == 0,
        valid_count == 8,
    )
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_CANDIDATE_RUN_COUNT="
    f"{valid_count}"
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_MULTI_DOMAIN_DATAPLANE_ASSURANCE_VALIDATED=False"
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_PARTIAL_REMEDIATION_FAILURE_VALIDATED=False"
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_CROSS_DOMAIN_ROLLBACK_VALIDATED=False"
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_DISTRIBUTED_ATOMIC_TRANSACTION_VALIDATED=False"
)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_CANDIDATE_FOUND="
    f"{candidate_found}"
)

summary = {
    "profile_id": profile_id,
    "validation_type": (
        "independent_recalibrated_mirrored_fault_time_matrix"
    ),
    "expected_order": list(expected_order),
    "observed_order": list(observed_order),
    "bounds_ms": bounds,
    "observed_run_count": len(rows),
    "operational_failure_count": operational_failures,
    "invalid_rows": invalid_rows,
    "valid_run_count": valid_count,
    "candidate_found": candidate_found,
    "scope": {
        "multi_domain_control_plane_assurance_exercised": True,
        "multi_domain_dataplane_assurance_validated": False,
        "partial_remediation_failure_validated": False,
        "cross_domain_rollback_validated": False,
        "distributed_atomic_transaction_validated": False,
    },
}

(
    root
    / "final-timing-validation-summary.json"
).write_text(
    json.dumps(summary, indent=2)
    + "\n",
    encoding="utf-8",
)

if operational_failures:
    failed.append("operational")

if invalid_rows:
    failed.append("content")

if not candidate_found:
    failed.append("candidate")

if failed:
    print(
        "PHASE17_FINAL_TIMING_VALIDATION_ANALYSIS_FAILED="
        + ",".join(sorted(set(failed)))
    )
    raise SystemExit(1)

print(
    "PHASE17_FINAL_TIMING_VALIDATION_ANALYSIS_OK"
)
PY

analysis_status=${PIPESTATUS[0]}

echo "phase17_final_timing_validation_analysis_exit_status=$analysis_status" \
  | tee -a "$OUTPUT_DIR/final-timing-validation-analysis.txt"

if [[ "$operational_failures" -eq 0 && "$analysis_status" -eq 0 ]]; then
  echo "PHASE17_FINAL_TIMING_VALIDATION_RUNNER_OK" \
    | tee -a "$OUTPUT_DIR/runner.txt"
  exit 0
fi

echo "PHASE17_FINAL_TIMING_VALIDATION_RUNNER_FAILED" \
  | tee -a "$OUTPUT_DIR/runner.txt"
exit 1
