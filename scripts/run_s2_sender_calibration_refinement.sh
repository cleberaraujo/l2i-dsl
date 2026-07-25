#!/usr/bin/env bash
# Refine the S2 user-space pacing strategy after the Phase 13 foundation run.
#
# The foundation matrix showed that CPU affinity made repeated short sleeps
# stable, but the final scheduler wake-up still caused a systematic under-rate.
# It also showed that one long sleep followed by a short spin was unsuitable for
# this VM. This runner therefore evaluates a third strategy: repeated short
# sleeps followed by a bounded final busy-wait.
#
# The script intentionally does not start BMv2, create namespaces, or modify
# repository files. Every probe sends real UDP datagrams to an isolated local
# loopback sink and writes its evidence outside the repository.

set +e
set +o errexit 2>/dev/null || true
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CALIBRATION_TOOL="$SCRIPT_DIR/s2_sender_calibration.py"

OUTPUT_DIR="${1:-}"

if [[ -z "$OUTPUT_DIR" ]]; then
  echo "usage: $0 OUTPUT_DIR" >&2
  exit 2
fi

mkdir -p "$OUTPUT_DIR/runs"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

# Match the project interpreter whenever possible so that the evidence records
# the same Python runtime used by the S2 smoke test.
if [[ -x "$HOME/l2i-dev/venv/bin/python" ]]; then
  PYTHON_BIN="$HOME/l2i-dev/venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

# Defaults reproduce the Phase 12 packet workload. Five repetitions provide a
# stronger stability gate than the three-repetition foundation matrix.
RATE_MBPS="${PHASE13_REF_RATE_MBPS:-2}"
DURATION_S="${PHASE13_REF_DURATION_S:-3}"
PACKET_SIZE="${PHASE13_REF_PACKET_SIZE:-1200}"
REPETITIONS="${PHASE13_REF_REPETITIONS:-5}"
SPIN_THRESHOLDS_US="${PHASE13_REF_SPIN_THRESHOLDS_US:-300 600 900 1200}"
RT_PRIORITY="${PHASE13_REF_RT_PRIORITY:-20}"
PROBE_TIMEOUT_S="${PHASE13_REF_PROBE_TIMEOUT_S:-45}"

MANIFEST="$OUTPUT_DIR/manifest.tsv"
RUNNER_LOG="$OUTPUT_DIR/runner.txt"
PREFLIGHT="$OUTPUT_DIR/preflight.txt"
ANALYSIS_JSON="$OUTPUT_DIR/calibration-summary.json"
ANALYSIS_LOG="$OUTPUT_DIR/calibration-analysis.txt"

# Select a CPU from the current allowed affinity mask instead of assuming that
# virtual CPU identifiers are contiguous or unrestricted by the hypervisor.
CALIBRATION_CPU="$($PYTHON_BIN - <<'PY'
import os

allowed = sorted(os.sched_getaffinity(0))

if not allowed:
    raise SystemExit("no CPU is available in the current affinity mask")

print(allowed[-1])
PY
)"

# Root probes match the privilege context of the production S2 orchestrator.
# Acquiring credentials once also prevents password prompts from contaminating
# probe timing.
sudo -v
sudo_status=$?

if [[ "$sudo_status" -ne 0 ]]; then
  echo "PHASE13_REFINEMENT_SUDO_AVAILABLE=False" | tee "$RUNNER_LOG"
  exit 1
fi

# Real-time scheduling is optional. CPU-affined SCHED_OTHER remains the primary
# portability target; SCHED_FIFO is retained only as a comparative control.
REALTIME_AVAILABLE=0

if command -v chrt >/dev/null 2>&1 \
   && sudo chrt -f "$RT_PRIORITY" true >/dev/null 2>&1
then
  REALTIME_AVAILABLE=1
fi

# Validate thresholds before launching any timed probe. The upper bound keeps
# the requested spin window below the 4.8 ms nominal interval of the default
# workload and prevents accidental full-interval busy waiting.
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
interval_us = (packet_size * 8.0 / (rate_mbps * 1_000_000.0)) * 1_000_000.0

print(int(0.0 <= threshold_us < interval_us))
PY
)"

  if [[ "$threshold_ok" != "1" ]]; then
    echo \
      "spin threshold must be non-negative and smaller than the nominal interval: $threshold us" \
      >&2
    exit 2
  fi

  validated_thresholds+=("$threshold")
done

if [[ "${#validated_thresholds[@]}" -eq 0 ]]; then
  echo "at least one spin threshold is required" >&2
  exit 2
fi

{
  echo "===== REFINEMENT PARAMETERS ====="
  echo "repo_dir=$REPO_DIR"
  echo "python_bin=$PYTHON_BIN"
  echo "rate_mbps=$RATE_MBPS"
  echo "duration_s=$DURATION_S"
  echo "packet_size=$PACKET_SIZE"
  echo "repetitions=$REPETITIONS"
  echo "spin_thresholds_us=${validated_thresholds[*]}"
  echo "rt_priority=$RT_PRIORITY"
  echo "calibration_cpu=$CALIBRATION_CPU"
  echo "realtime_available=$REALTIME_AVAILABLE"

  echo
  echo "===== SYSTEM ====="
  echo "hostname=$(hostname)"
  echo "kernel=$(uname -r)"
  echo "virtualization=$(systemd-detect-virt 2>/dev/null || echo unknown)"
  echo "boot_id=$(cat /proc/sys/kernel/random/boot_id)"
  echo "uptime=$(uptime -p)"

  echo
  echo "===== CPU ====="
  nproc
  lscpu 2>/dev/null || true

  echo
  echo "===== SCHEDULER ====="
  chrt -p $$ 2>/dev/null || true
  taskset -pc $$ 2>/dev/null || true
  grep -E 'Cpus_allowed|Mems_allowed' /proc/self/status || true

  echo
  echo "===== CGROUP CPU LIMIT ====="
  cat /sys/fs/cgroup/cpu.max 2>/dev/null || echo "cpu.max unavailable"
  cat /sys/fs/cgroup/cpu.weight 2>/dev/null || echo "cpu.weight unavailable"

  echo
  echo "===== REAL-TIME LIMITS ====="
  sysctl kernel.sched_rt_period_us 2>/dev/null || true
  sysctl kernel.sched_rt_runtime_us 2>/dev/null || true
  ulimit -r 2>/dev/null || true

  echo
  echo "===== TIMER ====="
  "$PYTHON_BIN" - <<'PY'
import time

for name in ("monotonic", "perf_counter", "process_time"):
    info = time.get_clock_info(name)
    print(
        f"{name}: implementation={info.implementation} "
        f"resolution={info.resolution} monotonic={info.monotonic}"
    )
PY
} | tee "$PREFLIGHT"

# The manifest remains compatible with the independent analyzer. Configuration
# names encode the threshold, while each JSON file records its numeric value.
printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
  configuration \
  pacing_mode \
  scheduling_mode \
  repetition \
  output_path \
  exit_status \
  > "$MANIFEST"

probe_failures=0

run_configuration() {
  local configuration="$1"
  local pacing_mode="$2"
  local scheduling_mode="$3"
  local spin_threshold_us="$4"
  local repetition

  for repetition in $(seq 1 "$REPETITIONS"); do
    local output="$OUTPUT_DIR/runs/${configuration}-rep${repetition}.json"
    local log="$OUTPUT_DIR/runs/${configuration}-rep${repetition}.log"
    local status

    local probe=(
      "$PYTHON_BIN"
      "$CALIBRATION_TOOL"
      probe
      --mode "$pacing_mode"
      --configuration "$configuration"
      --scheduling-mode "$scheduling_mode"
      --repetition "$repetition"
      --rate-mbps "$RATE_MBPS"
      --duration "$DURATION_S"
      --packet-size "$PACKET_SIZE"
      --spin-threshold-us "$spin_threshold_us"
      --output "$output"
    )

    echo \
      "PHASE13_REFINEMENT_RUN_BEGIN=${configuration}:rep${repetition}" \
      | tee -a "$RUNNER_LOG"

    case "$scheduling_mode" in
      affinity)
        timeout "$PROBE_TIMEOUT_S" \
          sudo env PYTHONDONTWRITEBYTECODE=1 \
          taskset -c "$CALIBRATION_CPU" \
          "${probe[@]}" \
          > "$log" 2>&1
        status=$?
        ;;

      realtime-affinity)
        timeout "$PROBE_TIMEOUT_S" \
          sudo env PYTHONDONTWRITEBYTECODE=1 \
          chrt -f "$RT_PRIORITY" \
          taskset -c "$CALIBRATION_CPU" \
          "${probe[@]}" \
          > "$log" 2>&1
        status=$?
        ;;

      *)
        echo "unsupported refinement scheduling mode: $scheduling_mode" > "$log"
        status=2
        ;;
    esac

    # Probe processes run as root. Returning ownership after every repetition
    # guarantees that partial evidence remains inspectable after interruption.
    sudo chown -R "$(id -un):$(id -gn)" "$OUTPUT_DIR"

    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$configuration" \
      "$pacing_mode" \
      "$scheduling_mode" \
      "$repetition" \
      "$output" \
      "$status" \
      >> "$MANIFEST"

    cat "$log" | tee -a "$RUNNER_LOG"

    echo \
      "PHASE13_REFINEMENT_RUN_STATUS=${configuration}:rep${repetition}:$status" \
      | tee -a "$RUNNER_LOG"

    if [[ "$status" -ne 0 ]]; then
      probe_failures=$((probe_failures + 1))
    fi
  done
}

# Re-run the strongest foundation baseline in the same boot and load context so
# that threshold candidates are not compared only with historical evidence.
run_configuration \
  current-affinity \
  current_repeated_sleep \
  affinity \
  0

if [[ "$REALTIME_AVAILABLE" -eq 1 ]]; then
  run_configuration \
    current-rt-affinity \
    current_repeated_sleep \
    realtime-affinity \
    0
else
  echo "PHASE13_REFINEMENT_CURRENT_RT_AFFINITY_SKIPPED=True" \
    | tee -a "$RUNNER_LOG"
fi

# Sweep the final spin window. Affinity is the primary target because it does
# not require a real-time policy. Real-time affinity is collected only when the
# VM supports it, allowing the scheduler contribution to be quantified.
for threshold in "${validated_thresholds[@]}"; do
  token="${threshold//./p}"

  run_configuration \
    "tailspin-${token}us-affinity" \
    repeated_sleep_spin \
    affinity \
    "$threshold"

  if [[ "$REALTIME_AVAILABLE" -eq 1 ]]; then
    run_configuration \
      "tailspin-${token}us-rt-affinity" \
      repeated_sleep_spin \
      realtime-affinity \
      "$threshold"
  fi
done

# Candidate absence remains a valid experimental result. The analyzer returns
# nonzero only for malformed or incomplete evidence, not for a failed gate.
"$PYTHON_BIN" \
  "$CALIBRATION_TOOL" \
  analyze \
  --manifest "$MANIFEST" \
  --output "$ANALYSIS_JSON" \
  --expected-repetitions "$REPETITIONS" \
  --max-abs-rate-error-pct 5.0 \
  --max-rate-cv 0.05 \
  --min-inter-send-ratio 0.98 \
  --max-cpu-ratio 0.35 \
  2>&1 \
  | tee "$ANALYSIS_LOG"

analysis_status=${PIPESTATUS[0]}

{
  echo "PHASE13_REFINEMENT_PROBE_FAILURE_COUNT=$probe_failures"
  echo "PHASE13_REFINEMENT_ANALYSIS_EXIT_STATUS=$analysis_status"

  if [[ "$probe_failures" -eq 0 && "$analysis_status" -eq 0 ]]; then
    echo "PHASE13_REFINEMENT_RUNNER_OK"
  else
    echo "PHASE13_REFINEMENT_RUNNER_FAILED"
  fi
} | tee -a "$RUNNER_LOG"

if [[ "$probe_failures" -eq 0 && "$analysis_status" -eq 0 ]]; then
  exit 0
fi

exit 1
