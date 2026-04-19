import argparse, json, sys
from l2i.validator import validate_spec
from l2i.policies import apply_policies
from l2i.capabilities import ensure_capability_valid, check_capabilities
from l2i.compose import compose_specs
from l2i.synth import synthesize_ir
#from l2i.emit import emit_netconf, emit_p4runtime_stub
from l2i.emit import emit_netconf_like, emit_p4runtime_like

# Perfis embutidos (você pode trocar por leitura de JSON externo se preferir)
LEGACY_PROFILE = {
  "profile_id":"legacy-vlan-tc",
  "queues":{"max_queues":4,"modes":{"strict":True,"wfq":{"supported":True,"weights_min":0.5,"weights_max":16}}},
  "meters":{"supported":True,"types":["tbf"],"min_rate_mbps":1,"max_rate_mbps":10000},
  "multicast":{"mode":"vlan_flood","max_groups":64,"max_replications_per_group":64},
  "ports":[{"name":"eth0","speed_mbps":1000},{"name":"eth1","speed_mbps":1000}],
  "atomic_commit": False,
  "telemetry":{"rtt_percentile":True,"throughput_sustained":True,"queue_occupancy":True,"delivery_ratio":False}
}

P4_PROFILE = {
  "profile_id":"p4-bmv2-basic",
  "queues":{"max_queues":8,"modes":{"strict":True,"wfq":{"supported":True,"weights_min":1,"weights_max":100}}},
  "meters":{"supported":True,"types":["trtcm"],"min_rate_mbps":1,"max_rate_mbps":40000},
  "multicast":{"mode":"l2mc_static","max_groups":1024,"max_replications_per_group":128},
  "ports":[{"name":"p1","speed_mbps":10000},{"name":"p2","speed_mbps":10000}],
  "atomic_commit": True,
  "telemetry":{"rtt_percentile":True,"throughput_sustained":True,"queue_occupancy":True,"delivery_ratio":True}
}

def e2e_pipeline(spec_doc, profile):
  # 1) schema+semântica
  spec, errs = validate_spec(spec_doc)
  if errs: return None, {"errors":[e.__dict__ for e in errs]}
  # 2) políticas
  st, specP, pol = apply_policies(spec)
  if st == "deny": return None, {"errors":[p.__dict__ for p in pol]}
  # 3) capacidades
  st2, specC, cap = check_capabilities(specP, profile)
  if st2 == "deny": return None, {"errors":[c.__dict__ for c in cap]}
  # 4) composição (aqui 1 doc → trivial)
  merged, confl = compose_specs([specC])
  if confl: return None, {"errors":[confl.__dict__]}
  # 5) síntese
  plan = synthesize_ir(merged, profile)
  return plan, None

def build_arg_parser():
  p = argparse.ArgumentParser(description="L2I v0 (pipeline end-to-end)")
  sub = p.add_subparsers(dest="cmd", required=True)
  sp = sub.add_parser("plan");    sp.add_argument("--profile", choices=["legacy","p4"], required=True); sp.add_argument("--spec", required=True)
  sn = sub.add_parser("netconf"); sn.add_argument("--profile", choices=["legacy","p4"], required=True); sn.add_argument("--spec", required=True)
  sp4 = sub.add_parser("p4");     sp4.add_argument("--profile", choices=["legacy","p4"], required=True); sp4.add_argument("--spec", required=True)
  return p

def main(argv):
  ap = build_arg_parser(); args = ap.parse_args(argv)
  profile = LEGACY_PROFILE if args.profile == "legacy" else P4_PROFILE
  ensure_capability_valid(profile)
  with open(args.spec,"r",encoding="utf-8") as f: spec_doc = json.load(f)
  plan, err = e2e_pipeline(spec_doc, profile)
  if args.cmd == "plan":
    print(json.dumps(plan, default=lambda o:o.__dict__, indent=2) if plan else json.dumps(err, indent=2))
  elif args.cmd == "netconf":
    if plan: print(emit_netconf(plan))
    else: print(json.dumps(err, indent=2))
  elif args.cmd == "p4":
    if plan: print(json.dumps(emit_p4runtime_stub(plan), indent=2))
    else: print(json.dumps(err, indent=2))

if __name__ == "__main__":
  main(sys.argv[1:])
