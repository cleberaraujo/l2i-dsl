#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, csv, os, math, datetime as dt
from collections import defaultdict, OrderedDict
import numpy as np
import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt

# -------------------- util --------------------
def ts_utc():
    # timezone-aware (evita DeprecationWarning do utcnow)
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def ensure_dir(p): os.makedirs(p, exist_ok=True)

def sfloat(x):
    try:
        if x is None or x == "": return float("nan")
        return float(x)
    except:
        return float("nan")

# -------------------- métricas --------------------
METRICS = OrderedDict([
    ("throughput_mbps", ("Throughput (Mbit/s)",            "Δ throughput (Mbit/s)",           "YlGnBu", "RdBu_r")),
    ("rtt_p50_ms",      ("RTT p50 (ms)",                   "Δ RTT p50 (ms)",                  "YlGnBu", "RdBu_r")),
    ("rtt_p95_ms",      ("RTT p95 (ms)",                   "Δ RTT p95 (ms)",                  "YlGnBu", "RdBu_r")),
    ("rtt_p99_ms",      ("RTT p99 (ms)",                   "Δ RTT p99 (ms)",                  "YlGnBu", "RdBu_r")),
    ("jitter_ms",       ("Jitter (ms)",                    "Δ Jitter (ms)",                   "YlGnBu", "RdBu_r")),
    ("delivery_ratio",  ("Delivery ratio (%)",             "Δ Delivery ratio (pp)",           "YlGnBu", "RdBu_r")),
    ("join_time_s",     ("Join time (s)",                  "Δ Join time (s)",                 "YlGnBu", "RdBu_r")),
])

def label_for(metric, variant):
    base, delta, *_ = METRICS[metric]
    return delta if variant == "delta" else base

def transform_value(metric, variant, v):
    """Converte para unidade de exibição (eixo Y)."""
    if math.isnan(v): return np.nan
    if metric == "delivery_ratio":
        # sempre em %, delta em pp
        return v*100.0 if v <= 1.0 else v
    return v

# -------------------- parsing & pivot --------------------
def load_rows(path):
    with open(path, "r", encoding="utf-8") as f:
        dr = csv.DictReader(f)
        rows = []
        for r in dr:
            row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
            rows.append(row)
    return rows

def pivot_pairs(rows):
    """
    data[(receptor, bwB, be)] = {
        'baseline': {metric -> val}, 'adapt': {metric -> val}
    }
    """
    data = defaultdict(lambda: {"baseline": {}, "adapt": {}})
    for r in rows:
        mode = (r.get("mode") or "").strip().lower()
        rec  = (r.get("receptor") or "").strip().upper()
        bwB  = sfloat(r.get("bwB"))
        be   = sfloat(r.get("be_mbps"))
        if rec not in ("B", "C"): 
            continue
        if math.isnan(bwB) or math.isnan(be):
            continue
        key = (rec, int(round(bwB)), int(round(be)))
        bucket = data[key]["baseline" if mode == "baseline" else "adapt"]
        for m in METRICS.keys():
            if m in r:
                bucket[m] = sfloat(r[m])
    return data

# -------------------- curvas --------------------
def collect_series(pairs, domain, metric, variant, bw_filter=None):
    """
    Retorna um dict: series[bwB] = (xs_be_sorted, ys_vals)
    xs: be_mbps, ys: valor transformado (pode ser delta)
    """
    # descobrir todos os bwB disponíveis para o domínio
    all_bw = sorted({ bw for (rec,bw,be) in pairs.keys() if rec == domain })
    if bw_filter:
        chosen = [b for b in all_bw if b in bw_filter]
    else:
        chosen = all_bw

    series = OrderedDict()
    for bw in chosen:
        # para este bw, colete todos os be
        bes = sorted({ be for (rec,b,be) in pairs.keys() if rec == domain and b == bw })
        xs, ys = [], []
        for be in bes:
            buckets = pairs.get((domain, bw, be), None)
            if not buckets:
                continue
            base  = buckets["baseline"].get(metric, float("nan"))
            adapt = buckets["adapt"].get(metric, float("nan"))
            if variant == "base":
                val = base
            elif variant == "adapt":
                val = adapt
            else:
                # delta requer ambos disponíveis
                val = (adapt - base) if (not math.isnan(adapt) and not math.isnan(base)) else float("nan")
            tv = transform_value(metric, variant, val)
            if not math.isnan(tv):
                xs.append(be); ys.append(tv)
        if xs:
            series[bw] = (xs, ys)
    return series

def plot_curves(series, title, ylabel, outpng):
    ensure_dir(os.path.dirname(outpng))
    plt.figure(figsize=(7.5, 5.0))
    for bw, (xs, ys) in series.items():
        # ordena por be apenas por segurança
        order = np.argsort(xs)
        xs_sorted = np.array(xs)[order]
        ys_sorted = np.array(ys)[order]
        plt.plot(xs_sorted, ys_sorted, marker="o", linewidth=1.8, label=f"bwB={bw} Mbps")

        # coloca rótulos de pontos (opcional; aqui deixo discreto)
        for x, y in zip(xs_sorted, ys_sorted):
            plt.text(x, y, f"{y:.2f}", ha="center", va="bottom", fontsize=8, color="#333333",
                     bbox=dict(facecolor="white", alpha=0.7, edgecolor="none", boxstyle="round,pad=0.15"))

    plt.title(title)
    plt.xlabel("be_mbps (tráfego concorrente)")
    plt.ylabel(ylabel)
    plt.grid(True, linestyle=":", linewidth=0.6, alpha=0.7)
    plt.legend(title="Cortes de bwB", ncol=2, fontsize=9)
    plt.tight_layout()
    plt.savefig(outpng, dpi=160)
    plt.close()
    print(f"[ok] salvo: {outpng}")

# -------------------- CLI --------------------
def parse_args():
    ap = argparse.ArgumentParser(description="Curvas S2 (vs be_mbps) por bwB para cada domínio")
    ap.add_argument("--csv", required=True, help="Arquivo SWEEP_S2_*.csv (row-wise)")
    ap.add_argument("--variant", choices=["delta", "base", "adapt"], default="delta")
    ap.add_argument("--domain", choices=["both", "B", "C"], default="both")
    ap.add_argument("--metrics", default="throughput,rtt50,rtt95,rtt99,jitter,delivery,join",
                    help="Lista separada por vírgulas (or 'all')")
    ap.add_argument("--bwB", nargs="*", type=int, help="Filtrar determinados cortes de bwB (ex.: --bwB 10 25 50)")
    ap.add_argument("--outdir", default="results/plots")
    ap.add_argument("--inspect", action="store_true", help="Mostra um resumo dos dados e sai")
    return ap.parse_args()

def metric_list(s):
    if s.strip().lower() == "all":
        s = "throughput,rtt50,rtt95,rtt99,jitter,delivery,join"
    out = []
    for m in [x.strip() for x in s.split(",") if x.strip()]:
        m = m.lower()
        if   m == "throughput": out.append("throughput_mbps")
        elif m == "rtt50":      out.append("rtt_p50_ms")
        elif m == "rtt95":      out.append("rtt_p95_ms")
        elif m == "rtt99":      out.append("rtt_p99_ms")
        elif m == "jitter":     out.append("jitter_ms")
        elif m == "delivery":   out.append("delivery_ratio")
        elif m == "join":       out.append("join_time_s")
        else:
            raise SystemExit(f"Métrica inválida: {m}")
    return out

def main():
    args = parse_args()
    rows  = load_rows(args.csv)
    pairs = pivot_pairs(rows)

    if args.inspect:
        for dom in ("B", "C"):
            pts = [k for k in pairs.keys() if k[0] == dom]
            bes = sorted({ k[2] for k in pts })
            bws = sorted({ k[1] for k in pts })
            paired = 0
            total  = 0
            for k in pts:
                b = pairs[k]["baseline"]; a = pairs[k]["adapt"]
                if (any(m in b for m in METRICS) and any(m in a for m in METRICS)):
                    paired += 1
                total += 1
            print(f"[inspect] dom {dom}: pontos={total}, pareados={paired} ({(paired/total*100 if total else 0):.1f}%), bwB={bws}, be={bes}")
        return

    stamp = ts_utc()
    ensure_dir(args.outdir)
    doms = ["B", "C"] if args.domain == "both" else [args.domain]
    mets = metric_list(args.metrics)

    for dom in doms:
        for metric in mets:
            series = collect_series(pairs, dom, metric, args.variant, bw_filter=args.bwB)
            if not series:
                print(f"[warn] nada para plotar em dom={dom}, metric={metric} (faltam pontos)")
                continue
            title  = f"S2 — {label_for(metric, args.variant)} ({dom})"
            ylabel = label_for(metric, args.variant)
            out    = os.path.join(args.outdir, f"CURVES_S2_{dom}_{metric}_{args.variant}_{stamp}.png")
            plot_curves(series, title, ylabel, out)

if __name__ == "__main__":
    main()
