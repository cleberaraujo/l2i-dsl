#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, csv, math, pathlib, datetime as dt
import numpy as np
import matplotlib.pyplot as plt

# ---------- util ----------
def ts():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def load_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            def fnum(k, default=np.nan):
                try:
                    return float(row.get(k, ""))
                except Exception:
                    return default
            rows.append({
                "bwB": int(float(row["bwB"])) if row.get("bwB") else np.nan,
                "be": int(float(row["be_mbps"])) if row.get("be_mbps") else np.nan,
                "thr_base": fnum("thr_base"),
                "thr_adapt": fnum("thr_adapt"),
                "thr_delta": fnum("thr_delta"),
                "rtt50_delta": fnum("rtt50_delta"),
                "rtt95_delta": fnum("rtt95_delta"),
                "rtt99_delta": fnum("rtt99_delta"),
            })
    return rows

def pivot(rows, key):
    bw_vals = sorted({r["bwB"] for r in rows if not math.isnan(r["bwB"])})
    be_vals = sorted({r["be"]  for r in rows if not math.isnan(r["be"])})
    M = np.full((len(bw_vals), len(be_vals)), np.nan, dtype=float)
    for r in rows:
        if math.isnan(r["bwB"]) or math.isnan(r["be"]): 
            continue
        i = bw_vals.index(r["bwB"])
        j = be_vals.index(r["be"])
        M[i, j] = r.get(key, np.nan)
    return bw_vals, be_vals, M

def convert_thr_units(M_delta, unit, M_base=None, M_adapt=None):
    if unit == "kbps":
        return M_delta * 1000.0, "Δ throughput (kbps)"
    if unit == "mbps":
        return M_delta, "Δ throughput (Mbit/s)"
    if unit == "percent":
        if M_base is None or M_adapt is None:
            raise ValueError("Para percent é preciso thr_base e thr_adapt.")
        with np.errstate(invalid="ignore", divide="ignore"):
            P = 100.0 * (M_adapt - M_base) / M_base
        return P, "Δ throughput (%)"
    raise ValueError("unit inválida")

def sym_limits(M):
    vmax = np.nanmax(np.abs(M))
    if not np.isfinite(vmax) or vmax == 0:
        vmax = 1.0
    return -vmax, vmax

def annotate_matrix_center(ax, M, fmt, annot_thresh, fontsize=8):
    H, W = M.shape
    for i in range(H):
        for j in range(W):
            v = M[i, j]
            if not np.isfinite(v):
                continue
            if annot_thresh is not None and abs(v) < annot_thresh:
                continue
            ax.text(j, i, fmt(v), ha="center", va="center", fontsize=fontsize, color="black")

def plot_heatmap(M, bw_vals, be_vals, *, title, zlabel, out_png, annot=True, annot_thresh=None, fmt=None):
    vmin, vmax = sym_limits(M)
    fig, ax = plt.subplots(figsize=(6.6, 6.2), dpi=140)
    im = ax.imshow(M, cmap="RdBu_r", vmin=vmin, vmax=vmax, aspect="auto")
    ax.set_xticks(range(len(be_vals)))
    ax.set_xticklabels([str(v) for v in be_vals])
    ax.set_yticks(range(len(bw_vals)))
    ax.set_yticklabels([str(v) for v in bw_vals])
    ax.set_xlabel("be_mbps (tráfego concorrente)")
    ax.set_ylabel("bwB (Mbps)")
    ax.set_title(title)

    # grid entre células
    ax.set_xticks(np.arange(-.5, len(be_vals), 1), minor=True)
    ax.set_yticks(np.arange(-.5, len(bw_vals), 1), minor=True)
    ax.grid(which="minor", color="w", linestyle="-", linewidth=0.8)
    ax.tick_params(which="minor", bottom=False, left=False)

    if annot and fmt is not None:
        annotate_matrix_center(ax, M, fmt=fmt, annot_thresh=annot_thresh, fontsize=8)

    cbar = fig.colorbar(im, ax=ax, shrink=0.86, pad=0.03)
    cbar.set_label(zlabel)
    fig.tight_layout()
    fig.savefig(out_png, bbox_inches="tight")
    plt.close(fig)
    print(f"[ok] salvo: {out_png}")

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="S1 heatmaps — Δ throughput e Δ RTTs (anotação central uniforme)")
    ap.add_argument("--csv", required=True, help="Arquivo SWEEP_S1_*.csv")
    ap.add_argument("--outdir", default="results/plots")
    ap.add_argument("--which", nargs="+",
                    choices=["thr","rtt50","rtt95","rtt99","all"], default=["all"],
                    help="Quais heatmaps gerar (default: all).")
    ap.add_argument("--unit", choices=["kbps","mbps","percent"], default="kbps",
                    help="Unidade do Δ throughput (default: kbps).")
    ap.add_argument("--no-annot", action="store_true", help="Desliga anotações sobre as células.")
    ap.add_argument("--thr-annot-thresh", type=float, default=None,
                    help="Limiar absoluto para anotar Δ throughput (na unidade escolhida).")
    ap.add_argument("--rtt-annot-thresh", type=float, default=None,
                    help="Limiar absoluto para anotar Δ RTT (ms).")
    args = ap.parse_args()

    rows = load_csv(args.csv)
    outdir = pathlib.Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    TSTAMP = ts()

    # limiares padrão
    thr_thresh = args.thr_annot_thresh
    if thr_thresh is None:
        thr_thresh = 0.05 if args.unit == "kbps" else (5e-5 if args.unit == "mbps" else 0.05)  # 50 bps / 5e-5 Mbps / 0.05%
    rtt_thresh = args.rtt_annot_thresh if args.rtt_annot_thresh is not None else 0.1  # ms

    # pivôs
    bw_vals, be_vals, M_thr_delta = pivot(rows, "thr_delta")
    _, _, M_thr_base  = pivot(rows, "thr_base")
    _, _, M_thr_adapt = pivot(rows, "thr_adapt")
    _, _, M_rtt50 = pivot(rows, "rtt50_delta")
    _, _, M_rtt95 = pivot(rows, "rtt95_delta")
    _, _, M_rtt99 = pivot(rows, "rtt99_delta")

    which = set(args.which)
    if "all" in which:
        which = {"thr","rtt50","rtt95","rtt99"}

    # 1) Δ throughput
    if "thr" in which:
        if args.unit == "percent":
            M_thr_plot, zlabel = convert_thr_units(M_thr_delta, "percent", M_thr_base, M_thr_adapt)
            fmt = lambda v: f"{v:.2f}%"
            title = "S1 — Δ throughput (%)"
        elif args.unit == "kbps":
            M_thr_plot, zlabel = convert_thr_units(M_thr_delta, "kbps")
            fmt = lambda v: f"{v:.1f}"
            title = "S1 — Δ throughput (kbps)"
        else:  # mbps
            M_thr_plot, zlabel = convert_thr_units(M_thr_delta, "mbps")
            fmt = lambda v: f"{v:.5f}"
            title = "S1 — Δ throughput (Mbit/s)"

        out_png = outdir / f"HEATMAP_S1_thr_delta_{TSTAMP}.png"
        plot_heatmap(
            M_thr_plot, bw_vals, be_vals,
            title=title, zlabel=zlabel, out_png=out_png,
            annot=not args.no_annot, annot_thresh=thr_thresh, fmt=fmt
        )

    # 2) Δ RTT p50
    if "rtt50" in which:
        plot_heatmap(
            M_rtt50, bw_vals, be_vals,
            title="S1 — Δ RTT p50 (ms)", zlabel="Δ RTT p50 (ms)",
            out_png=outdir / f"HEATMAP_S1_rtt50_delta_{TSTAMP}.png",
            annot=not args.no_annot, annot_thresh=rtt_thresh, fmt=lambda v: f"{v:.1f}"
        )
    # 3) Δ RTT p95
    if "rtt95" in which:
        plot_heatmap(
            M_rtt95, bw_vals, be_vals,
            title="S1 — Δ RTT p95 (ms)", zlabel="Δ RTT p95 (ms)",
            out_png=outdir / f"HEATMAP_S1_rtt95_delta_{TSTAMP}.png",
            annot=not args.no_annot, annot_thresh=rtt_thresh, fmt=lambda v: f"{v:.1f}"
        )
    # 4) Δ RTT p99
    if "rtt99" in which:
        plot_heatmap(
            M_rtt99, bw_vals, be_vals,
            title="S1 — Δ RTT p99 (ms)", zlabel="Δ RTT p99 (ms)",
            out_png=outdir / f"HEATMAP_S1_rtt99_delta_{TSTAMP}.png",
            annot=not args.no_annot, annot_thresh=rtt_thresh, fmt=lambda v: f"{v:.1f}"
        )

if __name__ == "__main__":
    main()
