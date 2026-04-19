"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0
"""

# l2i/synth.py
# Síntese do IR a partir de uma CanonicalSpec + perfil de capacidades.
# Política e capacidade já foram tratadas; aqui só traduzimos requisitos em ações IR.

from typing import Any, Dict
import datetime

from .models import CanonicalSpec, IRAction, IRPlan, mk_ir_plan


def _now_plan_id(prefix: str) -> str:
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{ts}"


def synthesize_ir(spec: CanonicalSpec, profile: Dict[str, Any]) -> IRPlan:
    """
    Gera um IRPlan mínimo e coerente com o executor legacy e com emissores NETCONF/P4-like.

    Regras v0:
      - bandwidth.min_mbps    -> ReserveMinRate(min_mbps)
      - bandwidth.max_mbps    -> CapMaxRate(max_mbps)
      - priority.level        -> SetPriority(level)        (não obrigatório para o legacy, mas mantemos)
      - multicast.enabled     -> (ignorado no S1 unicast; S2 cuidará)
    """
    flow = spec.get("flow", {}) or {}
    flow_id = flow.get("id", "DefaultFlow")

    # plan_id com sufixo por domínio, se houver "scope"
    scope = spec.get("scope", "")
    scope_tag = (scope or "plan")
    plan_id = _now_plan_id(f"ir-{scope_tag}")

    ir = mk_ir_plan(flow_id=flow_id, plan_id=plan_id, atomic=bool(profile.get("atomic_commit", False)))

    req = spec.get("requirements", {}) or {}

    # Bandwidth → ações
    bw = req.get("bandwidth", {}) or {}
    if "min_mbps" in bw:
        try:
            min_mbps = float(bw["min_mbps"])
            ir["actions"].append(IRAction(type="ReserveMinRate", params={"min_mbps": min_mbps}))
        except Exception:
            pass
    if "max_mbps" in bw:
        try:
            max_mbps = float(bw["max_mbps"])
            ir["actions"].append(IRAction(type="CapMaxRate", params={"max_mbps": max_mbps}))
        except Exception:
            pass

    # Prioridade → ação (opcional)
    pr = req.get("priority", {}) or {}
    level = pr.get("level")
    if level in {"high", "medium", "low"}:
        ir["actions"].append(IRAction(type="SetPriority", params={"level": level}))

    # Latência não vira ação direta no v0; fica em constraints para auditoria
    lat = req.get("latency", {}) or {}
    if "max_ms" in lat and "percentile" in lat:
        ir["constraints"]["latency"] = {
            "max_ms": lat["max_ms"],
            "percentile": lat["percentile"]
        }

    # Nota: Multicast será tratado no S2 (árvore por fonte); aqui ignoramos se enabled=False
    mc = req.get("multicast", {}) or {}
    if mc.get("enabled", False):
        ir["constraints"]["multicast"] = {"enabled": True, "note": "Handled in S2"}

    return ir
