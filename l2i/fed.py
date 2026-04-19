"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0
"""

# l2i/fed.py
# MAD federado v0 (com artefatos estritamente por domínio)

from copy import deepcopy
from typing import Dict, Tuple, Any, List

from l2i.validator import validate_spec
from l2i.policies import apply_policies
from l2i.capabilities import ensure_capability_valid, check_capabilities
from l2i.synth import synthesize_ir
from l2i.emit import emit_netconf_like, emit_p4runtime_like

def _with_scope(spec: dict, scope: str, issuer: str, audience: str) -> dict:
    sp = deepcopy(spec)
    sp["scope"] = scope
    meta = sp.get("metadata", {})
    meta.update({"issued_by": issuer, "audience": audience})
    sp["metadata"] = meta
    return sp

def slice_by_domains(spec: dict, domains: List[str],
                     issuer: str = "ced.gateway",
                     audience_prefix: str = "domain:") -> Dict[str, dict]:
    return {d: _with_scope(spec, scope=d, issuer=issuer, audience=f"{audience_prefix}{d}") for d in domains}

def plan_for_domain(spec_d: dict, profile: dict) -> Tuple[str, dict, dict, dict]:
    # CED
    specC, errs = validate_spec(spec_d)
    if errs:
        return "deny", {"errors": errs}, {}, {}
    # Políticas (MAD)
    pol_status, specP, pol_info = apply_policies(specC)
    if pol_status == "deny":
        return "deny", {"errors": [{"code": "E-POLICY-DENY", "detail": pol_info}]}, {}, {}
    # Capacidades (MAD)
    ensure_capability_valid(profile)
    cap_status, specCap, cap_info = check_capabilities(specP, profile)
    if cap_status == "deny":
        return "deny", {"errors": [{"code": "E-CAP-DENY", "detail": cap_info}]}, {}, {}
    # Síntese (AC)
    ir = synthesize_ir(specCap, profile)
    return "allow", ir, pol_info, cap_info

def plan_multidomain(spec: dict, profiles_by_domain: Dict[str, dict],
                     domains: List[str]) -> Dict[str, Dict[str, Any]]:
    out = {}
    sl = slice_by_domains(spec, domains)
    # Mapeamento de backends/artefatos por domínio (estrito)
    domain_backends = {
        "A": ["legacy_exec"],
        "B": ["netconf_like"],
        "C": ["p4runtime_like"],
    }
    for d in domains:
        status, ir, pol_info, cap_info = plan_for_domain(sl[d], profiles_by_domain[d])
        artifacts: Dict[str, Any] = {}
        backs = domain_backends.get(d, [])
        if status == "allow":
            if "netconf_like" in backs:
                artifacts["netconf"] = emit_netconf_like(ir)
            if "p4runtime_like" in backs:
                artifacts["p4runtime"] = emit_p4runtime_like(ir)
            # Em A (legacy_exec) não geramos nada aqui; o cenário injeta tc_cmds após aplicar.
        out[d] = {
            "status": "ok" if status == "allow" else "deny",
            "ir": ir,
            "policy": {"applied": status == "allow"} if status == "allow" else {},
            "capabilities": {"checked": status == "allow"} if status == "allow" else {},
            "backends": backs if status == "allow" else [],
            "artifacts": artifacts,
            "errors": [] if status == "allow" else ir.get("errors", [])
        }
    return out
