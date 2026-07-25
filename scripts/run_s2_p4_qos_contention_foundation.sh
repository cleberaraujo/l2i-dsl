#!/usr/bin/env bash
set -euo pipefail

# Run the first baseline/adapt matrix for multicast QoS under a demonstrably
# shared receiver-B egress. The runner treats scientific candidate selection as
# an analysis result, not as an operational success requirement.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="${1:-}"

if [[ -z "$OUTPUT_DIR" ]]; then
  echo "usage: $0 OUTPUT_DIR" >&2
  exit 2
fi

mkdir -p "$OUTPUT_DIR/runs"
OUTPUT_DIR="$(realpath "$OUTPUT_DIR")"

REPETITIONS="${PHASE14_FOUNDATION_REPETITIONS:-3}"
RUN_TIMEOUT_S="${PHASE14_FOUNDATION_RUN_TIMEOUT_S:-180}"

if ! [[ "$REPETITIONS" =~ ^[1-9][0-9]*$ ]]; then
  echo "PHASE14_FOUNDATION_INVALID_REPETITIONS=$REPETITIONS" >&2
  exit 2
fi

cd "$REPO_DIR"

{
  echo "repository=$REPO_DIR"
  echo "output_dir=$OUTPUT_DIR"
  echo "repetitions=$REPETITIONS"
  echo "capacity_mbps=${PHASE14_CAPACITY_MBPS:-3}"
  echo "multicast_rate_mbps=${PHASE14_MULTICAST_RATE_MBPS:-2}"
  echo "background_rate_mbps=${PHASE14_BACKGROUND_RATE_MBPS:-2}"
  echo "duration_s=${PHASE14_DURATION_S:-3}"
  echo "background_duration_s=${PHASE14_BACKGROUND_DURATION_S:-7}"
  echo "packet_size=${PHASE14_PACKET_SIZE:-1200}"
  echo "queue_limit_packets=${PHASE14_QUEUE_LIMIT_PACKETS:-64}"
  echo "spin_threshold_us=${PHASE14_SPIN_THRESHOLD_US:-900}"
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
  echo "PHASE14_FOUNDATION_BMV2_REQUIRED" >&2
  exit 1
fi

if ! grep -Fq 'p4runtime_available=1' "$OUTPUT_DIR/preflight.txt"; then
  echo "PHASE14_FOUNDATION_P4RUNTIME_REQUIRED" >&2
  exit 1
fi

cpu_count="$(python3 -c 'import os; print(len(os.sched_getaffinity(0)))')"
if (( cpu_count < 2 )); then
  echo "PHASE14_FOUNDATION_DISTINCT_SENDER_CPUS_UNAVAILABLE=$cpu_count" >&2
  exit 1
fi

printf '%s\t%s\t%s\t%s\n' \
  mode \
  repetition \
  output_dir \
  exit_status \
  > "$OUTPUT_DIR/manifest.tsv"

operational_failures=0

for mode in baseline adapt; do
  for repetition in $(seq 1 "$REPETITIONS"); do
    run_dir="$OUTPUT_DIR/runs/${mode}-rep${repetition}"
    run_log="$OUTPUT_DIR/runs/${mode}-rep${repetition}.log"
    mkdir -p "$run_dir"

    echo "PHASE14_FOUNDATION_RUN_BEGIN=${mode}:rep${repetition}" \
      | tee -a "$OUTPUT_DIR/runner.txt"

    set +e
    timeout "$RUN_TIMEOUT_S" \
      env \
        S2_QOS_MODE="$mode" \
        S2_QOS_OUTPUT_DIR="$run_dir" \
        S2_QOS_CAPACITY_MBPS="${PHASE14_CAPACITY_MBPS:-3}" \
        S2_QOS_MULTICAST_RESERVED_MBPS="${PHASE14_MULTICAST_RESERVED_MBPS:-2}" \
        S2_QOS_QUEUE_LIMIT_PACKETS="${PHASE14_QUEUE_LIMIT_PACKETS:-64}" \
        S2_QOS_DURATION="${PHASE14_DURATION_S:-3}" \
        S2_QOS_BACKGROUND_DURATION="${PHASE14_BACKGROUND_DURATION_S:-7}" \
        S2_QOS_MULTICAST_RATE_MBPS="${PHASE14_MULTICAST_RATE_MBPS:-2}" \
        S2_QOS_BACKGROUND_RATE_MBPS="${PHASE14_BACKGROUND_RATE_MBPS:-2}" \
        S2_QOS_PACKET_SIZE="${PHASE14_PACKET_SIZE:-1200}" \
        S2_QOS_SPIN_THRESHOLD_US="${PHASE14_SPIN_THRESHOLD_US:-900}" \
        S2_QOS_MAX_ABS_RATE_ERROR_PCT="${PHASE14_MAX_ABS_RATE_ERROR_PCT:-5}" \
        S2_QOS_MIN_INTER_SEND_RATIO="${PHASE14_MIN_INTER_SEND_RATIO:-0.98}" \
        ./setup_all.sh run_s2_p4_qos_contention \
        > "$run_log" 2>&1
    status=$?
    set -e

    printf '%s\t%s\t%s\t%s\n' \
      "$mode" \
      "$repetition" \
      "$run_dir" \
      "$status" \
      >> "$OUTPUT_DIR/manifest.tsv"

    echo "PHASE14_FOUNDATION_RUN_END=${mode}:rep${repetition}:status${status}" \
      | tee -a "$OUTPUT_DIR/runner.txt"

    if (( status != 0 )); then
      operational_failures=$((operational_failures + 1))
      tail -n 120 "$run_log" >&2 || true
    fi
  done
done

echo "PHASE14_FOUNDATION_OPERATIONAL_FAILURE_COUNT=$operational_failures" \
  | tee -a "$OUTPUT_DIR/runner.txt"

export PHASE14_FOUNDATION_OUTPUT_DIR="$OUTPUT_DIR"
export PHASE14_FOUNDATION_REPETITIONS="$REPETITIONS"

PYTHONDONTWRITEBYTECODE=1 \
python3 - <<'PY' \
  | tee "$OUTPUT_DIR/foundation-analysis.txt"
from __future__ import annotations

import csv
import json
import os
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


root = Path(os.environ["PHASE14_FOUNDATION_OUTPUT_DIR"])
expected_repetitions = int(os.environ["PHASE14_FOUNDATION_REPETITIONS"])
manifest_path = root / "manifest.tsv"

with manifest_path.open("r", encoding="utf-8", newline="") as handle:
    rows = list(csv.DictReader(handle, delimiter="\t"))

failed: list[str] = []
expected_rows = expected_repetitions * 2
print(f"PHASE14_FOUNDATION_EXPECTED_RUN_COUNT={expected_rows}")
print(f"PHASE14_FOUNDATION_OBSERVED_RUN_COUNT={len(rows)}")

if len(rows) != expected_rows:
    failed.append("run_count")

metrics: dict[str, dict[str, list[float]]] = defaultdict(
    lambda: defaultdict(list)
)
mode_run_count: dict[str, int] = defaultdict(int)
invalid_rows: list[str] = []

for row in rows:
    mode = row["mode"]
    repetition = row["repetition"]
    status = int(row["exit_status"])
    summary_path = Path(row["output_dir"]) / "summary.json"

    if status != 0 or not summary_path.is_file():
        invalid_rows.append(f"{mode}:rep{repetition}:status{status}")
        continue

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    scope = summary.get("scope") or {}
    checks = summary.get("operational_checks") or {}
    traffic = summary.get("traffic") or {}
    multicast = traffic.get("multicast") or {}
    multicast_summary = multicast.get("summary") or {}
    receivers = multicast_summary.get("receivers") or {}
    receiver_b = receivers.get("B") or {}
    receiver_c = receivers.get("C") or {}
    background = traffic.get("background") or {}
    bottleneck = summary.get("bottleneck") or {}
    class_counters = bottleneck.get("class_counters") or {}

    row_ok = (
        summary.get("passed") is True
        and summary.get("mode") == mode
        and scope.get("multicast_dataplane_exercised") is True
        and scope.get("shared_receiver_b_egress_exercised") is True
        and scope.get("linux_tc_egress_qos_exercised") is True
        and scope.get("p4_internal_queueing_exercised") is False
        and scope.get("requested_multicast_rate_validated") is True
        and scope.get("requested_background_rate_validated") is True
        and all(value is True for value in checks.values())
        and float(receiver_c.get("delivery_ratio") or 0.0) > 0.0
        and bool(class_counters)
    )

    if not row_ok:
        invalid_rows.append(f"{mode}:rep{repetition}:content")
        continue

    mode_run_count[mode] += 1
    metrics[mode]["receiver_b_ratio"].append(
        float(receiver_b.get("delivery_ratio") or 0.0)
    )
    metrics[mode]["receiver_c_ratio"].append(
        float(receiver_c.get("delivery_ratio") or 0.0)
    )
    metrics[mode]["receiver_b_p95_ms"].append(
        float((receiver_b.get("one_way_delay_ms") or {}).get("p95") or 0.0)
    )
    metrics[mode]["receiver_c_p95_ms"].append(
        float((receiver_c.get("one_way_delay_ms") or {}).get("p95") or 0.0)
    )
    metrics[mode]["background_delivery_ratio"].append(
        float(background.get("delivery_ratio") or 0.0)
    )
    metrics[mode]["multicast_rate_error_pct"].append(
        abs(
            float(
                ((multicast_summary.get("rate_validation") or {}).get(
                    "absolute_rate_error_pct"
                ))
                or 0.0
            )
        )
    )
    metrics[mode]["background_rate_error_pct"].append(
        abs(float((background.get("sender") or {}).get("rate_error_pct") or 0.0))
    )

print(f"PHASE14_FOUNDATION_INVALID_ROW_COUNT={len(invalid_rows)}")
if invalid_rows:
    print("PHASE14_FOUNDATION_INVALID_ROWS=" + ",".join(invalid_rows))
    failed.append("invalid_rows")

for mode in ("baseline", "adapt"):
    print(
        f"PHASE14_FOUNDATION_{mode.upper()}_VALID_RUN_COUNT="
        f"{mode_run_count[mode]}"
    )
    if mode_run_count[mode] != expected_repetitions:
        failed.append(f"{mode}_count")
        continue

    mode_metrics = metrics[mode]
    aggregates: dict[str, float] = {
        "median_receiver_b_ratio": statistics.median(
            mode_metrics["receiver_b_ratio"]
        ),
        "minimum_receiver_b_ratio": min(mode_metrics["receiver_b_ratio"]),
        "median_receiver_c_ratio": statistics.median(
            mode_metrics["receiver_c_ratio"]
        ),
        "minimum_receiver_c_ratio": min(mode_metrics["receiver_c_ratio"]),
        "median_receiver_b_p95_ms": statistics.median(
            mode_metrics["receiver_b_p95_ms"]
        ),
        "median_receiver_c_p95_ms": statistics.median(
            mode_metrics["receiver_c_p95_ms"]
        ),
        "median_background_delivery_ratio": statistics.median(
            mode_metrics["background_delivery_ratio"]
        ),
        "maximum_multicast_rate_error_pct": max(
            mode_metrics["multicast_rate_error_pct"]
        ),
        "maximum_background_rate_error_pct": max(
            mode_metrics["background_rate_error_pct"]
        ),
    }

    for name, value in aggregates.items():
        print(
            f"PHASE14_FOUNDATION_{mode.upper()}_{name.upper()}="
            f"{value:.9f}"
        )

if not failed:
    baseline = metrics["baseline"]
    adapt = metrics["adapt"]
    baseline_b = statistics.median(baseline["receiver_b_ratio"])
    adapt_b = statistics.median(adapt["receiver_b_ratio"])
    baseline_c_min = min(baseline["receiver_c_ratio"])
    adapt_c_min = min(adapt["receiver_c_ratio"])
    baseline_b_p95 = statistics.median(baseline["receiver_b_p95_ms"])
    adapt_b_p95 = statistics.median(adapt["receiver_b_p95_ms"])

    contention_observed = (
        baseline_b < 0.99
        or (
            baseline_b_p95 > 0.0
            and statistics.median(baseline["receiver_c_p95_ms"]) > 0.0
            and baseline_b_p95
            > statistics.median(baseline["receiver_c_p95_ms"]) * 2.0
        )
    )
    control_path_stable = baseline_c_min >= 0.99 and adapt_c_min >= 0.99
    delivery_isolation = adapt_b >= 0.99 and adapt_b >= baseline_b + 0.05
    latency_improved = (
        baseline_b_p95 > 0.0
        and adapt_b_p95 > 0.0
        and adapt_b_p95 < baseline_b_p95
    )
    candidate_found = (
        contention_observed
        and control_path_stable
        and delivery_isolation
        and latency_improved
    )
else:
    contention_observed = False
    control_path_stable = False
    delivery_isolation = False
    latency_improved = False
    candidate_found = False

print(f"PHASE14_FOUNDATION_CONTENTION_OBSERVED={contention_observed}")
print(f"PHASE14_FOUNDATION_CONTROL_PATH_STABLE={control_path_stable}")
print(f"PHASE14_FOUNDATION_DELIVERY_ISOLATION_CANDIDATE={delivery_isolation}")
print(f"PHASE14_FOUNDATION_LATENCY_IMPROVEMENT_CANDIDATE={latency_improved}")
print(f"PHASE14_FOUNDATION_CANDIDATE_FOUND={candidate_found}")
print("PHASE14_FOUNDATION_P4_INTERNAL_QUEUEING_VALIDATED=False")
print("PHASE14_FOUNDATION_LINUX_TC_EGRESS_QOS_EXERCISED=True")

if failed:
    print("PHASE14_FOUNDATION_ANALYSIS_FAILED=" + ",".join(failed))
    raise SystemExit(1)

summary: dict[str, Any] = {
    "scenario": "S2_P4_multicast_qos_contention_foundation_matrix",
    "repetitions": expected_repetitions,
    "operationally_valid": True,
    "candidate_found": candidate_found,
    "candidate_checks": {
        "contention_observed": contention_observed,
        "control_path_stable": control_path_stable,
        "delivery_isolation": delivery_isolation,
        "latency_improved": latency_improved,
    },
    "scope": {
        "p4_multicast_replication_exercised": True,
        "p4_unicast_background_forwarding_exercised": True,
        "linux_tc_shared_egress_contention_exercised": True,
        "p4_internal_queueing_validated": False,
        "recovery_validated": False,
    },
}
(root / "foundation-summary.json").write_text(
    json.dumps(summary, indent=2) + "\n",
    encoding="utf-8",
)

print("PHASE14_FOUNDATION_ANALYSIS_OK")
PY

analysis_status="${PIPESTATUS[0]}"

echo "phase14_foundation_analysis_exit_status=$analysis_status" \
  | tee -a "$OUTPUT_DIR/foundation-analysis.txt"

if (( operational_failures != 0 || analysis_status != 0 )); then
  echo "PHASE14_FOUNDATION_RUNNER_FAILED" | tee -a "$OUTPUT_DIR/runner.txt"
  exit 1
fi

echo "PHASE14_FOUNDATION_RUNNER_OK" | tee -a "$OUTPUT_DIR/runner.txt"
