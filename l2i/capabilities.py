"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

# l2i/capabilities.py
# Verificações de capacidade por domínio (MAD):
#  - ensure_capability_valid(profile): valida estrutura/consistência do profile
#  - check_capabilities(specP, profile): checa aderência da intenção às capacidades do profile
"""

from typing import Tuple, Dict, Any, List

def _err(reason: str, detail: Dict[str, Any] = None) -> Dict[str, Any]:
    d = {"reason": reason}
    if detail:
        d["detail"] = detail
    return d

def ensure_capability_valid(profile: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]]]:
    """
    Sanidade do perfil de capacidades (estrutura mínima e coerência de limites).
    NÃO lança exceção; retorna (ok, issues[]). O fed.py assume que isso não explode.
    """
    issues: List[Dict[str, Any]] = []
    req_top = ["profile_id", "queues", "meters", "multicast", "ports"]
    for k in req_top:
        if k not in profile:
            issues.append(_err("missing_key", {"key": k}))
    # queues
    q = profile.get("queues", {})
    if "max_queues" not in q:
        issues.append(_err("queues_missing_max_queues"))
    modes = q.get("modes", {})
    if not isinstance(modes, dict) or "strict" not in modes:
        issues.append(_err("queues_missing_modes_strict"))
    # meters
    m = profile.get("meters", {})
    if "supported" not in m:
        issues.append(_err("meters_missing_supported"))
    if m.get("supported"):
        for k in ["min_rate_mbps", "max_rate_mbps"]:
            if k not in m:
                issues.append(_err("meters_missing_limit", {"key": k}))
            else:
                try:
                    float(m[k])
                except Exception:
                    issues.append(_err("meters_limit_not_number", {"key": k, "val": m[k]}))
    # ports
    ports = profile.get("ports", [])
    if not isinstance(ports, list) or not ports:
        issues.append(_err("ports_missing_or_empty"))
    else:
        for p in ports:
            if "speed_mbps" not in p:
                issues.append(_err("port_missing_speed", {"port": p}))
    ok = len(issues) == 0
    return ok, issues

def _cap_port_speed(profile: Dict[str, Any]) -> float:
    # usa a menor velocidade dentre as portas declaradas (conservador)
    speeds = []
    for p in profile.get("ports", []):
        try:
            speeds.append(float(p.get("speed_mbps", 0)))
        except Exception:
            pass
    return min(speeds) if speeds else 0.0

def _meters_limits(profile: Dict[str, Any]) -> Tuple[bool, float, float]:
    m = profile.get("meters", {})
    if not m.get("supported", False):
        return False, 0.0, 0.0
    try:
        lo = float(m.get("min_rate_mbps", 0))
        hi = float(m.get("max_rate_mbps", 0))
        return True, lo, hi
    except Exception:
        return False, 0.0, 0.0

def check_capabilities(specP: Dict[str, Any], profile: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Checa se a intenção (após políticas) é suportada pelo profile.
    Retorna: (status, specCap, cap_info)
      - status: "allow" | "deny"
      - specCap: pode conter pequenos ajustes conservadores
      - cap_info: notas/ajustes ou motivo do deny
    Regras v0 (conservadoras, suficientes para S1):
      * Banda: se meters.supported=True, exige min/max dentro de [min_rate_mbps, max_rate_mbps] e ≤ speed de porta.
      * Prioridade: se queues.modes.strict=True, aceita levels {high,medium,low}; (critical é tratado em policies).
      * Multicast: se spec.multicast.enabled=True mas profile.multicast.mode=="none" => deny.
      * Latência: não há garantia determinística no profile; apenas aceitamos o campo (validação semântica é do CED/policies).
    """
    specCap = dict(specP)  # cópia rasa
    info: Dict[str, Any] = {"adjustments": [], "notes": []}

    # 1) Perfil válido?
    ok, issues = ensure_capability_valid(profile)
    if not ok:
        return "deny", specCap, {"issues": issues}

    # 2) Coletar capacidades relevantes
    port_lo = _cap_port_speed(profile)
    meters_ok, meter_min, meter_max = _meters_limits(profile)
    q_modes = profile.get("queues", {}).get("modes", {})
    has_strict = bool(q_modes.get("strict", False))
    wfq = q_modes.get("wfq", {})
    has_wfq = isinstance(wfq, dict) and wfq.get("supported", False)

    # 3) Extrair requisitos
    req = specCap.get("requirements", {})
    bw = req.get("bandwidth", {}) if isinstance(req.get("bandwidth", {}), dict) else {}
    pr = req.get("priority", {}) if isinstance(req.get("priority", {}), dict) else {}
    mc = req.get("multicast", {}) if isinstance(req.get("multicast", {}), dict) else {}

    # 4) Multicast vs profile
    mc_enabled = bool(mc.get("enabled", False))
    if mc_enabled:
        mc_mode = profile.get("multicast", {}).get("mode", "none")
        if mc_mode == "none":
            return "deny", specCap, _err("multicast_not_supported_in_domain", {"profile_id": profile.get("profile_id")})

    # 5) Prioridade vs profile
    level = pr.get("level")
    if level in {"high", "medium", "low"}:
        if not (has_strict or has_wfq):
            return "deny", specCap, _err("priority_requires_queues_support", {"profile_id": profile.get("profile_id")})
    # (priority=critical é tratado no policies.apply_policies)

    # 6) Banda vs meters/port speed
    min_req = bw.get("min_mbps")
    max_req = bw.get("max_mbps")

    # valores default conservadores para o caso da intenção não informar max
    if min_req is not None and max_req is None:
        max_req = float(min_req) * 2.0
        bw["max_mbps"] = max_req
        info["adjustments"].append({"bandwidth.max_mbps": "set_2x_min"})

    # Se medidores não são suportados, só aceitamos se o pedido couber fisicamente (porta)
    if not meters_ok:
        if min_req is not None and float(min_req) > port_lo:
            return "deny", specCap, _err("min_bandwidth_exceeds_port_speed", {"min_mbps": min_req, "port_speed_mbps": port_lo})
        if max_req is not None and float(max_req) > port_lo:
            # ajusta para o teto físico
            bw["max_mbps"] = float(port_lo)
            info["adjustments"].append({"bandwidth.max_mbps": f"clamped_to_port_speed_{port_lo}Mbps"})
    else:
        # meters suportados: checar intervalo [meter_min, meter_max] e porta
        if min_req is not None:
            if float(min_req) < meter_min:
                return "deny", specCap, _err("min_bandwidth_below_meter_min", {"min": min_req, "meter_min": meter_min})
            if float(min_req) > meter_max:
                return "deny", specCap, _err("min_bandwidth_above_meter_max", {"min": min_req, "meter_max": meter_max})
            if float(min_req) > port_lo:
                return "deny", specCap, _err("min_bandwidth_exceeds_port_speed", {"min": min_req, "port_speed_mbps": port_lo})
        if max_req is not None:
            if float(max_req) < meter_min:
                bw["max_mbps"] = float(meter_min)
                info["adjustments"].append({"bandwidth.max_mbps": f"bumped_to_meter_min_{meter_min}Mbps"})
            if float(max_req) > meter_max:
                bw["max_mbps"] = float(meter_max)
                info["adjustments"].append({"bandwidth.max_mbps": f"clamped_to_meter_max_{meter_max}Mbps"})
            if float(bw["max_mbps"]) > port_lo:
                bw["max_mbps"] = float(port_lo)
                info["adjustments"].append({"bandwidth.max_mbps": f"clamped_to_port_speed_{port_lo}Mbps"})

        # coerência min<=max
        if min_req is not None and max_req is not None:
            if float(min_req) > float(max_req):
                return "deny", specCap, _err("min_gt_max_bandwidth", {"min": min_req, "max": max_req})

    # 7) Propaga alterações no specCap
    if "bandwidth" in req:
        specCap["requirements"]["bandwidth"] = bw

    # Notas informativas (não bloqueantes)
    info["notes"].append({
        "profile_id": profile.get("profile_id"),
        "port_speed_mbps": port_lo,
        "meters": {
            "supported": meters_ok,
            "min_rate_mbps": meter_min if meters_ok else None,
            "max_rate_mbps": meter_max if meters_ok else None
        },
        "queues": {
            "strict": has_strict,
            "wfq_supported": has_wfq
        }
    })

    return "allow", specCap, info
