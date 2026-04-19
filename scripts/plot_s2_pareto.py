#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, csv, os, math, datetime as dt
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def ts_utc():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
def ensure_dir(p): os.makedirs(p, exist_ok=True)
def sfloat(x):
    try:
        if x is None or x=="": return float("nan")
        return float(x)
    except: return float("nan")

def load_rows(path):
    with open(path,"r",encoding="utf-8") as f:
        dr = csv.DictReader(f)
        rows = [{k.strip(): (v.strip() if isinstance(v,str) else v) for k,v in r.items()} for r in dr]
    return rows

def parse_args():
    ap = argparse.ArgumentParser(description="Pareto S2: throughput vs latência")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--domain", choices=["both","B","C"], default="both")
    ap.add_argument("--latency", choices=["rtt50","rtt95","rtt99"], default="rtt95")
    ap.add_argument("--variant", choices=["base","adapt","delta"], default="delta")
    ap.add_argument("--outdir", default="results/plots")
    ap.add_argument("--alpha", type=float, default=0.8, help="transparência dos pontos")
    return ap.parse_args()

def pick_latency_col(s):
    return {"rtt50":"rtt_p50_ms","rtt95":"rtt_p95_ms","rtt99":"rtt_p99_ms"}[s]

def main():
    args=parse_args(); rows=load_rows(args.csv)
    lat_col=pick_latency_col(args.latency)
    doms=["B","C"] if args.domain=="both" else [args.domain]
    ensure_dir(args.outdir); stamp=ts_utc()

    for d in doms:
        xs=[]; ys=[]; colors=[]; labels=[]
        for r in rows:
            if r.get("receptor","").upper()!=d: continue
            thr = sfloat(r.get("throughput_mbps"))
            lat = sfloat(r.get(lat_col))
            md  = r.get("mode","").lower()
            if args.variant=="base" and md!="baseline": continue
            if args.variant=="adapt" and md!="adapt": continue
            if args.variant=="delta":
                # delta exige pareamento: vamos montar por chave (bwB,be)
                # primeiro passe: coleto baseline e adapt por chave
                pass
            xs.append(thr); ys.append(lat); colors.append("#1f77b4" if md=="baseline" else "#d62728"); labels.append(md)

        if args.variant=="delta":
            # constrói pares
            idx={}
            for r in rows:
                if r.get("receptor","").upper()!=d: continue
                key=(int(float(r.get("bwB",0))), int(float(r.get("be_mbps",0))))
                md=r.get("mode","").lower()
                thr=sfloat(r.get("throughput_mbps"))
                lat=sfloat(r.get(lat_col))
                if key not in idx: idx[key]={"b":(np.nan,np.nan),"a":(np.nan,np.nan)}
                if md=="baseline": idx[key]["b"]=(thr,lat)
                elif md=="adapt":  idx[key]["a"]=(thr,lat)
            xs=[]; ys=[]
            for k,v in idx.items():
                (tb,lb),(ta,la)=v["b"],v["a"]
                if not (math.isnan(tb) or math.isnan(ta) or math.isnan(lb) or math.isnan(la)):
                    xs.append(ta-tb)  # Δ throughput
                    ys.append(la-lb)  # Δ latência (ms)
            colors=["#2ca02c"]*len(xs)  # verde = delta
            labels=["delta"]*len(xs)

        if not xs:
            print(f"[warn] sem pontos para {d} ({args.variant}/{args.latency})")
            continue

        plt.figure(figsize=(7,5))
        plt.scatter(xs, ys, c=colors, alpha=args.alpha, edgecolors="none")
        if args.variant=="delta":
            plt.axvline(0, color="#888", linewidth=1); plt.axhline(0, color="#888", linewidth=1)
            plt.title(f"S2 — Δ throughput vs Δ {args.latency.upper()} ({d})")
            plt.xlabel("Δ throughput (Mbit/s)"); plt.ylabel(f"Δ {args.latency.upper()} (ms)")
        else:
            # sobrepõe incumbente (baseline) e adapt
            from matplotlib.lines import Line2D
            legend_elems=[
                Line2D([0],[0], marker='o', color='w', label='baseline', markerfacecolor='#1f77b4', markersize=8),
                Line2D([0],[0], marker='o', color='w', label='adapt', markerfacecolor='#d62728', markersize=8)
            ]
            plt.legend(handles=legend_elems, loc="best")
            plt.title(f"S2 — Throughput vs {args.latency.upper()} ({d}, {args.variant})")
            plt.xlabel("Throughput (Mbit/s)")
            plt.ylabel(f"{args.latency.upper()} (ms)")
        plt.grid(True, linestyle=":", alpha=0.7)
        out=os.path.join(args.outdir, f"PARETO_S2_{d}_{args.variant}_{args.latency}_{stamp}.png")
        plt.tight_layout(); plt.savefig(out, dpi=160); plt.close()
        print(f"[ok] salvo: {out}")

if __name__=="__main__":
    main()
