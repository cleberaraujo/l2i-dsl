#!/usr/bin/env bash
set -euo pipefail

echo "[CLEANUP] Iniciando limpeza de namespaces e interfaces virtuais..."

# ==============================================================================
# 1. Remove namespaces usados nos cenários
# ==============================================================================

for ns in h1 h3 h5; do
  if ip netns list | grep -q "$ns"; then
    echo "[CLEANUP] Removendo namespace $ns..."
    ip netns del "$ns" 2>/dev/null || true
  fi
done

# ==============================================================================
# 2. Remove bridges usadas nos domínios
# ==============================================================================

for br in brA brB brC; do
  if ip link show "$br" &>/dev/null; then
    echo "[CLEANUP] Removendo bridge $br..."
    ip link del "$br" type bridge 2>/dev/null || true
  fi
done

# ==============================================================================
# 3. Remove interfaces veth conhecidas por nome fixo
# ==============================================================================

KNOWN_IFACES=(
  A_B_A A_B_B A_C_A A_C_C
  tapA1 tapB3 tapC5
  A-B B-A B-C C-B
  C-A A-C
)

for ifc in "${KNOWN_IFACES[@]}"; do
  if ip link show "$ifc" &>/dev/null; then
    echo "[CLEANUP] Removendo interface $ifc..."
    ip link del "$ifc" 2>/dev/null || true
  fi
done

# ==============================================================================
# 4. Remove dinamicamente interfaces temporárias (geradas por testes)
#    - tap_*  (ex: tap_h3-eth0, tap_h5-eth0)
#    - *_@*   (pares veth com nome composto)
#    - *_eth* (algumas criadas em bridges)
# ==============================================================================

for ifc in $(ip -o link show | awk -F': ' '{print $2}' | grep -E '(^tap_|@|A-|B-|C-)' | cut -d'@' -f1); do
  if [[ "$ifc" != "enp"* && "$ifc" != "lo" ]]; then
    echo "[CLEANUP] Removendo interface residual $ifc..."
    ip link del "$ifc" 2>/dev/null || true
  fi
done

# ==============================================================================
# 5. Remove links órfãos que sobraram (sem par veth correspondente)
# ==============================================================================

echo "[CLEANUP] Verificando links órfãos..."
orphans=$(ip -o link show | grep -E "state DOWN" | grep -E "veth|tap|A-|B-|C-" | awk -F': ' '{print $2}' | cut -d'@' -f1)
for ifc in $orphans; do
  echo "[CLEANUP] Removendo link órfão $ifc..."
  ip link del "$ifc" 2>/dev/null || true
done

# ==============================================================================
# 6. Limpeza final e status
# ==============================================================================

echo "[CLEANUP] Limpeza concluída. Estado atual das interfaces:"
ip -brief link show

echo "[CLEANUP] Namespaces restantes:"
ip netns list || true

sudo ip netns del h1 2>/dev/null; sudo ip netns del h2 2>/dev/null; sudo ip netns del h3 2>/dev/null
sudo ip link del brA 2>/dev/null; sudo ip link del brB 2>/dev/null; sudo ip link del brC 2>/dev/null
sudo ip link del A-B 2>/dev/null; sudo ip link del B-A 2>/dev/null
sudo ip link del B-C 2>/dev/null; sudo ip link del C-B 2>/dev/null
sudo ip link del tap_h1-eth0 2>/dev/null; sudo ip link del tap_h2-eth0 2>/dev/null; sudo ip link del tap_h3-eth0 2>/dev/null
