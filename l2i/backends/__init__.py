# -*- coding: utf-8 -*-
"""
Backends – base types shared by mock and real implementations.
Etapa 1: apenas tipos e helpers. Nenhum cenário alterado ainda.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Union

JsonDict = Dict[str, Any]


@dataclass
class BackendResult:
    ok: bool
    details: Union[str, JsonDict, None] = None

    def to_dict(self) -> JsonDict:
        return {"ok": self.ok, "details": self.details}


# Assinatura comum (mock/real) que os cenários irão chamar:
# - apply_qos(domain_cfg, intent_qos) -> BackendResult
# - apply_multicast(domain_cfg, intent_mc) -> BackendResult
# - remove_qos(domain_cfg, intent_qos) -> BackendResult (opcional)
# - remove_multicast(domain_cfg, intent_mc) -> BackendResult (opcional)
# - inspect_state(domain_cfg) -> dict
#
# domain_cfg: dict com infos do domínio B/C (host/port/credenciais/etc.)
# intent_qos / intent_mc: trechos da IR já normalizados pela L2i.
