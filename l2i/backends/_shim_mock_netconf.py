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
Backend NETCONF mock — não abre sessão real; apenas simula um <edit-config>.
Use para validar a L2i/pipeline. O router envelopa a resposta.
"""

from typing import Any, Dict, Tuple


def apply_qos(ctx: Dict[str, Any], intent: Dict[str, Any], backend: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """
    Mock NETCONF backend: does NOT connect anywhere.
    It returns a structure compatible with "real" backends for logging and tests.
    """

    qos = intent.get("qos", {}) or intent.get("requirements", {}) or {}
    prio = qos.get("priority", qos.get("priority_level", qos.get("level", "best_effort")))
    be_mbps = qos.get("be_mbps", qos.get("be", None))

    xml_snippet = f"""<config>
  <l2i>
    <qos>
      <priority>{prio}</priority>
      <be_mbps>{be_mbps if be_mbps is not None else ""}</be_mbps>
    </qos>
  </l2i>
</config>"""

    ok = True
    return ok, {
        "backend": "netconf_mock",
        "planned": {
            "kind": "netconf",
            "rpc": "edit-config",
            "edit_config_count": 1,
            "patch_count": 1,
            "payload_bytes": len(xml_snippet.encode("utf-8", errors="ignore")),
            "target": "running",
        },
        "executed": {
            "simulated": True,
            "ok": True,
        },
        "request": {
            "backend": backend,
            "intent": intent,
            "xml": xml_snippet,
        },
    }
