#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'USAGE'
Usage:
  setup_netconf_auth.sh [options]

Purpose:
  Prepare reproducible NETCONF-over-SSH authentication for Netopeer2 by:
    1) creating a dedicated system user (default: netconf),
    2) generating a client SSH key for the invoking user,
    3) authorizing that public key for the NETCONF user,
    4) printing the exact parameters to use in ncclient / project configs.

Options:
  --netconf-user USER     System account used by Netopeer2 auth (default: netconf)
  --client-user USER      Local client user who will own the SSH key (default: current user)
  --key-name NAME         SSH key filename under ~/.ssh/ (default: l2i_netconf_key)
  --host HOST             NETCONF target host to print in examples (default: 127.0.0.1)
  --port PORT             NETCONF SSH port to print in examples (default: 830)
  --force-keygen          Regenerate the client key even if it already exists
  --test                  After setup, run a Python ncclient connectivity test if ncclient is available
  -h, --help              Show this help message

Notes:
  - Run this script as a normal user. It will call sudo only where needed.
  - This script configures SSH authentication. It does not modify NACM policies.
    If the NETCONF session authenticates but edit-config is denied, configure NACM separately.
USAGE
}

NETCONF_USER="netconf"
CLIENT_USER="${SUDO_USER:-${USER}}"
KEY_NAME="l2i_netconf_key"
HOST="127.0.0.1"
PORT="830"
FORCE_KEYGEN=0
RUN_TEST=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --netconf-user)
      NETCONF_USER="$2"; shift 2 ;;
    --client-user)
      CLIENT_USER="$2"; shift 2 ;;
    --key-name)
      KEY_NAME="$2"; shift 2 ;;
    --host)
      HOST="$2"; shift 2 ;;
    --port)
      PORT="$2"; shift 2 ;;
    --force-keygen)
      FORCE_KEYGEN=1; shift ;;
    --test)
      RUN_TEST=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "[erro] Opção desconhecida: $1" >&2
      usage
      exit 1 ;;
  esac
done

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "[erro] Comando obrigatório não encontrado: $1" >&2
    exit 1
  }
}

need_cmd sudo
need_cmd ssh-keygen
need_cmd getent
need_cmd install
need_cmd awk

CLIENT_HOME="$(getent passwd "$CLIENT_USER" | awk -F: '{print $6}')"
if [[ -z "$CLIENT_HOME" || ! -d "$CLIENT_HOME" ]]; then
  echo "[erro] Não foi possível determinar o HOME do usuário cliente: $CLIENT_USER" >&2
  exit 1
fi

SSH_DIR="$CLIENT_HOME/.ssh"
KEY_PATH="$SSH_DIR/$KEY_NAME"
PUBKEY_PATH="$KEY_PATH.pub"
NETCONF_HOME="/home/$NETCONF_USER"
AUTHORIZED_KEYS="$NETCONF_HOME/.ssh/authorized_keys"

if ! id "$NETCONF_USER" >/dev/null 2>&1; then
  echo "[info] Criando usuário NETCONF: $NETCONF_USER"
  sudo useradd -m -s /bin/bash "$NETCONF_USER"
else
  echo "[info] Usuário NETCONF já existe: $NETCONF_USER"
fi

sudo mkdir -p "$NETCONF_HOME/.ssh"
sudo chmod 700 "$NETCONF_HOME/.ssh"
sudo chown -R "$NETCONF_USER:$NETCONF_USER" "$NETCONF_HOME/.ssh"

mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"

if [[ $FORCE_KEYGEN -eq 1 || ! -f "$KEY_PATH" || ! -f "$PUBKEY_PATH" ]]; then
  echo "[info] Gerando chave SSH do cliente em: $KEY_PATH"
  rm -f "$KEY_PATH" "$PUBKEY_PATH"
  ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "l2i-netconf@$(hostname)-$CLIENT_USER"
else
  echo "[info] Chave SSH do cliente já existe: $KEY_PATH"
fi

PUBKEY_CONTENT="$(cat "$PUBKEY_PATH")"
if sudo test -f "$AUTHORIZED_KEYS" && sudo grep -Fxq "$PUBKEY_CONTENT" "$AUTHORIZED_KEYS"; then
  echo "[info] Chave pública já autorizada para $NETCONF_USER"
else
  echo "[info] Autorizando chave pública para o usuário $NETCONF_USER"
  echo "$PUBKEY_CONTENT" | sudo tee -a "$AUTHORIZED_KEYS" >/dev/null
fi

sudo chmod 600 "$AUTHORIZED_KEYS"
sudo chown "$NETCONF_USER:$NETCONF_USER" "$AUTHORIZED_KEYS"

# Disable password login for the dedicated local account while keeping public-key auth usable.
sudo passwd -l "$NETCONF_USER" >/dev/null 2>&1 || true

FPRINT="$(ssh-keygen -lf "$PUBKEY_PATH" | awk '{print $2, $4}')"

echo
echo "[ok] Autenticação SSH preparada com sucesso."
echo "[ok] Usuário NETCONF : $NETCONF_USER"
echo "[ok] Usuário cliente : $CLIENT_USER"
echo "[ok] Chave privada   : $KEY_PATH"
echo "[ok] Chave pública   : $PUBKEY_PATH"
echo "[ok] Fingerprint     : $FPRINT"

echo
echo "[próximo passo] Use estes parâmetros no cliente NETCONF:"
echo "  host         = $HOST"
echo "  port         = $PORT"
echo "  username     = $NETCONF_USER"
echo "  key_filename = $KEY_PATH"
echo "  hostkey_verify = False"
echo
cat <<EOF2
Exemplo com ncclient:

from ncclient import manager
m = manager.connect(
    host="$HOST",
    port=$PORT,
    username="$NETCONF_USER",
    key_filename="$KEY_PATH",
    hostkey_verify=False,
    allow_agent=False,
    look_for_keys=False,
    timeout=10,
)
print(m.session_id)
m.close_session()
EOF2

echo
cat <<EOF3
Teste SSH puro (subsystem NETCONF):
  ssh -s -i "$KEY_PATH" -p $PORT \
      -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      "$NETCONF_USER@$HOST" netconf
EOF3

if [[ $RUN_TEST -eq 1 ]]; then
  echo
  echo "[info] Executando teste opcional via ncclient..."
  python3 - <<PY
import sys
try:
    from ncclient import manager
except Exception as e:
    print(f"[erro] ncclient indisponível no python3 atual: {e}")
    sys.exit(2)

try:
    m = manager.connect(
        host="$HOST",
        port=int("$PORT"),
        username="$NETCONF_USER",
        key_filename="$KEY_PATH",
        hostkey_verify=False,
        allow_agent=False,
        look_for_keys=False,
        timeout=10,
    )
    print(f"[ok] Sessão NETCONF estabelecida. session-id={m.session_id}")
    m.close_session()
except Exception as e:
    print(f"[erro] Falha no teste NETCONF: {e}")
    sys.exit(1)
PY
fi
