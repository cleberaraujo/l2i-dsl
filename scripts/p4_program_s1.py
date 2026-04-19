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

    # Fluxo padrão dos seus testes (H1->H3, UDP/5001)
    ap.add_argument("--src", default="10.0.0.1")
    ap.add_argument("--dst", default="10.0.0.3")
    ap.add_argument("--dport", type=int, default=5001)
    ap.add_argument("--dscp", type=int, default=0x28)  # DSCP=40 (AF41). Ajuste se quiser.

    args = ap.parse_args()

    sh.setup(device_id=args.device_id, grpc_addr=args.addr, election_id=(0,1))
    sh.load_p4info(str(Path(args.outdir) / "l2i_minimal.p4info.txt"))
    sh.set_fwd_pipe(str(Path(args.outdir) / "l2i_minimal.json"))

    p4 = sh.P4Objects()

    tbl = p4.get_table("tbl_set_dscp")
    act = p4.get_action("set_dscp")

    key = tbl.make_key([
        ("hdr.ipv4.srcAddr", to_u32(args.src)),
        ("hdr.ipv4.dstAddr", to_u32(args.dst)),
        ("hdr.udp.dstPort",  args.dport),
    ])
    data = act.make_data([("new_dscp", args.dscp)])
    tbl.entry_add([ (key, data) ])

    print("[ok] regra DSCP instalada para {}:{} -> {} dport {}".format(args.src, "", args.dst, args.dport))

if __name__ == "__main__":
    main()
