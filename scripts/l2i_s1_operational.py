#!/usr/bin/env python3
"""S1 production qualification entrypoint."""
import argparse
import json
import pathlib
import sys

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--spec",required=True); parser.add_argument("--output-dir",required=True)
    args=parser.parse_args(); stage=pathlib.Path(__file__).resolve().parents[2]; sys.path.insert(0,str(stage))
    from programs.campaign_c6.production_dispatcher import qualify
    spec=json.loads(pathlib.Path(args.spec).read_text())
    if spec.get("scenario")!="S1" or spec.get("implementation")!="real": raise SystemExit("S1_PRODUCTION_SPEC_INVALID")
    qualify(spec,pathlib.Path(args.output_dir))
if __name__=="__main__": main()
