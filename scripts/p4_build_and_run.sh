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

# O par veth0/veth1 pertence ao serviço BMv2 e deve sobreviver à criação e à
# limpeza das topologias S1/S2. Se apenas uma das pontas existir, recriamos o par
# para não iniciar o switch sobre uma infraestrutura parcial.
if ip link show veth0 >/dev/null 2>&1 \
   && ip link show veth1 >/dev/null 2>&1; then
  echo "[net] veth0/veth1 já existem (ok)"
else
  echo "[net] recriando veth0/veth1"
  as_root ip link del veth0 2>/dev/null || true
  as_root ip link del veth1 2>/dev/null || true
  as_root ip link add veth0 type veth peer name veth1
fi

as_root ip link set veth0 up
as_root ip link set veth1 up

# Encerra instâncias anteriores e remove PID files obsoletos.
as_root "$SCRIPT_DIR/p4_stop.sh" >/dev/null 2>&1 || true
as_root rm -f "$PIDFILE"

# O PID gravado é o PID real do simple_switch_grpc, não o PID do processo sudo.
# nohup evita que o serviço receba SIGHUP quando a sessão SSH termina.
echo "[run] simple_switch_grpc em 0.0.0.0:9559 (thrift ${THRIFT_PORT}, device-id=0)"
as_root nohup sh -c '
  pidfile="$1"
  logfile="$2"
  shift 2
  printf "%s\n" "$$" > "$pidfile"
  exec "$@" > "$logfile" 2>&1
' sh \
  "$PIDFILE" \
  "$LOG" \
  simple_switch_grpc \
  -i 0@veth0 \
  -i 1@veth1 \
  --device-id 0 \
  --thrift-port "$THRIFT_PORT" \
  --log-console \
  "$JSON" \
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
