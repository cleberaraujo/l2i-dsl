"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

Apply plan instrumentation helpers.

Goal: record *what* each backend intends to change (plan) and *how long* it took to apply,
without coupling this logic to specific scenarios.

This module is intentionally lightweight:
- It does not execute any network commands.
- It only summarizes already-available `planned` / `commands` structures returned by backends
  (or built by scenarios for Domain-A tc/htb).

The JSON produced by these helpers is meant to be embedded into the existing per-run summary
files (e.g., S1_<ts>.json, S2_<ts>.json) under keys such as:
- apply_plan
- apply_spans
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


def _now_ns() -> int:
    # monotonic for durations (not wall-clock)
    return time.monotonic_ns()


def start_span() -> int:
    return _now_ns()


def end_span(t0_ns: int) -> Dict[str, Any]:
    t1_ns = _now_ns()
    dt_ms = (t1_ns - t0_ns) / 1_000_000.0
    return {
        "t_start_ns": int(t0_ns),
        "t_end_ns": int(t1_ns),
        "duration_ms": float(dt_ms),
    }


def summarize_plan(planned: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Produce a compact, backend-agnostic summary of the plan."""
    if not planned:
        return {"kind": "none"}

    # Heuristics based on common keys used in our backends.
    if "cmds" in planned and isinstance(planned["cmds"], list):
        cmds = [str(c) for c in planned["cmds"]]
        tc_cmds = [c for c in cmds if c.strip().startswith("tc ")]
        return {
            "kind": "linux-tc",
            "cmd_count": len(cmds),
            "tc_cmd_count": len(tc_cmds),
            "has_qdisc": any("qdisc" in c for c in tc_cmds),
            "has_class": any(" class " in c or "classid" in c for c in tc_cmds),
            "has_filter": any(" filter " in c for c in tc_cmds),
        }

    if planned.get("kind") == "netconf" or "edit_config" in planned or "rpc" in planned:
        # We avoid storing full XML/YANG payload here (already available elsewhere if needed).
        return {
            "kind": "netconf",
            "rpc": planned.get("rpc", "edit-config"),
            "edit_config_count": int(planned.get("edit_config_count", 1)),
            "patch_count": int(planned.get("patch_count", planned.get("edit_config_count", 1))),
            "payload_bytes": int(planned.get("payload_bytes", 0)),
            "target": planned.get("target"),
        }

    if planned.get("kind") == "p4" or "entries_to_write" in planned:
        entries = planned.get("entries_to_write", [])
        return {
            "kind": "p4",
            "pipeline": planned.get("pipeline"),
            "entry_count": int(len(entries) if isinstance(entries, list) else planned.get("entry_count", 0)),
            "table_count": int(planned.get("table_count", 0)),
        }

    # Fallback: do not explode the summary; keep only top-level keys.
    return {
        "kind": "generic",
        "keys": sorted(list(planned.keys()))[:32],
    }


def make_span(
    *,
    domain: str,
    backend: str,
    span: Dict[str, Any],
    planned: Optional[Dict[str, Any]] = None,
    executed: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "domain": domain,
        "backend": backend,
        **span,
        "planned_summary": summarize_plan(planned),
    }
    if planned is not None:
        out["planned"] = planned
    if executed is not None:
        out["executed"] = executed
    return out


def merge_apply_spans(*spans: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for s in spans:
        merged.extend(s)
    return merged
