"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

Backend P4 mock — não conecta no P4Runtime; apenas simula table_mods.
Útil para manter o pipeline funcionando enquanto o real é plugado.
"""

from typing import Any, Dict, Mapping, Tuple


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


def _dscp_for_class(class_name: str) -> int:
    """Mirror the deterministic class mapping used by the real backend."""

    digits = "".join(character for character in class_name if character.isdigit())
    value = int(digits) if digits else 0
    if value >= 40:
        return 32
    if value >= 30:
        return 24
    if value >= 20:
        return 16
    if value >= 10:
        return 8
    return 0


def apply_qos(
    ctx: Dict[str, Any],
    intent: Dict[str, Any],
    backend: Dict[str, Any] | None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Mock P4 backend: does NOT connect to P4Runtime.
    It returns a structure compatible with the "real" shim for logging and tests.
    """

    payload = _intent_payload(intent)
    class_name = str(payload.get("class", "")).strip().lower()
    priority = str(payload.get("priority", "")).strip().lower()
    minimum = payload.get("min_mbps")
    if not class_name or not priority or minimum is None:
        return False, {
            "backend": "p4_mock",
            "planned": None,
            "exec": {"simulated": True, "ok": False},
            "error": "class, priority and min_mbps are required",
            "request": {
                "backend": backend,
                "intent": intent,
            },
        }
    normalized = {
        "class": class_name,
        "priority": priority,
        "min_mbps": minimum,
    }
    if payload.get("max_mbps") is not None:
        normalized["max_mbps"] = payload["max_mbps"]
    dscp = _dscp_for_class(class_name)

    return True, {
        "backend": "p4_mock",
        "planned": {
            "kind": "p4",
            "pipeline": "bmv2-mock",
            "entry_count": 1,
            "input_intent": normalized,
            "materialized_projection": {
                "source_class": class_name,
                "source_priority": priority,
                "new_dscp": dscp,
            },
            "not_materialized": [
                key
                for key in ("min_mbps", "max_mbps")
                if key in normalized
            ],
        },
        "exec": {"simulated": True, "ok": True},
        "request": {
            "backend": backend,
            "intent": intent,
            "normalized_intent": normalized,
            "priority": priority,
            "new_dscp": dscp,
        },
    }
