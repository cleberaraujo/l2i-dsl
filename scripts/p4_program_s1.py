#!/usr/bin/env python3
"""Program and verify the S1 QoS rule in the current L2I P4 pipeline.

The pipeline must already be loaded in BMv2. This script discovers object IDs
from the generated P4Info, performs an INSERT-or-MODIFY operation, and validates
the resulting entry through P4Runtime readback.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path

# Allow direct execution as ``python scripts/p4_program_s1.py`` without
# requiring the repository to be installed as a Python package.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from google.protobuf import text_format
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2

from l2i.third_party.p4rt_min.client import P4RTClient


TABLE_NAME = "MyIngress.qos_table"
MATCH_NAME = "hdr.ipv4.dstAddr"
ACTION_NAME = "MyIngress.set_dscp"
PARAM_NAME = "new_dscp"


def fail(message: str) -> "NoReturn":
    print(f"P4_S1_FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_p4info(path: Path) -> p4info_pb2.P4Info:
    if not path.is_file():
        fail(f"P4Info not found: {path}")

    p4info = p4info_pb2.P4Info()
    text_format.Merge(path.read_text(encoding="utf-8"), p4info)
    return p4info


def encode_uint(value: int, bitwidth: int) -> bytes:
    if value < 0 or value >= (1 << bitwidth):
        fail(f"value {value} is outside bit<{bitwidth}> range")
    return value.to_bytes(max(1, (bitwidth + 7) // 8), "big")


def decode_uint(value: bytes) -> int:
    return int.from_bytes(value, "big")


def find_required_objects(p4info: p4info_pb2.P4Info):
    table = next(
        (item for item in p4info.tables if item.preamble.name == TABLE_NAME),
        None,
    )
    if table is None:
        fail(f"table not found: {TABLE_NAME}")

    match_def = next(
        (item for item in table.match_fields if item.name == MATCH_NAME),
        None,
    )
    if match_def is None:
        fail(f"match field not found: {MATCH_NAME}")

    action = next(
        (item for item in p4info.actions if item.preamble.name == ACTION_NAME),
        None,
    )
    if action is None:
        fail(f"action not found: {ACTION_NAME}")

    param = next(
        (item for item in action.params if item.name == PARAM_NAME),
        None,
    )
    if param is None:
        fail(f"action parameter not found: {PARAM_NAME}")

    try:
        match_type = p4info_pb2.MatchField.MatchType.Name(
            int(match_def.match_type)
        )
    except Exception:
        match_type = str(match_def.match_type)

    if match_type not in {"EXACT", "LPM", "TERNARY"}:
        fail(f"unsupported match type for {MATCH_NAME}: {match_type}")

    return table, match_def, action, param, match_type


def build_entry(
    *,
    table,
    match_def,
    action,
    param,
    match_type: str,
    dst_ip: str,
    dscp: int,
    include_action: bool,
) -> p4runtime_pb2.TableEntry:
    entry = p4runtime_pb2.TableEntry()
    entry.table_id = int(table.preamble.id)

    field = entry.match.add()
    field.field_id = int(match_def.id)
    ip_bytes = ipaddress.ip_address(dst_ip).packed

    if match_type == "EXACT":
        field.exact.value = ip_bytes
    elif match_type == "LPM":
        field.lpm.value = ip_bytes
        field.lpm.prefix_len = 32
    elif match_type == "TERNARY":
        field.ternary.value = ip_bytes
        field.ternary.mask = b"\xff\xff\xff\xff"

    if include_action:
        entry.action.action.action_id = int(action.preamble.id)
        action_param = entry.action.action.params.add()
        action_param.param_id = int(param.id)
        action_param.value = encode_uint(dscp, int(param.bitwidth))

    return entry


def table_entity(entry: p4runtime_pb2.TableEntry) -> p4runtime_pb2.Entity:
    entity = p4runtime_pb2.Entity()
    entity.table_entry.CopyFrom(entry)
    return entity


def write_upsert(client: P4RTClient, entity: p4runtime_pb2.Entity) -> None:
    update = p4runtime_pb2.Update()
    update.type = p4runtime_pb2.Update.INSERT
    update.entity.CopyFrom(entity)

    ok, message = client.write([update])
    mode = "INSERT"

    if not ok:
        update.type = p4runtime_pb2.Update.MODIFY
        ok, modify_message = client.write([update])
        mode = "MODIFY"
        message = f"insert={message}; modify={modify_message}"

    print(f"P4_S1_WRITE_OK={ok} mode={mode} message={message}")
    if not ok:
        fail("INSERT and MODIFY both failed")


def read_table(client: P4RTClient, table_id: int):
    query = p4runtime_pb2.Entity()
    query.table_entry.table_id = table_id
    ok, message, responses = client.read([query])
    print(f"P4_S1_READ_OK={ok} message={message}")
    if not ok:
        fail("table read failed")

    entries = []
    for response in responses:
        for entity in response.entities:
            if entity.HasField("table_entry"):
                entries.append(entity.table_entry)
    return entries


def entry_matches(
    entry,
    *,
    table,
    match_def,
    action,
    param,
    dst_ip: str,
    dscp: int,
) -> bool:
    if int(entry.table_id) != int(table.preamble.id):
        return False

    ip_bytes = ipaddress.ip_address(dst_ip).packed
    match_ok = False
    for field in entry.match:
        if int(field.field_id) != int(match_def.id):
            continue
        if field.HasField("exact") and field.exact.value == ip_bytes:
            match_ok = True
        elif (
            field.HasField("lpm")
            and field.lpm.value == ip_bytes
            and int(field.lpm.prefix_len) == 32
        ):
            match_ok = True
        elif (
            field.HasField("ternary")
            and field.ternary.value == ip_bytes
            and field.ternary.mask
        ):
            match_ok = True

    if not match_ok or not entry.action.HasField("action"):
        return False

    observed_action = entry.action.action
    if int(observed_action.action_id) != int(action.preamble.id):
        return False

    observed_param = next(
        (
            item
            for item in observed_action.params
            if int(item.param_id) == int(param.id)
        ),
        None,
    )
    return (
        observed_param is not None
        and decode_uint(observed_param.value) == dscp
    )


def delete_entry(client: P4RTClient, entry: p4runtime_pb2.TableEntry) -> None:
    update = p4runtime_pb2.Update()
    update.type = p4runtime_pb2.Update.DELETE
    update.entity.table_entry.CopyFrom(entry)
    ok, message = client.write([update])
    print(f"P4_S1_DELETE_OK={ok} message={message}")
    if not ok:
        fail("DELETE failed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Program and verify the current L2I S1 QoS table entry."
    )
    parser.add_argument("--addr", default="127.0.0.1:9559")
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--outdir", default="/tmp/l2i_minimal")
    parser.add_argument("--dst", default="10.0.0.3")
    parser.add_argument("--dscp", type=int, default=40)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete the programmed entry after readback validation",
    )
    parser.add_argument(
        "--src",
        default=None,
        help="deprecated compatibility option; not used by the current pipeline",
    )
    parser.add_argument(
        "--dport",
        type=int,
        default=None,
        help="deprecated compatibility option; not used by the current pipeline",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not 0 <= args.dscp <= 63:
        fail("DSCP must be in the range 0..63")

    p4info_path = Path(args.outdir) / "l2i_minimal.p4info.txtpb"
    p4info = load_p4info(p4info_path)
    table, match_def, action, param, match_type = find_required_objects(p4info)

    print(f"P4_S1_P4INFO_OK file={p4info_path}")
    print(
        f"P4_S1_INTERFACE_OK table={TABLE_NAME} match={MATCH_NAME} "
        f"type={match_type} action={ACTION_NAME} param={PARAM_NAME}"
    )

    entry = build_entry(
        table=table,
        match_def=match_def,
        action=action,
        param=param,
        match_type=match_type,
        dst_ip=args.dst,
        dscp=args.dscp,
        include_action=True,
    )
    delete_key = build_entry(
        table=table,
        match_def=match_def,
        action=action,
        param=param,
        match_type=match_type,
        dst_ip=args.dst,
        dscp=args.dscp,
        include_action=False,
    )

    client = P4RTClient(
        address=args.addr,
        device_id=args.device_id,
        election_id=(0, 501),
        timeout_s=args.timeout,
    )

    try:
        client.connect()
        arbitration = client.ensure_primary(wait_s=2.0)
        print(
            f"P4_S1_ARBITRATION_OK={arbitration.ok} "
            f"P4_S1_IS_PRIMARY={arbitration.is_primary}"
        )
        if not arbitration.ok or not arbitration.is_primary:
            fail("primary arbitration was not granted")

        write_upsert(client, table_entity(entry))
        entries = read_table(client, int(table.preamble.id))
        found = any(
            entry_matches(
                item,
                table=table,
                match_def=match_def,
                action=action,
                param=param,
                dst_ip=args.dst,
                dscp=args.dscp,
            )
            for item in entries
        )
        print(f"P4_S1_READBACK_OK={found}")
        if not found:
            fail("programmed QoS entry was not found in readback")

        if args.cleanup:
            delete_entry(client, delete_key)
            entries_after = read_table(client, int(table.preamble.id))
            still_present = any(
                entry_matches(
                    item,
                    table=table,
                    match_def=match_def,
                    action=action,
                    param=param,
                    dst_ip=args.dst,
                    dscp=args.dscp,
                )
                for item in entries_after
            )
            print(f"P4_S1_CLEAN_OK={not still_present}")
            if still_present:
                fail("QoS entry remained after DELETE")

        print("P4_S1_PROGRAM_FUNCTIONAL_OK")
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
