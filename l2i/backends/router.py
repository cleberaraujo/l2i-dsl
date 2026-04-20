"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

l2i/backends/router.py

Seleciona módulos de backend por domínio (A, B, C) e por modo (mock|real).

Motivação científica:
- o experimento deve refletir uma cadeia multi-domínio (A: tc/htb, B: NETCONF, C: P4),
  mesmo quando algum domínio está em modo "mock" ou "placeholder".

Contrato:
- get_backends("mock"|"real") -> {"A": module, "B": module|[modules], "C": module|[modules]}
"""

from __future__ import annotations

import importlib
from typing import Dict, Any


def _import_backend(mod: str):
    """
    Import obrigatório. Se falhar, levanta ImportError com contexto.
    """
    try:
        return importlib.import_module(f"l2i.backends.{mod}")
    except Exception as e:
        raise ImportError(f"Failed to import backend module l2i.backends.{mod}: {e}") from e


def get_backends(mode: str) -> Dict[str, Any]:
    if mode not in ("mock", "real"):
        raise ValueError(f"Invalid backend mode: {mode}")

    # A é sempre local (tc/htb), independente de mock/real:
    # - em "mock" ele ainda pode registrar o que *seria* aplicado;
    # - em "real" ele aplica de verdade via tc.
    a_mod = _import_backend("_shim_linux_tc_local")

    if mode == "mock":
        b_mod = _import_backend("_shim_mock_netconf")
        c_mod = _import_backend("_shim_mock_p4")
    else:
        b_mod = _import_backend("_shim_real_netconf")
        c_mod = _import_backend("_shim_real_p4")

    return {"A": a_mod, "B": b_mod, "C": c_mod}
