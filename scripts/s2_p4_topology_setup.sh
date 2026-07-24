#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "[S2-P4-setup][erro] Execute como root." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$SCRIPT_DIR/s2_p4_topology_cleanup.sh" >/dev/null 2>&1 || true

readonly -a NAMESPACES=(h1 h2 h3 h4)
readonly -a ADDRESSES=(10.0.0.1 10.0.0.2 10.0.0.3 10.0.0.4)
readonly -a P4_PORTS=(0 3 1 2)

for port in 0 1 2 3; do
  for dev in "veth${port}" "veth${port}-peer"; do
    if ! ip link show "$dev" >/dev/null 2>&1; then
      echo "[S2-P4-setup][erro] Interface persistente ausente: $dev" >&2
      echo "[S2-P4-setup][erro] Inicie o BMv2 antes de criar a topologia." >&2
      exit 1
    fi
  done
done

create_attachment() {
  local ns="$1"
  local ipaddr="$2"
  local port="$3"
  local bridge="p4s2b${port}"
  local ns_end="s2-${ns}"
  local root_end="s2b-${ns}"
  local p4_peer="veth${port}-peer"

  echo "[S2-P4-setup] ${ns} (${ipaddr}) <-> BMv2 port ${port}"

  ip netns add "$ns"
  ip link add "$ns_end" type veth peer name "$root_end"
  ip link set "$ns_end" netns "$ns"

  ip link add "$bridge" type bridge
  ip link set dev "$bridge" type bridge stp_state 0 forward_delay 0 mcast_snooping 0
  ip link set "$bridge" up

  ip link set "$p4_peer" nomaster 2>/dev/null || true
  ip link set "$p4_peer" master "$bridge"
  ip link set "$p4_peer" up

  ip link set "$root_end" master "$bridge"
  ip link set "$root_end" up

  ip -n "$ns" link set lo up
  ip -n "$ns" link set "$ns_end" name "${ns}-eth0"
  ip -n "$ns" link set "${ns}-eth0" up
  ip -n "$ns" addr add "${ipaddr}/24" dev "${ns}-eth0"
}

for i in "${!NAMESPACES[@]}"; do
  create_attachment \
    "${NAMESPACES[$i]}" \
    "${ADDRESSES[$i]}" \
    "${P4_PORTS[$i]}"
done

# A fonte deve enviar o grupo multicast pela interface conectada ao BMv2.
ip -n h1 route replace 239.1.1.1/32 dev h1-eth0

for ns in "${NAMESPACES[@]}"; do
  ip -n "$ns" -br addr show
done

echo "S2_P4_TOPOLOGY_SETUP_OK"
echo "S2_P4_SOURCE_PORT=0"
echo "S2_P4_RECEIVER_B_PORT=1"
echo "S2_P4_RECEIVER_C_PORT=2"
echo "S2_P4_SUPPORT_PORT=3"
