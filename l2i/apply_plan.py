"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0
"""

# -*- coding: utf-8 -*-
"""Apply Plan (execution instrumentation)

This module provides a small, dependency-free recorder to capture:
- start/end timestamps (monotonic + wall clock ISO8601) for each "apply" phase
- a lightweight description of the intended changes (plan) per domain
- optional backend-specific artifacts (e.g., NETCONF edit-config XML, P4 entries)

Design goals:
- opt-in and low risk: if unused, it has zero effect.
- stable JSON: can be embedded in run summaries and later post-processed.
- no coupling to scenarios: scenarios call the recorder; the recorder does not import scenarios.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time
import datetime


def _utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class ApplyPhase:
    domain: str
    name: str
    t_wall_start: str
    t_wall_end: Optional[str] = None
    t_mono_start_s: float = 0.0
    t_mono_end_s: Optional[float] = None
    duration_ms: Optional[float] = None
    plan: Dict[str, Any] = field(default_factory=dict)
    notes: Optional[str] = None
    ok: Optional[bool] = None
    error: Optional[str] = None

    def end(self, ok: bool = True, error: Optional[str] = None) -> None:
        self.t_wall_end = _utc_iso()
        self.t_mono_end_s = time.monotonic()
        self.duration_ms = (self.t_mono_end_s - self.t_mono_start_s) * 1000.0
        self.ok = ok
        self.error = error


class ApplyPlan:
    """Recorder for apply phases across domains."""

    def __init__(self, run_id: str, mode: str, backend: str, scenario: str) -> None:
        self.run_id = run_id
        self.mode = mode
        self.backend = backend
        self.scenario = scenario
        self.created_at = _utc_iso()
        self.phases: List[ApplyPhase] = []
        self.artifacts: Dict[str, Dict[str, Any]] = {}  # domain -> key -> value

    def attach_artifact(self, domain: str, key: str, value: Any) -> None:
        self.artifacts.setdefault(domain, {})[key] = value

    def add_note(self, domain: str, note: str) -> None:
        self.artifacts.setdefault(domain, {}).setdefault("notes", [])
        self.artifacts[domain]["notes"].append(note)

    def phase(self, domain: str, name: str, plan: Optional[Dict[str, Any]] = None, notes: Optional[str] = None):
        ap = self

        class _Ctx:
            def __enter__(self_inner):
                p = ApplyPhase(
                    domain=domain,
                    name=name,
                    t_wall_start=_utc_iso(),
                    t_mono_start_s=time.monotonic(),
                    plan=plan or {},
                    notes=notes,
                )
                ap.phases.append(p)
                return p

            def __exit__(self_inner, exc_type, exc, tb):
                p = ap.phases[-1]
                if exc is None:
                    p.end(ok=True)
                    return False
                p.end(ok=False, error=str(exc))
                return False  # do not swallow exception

        return _Ctx()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario": self.scenario,
            "mode": self.mode,
            "backend": self.backend,
            "created_at": self.created_at,
            "phases": [
                {
                    "domain": p.domain,
                    "name": p.name,
                    "t_wall_start": p.t_wall_start,
                    "t_wall_end": p.t_wall_end,
                    "duration_ms": p.duration_ms,
                    "plan": p.plan,
                    "notes": p.notes,
                    "ok": p.ok,
                    "error": p.error,
                }
                for p in self.phases
            ],
            "artifacts": self.artifacts,
        }
