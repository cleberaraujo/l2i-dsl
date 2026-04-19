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
Backend P4 mock — não conecta no P4Runtime; apenas simula table_mods.
Útil para manter o pipeline funcionando enquanto o real é plugado.
"""

from typing import Any, Dict, Tuple


def apply_qos(ctx: Dict[str, Any], intent: Dict[str, Any], backend: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """
    Mock P4 backend: does NOT connect to P4Runtime.
    It returns a structure compatible with the "real" shim for logging and tests.
    """

    qos = intent.get("qos", {}) or intent.get("requirements", {}) or {}
    prio = qos.get("priority", qos.get("priority_level", qos.get("level", "best_effort")))

    ok = True
    return ok, {
        "backend": "p4_mock",
        "planned": {
            "kind": "p4",
            "pipeline": "bmv2-mock",
            "entry_count": 1,
        },
        "exec": {"simulated": True, "ok": True},
        "request": {
            "backend": backend,
            "intent": intent,
            "priority": prio,
        },
    }
