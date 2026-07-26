#!/usr/bin/env bash
set -uo pipefail

# Validate Phase 18 selective multidomain assurance across a mirrored
# twelve-run matrix. Functional correctness is enforced, while the certified
# Phase 17 timing bounds remain observe-only and are not recalibrated here.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$REPO_DIR/results/S2/multidomain-selective-assurance-validation-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_TIMEOUT_S="${PHASE18_VALIDATION_RUN_TIMEOUT_S:-300}"

mkdir -p "$OUTPUT_DIR/runs"
MANIFEST="$OUTPUT_DIR/manifest.tsv"
PREFLIGHT="$OUTPUT_DIR/preflight.txt"
ANALYSIS="$OUTPUT_DIR/selective-validation-analysis.txt"
SUMMARY="$OUTPUT_DIR/selective-validation-summary.json"
RUNNER="$OUTPUT_DIR/runner.txt"

printf 'order_index\tcondition_id\tcondition_repetition\tfault_domains\tsynthetic_domain\tsynthetic_count\tfault_after_s\tno_fault_observation_s\texit_status\toutput_dir\n' > "$MANIFEST"

{
  echo "validation_profile_id=phase18-s2-multidomain-selective-assurance-validation-v1"
  echo "validation_type=mirrored_selective_fault_matrix"
  echo "expected_run_count=12"
  echo "mirrored_order=A,B,C,D,E,F,F,E,D,C,B,A"
  echo "timing_candidate_policy=observe-only"
  echo "run_timeout_s=$RUN_TIMEOUT_S"
  echo "branch=$(git -C "$REPO_DIR" branch --show-current)"
  echo "head=$(git -C "$REPO_DIR" rev-parse HEAD)"
  echo "origin_develop=$(git -C "$REPO_DIR" rev-parse origin/develop 2>/dev/null || true)"
  echo
  pgrep -af simple_switch_grpc || true
  pgrep -af netopeer2-server || true
  echo
  ss -H -ltn | grep -E ':(830|9559)\b' || true
} > "$PREFLIGHT"

# The fault delay is mirrored with the condition order to reduce systematic
# ordering effects. No-fault controls use the explicit observation duration.
conditions=$(cat <<'MATRIX'
1|A|1|||0|3|4
2|B|1|A||0|4|4
3|C|1|B||0|5|4
4|D|1|C||0|6|4
5|E|1|A,B,C||0|7|4
6|F|1|A,B,C|B|1|8|4
7|F|2|A,B,C|B|1|8|4
8|E|2|A,B,C||0|7|4
9|D|2|C||0|6|4
10|C|2|B||0|5|4
11|B|2|A||0|4|4
12|A|2|||0|3|4
MATRIX
)

operational_failures=0

while IFS='|' read -r order_index condition_id condition_repetition fault_domains synthetic_domain synthetic_count fault_after_s no_fault_observation_s; do
  identity="order${order_index}-${condition_id}-rep${condition_repetition}"
  run_dir="$OUTPUT_DIR/$identity"
  run_log="$OUTPUT_DIR/runs/$identity.log"
  mkdir -p "$run_dir"

  echo "PHASE18_VALIDATION_RUN_BEGIN=$identity"

  timeout "$RUN_TIMEOUT_S" \
  env \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_OUTPUT_DIR="$run_dir" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_CONDITION_ID="$condition_id" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_PROFILE_ID="phase18-s2-multidomain-selective-assurance-validation-v1" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_FAULT_DOMAINS="$fault_domains" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_SYNTHETIC_REJECTION_DOMAIN="$synthetic_domain" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_SYNTHETIC_REJECTION_COUNT="$synthetic_count" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_TIMING_CANDIDATE_POLICY="observe-only" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_FAULT_AFTER_S="$fault_after_s" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_NO_FAULT_OBSERVATION_S="$no_fault_observation_s" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_INJECTOR_COMPLETION_TIMEOUT_S="${PHASE18_VALIDATION_INJECTOR_COMPLETION_TIMEOUT_S:-12}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_RECOVERY_TIMEOUT_S="${PHASE18_VALIDATION_RECOVERY_TIMEOUT_S:-18}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_POLL_INTERVAL_S="${PHASE18_VALIDATION_POLL_INTERVAL_S:-0.05}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_DRIFT_CONFIRMATIONS="${PHASE18_VALIDATION_DRIFT_CONFIRMATIONS:-3}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_CONVERGENCE_CONFIRMATIONS="${PHASE18_VALIDATION_CONVERGENCE_CONFIRMATIONS:-2}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_REMEDIATION_ATTEMPTS="${PHASE18_VALIDATION_MAX_REMEDIATION_ATTEMPTS:-3}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_INITIAL_BACKOFF_S="${PHASE18_VALIDATION_INITIAL_BACKOFF_S:-0.05}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_BACKOFF_MULTIPLIER="${PHASE18_VALIDATION_BACKOFF_MULTIPLIER:-2}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_BACKOFF_S="${PHASE18_VALIDATION_MAX_BACKOFF_S:-0.5}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_DETECTION_MS="${PHASE18_VALIDATION_MAX_DETECTION_MS:-4000}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_CONTROL_PLANE_RECOVERY_MS="${PHASE18_VALIDATION_MAX_CONTROL_PLANE_RECOVERY_MS:-3750}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_TOTAL_RECONCILIATION_MS="${PHASE18_VALIDATION_MAX_TOTAL_RECONCILIATION_MS:-6250}" \
    "$REPO_DIR/setup_all.sh" run_s2_multidomain_selective_assurance \
    > "$run_log" 2>&1

  status=$?
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$order_index" \
    "$condition_id" \
    "$condition_repetition" \
    "$fault_domains" \
    "$synthetic_domain" \
    "$synthetic_count" \
    "$fault_after_s" \
    "$no_fault_observation_s" \
    "$status" \
    "$run_dir" \
    >> "$MANIFEST"

  echo "PHASE18_VALIDATION_RUN_END=$identity:status$status"
  if [[ "$status" -ne 0 ]]; then
    operational_failures=$((operational_failures + 1))
  fi
done <<< "$conditions"

export PHASE18_VALIDATION_OUTPUT_DIR="$OUTPUT_DIR"
export PHASE18_VALIDATION_OPERATIONAL_FAILURES="$operational_failures"

PYTHONDONTWRITEBYTECODE=1 \
python3 - <<'PY' \
  | tee "$ANALYSIS"
"""Analyze the mirrored Phase 18 selective-assurance validation matrix."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any


root = Path(os.environ["PHASE18_VALIDATION_OUTPUT_DIR"])
operational_failures = int(
    os.environ["PHASE18_VALIDATION_OPERATIONAL_FAILURES"]
)
manifest_path = root / "manifest.tsv"
summary_path = root / "selective-validation-summary.json"

with manifest_path.open(
    "r",
    encoding="utf-8",
    newline="",
) as handle:
    rows = list(csv.DictReader(handle, delimiter="\t"))

expected_order = [
    "A",
    "B",
    "C",
    "D",
    "E",
    "F",
    "F",
    "E",
    "D",
    "C",
    "B",
    "A",
]

expected_faults = {
    "A": set(),
    "B": {"A"},
    "C": {"B"},
    "D": {"C"},
    "E": {"A", "B", "C"},
    "F": {"A", "B", "C"},
}

expected_attempt_counts = {
    "A": {"A": 0, "B": 0, "C": 0},
    "B": {"A": 1, "B": 0, "C": 0},
    "C": {"A": 0, "B": 1, "C": 0},
    "D": {"A": 0, "B": 0, "C": 1},
    "E": {"A": 1, "B": 1, "C": 1},
    "F": {"A": 1, "B": 2, "C": 1},
}

expected_backend_counts = {
    "A": {"A": 0, "B": 0, "C": 0},
    "B": {"A": 1, "B": 0, "C": 0},
    "C": {"A": 0, "B": 1, "C": 0},
    "D": {"A": 0, "B": 0, "C": 1},
    "E": {"A": 1, "B": 1, "C": 1},
    "F": {"A": 1, "B": 1, "C": 1},
}

expected_synthetic_counts = {
    "A": {"A": 0, "B": 0, "C": 0},
    "B": {"A": 0, "B": 0, "C": 0},
    "C": {"A": 0, "B": 0, "C": 0},
    "D": {"A": 0, "B": 0, "C": 0},
    "E": {"A": 0, "B": 0, "C": 0},
    "F": {"A": 0, "B": 1, "C": 0},
}

expected_sequences = {
    "A": [],
    "B": [["A"]],
    "C": [["B"]],
    "D": [["C"]],
    "E": [["A", "B", "C"]],
    "F": [["A", "B", "C"], ["B"]],
}

records: list[dict[str, Any]] = []
invalid_rows: list[str] = []
content_failures: list[str] = []

for row in rows:
    identity = (
        f"order{row['order_index']}-"
        f"{row['condition_id']}-"
        f"rep{row['condition_repetition']}"
    )
    run_dir = Path(row["output_dir"])
    summary_file = run_dir / "summary.json"
    log_file = root / "runs" / f"{identity}.log"
    status = int(row["exit_status"])

    if status != 0 or not summary_file.is_file() or not log_file.is_file():
        invalid_rows.append(f"{identity}:status_or_artifact")
        continue

    try:
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
    except Exception as exc:
        invalid_rows.append(
            f"{identity}:json:{type(exc).__name__}"
        )
        continue

    records.append(
        {
            "identity": identity,
            "condition_id": row["condition_id"],
            "condition_repetition": int(row["condition_repetition"]),
            "summary": summary,
            "log_file": log_file,
        }
    )

for record in records:
    identity = record["identity"]
    condition_id = record["condition_id"]
    summary = record["summary"]
    log_file = record["log_file"]

    configuration = summary.get("configuration") or {}
    classification = summary.get("classification") or {}
    injector = summary.get("fault_injector") or {}
    assurance = summary.get("assurance") or {}
    adapter = summary.get("multidomain_adapter") or {}
    continuity = summary.get("service_continuity") or {}
    scope = summary.get("scope") or {}
    cleanup = summary.get("cleanup") or {}
    operational_checks = summary.get("operational_checks") or {}

    expected_domain_set = expected_faults[condition_id]
    candidate_checks = classification.get("candidate_checks") or {}
    events = assurance.get("events") or []
    backoff_count = sum(
        event.get("event_type") == "remediation_backoff_started"
        for event in events
    )

    checks = {
        "scenario": (
            summary.get("scenario")
            == "S2_MAD_multidomain_selective_assurance"
        ),
        "profile": (
            summary.get("assurance_profile_id")
            == "phase18-s2-multidomain-selective-assurance-validation-v1"
        ),
        "condition": summary.get("condition_id") == condition_id,
        "passed": summary.get("passed") is True,
        "cleanup_ok": summary.get("cleanup_ok") is True,
        "candidate": classification.get("candidate_found") is True,
        "candidate_checks": (
            bool(candidate_checks)
            and all(value is True for value in candidate_checks.values())
        ),
        "timing_observe_only": (
            configuration.get("timing_candidate_policy") == "observe-only"
        ),
        "expected_fault_domains": (
            set(classification.get("expected_fault_domains") or [])
            == expected_domain_set
        ),
        "observed_fault_domains": (
            set(classification.get("observed_drift_domains") or [])
            == expected_domain_set
        ),
        "remediated_domains": (
            set(classification.get("remediated_domains") or [])
            == expected_domain_set
        ),
        "request_sequence": (
            classification.get("remediation_request_sequence")
            == expected_sequences[condition_id]
        ),
        "attempt_counts": (
            (adapter.get("domain_remediation_counts") or {})
            == expected_attempt_counts[condition_id]
        ),
        "backend_counts": (
            (adapter.get("domain_backend_remediation_counts") or {})
            == expected_backend_counts[condition_id]
        ),
        "synthetic_counts": (
            (adapter.get("domain_synthetic_rejection_counts") or {})
            == expected_synthetic_counts[condition_id]
        ),
        "injector_selected": (
            set(injector.get("selected_fault_domains") or [])
            == expected_domain_set
        ),
        "selected_writes": (
            injector.get("all_selected_writes_accepted") is True
        ),
        "selected_absence": (
            injector.get("all_selected_absence_confirmed") is True
        ),
        "unaffected_presence": (
            injector.get("all_unaffected_presence_confirmed") is True
        ),
        "service_continuity": all(
            (
                continuity.get("bmv2_pid_continuity") is True,
                continuity.get("netopeer_pid_continuity") is True,
                continuity.get("linux_device_present") is True,
                continuity.get("p4runtime_available") is True,
                continuity.get("netconf_available") is True,
            )
        ),
        "operational_checks": (
            bool(operational_checks)
            and all(value is True for value in operational_checks.values())
        ),
        "cleanup": all(
            (
                cleanup.get("linux_state_absent") is True,
                cleanup.get("netconf_state_absent") is True,
                cleanup.get("p4_state_absent") is True,
                cleanup.get("linux_link_absent") is True,
                not (cleanup.get("errors") or []),
            )
        ),
        "no_real_backend_failure_claim": (
            scope.get(
                "real_backend_partial_remediation_failure_validated"
            )
            is False
        ),
        "no_dataplane_claim": (
            scope.get("multi_domain_dataplane_assurance_validated")
            is False
        ),
        "no_rollback_claim": (
            scope.get("cross_domain_rollback_validated") is False
        ),
        "no_atomicity_claim": (
            scope.get("distributed_atomic_transaction_validated")
            is False
        ),
        "run_ok_marker": (
            "PHASE18_SELECTIVE_ASSURANCE_RUN_OK"
            in log_file.read_text(encoding="utf-8", errors="replace")
        ),
    }

    if condition_id == "A":
        checks.update(
            {
                "zero_incidents": assurance.get("incident_count") == 0,
                "zero_attempts": (
                    assurance.get("remediation_attempt_count") == 0
                ),
                "zero_convergences": (
                    assurance.get("successful_convergence_count") == 0
                ),
                "no_preservation_retry": (
                    classification.get("preservation_observation") is None
                ),
            }
        )
    else:
        checks.update(
            {
                "one_incident": assurance.get("incident_count") == 1,
                "one_convergence": (
                    assurance.get("successful_convergence_count") == 1
                ),
                "no_controller_failure": assurance.get("failure") is None,
            }
        )

    if condition_id == "F":
        checks.update(
            {
                "two_attempts": (
                    assurance.get("remediation_attempt_count") == 2
                ),
                "one_backoff": backoff_count == 1,
                "preservation_observed": (
                    classification.get("preservation_observation")
                    is not None
                ),
            }
        )
    elif condition_id != "A":
        checks.update(
            {
                "one_attempt": (
                    assurance.get("remediation_attempt_count") == 1
                ),
                "zero_backoff": backoff_count == 0,
                "no_preservation_retry": (
                    classification.get("preservation_observation") is None
                ),
            }
        )

    failed_checks = [
        name
        for name, accepted in checks.items()
        if not accepted
    ]

    print(
        "PHASE18_VALIDATION_RUN_FAILED_CHECK_COUNT="
        f"{identity}:{len(failed_checks)}"
    )

    for name in failed_checks:
        print(
            "PHASE18_VALIDATION_RUN_FAILED_CHECK="
            f"{identity}:{name}"
        )

    if failed_checks:
        content_failures.append(
            f"{identity}:" + ",".join(failed_checks)
        )

observed_order = [row["condition_id"] for row in rows]
mirrored_order_ok = observed_order == expected_order

condition_counts = {
    condition: sum(
        record["condition_id"] == condition
        for record in records
    )
    for condition in expected_faults
}

no_false_positive_count = sum(
    record["condition_id"] == "A"
    and (record["summary"].get("assurance") or {}).get("incident_count") == 0
    and (record["summary"].get("assurance") or {}).get(
        "remediation_attempt_count"
    )
    == 0
    for record in records
)

selective_count = sum(
    record["condition_id"] in {"B", "C", "D"}
    for record in records
)

simultaneous_count = sum(
    record["condition_id"] in {"E", "F"}
    for record in records
)

synthetic_retry_count = sum(
    record["condition_id"] == "F"
    and (record["summary"].get("assurance") or {}).get(
        "remediation_attempt_count"
    )
    == 2
    and (
        record["summary"].get("classification") or {}
    ).get("remediation_request_sequence")
    == [["A", "B", "C"], ["B"]]
    for record in records
)

service_continuity_count = sum(
    all(
        (
            (
                record["summary"].get("service_continuity") or {}
            ).get("bmv2_pid_continuity")
            is True,
            (
                record["summary"].get("service_continuity") or {}
            ).get("netopeer_pid_continuity")
            is True,
            (
                record["summary"].get("service_continuity") or {}
            ).get("linux_device_present")
            is True,
        )
    )
    for record in records
)

candidate_run_count = sum(
    (record["summary"].get("classification") or {}).get(
        "candidate_found"
    )
    is True
    for record in records
)

valid_run_count = len(records) - len(content_failures)
operationally_valid = all(
    (
        operational_failures == 0,
        len(rows) == 12,
        mirrored_order_ok,
        not invalid_rows,
        not content_failures,
        valid_run_count == 12,
        candidate_run_count == 12,
        no_false_positive_count == 2,
        selective_count == 6,
        simultaneous_count == 4,
        synthetic_retry_count == 2,
        service_continuity_count == 12,
        all(count == 2 for count in condition_counts.values()),
    )
)

aggregate = {
    "condition_order": observed_order,
    "mirrored_order_ok": mirrored_order_ok,
    "observed_run_count": len(rows),
    "valid_run_count": valid_run_count,
    "candidate_run_count": candidate_run_count,
    "condition_counts": condition_counts,
    "no_false_positive_count": no_false_positive_count,
    "selective_count": selective_count,
    "simultaneous_count": simultaneous_count,
    "synthetic_retry_count": synthetic_retry_count,
    "service_continuity_count": service_continuity_count,
    "invalid_rows": invalid_rows,
    "content_failures": content_failures,
    "operational_failure_count": operational_failures,
    "operationally_valid": operationally_valid,
    "scope": {
        "multidomain_control_plane_selectivity_validated": operationally_valid,
        "no_false_positive_control_validated": operationally_valid,
        "synthetic_selective_retry_validated": operationally_valid,
        "real_backend_partial_remediation_failure_validated": False,
        "multi_domain_dataplane_assurance_validated": False,
        "cross_domain_rollback_validated": False,
        "distributed_atomic_transaction_validated": False,
    },
}

summary_path.write_text(
    json.dumps(aggregate, indent=2) + "\n",
    encoding="utf-8",
)

print(
    "PHASE18_VALIDATION_OBSERVED_RUN_COUNT="
    f"{len(rows)}"
)
print(
    "PHASE18_VALIDATION_MIRRORED_ORDER_OK="
    f"{mirrored_order_ok}"
)
print(
    "PHASE18_VALIDATION_INVALID_ROW_COUNT="
    f"{len(invalid_rows)}"
)
for value in invalid_rows:
    print(f"PHASE18_VALIDATION_INVALID_ROW={value}")
print(
    "PHASE18_VALIDATION_CONTENT_FAILURE_COUNT="
    f"{len(content_failures)}"
)
for value in content_failures:
    print(f"PHASE18_VALIDATION_CONTENT_FAILURE={value}")
print(f"PHASE18_VALIDATION_VALID_RUN_COUNT={valid_run_count}")
print(
    "PHASE18_VALIDATION_CANDIDATE_RUN_COUNT="
    f"{candidate_run_count}"
)
print(
    "PHASE18_VALIDATION_NO_FALSE_POSITIVE_CONTROL_COUNT="
    f"{no_false_positive_count}"
)
print(
    "PHASE18_VALIDATION_SELECTIVE_DOMAIN_COUNT="
    f"{selective_count}"
)
print(
    "PHASE18_VALIDATION_SIMULTANEOUS_DOMAIN_COUNT="
    f"{simultaneous_count}"
)
print(
    "PHASE18_VALIDATION_SYNTHETIC_RETRY_COUNT="
    f"{synthetic_retry_count}"
)
print(
    "PHASE18_VALIDATION_SERVICE_CONTINUITY_COUNT="
    f"{service_continuity_count}"
)
for condition, count in sorted(condition_counts.items()):
    print(
        "PHASE18_VALIDATION_CONDITION_COUNT="
        f"{condition}:{count}"
    )
print(
    "PHASE18_VALIDATION_REAL_BACKEND_PARTIAL_FAILURE_VALIDATED=False"
)
print(
    "PHASE18_VALIDATION_MULTI_DOMAIN_DATAPLANE_ASSURANCE_VALIDATED=False"
)
print(
    "PHASE18_VALIDATION_CROSS_DOMAIN_ROLLBACK_VALIDATED=False"
)
print(
    "PHASE18_VALIDATION_DISTRIBUTED_ATOMIC_TRANSACTION_VALIDATED=False"
)
print(
    "PHASE18_VALIDATION_OPERATIONAL_FAILURE_COUNT="
    f"{operational_failures}"
)

if not operationally_valid:
    print("PHASE18_VALIDATION_ANALYSIS_FAILED")
    raise SystemExit(1)

print("PHASE18_VALIDATION_ANALYSIS_OK")
PY

analysis_status=${PIPESTATUS[0]}

{
  echo "PHASE18_VALIDATION_OPERATIONAL_FAILURE_COUNT=$operational_failures"
  echo "phase18_validation_analysis_exit_status=$analysis_status"

  if [[ "$operational_failures" -eq 0 && "$analysis_status" -eq 0 ]]; then
    echo "PHASE18_VALIDATION_RUNNER_OK"
  else
    echo "PHASE18_VALIDATION_RUNNER_FAILED"
  fi
} | tee "$RUNNER"

if [[ "$operational_failures" -ne 0 || "$analysis_status" -ne 0 ]]; then
  exit 1
fi

exit 0
