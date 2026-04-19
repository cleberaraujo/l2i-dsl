#!/usr/bin/env bash
set -e

CMD="${1:-up}"

BR="br-s1"
NS_LIST=("h1" "h2" "h3")

cleanup() {
  # Remove namespaces
  for ns in "${NS_LIST[@]}"; do
    ip netns del "$ns" 2>/dev/null || true
  done

  # Remove veths lado root
  ip link del veth-h1-br 2>/dev/null || true
  ip link del veth-h2-br 2>/dev/null || true
  ip link del veth-h3-br 2>/dev/null || true

  # Remove bridge
  ip link del "$BR" 2>/dev/null || true
}

if [ "$CMD" = "down" ]; then
  echo "[S1 topo] Limpando topologia..."
  cleanup
  echo "[S1 topo] ok (down)."
  exit 0
fi

echo "[S1 topo] Subindo topologia..."

# Limpa antes, para não dar conflito
cleanup

# Cria bridge L2 no root
ip link add "$BR" type bridge
ip link set "$BR" up

idx=1
for ns in "${NS_LIST[@]}"; do
  echo "[S1 topo] Criando namespace $ns..."

  ip netns add "$ns"
  ip netns exec "$ns" ip link set lo up

  # veth par: lado host (no ns) e lado bridge (no root)
  HOST_IF="veth-${ns}"
  BR_IF="veth-${ns}-br"

  ip link add "$HOST_IF" type veth peer name "$BR_IF"
  ip link set "$HOST_IF" netns "$ns"

  # IPs: h1=10.0.0.1/24, h2=10.0.0.2/24, h3=10.0.0.3/24
  IP="10.0.0.${idx}/24"

  ip netns exec "$ns" ip addr add "$IP" dev "$HOST_IF"
  ip netns exec "$ns" ip link set "$HOST_IF" up

  ip link set "$BR_IF" master "$BR"
  ip link set "$BR_IF" up

  idx=$((idx+1))
done

echo "[S1 topo] Topologia criada:"
ip netns list
echo "[S1 topo] Teste rápido: ping de h1 para h3..."
ip netns exec h1 ping -c 2 10.0.0.3 || echo "[S1 topo] Aviso: ping falhou (verifique manualmente)."

echo "[S1 topo] ok (up)."
