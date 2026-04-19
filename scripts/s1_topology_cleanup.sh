#!/usr/bin/env bash
set -euo pipefail

echo "[S1] Removendo namespaces e bridge..."

for ns in h1 h2 h3; do
  ip netns del "$ns" 2>/dev/null || true
done

ip link del br-s1 2>/dev/null || true

for dev in h1-eth0-br h2-eth0-br h3-eth0-br; do
  ip link del "$dev" 2>/dev/null || true
done

echo "[S1] Topologia S1 limpa."
