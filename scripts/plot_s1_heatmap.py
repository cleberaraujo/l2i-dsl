#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import math
import os
from pathlib import Path
import datetime as dt

import numpy as np
import matplotlib.pyplot as plt

def ts():
    # timestamp legível e estável
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def load_rows(csv_path):
    rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            # parse seguro, deixando NaN se vazio/“nan”
            def ffloat(k):
                try:
                    v = r.get(k, "")
                    if v is None or v.strip() == "" or v.strip().lower() == "nan":
                        return float("nan")
                    return float(v)
                except Exception:
                    return float("nan")

            def fint(k):
                try:
                    v = r.get(k, "")
                    if v is None or v.strip() == "":
                        return 0
                    return int(float(v))
                except Exception:
                    return 0

            rows.append({
                "bwB": ffloat("bwB"),
                "be_mbps": ffloat("be_mbps"),
                "thr_base": ffloat("thr_base"),
                "thr_adapt": ffloat("thr_adapt"),
                "thr_delta": ffloat("thr_delta"),   # Mbit/s (pode ser muito pequeno)
                "rtt50_base": ffloat("rtt50_base"),
                "rtt50_adapt": ffloat("rtt50_adapt"),
                "rtt50_delta": ffloat("rtt50_delta"),
                "rtt95_base": ffloat("rtt95_base"),
                "rtt95_adapt": ffloat("rtt95_adapt"),
                "rtt95_delta": ffloat("rtt95_delta"),
                "rtt99_base": ffloat("rtt99_base"),
                "rtt99_adapt": ffloat("rtt99_adapt"),
                "rtt99_delta": ffloat("rtt99_delta"),
                "samples_base": fint("samples_base"),
                "samples_adapt": fint("samples_adapt"),
            })
    return rows

def make_grid(rows, key):
    """
    Constrói matriz (bwB x be) para a métrica 'key'.
    Retorna (mat, ys, xs) com ys=valores únicos de bwB (ordenados) e xs=be_mbps.
    """
    ys = sorted({r["bwB"] for r in rows if not math.isnan(r["bwB"])})
    xs = sorted({r["be_mbps"] for r in rows if not math.isnan(r["be_mbps"])})
    y_ix = {v:i for i,v in enumerate(ys)}
    x_ix = {v:i for i,v in enumerate(xs)}
    mat = np.full((len(ys), len(xs)), np.nan, dtype=float)
    for r in rows:
        y = r["bwB"]; x = r["be_mbps"]
        if math.isnan(y) or math.isnan(x): 
            continue
        i = y_ix[y]; j = x_ix[x]
        v = r.get(key, float("nan"))
        mat[i, j] = v
    return mat, ys, xs

def draw_heatmap(ax, data, ys, xs, *, title, cbar_label, cmap="RdBu_r",
                 vmin=None, vmax=None, annotate=True, fmt="{:.1f}",
                 nan_text="–"):
    im = ax.imshow(data, cmap=cmap, vmin=vmin, vmax=vmax, origin="upper",
                   aspect="equal")
    ax.set_xticks(range(len(xs)))
    ax.set_yticks(range(len(ys)))
    ax.set_xticklabels([str(int(x)) if float(x).is_integer() else str(x) for x in xs])
    ax.set_yticklabels([str(int(y)) if float(y).is_integer() else str(y) for y in ys])
    ax.set_xlabel("be_mbps (tráfego concorrente)")
    ax.set_ylabel("bwB (Mbps)")
    ax.set_title(title, pad=10)

    cbar = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label(cbar_label)

    if annotate:
        ny, nx = data.shape
        # escolha de cor do texto baseada no contraste com o fundo
        for i in range(ny):
            for j in range(nx):
                val = data[i, j]
                if np.isnan(val):
                    txt = nan_text
                else:
                    try:
                        txt = fmt.format(val)
                    except Exception:
                        txt = f"{val:.2f}"
                # cor do texto de acordo com a intensidade local
                # pegamos o valor normalizado da colormap para decidir cor do texto
                if vmin is None or vmax is None or np.isnan(val):
                    text_color = "black"
                else:
                    norm_val = (val - vmin) / (vmax - vmin + 1e-12)
                    # tons escuros (quentes frios) → texto branco
                    text_color = "white" if 0.15 < norm_val < 0.85 and abs(val) > 0.1 else "black"
                ax.text(j, i, txt, ha="center", va="center",
                        fontsize=10, color=text_color,
                        bbox=dict(boxstyle="round,pad=0.2", fc=(1,1,1,0.35), ec="none"))

    return im

def robust_limits(array, symmetric=False, lower_q=0.01, upper_q=0.99):
    """Limites de cor robustos com base em quantis (ignora NaN)."""
    a = array[~np.isnan(array)]
    if a.size == 0:
        return None, None
    lo = np.quantile(a, lower_q)
    hi = np.quantile(a, upper_q)
    if symmetric:
        m = max(abs(lo), abs(hi))
        return -m, m
    return lo, hi

def main():
    p = argparse.ArgumentParser(description="Heatmaps para S1 a partir do CSV do sweep.")
    p.add_argument("--csv", required=True, help="Arquivo CSV gerado por sweep_s1.py")
    p.add_argument("--outdir", default="results/plots", help="Diretório de saída")
    p.add_argument("--no-annot", action="store_true", help="Não desenhar valores nas células")
    args = p.parse_args()

    rows = load_rows(args.csv)
    Path(args.outdir).mkdir(parents=True, exist_ok=True)
    stamp = ts()

    # --- Grids das métricas ---
    rtt50_delta, ys, xs = make_grid(rows, "rtt50_delta")
    rtt95_delta, _, _   = make_grid(rows, "rtt95_delta")
    rtt99_delta, _, _   = make_grid(rows, "rtt99_delta")

    thr_delta_mbps, _, _ = make_grid(rows, "thr_delta")  # Mbit/s
    # Converte para kbps para visualização mais intuitiva
    thr_delta_kbps = thr_delta_mbps * 1000.0

    # Limites de cor (robustos) – simétricos para deltas
    vmin50, vmax50 = robust_limits(rtt50_delta, symmetric=True)
    vmin95, vmax95 = robust_limits(rtt95_delta, symmetric=True)
    vmin99, vmax99 = robust_limits(rtt99_delta, symmetric=True)
    vmin_thr, vmax_thr = robust_limits(thr_delta_kbps, symmetric=True)

    # Formatação dos rótulos:
    # - RTT em ms com 1 casa
    # - Throughput em kbps com 1 casa (ou 2, se preferir)
    annot = not args.no_annot

    # --- RTT p50 ---
    fig, ax = plt.subplots(figsize=(8, 7))
    draw_heatmap(
        ax, rtt50_delta, ys, xs,
        title="S1 — Δ RTT p50 (ms)",
        cbar_label="Δ RTT p50 (ms)",
        vmin=vmin50, vmax=vmax50,
        annotate=annot, fmt="{:.1f}"
    )
    out = Path(args.outdir) / f"HEATMAP_S1_rtt50_delta_{stamp}.png"
    plt.tight_layout()
    plt.savefig(out, dpi=150)
    plt.close(fig)

    # --- RTT p95 ---
    fig, ax = plt.subplots(figsize=(8, 7))
    draw_heatmap(
        ax, rtt95_delta, ys, xs,
        title="S1 — Δ RTT p95 (ms)",
        cbar_label="Δ RTT p95 (ms)",
        vmin=vmin95, vmax=vmax95,
        annotate=annot, fmt="{:.1f}"
    )
    out = Path(args.outdir) / f"HEATMAP_S1_rtt95_delta_{stamp}.png"
    plt.tight_layout()
    plt.savefig(out, dpi=150)
    plt.close(fig)

    # --- RTT p99 ---
    fig, ax = plt.subplots(figsize=(8, 7))
    draw_heatmap(
        ax, rtt99_delta, ys, xs,
        title="S1 — Δ RTT p99 (ms)",
        cbar_label="Δ RTT p99 (ms)",
        vmin=vmin99, vmax=vmax99,
        annotate=annot, fmt="{:.1f}"
    )
    out = Path(args.outdir) / f"HEATMAP_S1_rtt99_delta_{stamp}.png"
    plt.tight_layout()
    plt.savefig(out, dpi=150)
    plt.close(fig)

    # --- Δ throughput (kbps) — agora COM anotações nas células ---
    fig, ax = plt.subplots(figsize=(8, 7))
    draw_heatmap(
        ax, thr_delta_kbps, ys, xs,
        title="S1 — Δ throughput (kbps)",
        cbar_label="Δ throughput (kbps)",
        vmin=vmin_thr, vmax=vmax_thr,
        annotate=annot, fmt="{:.1f}"
    )
    out = Path(args.outdir) / f"HEATMAP_S1_thr_delta_{stamp}.png"
    plt.tight_layout()
    plt.savefig(out, dpi=150)
    plt.close(fig)

    print(f"[ok] Figuras salvas em: {args.outdir}")

if __name__ == "__main__":
    main()
