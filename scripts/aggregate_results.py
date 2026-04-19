# scripts/aggregate_results.py
# Agrega resultados JSON -> CSV com média, desvio, IC95% (Student t para n=10: 2.262).

from __future__ import annotations
import json, math
from pathlib import Path
from statistics import mean, stdev

INDIR = Path("results/json")
OUTDIR = Path("results/agg")

def t_crit_95(df: int) -> float:
  # Aproximação: se df==9 (n=10), t=2.262; df>=30 ~1.96
  if df <= 1: return 12.706  # df=1
  if df == 2: return 4.303
  if df == 3: return 3.182
  if df == 4: return 2.776
  if df == 5: return 2.571
  if df == 6: return 2.447
  if df == 7: return 2.365
  if df == 8: return 2.306
  if df == 9: return 2.262
  if df < 30: return 2.045
  return 1.960

def extract_metrics(js: dict):
  # Usa última medição da história (round evaluate) — tipicamente a de convergência ou parada
  last_eval = None
  for e in js.get("history", []):
    if e.get("decision","").startswith("evaluate"):
      last_eval = e
  if not last_eval:
    return None
  lat = last_eval.get("metrics", {}).get("latency", {})
  p99 = lat.get("p99", float("nan"))
  # throughput_mbps se presente
  thr = last_eval.get("metrics", {}).get("throughput_mbps", None)
  return {
    "converged": bool(js.get("converged", False)),
    "rounds": int(js.get("rounds", 0)),
    "p99_ms": float(p99),
    "thr_mbps": float(thr) if (thr is not None) else None
  }

def aggregate():
  OUTDIR.mkdir(parents=True, exist_ok=True)
  groups = {}
  for f in sorted(INDIR.glob("*.json")):
    with open(f, "r", encoding="utf-8") as fh:
      js = json.load(fh)
    sid = js.get("scenario_id","UNK")
    m = extract_metrics(js)
    if not m: 
      continue
    groups.setdefault(sid, []).append(m)

  # Gera CSVs
  # 1) Resumo por cenário: convergência (%), rodadas (média ± IC95), p99 (média ± IC95), throughput (se houver)
  lines = []
  header = ["scenario_id","n","convergence_rate","rounds_mean","rounds_ic95","p99_mean_ms","p99_ic95_ms","thr_mean_mbps","thr_ic95_mbps"]
  lines.append(",".join(header))
  for sid, arr in groups.items():
    n = len(arr)
    conv = sum(1 for x in arr if x["converged"]) / n if n else 0.0
    rounds = [x["rounds"] for x in arr]
    p99s = [x["p99_ms"] for x in arr]
    thrs = [x["thr_mbps"] for x in arr if x["thr_mbps"] is not None]

    def mean_ci95(xs):
      if not xs: return (float("nan"), float("nan"))
      m = mean(xs)
      if len(xs) == 1: return (m, float("nan"))
      s = stdev(xs)
      t = t_crit_95(len(xs)-1)
      half = t * s / math.sqrt(len(xs))
      return (m, half)

    r_m, r_ci = mean_ci95(rounds)
    p_m, p_ci = mean_ci95(p99s)
    if thrs:
      t_m, t_ci = mean_ci95(thrs)
    else:
      t_m, t_ci = (float("nan"), float("nan"))

    lines.append(",".join([
      sid, str(n), f"{conv:.3f}", 
      f"{r_m:.3f}", f"{r_ci:.3f}",
      f"{p_m:.3f}", f"{p_ci:.3f}",
      (f"{t_m:.3f}" if not math.isnan(t_m) else ""), 
      (f"{t_ci:.3f}" if not math.isnan(t_ci) else "")
    ]))

  with open(OUTDIR / "summary.csv", "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))

  print("Wrote:", OUTDIR / "summary.csv")

if __name__ == "__main__":
  aggregate()