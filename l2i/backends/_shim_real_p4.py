"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

_shim_real_p4.py (Domínio C / P4Runtime)

Objetivo (científico e reprodutível):
- materializar a adaptação no domínio C via P4Runtime
- sempre produzir: probe do P4Info + readback da tabela
- decidir "aplicado" com base no *estado observado* (read-back), não apenas no retorno do RPC,
  pois BMv2/P4Runtime pode retornar erros opacos (StatusCode.UNKNOWN) mesmo quando o estado final
  fica consistente.

Problema observado nos seus artefatos:
- exec.stderr="write failed" (grpc UNKNOWN), mas readback mostra entries=1 com a regra correta.
  Portanto o sistema precisa de "post-write verification".

Melhorias nesta versão:
1) Interpreta match_type por *nome* do enum (mais robusto do que números, que variam com stubs/versões).
2) Faz upsert: tenta INSERT e depois MODIFY.
3) Faz verificação pós-write: se a regra esperada aparece no readback, marca applied=True e registra
   write_error_but_verified=True.
"""

# -*- coding: utf-8 -*-

from __future__ import annotations

from typing import Any, Dict, Tuple, Optional, List
import ipaddress

from google.protobuf import text_format
from p4.v1 import p4runtime_pb2
from p4.config.v1 import p4info_pb2

from l2i.third_party.p4rt_min.client import P4RTClient


DEFAULT_TABLE = "MyIngress.qos_table"
DEFAULT_MATCH_FIELD = "hdr.ipv4.dstAddr"
DEFAULT_PREFER_ACTION = "set_dscp"


def _mask_secret(t: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(t)
    for k in ("password", "pass", "secret"):
        if k in out and out[k] is not None:
            out[k] = "***"
    return out


def _load_p4info(p4info_path: str) -> p4info_pb2.P4Info:
    p4info = p4info_pb2.P4Info()
    txt = open(p4info_path, "r", encoding="utf-8").read()
    text_format.Merge(txt, p4info)
    return p4info


def _encode_u32_be(v: int, nbytes: int) -> bytes:
    if v < 0:
        raise ValueError("uint must be >= 0")
    return int(v).to_bytes(nbytes, "big", signed=False)


def _encode_ipv4(ip: str) -> bytes:
    addr = ipaddress.ip_address(ip)
    if addr.version != 4:
        raise ValueError("only IPv4 supported")
    return addr.packed


def _find_table(p4info: p4info_pb2.P4Info, table_name: str) -> Optional[p4info_pb2.Table]:
    return next((t for t in p4info.tables if t.preamble.name == table_name), None)


def _find_action_by_id(p4info: p4info_pb2.P4Info, action_id: int) -> Optional[p4info_pb2.Action]:
    return next((a for a in p4info.actions if int(a.preamble.id) == int(action_id)), None)


def _probe_table_actions(p4info: p4info_pb2.P4Info, table: p4info_pb2.Table) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for aref in table.action_refs:
        act = _find_action_by_id(p4info, aref.id)
        if act is None:
            continue
        out.append({
            "action_id": int(act.preamble.id),
            "action_name": act.preamble.name,
            "params": [{"name": p.name, "id": int(p.id)} for p in act.params],
        })
    return out


def _find_match_field(table: p4info_pb2.P4Info.Table, field_name: str) -> Optional[p4info_pb2.MatchField]:
    return next((m for m in table.match_fields if m.name == field_name), None)


def _pick_action(actions: List[Dict[str, Any]], prefer_substr: str) -> Optional[Dict[str, Any]]:
    pref = (prefer_substr or "").strip()
    if pref:
        for a in actions:
            if pref in a["action_name"]:
                return a
    for a in actions:
        if a["action_name"] != "NoAction":
            return a
    return actions[0] if actions else None


def _readback_dump_table(client: P4RTClient, table_id: int, table_name: str) -> str:
    ent = p4runtime_pb2.Entity()
    ent.table_entry.table_id = int(table_id)
    ok, msg, resps = client.read([ent])
    if not ok:
        return f"[readback_failed] {msg}"

    out_lines = [f"# Readback table={table_name} table_id={table_id} responses={len(resps)}"]
    n_entries = 0
    for rr in resps:
        for e in rr.entities:
            if e.HasField("table_entry"):
                n_entries += 1
                out_lines.append(text_format.MessageToString(e.table_entry).strip())
                out_lines.append("---")
    out_lines.append(f"# entries={n_entries}")
    return "\n".join(out_lines).strip() + "\n"


def _intent_to_dscp(intent: Dict[str, Any]) -> int:
    """
    Mapeamento determinístico (reprodutível):
    prio10 -> CS1 (8)
    prio20 -> CS2 (16)
    prio30 -> CS3 (24)
    prio40 -> CS4 (32)
    default -> CS0 (0)
    """
    cls = str(intent.get("class", "prio20")).lower()
    digits = "".join([c for c in cls if c.isdigit()])
    try:
        p = int(digits) if digits else 0
    except Exception:
        p = 0

    if p >= 40:
        return 32
    if p >= 30:
        return 24
    if p >= 20:
        return 16
    if p >= 10:
        return 8
    return 0


def _match_type_name(mf_def: p4info_pb2.MatchField) -> str:
    """
    Retorna o NOME do match_type (EXACT/LPM/TERNARY/RANGE/OPTIONAL),
    evitando dependência do valor numérico, que pode variar conforme stubs/versões.
    """
    try:
        # Alguns stubs expõem enum como classe interna MatchField.MatchType
        return p4info_pb2.MatchField.MatchType.Name(int(mf_def.match_type))
    except Exception:
        # Fallback: tenta via atributo 'match_type' como string ou repr
        return str(mf_def.match_type)


def _entry_matches_expected(table_entry: p4runtime_pb2.TableEntry, table_id: int, field_id: int,
                            dst_ip: str, action_id: int, dscp: int) -> bool:
    """
    Verifica se uma TableEntry lida do switch corresponde ao que queríamos instalar.
    Para o experimento atual, checamos:
    - table_id
    - match field_id e valor IPv4 em EXACT ou LPM ou TERNARY
    - action_id e param[0] == dscp (aceita 1 byte ou 4 bytes BE com o mesmo valor)
    """
    if int(table_entry.table_id) != int(table_id):
        return False

    ip_bytes = _encode_ipv4(dst_ip)

    ok_match = False
    for m in table_entry.match:
        if int(m.field_id) != int(field_id):
            continue
        if m.HasField("exact") and m.exact.value == ip_bytes:
            ok_match = True
        if m.HasField("lpm") and m.lpm.value == ip_bytes and int(m.lpm.prefix_len) == 32:
            ok_match = True
        if m.HasField("ternary") and m.ternary.value == ip_bytes:
            # máscara pode variar; para /32 esperamos FFFF_FFFF, mas aceitamos qualquer máscara != 0
            if m.ternary.mask and any(b != 0x00 for b in m.ternary.mask):
                ok_match = True
    if not ok_match:
        return False

    if not table_entry.action or not table_entry.action.action:
        return False
    if int(table_entry.action.action.action_id) != int(action_id):
        return False

    params = list(table_entry.action.action.params)
    if not params:
        return False
    v = bytes(params[0].value)

    # aceita 1 byte ou 4 bytes BE equivalentes
    if len(v) == 1:
        return (v[0] & 0x3F) == (dscp & 0x3F)
    if len(v) == 4:
        try:
            return int.from_bytes(v, "big") == int(dscp)
        except Exception:
            return False
    # se vier outro tamanho, tenta interpretar como big-endian
    try:
        return int.from_bytes(v, "big") == int(dscp)
    except Exception:
        return False


def apply_qos(domain: Any, intent: Dict[str, Any], target: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict[str, Any]]:
    domain_name = domain["name"] if isinstance(domain, dict) and "name" in domain else str(domain)

    if not isinstance(target, dict):
        return False, {
            "domain": {"name": domain_name},
            "intent": intent,
            "target": None,
            "channel": "p4runtime",
            "p4info_probe": None,
            "arbitration": None,
            "exec": {"ok": False, "stderr": "missing target", "detail": ""},
            "installed_rule": None,
            "readback_dump": "[no_dump] missing target",
        }

    address = target.get("address") or target.get("addr") or "127.0.0.1:9559"
    device_id = int(target.get("device_id", 0))
    election_id = target.get("election_id", [0, 100])
    if isinstance(election_id, (list, tuple)) and len(election_id) == 2:
        election_id = (int(election_id[0]), int(election_id[1]))
    else:
        election_id = (0, 100)

    p4info_path = target.get("p4info", "/tmp/l2i_minimal/l2i_minimal.p4info.txtpb")
    dst_ip = target.get("dst_ip", "10.0.0.4")

    table_name = target.get("table_name", DEFAULT_TABLE)
    match_field_name = target.get("match_field_name", DEFAULT_MATCH_FIELD)
    prefer_action = target.get("prefer_action", DEFAULT_PREFER_ACTION)

    info: Dict[str, Any] = {
        "domain": {"name": domain_name},
        "intent": intent,
        "target": _mask_secret(target),
        "channel": "p4runtime",
        "p4info_probe": None,
        "arbitration": {"requested_election_id": list(election_id), "status": None},
        "exec": {
            "ok": False,
            "stderr": "",
            "detail": "",
            "write_error_but_verified": False,
        },
        "installed_rule": None,
        "readback_dump": None,
    }

    client = P4RTClient(
        address=address,
        device_id=device_id,
        election_id=election_id,
        timeout_s=float(target.get("timeout_s", 5.0)),
    )

    try:
        client.connect()

        arb = client.ensure_primary(wait_s=float(target.get("arb_wait_s", 2.0)))
        info["arbitration"]["status"] = {"ok": arb.ok, "is_primary": arb.is_primary, "message": arb.message}

        p4info = _load_p4info(p4info_path)

        table = _find_table(p4info, table_name)
        if table is None:
            info["p4info_probe"] = {"table_name": table_name, "table_found": False}
            info["exec"]["stderr"] = "p4info mismatch"
            info["exec"]["detail"] = f"table not found: {table_name}"
            info["readback_dump"] = "[no_dump] table not found"
            return False, info

        actions = _probe_table_actions(p4info, table)
        mf_def = _find_match_field(table, match_field_name)
        mtype_name = _match_type_name(mf_def) if mf_def else None

        info["p4info_probe"] = {
            "table_name": table_name,
            "table_found": True,
            "table_id": int(table.preamble.id),
            "match_field_name": match_field_name,
            "match_field_found": bool(mf_def),
            "match_field_id": int(mf_def.id) if mf_def else None,
            "match_type": int(mf_def.match_type) if mf_def else None,
            "match_type_name": mtype_name,
            "actions": actions,
        }

        if not arb.ok or not arb.is_primary:
            info["exec"]["stderr"] = "not primary (arbitration not granted)"
            info["readback_dump"] = _readback_dump_table(client, int(table.preamble.id), table_name)
            return False, info

        if not mf_def:
            info["exec"]["stderr"] = "p4info mismatch"
            info["exec"]["detail"] = f"match field not found in table {table_name}: {match_field_name}"
            info["readback_dump"] = _readback_dump_table(client, int(table.preamble.id), table_name)
            return False, info

        chosen = _pick_action(actions, prefer_action)
        if not chosen:
            info["exec"]["stderr"] = "p4info mismatch"
            info["exec"]["detail"] = f"no actions for table {table_name}"
            info["readback_dump"] = _readback_dump_table(client, int(table.preamble.id), table_name)
            return False, info

        if len(chosen.get("params", [])) != 1:
            info["exec"]["stderr"] = "unsupported action shape"
            info["exec"]["detail"] = (
                f"chosen action {chosen['action_name']} has {len(chosen.get('params', []))} params; expected 1"
            )
            info["readback_dump"] = _readback_dump_table(client, int(table.preamble.id), table_name)
            return False, info

        dscp = _intent_to_dscp(intent)
        ip_bytes = _encode_ipv4(dst_ip)

        entry = p4runtime_pb2.TableEntry()
        entry.table_id = int(table.preamble.id)

        # Match conforme NOME do enum (mais robusto do que confiar em números)
        mf = entry.match.add()
        mf.field_id = int(mf_def.id)

        if mtype_name == "EXACT":
            mf.exact.value = ip_bytes
            match_repr = f"{match_field_name}=={dst_ip}"
        elif mtype_name == "LPM":
            mf.lpm.value = ip_bytes
            mf.lpm.prefix_len = 32
            match_repr = f"{match_field_name}={dst_ip}/32"
        elif mtype_name == "TERNARY":
            mf.ternary.value = ip_bytes
            mf.ternary.mask = b"\xff\xff\xff\xff"
            match_repr = f"{match_field_name}~={dst_ip} mask=255.255.255.255"
        elif mtype_name == "RANGE":
            # não faz sentido para IPv4 /32 neste experimento; falha com explicação
            info["exec"]["stderr"] = "unsupported match type"
            info["exec"]["detail"] = "RANGE match is not supported for IPv4 dstAddr in this experiment"
            info["readback_dump"] = _readback_dump_table(client, int(table.preamble.id), table_name)
            return False, info
        elif mtype_name == "OPTIONAL":
            # opcional: tratamos como EXACT (valor presente) se os stubs suportarem
            mf.optional.value = ip_bytes
            match_repr = f"{match_field_name} optional=={dst_ip}"
        else:
            info["exec"]["stderr"] = "unsupported match type"
            info["exec"]["detail"] = f"match_type_name={mtype_name} not supported by this backend"
            info["readback_dump"] = _readback_dump_table(client, int(table.preamble.id), table_name)
            return False, info

        # Action + param: DSCP em 1 byte (0..63)
        entry.action.action.action_id = int(chosen["action_id"])
        ap = entry.action.action.params.add()
        ap.param_id = int(chosen["params"][0]["id"])
        ap.value = bytes([dscp & 0x3F])

        # Upsert: INSERT, se falhar tenta MODIFY
        def _write_update(update_type: int) -> Tuple[bool, str]:
            upd = p4runtime_pb2.Update()
            upd.type = update_type
            upd.entity.table_entry.CopyFrom(entry)
            return client.write([upd])

        ok_insert, msg_insert = _write_update(p4runtime_pb2.Update.INSERT)
        if not ok_insert:
            ok_mod, msg_mod = _write_update(p4runtime_pb2.Update.MODIFY)
            if not ok_mod:
                # Mesmo falhando, faremos verificação por readback
                info["exec"]["stderr"] = "write failed"
                info["exec"]["detail"] = f"INSERT: {msg_insert} | MODIFY: {msg_mod}"
            else:
                info["exec"]["ok"] = True
        else:
            info["exec"]["ok"] = True

        # Readback + verificação do estado final (critério científico)
        rb_ent = p4runtime_pb2.Entity()
        rb_ent.table_entry.table_id = int(table.preamble.id)
        okr, msgr, resps = client.read([rb_ent])
        if not okr:
            info["readback_dump"] = f"[readback_failed] {msgr}"
            return bool(info["exec"]["ok"]), info

        # monta dump textual
        out_lines = [f"# Readback table={table_name} table_id={int(table.preamble.id)} responses={len(resps)}"]
        found_expected = False
        n_entries = 0
        for rr in resps:
            for e in rr.entities:
                if not e.HasField("table_entry"):
                    continue
                n_entries += 1
                te = e.table_entry
                if _entry_matches_expected(
                    te,
                    table_id=int(table.preamble.id),
                    field_id=int(mf_def.id),
                    dst_ip=dst_ip,
                    action_id=int(chosen["action_id"]),
                    dscp=int(dscp),
                ):
                    found_expected = True
                out_lines.append(text_format.MessageToString(te).strip())
                out_lines.append("---")
        out_lines.append(f"# entries={n_entries}")
        info["readback_dump"] = "\n".join(out_lines).strip() + "\n"

        if found_expected:
            # Se o estado final é consistente, consideramos aplicado.
            # Se houve erro no RPC, registramos a divergência (isso vira evidência científica).
            if not info["exec"]["ok"]:
                info["exec"]["write_error_but_verified"] = True
            info["exec"]["ok"] = True
            info["installed_rule"] = {
                "table": table_name,
                "table_id": int(table.preamble.id),
                "match": match_repr,
                "action": chosen["action_name"],
                "action_id": int(chosen["action_id"]),
                "new_dscp": int(dscp),
                "election_id": list(election_id),
            }
            return True, info

        # se não achou a regra esperada, mantém ok conforme RPC
        if info["exec"]["ok"]:
            info["installed_rule"] = {
                "table": table_name,
                "table_id": int(table.preamble.id),
                "match": match_repr,
                "action": chosen["action_name"],
                "action_id": int(chosen["action_id"]),
                "new_dscp": int(dscp),
                "election_id": list(election_id),
            }

        return bool(info["exec"]["ok"]), info

    except Exception as e:
        info["exec"]["stderr"] = "p4runtime operation failed"
        info["exec"]["detail"] = f"{type(e).__name__}: {e}"
        if info["readback_dump"] is None:
            info["readback_dump"] = f"[no_dump] exception before readback: {type(e).__name__}: {e}"
        return False, info

    finally:
        try:
            client.close()
        except Exception:
            pass
