#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sweep_s2.py — grid runner for Scenario S2 (multicast source-oriented).

See sweep_s1.py for design goals and conventions.
Key S2 detail: domain A may appear as (A_env, A_overlay) in backend_apply.
We normalize apply_A = apply_A_env OR apply_A_overlay OR apply_A.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _utc_ts() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def _bool(v: Any) -> bool:
    return bool(v) if v is not None else False

def _safe_num(v: Any) -> str:
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
    # apply flags
    apply_A: bool = False
    apply_B: bool = False
    apply_C: bool = False
    # metrics (global + per-receiver)
    rtt_p50_ms: str = ""
    rtt_p95_ms: str = ""
    rtt_p99_ms: str = ""
    rtt_p99_B_ms: str = ""
    rtt_p99_C_ms: str = ""
    delivery_ratio: str = ""
    delivery_B: str = ""
    delivery_C: str = ""
    throughput_B_mbps: str = ""
    throughput_C_mbps: str = ""
    # provenance
    run_id: str = ""
    summary_path: str = ""
    attempts: int = 1


def build_args() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="L2i spec JSON (e.g., specs/valid/s2_multicast_source_oriented.json)")
    ap.add_argument("--duration", type=float, default=30, help="Scenario duration (s)")
    ap.add_argument("--bwA", type=float, default=100, help="Domain A link bw (Mbps)")
    ap.add_argument("--bwB", type=float, nargs="+", required=True, help="Domain B bw grid (Mbps)")
    ap.add_argument("--bwC", type=float, default=100, help="Domain C link bw (Mbps)")
    ap.add_argument("--delay-ms", type=float, default=1, help="Link delay (ms)")
    ap.add_argument("--be", type=float, nargs="+", required=True, dest="be_grid", help="Best-effort load grid (Mbps)")
    ap.add_argument("--backends", nargs="+", choices=["mock", "real"], default=["mock", "real"])
    ap.add_argument("--modes", nargs="+", choices=["baseline", "adapt"], default=["baseline", "adapt"])
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--python-bin", default=sys.executable)
    ap.add_argument("--dump-jsonl", action="store_true")
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
        python_bin, "-m", "scenarios.multicast_s2",
        "--spec", spec,
        "--duration", str(duration),
        "--bwA", str(bwA),
        "--bwC", str(bwC),
        "--delay-ms", str(delay_ms),
        "--bwB", str(bwB),
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
                "scenario": "S2",
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

    summary_path: Optional[Path] = None
    if last_rc == 0:
        res_dir = Path("results") / "S2"
        if res_dir.exists():
            cands = sorted(res_dir.glob("S2_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
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
        # normalize A (env/overlay)
        rr.apply_A = _bool(ba.get("apply_A")) or _bool(ba.get("apply_A_env")) or _bool(ba.get("apply_A_overlay"))
        rr.apply_B = _bool(ba.get("apply_B"))
        rr.apply_C = _bool(ba.get("apply_C"))

        m = s.get("metrics", {}) if isinstance(s.get("metrics"), dict) else {}
        rtt = m.get("rtt_ms", {}) if isinstance(m.get("rtt_ms"), dict) else {}
        rr.rtt_p50_ms = _safe_num(rtt.get("p50"))
        rr.rtt_p95_ms = _safe_num(rtt.get("p95"))
        rr.rtt_p99_ms = _safe_num(rtt.get("p99"))
        rr.rtt_p99_B_ms = _safe_num(m.get("rtt_p99_B_ms"))
        rr.rtt_p99_C_ms = _safe_num(m.get("rtt_p99_C_ms"))

        rr.delivery_ratio = _safe_num(m.get("delivery_ratio"))
        rr.delivery_B = _safe_num(m.get("delivery_ratio_B"))
        rr.delivery_C = _safe_num(m.get("delivery_ratio_C"))
        rr.throughput_B_mbps = _safe_num(m.get("throughput_B_mbps"))
        rr.throughput_C_mbps = _safe_num(m.get("throughput_C_mbps"))

    return rr


def main() -> None:
    args = build_args().parse_args()

    out_dir = Path("results") / "compare"
    _ensure_dir(out_dir)

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    csv_path = out_dir / f"SWEEP_S2_{ts}.csv"
    jsonl_path = out_dir / f"SWEEP_S2_{ts}_runs.jsonl" if args.dump_jsonl else None
    if jsonl_path and jsonl_path.exists():
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

                    if mode == "baseline":
                        rr.apply_A = False
                        rr.apply_B = False
                        rr.apply_C = False

                    row = {
                        "scenario": "S2",
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
                        "rtt_p99_B_ms": rr.rtt_p99_B_ms,
                        "rtt_p99_C_ms": rr.rtt_p99_C_ms,
                        "delivery_ratio": rr.delivery_ratio,
                        "delivery_ratio_B": rr.delivery_B,
                        "delivery_ratio_C": rr.delivery_C,
                        "throughput_B_mbps": rr.throughput_B_mbps,
                        "throughput_C_mbps": rr.throughput_C_mbps,
                    }
                    rows.append(row)

    fieldnames = list(rows[0].keys()) if rows else []
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
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
