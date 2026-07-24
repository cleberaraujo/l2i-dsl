#!/usr/bin/env bash
set -euo pipefail

OUTDIR="${P4_OUTDIR:-/tmp/l2i_minimal}"
PIDFILE="${BMV2_PIDFILE:-$OUTDIR/bmv2.pid}"
REMOVE_LINKS=0

case "${1:-}" in
  "") ;;
  --remove-links) REMOVE_LINKS=1 ;;
  *)
    echo "Uso: $0 [--remove-links]" >&2
    exit 2
    ;;
esac

as_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

wait_process_exit() {
  local pid="$1"
  local _

  for _ in $(seq 1 30); do
    if ! as_root kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done

  return 1
}

stop_pid=""
if [[ -f "$PIDFILE" ]]; then
  stop_pid="$(cat "$PIDFILE" 2>/dev/null || true)"
fi

if [[ "$stop_pid" =~ ^[0-9]+$ ]] \
   && as_root kill -0 "$stop_pid" 2>/dev/null; then
  echo "[stop] Encerrando BMv2 pelo PID file (PID=$stop_pid)"
  as_root kill -TERM "$stop_pid" 2>/dev/null || true

  if ! wait_process_exit "$stop_pid"; then
    echo "[stop] BMv2 não encerrou com SIGTERM; enviando SIGKILL."
    as_root kill -KILL "$stop_pid" 2>/dev/null || true
    wait_process_exit "$stop_pid" || true
  fi
fi

# Fallback para versões antigas cujo PID file continha o PID do sudo ou estava
# ausente. O padrão ancora o nome do executável e não depende do caminho.
if pgrep -f '(^|/)simple_switch_grpc([[:space:]]|$)' >/dev/null 2>&1; then
  echo "[stop] Encerrando instância BMv2 residual por nome."
  as_root pkill -TERM -f '(^|/)simple_switch_grpc([[:space:]]|$)' 2>/dev/null || true
  sleep 0.3
fi

if pgrep -f '(^|/)simple_switch_grpc([[:space:]]|$)' >/dev/null 2>&1; then
  as_root pkill -KILL -f '(^|/)simple_switch_grpc([[:space:]]|$)' 2>/dev/null || true
fi

as_root rm -f "$PIDFILE"

if [[ "$REMOVE_LINKS" -eq 1 ]]; then
  echo "[stop] Removendo interfaces persistentes do BMv2."
  for dev in veth0 veth0-peer veth1 veth1-peer; do
    as_root ip link del "$dev" 2>/dev/null || true
  done
fi

echo "[ok] BMv2 encerrado."
