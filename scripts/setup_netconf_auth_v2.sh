#!/usr/bin/env bash
set -euo pipefail

# setup_netconf_auth_v2.sh
#
# Reproducible setup for NETCONF over SSH using Netopeer2.
#
# What it does:
#   - creates (or normalizes) a dedicated non-interactive system user
#   - assigns a dedicated home directory for SSH authorized_keys
#   - generates a client SSH keypair if missing
#   - installs the public key for the NETCONF user
#   - optionally starts netopeer2-server (if not already running)
#   - optionally tests a NETCONF session using ncclient, preferring the project venv
#
# Notes:
#   - The test validates authentication and NETCONF session establishment.
#   - It does NOT configure NACM/write permissions. A successful test only means
#     NETCONF-over-SSH authentication is working.

NETCONF_USER="netconf"
NETCONF_HOME="/var/lib/netconf"
KEY_PATH="${HOME}/.ssh/l2i_netconf_key"
PORT="830"
HOST="127.0.0.1"
SERVER_BIN="/usr/local/sbin/netopeer2-server"
DO_TEST=0
FORCE_REGEN=0

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --user <name>          NETCONF OS user (default: ${NETCONF_USER})
  --home <dir>           Home directory used to store authorized_keys (default: ${NETCONF_HOME})
  --key <path>           Client private key path (default: ${KEY_PATH})
  --host <addr>          NETCONF host for test (default: ${HOST})
  --port <port>          NETCONF port for test (default: ${PORT})
  --server-bin <path>    netopeer2-server path (default: ${SERVER_BIN})
  --force-regen-key      Re-generate the SSH key even if it already exists
  --test                 Run an end-to-end NETCONF test after setup
  -h, --help             Show this help
USAGE
}

log() { printf '[info] %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*"; }
err() { printf '[erro] %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) NETCONF_USER="$2"; shift 2 ;;
    --home) NETCONF_HOME="$2"; shift 2 ;;
    --key) KEY_PATH="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --server-bin) SERVER_BIN="$2"; shift 2 ;;
    --force-regen-key) FORCE_REGEN=1; shift ;;
    --test) DO_TEST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "Opção desconhecida: $1"; usage; exit 1 ;;
  esac
done

KEY_PATH="${KEY_PATH/#\~/$HOME}"
PUB_PATH="${KEY_PATH}.pub"
SSH_DIR="${NETCONF_HOME}/.ssh"
AUTH_KEYS="${SSH_DIR}/authorized_keys"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { err "Comando obrigatório não encontrado: $1"; exit 1; }
}

require_cmd sudo
require_cmd ssh-keygen
require_cmd getent
require_cmd useradd
require_cmd usermod
require_cmd install
require_cmd ss
require_cmd pkill

log "Configurando usuário dedicado de sistema para NETCONF: ${NETCONF_USER}"
if ! getent passwd "${NETCONF_USER}" >/dev/null; then
  sudo useradd \
    --system \
    --home-dir "${NETCONF_HOME}" \
    --shell /usr/sbin/nologin \
    --create-home \
    "${NETCONF_USER}"
  log "Usuário ${NETCONF_USER} criado."
else
  log "Usuário ${NETCONF_USER} já existe; normalizando shell/home."
  sudo usermod -d "${NETCONF_HOME}" -s /usr/sbin/nologin "${NETCONF_USER}"
fi

# lock password to avoid interactive password login
sudo passwd -l "${NETCONF_USER}" >/dev/null 2>&1 || true

log "Preparando diretório SSH do usuário NETCONF em ${SSH_DIR}"
sudo install -d -m 700 -o "${NETCONF_USER}" -g "${NETCONF_USER}" "${SSH_DIR}"

log "Preparando chave SSH do cliente em ${KEY_PATH}"
mkdir -p "$(dirname "${KEY_PATH}")"
if [[ ${FORCE_REGEN} -eq 1 || ! -f "${KEY_PATH}" || ! -f "${PUB_PATH}" ]]; then
  rm -f "${KEY_PATH}" "${PUB_PATH}"
  ssh-keygen -t ed25519 -f "${KEY_PATH}" -N "" -C "l2i-netconf@$(hostname)" >/dev/null
  log "Chave gerada com sucesso."
else
  log "Chave já existe; reutilizando."
fi

log "Instalando authorized_keys para ${NETCONF_USER}"
sudo install -m 600 -o "${NETCONF_USER}" -g "${NETCONF_USER}" "${PUB_PATH}" "${AUTH_KEYS}"

# Make sure parent home permissions are not too open for SSH pubkey auth.
sudo chown "${NETCONF_USER}:${NETCONF_USER}" "${NETCONF_HOME}"
sudo chmod 755 "${NETCONF_HOME}"

# Print identity summary for reproducibility
log "Resumo da identidade configurada:"
getent passwd "${NETCONF_USER}" | sed 's/^/[info]   passwd: /'
id "${NETCONF_USER}" | sed 's/^/[info]   id: /'
printf '[info]   key: %s\n' "${KEY_PATH}"
printf '[info]   pub: %s\n' "${PUB_PATH}"
printf '[info]   authorized_keys: %s\n' "${AUTH_KEYS}"

find_python_for_test() {
  local candidates=()
  if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    candidates+=("${VIRTUAL_ENV}/bin/python")
  fi
  if [[ -x "${HOME}/net-dev/venv/bin/python" ]]; then
    candidates+=("${HOME}/net-dev/venv/bin/python")
  fi
  candidates+=("python3")

  for py in "${candidates[@]}"; do
    if "$py" - <<'PY' >/dev/null 2>&1
import ncclient
PY
    then
      printf '%s' "$py"
      return 0
    fi
  done
  return 1
}

start_server_if_needed() {
  if ss -ltn | awk '{print $4}' | grep -qE "(^|:)${PORT}$"; then
    log "Há algo escutando na porta ${PORT}; não vou iniciar outro servidor."
    return 0
  fi

  if [[ ! -x "${SERVER_BIN}" ]]; then
    err "Servidor Netopeer2 não encontrado em ${SERVER_BIN}."
    return 1
  fi

  log "Iniciando ${SERVER_BIN} em background..."
  sudo "${SERVER_BIN}" -d >/tmp/l2i_netopeer2_setup.log 2>&1 || true

  for _ in $(seq 1 20); do
    if ss -ltn | awk '{print $4}' | grep -qE "(^|:)${PORT}$"; then
      log "Servidor NETCONF está escutando na porta ${PORT}."
      return 0
    fi
    sleep 1
  done

  err "Servidor Netopeer2 não abriu a porta ${PORT}. Últimas linhas do log:"
  tail -n 20 /tmp/l2i_netopeer2_setup.log >&2 || true
  return 1
}

if [[ ${DO_TEST} -eq 1 ]]; then
  log "Executando teste opcional via ncclient..."

  PYTHON_TEST="$(find_python_for_test || true)"
  if [[ -z "${PYTHON_TEST}" ]]; then
    err "ncclient indisponível. Ative a venv do projeto ou instale ncclient nela."
    err "Tentados: VIRTUAL_ENV, ~/net-dev/venv, python3 do sistema."
    exit 1
  fi
  log "Usando Python para o teste: ${PYTHON_TEST}"

  start_server_if_needed

  export L2I_NETCONF_TEST_HOST="${HOST}"
  export L2I_NETCONF_TEST_PORT="${PORT}"
  export L2I_NETCONF_TEST_USER="${NETCONF_USER}"
  export L2I_NETCONF_TEST_KEY="${KEY_PATH}"

  "${PYTHON_TEST}" - <<'PY'
import os
from ncclient import manager

host = os.environ['L2I_NETCONF_TEST_HOST']
port = int(os.environ['L2I_NETCONF_TEST_PORT'])
user = os.environ['L2I_NETCONF_TEST_USER']
key = os.path.expanduser(os.environ['L2I_NETCONF_TEST_KEY'])

m = manager.connect(
    host=host,
    port=port,
    username=user,
    key_filename=key,
    hostkey_verify=False,
    allow_agent=False,
    look_for_keys=False,
    timeout=10,
)
print('[ok] NETCONF over SSH autenticado com sucesso.')
print(f'[ok] Session ID: {m.session_id}')
print(f'[ok] Server capabilities: {len(list(m.server_capabilities))}')
m.close_session()
PY
fi

cat <<EOF2

[ok] Configuração concluída.

Parâmetros para ncclient:
  host='${HOST}'
  port=${PORT}
  username='${NETCONF_USER}'
  key_filename='${KEY_PATH}'
  hostkey_verify=False

Observação:
  - Este script valida autenticação e sessão NETCONF.
  - Para operações edit-config/get-config com permissões amplas, ainda pode ser necessário
    configurar NACM/políticas no servidor.
EOF2
