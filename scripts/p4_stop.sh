#!/usr/bin/env bash
set -euo pipefail

OUTDIR="/tmp/l2i_minimal"
PIDFILE="$OUTDIR/bmv2.pid"

if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE" || true)"
  if [[ -n "${PID}" ]] && ps -p "$PID" >/dev/null 2>&1; then
    echo "[stop] Matando BMv2 (PID=$PID)"
    kill "$PID" || true
    sleep 0.3
  fi
  rm -f "$PIDFILE"
fi

echo "[ok] Encerrado."
