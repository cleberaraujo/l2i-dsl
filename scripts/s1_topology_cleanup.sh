#!/usr/bin/env bash
set -Eeuo pipefail

# Remove every object created by the canonical S1 topology.  Every operation is
# idempotent because cleanup also runs after interrupted or failed executions.

terminate_namespace_processes() {
  local namespace="$1"
  local attempt
  local -a pids=()

  mapfile -t pids < <(ip netns pids "$namespace" 2>/dev/null || true)
  if [[ "${#pids[@]}" -eq 0 ]]; then
    return
  fi

  kill -TERM "${pids[@]}" 2>/dev/null || true
  for attempt in {1..20}; do
    mapfile -t pids < <(ip netns pids "$namespace" 2>/dev/null || true)
    if [[ "${#pids[@]}" -eq 0 ]]; then
      return
    fi
    sleep 0.05
  done
  kill -KILL "${pids[@]}" 2>/dev/null || true
  for attempt in {1..20}; do
    mapfile -t pids < <(ip netns pids "$namespace" 2>/dev/null || true)
    if [[ "${#pids[@]}" -eq 0 ]]; then
      return
    fi
    sleep 0.05
  done
  echo "[S1][error] Processes remain in namespace $namespace: ${pids[*]}" >&2
  return 1
}

for namespace in h1 h2 h3; do
  terminate_namespace_processes "$namespace"
  ip netns del "$namespace" 2>/dev/null || true
done

for interface in \
  h1-eth0-br \
  h2-eth0-br \
  h3-eth0-br \
  s1-ab-a \
  s1-ab-b \
  s1-bc-b \
  s1-bc-c
do
  ip link del "$interface" 2>/dev/null || true
done

for bridge in brA brB brC; do
  ip link del "$bridge" 2>/dev/null || true
done

echo "[S1] Canonical topology removed."
