"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0
"""

"""
Arquivo: l2i/compose.py
Descrição: (MAD) operador (meet) para compor múltiplas intenções do mesmo fluxo e detectar conflitos
Autor: Cleber Araújo (antoniocleber@ifba.edu.br)
Criação: 01/09/2025
Versão: 0.0.0
"""

from typing import Dict, List, Optional, Tuple
from .models import CanonicalSpec, ErrorMsg

# Requisitos de latência
def _meet_latency(a: Optional[Dict], b: Optional[Dict]) -> Optional[Dict]:
    """
    Concilia dois requisitos de latência
    Escolhe o menor 'max_ms' e o percentil mais exigente
    Retorna None se encontrar conflito
    """
    if not a: return b
    if not b: return a
    porder = {"P50":0,"P95":1,"P99":2}
    return {"max_ms": min(a["max_ms"], b["max_ms"]),
            "percentile": max(a.get("percentile","P99"), b.get("percentile","P99"),
                              key=lambda x: porder[x])}

# Requisitos de largura de banda
def _meet_bandwidth(a: Optional[Dict], b: Optional[Dict]) -> Optional[Dict]:
    """
    Concilia dois requisitos de banda
    Usa o maior min_mbps e o menor max_mbps disponível
    Retorna None em caso de min > max (conflito)
    """
    if not a: return b
    if not b: return a
    minmbps = max(a.get("min_mbps",0), b.get("min_mbps",0))
    maxs = [x for x in [a.get("max_mbps"), b.get("max_mbps")] if x is not None]
    maxmbps = min(maxs) if maxs else None
    burst = min([x for x in [a.get("burst_mbps"), b.get("burst_mbps")] if x is not None], default=None)
    if maxmbps is not None and minmbps > maxmbps:
        return None  # conflito
    out = {"min_mbps": minmbps}
    if maxmbps is not None: out["max_mbps"] = maxmbps
    if burst is not None: out["burst_mbps"] = burst
    return out

# Requisitos de prioridades
def _meet_priority(a: Optional[Dict], b: Optional[Dict]) -> Optional[Dict]:
    """
    Concilia prioridades escolhendo a mais alta
    """
    if not a: return b
    if not b: return a
    order = {"low":0,"medium":1,"high":2,"critical":3}
    return {"level": max([a["level"], b["level"]], key=lambda x: order[x])}

# Requisitos de tráfegos multicast
def _meet_multicast(a: Optional[Dict], b: Optional[Dict]) -> Optional[Dict]:
    """
    Concilia requisitos multicast
    Só é compatível se ambos estiverem habilitados e tiverem o mesmo group_id
    """
    if not a: return b
    if not b: return a
    ea, eb = a.get("enabled",False), b.get("enabled",False)
    if not ea and not eb: return {"enabled": False}
    if ea and eb and a.get("group_id")==b.get("group_id"):
        return {"enabled": True, "group_id": a["group_id"]}
    return None  # conflito

# Função principal de composição
def compose_specs(specs: List[CanonicalSpec]) -> Tuple[Optional[CanonicalSpec], Optional[ErrorMsg]]:
    """
    Tenta compor várias CanonicalSpec em uma só (união de requisitos)
    Retorna (spec_composta, erro) sendo erro None em caso de sucesso
    """
    assert specs, "compose_specs: empty set"
    base = specs[0]
    L, B, P, M = base.latency, base.bandwidth, base.priority, base.multicast
    for s in specs[1:]:
        L = _meet_latency(L, s.latency)
        if L is None: return None, ErrorMsg("E-COMP-LAT","latency conflict")
        B = _meet_bandwidth(B, s.bandwidth)
        if B is None: return None, ErrorMsg("E-COMP-BW","bandwidth conflict")
        P = _meet_priority(P, s.priority)
        M = _meet_multicast(M, s.multicast)
        if M is None: return None, ErrorMsg("E-COMP-MC","multicast conflict (group mismatch)")
    return CanonicalSpec(
        tenant=base.tenant, scope=base.scope, flow_id=base.flow_id,
        latency=L, bandwidth=B, priority=P, multicast=M,
        target_profile=base.target_profile
    ), None
