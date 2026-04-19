#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, csv, os, math, datetime as dt
from collections import defaultdict
import numpy as np
import matplotlib.pyplot as plt

# ------------- util -------------
def ts_utc():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def ensure_dir(p): os.makedirs(p, exist_ok=True)

def sfloat(x):
    try:
        if x is None or x == "": return float("nan")
        return float(x)
    except:
        return float("nan")

# ------------- parsing (pivot row-wise -> paired) -------------
METRICS = ["throughput_mbps","rtt_p50_ms","rtt_p95_ms","rtt_p99_ms","jitter_ms","delivery_ratio","join_time_s"]

def load_rows(path):
    with open(path, "r", encoding="utf-8") as f:
        dr = csv.DictReader(f)
        rows = []
        for r in dr:
            row = {k.strip(): (v.strip() if isinstance(v,str) else v) for k,v in r.items()}
            rows.append(row)
    return rows

def pivot_pairs(rows):
    """
    Constrói um dicionário:
      data[(receptor, bwB, be_mbps)] = {
         'baseline': {metric -> value}, 'adapt': {metric -> value}
      }
    """
    data = defaultdict(lambda: {"baseline": {}, "adapt": {}})
    for r in rows:
        mode = (r.get("mode") or "").strip().lower()         # 'baseline'/'adapt'
        rec  = (r.get("receptor") or "").strip().upper()     # 'B'/'C'
        bwB  = sfloat(r.get("bwB"))
        be   = sfloat(r.get("be_mbps"))

        if rec not in ("B","C"):  # ignora outras coisas, se houver
            continue
        if math.isnan(bwB) or math.isnan(be):
            continue

        key = (rec, int(round(bwB)), int(round(be)))
        bucket = data[key]["baseline" if mode=="baseline" else "adapt"]

        for m in METRICS:
            if m in r:
                bucket[m] = sfloat(r[m])

    return data

# ------------- heatmap helpers -------------
def color_value(metric, variant, val):
    if math.isnan(val): return np.nan
    # delivery_ratio: exibe em pp (0..100) na cor
    if metric == "delivery_ratio":
        return val*100.0 if val <= 1.0 else val
    return val

def cell_label(metric, variant, val):
    if math.isnan(val): return ""
    if metric == "throughput_mbps":
        # deltas pequenos → kbps
        if variant == "delta" and abs(val) < 0.5:
            return f"{val*1000:.1f}"
        return f"{val:.3f}"
    if metric in ("rtt_p50_ms","rtt_p95_ms","rtt_p99_ms","jitter_ms"):
        return f"{val:.1f}"
    if metric == "delivery_ratio":
        # rótulo sempre em % com 1 casa
        v = val*100.0 if val <= 1.0 else val
        return f"{v:.1f}"
    if metric == "join_time_s":
        return f"{val:.2f}"
    return f"{val:.2f}"

def annotate(ax, TXT):
    for i in range(len(TXT)):
        for j in range(len(TXT[0])):
            t = TXT[i][j]
            if not t: continue
            ax.text(j, i, t, ha="center", va="center",
                    fontsize=9, color="black",
                    bbox=dict(facecolor="white", alpha=0.6, edgecolor="none", boxstyle="round,pad=0.25"))

def build_matrix_from_pairs(pairs, domain, metric, variant):
    # grade de eixos
    xs = sorted({ be for (rec,bw,be) in pairs.keys() if rec==domain })
    ys = sorted({ bw for (rec,bw,be) in pairs.keys() if rec==domain })
    if not xs or not ys:
        return None, [], [], []

    xi = {x:i for i,x in enumerate(xs)}
    yi = {y:i for i,y in enumerate(ys)}

    M   = np.full((len(ys), len(xs)), np.nan, dtype=float)
    TXT = [[ "" for _ in xs ] for __ in ys]

    for (rec,bw,be), buckets in pairs.items():
        if rec != domain: continue
        base  = buckets["baseline"].get(metric, float("nan"))
        adapt = buckets["adapt"].get(metric, float("nan"))
        if variant == "base":
            v = base
        elif variant == "adapt":
            v = adapt
        else:  # delta
            v = (adapt - base) if (not math.isnan(adapt) and not math.isnan(base)) else float("nan")

        M[yi[bw], xi[be]] = color_value(metric, variant, v)
        TXT[yi[bw]][xi[be]] = cell_label(metric, variant, v)

    return M, xs, ys, TXT

def plot_matrix(M, xs, ys, title, cbar_label, TXT, outpng, cmap):
    import matplotlib
    matplotlib.use("Agg")  # garante headless
    fig, ax = plt.subplots(figsize=(7.2,7.2))
    im = ax.imshow(M, cmap=cmap, aspect="auto")
    ax.set_xticks(np.arange(len(xs))); ax.set_xticklabels([str(int(x)) for x in xs])
    ax.set_yticks(np.arange(len(ys))); ax.set_yticklabels([str(int(y)) for y in ys])
    ax.set_xlabel("be_mbps (tráfego concorrente)")
    ax.set_ylabel("bwB (Mbps)")
    ax.set_title(title)
    cbar = fig.colorbar(im, ax=ax); cbar.set_label(cbar_label)
    annotate(ax, TXT)
    fig.tight_layout()
    ensure_dir(os.path.dirname(outpng)); fig.savefig(outpng, dpi=160); plt.close(fig)
    print(f"[ok] salvo: {outpng}")

# ------------- CLI -------------
def parse_args():
    ap = argparse.ArgumentParser(description="Heatmaps do S2 a partir de SWEEP_S2_*.csv (row-wise)")
    ap.add_argument("--csv", required=True, help="Arquivo SWEEP_S2_*.csv")
    ap.add_argument("--variant", choices=["delta","base","adapt"], default="delta")
    ap.add_argument("--domain", choices=["both","B","C"], default="both")
    ap.add_argument("--metrics", default="throughput,rtt50,rtt95,rtt99,jitter,delivery,join",
                    help="Lista separada por vírgulas (or 'all')")
    ap.add_argument("--outdir", default="results/plots")
    ap.add_argument("--inspect", action="store_true", help="Resumo dos pares baseline/adapt e sai")
    return ap.parse_args()

def metric_list(s):
    if s.strip().lower()=="all":
        s = "throughput,rtt50,rtt95,rtt99,jitter,delivery,join"
    return [x.strip() for x in s.split(",") if x.strip()]

def metric_to_canon(m):
    m = m.lower()
    if m=="throughput": return "throughput_mbps", ("Δ throughput (Mbit/s)","Throughput (Mbit/s)")
    if m=="rtt50":      return "rtt_p50_ms",      ("Δ RTT p50 (ms)","RTT p50 (ms)")
    if m=="rtt95":      return "rtt_p95_ms",      ("Δ RTT p95 (ms)","RTT p95 (ms)")
    if m=="rtt99":      return "rtt_p99_ms",      ("Δ RTT p99 (ms)","RTT p99 (ms)")
    if m=="jitter":     return "jitter_ms",       ("Δ jitter (ms)","Jitter (ms)")
    if m=="delivery":   return "delivery_ratio",  ("Δ delivery ratio (pp)","Delivery ratio (%)")
    if m=="join":       return "join_time_s",     ("Δ join time (s)","Join time (s)")
    raise ValueError(m)

def main():
    args = parse_args()
    rows  = load_rows(args.csv)
    pairs = pivot_pairs(rows)

    if args.inspect:
        # estatísticas úteis
        by_dom = {"B": [], "C": []}
        for (rec,bw,be), buckets in pairs.items():
            has_b = any(m in buckets["baseline"] for m in METRICS)
            has_a = any(m in buckets["adapt"]   for m in METRICS)
            by_dom[rec].append((has_b, has_a))
        for d in ("B","C"):
            total = len(by_dom[d])
            paired = sum(1 for hb,ha in by_dom[d] if hb and ha)
            print(f"[inspect] domínio {d}: pontos={total}, pareados(baseline+adapt)={paired} ({(paired/total*100 if total else 0):.1f}%)")
        return

    doms = ["B","C"] if args.domain=="both" else [args.domain]
    mets = metric_list(args.metrics)
    stamp = ts_utc()
    ensure_dir(args.outdir)

    for dom in doms:
        for m in mets:
            canon, labels = metric_to_canon(m)
            title = f"S2 — {labels[0] if args.variant=='delta' else labels[1]} ({dom})"
            cmap  = "RdBu_r" if args.variant=="delta" else "YlGnBu"

            M, xs, ys, TXT = build_matrix_from_pairs(pairs, dom, canon, args.variant)
            if M is None or not xs or not ys:
                print(f"[warn] nada para plotar em {dom}/{m} (faltam pontos)")
                continue

            out = os.path.join(args.outdir, f"HEATMAP_S2_{dom}_{m}_{args.variant}_{stamp}.png")
            plot_matrix(M, xs, ys, title, title.split(" — ",1)[1], TXT, out, cmap)

if __name__ == "__main__":
    main()
