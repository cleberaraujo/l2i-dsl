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
_shim_real_netconf.py

Backend NETCONF "real" para o Domínio B.

Correção crítica:
- o YANG do l2i-qos usa uint32 em min-mbps/max-mbps (conforme erro do netopeer2).
- portanto, valores vindos do intent (que podem chegar como 2.0) DEVEM ser serializados como inteiro.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from ncclient import manager
from ncclient.operations import RPCError


def _mask_secret(t: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(t)
    for k in ("password", "pass", "secret"):
        if k in out and out[k] is not None:
            out[k] = "***"
    return out


def _fmt_uint32(v: Any) -> str | None:
    """
    Serializa algo como uint32 string:
    - aceita int, float "inteiro" (ex.: 2.0), str numérica
    - rejeita float fracionário (ex.: 2.5)
    Retorna None se v for None.
    """
    if v is None:
        return None

    # tenta normalizar
    if isinstance(v, bool):
        # bool é int em Python, mas não queremos aceitar
        raise ValueError("uint32 value must be numeric, not bool")

    if isinstance(v, (int,)):
        if v < 0:
            raise ValueError("uint32 must be >= 0")
        return str(v)

    if isinstance(v, float):
        if v < 0:
            raise ValueError("uint32 must be >= 0")
        if not v.is_integer():
            raise ValueError(f"uint32 cannot be fractional: {v}")
        return str(int(v))

    if isinstance(v, str):
        s = v.strip()
        # aceita "2" ou "2.0"
        if s.isdigit():
            return s
        try:
            f = float(s)
            if f < 0 or (not f.is_integer()):
                raise ValueError
            return str(int(f))
        except Exception:
            raise ValueError(f"uint32 string is invalid: {v!r}")

    raise ValueError(f"unsupported uint32 type: {type(v).__name__}")


def _build_qos_xml(intent: Dict[str, Any]) -> str:
    cls = intent.get("class", "default")
    min_mbps = _fmt_uint32(intent.get("min_mbps"))
    max_mbps = _fmt_uint32(intent.get("max_mbps"))

    parts = [f"<class>{cls}</class>"]
    if min_mbps is not None:
        parts.append(f"<min-mbps>{min_mbps}</min-mbps>")
    if max_mbps is not None:
        parts.append(f"<max-mbps>{max_mbps}</max-mbps>")

    return f"""
<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <qos xmlns="urn:l2i:qos">
    {''.join(parts)}
  </qos>
</config>
""".strip()


def apply_qos(domain: Any, intent: Dict[str, Any], target: Dict[str, Any] | None = None) -> Tuple[bool, Dict[str, Any]]:
    domain_name = domain["name"] if isinstance(domain, dict) and "name" in domain else str(domain)

    if not isinstance(target, dict):
        return False, {
            "domain": {"name": domain_name},
            "intent": intent,
            "target": None,
            "channel": "netconf",
            "planned": {"note": "missing target dict (host/port/user/password)"},
            "exec": {"ok": False, "stderr": "missing target", "detail": ""},
            "running_snapshot": None,
        }

    host = target.get("host", "127.0.0.1")
    port = int(target.get("port", 830))
    user = target.get("user") or target.get("username") or ""
    password = target.get("password") or ""
    key_filename = (
        target.get("key_filename")
        or target.get("ssh_key")
        or target.get("private_key")
        or ""
    )
    timeout = int(target.get("timeout", 10))

    # Se o intent vier com floats tipo 2.0, isso agora vira "2" no XML.
    try:
        xml_cfg = _build_qos_xml(intent)
    except Exception as e:
        return False, {
            "domain": {"name": domain_name},
            "intent": intent,
            "target": _mask_secret(target),
            "channel": "netconf",
            "planned": {"host": host, "port": port, "username": user, "yang_model": "urn:l2i:qos"},
            "exec": {"ok": False, "stderr": "intent->xml serialization failed", "detail": f"{type(e).__name__}: {e}"},
            "running_snapshot": None,
        }

    info: Dict[str, Any] = {
        "domain": {"name": domain_name},
        "intent": intent,
        "target": _mask_secret(target),
        "channel": "netconf",
        "planned": {
            "host": host,
            "port": port,
            "username": user,
            "key_filename": key_filename if key_filename else None,
            "yang_model": "urn:l2i:qos",
            "xml_sent": xml_cfg,
        },
        "exec": {"ok": False, "stdout": "", "stderr": "", "detail": ""},
        "running_snapshot": None,
    }

    if not user:
        info["exec"]["stderr"] = "empty username (check backends_real.yaml)"
        return False, info

    if not password and not key_filename:
        info["exec"]["stderr"] = "missing authentication method: provide password or key_filename"
        return False, info

    try:
        connect_kwargs = {
            "host": host,
            "port": port,
            "username": user,
            "hostkey_verify": False,
            "allow_agent": False,
            "look_for_keys": False,
            "timeout": timeout,
        }

        if key_filename:
            connect_kwargs["key_filename"] = key_filename

        if password:
            connect_kwargs["password"] = password

        with manager.connect(**connect_kwargs) as m:

            try:
                m.edit_config(target="running", config=xml_cfg)
            except RPCError as e:
                info["exec"]["stderr"] = f"RPCError: {e}"
                return False, info

            # read-back (padronização)
            try:
                get_reply = m.get_config(
                    source="running",
                    filter=("subtree", '<qos xmlns="urn:l2i:qos"/>')
                )
                info["running_snapshot"] = get_reply.xml
            except Exception as e:
                info["exec"]["detail"] = f"get_config failed: {e}"

            info["exec"]["ok"] = True
            return True, info

    except Exception as e:
        info["exec"]["stderr"] = "NETCONF real operation failed (connect/edit-config)."
        info["exec"]["detail"] = f"{type(e).__name__}: {e}"
        return False, info
