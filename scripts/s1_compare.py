#!/usr/bin/env python3
# scripts/s1_compare.py
#
# Executa S1 em baseline e adapt, coleta os dois resumos S1_*.json
# e gera results/compare/<prefix>.{csv,md}.
#
# Robusto: tenta extrair JSON do stdout por substring { ... },
# e, se falhar, aguarda o S1_*.json mais recente salvo em disco (polling).

import subprocess as sp
import sys, os, json, time, datetime, csv, glob
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable or "/usr/bin/python3"
CMP_DIR = ROOT / "results" / "compare"
JSON_DIR = ROOT / "results" / "json"
CMP_DIR.mkdir(parents=True, exist_ok=True)
JSON_DIR.mkdir(parents=True, exist_ok=True)

def _json_from_stdout(stdout: str) -> Optional[dict]:
    """Tenta pegar a substring entre o primeiro '{' e o último '}'."""
    try:
        i = stdout.find("{")
        j = stdout.rfind("}")
        if i == -1 or j == -1 or j <= i:
            return None
        blob = stdout[i:j+1]
        return json.loads(blob)
    except Exception:
        return None

def _latest_summary_from_disk(now_ts: float, window_sec: int = 300) -> Optional[dict]:
    """Carrega o S1_*.json mais novo modificado recentemente (dentro da janela)."""
    try:
        candidates = list(JSON_DIR.glob("S1_*.json"))
        if not candidates:
            return None
        # ordena por mtime desc
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in candidates:
            age = now_ts - p.stat().st_mtime
            if age <= window_sec:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
        # se nenhum está dentro da janela, tenta o mais recente mesmo assim
        with open(candidates[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _most_recent_summary_path_after(ts_start: float) -> Optional[Path]:
    """Retorna o caminho do S1_*.json mais recente cuja mtime >= ts_start (ou None)."""
    try:
        candidates = list(JSON_DIR.glob("S1_*.json"))
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in candidates:
            if p.stat().st_mtime >= ts_start - 1.0:
                return p
        return None
    except Exception:
        return None

def run_and_capture(cmd, wait_for_summary: int = 300) -> dict:
    """
    Executa o comando (lista) e tenta extrair o JSON:
     - 1) tenta extrair JSON do stdout
     - 2) aguarda por um arquivo S1_*.json com mtime >= start_time (polling)
     - 3) fallback: carrega o S1_*.json mais novo dentro da janela (se existir)
    """
    print("[CMD]", " ".join(cmd))
    start_time = time.time()
    p = sp.run(cmd, cwd=ROOT, stdout=sp.PIPE, stderr=sp.STDOUT, text=True)
    out = p.stdout or ""
    print(out)

    # 1) tenta JSON direto do stdout (entre { ... })
    j = _json_from_stdout(out)
    if j is not None:
        return j

    # 2) polling por arquivo S1_*.json gerado recentemente (mtime >= start_time)
    deadline = time.time() + wait_for_summary
    while time.time() < deadline:
        cand = _most_recent_summary_path_after(start_time)
        if cand is not None:
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    j = json.load(f)
                return j
            except Exception:
                # talvez arquivo parcial — aguarda e tenta de novo
                pass
        time.sleep(1.0)

    # 3) fallback: tenta qualquer JSON recente no disco (janela padrão)
    j = _latest_summary_from_disk(time.time(), window_sec=300)
    if j is not None:
        return j

    raise RuntimeError("Não foi possível capturar o JSON (stdout ou arquivo S1_*.json).")

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default="specs/valid/s1_unicast_qos.json")
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--be-mbps", type=int, default=30)
    ap.add_argument("--bwA", type=int, default=100)
    ap.add_argument("--bwB", type=int, default=50)
    ap.add_argument("--bwC", type=int, default=100)
    ap.add_argument("--delay-ms", type=int, default=1)
    ap.add_argument("--outfile-prefix", default=None,
                    help="prefixo em results/compare/ (default = timestamp UTC)")
    ap.add_argument("--wait-json-seconds", type=int, default=300,
                    help="quanto tempo aguardar por um S1_*.json gerado pelo cenário (segundos)")
    args = ap.parse_args()

    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    prefix = args.outfile_prefix or f"CMP_{ts}"

    base_cmd = [
        "sudo", PY, "-m", "scenarios.multidomain_s1",
        "--spec", args.spec,
        "--duration", str(args.duration),
        "--be-mbps", str(args.be_mbps),
        "--bwA", str(args.bwA),
        "--bwB", str(args.bwB),
        "--bwC", str(args.bwC),
        "--delay-ms", str(args.delay_ms),
    ]

    # baseline
    print("[INFO] Running baseline...")
    j_base = run_and_capture(base_cmd + ["--mode", "baseline"], wait_for_summary=args.wait_json_seconds)

    # adapt (pequena pausa para não colidir timestamps)
    time.sleep(1.0)
    print("[INFO] Running adapt...")
    j_adpt = run_and_capture(base_cmd + ["--mode", "adapt"], wait_for_summary=args.wait_json_seconds)

    # ===== CSV =====
    csv_path = CMP_DIR / f"{prefix}.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "baseline", "adapt", "delta_adapt_minus_baseline"])
        def getm(j, k):
            try:
                return j["metrics"][k]
            except Exception:
                return None
        def row(name):
            b = getm(j_base, name); a = getm(j_adpt, name)
            d = None
            if isinstance(a, (int,float)) and isinstance(b, (int,float)):
                d = a - b
            w.writerow([name, b, a, d])
        for k in ("throughput_mbps","rtt_p50_ms","rtt_p95_ms","rtt_p99_ms","rtt_samples"):
            row(k)

    # ===== Markdown =====
    md_path = CMP_DIR / f"{prefix}.md"
    def fmt(v):
        if v is None: return "—"
        if isinstance(v, float): return f"{v:.3f}"
        return str(v)

    base_id = j_base.get("timestamp_utc", "BASE")
    adpt_id = j_adpt.get("timestamp_utc", "ADPT")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# S1 compare: baseline vs adapt\n\n")
        f.write(f"- spec: `{args.spec}`  \n")
        f.write(f"- duration: {args.duration}s, be_mbps: {args.be_mbps}  \n")
        f.write(f"- baseline: `results/json/S1_{base_id}.json`\n")
        f.write(f"- adapt:    `results/json/S1_{adpt_id}.json`\n\n")
        f.write("## Métricas\n\n")
        f.write("| métrica | baseline | adapt | Δ (adapt−baseline) |\n")
        f.write("|---|---:|---:|---:|\n")
        pairs = [
            ("throughput_mbps", j_base["metrics"].get("throughput_mbps"), j_adpt["metrics"].get("throughput_mbps")),
            ("rtt_p50_ms",      j_base["metrics"].get("rtt_p50_ms"),      j_adpt["metrics"].get("rtt_p50_ms")),
            ("rtt_p95_ms",      j_base["metrics"].get("rtt_p95_ms"),      j_adpt["metrics"].get("rtt_p95_ms")),
            ("rtt_p99_ms",      j_base["metrics"].get("rtt_p99_ms"),      j_adpt["metrics"].get("rtt_p99_ms")),
            ("rtt_samples",     j_base["metrics"].get("rtt_samples"),     j_adpt["metrics"].get("rtt_samples")),
        ]
        for name, b, a in pairs:
            d = (a - b) if (isinstance(a,(int,float)) and isinstance(b,(int,float))) else None
            f.write(f"| {name} | {fmt(b)} | {fmt(a)} | {fmt(d)} |\n")

    print(f"[OK] CSV: {csv_path}")
    print(f"[OK] MD : {md_path}")

if __name__ == "__main__":
    main()
