#!/usr/bin/env bash
set -e

echo "[S2-clean] Limpando topologia S2..."

ip netns del h1 2>/dev/null || true
ip netns del h2 2>/dev/null || true
ip netns del h3 2>/dev/null || true
ip netns del h4 2>/dev/null || true

ip link del veth-h1 2>/dev/null || true
ip link del veth-h2 2>/dev/null || true
ip link del veth-h3 2>/dev/null || true
ip link del veth-h4 2>/dev/null || true

ip link del brs2 2>/dev/null || true

echo "[S2-clean] OK."
