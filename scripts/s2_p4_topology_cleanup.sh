#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "[S2-P4-clean][erro] Execute como root." >&2
  exit 1
fi

for ns in h1 h2 h3 h4; do
  ip netns del "$ns" 2>/dev/null || true
done

for bridge in p4s2b0 p4s2b1 p4s2b2 p4s2b3; do
  ip link del "$bridge" 2>/dev/null || true
done

for dev in s2b-h1 s2b-h2 s2b-h3 s2b-h4; do
  ip link del "$dev" 2>/dev/null || true
done

# Os peers persistentes devem sobreviver e permanecer disponíveis ao BMv2.
for port in 0 1 2 3; do
  peer="veth${port}-peer"
  if ip link show "$peer" >/dev/null 2>&1; then
    ip link set "$peer" nomaster 2>/dev/null || true
    ip link set "$peer" up 2>/dev/null || true
  fi
done

echo "S2_P4_TOPOLOGY_CLEANUP_OK"
