#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, csv, os, math, datetime as dt
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def ts_utc(): return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
def ensure_dir(p): os.makedirs(p, exist_ok=True)
def sfloat(x):
    try:
        if x is None or x=="": return float("nan")
        return float(x)
    except: return float("nan")

def load_rows(path):
    with open(path,"r",encoding="utf-8") as f:
        dr=csv.DictReader(f)
        return [{k.strip(): (v.strip() if isinstance(v,str) else v) for k,v in r.items()} for r in dr]

def parse_args():
    ap=argparse.ArgumentParser(description="Improvement map S2 (Δthr>0 & Δrtt95<0)")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--domain", choices=["both","B","C"], default="both")
    ap.add_argument("--outdir", default="results/plots")
    ap.add_argument("--rtt_col", choices=["rtt_p50_ms","rtt_p95_ms","rtt_p99_ms"], default="rtt_p95_ms")
    ap.add_argument("--thr_margin", type=float, default=0.0, help="margem mínima de ganho de throughput (Mbit/s)")
    ap.add_argument("--rtt_margin", type=float, default=0.0, help="margem mínima de queda de RTT (ms; use >=0)")
    return ap.parse_args()

def main():
    args=parse_args(); rows=load_rows(args.csv)
    doms=["B","C"] if args.domain=="both" else [args.domain]
    ensure_dir(args.outdir); stamp=ts_utc()

    for d in doms:
        # indexa baseline/adapt por chave
        idx={}
        bw_set=set(); be_set=set()
        for r in rows:
            if r.get("receptor","").upper()!=d: continue
            key=(int(float(r.get("bwB",0))), int(float(r.get("be_mbps",0))))
            bw_set.add(key[0]); be_set.add(key[1])
            md=r.get("mode","").lower()
            thr=sfloat(r.get("throughput_mbps"))
            rtt=sfloat(r.get(args.rtt_col))
            if key not in idx: idx[key]={"b":(np.nan,np.nan),"a":(np.nan,np.nan)}
            if md=="baseline": idx[key]["b"]=(thr,rtt)
            elif md=="adapt":  idx[key]["a"]=(thr,rtt)

        bw_list=sorted(bw_set); be_list=sorted(be_set)
        H=np.zeros((len(bw_list), len(be_list)))  # score: 2=ganho simultâneo, 1=parcial, 0=neutro, -1=regressão
        labels=[["" for _ in be_list] for __ in bw_list]

        for i,bw in enumerate(bw_list):
            for j,be in enumerate(be_list):
                (tb,rb),(ta,ra)=idx.get((bw,be),{"b":(np.nan,np.nan),"a":(np.nan,np.nan)}).values()
                if any(math.isnan(x) for x in [tb,rb,ta,ra]):
                    H[i][j]=np.nan; labels[i][j]=""
                    continue
                d_thr = ta-tb
                d_rtt = ra-rb
                good_thr = d_thr >  args.thr_margin
                good_lat = d_rtt < -args.rtt_margin
                if good_thr and good_lat:
                    H[i][j]=2;  labels[i][j]="✓✓"
                elif good_thr and not good_lat:
                    H[i][j]=1;  labels[i][j]="↑"
                elif (not good_thr) and good_lat:
                    H[i][j]=1;  labels[i][j]="↓"
                elif abs(d_thr)<=args.thr_margin and abs(d_rtt)<=args.rtt_margin:
                    H[i][j]=0;  labels[i][j]="·"
                else:
                    H[i][j]=-1; labels[i][j]="×"

        # colormap discreta
        from matplotlib.colors import ListedColormap, BoundaryNorm
        cmap = ListedColormap(["#d62728","#dddddd","#ffdd88","#2ca02c"])  # -1,0,1,2
        bounds=[-1.5,-0.5,0.5,1.5,2.5]
        norm=BoundaryNorm(bounds, cmap.N)

        fig, ax = plt.subplots(figsize=(8,5.2))
        im=ax.imshow(H, cmap=cmap, norm=norm, aspect="auto", origin="upper")
        ax.set_xticks(range(len(be_list))); ax.set_xticklabels(be_list)
        ax.set_yticks(range(len(bw_list))); ax.set_yticklabels(bw_list)
        ax.set_xlabel("be_mbps"); ax.set_ylabel("bwB (Mbps)")
        ax.set_title(f"S2 — Improvement map ({d})  (RTT={args.rtt_col}, thr_margin={args.thr_margin}, rtt_margin={args.rtt_margin})")

        # anota símbolos
        for i in range(len(bw_list)):
            for j in range(len(be_list)):
                if not math.isnan(H[i][j]):
                    ax.text(j, i, labels[i][j], ha="center", va="center", fontsize=12, color="#222")

        # legenda manual
        from matplotlib.patches import Patch
        legend_elems=[
            Patch(color="#2ca02c", label="Ganho simultâneo (thr↑, rtt↓)"),
            Patch(color="#ffdd88", label="Parcial (apenas uma melhora)"),
            Patch(color="#dddddd", label="Neutro"),
            Patch(color="#d62728", label="Regressão")
        ]
        ax.legend(handles=legend_elems, loc="upper right", framealpha=1.0)
        plt.tight_layout()
        out=os.path.join(args.outdir, f"IMPROV_MAP_S2_{d}_{args.rtt_col}_{stamp}.png")
        plt.savefig(out, dpi=170); plt.close()
        print(f"[ok] salvo: {out}")

if __name__=="__main__":
    main()
