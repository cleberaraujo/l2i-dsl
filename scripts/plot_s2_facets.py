#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, csv, os, math, datetime as dt
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from collections import OrderedDict

def ts_utc(): 
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def ensure_dir(p): 
    os.makedirs(p, exist_ok=True)

def sfloat(x):
    try:
        if x is None or x == "": 
            return float("nan")
        return float(x)
    except:
        return float("nan")

METRICS = OrderedDict([
    ("throughput_mbps", ("Throughput (Mbit/s)",)),
    ("delivery_ratio",  ("Delivery ratio (%)",)),
    ("jitter_ms",       ("Jitter (ms)",)),
    ("rtt_p50_ms",      ("RTT p50 (ms)",)),
    ("rtt_p95_ms",      ("RTT p95 (ms)",)),
    ("rtt_p99_ms",      ("RTT p99 (ms)",)),
    ("join_time_s",     ("Join time (s)",)),
])

def transform(metric, v):
    if math.isnan(v): 
        return np.nan
    # delivery_ratio pode vir em [0,1]; exibir %
    if metric == "delivery_ratio" and v <= 1.0: 
        return v * 100.0
    return v

def load_rows(path):
    with open(path, "r", encoding="utf-8") as f:
        dr = csv.DictReader(f)
        rows = [{k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()} for r in dr]
    return rows

def metric_list(s):
    if s.strip().lower() == "all":
        s = "throughput,delivery,jitter,rtt50,rtt95,rtt99,join"
    out = []
    for m in [x.strip() for x in s.split(",") if x.strip()]:
        m = m.lower()
        if   m == "throughput": out.append("throughput_mbps")
        elif m == "delivery":   out.append("delivery_ratio")
        elif m == "jitter":     out.append("jitter_ms")
        elif m == "rtt50":      out.append("rtt_p50_ms")
        elif m == "rtt95":      out.append("rtt_p95_ms")
        elif m == "rtt99":      out.append("rtt_p99_ms")
        elif m == "join":       out.append("join_time_s")
        else: raise SystemExit(f"Métrica inválida: {m}")
    return out

def collect(rows, domain, metric):
    """
    Retorna: cuts[bwB] = {"be": [..], "base": [..], "adapt":[..]}
    usando dicionários por be (baseline/adapt) para evitar comprimentos desalinhados.
    """
    # Estrutura: per-bw -> dicts por be
    bw_maps = {}
    for r in rows:
        if r.get("receptor", "").upper() != domain:
            continue
        bw = int(round(sfloat(r.get("bwB"))))
        be = int(round(sfloat(r.get("be_mbps"))))
        val = transform(metric, sfloat(r.get(metric)))
        mode = r.get("mode", "").lower()
        if bw not in bw_maps:
            bw_maps[bw] = {"base": {}, "adapt": {}}
        if math.isnan(val):
            continue
        if mode == "baseline":
            bw_maps[bw]["base"][be]  = val
        elif mode == "adapt":
            bw_maps[bw]["adapt"][be] = val

    # Converte dicionários para listas ordenadas por be (união das chaves)
    cuts = {}
    for bw, maps in bw_maps.items():
        bes = sorted(set(maps["base"].keys()) | set(maps["adapt"].keys()))
        base_list  = [maps["base"].get(b,  np.nan) for b in bes]
        adapt_list = [maps["adapt"].get(b, np.nan) for b in bes]
        cuts[bw] = {"be": bes, "base": base_list, "adapt": adapt_list}
    return cuts

def main():
    ap = argparse.ArgumentParser(description="S2 facets: baseline vs adapt em small-multiples")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--domain", choices=["B","C"], default="B")
    ap.add_argument("--metrics", default="throughput,delivery,jitter,rtt50,rtt95,rtt99,join",
                    help="Lista separada por vírgulas (ou 'all')")
    ap.add_argument("--bwB", nargs="*", type=int, help="Filtrar cortes de bwB (ex.: --bwB 25 50)")
    ap.add_argument("--outdir", default="results/plots")
    ap.add_argument("--maxcols", type=int, default=4, help="máximo de colunas (bwB) na grade")
    args = ap.parse_args()

    rows = load_rows(args.csv)
    mets = metric_list(args.metrics)

    # Primeiro métrica só para descobrir quais bw existem para esse domínio
    cuts0 = collect(rows, args.domain, mets[0])
    bw_all = sorted(cuts0.keys())
    if args.bwB:
        bw_use = [b for b in bw_all if b in set(args.bwB)]
    else:
        bw_use = bw_all
    if not bw_use:
        print(f"[warn] nenhum corte de bwB encontrado para {args.domain}")
        return

    ensure_dir(args.outdir)
    stamp = ts_utc()
    cols = min(len(bw_use), args.maxcols)
    pages = (len(bw_use) + cols - 1) // cols

    for page in range(pages):
        bw_slice = bw_use[page*cols:(page+1)*cols]
        fig, axes = plt.subplots(len(mets), len(bw_slice), figsize=(4.8*len(bw_slice), 2.8*len(mets)), squeeze=False)

        for i, metric in enumerate(mets):
            cuts = collect(rows, args.domain, metric)
            label = METRICS[metric][0]
            for j, bw in enumerate(bw_slice):
                ax = axes[i][j]
                if bw not in cuts:
                    ax.axis("off")
                    continue

                be   = np.array(cuts[bw]["be"], dtype=float)
                base = np.array(cuts[bw]["base"], dtype=float)
                adapt= np.array(cuts[bw]["adapt"], dtype=float)

                # Ordena por be (garantia extra)
                order = np.argsort(be)
                be, base, adapt = be[order], base[order], adapt[order]

                # Plot
                ax.plot(be, base,  marker="o", linewidth=1.8, label="baseline", color="#1f77b4")
                ax.plot(be, adapt, marker="o", linewidth=1.8, label="adapt",    color="#d62728")

                # Anota valores (adapt)
                for x, y in zip(be, adapt):
                    if not math.isnan(y):
                        ax.text(x, y, f"{y:.2f}", ha="center", va="bottom", fontsize=8, color="#333",
                                bbox=dict(facecolor="white", alpha=0.7, edgecolor="none", boxstyle="round,pad=0.15"))

                ax.grid(True, linestyle=":", alpha=0.7)
                if i == 0:
                    ax.set_title(f"bwB={int(bw)} Mbps")
                if j == 0:
                    ax.set_ylabel(label)
                if i == len(mets) - 1:
                    ax.set_xlabel("be_mbps")
                if i == 0 and j == len(bw_slice) - 1:
                    ax.legend(loc="best", fontsize=9)

        fig.suptitle(
            f"S2 — Facets ({args.domain}) — métricas × cortes de bwB (pág {page+1}/{pages})", 
            y=1.02, fontsize=14
        )
        plt.tight_layout()
        out = os.path.join(args.outdir, f"FACETS_S2_{args.domain}_p{page+1}_{stamp}.png")
        plt.savefig(out, dpi=170, bbox_inches="tight")
        plt.close()
        print(f"[ok] salvo: {out}")

if __name__ == "__main__":
    main()
