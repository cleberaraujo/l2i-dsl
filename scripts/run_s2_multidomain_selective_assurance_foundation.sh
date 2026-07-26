#!/usr/bin/env bash
set -uo pipefail

# Exercise every Phase 18 foundation condition once. The runner keeps shell
# execution status, operational validity, and scientific candidate checks as
# separate evidence dimensions.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$REPO_DIR/results/S2/multidomain-selective-assurance-foundation-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_TIMEOUT_S="${PHASE18_FOUNDATION_RUN_TIMEOUT_S:-300}"

mkdir -p "$OUTPUT_DIR/runs"
MANIFEST="$OUTPUT_DIR/manifest.tsv"
PREFLIGHT="$OUTPUT_DIR/preflight.txt"
ANALYSIS="$OUTPUT_DIR/foundation-analysis.txt"
SUMMARY="$OUTPUT_DIR/foundation-summary.json"
RUNNER="$OUTPUT_DIR/runner.txt"

printf 'order_index\tcondition_id\tfault_domains\tsynthetic_domain\tsynthetic_count\texit_status\toutput_dir\n' > "$MANIFEST"

{
  echo "validation_profile_id=phase18-s2-multidomain-selective-assurance-foundation-v1"
  echo "expected_condition_order=A,B,C,D,E,F"
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

conditions=$(cat <<'EOF'
1|A|||0
2|B|A||0
3|C|B||0
4|D|C||0
5|E|A,B,C||0
6|F|A,B,C|B|1
EOF
)

operational_failures=0

while IFS='|' read -r order_index condition_id fault_domains synthetic_domain synthetic_count; do
  run_dir="$OUTPUT_DIR/condition-${condition_id}"
  run_log="$OUTPUT_DIR/runs/condition-${condition_id}.log"
  mkdir -p "$run_dir"

  echo "[Phase18-foundation] condition=$condition_id fault_domains=${fault_domains:-none} synthetic_domain=${synthetic_domain:-none} output=$run_dir"

  timeout "$RUN_TIMEOUT_S" \
  env \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_OUTPUT_DIR="$run_dir" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_CONDITION_ID="$condition_id" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_PROFILE_ID="phase18-s2-multidomain-selective-assurance-foundation-v1" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_FAULT_DOMAINS="$fault_domains" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_SYNTHETIC_REJECTION_DOMAIN="$synthetic_domain" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_SYNTHETIC_REJECTION_COUNT="$synthetic_count" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_TIMING_CANDIDATE_POLICY="observe-only" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_FAULT_AFTER_S="${PHASE18_FOUNDATION_FAULT_AFTER_S:-4}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_NO_FAULT_OBSERVATION_S="${PHASE18_FOUNDATION_NO_FAULT_OBSERVATION_S:-3}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_INJECTOR_COMPLETION_TIMEOUT_S="${PHASE18_FOUNDATION_INJECTOR_COMPLETION_TIMEOUT_S:-12}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_RECOVERY_TIMEOUT_S="${PHASE18_FOUNDATION_RECOVERY_TIMEOUT_S:-18}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_POLL_INTERVAL_S="${PHASE18_FOUNDATION_POLL_INTERVAL_S:-0.05}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_DRIFT_CONFIRMATIONS="${PHASE18_FOUNDATION_DRIFT_CONFIRMATIONS:-3}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_CONVERGENCE_CONFIRMATIONS="${PHASE18_FOUNDATION_CONVERGENCE_CONFIRMATIONS:-2}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_REMEDIATION_ATTEMPTS="${PHASE18_FOUNDATION_MAX_REMEDIATION_ATTEMPTS:-3}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_INITIAL_BACKOFF_S="${PHASE18_FOUNDATION_INITIAL_BACKOFF_S:-0.05}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_BACKOFF_MULTIPLIER="${PHASE18_FOUNDATION_BACKOFF_MULTIPLIER:-2}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_BACKOFF_S="${PHASE18_FOUNDATION_MAX_BACKOFF_S:-0.5}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_DETECTION_MS="${PHASE18_FOUNDATION_MAX_DETECTION_MS:-4000}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_CONTROL_PLANE_RECOVERY_MS="${PHASE18_FOUNDATION_MAX_CONTROL_PLANE_RECOVERY_MS:-3750}" \
    S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_TOTAL_RECONCILIATION_MS="${PHASE18_FOUNDATION_MAX_TOTAL_RECONCILIATION_MS:-6250}" \
    "$REPO_DIR/setup_all.sh" run_s2_multidomain_selective_assurance \
    > "$run_log" 2>&1

  status=$?
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$order_index" \
    "$condition_id" \
    "$fault_domains" \
    "$synthetic_domain" \
    "$synthetic_count" \
    "$status" \
    "$run_dir" \
    >> "$MANIFEST"

  echo "PHASE18_FOUNDATION_RUN_END=condition-${condition_id}:status${status}"
  if [[ "$status" -ne 0 ]]; then
    operational_failures=$((operational_failures + 1))
  fi
done <<< "$conditions"

export PHASE18_FOUNDATION_OUTPUT_DIR="$OUTPUT_DIR"
export PHASE18_FOUNDATION_OPERATIONAL_FAILURES="$operational_failures"

PYTHONDONTWRITEBYTECODE=1 \
python3 - <<'PY' \
  | tee "$ANALYSIS"
"""Analyze the six-condition Phase 18 selective-assurance foundation."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any


root = Path(os.environ["PHASE18_FOUNDATION_OUTPUT_DIR"])
operational_failures = int(
    os.environ["PHASE18_FOUNDATION_OPERATIONAL_FAILURES"]
)
manifest_path = root / "manifest.tsv"
summary_path = root / "foundation-summary.json"

with manifest_path.open(
    "r",
    encoding="utf-8",
    newline="",
) as handle:
    rows = list(csv.DictReader(handle, delimiter="\t"))

expected = {
    "A": {
        "fault_domains": set(),
        "attempts": {"A": 0, "B": 0, "C": 0},
        "request_sequence": [],
        "synthetic": {"A": 0, "B": 0, "C": 0},
    },
    "B": {
        "fault_domains": {"A"},
        "attempts": {"A": 1, "B": 0, "C": 0},
        "request_sequence": [["A"]],
        "synthetic": {"A": 0, "B": 0, "C": 0},
    },
    "C": {
        "fault_domains": {"B"},
        "attempts": {"A": 0, "B": 1, "C": 0},
        "request_sequence": [["B"]],
        "synthetic": {"A": 0, "B": 0, "C": 0},
    },
    "D": {
        "fault_domains": {"C"},
        "attempts": {"A": 0, "B": 0, "C": 1},
        "request_sequence": [["C"]],
        "synthetic": {"A": 0, "B": 0, "C": 0},
    },
    "E": {
        "fault_domains": {"A", "B", "C"},
        "attempts": {"A": 1, "B": 1, "C": 1},
        "request_sequence": [["A", "B", "C"]],
        "synthetic": {"A": 0, "B": 0, "C": 0},
    },
    "F": {
        "fault_domains": {"A", "B", "C"},
        "attempts": {"A": 1, "B": 2, "C": 1},
        "request_sequence": [["A", "B", "C"], ["B"]],
        "synthetic": {"A": 0, "B": 1, "C": 0},
    },
}

records: dict[str, dict[str, Any]] = {}
invalid_rows: list[str] = []

for row in rows:
    condition_id = row["condition_id"]
    run_dir = Path(row["output_dir"])
    summary_file = run_dir / "summary.json"
    status = int(row["exit_status"])
    if status != 0 or not summary_file.is_file():
        invalid_rows.append(f"{condition_id}:status_or_summary")
        continue
    try:
        summary = json.loads(
            summary_file.read_text(encoding="utf-8")
        )
    except Exception as exc:
        invalid_rows.append(
            f"{condition_id}:json:{type(exc).__name__}"
        )
        continue
    records[condition_id] = summary

content_failures: list[str] = []

for condition_id, specification in expected.items():
    summary = records.get(condition_id)
    if summary is None:
        content_failures.append(f"{condition_id}:missing")
        continue

    configuration = summary.get("configuration") or {}
    classification = summary.get("classification") or {}
    injector = summary.get("fault_injector") or {}
    assurance = summary.get("assurance") or {}
    adapter = summary.get("multidomain_adapter") or {}
    continuity = summary.get("service_continuity") or {}
    scope = summary.get("scope") or {}
    cleanup = summary.get("cleanup") or {}

    fault_domains = set(configuration.get("fault_domains") or [])
    observed_domains = set(
        classification.get("observed_drift_domains") or []
    )
    remediated_domains = set(
        classification.get("remediated_domains") or []
    )
    request_sequence = (
        classification.get("remediation_request_sequence") or []
    )
    attempt_counts = (
        adapter.get("domain_remediation_counts") or {}
    )
    backend_counts = (
        adapter.get("domain_backend_remediation_counts") or {}
    )
    synthetic_counts = (
        adapter.get("domain_synthetic_rejection_counts") or {}
    )

    expected_faults = specification["fault_domains"]
    checks = {
        "scenario": (
            summary.get("scenario")
            == "S2_MAD_multidomain_selective_assurance"
        ),
        "condition_id": summary.get("condition_id") == condition_id,
        "passed": summary.get("passed") is True,
        "cleanup_ok": summary.get("cleanup_ok") is True,
        "candidate_found": (
            classification.get("candidate_found") is True
        ),
        "candidate_checks": (
            bool(classification.get("candidate_checks"))
            and all(
                value is True
                for value in (
                    classification.get("candidate_checks") or {}
                ).values()
            )
        ),
        "fault_domains": fault_domains == expected_faults,
        "injector_selected": (
            set(injector.get("selected_fault_domains") or [])
            == expected_faults
        ),
        "injector_selected_writes": (
            injector.get("all_selected_writes_accepted") is True
        ),
        "injector_selected_absence": (
            injector.get("all_selected_absence_confirmed") is True
        ),
        "injector_unaffected_presence": (
            injector.get("all_unaffected_presence_confirmed") is True
        ),
        "observed_domains": observed_domains == expected_faults,
        "remediated_domains": remediated_domains == expected_faults,
        "request_sequence": (
            request_sequence == specification["request_sequence"]
        ),
        "attempt_counts": all(
            int(attempt_counts.get(domain, 0))
            == specification["attempts"][domain]
            for domain in ("A", "B", "C")
        ),
        "backend_counts": all(
            int(backend_counts.get(domain, 0))
            == (1 if domain in expected_faults else 0)
            for domain in ("A", "B", "C")
        ),
        "synthetic_counts": all(
            int(synthetic_counts.get(domain, 0))
            == specification["synthetic"][domain]
            for domain in ("A", "B", "C")
        ),
        "bmv2_continuity": (
            continuity.get("bmv2_pid_continuity") is True
        ),
        "netopeer_continuity": (
            continuity.get("netopeer_pid_continuity") is True
        ),
        "linux_link_continuity": (
            continuity.get("linux_device_present") is True
        ),
        "cleanup_linux": cleanup.get("linux_state_absent") is True,
        "cleanup_netconf": cleanup.get("netconf_state_absent") is True,
        "cleanup_p4": cleanup.get("p4_state_absent") is True,
        "cleanup_link": cleanup.get("linux_link_absent") is True,
        "scope_no_real_backend_failure": (
            scope.get(
                "real_backend_partial_remediation_failure_validated"
            )
            is False
        ),
        "scope_no_generic_partial_failure_claim": (
            scope.get("partial_remediation_failure_validated") is False
        ),
        "scope_no_rollback": (
            scope.get("cross_domain_rollback_validated") is False
        ),
        "scope_no_atomicity": (
            scope.get("distributed_atomic_transaction_validated")
            is False
        ),
        "scope_no_dataplane": (
            scope.get("multi_domain_dataplane_assurance_validated")
            is False
        ),
    }

    if condition_id == "A":
        checks.update(
            {
                "control_fault_not_performed": (
                    injector.get("fault_performed") is False
                ),
                "control_zero_incidents": (
                    assurance.get("incident_count") == 0
                ),
                "control_zero_attempts": (
                    assurance.get("remediation_attempt_count") == 0
                ),
                "control_no_false_positive": (
                    not observed_domains and not remediated_domains
                ),
            }
        )
    else:
        checks.update(
            {
                "fault_performed": (
                    injector.get("fault_performed") is True
                ),
                "one_incident": assurance.get("incident_count") == 1,
                "one_convergence": (
                    assurance.get("successful_convergence_count") == 1
                ),
            }
        )

    if condition_id == "F":
        backoff_count = sum(
            event.get("event_type") == "remediation_backoff_started"
            for event in assurance.get("events") or []
        )
        checks.update(
            {
                "synthetic_scope": (
                    scope.get(
                        "synthetic_partial_remediation_rejection_exercised"
                    )
                    is True
                ),
                "selective_retry_scope": (
                    scope.get(
                        "selective_retry_after_synthetic_rejection_exercised"
                    )
                    is True
                ),
                "two_attempts": (
                    assurance.get("remediation_attempt_count") == 2
                ),
                "one_backoff": backoff_count == 1,
                "preservation_observation": (
                    classification.get("preservation_observation")
                    is not None
                ),
            }
        )

    failed_checks = [
        name for name, accepted in checks.items() if not accepted
    ]
    print(
        "PHASE18_FOUNDATION_CONDITION_"
        f"{condition_id}_FAILED_CHECK_COUNT={len(failed_checks)}"
    )
    for name in failed_checks:
        print(
            "PHASE18_FOUNDATION_CONDITION_"
            f"{condition_id}_FAILED_CHECK={name}"
        )
    if failed_checks:
        content_failures.append(
            f"{condition_id}:" + ",".join(failed_checks)
        )

condition_order = [row["condition_id"] for row in rows]
order_ok = condition_order == ["A", "B", "C", "D", "E", "F"]
valid_count = len(records)
candidate_count = sum(
    (record.get("classification") or {}).get("candidate_found")
    is True
    for record in records.values()
)
control_count = sum(
    condition_id == "A"
    and (record.get("assurance") or {}).get("incident_count") == 0
    for condition_id, record in records.items()
)
selective_count = sum(
    condition_id in {"B", "C", "D"}
    and set(
        (record.get("classification") or {}).get(
            "observed_drift_domains"
        )
        or []
    )
    == expected[condition_id]["fault_domains"]
    for condition_id, record in records.items()
)
simultaneous_count = sum(
    condition_id in {"E", "F"}
    and set(
        (record.get("classification") or {}).get(
            "observed_drift_domains"
        )
        or []
    )
    == {"A", "B", "C"}
    for condition_id, record in records.items()
)
synthetic_retry_count = sum(
    condition_id == "F"
    and (record.get("classification") or {}).get(
        "preservation_observation"
    )
    is not None
    and (record.get("assurance") or {}).get(
        "remediation_attempt_count"
    )
    == 2
    for condition_id, record in records.items()
)
continuity_count = sum(
    all(
        (
            (record.get("service_continuity") or {}).get(
                "bmv2_pid_continuity"
            )
            is True,
            (record.get("service_continuity") or {}).get(
                "netopeer_pid_continuity"
            )
            is True,
            (record.get("service_continuity") or {}).get(
                "linux_device_present"
            )
            is True,
        )
    )
    for record in records.values()
)

operationally_valid = all(
    (
        operational_failures == 0,
        not invalid_rows,
        not content_failures,
        order_ok,
        valid_count == 6,
        candidate_count == 6,
    )
)

aggregate = {
    "condition_order": condition_order,
    "order_ok": order_ok,
    "valid_run_count": valid_count,
    "candidate_run_count": candidate_count,
    "control_count": control_count,
    "selective_count": selective_count,
    "simultaneous_count": simultaneous_count,
    "synthetic_retry_count": synthetic_retry_count,
    "continuity_count": continuity_count,
    "invalid_rows": invalid_rows,
    "content_failures": content_failures,
    "operationally_valid": operationally_valid,
    "scope": {
        "multidomain_control_plane_selectivity_exercised": True,
        "synthetic_partial_remediation_rejection_exercised": True,
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

print(f"PHASE18_FOUNDATION_OBSERVED_CONDITION_COUNT={len(rows)}")
print(f"PHASE18_FOUNDATION_CONDITION_ORDER_OK={order_ok}")
print(f"PHASE18_FOUNDATION_INVALID_ROW_COUNT={len(invalid_rows)}")
for row in invalid_rows:
    print(f"PHASE18_FOUNDATION_INVALID_ROW={row}")
print(
    "PHASE18_FOUNDATION_CONTENT_FAILURE_COUNT="
    f"{len(content_failures)}"
)
for failure in content_failures:
    print(f"PHASE18_FOUNDATION_CONTENT_FAILURE={failure}")
print(f"PHASE18_FOUNDATION_VALID_RUN_COUNT={valid_count}")
print(f"PHASE18_FOUNDATION_CANDIDATE_RUN_COUNT={candidate_count}")
print(f"PHASE18_FOUNDATION_NO_FALSE_POSITIVE_CONTROL_COUNT={control_count}")
print(f"PHASE18_FOUNDATION_SELECTIVE_DOMAIN_COUNT={selective_count}")
print(f"PHASE18_FOUNDATION_SIMULTANEOUS_DOMAIN_COUNT={simultaneous_count}")
print(f"PHASE18_FOUNDATION_SYNTHETIC_RETRY_COUNT={synthetic_retry_count}")
print(f"PHASE18_FOUNDATION_SERVICE_CONTINUITY_COUNT={continuity_count}")
print("PHASE18_FOUNDATION_REAL_BACKEND_PARTIAL_FAILURE_VALIDATED=False")
print("PHASE18_FOUNDATION_MULTI_DOMAIN_DATAPLANE_ASSURANCE_VALIDATED=False")
print("PHASE18_FOUNDATION_CROSS_DOMAIN_ROLLBACK_VALIDATED=False")
print("PHASE18_FOUNDATION_DISTRIBUTED_ATOMIC_TRANSACTION_VALIDATED=False")

if not operationally_valid:
    print("PHASE18_FOUNDATION_ANALYSIS_FAILED")
    raise SystemExit(1)

print("PHASE18_FOUNDATION_ANALYSIS_OK")
PY
analysis_status=${PIPESTATUS[0]}

{
  echo "PHASE18_FOUNDATION_OPERATIONAL_FAILURE_COUNT=$operational_failures"
  echo "phase18_foundation_analysis_exit_status=$analysis_status"
  if [[ "$operational_failures" -eq 0 && "$analysis_status" -eq 0 ]]; then
    echo "PHASE18_FOUNDATION_RUNNER_OK"
  else
    echo "PHASE18_FOUNDATION_RUNNER_FAILED"
  fi
} | tee "$RUNNER"

if [[ "$operational_failures" -ne 0 || "$analysis_status" -ne 0 ]]; then
  exit 1
fi
exit 0
