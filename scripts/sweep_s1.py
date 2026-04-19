#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sweep_s1.py — grid runner for Scenario S1 (multidomain unicast QoS).

Goals (per your requirements):
- Unified CLI with S2: --backends mock real, --modes baseline adapt, --dump-jsonl.
- Clear per-run feedback: [CMD], retries, and warnings.
- Robust CSV (no NaN/"nan"; avoid None by using False/"" as appropriate).
- Always run scenarios with the SAME Python interpreter used to execute this sweep
  (critical for venv + p4runtime protobuf deps).

Outputs:
- results/compare/SWEEP_S1_<ts>.csv
- optional results/compare/SWEEP_S1_<ts>_runs.jsonl (raw execution metadata)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ------------------------- helpers -------------------------

def _utc_ts() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def _bool(v: Any) -> bool:
    return bool(v) if v is not None else False

def _safe_num(v: Any) -> str:
    # Avoid NaN/None in CSV: return "" when unknown.
    if v is None:
        return ""
    try:
        if isinstance(v, float) and (v != v):  # NaN
            return ""
        return str(v)
    except Exception:
        return ""

def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def _pick(d: Dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur

def _run_cmd(cmd: List[str], timeout_s: int = 900) -> Tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_s,
    )
    return p.returncode, p.stdout, p.stderr


@dataclass
class RunResult:
    ok: bool
    mode: str
    backend: str
    bwA: float
    bwB: float
    bwC: float
    delay_ms: float
    be_mbps: float
    # summary fields
    duration_ms: str = ""
    t_wall_start: str = ""
    t_wall_end: str = ""
    control_plane_ms_total: str = ""
    # apply flags (always boolean in CSV)
    apply_A: bool = False
    apply_B: bool = False
    apply_C: bool = False
    # core metrics
    rtt_p50_ms: str = ""
    rtt_p95_ms: str = ""
    rtt_p99_ms: str = ""
    throughput_mbps: str = ""
    delivery_ratio: str = ""
    # provenance
    run_id: str = ""
    summary_path: str = ""
    attempts: int = 1


def build_args() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="L2i spec JSON (e.g., specs/valid/s1_unicast_qos.json)")
    ap.add_argument("--duration", type=float, default=30, help="Scenario duration (s)")
    ap.add_argument("--bwA", type=float, default=100, help="Domain A (tc/htb) link bw (Mbps)")
    ap.add_argument("--bwB", type=float, nargs="+", required=True, help="Domain B (NETCONF) bw grid (Mbps)")
    ap.add_argument("--bwC", type=float, default=100, help="Domain C (P4) link bw (Mbps)")
    ap.add_argument("--delay-ms", type=float, default=1, help="Link delay (ms)")
    ap.add_argument("--be", type=float, nargs="+", required=True, dest="be_grid", help="Best-effort load grid (Mbps)")
    ap.add_argument("--backends", nargs="+", choices=["mock", "real"], default=["mock", "real"],
                    help="Backends to run (space-separated): mock real")
    ap.add_argument("--modes", nargs="+", choices=["baseline", "adapt"], default=["baseline", "adapt"],
                    help="Modes to run (space-separated): baseline adapt")
    ap.add_argument("--retries", type=int, default=2, help="Retries per point upon non-zero exit")
    ap.add_argument("--python-bin", default=sys.executable,
                    help="Python executable to run the scenarios (default: current interpreter)")
    ap.add_argument("--dump-jsonl", action="store_true", help="Also write JSONL with per-run metadata")
    return ap


def run_one(
    *,
    python_bin: str,
    spec: str,
    duration: float,
    bwA: float,
    bwB: float,
    bwC: float,
    delay_ms: float,
    be_mbps: float,
    mode: str,
    backend: str,
    retries: int,
    jsonl_out: Optional[Path] = None,
) -> RunResult:
    cmd = [
        python_bin, "-m", "scenarios.multidomain_s1",
        "--spec", spec,
        "--duration", str(duration),
        "--bwA", str(bwA),
        "--bwB", str(bwB),
        "--bwC", str(bwC),
        "--delay-ms", str(delay_ms),
        "--be-mbps", str(be_mbps),
        "--mode", mode,
        "--backend", backend,
    ]

    attempt = 0
    last_rc, last_out, last_err = 1, "", ""
    while True:
        attempt += 1
        print(f"[CMD] {' '.join(cmd)}")
        rc, out, err = _run_cmd(cmd)
        last_rc, last_out, last_err = rc, out, err

        if jsonl_out is not None:
            rec = {
                "ts_utc": _utc_ts(),
                "scenario": "S1",
                "mode": mode,
                "backend": backend,
                "bwA": bwA, "bwB": bwB, "bwC": bwC,
                "delay_ms": delay_ms, "be_mbps": be_mbps,
                "attempt": attempt,
                "cmd": cmd,
                "rc": rc,
                "stdout_tail": out[-2000:],
                "stderr_tail": err[-2000:],
            }
            with jsonl_out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        if rc == 0:
            break
        if attempt > retries:
            print(f"[warn] Falha em backend={backend}, bwB={bwB}, be={be_mbps}, mode={mode}: Comando falhou (rc={rc}).")
            print("STDOUT:\n" + out[-2000:])
            print("\nSTDERR:\n" + err[-2000:])
            break
        print(f"[retry] tentativa {attempt}/{retries} falhou (rc={rc}); tentando novamente...")

    rr = RunResult(
        ok=(last_rc == 0),
        mode=mode, backend=backend,
        bwA=bwA, bwB=bwB, bwC=bwC,
        delay_ms=delay_ms, be_mbps=be_mbps,
        attempts=attempt,
    )

    # Parse summary if present (scenario writes it on success)
    # We expect output in results/S1/S1_<ts>.json, but rely on stdout hint if available.
    # Fallback: pick newest matching file.
    summary_path: Optional[Path] = None
    if last_rc == 0:
        # Heuristic: find newest summary in results/S1 with prefix "S1_"
        res_dir = Path("results") / "S1"
        if res_dir.exists():
            cands = sorted(res_dir.glob("S1_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if cands:
                summary_path = cands[0]
    if summary_path and summary_path.exists():
        s = _read_json(summary_path)
        rr.summary_path = str(summary_path)
        rr.run_id = str(s.get("run_id") or s.get("run") or "")
        rr.duration_ms = _safe_num(s.get("duration_ms"))
        rr.t_wall_start = str(s.get("t_wall_start") or "")
        rr.t_wall_end = str(s.get("t_wall_end") or "")
        rr.control_plane_ms_total = _safe_num(s.get("control_plane_ms_total"))

        ba = s.get("backend_apply", {}) if isinstance(s.get("backend_apply"), dict) else {}
        rr.apply_A = _bool(ba.get("apply_A"))
        rr.apply_B = _bool(ba.get("apply_B"))
        rr.apply_C = _bool(ba.get("apply_C"))

        m = s.get("metrics", {}) if isinstance(s.get("metrics"), dict) else {}
        rtt = m.get("rtt_ms", {}) if isinstance(m.get("rtt_ms"), dict) else {}
        rr.rtt_p50_ms = _safe_num(rtt.get("p50"))
        rr.rtt_p95_ms = _safe_num(rtt.get("p95"))
        rr.rtt_p99_ms = _safe_num(rtt.get("p99"))
        rr.throughput_mbps = _safe_num(m.get("throughput_mbps"))
        rr.delivery_ratio = _safe_num(m.get("delivery_ratio"))
    return rr


def main() -> None:
    args = build_args().parse_args()

    out_dir = Path("results") / "compare"
    _ensure_dir(out_dir)

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    csv_path = out_dir / f"SWEEP_S1_{ts}.csv"
    jsonl_path = out_dir / f"SWEEP_S1_{ts}_runs.jsonl" if args.dump_jsonl else None
    if jsonl_path:
        if jsonl_path.exists():
            jsonl_path.unlink()

    rows: List[Dict[str, Any]] = []

    for backend in args.backends:
        for mode in args.modes:
            for bwB in args.bwB:
                for be in args.be_grid:
                    rr = run_one(
                        python_bin=args.python_bin,
                        spec=args.spec,
                        duration=args.duration,
                        bwA=args.bwA,
                        bwB=bwB,
                        bwC=args.bwC,
                        delay_ms=args.delay_ms,
                        be_mbps=be,
                        mode=mode,
                        backend=backend,
                        retries=args.retries,
                        jsonl_out=jsonl_path,
                    )

                    # In baseline, backends are logically irrelevant; normalize apply flags to False.
                    if mode == "baseline":
                        rr.apply_A = False
                        rr.apply_B = False
                        rr.apply_C = False

                    row = {
                        "scenario": "S1",
                        "backend": backend,
                        "mode": mode,
                        "bwA": rr.bwA,
                        "bwB": rr.bwB,
                        "bwC": rr.bwC,
                        "delay_ms": rr.delay_ms,
                        "be_mbps": rr.be_mbps,
                        "ok": rr.ok,
                        "attempts": rr.attempts,
                        "run_id": rr.run_id,
                        "summary_path": rr.summary_path,
                        "t_wall_start": rr.t_wall_start,
                        "t_wall_end": rr.t_wall_end,
                        "duration_ms": rr.duration_ms,
                        "control_plane_ms_total": rr.control_plane_ms_total,
                        "apply_A": rr.apply_A,
                        "apply_B": rr.apply_B,
                        "apply_C": rr.apply_C,
                        "rtt_p50_ms": rr.rtt_p50_ms,
                        "rtt_p95_ms": rr.rtt_p95_ms,
                        "rtt_p99_ms": rr.rtt_p99_ms,
                        "throughput_mbps": rr.throughput_mbps,
                        "delivery_ratio": rr.delivery_ratio,
                    }
                    rows.append(row)

    # Write CSV
    fieldnames = list(rows[0].keys()) if rows else []
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            # sanitize to avoid "None"/"nan"
            clean = {}
            for k, v in r.items():
                if isinstance(v, bool):
                    clean[k] = "True" if v else "False"
                elif v is None:
                    clean[k] = ""
                else:
                    clean[k] = str(v)
            w.writerow(clean)

    print(f"[ok] CSV salvo em: {csv_path}")
    if jsonl_path:
        print(f"[ok] JSONL (execuções) salvo em: {jsonl_path}")


if __name__ == "__main__":
    main()
