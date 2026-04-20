"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

# l2i/models.py
# Modelos canônicos usados no pipeline L2i.
# Mantemos estruturas simples (TypedDict/alias) para casar com JSON direto.
"""

from typing import Any, Dict, List, Optional, TypedDict

# -----------------------------------------------------------------------------
# Tipos básicos

# Especificação canônica (após CED/MAD): dicionário flexível validado.
CanonicalSpec = Dict[str, Any]


class ErrorMsg(TypedDict, total=False):
    """
    Mensagem de erro padronizada usada por validator/policies/capabilities.
    - code: identificador curto do erro (ex.: "E-SCHEMA-MISSING", "E-CAP-DENY")
    - field: (opcional) caminho/field associado ao erro (ex.: "requirements.bandwidth.min_mbps")
    - detail: (opcional) objeto com informações adicionais
    """
    code: str
    field: Optional[str]
    detail: Any


class IRAction(TypedDict, total=False):
    """
    Ação IR genérica.
    Exemplos de type:
      - "ReserveMinRate"   params: {"min_mbps": float}
      - "CapMaxRate"       params: {"max_mbps": float}
      - "SetPriority"      params: {"level": "high"|"medium"|"low"}
      - "MarkDSCP"         params: {"dscp": int}
    """
    type: str
    params: Dict[str, Any]


class IRPlan(TypedDict, total=False):
    """
    Plano intermediário IR entregue ao AC/emitters.
    """
    plan_id: str
    flow_id: str
    actions: List[IRAction]
    constraints: Dict[str, Any]
    atomic: bool


def mk_ir_plan(flow_id: str, plan_id: Optional[str] = None, atomic: bool = False) -> IRPlan:
    """
    Constrói um IRPlan básico, pronto para receber ações.
    """
    if plan_id is None:
        plan_id = f"ir-{flow_id}"
    return IRPlan(
        plan_id=plan_id,
        flow_id=flow_id,
        actions=[],
        constraints={},
        atomic=atomic
    )
