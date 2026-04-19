# scripts/run_batch.py
# Executa diversos cenários (parâmetros) em K repetições e salva resultados JSON.

from __future__ import annotations
import subprocess, shlex, os, time, json
from pathlib import Path

PY = "python"  # ou "python3"

SCENARIOS = [
  # id, args dict (chaves devem bater com o argparse do mininet_closed_loop_cli.py)
  ("S1_baseline_medium_5_6", {"priority":"medium", "min_mbps":5.0, "max_mbps":6.0, "latency_max_ms":30}),
  ("S2_high_5_8",            {"priority":"high",   "min_mbps":5.0, "max_mbps":8.0, "latency_max_ms":30}),
  ("S3_medium_10_12",        {"priority":"medium", "min_mbps":10.0,"max_mbps":12.0,"latency_max_ms":30}),
  ("S4_medium_5_6_tight",    {"priority":"medium", "min_mbps":5.0, "max_mbps":6.0, "latency_max_ms":20}),
]

REPS = 10
OUTDIR = Path("results/json")
SCEN_SCRIPT = "scenarios/mininet_closed_loop_cli.py"

def run():
  OUTDIR.mkdir(parents=True, exist_ok=True)
  for sid, cfg in SCENARIOS:
    for r in range(1, REPS+1):
      outpath = OUTDIR / f"{sid}_run{r:02d}.json"
      if outpath.exists():
        print("skip existing", outpath)
        continue
      args = [
        "sudo", PY, SCEN_SCRIPT,
        "--scenario_id", sid,
        "--output", str(outpath),
        "--latency_max_ms", str(cfg.get("latency_max_ms",30)),
        "--latency_percentile", cfg.get("latency_percentile","P99"),
        "--min_mbps", str(cfg.get("min_mbps",5.0)),
        "--max_mbps", str(cfg.get("max_mbps",6.0)),
        "--priority", cfg.get("priority","medium"),
        "--be_duration", str(cfg.get("be_duration",18)),
        "--policy_max_priority", cfg.get("policy_max_priority","high"),
        "--policy_bw_ceiling", str(cfg.get("policy_bw_ceiling",20.0)),
        "--policy_rounds", str(cfg.get("policy_rounds",5)),
      ]
      print(">>", " ".join(args))
      t0 = time.time()
      p = subprocess.run(args)
      dt = time.time() - t0
      print(f"[{sid} run {r}] rc={p.returncode} time={dt:.1f}s")
      if p.returncode != 0:
        print("WARN: execução retornou RC != 0; o arquivo pode não existir ou estar incompleto.")

if __name__ == "__main__":
  run()