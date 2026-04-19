#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import datetime as dt
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

# ------------------------------------------------------------
# util
# ------------------------------------------------------------

def utc_ts() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def to_float(x) -> float:
    try:
        if x is None:
            return math.nan
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if s == "" or s.lower() in ("nan", "none", "null"):
            return math.nan
        return float(s)
    except Exception:
        return math.nan

def safe_delta(a: float, b: float) -> float:
    if not (math.isfinite(a) and math.isfinite(b)):
        return math.nan
    return b - a

# formata SEM notação científica; se não for finito, retorna 'NaN'
def fmt_fixed(x: float, decimals: int) -> str:
    if not math.isfinite(x):
        return "NaN"
    return f"{x:.{decimals}f}"

# ------------------------------------------------------------
# captura robusta de JSON
# ------------------------------------------------------------

def extract_last_json(blob: str) -> Optional[dict]:
    """
    Procura o ÚLTIMO bloco que começa em '{' e tenta json.loads.
    Recuamos progressivamente se falhar (resiliente a lixo antes/depois).
    """
    i = blob.rfind("{")
    while i != -1:
        candidate = blob[i:]
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and "scenario" in obj and "metrics" in obj:
                return obj
        except Exception:
            pass
        i = blob.rfind("{", 0, i)
    return None

def run_once(cmd: list, verbose: bool, timeout: Optional[int]) -> Tuple[bool, Optional[dict], str, str]:
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    out = p.stdout or ""
    err = p.stderr or ""
    obj = extract_last_json(out)
    ok = obj is not None
    if verbose and not ok:
        sys.stderr.write(f"[warn] parsing falhou (rc={p.returncode}). tail stdout:\n{out[-400:]}\n")
        if err:
            sys.stderr.write(f"[warn] tail stderr:\n{err[-400:]}\n")
    return ok, obj, out, err

def run_and_capture(cmd: list, verbose: bool=False, timeout: Optional[int]=None, retry: int=1) -> Tuple[bool, Optional[dict]]:
    ok, obj, out, err = run_once(cmd, verbose, timeout)
    if ok:
        # sanity: requer ao menos uma amostra de RTT (evita JSON parcial)
        m = obj.get("metrics", {}) if obj else {}
        smp = m.get("rtt_samples", 0)
        try:
            smp = int(float(smp))
        except Exception:
            smp = 0
        if smp > 0:
            return True, obj
        # se amostras == 0, trata como falha de coleta
        ok = False

    # retry uma vez (se configurado)
    attempts = retry if retry is not None else 0
    while not ok and attempts > 0:
        time.sleep(0.4)
        ok, obj, out, err = run_once(cmd, verbose, timeout)
        if ok:
            m = obj.get("metrics", {}) if obj else {}
            smp = m.get("rtt_samples", 0)
            try:
                smp = int(float(smp))
            except Exception:
                smp = 0
            if smp > 0:
                return True, obj
            ok = False
        attempts -= 1

    return False, None

# ------------------------------------------------------------
# leitura de métricas do sumário S1
# ------------------------------------------------------------

def get_metrics(j: dict):
    m = j.get("metrics", {})
    thr = to_float(m.get("throughput_mbps"))
    r50 = to_float(m.get("rtt_p50_ms"))
    r95 = to_float(m.get("rtt_p95_ms"))
    r99 = to_float(m.get("rtt_p99_ms"))
    smp = m.get("rtt_samples", 0)
    try:
        smp = int(float(smp))
    except Exception:
        smp = 0
    return thr, r50, r95, r99, smp

# ------------------------------------------------------------
# sweep
# ------------------------------------------------------------

HEADER = [
    "bwB","be_mbps",
    "thr_base","thr_adapt","thr_delta",
    "rtt50_base","rtt50_adapt","rtt50_delta",
    "rtt95_base","rtt95_adapt","rtt95_delta",
    "rtt99_base","rtt99_adapt","rtt99_delta",
    "samples_base","samples_adapt",
]

def main():
    ap = argparse.ArgumentParser(description="Sweep S1 (unicast QoS) com coleta robusta e saída limpa.")
    ap.add_argument("--spec", required=True, help="Spec JSON do S1 (ex.: specs/valid/s1_unicast_qos.json)")
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--bwA", type=int, default=100)
    ap.add_argument("--bwC", type=int, default=100)
    ap.add_argument("--delay-ms", type=int, default=1)
    ap.add_argument("--bwB", type=int, nargs="+", required=True, help="Lista de capacidade no domínio B (ex.: 100 50 25 10)")
    ap.add_argument("--be", type=int, nargs="+", required=True, help="Lista de tráfego concorrente em Mbps (ex.: 10 30 60 90)")
    ap.add_argument("--outdir", default="results/compare", help="Diretório para salvar CSV")
    ap.add_argument("--python", default="python3", help="Python p/ invocar o cenário")
    ap.add_argument("--timeout", type=int, default=None, help="Timeout (s) por execução do cenário")
    ap.add_argument("--retry", type=int, default=1, help="Reexecuções quando parsing/metricas falham (default: 1)")
    ap.add_argument("--decimals", type=int, default=6, help="Casas decimais ao salvar floats (default: 6)")
    ap.add_argument("--verbose", action="store_true", help="Mostra diagnóstico em falhas de parsing/metricas")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    ensure_dir(outdir)
    ts = utc_ts()
    out_csv = outdir / f"SWEEP_S1_{ts}.csv"

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)

        for bwB in args.bwB:
            for be in args.be:
                base_cmd = [
                    args.python, "-m", "scenarios.multidomain_s1",
                    "--spec", args.spec,
                    "--duration", str(args.duration),
                    "--be-mbps", str(be),
                    "--bwA", str(args.bwA),
                    "--bwB", str(bwB),
                    "--bwC", str(args.bwC),
                    "--delay-ms", str(args.delay_ms),
                    "--mode", "baseline",
                ]
                print(f"[CMD] bwB={bwB} be={be} mode=baseline")
                ok_b, jb = run_and_capture(base_cmd, verbose=args.verbose, timeout=args.timeout, retry=args.retry)

                adapt_cmd = base_cmd[:-2] + ["--mode", "adapt"]
                print(f"[CMD] bwB={bwB} be={be} mode=adapt")
                ok_a, ja = run_and_capture(adapt_cmd, verbose=args.verbose, timeout=args.timeout, retry=args.retry)

                if ok_b:
                    thr_b, r50_b, r95_b, r99_b, smp_b = get_metrics(jb)
                else:
                    thr_b = r50_b = r95_b = r99_b = math.nan
                    smp_b = 0

                if ok_a:
                    thr_a, r50_a, r95_a, r99_a, smp_a = get_metrics(ja)
                else:
                    thr_a = r50_a = r95_a = r99_a = math.nan
                    smp_a = 0

                d_thr = safe_delta(thr_b, thr_a)
                d_r50 = safe_delta(r50_b, r50_a)
                d_r95 = safe_delta(r95_b, r95_a)
                d_r99 = safe_delta(r99_b, r99_a)

                # escreve com formatação de ponto fixo (sem notação científica)
                row = [
                    str(bwB), str(be),
                    fmt_fixed(thr_b, args.decimals),
                    fmt_fixed(thr_a, args.decimals),
                    fmt_fixed(d_thr, args.decimals),
                    fmt_fixed(r50_b, args.decimals),
                    fmt_fixed(r50_a, args.decimals),
                    fmt_fixed(d_r50, args.decimals),
                    fmt_fixed(r95_b, args.decimals),
                    fmt_fixed(r95_a, args.decimals),
                    fmt_fixed(d_r95, args.decimals),
                    fmt_fixed(r99_b, args.decimals),
                    fmt_fixed(r99_a, args.decimals),
                    fmt_fixed(d_r99, args.decimals),
                    str(smp_b), str(smp_a)
                ]
                w.writerow(row)
                f.flush()

    print(f"[ok] CSV salvo em: {out_csv}")

if __name__ == "__main__":
    main()
