"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

Backend NETCONF 'real' – Etapa 1 (stub seguro).
Apenas define a API compatível, sem efetivar edit-config ainda.
"""

# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Any, Dict
from . import BackendResult
from .config import load_backend_config

# Import opcional – só vamos exigir de fato quando ligarmos o modo 'real'
try:
    from ncclient import manager  # type: ignore
except Exception:
    manager = None  # pragma: no cover


def apply_qos(domain_cfg: Dict[str, Any], intent_qos: Dict[str, Any]) -> BackendResult:
    # Etapa 1: apenas sinalização de que o backend existe.
    return BackendResult(
        ok=True,
        details={
            "backend": "real_netconf",
            "status": "stub",
            "note": "Etapa 1 – implementação efetiva virá na Etapa 3",
        },
    )


def apply_multicast(domain_cfg: Dict[str, Any], intent_mc: Dict[str, Any]) -> BackendResult:
    return BackendResult(
        ok=True,
        details={
            "backend": "real_netconf",
            "status": "stub",
            "note": "Etapa 1 – implementação efetiva virá na Etapa 3",
        },
    )


def inspect_state(domain_cfg: Dict[str, Any]) -> Dict[str, Any]:
    # Podemos retornar um snapshot mínimo só para sanidade
    cfg = load_backend_config()
    return {
        "backend": "real_netconf",
        "target": {
            "host": cfg["netconf"]["host"],
            "port": cfg["netconf"]["port"],
        },
        "status": "stub",
    }
