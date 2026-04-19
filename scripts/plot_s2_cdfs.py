#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, csv, os, math, datetime as dt
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from collections import OrderedDict, defaultdict

def ts_utc():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def ensure_dir(p): os.makedirs(p, exist_ok=True)

def sfloat(x):
    try:
        if x is None or x == "": return float("nan")
        return float(x)
    except: return float("nan")

METRICS = OrderedDict([
    ("throughput_mbps", ("Throughput (Mbit/s)",)),
    ("rtt_p50_ms",      ("RTT p50 (ms)",)),
    ("rtt_p95_ms",      ("RTT p95 (ms)",)),
    ("rtt_p99_ms",      ("RTT p99 (ms)",)),
    ("jitter_ms",       ("Jitter (ms)",)),
    ("delivery_ratio",  ("Delivery ratio (%)",)),
    ("join_time_s",     ("Join time (s)",)),
])

def transform(metric, v):
    if math.isnan(v): return np.nan
    if metric == "delivery_ratio" and v <= 1.0:  # para exibir em %
        return v*100.0
    return v

def load_rows(path):
    with open(path, "r", encoding="utf-8") as f:
        dr = csv.DictReader(f)
        rows = []
        for r in dr:
            row = {k.strip(): (v.strip() if isinstance(v,str) else v) for k,v in r.items()}
            rows.append(row)
    return rows

def values_by_mode(rows, domain, metric):
    base, adapt = [], []
    for r in rows:
        if (r.get("receptor","").upper()!=domain): continue
        m = sfloat(r.get(metric))
        mv = transform(metric, m)
        if math.isnan(mv): continue
        if (r.get("mode","").lower()=="baseline"): base.append(mv)
        elif (r.get("mode","").lower()=="adapt"): adapt.append(mv)
    return np.array(base), np.array(adapt)

def ecdf(arr):
    if arr.size==0: return np.array([]), np.array([])
    a = np.sort(arr)
    y = np.arange(1, len(a)+1)/len(a)
    return a, y

def plot_cdf(ax, data, label, color):
    x,y = ecdf(data)
    if x.size:
        ax.plot(x,y,label=label,color=color,linewidth=2)

def parse_args():
    ap = argparse.ArgumentParser(description="CDFs S2 baseline vs adapt por domínio/métrica")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--domain", choices=["both","B","C"], default="both")
    ap.add_argument("--metrics", default="throughput,rtt50,rtt95,rtt99,jitter,delivery,join",
                    help="Lista separada por vírgulas (ou 'all')")
    ap.add_argument("--outdir", default="results/plots")
    ap.add_argument("--inspect", action="store_true")
    return ap.parse_args()

def metric_list(s):
    if s.strip().lower()=="all":
        s="throughput,rtt50,rtt95,rtt99,jitter,delivery,join"
    out=[]
    for m in [x.strip() for x in s.split(",") if x.strip()]:
        m=m.lower()
        if   m=="throughput": out.append("throughput_mbps")
        elif m=="rtt50":      out.append("rtt_p50_ms")
        elif m=="rtt95":      out.append("rtt_p95_ms")
        elif m=="rtt99":      out.append("rtt_p99_ms")
        elif m=="jitter":     out.append("jitter_ms")
        elif m=="delivery":   out.append("delivery_ratio")
        elif m=="join":       out.append("join_time_s")
        else: raise SystemExit(f"Métrica inválida: {m}")
    return out

def main():
    args=parse_args()
    rows=load_rows(args.csv)
    doms=["B","C"] if args.domain=="both" else [args.domain]
    mets=metric_list(args.metrics)
    ensure_dir(args.outdir); stamp=ts_utc()

    if args.inspect:
        for d in doms:
            print(f"[inspect] domínio {d}")
            for m in mets:
                b,a = values_by_mode(rows,d,m)
                print(f"  {m}: baseline={b.size} pontos, adapt={a.size} pontos")
        return

    for d in doms:
        for m in mets:
            b,a = values_by_mode(rows,d,m)
            if b.size==0 and a.size==0:
                print(f"[warn] sem dados para {d}/{m}")
                continue
            fig, ax = plt.subplots(figsize=(6.8,4.6))
            plot_cdf(ax,b,"baseline","#1f77b4")
            plot_cdf(ax,a,"adapt","#d62728")
            ax.grid(True, linestyle=":", alpha=0.7)
            label = METRICS[m][0]
            ax.set_xlabel(label)
            ax.set_ylabel("F(x)")
            ax.set_title(f"S2 — CDF {label} ({d})")
            ax.legend()
            out=os.path.join(args.outdir, f"CDF_S2_{d}_{m}_{stamp}.png")
            plt.tight_layout(); plt.savefig(out, dpi=160); plt.close()
            print(f"[ok] salvo: {out}")

if __name__=="__main__":
    main()
