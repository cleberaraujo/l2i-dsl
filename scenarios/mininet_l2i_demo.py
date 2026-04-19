# scenarios/mininet_l2i_demo.py
"""
Cenário Mininet v0 (3 switches, 3 hosts):
  h1 -- s1 -- s2 -- s3 -- h3
             \            /
              ---- h2 ----

Fluxo de interesse: h1 -> h3 (flow_id=VideoHD_A)
Tráfego competidor (best-effort): h2 -> h3

Pipeline:
  - Gera IRPlan a partir de spec JSON (ou embutida)
  - Aplica 'tc/htb' em h1-eth0 para classificar tráfego destinado a 10.0.0.3
  - Mede RTT P99 (ping) e throughput iperf3
  - Avalia SLO; imprime relatório
"""

from __future__ import annotations
import os, sys, json, time, tempfile, signal
from pathlib import Path
from typing import Optional, Dict

from mininet.net import Mininet
#from mininet.node import Controller, OVSBridge
from mininet.node import OVSBridge
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.cli import CLI

# Importa nossos módulos L2I
sys.path.append(str(Path(__file__).resolve().parents[1]))  # adicionar raiz do projeto ao sys.path
from l2i.validator import validate_spec
from l2i.policies import apply_policies
from l2i.capabilities import ensure_capability_valid, check_capabilities
from l2i.compose import compose_specs
from l2i.synth import synthesize_ir
from l2i.legacy_exec import MininetRunner, apply_tc_plan, clear_tc
from l2i.ac5_telemetry import measure_ping_rtts, measure_iperf3_throughput, evaluate_slo

LEGACY_PROFILE = {
  "profile_id":"legacy-vlan-tc",
  "queues":{"max_queues":4,"modes":{"strict":True,"wfq":{"supported":True,"weights_min":0.5,"weights_max":16}}},
  "meters":{"supported":True,"types":["tbf"],"min_rate_mbps":1,"max_rate_mbps":10000},
  "multicast":{"mode":"vlan_flood","max_groups":64,"max_replications_per_group":64},
  "ports":[{"name":"eth0","speed_mbps":1000}],
  "atomic_commit": False,
  "telemetry":{"rtt_percentile":True,"throughput_sustained":True,"queue_occupancy":True,"delivery_ratio":False}
}

SPEC = {
  "l2i_version":"0.1",
  "tenant":"org.example",
  "scope":"fabric-demo",
  "flow":{"id":"VideoHD_A"},
  "requirements":{
    "latency":{"max_ms":30,"percentile":"P99"},
    "bandwidth":{"min_mbps":5,"max_mbps":8},
    "priority":{"level":"high"}
  }
}

def plan_from_spec(spec: Dict) -> dict:
  specC, errs = validate_spec(spec)
  if errs:
    raise RuntimeError(f"Spec inválida: {errs}")
  st, specP, pol = apply_policies(specC)
  if st == "deny":
    raise RuntimeError(f"Política negou: {pol}")
  ensure_capability_valid(LEGACY_PROFILE)
  st2, specCap, cap = check_capabilities(specP, LEGACY_PROFILE)
  if st2 == "deny":
    raise RuntimeError(f"Cap não suporta: {cap}")
  merged, confl = compose_specs([specCap])
  if confl:
    raise RuntimeError(f"Conflito composição: {confl}")
  plan = synthesize_ir(merged, LEGACY_PROFILE)
  return plan

def build_net() -> Mininet:
  #net = Mininet(controller=Controller, switch=OVSBridge, link=TCLink, cleanup=True, autoSetMacs=True)
  net = Mininet(controller=None, switch=OVSBridge, link=TCLink, cleanup=True, autoSetMacs=True)
  #c0 = net.addController('c0')

  s1 = net.addSwitch('s1'); s2 = net.addSwitch('s2'); s3 = net.addSwitch('s3')
  h1 = net.addHost('h1', ip='10.0.0.1/24'); h2 = net.addHost('h2', ip='10.0.0.2/24'); h3 = net.addHost('h3', ip='10.0.0.3/24')

  # links
  net.addLink(h1, s1, bw=100, delay='1ms')
  net.addLink(s1, s2, bw=100, delay='1ms')
  net.addLink(s2, s3, bw=100, delay='1ms')
  net.addLink(s3, h3, bw=100, delay='1ms')
  net.addLink(h2, s2, bw=100, delay='1ms')

  net.start()
  # Rotas simples
  for h in (h1, h2, h3):
    h.cmd("ip route add default dev %s-eth0" % h.name)

  return net

def background_traffic(h2, h3, duration_s=10):
  # iperf3 servidor em h3
  h3.cmd("pkill -9 iperf3 || true")
  h3.cmd("iperf3 -s -D")  # daemon
  time.sleep(0.5)
  # fluxo best-effort h2->h3 (sem JSON)
  print("[BE] iniciando iperf3 h2->h3")
  h2.cmd(f"iperf3 -c 10.0.0.3 -t {duration_s} > /tmp/iperf3_be.txt 2>&1 &")

def main():
  setLogLevel('warning')
  net = build_net()
  h1, h2, h3 = net.get('h1','h2','h3')

  print("[L2I] Gerando plano (IR) a partir da Spec")
  plan = plan_from_spec(SPEC)
  print("[L2I] Plano:", json.dumps(plan.__dict__, default=lambda o:o.__dict__, indent=2))

  # Aplica 'tc' no egress de h1 (interface h1-eth0), classificando tráfego destinado a 10.0.0.3
  print("[L2I] Aplicando TC em h1-eth0 (classe da intenção por dst=10.0.0.3)")
  runner = MininetRunner(h1)
  clear_tc("h1-eth0", runner)
  apply_tc_plan(plan, "h1-eth0", runner=runner, selector={"dst":"10.0.0.3"})

  # Tráfego competidor (BE) de h2->h3
  background_traffic(h2, h3, duration_s=12)

  # Medições do fluxo de interesse h1->h3
  print("[L2I] Medindo RTT (ping)…")
  lat = measure_ping_rtts("10.0.0.3", count=50, interval=0.05, iface="h1-eth0")
  print("[L2I] RTT stats:", lat)

  print("[L2I] Medindo throughput (iperf3)…")
  # iperf3 servidor dedicado para a medição do fluxo de interesse
  h3.cmd("pkill -9 iperf3 || true"); h3.cmd("iperf3 -s -D"); time.sleep(0.5)
  thr = None
  # Cliente em h1 (com JSON para parse)
  out = h1.cmd("iperf3 -c 10.0.0.3 -t 5 -J")
  try:
    data = json.loads(out)
    thr = data["end"]["sum_received"]["bits_per_second"] / 1e6
  except Exception as e:
    print("[WARN] iperf3 parse:", e)
    thr = None

  report = evaluate_slo(lat, thr, plan.constraints)
  print("[L2I] Relatório SLO:", json.dumps(report, indent=2))

  # Se quiser inspecionar manualmente:
  # CLI(net)

  net.stop()

if __name__ == "__main__":
  main()
