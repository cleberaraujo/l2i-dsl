"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

# l2i/emit.py
# Emissores simplificados de artefatos por domínio (dry-run):
#  - NETCONF-like (B)
#  - P4Runtime-like (C)
"""

import json
from typing import Dict, Any

def emit_netconf_like(ir: Dict[str, Any]) -> str:
    """
    Gera um "payload" NETCONF-like com o plano IR.
    Na prática, retornamos um JSON-string contendo as operações desejadas.
    """
    payload = {
        "rpc": "edit-config",
        "target": "running",
        "config": {
            "l2i:flows": {
                "plan_id": ir.get("plan_id"),
                "flow_id": ir.get("flow_id"),
                "actions": ir.get("actions", []),
                "constraints": ir.get("constraints", {})
            }
        }
    }
    return json.dumps(payload, indent=2)

def emit_p4runtime_like(ir: Dict[str, Any]) -> str:
    """
    Gera um "payload" P4Runtime-like com o plano IR.
    Também como JSON-string.
    """
    # exemplo: traduz prioridade em DSCP; reserva/cap em meters; etc.
    actions = ir.get("actions", [])
    meters = {}
    dscp = None
    for a in actions:
        t = a.get("type")
        p = a.get("params", {})
        if t == "ReserveMinRate":
            meters["min_mbps"] = p.get("min_mbps")
        elif t == "CapMaxRate":
            meters["max_mbps"] = p.get("max_mbps")
        elif t == "SetPriority":
            lvl = p.get("level")
            if lvl == "high": dscp = 46   # EF
            elif lvl == "medium": dscp = 26
            elif lvl == "low": dscp = 10

    payload = {
        "write_request": {
            "device_id": "p4_device_1",
            "plan_id": ir.get("plan_id"),
            "flow_id": ir.get("flow_id"),
            "tables": [
                {"table": "ingress.qos.mark", "action": "set_dscp", "params": {"dscp": dscp}} if dscp is not None else {},
            ],
            "meters": meters
        }
    }
    return json.dumps(payload, indent=2)
