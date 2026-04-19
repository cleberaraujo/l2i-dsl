#!/usr/bin/env bash
set -euo pipefail

# Topologia S1:
#  h1 (10.0.0.1/24) ---+
#                       +--- br-s1 --- h2 (10.0.0.2/24)
#  h3 (10.0.0.3/24) ---+
#
# Com namespaces:
#  ns: h1, h2, h3
#  interfaces ns: h1-eth0, h2-eth0, h3-eth0
#  lado root:    h1-eth0-br, h2-eth0-br, h3-eth0-br

echo "[S1] Limpando resquícios antigos (se houver)..."
for ns in h1 h2 h3; do
  ip netns del "$ns" 2>/dev/null || true
done

ip link del br-s1 2>/dev/null || true
for dev in h1-eth0-br h2-eth0-br h3-eth0-br; do
  ip link del "$dev" 2>/dev/null || true
done

echo "[S1] Criando bridge br-s1..."
ip link add br-s1 type bridge
ip link set br-s1 up

create_host () {
  local ns="$1"
  local ipaddr="$2"

  echo "[S1] Criando namespace $ns com IP $ipaddr..."

  ip netns add "$ns"

  # veth par: ns <-> bridge
  ip link add "${ns}-eth0" type veth peer name "${ns}-eth0-br"

  # move uma ponta para o namespace
  ip link set "${ns}-eth0" netns "$ns"

  # configura IP dentro do ns
  ip netns exec "$ns" ip addr add "${ipaddr}/24" dev "${ns}-eth0"
  ip netns exec "$ns" ip link set "${ns}-eth0" up
  ip netns exec "$ns" ip link set lo up

  # conecta a outra ponta na bridge
  ip link set "${ns}-eth0-br" master br-s1
  ip link set "${ns}-eth0-br" up
}

create_host h1 10.0.0.1
create_host h2 10.0.0.2
create_host h3 10.0.0.3

echo "[S1] Topologia S1 criada com sucesso."
ip netns list
