#!/usr/bin/env python3
import argparse, csv, math, os
import matplotlib.pyplot as plt
from collections import defaultdict

def fix_float_str(s: str):
    if s is None:
        return None
    s = str(s).strip()
    if s == "" or s.lower() == "nan":
        return None
    # Corrige valores tipo ".123" -> "0.123"
    if s.startswith("."):
        s = "0" + s
    return s

def to_int_safe(x):
    s = fix_float_str(x)
    if s is None:
        return None
    try:
        return int(round(float(s)))
    except Exception:
        return None

def to_float_safe(x):
    s = fix_float_str(x)
    if s is None:
        return None
    try:
        return float(s)
    except Exception:
        return None

def read_rows(csv_path):
    with open(csv_path, newline='', encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for raw in rdr:
            # normaliza e mapeia
            row = {}
            row["bwB"]           = to_int_safe(raw.get("bwB"))
            row["be_mbps"]       = to_int_safe(raw.get("be_mbps"))

            row["thr_base"]      = to_float_safe(raw.get("thr_base"))
            row["thr_adapt"]     = to_float_safe(raw.get("thr_adapt"))
            row["thr_delta"]     = to_float_safe(raw.get("thr_delta"))

            row["rtt50_base"]    = to_float_safe(raw.get("rtt50_base"))
            row["rtt50_adapt"]   = to_float_safe(raw.get("rtt50_adapt"))
            row["rtt50_delta"]   = to_float_safe(raw.get("rtt50_delta"))

            row["rtt95_base"]    = to_float_safe(raw.get("rtt95_base"))
            row["rtt95_adapt"]   = to_float_safe(raw.get("rtt95_adapt"))
            row["rtt95_delta"]   = to_float_safe(raw.get("rtt95_delta"))

            row["rtt99_base"]    = to_float_safe(raw.get("rtt99_base"))
            row["rtt99_adapt"]   = to_float_safe(raw.get("rtt99_adapt"))
            row["rtt99_delta"]   = to_float_safe(raw.get("rtt99_delta"))

            row["samples_base"]  = to_int_safe(raw.get("samples_base"))
            row["samples_adapt"] = to_int_safe(raw.get("samples_adapt"))

            # pula linhas sem pivôs obrigatórios
            if row["bwB"] is None or row["be_mbps"] is None:
                continue
            yield row

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="arquivo SWEEP_S1_*.csv")
    ap.add_argument("--outdir", default="results/plots", help="pasta de saída dos PNGs")
    args = ap.parse_args()

    rows = list(read_rows(args.csv))
    if not rows:
        raise SystemExit("CSV sem linhas válidas.")

    os.makedirs(args.outdir, exist_ok=True)

    # organiza por bwB
    by_bw = defaultdict(list)
    for r in rows:
        by_bw[r["bwB"]].append(r)

    # Para cada bwB, ordena por BE e plota
    for bwB, lst in sorted(by_bw.items()):
        lst.sort(key=lambda x: (x["be_mbps"] if x["be_mbps"] is not None else 10**9))

        be = [r["be_mbps"] for r in lst]
        thr_b = [r["thr_base"]  for r in lst]
        thr_a = [r["thr_adapt"] for r in lst]

        # barras: throughput baseline vs adapt
        x = range(len(be))
        width = 0.4

        plt.figure(figsize=(8,4.5))
        # baseline
        plt.bar([i - width/2 for i in x], [v if v is not None else 0 for v in thr_b], width, label="baseline")
        # adapt
        plt.bar([i + width/2 for i in x], [v if v is not None else 0 for v in thr_a], width, label="adapt")

        plt.xticks(list(x), [str(v) for v in be])
        plt.xlabel("BE trafego concorrente (Mbps)")
        plt.ylabel("Throughput (Mbps)")
        plt.title(f"S1 — Throughput (bwB={bwB} Mbps)")
        plt.legend()
        out1 = os.path.join(args.outdir, f"S1_throughput_bwB{bwB}.png")
        plt.tight_layout()
        plt.savefig(out1, dpi=150)
        plt.close()

        # linhas: deltas de RTT (adapt - baseline)
        r50 = [ (r["rtt50_delta"] if r["rtt50_delta"] is not None else 0.0) for r in lst ]
        r95 = [ (r["rtt95_delta"] if r["rtt95_delta"] is not None else 0.0) for r in lst ]
        r99 = [ (r["rtt99_delta"] if r["rtt99_delta"] is not None else 0.0) for r in lst ]

        plt.figure(figsize=(8,4.5))
        plt.plot(be, r50, marker="o", label="Δ RTT p50 (ms)")
        plt.plot(be, r95, marker="o", label="Δ RTT p95 (ms)")
        plt.plot(be, r99, marker="o", label="Δ RTT p99 (ms)")
        plt.axhline(0, linewidth=1)
        plt.xlabel("BE trafego concorrente (Mbps)")
        plt.ylabel("Δ RTT (ms)  adapt - baseline")
        plt.title(f"S1 — Δ RTT vs BE (bwB={bwB} Mbps)")
        plt.legend()
        out2 = os.path.join(args.outdir, f"S1_rtt_delta_bwB{bwB}.png")
        plt.tight_layout()
        plt.savefig(out2, dpi=150)
        plt.close()

    print(f"[ok] Gráficos salvos em: {args.outdir}")

if __name__ == "__main__":
    main()
