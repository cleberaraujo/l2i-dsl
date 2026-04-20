i"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

Backend P4Runtime 'real' – Etapa 1 (stub seguro).
Define a API, sem programar tabelas ainda.
"""

# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Any, Dict, Tuple
from . import BackendResult
from .config import load_backend_config

# Imports opcionais – exigidos apenas quando ligarmos Etapa 4
try:
    import grpc  # type: ignore
    from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc  # type: ignore
except Exception:
    grpc = None       # pragma: no cover
    p4runtime_pb2 = None  # pragma: no cover
    p4runtime_pb2_grpc = None  # pragma: no cover


def apply_qos(domain_cfg: Dict[str, Any], intent_qos: Dict[str, Any]) -> BackendResult:
    return BackendResult(
        ok=True,
        details={
            "backend": "real_p4",
            "status": "stub",
            "note": "Etapa 1 – implementação efetiva virá na Etapa 4",
        },
    )


def apply_multicast(domain_cfg: Dict[str, Any], intent_mc: Dict[str, Any]) -> BackendResult:
    return BackendResult(
        ok=True,
        details={
            "backend": "real_p4",
            "status": "stub",
            "note": "Etapa 1 – implementação efetiva virá na Etapa 4",
        },
    )


def inspect_state(domain_cfg: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_backend_config()
    return {
        "backend": "real_p4",
        "target": {
            "address": cfg["p4runtime"]["address"],
            "device_id": cfg["p4runtime"]["device_id"],
        },
        "status": "stub",
    }
