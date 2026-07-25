#!/usr/bin/env bash
set -euo pipefail

# Validate multicast QoS with explicit payload-to-tc rate translation, longer
# traffic intervals, and a counterbalanced baseline/adapt execution order.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="${1:-}"

if [[ -z "$OUTPUT_DIR" ]]; then
  echo "usage: $0 OUTPUT_DIR" >&2
  exit 2
fi

mkdir -p "$OUTPUT_DIR/runs"
OUTPUT_DIR="$(realpath "$OUTPUT_DIR")"

REPETITIONS_PER_MODE="${PHASE14_SEMANTIC_REPETITIONS_PER_MODE:-4}"
RUN_TIMEOUT_S="${PHASE14_SEMANTIC_RUN_TIMEOUT_S:-240}"

if ! [[ "$REPETITIONS_PER_MODE" =~ ^[1-9][0-9]*$ ]]; then
  echo "PHASE14_SEMANTIC_INVALID_REPETITIONS=$REPETITIONS_PER_MODE" >&2
  exit 2
fi

cd "$REPO_DIR"

{
  echo "repository=$REPO_DIR"
  echo "output_dir=$OUTPUT_DIR"
  echo "repetitions_per_mode=$REPETITIONS_PER_MODE"
  echo "capacity_mbps=${PHASE14_SEMANTIC_CAPACITY_MBPS:-3}"
  echo "multicast_payload_rate_mbps=${PHASE14_SEMANTIC_MULTICAST_RATE_MBPS:-2}"
  echo "background_payload_rate_mbps=${PHASE14_SEMANTIC_BACKGROUND_RATE_MBPS:-2}"
  echo "multicast_reserved_tc_mbps=${PHASE14_SEMANTIC_MULTICAST_RESERVED_MBPS:-2.1}"
  echo "tc_overhead_bytes=${PHASE14_SEMANTIC_TC_OVERHEAD_BYTES:-42}"
  echo "duration_s=${PHASE14_SEMANTIC_DURATION_S:-12}"
  echo "background_duration_s=${PHASE14_SEMANTIC_BACKGROUND_DURATION_S:-18}"
  echo "packet_size=${PHASE14_SEMANTIC_PACKET_SIZE:-1200}"
  echo "queue_limit_packets=${PHASE14_SEMANTIC_QUEUE_LIMIT_PACKETS:-64}"
  echo "spin_threshold_us=${PHASE14_SEMANTIC_SPIN_THRESHOLD_US:-900}"
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
  echo "PHASE14_SEMANTIC_BMV2_REQUIRED" >&2
  exit 1
fi

if ! grep -Fq 'p4runtime_available=1' "$OUTPUT_DIR/preflight.txt"; then
  echo "PHASE14_SEMANTIC_P4RUNTIME_REQUIRED" >&2
  exit 1
fi

cpu_count="$(python3 -c 'import os; print(len(os.sched_getaffinity(0)))')"
if (( cpu_count < 2 )); then
  echo "PHASE14_SEMANTIC_DISTINCT_SENDER_CPUS_UNAVAILABLE=$cpu_count" >&2
  exit 1
fi

printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  order_index \
  pair \
  position \
  mode \
  repetition \
  output_dir \
  exit_status \
  > "$OUTPUT_DIR/manifest.tsv"

operational_failures=0
order_index=0
baseline_repetition=0
adapt_repetition=0

for pair in $(seq 1 "$REPETITIONS_PER_MODE"); do
  if (( pair % 2 == 1 )); then
    pair_modes=(baseline adapt)
  else
    pair_modes=(adapt baseline)
  fi

  position=0
  for mode in "${pair_modes[@]}"; do
    order_index=$((order_index + 1))
    position=$((position + 1))

    if [[ "$mode" == "baseline" ]]; then
      baseline_repetition=$((baseline_repetition + 1))
      repetition="$baseline_repetition"
    else
      adapt_repetition=$((adapt_repetition + 1))
      repetition="$adapt_repetition"
    fi

    run_dir="$OUTPUT_DIR/runs/order${order_index}-${mode}-rep${repetition}"
    run_log="$OUTPUT_DIR/runs/order${order_index}-${mode}-rep${repetition}.log"
    mkdir -p "$run_dir"

    echo \
      "PHASE14_SEMANTIC_RUN_BEGIN=order${order_index}:pair${pair}:position${position}:${mode}:rep${repetition}" \
      | tee -a "$OUTPUT_DIR/runner.txt"

    set +e
    timeout "$RUN_TIMEOUT_S" \
      env \
        S2_QOS_MODE="$mode" \
        S2_QOS_OUTPUT_DIR="$run_dir" \
        S2_QOS_CAPACITY_MBPS="${PHASE14_SEMANTIC_CAPACITY_MBPS:-3}" \
        S2_QOS_MULTICAST_RESERVED_MBPS="${PHASE14_SEMANTIC_MULTICAST_RESERVED_MBPS:-2.1}" \
        S2_QOS_TC_OVERHEAD_BYTES="${PHASE14_SEMANTIC_TC_OVERHEAD_BYTES:-42}" \
        S2_QOS_QUEUE_LIMIT_PACKETS="${PHASE14_SEMANTIC_QUEUE_LIMIT_PACKETS:-64}" \
        S2_QOS_DURATION="${PHASE14_SEMANTIC_DURATION_S:-12}" \
        S2_QOS_BACKGROUND_DURATION="${PHASE14_SEMANTIC_BACKGROUND_DURATION_S:-18}" \
        S2_QOS_BACKGROUND_PREFILL_S="${PHASE14_SEMANTIC_BACKGROUND_PREFILL_S:-1.5}" \
        S2_QOS_BACKGROUND_DRAIN_S="${PHASE14_SEMANTIC_BACKGROUND_DRAIN_S:-1.5}" \
        S2_QOS_MULTICAST_RATE_MBPS="${PHASE14_SEMANTIC_MULTICAST_RATE_MBPS:-2}" \
        S2_QOS_BACKGROUND_RATE_MBPS="${PHASE14_SEMANTIC_BACKGROUND_RATE_MBPS:-2}" \
        S2_QOS_PACKET_SIZE="${PHASE14_SEMANTIC_PACKET_SIZE:-1200}" \
        S2_QOS_SPIN_THRESHOLD_US="${PHASE14_SEMANTIC_SPIN_THRESHOLD_US:-900}" \
        S2_QOS_MAX_ABS_RATE_ERROR_PCT="${PHASE14_SEMANTIC_MAX_ABS_RATE_ERROR_PCT:-5}" \
        S2_QOS_MIN_INTER_SEND_RATIO="${PHASE14_SEMANTIC_MIN_INTER_SEND_RATIO:-0.98}" \
        S2_QOS_WORKER_MAX_RUNTIME_S="${PHASE14_SEMANTIC_WORKER_MAX_RUNTIME_S:-180}" \
        ./setup_all.sh run_s2_p4_qos_contention \
        > "$run_log" 2>&1
    status=$?
    set -e

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$order_index" \
      "$pair" \
      "$position" \
      "$mode" \
      "$repetition" \
      "$run_dir" \
      "$status" \
      >> "$OUTPUT_DIR/manifest.tsv"

    echo \
      "PHASE14_SEMANTIC_RUN_END=order${order_index}:${mode}:rep${repetition}:status${status}" \
      | tee -a "$OUTPUT_DIR/runner.txt"

    if (( status != 0 )); then
      operational_failures=$((operational_failures + 1))
      tail -n 160 "$run_log" >&2 || true
    fi
  done
done

echo "PHASE14_SEMANTIC_OPERATIONAL_FAILURE_COUNT=$operational_failures" \
  | tee -a "$OUTPUT_DIR/runner.txt"

export PHASE14_SEMANTIC_OUTPUT_DIR="$OUTPUT_DIR"
export PHASE14_SEMANTIC_REPETITIONS_PER_MODE="$REPETITIONS_PER_MODE"

PYTHONDONTWRITEBYTECODE=1 \
python3 - <<'PY' \
  | tee "$OUTPUT_DIR/semantic-analysis.txt"
from __future__ import annotations

import csv
import json
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


root = Path(os.environ["PHASE14_SEMANTIC_OUTPUT_DIR"])
repetitions = int(os.environ["PHASE14_SEMANTIC_REPETITIONS_PER_MODE"])
manifest_path = root / "manifest.tsv"

with manifest_path.open("r", encoding="utf-8", newline="") as handle:
    rows = list(csv.DictReader(handle, delimiter="\t"))

failed: list[str] = []
invalid_rows: list[str] = []
expected_count = repetitions * 2

print(f"PHASE14_SEMANTIC_EXPECTED_RUN_COUNT={expected_count}")
print(f"PHASE14_SEMANTIC_OBSERVED_RUN_COUNT={len(rows)}")

if len(rows) != expected_count:
    failed.append("run_count")

expected_order: list[str] = []
for pair in range(1, repetitions + 1):
    expected_order.extend(
        ["baseline", "adapt"]
        if pair % 2 == 1
        else ["adapt", "baseline"]
    )

observed_order = [row["mode"] for row in rows]
counterbalanced_order_ok = observed_order == expected_order
print(
    "PHASE14_SEMANTIC_COUNTERBALANCED_ORDER_OK="
    f"{counterbalanced_order_ok}"
)

if not counterbalanced_order_ok:
    failed.append("counterbalanced_order")

counts = Counter(observed_order)
print(f"PHASE14_SEMANTIC_BASELINE_RUN_COUNT={counts['baseline']}")
print(f"PHASE14_SEMANTIC_ADAPT_RUN_COUNT={counts['adapt']}")

if counts != Counter({"baseline": repetitions, "adapt": repetitions}):
    failed.append("mode_counts")

metrics: dict[str, dict[str, list[float]]] = defaultdict(
    lambda: defaultdict(list)
)
semantic_coverages: list[bool] = []
configured_coverages: list[bool] = []
adapt_mcast_drops: list[int] = []
baseline_shared_drops: list[int] = []
adapt_durations: list[float] = []

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
    accounting = bottleneck.get("rate_accounting") or {}
    configured = accounting.get("configured") or {}
    observed = accounting.get("observed") or {}

    row_ok = (
        summary.get("passed") is True
        and summary.get("mode") == mode
        and scope.get("multicast_dataplane_exercised") is True
        and scope.get("shared_receiver_b_egress_exercised") is True
        and scope.get("linux_tc_egress_qos_exercised") is True
        and scope.get("p4_internal_queueing_exercised") is False
        and scope.get("requested_multicast_rate_validated") is True
        and scope.get("requested_background_rate_validated") is True
        and scope.get("tc_rate_accounting_model_applied") is True
        and all(value is True for value in checks.values())
        and bool(class_counters)
    )

    if mode == "adapt":
        row_ok = (
            row_ok
            and scope.get("tc_multicast_reservation_exercised") is True
            and configured.get(
                "configured_multicast_reservation_covers_expected_load"
            )
            is True
            and observed.get(
                "observed_multicast_reservation_covers_load"
            )
            is True
        )

    if not row_ok:
        invalid_rows.append(f"{mode}:rep{repetition}:content")
        continue

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

    if mode == "baseline":
        baseline_shared_drops.append(
            int((class_counters.get("1:30") or {}).get("drops") or 0)
        )
    else:
        configured_coverages.append(
            configured.get(
                "configured_multicast_reservation_covers_expected_load"
            )
            is True
        )
        semantic_coverages.append(
            observed.get(
                "observed_multicast_reservation_covers_load"
            )
            is True
        )
        multicast_class = observed.get("multicast_class") or {}
        adapt_mcast_drops.append(int(multicast_class.get("drops") or 0))
        adapt_durations.append(float(multicast.get("duration_s") or 0.0))
        metrics[mode]["observed_required_tc_rate_mbps"].append(
            float(multicast_class.get("required_tc_rate_mbps") or 0.0)
        )
        metrics[mode]["reservation_margin_mbps"].append(
            float(observed.get("observed_multicast_reservation_margin_mbps") or 0.0)
        )
        metrics[mode]["tc_bytes_per_packet"].append(
            float(multicast_class.get("bytes_per_packet") or 0.0)
        )

print(f"PHASE14_SEMANTIC_INVALID_ROW_COUNT={len(invalid_rows)}")
if invalid_rows:
    print("PHASE14_SEMANTIC_INVALID_ROWS=" + ",".join(invalid_rows))
    failed.append("invalid_rows")

for mode in ("baseline", "adapt"):
    valid_count = len(metrics[mode]["receiver_b_ratio"])
    print(f"PHASE14_SEMANTIC_{mode.upper()}_VALID_RUN_COUNT={valid_count}")
    if valid_count != repetitions:
        failed.append(f"{mode}_valid_count")


def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def minimum(values: list[float]) -> float:
    return min(values) if values else 0.0


def maximum(values: list[float]) -> float:
    return max(values) if values else 0.0


baseline = metrics["baseline"]
adapt = metrics["adapt"]

aggregates: dict[str, float] = {
    "baseline_median_receiver_b_ratio": median(baseline["receiver_b_ratio"]),
    "baseline_minimum_receiver_b_ratio": minimum(baseline["receiver_b_ratio"]),
    "adapt_median_receiver_b_ratio": median(adapt["receiver_b_ratio"]),
    "adapt_minimum_receiver_b_ratio": minimum(adapt["receiver_b_ratio"]),
    "baseline_minimum_receiver_c_ratio": minimum(baseline["receiver_c_ratio"]),
    "adapt_minimum_receiver_c_ratio": minimum(adapt["receiver_c_ratio"]),
    "baseline_median_receiver_b_p95_ms": median(baseline["receiver_b_p95_ms"]),
    "adapt_median_receiver_b_p95_ms": median(adapt["receiver_b_p95_ms"]),
    "baseline_median_background_delivery_ratio": median(
        baseline["background_delivery_ratio"]
    ),
    "adapt_median_background_delivery_ratio": median(
        adapt["background_delivery_ratio"]
    ),
    "adapt_maximum_required_tc_rate_mbps": maximum(
        adapt["observed_required_tc_rate_mbps"]
    ),
    "adapt_minimum_reservation_margin_mbps": minimum(
        adapt["reservation_margin_mbps"]
    ),
    "adapt_median_tc_bytes_per_packet": median(adapt["tc_bytes_per_packet"]),
    "adapt_maximum_multicast_class_drops": float(max(adapt_mcast_drops or [0])),
    "baseline_maximum_shared_class_drops": float(max(baseline_shared_drops or [0])),
}

for name, value in aggregates.items():
    print(f"PHASE14_SEMANTIC_{name.upper()}={value:.9f}")

if failed:
    contention_observed = False
    control_path_stable = False
    delivery_isolation = False
    latency_isolation = False
    semantic_accounting_valid = False
    sustained_adapt_delivery = False
    candidate_found = False
else:
    baseline_b = aggregates["baseline_median_receiver_b_ratio"]
    adapt_b = aggregates["adapt_median_receiver_b_ratio"]
    baseline_p95 = aggregates["baseline_median_receiver_b_p95_ms"]
    adapt_p95 = aggregates["adapt_median_receiver_b_p95_ms"]

    contention_observed = (
        baseline_b < 0.95
        or aggregates["baseline_maximum_shared_class_drops"] > 0.0
    )
    control_path_stable = (
        aggregates["baseline_minimum_receiver_c_ratio"] >= 0.99
        and aggregates["adapt_minimum_receiver_c_ratio"] >= 0.99
    )
    delivery_isolation = (
        aggregates["adapt_minimum_receiver_b_ratio"] >= 0.99
        and adapt_b >= baseline_b + 0.05
    )
    latency_isolation = (
        baseline_p95 > 0.0
        and adapt_p95 > 0.0
        and adapt_p95 <= baseline_p95 * 0.50
    )
    semantic_accounting_valid = (
        len(configured_coverages) == repetitions
        and len(semantic_coverages) == repetitions
        and all(configured_coverages)
        and all(semantic_coverages)
        and aggregates["adapt_minimum_reservation_margin_mbps"] >= 0.0
    )
    sustained_adapt_delivery = (
        len(adapt_durations) == repetitions
        and min(adapt_durations) >= 10.0
        and aggregates["adapt_maximum_multicast_class_drops"] == 0.0
        and aggregates["adapt_minimum_receiver_b_ratio"] >= 0.99
    )
    candidate_found = all(
        (
            counterbalanced_order_ok,
            contention_observed,
            control_path_stable,
            delivery_isolation,
            latency_isolation,
            semantic_accounting_valid,
            sustained_adapt_delivery,
        )
    )

print(f"PHASE14_SEMANTIC_CONTENTION_OBSERVED={contention_observed}")
print(f"PHASE14_SEMANTIC_CONTROL_PATH_STABLE={control_path_stable}")
print(f"PHASE14_SEMANTIC_DELIVERY_ISOLATION_CANDIDATE={delivery_isolation}")
print(f"PHASE14_SEMANTIC_LATENCY_ISOLATION_CANDIDATE={latency_isolation}")
print(f"PHASE14_SEMANTIC_RATE_ACCOUNTING_VALID={semantic_accounting_valid}")
print(f"PHASE14_SEMANTIC_SUSTAINED_ADAPT_DELIVERY={sustained_adapt_delivery}")
print(f"PHASE14_SEMANTIC_CANDIDATE_FOUND={candidate_found}")
print("PHASE14_SEMANTIC_P4_INTERNAL_QUEUEING_VALIDATED=False")
print("PHASE14_SEMANTIC_LINUX_TC_EGRESS_QOS_EXERCISED=True")

summary: dict[str, Any] = {
    "scenario": "S2_P4_multicast_qos_contention_semantic_validation_matrix",
    "repetitions_per_mode": repetitions,
    "counterbalanced_order": observed_order,
    "operationally_valid": not failed,
    "candidate_found": candidate_found,
    "aggregates": aggregates,
    "candidate_checks": {
        "counterbalanced_order_ok": counterbalanced_order_ok,
        "contention_observed": contention_observed,
        "control_path_stable": control_path_stable,
        "delivery_isolation": delivery_isolation,
        "latency_isolation": latency_isolation,
        "semantic_rate_accounting_valid": semantic_accounting_valid,
        "sustained_adapt_delivery": sustained_adapt_delivery,
    },
    "scope": {
        "p4_multicast_replication_exercised": True,
        "p4_unicast_background_forwarding_exercised": True,
        "linux_tc_shared_egress_contention_exercised": True,
        "linux_tc_semantic_rate_accounting_exercised": True,
        "p4_internal_queueing_validated": False,
        "recovery_validated": False,
    },
}

(root / "semantic-summary.json").write_text(
    json.dumps(summary, indent=2) + "\n",
    encoding="utf-8",
)

if failed:
    print("PHASE14_SEMANTIC_ANALYSIS_FAILED=" + ",".join(failed))
    raise SystemExit(1)

print("PHASE14_SEMANTIC_ANALYSIS_OK")
PY

analysis_status="${PIPESTATUS[0]}"

echo "phase14_semantic_analysis_exit_status=$analysis_status" \
  | tee -a "$OUTPUT_DIR/semantic-analysis.txt"

if (( operational_failures != 0 || analysis_status != 0 )); then
  echo "PHASE14_SEMANTIC_RUNNER_FAILED" | tee -a "$OUTPUT_DIR/runner.txt"
  exit 1
fi

echo "PHASE14_SEMANTIC_RUNNER_OK" | tee -a "$OUTPUT_DIR/runner.txt"
