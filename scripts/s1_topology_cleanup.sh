#!/usr/bin/env bash
set -Eeuo pipefail

# Remove every object created by the canonical S1 topology.  Every operation is
# idempotent because cleanup also runs after interrupted or failed executions.

for namespace in h1 h2 h3; do
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
