#!/usr/bin/env bash
set -euo pipefail

P4SRC="p4src/l2i_minimal.p4"
OUTDIR="/tmp/l2i_minimal"
JSON="${OUTDIR}/l2i_minimal.json"
P4INFO="${OUTDIR}/l2i_minimal.p4info.txtpb"
LOG="${OUTDIR}/bmv2.log"
PIDFILE="${OUTDIR}/bmv2.pid"

echo "[prep] Criando diretório de saída: ${OUTDIR}"
mkdir -p "${OUTDIR}"

echo "[build] p4c-bm2-ss → ${JSON} / ${P4INFO}"
p4c-bm2-ss \
  -I p4src \
  --p4runtime-file "${P4INFO}" \
  --p4runtime-format text \
  -o "${JSON}" \
  "${P4SRC}"

echo "[ok] JSON:   ${JSON}"
echo "[ok] P4INFO: ${P4INFO}"

# veth0/veth1 para ligar o bmv2 em algo simples
if ip link show veth0 &>/dev/null; then
  echo "[net] veth0/veth1 já existem (ok)"
else
  echo "[net] criando veth0/veth1"
  sudo ip link add veth0 type veth peer name veth1
  sudo ip link set veth0 up
  sudo ip link set veth1 up
fi

# Mata instância anterior do simple_switch_grpc, se existir
if pgrep -x simple_switch_grpc >/dev/null 2>&1; then
  echo "[run] matando simple_switch_grpc antigo"
  sudo pkill simple_switch_grpc || true
fi

echo "[run] simple_switch_grpc em 0.0.0.0:9559 (thrift 9090, device-id=0)"
# Observação: precisamos da porta Thrift 9090 ativa para o simple_switch_CLI
sudo simple_switch_grpc \
  -i 0@veth0 \
  -i 1@veth1 \
  --device-id 0 \
  --thrift-port 9090 \
  --log-console \
  "${JSON}" \
  > "${LOG}" 2>&1 &

SW_PID=$!
echo "${SW_PID}" > "${PIDFILE}"

echo "[ok] PID:   ${SW_PID}"
echo "[ok] Log:   ${LOG}"
echo "[ok] JSON:  ${JSON}"
echo "[ok] P4INFO:${P4INFO}"
