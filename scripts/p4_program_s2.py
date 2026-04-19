#!/usr/bin/env python3
import argparse, ipaddress, sys
from pathlib import Path

try:
    import p4runtime_sh.shell as sh
except Exception as e:
    sys.stderr.write("ERRO: p4runtime_sh ausente. Instale via pipx.\n")
    sys.exit(1)

def to_u32(ip):
    return int(ipaddress.ip_address(ip))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default="127.0.0.1:9559")
    ap.add_argument("--device-id", type=int, default=0)
    ap.add_argument("--outdir", default="/tmp/l2i_minimal")

    # Grupo multicast e destino de teste
    ap.add_argument("--mgrp", type=int, default=1)
    ap.add_argument("--dst-mcast", default="239.1.1.1")
    ap.add_argument("--ports", type=int, nargs="+", default=[0,1])  # replicar em veth0 e veth1

    args = ap.parse_args()

    sh.setup(device_id=args.device_id, grpc_addr=args.addr, election_id=(0,1))
    sh.load_p4info(str(Path(args.outdir) / "l2i_minimal.p4info.txt"))
    sh.set_fwd_pipe(str(Path(args.outdir) / "l2i_minimal.json"))

    # 1) PRE: criar MulticastGroupEntry (mgrp -> replicas)
    # p4runtime_sh fornece helper:
    sh.create_multicast_group(args.mgrp, args.ports)
    print(f"[ok] multicast group {args.mgrp} com ports {args.ports}")

    # 2) Tabela de roteamento multicast: dstIP -> mgrp
    p4 = sh.P4Objects()
    tbl = p4.get_table("tbl_route_mcast")
    act = p4.get_action("set_mcast")

    key = tbl.make_key([ ("hdr.ipv4.dstAddr", to_u32(args.dst_mcast)) ])
    data = act.make_data([ ("grp", args.mgrp) ])
    tbl.entry_add([ (key, data) ])

    print(f"[ok] regra mcast: {args.dst_mcast} -> mgrp {args.mgrp}")

if __name__ == "__main__":
    main()
