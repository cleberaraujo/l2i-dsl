#!/usr/bin/env python3
"""S2 nine-role production qualification entrypoint."""
import argparse
import json
import pathlib
import sys

ROLES=("P4RUNTIME_PRE","NETCONF","LINUX_TC","SENDER","RECEIVER_B","RECEIVER_C","PROBE","BARRIER","RECONCILE")
def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--spec",required=True); parser.add_argument("--output-dir",required=True)
    args=parser.parse_args(); stage=pathlib.Path(__file__).resolve().parents[2]; sys.path.insert(0,str(stage))
    from programs.campaign_c6.production_dispatcher import qualify
    spec=json.loads(pathlib.Path(args.spec).read_text())
    if spec.get("scenario")!="S2" or spec.get("implementation")!="real": raise SystemExit("S2_PRODUCTION_SPEC_INVALID")
    spec["roles"]=list(ROLES); qualify(spec,pathlib.Path(args.output_dir))
if __name__=="__main__": main()
