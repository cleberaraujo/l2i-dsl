"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

Backend NETCONF mock — não abre sessão real; apenas simula um <edit-config>.
Use para validar a L2i/pipeline. O router envelopa a resposta.
"""

from html import escape
from typing import Any, Dict, Mapping, Optional, Tuple


def _intent_payload(intent: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize the canonical direct intent and retained nested callers."""

    nested_qos = intent.get("qos")
    nested_requirements = intent.get("requirements")
    if isinstance(nested_qos, Mapping) and nested_qos:
        payload = dict(nested_qos)
    elif isinstance(nested_requirements, Mapping) and nested_requirements:
        payload = dict(nested_requirements)
    else:
        payload = dict(intent)

    bandwidth = payload.get("bandwidth")
    bandwidth_fields = (
        dict(bandwidth)
        if isinstance(bandwidth, Mapping)
        else {}
    )
    priority_field = payload.get("priority")
    priority = (
        str(priority_field.get("level", "")).strip().lower()
        if isinstance(priority_field, Mapping)
        else str(
            priority_field
            or payload.get("priority_level")
            or payload.get("level")
            or ""
        ).strip().lower()
    )
    class_name = str(payload.get("class", "")).strip().lower()
    if not class_name and priority:
        class_name = {
            "high": "prio10",
            "medium": "prio20",
            "normal": "prio20",
            "low": "prio30",
            "best_effort": "prio30",
            "best-effort": "prio30",
        }.get(priority, "")
    if not priority and class_name:
        priority = {
            "prio10": "high",
            "prio20": "medium",
            "prio30": "best_effort",
        }.get(class_name, "")

    normalized: Dict[str, Any] = {
        "class": class_name,
        "min_mbps": payload.get(
            "min_mbps",
            payload.get(
                "bandwidth_min_mbps",
                bandwidth_fields.get("min_mbps"),
            ),
        ),
        "priority": priority,
    }
    direct_maximum_present = "max_mbps" in payload
    legacy_maximum_present = "bandwidth_max_mbps" in payload
    maximum = payload.get(
        "max_mbps",
        payload.get(
            "bandwidth_max_mbps",
            bandwidth_fields.get("max_mbps"),
        ),
    )
    # Historical S1 timing callers encode an absent maximum as
    # bandwidth_max_mbps=0.  Preserve that caller without accepting zero as a
    # valid canonical max_mbps value.
    if (
        not direct_maximum_present
        and legacy_maximum_present
        and maximum is not None
        and float(maximum) <= 0
    ):
        maximum = None
    if maximum is not None:
        normalized["max_mbps"] = maximum
    return normalized


def _uint32(value: Any, label: str) -> Optional[int]:
    """Normalize one optional YANG uint32 without truncating fractions."""

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    number = float(value)
    if not number.is_integer() or not 0 <= number <= 4_294_967_295:
        raise ValueError(f"{label} must be a uint32")
    return int(number)


def _build_qos_xml(intent: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Serialize the semantic fields used by the real NETCONF backend."""

    payload = _intent_payload(intent)
    class_name = str(payload.get("class", "")).strip()
    if not class_name:
        raise ValueError("intent.class is required")
    minimum = _uint32(payload.get("min_mbps"), "intent.min_mbps")
    maximum = _uint32(payload.get("max_mbps"), "intent.max_mbps")
    if minimum is None:
        raise ValueError("intent.min_mbps is required")
    if maximum is not None and minimum > maximum:
        raise ValueError("intent.min_mbps must not exceed max_mbps")

    leaves = [
        f"<class>{escape(class_name)}</class>",
        f"<min-mbps>{minimum}</min-mbps>",
    ]
    if maximum is not None:
        leaves.append(f"<max-mbps>{maximum}</max-mbps>")
    xml = (
        '<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        '<qos xmlns="urn:l2i:qos">'
        + "".join(leaves)
        + "</qos></config>"
    )
    normalized = {
        "class": class_name,
        "min_mbps": minimum,
        "priority": str(payload.get("priority", "")).strip(),
    }
    if maximum is not None:
        normalized["max_mbps"] = maximum
    return xml, normalized


def apply_qos(
    ctx: Dict[str, Any],
    intent: Dict[str, Any],
    backend: Dict[str, Any] | None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Mock NETCONF backend: does NOT connect anywhere.
    It returns a structure compatible with "real" backends for logging and tests.
    """

    try:
        xml_snippet, normalized = _build_qos_xml(intent)
    except (TypeError, ValueError) as exc:
        return False, {
            "backend": "netconf_mock",
            "planned": None,
            "executed": {
                "simulated": True,
                "ok": False,
            },
            "error": f"{type(exc).__name__}: {exc}",
            "request": {
                "backend": backend,
                "intent": intent,
            },
        }

    return True, {
        "backend": "netconf_mock",
        "planned": {
            "kind": "netconf",
            "rpc": "edit-config",
            "edit_config_count": 1,
            "patch_count": 1,
            "payload_bytes": len(xml_snippet.encode("utf-8", errors="ignore")),
            "target": "running",
            "normalized_intent": normalized,
        },
        "executed": {
            "simulated": True,
            "ok": True,
        },
        "request": {
            "backend": backend,
            "intent": intent,
            "normalized_intent": normalized,
            "xml": xml_snippet,
        },
    }
