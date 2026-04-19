"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0
"""

# -*- coding: utf-8 -*-
"""
Config loader para backends 'reais'. Lê YAML opcional quando --backend real.
Se o arquivo não existir, retorna defaults seguros.
"""

from __future__ import annotations
import os
from typing import Any, Dict
try:
    import yaml  # type: ignore
except Exception:
    yaml = None  # yaml é opcional; só exigimos quando 'real'

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),  # dsl/l2i
    "profiles",
    "backends_real.yaml"
)

def load_backend_config(path: str | None = None) -> Dict[str, Any]:
    cfg_path = path or DEFAULT_PATH
    if not os.path.exists(cfg_path):
        # Defaults neutros (localhost) – você pode editar depois no YAML.
        return {
            "netconf": {
                "host": "127.0.0.1",
                "port": 830,
                "username": "dev",
                "password": "dev",
                "capabilities": ["base:1.1"],
            },
            "p4runtime": {
                "address": "127.0.0.1:50051",
                "device_id": 0,
                "pipeline_json": "/tmp/l2i_base.json",
                "election_id": [0, 1],
                "manage_switch": False,
            },
        }
    if yaml is None:
        raise RuntimeError(
            "PyYAML não encontrado, mas um backends_real.yaml foi solicitado. "
            "Instale com: pip install pyyaml"
        )
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data
