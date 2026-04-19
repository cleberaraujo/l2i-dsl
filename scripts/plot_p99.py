# scripts/plot_p99.py
# Boxplot de P99 (ms) por cenário a partir dos JSONs.

import json
from pathlib import Path
import matplotlib.pyplot as plt

INDIR = Path("results/json")

def load():
  by_sid = {}
  for f in sorted(INDIR.glob("*.json")):
    js = json.load(open(f))
    sid = js.get("scenario_id","UNK")
    # pega P99 da última avaliação
    p99 = None
    for e in js.get("history", []):
      if e.get("decision","").startswith("evaluate"):
        p99 = e.get("metrics", {}).get("latency", {}).get("p99", None)
    if p99 is not None:
      by_sid.setdefault(sid, []).append(p99)
  return by_sid

def main():
  by_sid = load()
  labels = sorted(by_sid.keys())
  data = [by_sid[k] for k in labels]
  plt.figure()
  plt.boxplot(data, labels=labels, showmeans=True)
  plt.ylabel("RTT P99 (ms)")
  plt.xticks(rotation=20)
  plt.tight_layout()
  plt.savefig("results/agg/p99_boxplot.png", dpi=160)
  print("Saved results/agg/p99_boxplot.png")

if __name__ == "__main__":
  main()