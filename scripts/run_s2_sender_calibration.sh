#!/usr/bin/env bash
# Run a reproducible pacing calibration matrix for the S2 UDP sender.
#
# The runner compares the Phase 12 pacing implementation with a hybrid
# sleep-and-spin candidate under normal, CPU-affined, and real-time scheduling.
# It does not start BMv2, create namespaces, or modify repository files.

set +e
set +o errexit 2>/dev/null || true
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CALIBRATION_TOOL="$SCRIPT_DIR/s2_sender_calibration.py"

# The caller supplies an evidence directory. Keeping all outputs outside the
# repository preserves a clean worktree and simplifies artifact packaging.
OUTPUT_DIR="${1:-}"

if [[ -z "$OUTPUT_DIR" ]]; then
  echo "usage: $0 OUTPUT_DIR" >&2
  exit 2
fi

mkdir -p "$OUTPUT_DIR/runs"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

# Use the project virtual environment when available. The tool uses only the
# Python standard library, but matching the project interpreter improves
# provenance and reproducibility.
if [[ -x "$HOME/l2i-dev/venv/bin/python" ]]; then
  PYTHON_BIN="$HOME/l2i-dev/venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

# Calibration parameters may be overridden by environment variables. Defaults
# reproduce the Phase 12 multicast smoke workload exactly.
RATE_MBPS="${PHASE13_CAL_RATE_MBPS:-2}"
DURATION_S="${PHASE13_CAL_DURATION_S:-3}"
PACKET_SIZE="${PHASE13_CAL_PACKET_SIZE:-1200}"
REPETITIONS="${PHASE13_CAL_REPETITIONS:-3}"
SPIN_THRESHOLD_US="${PHASE13_CAL_SPIN_THRESHOLD_US:-300}"
RT_PRIORITY="${PHASE13_CAL_RT_PRIORITY:-20}"
PROBE_TIMEOUT_S="${PHASE13_CAL_PROBE_TIMEOUT_S:-45}"

MANIFEST="$OUTPUT_DIR/manifest.tsv"
RUNNER_LOG="$OUTPUT_DIR/runner.txt"
PREFLIGHT="$OUTPUT_DIR/preflight.txt"
ANALYSIS_JSON="$OUTPUT_DIR/calibration-summary.json"
ANALYSIS_LOG="$OUTPUT_DIR/calibration-analysis.txt"

# Select the highest CPU currently allowed by the process affinity mask. This
# avoids assuming that virtual CPU numbering is contiguous or unrestricted.
CALIBRATION_CPU="$($PYTHON_BIN - <<'PY'
import os

allowed = sorted(os.sched_getaffinity(0))

if not allowed:
    raise SystemExit("no CPU is available in the current affinity mask")

print(allowed[-1])
PY
)"

# Acquire sudo credentials once so that each probe can run non-interactively.
# The production S2 orchestrator also runs as root, so root probes more closely
# match the scheduler and system-call context of the real experiment.
sudo -v
sudo_status=$?

if [[ "$sudo_status" -ne 0 ]]; then
  echo "PHASE13_CALIBRATION_SUDO_AVAILABLE=False" | tee "$RUNNER_LOG"
  exit 1
fi

# Check whether the VM permits SCHED_FIFO. Some hypervisors or containers remove
# CAP_SYS_NICE even for root; this is recorded rather than treated as a failure.
REALTIME_AVAILABLE=0

if command -v chrt >/dev/null 2>&1 \
   && sudo chrt -f "$RT_PRIORITY" true >/dev/null 2>&1
then
  REALTIME_AVAILABLE=1
fi

{
  echo "===== CALIBRATION PARAMETERS ====="
  echo "repo_dir=$REPO_DIR"
  echo "python_bin=$PYTHON_BIN"
  echo "rate_mbps=$RATE_MBPS"
  echo "duration_s=$DURATION_S"
  echo "packet_size=$PACKET_SIZE"
  echo "repetitions=$REPETITIONS"
  echo "spin_threshold_us=$SPIN_THRESHOLD_US"
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
  echo "===== CURRENT SCHEDULER ====="
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

# The manifest is the only input used by the independent analyzer. Absolute
# paths make the evidence portable within an archived VM filesystem.
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
      --spin-threshold-us "$SPIN_THRESHOLD_US"
      --output "$output"
    )

    echo \
      "PHASE13_CALIBRATION_RUN_BEGIN=${configuration}:rep${repetition}" \
      | tee -a "$RUNNER_LOG"

    case "$scheduling_mode" in
      normal)
        timeout "$PROBE_TIMEOUT_S" \
          sudo env PYTHONDONTWRITEBYTECODE=1 \
          "${probe[@]}" \
          > "$log" 2>&1
        status=$?
        ;;

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
        echo "unknown scheduling mode: $scheduling_mode" > "$log"
        status=2
        ;;
    esac

    # Root creates the probe outputs. Return ownership to the repository user so
    # that evidence can be inspected, archived, and removed without sudo.
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
      "PHASE13_CALIBRATION_RUN_STATUS=${configuration}:rep${repetition}:$status" \
      | tee -a "$RUNNER_LOG"

    if [[ "$status" -ne 0 ]]; then
      probe_failures=$((probe_failures + 1))
    fi
  done
}

# Run the exact current implementation first so that all candidates are compared
# with a same-boot, same-load baseline.
run_configuration \
  current-normal \
  current_repeated_sleep \
  normal

run_configuration \
  current-affinity \
  current_repeated_sleep \
  affinity

if [[ "$REALTIME_AVAILABLE" -eq 1 ]]; then
  run_configuration \
    current-rt-affinity \
    current_repeated_sleep \
    realtime-affinity
else
  echo "PHASE13_CALIBRATION_CURRENT_RT_AFFINITY_SKIPPED=True" \
    | tee -a "$RUNNER_LOG"
fi

# Compare the hybrid candidate under the same scheduler configurations.
run_configuration \
  hybrid-normal \
  hybrid_sleep_spin \
  normal

run_configuration \
  hybrid-affinity \
  hybrid_sleep_spin \
  affinity

if [[ "$REALTIME_AVAILABLE" -eq 1 ]]; then
  run_configuration \
    hybrid-rt-affinity \
    hybrid_sleep_spin \
    realtime-affinity
else
  echo "PHASE13_CALIBRATION_HYBRID_RT_AFFINITY_SKIPPED=True" \
    | tee -a "$RUNNER_LOG"
fi

# The analyzer uses explicit gates. Candidate absence is recorded as a result
# and does not make the analysis command fail.
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
  echo "PHASE13_CALIBRATION_PROBE_FAILURE_COUNT=$probe_failures"
  echo "PHASE13_CALIBRATION_ANALYSIS_EXIT_STATUS=$analysis_status"

  if [[ "$probe_failures" -eq 0 && "$analysis_status" -eq 0 ]]; then
    echo "PHASE13_CALIBRATION_RUNNER_OK"
  else
    echo "PHASE13_CALIBRATION_RUNNER_FAILED"
  fi
} | tee -a "$RUNNER_LOG"

if [[ "$probe_failures" -eq 0 && "$analysis_status" -eq 0 ]]; then
  exit 0
fi

exit 1
