#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "[CLEANUP][erro] Execute este script como root." >&2
  exit 1
fi

echo "[CLEANUP] Iniciando limpeza seletiva de namespaces e interfaces L2I..."

# veth0..veth3 pertencem ao serviço persistente BMv2. A limpeza de topologias
# de cenário nunca deve removê-las. O ciclo de vida dessas interfaces é tratado
# exclusivamente por p4_build_and_run.sh / p4_stop.sh.
readonly -a PROTECTED_IFACES=(
  veth0 veth0-peer
  veth1 veth1-peer
  veth2 veth2-peer
  veth3 veth3-peer
)

is_protected_iface() {
  local candidate="$1"
  local protected

  for protected in "${PROTECTED_IFACES[@]}"; do
    if [[ "$candidate" == "$protected" ]]; then
      return 0
    fi
  done

  return 1
}

delete_link_if_present() {
  local dev="$1"

  if is_protected_iface "$dev"; then
    echo "[CLEANUP] Preservando interface do BMv2: $dev"
    return 0
  fi

  if ip link show "$dev" >/dev/null 2>&1; then
    echo "[CLEANUP] Removendo interface $dev..."
    ip link del "$dev" 2>/dev/null || true
  fi
}

delete_namespace_if_present() {
  local ns="$1"

  if ip netns list | awk '{print $1}' | grep -Fxq "$ns"; then
    echo "[CLEANUP] Removendo namespace $ns..."
    ip netns del "$ns" 2>/dev/null || true
  fi
}

# Namespaces conhecidos dos cenários canônicos, variantes históricas e teste
# isolado do backend Linux TC.
for ns in h1 h2 h3 h4 h5 l2i-tc-test; do
  delete_namespace_if_present "$ns"
done

# Bridges conhecidas. Não há descoberta genérica de bridges para evitar tocar
# em dispositivos externos ao artefato.
for br in brA brB brC br-s1 br-s2 brs2 p4s2b0 p4s2b1 p4s2b2 p4s2b3; do
  delete_link_if_present "$br"
done

# Interfaces com nomes fixos usadas pelas topologias e versões históricas.
readonly -a KNOWN_IFACES=(
  A_B_A A_B_B A_C_A A_C_C
  tapA1 tapB3 tapC5
  A-B B-A B-C C-B C-A A-C
  tap_h1-eth0 tap_h2-eth0 tap_h3-eth0 tap_h4-eth0 tap_h5-eth0
  h1-eth0-br h2-eth0-br h3-eth0-br h4-eth0-br h5-eth0-br
  veth-br-h1 veth-br-h2 veth-br-h3 veth-br-h4 veth-br-h5
  l2itc-host
  s2b-h1 s2b-h2 s2b-h3 s2b-h4
)

for dev in "${KNOWN_IFACES[@]}"; do
  delete_link_if_present "$dev"
done

# Descoberta residual restrita a padrões pertencentes ao artefato. A antiga
# regra baseada em '@' alcançava todo par veth do host e, por isso, removia
# indevidamente as interfaces persistentes do BMv2.
mapfile -t residual_ifaces < <(
  ip -o link show \
    | awk -F': ' '{print $2}' \
    | cut -d'@' -f1 \
    | awk '
        /^tap_/ ||
        /^(A|B|C)-/ ||
        /^h[0-9]+-eth[0-9]+-br$/ ||
        /^veth-br-h[0-9]+$/ ||
        /^l2itc-/ ||
        /^s2b-h[1-4]$/ ||
        /^p4s2b[0-3]$/
      ' \
    | sort -u
)

for dev in "${residual_ifaces[@]}"; do
  [[ -n "$dev" ]] || continue
  delete_link_if_present "$dev"
done

echo "[CLEANUP] Limpeza seletiva concluída."

echo "[CLEANUP] Estado das interfaces protegidas do BMv2:"
for dev in "${PROTECTED_IFACES[@]}"; do
  if ip link show "$dev" >/dev/null 2>&1; then
    echo "[CLEANUP] P4_LINK_PRESERVED=$dev"
  else
    echo "[CLEANUP] P4_LINK_NOT_PRESENT=$dev"
  fi
done

echo "[CLEANUP] Namespaces restantes:"
ip netns list || true
