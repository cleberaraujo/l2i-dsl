#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

P4SRC="${P4SRC:-$REPO_DIR/p4src/l2i_minimal.p4}"
OUTDIR="${P4_OUTDIR:-/tmp/l2i_minimal}"
JSON="${OUTDIR}/l2i_minimal.json"
P4INFO="${OUTDIR}/l2i_minimal.p4info.txtpb"
LOG="${BMV2_LOG_FILE:-${OUTDIR}/bmv2.log}"
PIDFILE="${BMV2_PIDFILE:-${OUTDIR}/bmv2.pid}"
THRIFT_PORT="${P4_THRIFT_PORT:-9090}"
GRPC_ADDR="${P4_GRPC_ADDR:-0.0.0.0:9559}"
LOG_CONSOLE="${BMV2_LOG_CONSOLE:-0}"
REQUIRE_THRIFT="${P4_REQUIRE_THRIFT:-0}"

as_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

echo "[prep] Criando diretório de saída: ${OUTDIR}"
mkdir -p "${OUTDIR}"

echo "[build] p4c-bm2-ss → ${JSON} / ${P4INFO}"
p4c-bm2-ss \
  -I "$REPO_DIR/p4src" \
  --p4runtime-file "${P4INFO}" \
  --p4runtime-format text \
  -o "${JSON}" \
  "${P4SRC}"

echo "[ok] JSON:   ${JSON}"
echo "[ok] P4INFO: ${P4INFO}"

# Cada porta do BMv2 usa um par veth independente. Quatro portas são mantidas
# para o S2: source (0), receiver B (1), receiver C (2) e tráfego de suporte (3).
readonly -a P4_PORT_IDS=(0 1 2 3)

ensure_independent_p4_links() {
  local complete=1
  local port dev peer

  for port in "${P4_PORT_IDS[@]}"; do
    dev="veth${port}"
    peer="veth${port}-peer"
    if ! ip link show "$dev" >/dev/null 2>&1 \
       || ! ip link show "$peer" >/dev/null 2>&1; then
      complete=0
      break
    fi
  done

  if [[ "$complete" -eq 1 ]]; then
    echo "[net] quatro pares independentes do BMv2 já existem (ok)"
    return 0
  fi

  echo "[net] recriando quatro pares independentes do BMv2"

  for port in "${P4_PORT_IDS[@]}"; do
    as_root ip link del "veth${port}" 2>/dev/null || true
    as_root ip link del "veth${port}-peer" 2>/dev/null || true
  done

  for port in "${P4_PORT_IDS[@]}"; do
    as_root ip link add "veth${port}" type veth peer name "veth${port}-peer"
  done
}

# Encerra a instância anterior antes de alterar as interfaces usadas por ela.
as_root "$SCRIPT_DIR/p4_stop.sh" >/dev/null 2>&1 || true
as_root rm -f "$PIDFILE"

ensure_independent_p4_links

for port in "${P4_PORT_IDS[@]}"; do
  as_root ip link set "veth${port}" up
  as_root ip link set "veth${port}-peer" up
done

# Começa com um log vazio. O rastreamento por pacote (--log-console) fica
# desabilitado por padrão.
: > "$LOG"

bmv2_cmd=(simple_switch_grpc)
for port in "${P4_PORT_IDS[@]}"; do
  bmv2_cmd+=("-i" "${port}@veth${port}")
done
bmv2_cmd+=(--device-id 0)

if [[ "$REQUIRE_THRIFT" == "1" ]]; then
  bmv2_cmd+=(--thrift-port "$THRIFT_PORT")
fi

if [[ "$LOG_CONSOLE" == "1" ]]; then
  bmv2_cmd+=(--log-console)
fi

bmv2_cmd+=(
  "$JSON"
  --
  --grpc-server-addr "$GRPC_ADDR"
)

# O PID gravado é o PID real do simple_switch_grpc, não o PID do processo sudo.
# nohup evita que o serviço receba SIGHUP quando a sessão SSH termina.
echo "[run] simple_switch_grpc em ${GRPC_ADDR} (device-id=0)"
if [[ "$REQUIRE_THRIFT" == "1" ]]; then
  echo "[run] Thrift obrigatório solicitado em :${THRIFT_PORT}"
else
  echo "[run] Thrift não requerido; P4Runtime é a interface canônica"
fi
echo "[run] log-console=${LOG_CONSOLE}"
as_root nohup sh -c '
  pidfile="$1"
  logfile="$2"
  shift 2
  printf "%s\n" "$$" > "$pidfile"
  exec "$@" > "$logfile" 2>&1
' sh \
  "$PIDFILE" \
  "$LOG" \
  "${bmv2_cmd[@]}" \
  </dev/null >/dev/null 2>&1 &

launcher_pid=$!

for _ in $(seq 1 50); do
  if [[ -s "$PIDFILE" ]]; then
    break
  fi
  sleep 0.1
done

if [[ ! -s "$PIDFILE" ]]; then
  echo "[erro] BMv2 não gravou o PID file: $PIDFILE" >&2
  wait "$launcher_pid" 2>/dev/null || true
  exit 1
fi

SW_PID="$(cat "$PIDFILE")"

if ! as_root kill -0 "$SW_PID" 2>/dev/null; then
  echo "[erro] Processo BMv2 não permaneceu ativo (PID=$SW_PID)." >&2
  tail -n 80 "$LOG" >&2 || true
  exit 1
fi

echo "[ok] PID real: ${SW_PID}"
echo "[ok] Log:      ${LOG}"
echo "[ok] JSON:     ${JSON}"
echo "[ok] P4INFO:   ${P4INFO}"
