#!/usr/bin/env bash
set -e

echo "[S2-setup] Limpando restos antigos..."

# Remove namespaces (se existirem)
ip netns del h1 2>/dev/null || true
ip netns del h2 2>/dev/null || true
ip netns del h3 2>/dev/null || true
ip netns del h4 2>/dev/null || true

# Remove links/bridge antigos (se existirem)
ip link del veth-h1 2>/dev/null || true
ip link del veth-h2 2>/dev/null || true
ip link del veth-h3 2>/dev/null || true
ip link del veth-h4 2>/dev/null || true
ip link del brs2 2>/dev/null || true

echo "[S2-setup] Criando namespaces h1, h2, h3, h4..."
ip netns add h1
ip netns add h2
ip netns add h3
ip netns add h4

echo "[S2-setup] Criando bridge brs2..."
ip link add brs2 type bridge
ip link set brs2 up

############################################################
# Helper: cria veth(ns) <-> veth(br) e configura IP
############################################################
create_host () {
  local ns="$1"
  local ipaddr="$2"

  echo "[S2-setup] Criando par veth para ${ns}..."
  ip link add "veth-${ns}" type veth peer name "veth-br-${ns}"
  ip link set "veth-${ns}" netns "${ns}"

  ip link set "veth-br-${ns}" master brs2
  ip link set "veth-br-${ns}" up

  ip netns exec "${ns}" ip link set lo up

  # >>> RENOME CRÍTICO: veth-${ns} -> ${ns}-eth0 <<<
  ip netns exec "${ns}" ip link set "veth-${ns}" name "${ns}-eth0"
  ip netns exec "${ns}" ip link set "${ns}-eth0" up
  ip netns exec "${ns}" ip addr add "${ipaddr}/24" dev "${ns}-eth0"
}

# Endereçamento:
#  h1 = fonte
#  h2 = receptor (B opcional)
#  h3 = receptor B (10.0.0.3)   <- seu preflight_B já usa 10.0.0.3
#  h4 = receptor C (10.0.0.4)   <- seu preflight_C já usa 10.0.0.4
create_host h1 10.0.0.1
create_host h2 10.0.0.2
create_host h3 10.0.0.3
create_host h4 10.0.0.4

echo "[S2-setup] Topologia S2 criada."
echo "[S2-setup] Verifique com: ip -n h1 a ; ip -n h2 a ; ip -n h3 a ; ip -n h4 a"
