"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

# l2i/validator.py
# CED/validador v0: validação leve e normalização da intenção.
# Retorna (specC, errs), onde errs é uma lista de ErrorMsg (vazia => válido).
"""

from typing import Any, Dict, List, Tuple, Optional
from .models import ErrorMsg  # CanonicalSpec é um alias de tipo; NÃO deve ser chamado

# ---------------------------
# Helpers
# ---------------------------

def _err(code: str, field: Optional[str] = None, detail: Any = None) -> ErrorMsg:
    e: ErrorMsg = {"code": code}
    if field is not None:
        e["field"] = field
    if detail is not None:
        e["detail"] = detail
    return e

def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}

def _as_str(x: Any, default: str = "") -> str:
    return x if isinstance(x, str) else default

def _as_bool(x: Any, default: bool = False) -> bool:
    if isinstance(x, bool):
        return x
    return default

def _as_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

# ---------------------------
# Normalizador principal
# ---------------------------

def _normalize(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Cria um dicionário normalizado (sem lançar exceção).
    Usa defaults conservadores e mantém campos desconhecidos em metadata.
    """
    out: Dict[str, Any] = {}

    # Versão / tenant / scope / metadata
    out["l2i_version"] = _as_str(doc.get("l2i_version"), "0.1")
    out["tenant"] = _as_str(doc.get("tenant"), "")
    if "scope" in doc:
        out["scope"] = _as_str(doc.get("scope"))

    # metadata (mantemos campos desconhecidos aqui se quiser)
    out["metadata"] = _as_dict(doc.get("metadata"))

    # Flow
    flow = _as_dict(doc.get("flow"))
    flow_id = _as_str(flow.get("id"), "DefaultFlow")
    out["flow"] = {"id": flow_id}

    # Requirements
    req_in = _as_dict(doc.get("requirements"))
    req_out: Dict[str, Any] = {}

    # Latency
    lat_in = _as_dict(req_in.get("latency"))
    if lat_in:
        lat_norm: Dict[str, Any] = {}
        max_ms = _as_float(lat_in.get("max_ms"))
        if max_ms is not None:
            lat_norm["max_ms"] = max_ms
        pct = _as_str(lat_in.get("percentile"))
        if pct:
            lat_norm["percentile"] = pct
        if lat_norm:
            req_out["latency"] = lat_norm

    # Bandwidth
    bw_in = _as_dict(req_in.get("bandwidth"))
    if bw_in:
        bw_norm: Dict[str, Any] = {}
        mn = _as_float(bw_in.get("min_mbps"))
        mx = _as_float(bw_in.get("max_mbps"))
        if mn is not None:
            bw_norm["min_mbps"] = mn
        if mx is not None:
            bw_norm["max_mbps"] = mx
        if bw_norm:
            req_out["bandwidth"] = bw_norm

    # Priority
    pr_in = _as_dict(req_in.get("priority"))
    if pr_in:
        level = _as_str(pr_in.get("level"))
        if level:
            req_out["priority"] = {"level": level}

    # Multicast
    mc_in = _as_dict(req_in.get("multicast"))
    if mc_in:
        mc_norm: Dict[str, Any] = {}
        mc_norm["enabled"] = _as_bool(mc_in.get("enabled"), False)
        # Campos opcionais que poderemos usar no S2
        if "group_id" in mc_in:
            mc_norm["group_id"] = _as_str(mc_in.get("group_id"))
        if "replicas" in mc_in and isinstance(mc_in["replicas"], list):
            # replicas: lista de dicionários {"dst": "...", "port": "..."} – mantemos como está
            mc_norm["replicas"] = mc_in["replicas"]
        if mc_norm:
            req_out["multicast"] = mc_norm

    out["requirements"] = req_out

    return out

# ---------------------------
# Validações semânticas v0
# ---------------------------

def _semantic_checks(spec: Dict[str, Any]) -> List[ErrorMsg]:
    """
    Checagens básicas do v0 (não substitui um schema formal, mas cobre o necessário).
    """
    errs: List[ErrorMsg] = []

    # flow.id presente
    if not _as_str(spec.get("flow", {}).get("id")):
        errs.append(_err("E-FLOW-ID-MISSING", "flow.id"))

    req = spec.get("requirements", {})

    # latency
    lat = req.get("latency")
    if lat:
        if "max_ms" in lat:
            mx = _as_float(lat.get("max_ms"))
            if mx is None or mx <= 0:
                errs.append(_err("E-LATENCY-MAX-INVALID", "requirements.latency.max_ms", lat.get("max_ms")))
        if "percentile" in lat:
            pct = _as_str(lat.get("percentile")).upper()
            if pct not in {"P50", "P95", "P99"}:
                errs.append(_err("E-LATENCY-PCT-INVALID", "requirements.latency.percentile", lat.get("percentile")))

    # bandwidth
    bw = req.get("bandwidth")
    if bw:
        mn = _as_float(bw.get("min_mbps")) if "min_mbps" in bw else None
        mx = _as_float(bw.get("max_mbps")) if "max_mbps" in bw else None
        if mn is not None and mn <= 0:
            errs.append(_err("E-BW-MIN-INVALID", "requirements.bandwidth.min_mbps", bw.get("min_mbps")))
        if mx is not None and mx <= 0:
            errs.append(_err("E-BW-MAX-INVALID", "requirements.bandwidth.max_mbps", bw.get("max_mbps")))
        if mn is not None and mx is not None and mn > mx:
            errs.append(_err("E-BW-MIN-GT-MAX", "requirements.bandwidth", {"min_mbps": mn, "max_mbps": mx}))

    # priority
    pr = req.get("priority")
    if pr:
        level = _as_str(pr.get("level")).lower()
        if level and level not in {"high", "medium", "low", "critical"}:
            errs.append(_err("E-PRIORITY-LEVEL-INVALID", "requirements.priority.level", pr.get("level")))

    # multicast (checa apenas o domínio de enabled)
    mc = req.get("multicast")
    if mc and not isinstance(mc.get("enabled"), bool):
        errs.append(_err("E-MCAST-ENABLED-NOT-BOOL", "requirements.multicast.enabled", mc.get("enabled")))

    return errs

# ---------------------------
# API pública
# ---------------------------

def validate_spec(doc: Dict[str, Any]) -> Tuple[Dict[str, Any], List[ErrorMsg]]:
    """
    Valida e normaliza a intenção.
    - Se doc não é dict, retorna erro de tipo.
    - Retorna (spec_normalizada, lista_erros). Lista vazia => válido.
    """
    if not isinstance(doc, dict):
        return {}, [_err("E-DOC-NOT-OBJECT", detail=type(doc).__name__)]

    spec = _normalize(doc)
    errs = _semantic_checks(spec)
    return spec, errs
