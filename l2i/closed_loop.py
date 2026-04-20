"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

# l2i/closed_loop.py
# Laço fechado L2I v0: MAD (política de ajustes) + AC5 (telemetria) + AC (re-síntese)
# Aplica passos cautelosos e monotônicos até satisfazer SLOs ou atingir limites.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Callable, Optional, List, Tuple
import copy
import json
import time

from .validator import validate_spec
from .policies import apply_policies
from .capabilities import ensure_capability_valid, check_capabilities
from .compose import compose_specs
from .synth import synthesize_ir
from .models import ErrorMsg, IRPlan

# -------------------------
# Políticas de Ajuste (MAD)
# -------------------------

@dataclass
class AdjustmentPolicy:
    # níveis permitidos de prioridade (ordem de reforço)
    priority_order: List[str] = field(default_factory=lambda: ["low","medium","high","critical"])
    max_priority: str = "high"           # teto organizacional (não sobe para 'critical' por padrão)
    # banda: teto permitido para aumento de max_mbps
    bandwidth_max_ceiling_mbps: float = 20.0
    # passos
    step_increase_max_mbps: float = 2.0  # se throughput < min, aumentar max_mbps em +2 Mbps
    step_relax_latency_ms: int = 5       # se P99 > bound, relaxar bound em +5ms (se permitido)
    # permissões
    allow_latency_relax: bool = True     # pode relaxar latência?
    max_latency_relax_total_ms: int = 10 # no total, não relaxar >10ms
    # iterações
    max_rounds: int = 5
    # regras de precedência de ajustes
    # 1) tentar elevar prioridade (até max_priority)
    # 2) aumentar max_mbps (até bandwidth_max_ceiling_mbps)
    # 3) relaxar latência (se permitido e dentro do teto)

@dataclass
class ClosedLoopLogEntry:
    round: int
    decision: str
    spec_snapshot: Dict[str, Any]
    plan_constraints: Dict[str, Any]
    metrics: Dict[str, Any]
    violations: List[Dict[str, Any]]

@dataclass
class ClosedLoopResult:
    converged: bool
    rounds: int
    history: List[ClosedLoopLogEntry]

# -------------------------
# Helpers
# -------------------------

def _priority_index(order: List[str], level: str) -> int:
    try:
        return order.index(level)
    except ValueError:
        return -1

def _cap_priority_up(spec_req: Dict[str, Any], pol: AdjustmentPolicy) -> bool:
    """Sobe prioridade em 1 nível, respeitando o teto. Retorna True se alterou."""
    cur = (spec_req.get("priority") or {}).get("level")
    if not cur:
        # se não há prioridade, setar 'medium' (neutro) como primeiro passo
        spec_req["priority"] = {"level": "medium"}
        return True
    i = _priority_index(pol.priority_order, cur)
    j = min(_priority_index(pol.priority_order, pol.max_priority), len(pol.priority_order)-1)
    if i < 0 or i >= j:
        return False
    nxt = pol.priority_order[i+1]
    spec_req["priority"]["level"] = nxt
    return True

def _cap_increase_max(spec_req: Dict[str, Any], pol: AdjustmentPolicy) -> bool:
    """Aumenta max_mbps em step, até o teto de política; garante max>=min."""
    bw = spec_req.get("bandwidth") or {}
    minr = bw.get("min_mbps")
    maxr = bw.get("max_mbps")
    if maxr is None:
        # se não havia teto, cria um teto igual ao mínimo (começa “apertado”)
        if minr is None:
            return False
        maxr = float(minr)
    new_max = min(maxr + pol.step_increase_max_mbps, pol.bandwidth_max_ceiling_mbps)
    if new_max <= maxr + 1e-9:
        return False
    bw["max_mbps"] = new_max
    # manter coerência min<=max
    if minr is not None and bw["max_mbps"] < minr:
        bw["max_mbps"] = float(minr)
    spec_req["bandwidth"] = bw
    return True

def _cap_relax_latency(spec_req: Dict[str, Any], pol: AdjustmentPolicy, already_relaxed_ms: int) -> Tuple[bool,int]:
    """Relaxa bound de latência até limite total. Retorna (alterou?, total_relax_ms)."""
    if not pol.allow_latency_relax:
        return (False, already_relaxed_ms)
    lat = spec_req.get("latency")
    if not lat:
        return (False, already_relaxed_ms)
    cur = int(lat.get("max_ms", 0))
    # se já atingiu teto de relaxo, não muda
    if already_relaxed_ms >= pol.max_latency_relax_total_ms:
        return (False, already_relaxed_ms)
    step = min(pol.step_relax_latency_ms, pol.max_latency_relax_total_ms - already_relaxed_ms)
    lat["max_ms"] = cur + step
    spec_req["latency"] = lat
    return (True, already_relaxed_ms + step)

# -------------------------
# Pipeline E2E (igual aos anteriores)
# -------------------------

def _plan_from_spec_dict(spec_doc: Dict[str, Any], profile: Dict[str, Any]) -> Tuple[Optional[IRPlan], Optional[Dict[str, Any]]]:
    specC, errs = validate_spec(spec_doc)
    if errs: return None, {"errors":[e.__dict__ for e in errs]}
    st, specP, pol = apply_policies(specC)
    if st == "deny": return None, {"errors":[p.__dict__ for p in pol]}
    ensure_capability_valid(profile)
    st2, specCap, cap = check_capabilities(specP, profile)
    if st2 == "deny": return None, {"errors":[c.__dict__ for c in cap]}
    merged, confl = compose_specs([specCap])
    if confl: return None, {"errors":[confl.__dict__]}
    plan = synthesize_ir(merged, profile)
    return plan, None

# -------------------------
# Controlador de Laço Fechado
# -------------------------

class ClosedLoopController:
    """
    Orquestra o laço:
      1) planeja a partir da spec,
      2) aplica (callback apply_fn),
      3) mede (callback measure_fn),
      4) avalia SLO (callback evaluate_fn retorna dict {"violations":[...], "metrics":...}),
      5) decide ajustes (política MAD) e repete até convergência/limites.
    """
    def __init__(self,
                 profile: Dict[str, Any],
                 policy: AdjustmentPolicy,
                 apply_fn: Callable[[IRPlan], None],
                 measure_and_evaluate_fn: Callable[[IRPlan], Dict[str, Any]],
                 on_round_end: Optional[Callable[[ClosedLoopLogEntry], None]] = None):
        self.profile = profile
        self.policy = policy
        self.apply_fn = apply_fn
        self.measure_and_evaluate_fn = measure_and_evaluate_fn
        self.on_round_end = on_round_end
        self._latency_relaxed_ms_total = 0

    def run_until_converge(self, spec_doc: Dict[str, Any]) -> ClosedLoopResult:
        hist: List[ClosedLoopLogEntry] = []
        cur_spec = copy.deepcopy(spec_doc)

        for rnd in range(1, self.policy.max_rounds + 1):
            # 1) Planeja
            plan, err = _plan_from_spec_dict(cur_spec, self.profile)
            if not plan:
                entry = ClosedLoopLogEntry(
                    round=rnd, decision="plan-error",
                    spec_snapshot=copy.deepcopy(cur_spec),
                    plan_constraints={}, metrics={}, violations=err.get("errors", [])
                )
                if self.on_round_end: self.on_round_end(entry)
                hist.append(entry)
                break

            # 2) Aplica
            self.apply_fn(plan)

            # 3) Mede + avalia
            rep = self.measure_and_evaluate_fn(plan)
            violations = rep.get("violations", [])
            metrics = rep.get("metrics", {})
            entry = ClosedLoopLogEntry(
                round=rnd, decision="evaluate",
                spec_snapshot=copy.deepcopy(cur_spec),
                plan_constraints=copy.deepcopy(plan.constraints),
                metrics=metrics, violations=violations
            )
            if self.on_round_end: self.on_round_end(entry)
            hist.append(entry)

            # 4) Convergência?
            if not violations:
                return ClosedLoopResult(converged=True, rounds=rnd, history=hist)

            # 5) Ajustes (ordem fixa: prioridade → banda → latência)
            changed = False
            req = cur_spec.setdefault("requirements", {})

            # (a) tentar subir prioridade
            changed = _cap_priority_up(req, self.policy)
            if changed:
                entry2 = ClosedLoopLogEntry(
                    round=rnd, decision="adjust:priority",
                    spec_snapshot=copy.deepcopy(cur_spec),
                    plan_constraints=copy.deepcopy(plan.constraints),
                    metrics=metrics, violations=violations
                )
                if self.on_round_end: self.on_round_end(entry2)
                hist.append(entry2)
                continue  # replaneja no próximo ciclo

            # (b) tentar aumentar max_mbps
            changed = _cap_increase_max(req, self.policy)
            if changed:
                entry2 = ClosedLoopLogEntry(
                    round=rnd, decision="adjust:max_mbps",
                    spec_snapshot=copy.deepcopy(cur_spec),
                    plan_constraints=copy.deepcopy(plan.constraints),
                    metrics=metrics, violations=violations
                )
                if self.on_round_end: self.on_round_end(entry2)
                hist.append(entry2)
                continue

            # (c) tentar relaxar latência (limitado)
            changed, self._latency_relaxed_ms_total = _cap_relax_latency(req, self.policy, self._latency_relaxed_ms_total)
            if changed:
                entry2 = ClosedLoopLogEntry(
                    round=rnd, decision="adjust:latency_relax",
                    spec_snapshot=copy.deepcopy(cur_spec),
                    plan_constraints=copy.deepcopy(plan.constraints),
                    metrics=metrics, violations=violations
                )
                if self.on_round_end: self.on_round_end(entry2)
                hist.append(entry2)
                continue

            # (d) nada mais a fazer (atingiu limites)
            entry3 = ClosedLoopLogEntry(
                round=rnd, decision="halt:no-more-adjustments",
                spec_snapshot=copy.deepcopy(cur_spec),
                plan_constraints=copy.deepcopy(plan.constraints),
                metrics=metrics, violations=violations
            )
            if self.on_round_end: self.on_round_end(entry3)
            hist.append(entry3)
            return ClosedLoopResult(converged=False, rounds=rnd, history=hist)

        # excedeu max_rounds
        return ClosedLoopResult(converged=False, rounds=self.policy.max_rounds, history=hist)
