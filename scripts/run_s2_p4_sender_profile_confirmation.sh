#!/usr/bin/env bash
# Confirm calibrated sender pacing profiles on the real S2 BMv2 packet path.
#
# Phase 13 loopback calibration isolates timer and scheduler behavior, but it
# does not include namespace transitions, Linux veth pairs, BMv2 processing, or
# multicast replication. This runner repeats the validated portable profiles on
# the production smoke-test path before any profile becomes the repository
# default.
#
# Every run keeps the no-catch-up invariant, pins only the sender process to one
# allowed CPU, and records both offered-rate fidelity and receiver delivery. The
# analyzer ranks passing profiles by rate accuracy first, then CPU cost and
# stability. This production ranking differs deliberately from the exploratory
# calibration ranking, where minimum CPU cost was the first tie-breaker.

set +e
set +o errexit 2>/dev/null || true
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SETUP_SCRIPT="$REPO_DIR/setup_all.sh"

OUTPUT_DIR="${1:-}"

if [[ -z "$OUTPUT_DIR" ]]; then
  echo "usage: $0 OUTPUT_DIR" >&2
  exit 2
fi

mkdir -p "$OUTPUT_DIR/runs"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

if [[ -x "$HOME/l2i-dev/venv/bin/python" ]]; then
  PYTHON_BIN="$HOME/l2i-dev/venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

RATE_MBPS="${PHASE13_CONFIRM_RATE_MBPS:-2}"
DURATION_S="${PHASE13_CONFIRM_DURATION_S:-3}"
PACKET_SIZE="${PHASE13_CONFIRM_PACKET_SIZE:-1200}"
REPETITIONS="${PHASE13_CONFIRM_REPETITIONS:-3}"
SPIN_THRESHOLDS_US="${PHASE13_CONFIRM_SPIN_THRESHOLDS_US:-300 600 900}"
MIN_DELIVERY="${PHASE13_CONFIRM_MIN_DELIVERY:-0.99}"
MAX_ABS_RATE_ERROR_PCT="${PHASE13_CONFIRM_MAX_ABS_RATE_ERROR_PCT:-5}"
MIN_INTER_SEND_RATIO="${PHASE13_CONFIRM_MIN_INTER_SEND_RATIO:-0.98}"
MAX_CPU_RATIO="${PHASE13_CONFIRM_MAX_CPU_RATIO:-0.35}"
RUN_TIMEOUT_S="${PHASE13_CONFIRM_RUN_TIMEOUT_S:-120}"

MANIFEST="$OUTPUT_DIR/manifest.tsv"
PREFLIGHT="$OUTPUT_DIR/preflight.txt"
RUNNER_LOG="$OUTPUT_DIR/runner.txt"
ANALYSIS_LOG="$OUTPUT_DIR/production-analysis.txt"
ANALYSIS_JSON="$OUTPUT_DIR/production-summary.json"

validated_thresholds=()

for threshold in $SPIN_THRESHOLDS_US; do
  if ! [[ "$threshold" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "invalid spin threshold: $threshold" >&2
    exit 2
  fi

  threshold_ok="$($PYTHON_BIN - "$threshold" "$RATE_MBPS" "$PACKET_SIZE" <<'PY'
import sys

threshold_us = float(sys.argv[1])
rate_mbps = float(sys.argv[2])
packet_size = int(sys.argv[3])
interval_us = (packet_size * 8.0) / rate_mbps

print(int(0.0 < threshold_us < interval_us))
PY
)"

  if [[ "$threshold_ok" != "1" ]]; then
    echo \
      "spin threshold must be positive and smaller than the nominal interval: $threshold us" \
      >&2
    exit 2
  fi

  validated_thresholds+=("$threshold")
done

if [[ "${#validated_thresholds[@]}" -eq 0 ]]; then
  echo "at least one spin threshold is required" >&2
  exit 2
fi

if ! pgrep -f '[s]imple_switch_grpc' >/dev/null; then
  echo "BMv2 must be running before production-path confirmation" >&2
  exit 1
fi

if ! ss -H -ltn | grep -Eq ':9559\b'; then
  echo "P4Runtime port 9559 must be listening before confirmation" >&2
  exit 1
fi

{
  echo "===== CONFIRMATION PARAMETERS ====="
  echo "repo_dir=$REPO_DIR"
  echo "python_bin=$PYTHON_BIN"
  echo "rate_mbps=$RATE_MBPS"
  echo "duration_s=$DURATION_S"
  echo "packet_size=$PACKET_SIZE"
  echo "repetitions=$REPETITIONS"
  echo "spin_thresholds_us=${validated_thresholds[*]}"
  echo "min_delivery=$MIN_DELIVERY"
  echo "max_abs_rate_error_pct=$MAX_ABS_RATE_ERROR_PCT"
  echo "min_inter_send_ratio=$MIN_INTER_SEND_RATIO"
  echo "max_cpu_ratio=$MAX_CPU_RATIO"

  echo
  echo "===== REPOSITORY ====="
  echo "branch=$(git -C "$REPO_DIR" branch --show-current)"
  echo "head=$(git -C "$REPO_DIR" rev-parse HEAD)"
  git -C "$REPO_DIR" status --short

  echo
  echo "===== SYSTEM ====="
  echo "hostname=$(hostname)"
  echo "kernel=$(uname -r)"
  echo "virtualization=$(systemd-detect-virt 2>/dev/null || echo unknown)"
  echo "boot_id=$(cat /proc/sys/kernel/random/boot_id)"
  echo "uptime=$(uptime -p)"

  echo
  echo "===== P4 STATE ====="
  pgrep -af simple_switch_grpc
  ss -H -ltn | grep -E ':9559\b'

  echo
  echo "===== CPU AFFINITY ====="
  taskset -pc $$ 2>/dev/null || true
  grep -E 'Cpus_allowed|Mems_allowed' /proc/self/status || true
} | tee "$PREFLIGHT"

printf '%s\t%s\t%s\t%s\t%s\n' \
  configuration \
  spin_threshold_us \
  repetition \
  output_dir \
  exit_status \
  > "$MANIFEST"

operational_failures=0

for threshold in "${validated_thresholds[@]}"; do
  token="${threshold//./p}"
  configuration="tailspin-${token}us-affinity"

  for repetition in $(seq 1 "$REPETITIONS"); do
    run_dir="$OUTPUT_DIR/runs/${configuration}-rep${repetition}"
    run_log="$OUTPUT_DIR/runs/${configuration}-rep${repetition}.log"

    mkdir -p "$run_dir"

    echo \
      "PHASE13_PRODUCTION_RUN_BEGIN=${configuration}:rep${repetition}" \
      | tee -a "$RUNNER_LOG"

    timeout "$RUN_TIMEOUT_S" \
      env \
        S2_DP_OUTPUT_DIR="$run_dir" \
        S2_DP_DURATION="$DURATION_S" \
        S2_DP_RATE_MBPS="$RATE_MBPS" \
        S2_DP_PACKET_SIZE="$PACKET_SIZE" \
        S2_DP_MIN_DELIVERY="$MIN_DELIVERY" \
        S2_DP_PACING_MODE=repeated_sleep_spin \
        S2_DP_SPIN_THRESHOLD_US="$threshold" \
        S2_DP_SENDER_CPU=auto \
        S2_DP_MAX_ABS_RATE_ERROR_PCT="$MAX_ABS_RATE_ERROR_PCT" \
        S2_DP_MIN_INTER_SEND_RATIO="$MIN_INTER_SEND_RATIO" \
        "$SETUP_SCRIPT" run_s2_p4_dataplane_smoke \
      > "$run_log" 2>&1

    status=$?

    printf '%s\t%s\t%s\t%s\t%s\n' \
      "$configuration" \
      "$threshold" \
      "$repetition" \
      "$run_dir" \
      "$status" \
      >> "$MANIFEST"

    if [[ ! -f "$run_dir/summary.json" ]]; then
      operational_failures=$((operational_failures + 1))
    fi

    echo \
      "PHASE13_PRODUCTION_RUN_END=${configuration}:rep${repetition}:status${status}" \
      | tee -a "$RUNNER_LOG"
  done
done

export PHASE13_PRODUCTION_CONFIRMATION_DIR="$OUTPUT_DIR"
export PHASE13_PRODUCTION_MAX_CPU_RATIO="$MAX_CPU_RATIO"

"$PYTHON_BIN" - <<'PY' | tee "$ANALYSIS_LOG"
from __future__ import annotations

import csv
import json
import math
import os
import statistics
from pathlib import Path
from typing import Any

root = Path(os.environ["PHASE13_PRODUCTION_CONFIRMATION_DIR"])
max_cpu_ratio = float(os.environ["PHASE13_PRODUCTION_MAX_CPU_RATIO"])
manifest_path = root / "manifest.tsv"
output_path = root / "production-summary.json"


def coefficient_of_variation(values: list[float]) -> float:
    """Return population CV while handling zero-valued or singleton samples."""

    if not values:
        return math.inf
    mean = statistics.fmean(values)
    if mean == 0.0:
        return 0.0 if all(value == 0.0 for value in values) else math.inf
    return statistics.pstdev(values) / mean


with manifest_path.open("r", encoding="utf-8", newline="") as handle:
    rows = list(csv.DictReader(handle, delimiter="\t"))

configurations: dict[str, list[dict[str, Any]]] = {}
invalid_rows: list[dict[str, str]] = []

for row in rows:
    summary_path = Path(row["output_dir"]) / "summary.json"
    if not summary_path.is_file():
        invalid_rows.append({**row, "reason": "missing_summary"})
        continue

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        invalid_rows.append({**row, "reason": f"invalid_summary:{exc}"})
        continue

    configurations.setdefault(row["configuration"], []).append(
        {
            "manifest": row,
            "summary": summary,
        }
    )

configuration_summaries: dict[str, dict[str, Any]] = {}

for configuration, samples in sorted(configurations.items()):
    rates: list[float] = []
    absolute_errors: list[float] = []
    cpu_ratios: list[float] = []
    inter_send_ratios: list[float] = []
    delivery_b: list[float] = []
    delivery_c: list[float] = []
    statuses: list[int] = []
    rate_validations: list[bool] = []

    for sample in samples:
        row = sample["manifest"]
        summary = sample["summary"]
        sender = summary.get("sender") or {}
        rate_validation = summary.get("rate_validation") or {}
        receivers = summary.get("receivers") or {}

        rates.append(float(sender.get("rate_payload_mbps_actual") or 0.0))
        absolute_errors.append(
            abs(float(sender.get("rate_error_pct") or 0.0))
        )
        cpu_ratios.append(float(sender.get("cpu_ratio") or 0.0))
        inter_send_ratios.append(
            float(rate_validation.get("minimum_inter_send_ratio") or 0.0)
        )
        delivery_b.append(
            float((receivers.get("B") or {}).get("delivery_ratio") or 0.0)
        )
        delivery_c.append(
            float((receivers.get("C") or {}).get("delivery_ratio") or 0.0)
        )
        statuses.append(int(row["exit_status"]))
        rate_validations.append(rate_validation.get("validated") is True)

    threshold = float(samples[0]["manifest"]["spin_threshold_us"])
    median_rate = statistics.median(rates)
    maximum_absolute_error = max(absolute_errors)
    rate_cv = coefficient_of_variation(rates)
    median_cpu = statistics.median(cpu_ratios)
    minimum_inter_send_ratio = min(inter_send_ratios)
    minimum_delivery_b = min(delivery_b)
    minimum_delivery_c = min(delivery_c)

    gates = {
        "complete": len(samples) > 0 and all(status == 0 for status in statuses),
        "rate": all(rate_validations),
        "no_microburst": minimum_inter_send_ratio >= 0.98,
        "delivery_b": minimum_delivery_b >= 0.99,
        "delivery_c": minimum_delivery_c >= 0.99,
        "cpu": median_cpu <= max_cpu_ratio,
    }
    gates["passed"] = all(gates.values())

    result = {
        "configuration": configuration,
        "spin_threshold_us": threshold,
        "sample_count": len(samples),
        "median_rate_mbps": median_rate,
        "maximum_absolute_rate_error_pct": maximum_absolute_error,
        "rate_coefficient_of_variation": rate_cv,
        "median_cpu_ratio": median_cpu,
        "minimum_inter_send_ratio": minimum_inter_send_ratio,
        "minimum_receiver_b_delivery_ratio": minimum_delivery_b,
        "minimum_receiver_c_delivery_ratio": minimum_delivery_c,
        "gates": gates,
    }
    configuration_summaries[configuration] = result

    token = configuration.upper().replace("-", "_").replace(".", "P")
    print(f"PHASE13_PROD_{token}_MEDIAN_RATE_MBPS={median_rate:.9f}")
    print(
        f"PHASE13_PROD_{token}_MAX_ABS_RATE_ERROR_PCT="
        f"{maximum_absolute_error:.6f}"
    )
    print(f"PHASE13_PROD_{token}_RATE_CV={rate_cv:.9f}")
    print(f"PHASE13_PROD_{token}_MEDIAN_CPU_RATIO={median_cpu:.9f}")
    print(
        f"PHASE13_PROD_{token}_MIN_INTER_SEND_RATIO="
        f"{minimum_inter_send_ratio:.9f}"
    )
    print(
        f"PHASE13_PROD_{token}_MIN_RECEIVER_B_RATIO="
        f"{minimum_delivery_b:.9f}"
    )
    print(
        f"PHASE13_PROD_{token}_MIN_RECEIVER_C_RATIO="
        f"{minimum_delivery_c:.9f}"
    )
    print(f"PHASE13_PROD_{token}_PASSED={gates['passed']}")

passing = [
    summary
    for summary in configuration_summaries.values()
    if summary["gates"]["passed"]
]

# Offered-load accuracy is the primary scientific requirement on the real data
# path. CPU cost and run-to-run stability break ties after all safety gates pass.
def production_rank(summary: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(summary["maximum_absolute_rate_error_pct"]),
        float(summary["median_cpu_ratio"]),
        float(summary["rate_coefficient_of_variation"]),
        float(summary["spin_threshold_us"]),
    )

selected = min(passing, key=production_rank) if passing else None
selected_name = str(selected["configuration"]) if selected else None

result = {
    "scenario": "S2_P4_sender_profile_production_confirmation",
    "invalid_rows": invalid_rows,
    "configurations": configuration_summaries,
    "passing_configuration_count": len(passing),
    "selection_policy": (
        "minimum_maximum_absolute_rate_error_then_cpu_then_cv_then_threshold"
    ),
    "selected_candidate": selected_name,
    "candidate_found": selected_name is not None,
}

output_path.write_text(
    json.dumps(result, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)

print(f"PHASE13_PRODUCTION_INVALID_ROW_COUNT={len(invalid_rows)}")
print(f"PHASE13_PRODUCTION_PASSING_CONFIGURATION_COUNT={len(passing)}")
print(f"PHASE13_PRODUCTION_CANDIDATE={selected_name or 'NONE'}")
print(f"PHASE13_PRODUCTION_CANDIDATE_FOUND={selected_name is not None}")
print("PHASE13_PRODUCTION_ANALYSIS_OK")
PY

analysis_status=${PIPESTATUS[0]}

if [[ "$analysis_status" -ne 0 ]]; then
  operational_failures=$((operational_failures + 1))
fi

{
  echo "PHASE13_PRODUCTION_OPERATIONAL_FAILURE_COUNT=$operational_failures"

  if [[ "$operational_failures" -eq 0 ]]; then
    echo "PHASE13_PRODUCTION_CONFIRMATION_RUNNER_OK"
  else
    echo "PHASE13_PRODUCTION_CONFIRMATION_RUNNER_FAILED"
  fi
} | tee -a "$RUNNER_LOG"

exit "$operational_failures"
