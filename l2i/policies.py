"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

# l2i/policies.py
# Políticas organizacionais v0 para L2i.
# - NÃO usa isinstance(..., CanonicalSpec); trata spec como dict comum.
# - Regras:
#    * priority=critical: permitido somente a tenants privilegiados
#    * priority in {high, medium, low}: permitido a todos
#    * latency.max_ms: se < 5ms, eleva para 5ms (limite organizacional)
#    * bandwidth: se min_mbps existir e max_mbps não, define max = 2 * min
"""

from typing import Tuple, Dict, Any

def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}

def apply_policies(specC: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Aplica políticas organizacionais sobre a especificação já validada.
    Retorna:
      - status: "allow" | "deny"
      - specP:  spec pós-política (com possíveis ajustes conservadores)
      - polinfo: metadados (ajustes e/ou razão do deny)
    """
    # cópia rasa para não mutar o argumento
    specP: Dict[str, Any] = dict(specC) if isinstance(specC, dict) else {}
    polinfo: Dict[str, Any] = {"adjustments": []}

    # Tenant e privilégios (exemplo)
    tenant = specP.get("tenant", "")
    privileged = {"org.premium", "org.admin"}  # ajuste conforme a sua realidade

    # Requirements
    req = _as_dict(specP.get("requirements"))

    # PRIORIDADE
    pr = _as_dict(req.get("priority"))
    level = pr.get("level")
    if level == "critical" and tenant not in privileged:
        return (
            "deny",
            specP,
            {
                "reason": "priority_critical_requires_privileged_tenant",
                "tenant": tenant,
                "policy": "org.priority.critical.gate"
            },
        )
    # 'high'/'medium'/'low' são permitidos para todos (sem alteração)

    # LATÊNCIA
    lat = _as_dict(req.get("latency"))
    if lat:
        mx = lat.get("max_ms")
        try:
            mxv = float(mx) if mx is not None else None
        except Exception:
            mxv = None
        if mxv is not None and mxv < 5.0:
            lat["max_ms"] = 5.0
            polinfo["adjustments"].append({"latency.max_ms": "bumped_to_5ms"})
        pct = lat.get("percentile")
        if pct not in {"P50", "P95", "P99"}:
            lat["percentile"] = "P99"
            polinfo["adjustments"].append({"latency.percentile": "set_P99"})
        req["latency"] = lat  # reatribui (caso lat tenha sido criado por _as_dict)

    # LARGURA DE BANDA
    bw = _as_dict(req.get("bandwidth"))
    if bw:
        mn = bw.get("min_mbps")
        mx = bw.get("max_mbps")
        try:
            mnv = float(mn) if mn is not None else None
        except Exception:
            mnv = None
        try:
            mxv = float(mx) if mx is not None else None
        except Exception:
            mxv = None

        if mnv is not None and mxv is None:
            bw["max_mbps"] = float(mnv) * 2.0
            polinfo["adjustments"].append({"bandwidth.max_mbps": "set_2x_min"})

        req["bandwidth"] = bw  # reatribui

    # Propaga de volta
    specP["requirements"] = req

    return "allow", specP, polinfo
