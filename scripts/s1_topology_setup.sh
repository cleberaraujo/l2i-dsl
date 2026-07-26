#!/usr/bin/env bash
set -Eeuo pipefail

# Canonical S1 data-plane topology.
#
# The topology deliberately separates the two traffic sources so their offered
# loads are independent before they reach the shared B->C bottleneck:
#
#   h1 sensitive source --+
#                        +-- brA -- A/B -- brB -- B/C -- brC -- h3 destination
#   h2 best-effort source+
#
# NETCONF and P4Runtime remain control-plane materialization targets in this
# experiment.  The forwarding path above is implemented with Linux bridges and
# veth pairs, so S1 must not attribute traffic-metric changes to NETCONF or P4.

readonly S1_NAMESPACES=(h1 h2 h3)
readonly S1_BRIDGES=(brA brB brC)
readonly S1_ROOT_INTERFACES=(
  h1-eth0-br
  h2-eth0-br
  h3-eth0-br
  s1-ab-a
  s1-ab-b
  s1-bc-b
  s1-bc-c
)

if ! command -v ethtool >/dev/null 2>&1; then
  echo "[S1][error] ethtool is required to control veth offloads." >&2
  exit 1
fi

disable_emulation_offloads() {
  local namespace="$1"
  local device="$2"
  local feature
  local state
  local -a command_prefix=()

  if [[ "$namespace" != "root" ]]; then
    command_prefix=(ip netns exec "$namespace")
  fi

  for feature in tso gso gro; do
    "${command_prefix[@]}" ethtool -K "$device" "$feature" off \
      >/dev/null 2>&1 || true
  done
  state="$("${command_prefix[@]}" ethtool -k "$device")"

  grep -Eq '^tcp-segmentation-offload: off( \[fixed\])?$' <<<"$state"
  grep -Eq '^generic-segmentation-offload: off( \[fixed\])?$' <<<"$state"
  grep -Eq '^generic-receive-offload: off( \[fixed\])?$' <<<"$state"
}

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

remove_previous_topology() {
  local namespace
  local bridge
  local interface

  # Namespace deletion also removes the namespace-side veth endpoint.
  for namespace in "${S1_NAMESPACES[@]}"; do
    terminate_namespace_processes "$namespace"
    ip netns del "$namespace" 2>/dev/null || true
  done

  # Deleting a veth endpoint removes its peer.  Explicit cleanup keeps the
  # script idempotent when a previous setup stopped between two commands.
  for interface in "${S1_ROOT_INTERFACES[@]}"; do
    ip link del "$interface" 2>/dev/null || true
  done

  for bridge in "${S1_BRIDGES[@]}"; do
    ip link del "$bridge" 2>/dev/null || true
  done
}

create_bridge() {
  local bridge="$1"

  ip link add "$bridge" type bridge
  ip link set "$bridge" type bridge stp_state 0
  ip link set "$bridge" up
}

attach_root_interface() {
  local interface="$1"
  local bridge="$2"

  ip link set "$interface" master "$bridge"
  ip link set "$interface" up
}

create_host() {
  local namespace="$1"
  local address="$2"
  local bridge="$3"
  local namespace_interface="${namespace}-eth0"
  local root_interface="${namespace}-eth0-br"

  ip netns add "$namespace"
  ip link add "$namespace_interface" type veth peer name "$root_interface"
  ip link set "$namespace_interface" netns "$namespace"

  ip netns exec "$namespace" ip link set lo up
  ip netns exec "$namespace" ip addr add "$address/24" dev "$namespace_interface"
  ip netns exec "$namespace" ip link set "$namespace_interface" up

  attach_root_interface "$root_interface" "$bridge"
  disable_emulation_offloads "$namespace" "$namespace_interface"
  disable_emulation_offloads root "$root_interface"
}

create_interdomain_link() {
  local left_interface="$1"
  local left_bridge="$2"
  local right_interface="$3"
  local right_bridge="$4"

  ip link add "$left_interface" type veth peer name "$right_interface"
  attach_root_interface "$left_interface" "$left_bridge"
  attach_root_interface "$right_interface" "$right_bridge"
  disable_emulation_offloads root "$left_interface"
  disable_emulation_offloads root "$right_interface"
}

main() {
  local bridge

  echo "[S1] Removing any previous canonical topology."
  remove_previous_topology

  echo "[S1] Creating the A, B, and C bridge segments."
  for bridge in "${S1_BRIDGES[@]}"; do
    create_bridge "$bridge"
  done

  echo "[S1] Creating two independent sources and one common destination."
  create_host h1 10.0.0.1 brA
  create_host h2 10.0.0.2 brA
  create_host h3 10.0.0.3 brC

  echo "[S1] Creating the A-B and B-C inter-domain links."
  create_interdomain_link s1-ab-a brA s1-ab-b brB
  create_interdomain_link s1-bc-b brB s1-bc-c brC

  echo "[S1] Canonical topology created."
  echo "PHASE19_S1_TOPOLOGY_THREE_SEGMENTS=True"
  echo "PHASE19_S1_TOPOLOGY_INDEPENDENT_SOURCES=True"
  echo "PHASE19_S1_TOPOLOGY_SHARED_BOTTLENECK_INTERFACE=s1-bc-b"
  echo "PHASE19_S1_TOPOLOGY_OFFLOADS_DISABLED=True"
  ip netns list
}

main "$@"
