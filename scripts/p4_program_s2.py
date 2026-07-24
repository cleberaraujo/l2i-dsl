#!/usr/bin/env python3
"""Program and verify the S2 multicast state in the current L2I pipeline.

The pipeline must already be loaded in BMv2. The script creates the PRE
MulticastGroupEntry and the IPv4 multicast-table entry, validates both through
P4Runtime readback, and can remove the test state with ``--cleanup``.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path

# Allow direct execution as ``python scripts/p4_program_s2.py`` without
# requiring the repository to be installed as a Python package.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from google.protobuf import text_format
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2

from l2i.third_party.p4rt_min.client import P4RTClient


TABLE_NAME = "MyIngress.mcast_table"
MATCH_NAME = "hdr.ipv4.dstAddr"
ACTION_NAME = "MyIngress.set_mcast_group"
PARAM_NAME = "grp"


def fail(message: str) -> "NoReturn":
    print(f"P4_S2_FAILED: {message}", file=sys.stderr)
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

    if match_type != "LPM":
        fail(f"expected LPM match for {MATCH_NAME}, got {match_type}")

    return table, match_def, action, param


def table_entity(entry: p4runtime_pb2.TableEntry) -> p4runtime_pb2.Entity:
    entity = p4runtime_pb2.Entity()
    entity.table_entry.CopyFrom(entry)
    return entity


def build_mcast_entry(
    *,
    table,
    match_def,
    action,
    param,
    dst_ip: str,
    group_id: int,
    include_action: bool,
) -> p4runtime_pb2.TableEntry:
    entry = p4runtime_pb2.TableEntry()
    entry.table_id = int(table.preamble.id)

    field = entry.match.add()
    field.field_id = int(match_def.id)
    field.lpm.value = ipaddress.ip_address(dst_ip).packed
    field.lpm.prefix_len = 32

    if include_action:
        entry.action.action.action_id = int(action.preamble.id)
        action_param = entry.action.action.params.add()
        action_param.param_id = int(param.id)
        action_param.value = encode_uint(group_id, int(param.bitwidth))

    return entry


def build_pre_entity(
    group_id: int,
    ports: list[int],
    *,
    include_replicas: bool,
) -> p4runtime_pb2.Entity:
    entity = p4runtime_pb2.Entity()
    group = (
        entity.packet_replication_engine_entry.multicast_group_entry
    )
    group.multicast_group_id = group_id

    if include_replicas:
        for instance, port in enumerate(ports, start=1):
            replica = group.replicas.add()
            replica.egress_port = port
            replica.instance = instance

    return entity


def write_upsert(
    client: P4RTClient,
    entity: p4runtime_pb2.Entity,
    label: str,
) -> None:
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

    print(f"{label}_WRITE_OK={ok} mode={mode} message={message}")
    if not ok:
        fail(f"{label}: INSERT and MODIFY both failed")


def write_delete(
    client: P4RTClient,
    entity: p4runtime_pb2.Entity,
    label: str,
) -> None:
    update = p4runtime_pb2.Update()
    update.type = p4runtime_pb2.Update.DELETE
    update.entity.CopyFrom(entity)
    ok, message = client.write([update])
    print(f"{label}_DELETE_OK={ok} message={message}")
    if not ok:
        fail(f"{label}: DELETE failed")


def read_table(client: P4RTClient, table_id: int):
    query = p4runtime_pb2.Entity()
    query.table_entry.table_id = table_id
    ok, message, responses = client.read([query])
    print(f"P4_S2_TABLE_READ_OK={ok} message={message}")
    if not ok:
        fail("multicast table read failed")

    entries = []
    for response in responses:
        for entity in response.entities:
            if entity.HasField("table_entry"):
                entries.append(entity.table_entry)
    return entries


def read_pre_group(client: P4RTClient, group_id: int):
    query = p4runtime_pb2.Entity()
    (
        query.packet_replication_engine_entry.multicast_group_entry.multicast_group_id
    ) = group_id

    ok, message, responses = client.read([query])
    if not ok:
        if (
            "NOT_FOUND" in message
            or "Multicast group does not exist" in message
        ):
            print("P4_S2_PRE_READ_RESULT=NOT_FOUND")
            return []
        fail(f"PRE read failed: {message}")

    print("P4_S2_PRE_READ_RESULT=RPC_OK")
    groups = []
    for response in responses:
        for entity in response.entities:
            if not entity.HasField("packet_replication_engine_entry"):
                continue
            pre = entity.packet_replication_engine_entry
            if pre.HasField("multicast_group_entry"):
                groups.append(pre.multicast_group_entry)
    return groups


def mcast_entry_matches(
    entry,
    *,
    table,
    match_def,
    action,
    param,
    dst_ip: str,
    group_id: int,
) -> bool:
    if int(entry.table_id) != int(table.preamble.id):
        return False

    expected_ip = ipaddress.ip_address(dst_ip).packed
    match_ok = any(
        int(field.field_id) == int(match_def.id)
        and field.HasField("lpm")
        and field.lpm.value == expected_ip
        and int(field.lpm.prefix_len) == 32
        for field in entry.match
    )
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
        and decode_uint(observed_param.value) == group_id
    )


def replica_port(replica) -> int:
    if replica.port:
        return decode_uint(replica.port)
    return int(replica.egress_port)


def pre_group_matches(group, group_id: int, ports: list[int]) -> bool:
    if int(group.multicast_group_id) != group_id:
        return False
    observed_ports = {replica_port(replica) for replica in group.replicas}
    return observed_ports == set(ports)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Program and verify the current L2I S2 multicast state."
    )
    parser.add_argument("--addr", default="127.0.0.1:9559")
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--outdir", default="/tmp/l2i_minimal")
    parser.add_argument("--mgrp", type=int, default=1)
    parser.add_argument("--dst-mcast", default="239.1.1.1")
    parser.add_argument("--ports", type=int, nargs="+", default=[0, 1])
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete the table entry and PRE group after validation",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not 1 <= args.mgrp <= 0xFFFF:
        fail("multicast group must be in the range 1..65535")
    if not args.ports:
        fail("at least one egress port is required")
    if any(port < 0 or port > 0xFFFFFFFF for port in args.ports):
        fail("egress ports must be uint32 values")

    p4info_path = Path(args.outdir) / "l2i_minimal.p4info.txtpb"
    p4info = load_p4info(p4info_path)
    table, match_def, action, param = find_required_objects(p4info)

    print(f"P4_S2_P4INFO_OK file={p4info_path}")
    print(
        f"P4_S2_INTERFACE_OK table={TABLE_NAME} match={MATCH_NAME} "
        f"action={ACTION_NAME} param={PARAM_NAME}"
    )

    table_entry = build_mcast_entry(
        table=table,
        match_def=match_def,
        action=action,
        param=param,
        dst_ip=args.dst_mcast,
        group_id=args.mgrp,
        include_action=True,
    )
    table_delete_key = build_mcast_entry(
        table=table,
        match_def=match_def,
        action=action,
        param=param,
        dst_ip=args.dst_mcast,
        group_id=args.mgrp,
        include_action=False,
    )
    pre_entity = build_pre_entity(
        args.mgrp,
        args.ports,
        include_replicas=True,
    )
    pre_delete_key = build_pre_entity(
        args.mgrp,
        args.ports,
        include_replicas=False,
    )

    client = P4RTClient(
        address=args.addr,
        device_id=args.device_id,
        election_id=(0, 502),
        timeout_s=args.timeout,
    )

    try:
        client.connect()
        arbitration = client.ensure_primary(wait_s=2.0)
        print(
            f"P4_S2_ARBITRATION_OK={arbitration.ok} "
            f"P4_S2_IS_PRIMARY={arbitration.is_primary}"
        )
        if not arbitration.ok or not arbitration.is_primary:
            fail("primary arbitration was not granted")

        write_upsert(client, pre_entity, "P4_S2_PRE")
        write_upsert(client, table_entity(table_entry), "P4_S2_TABLE")

        groups = read_pre_group(client, args.mgrp)
        entries = read_table(client, int(table.preamble.id))

        pre_found = any(
            pre_group_matches(group, args.mgrp, args.ports)
            for group in groups
        )
        table_found = any(
            mcast_entry_matches(
                entry,
                table=table,
                match_def=match_def,
                action=action,
                param=param,
                dst_ip=args.dst_mcast,
                group_id=args.mgrp,
            )
            for entry in entries
        )

        print(f"P4_S2_PRE_READBACK_OK={pre_found}")
        print(f"P4_S2_TABLE_READBACK_OK={table_found}")
        if not pre_found or not table_found:
            fail("multicast state was not confirmed by readback")

        if args.cleanup:
            write_delete(
                client,
                table_entity(table_delete_key),
                "P4_S2_TABLE",
            )
            write_delete(client, pre_delete_key, "P4_S2_PRE")

            entries_after = read_table(client, int(table.preamble.id))
            groups_after = read_pre_group(client, args.mgrp)

            table_still_present = any(
                mcast_entry_matches(
                    entry,
                    table=table,
                    match_def=match_def,
                    action=action,
                    param=param,
                    dst_ip=args.dst_mcast,
                    group_id=args.mgrp,
                )
                for entry in entries_after
            )
            pre_still_present = any(
                int(group.multicast_group_id) == args.mgrp
                for group in groups_after
            )

            print(f"P4_S2_TABLE_CLEAN_OK={not table_still_present}")
            print(f"P4_S2_PRE_CLEAN_OK={not pre_still_present}")
            if table_still_present or pre_still_present:
                fail("multicast state remained after DELETE")

        print("P4_S2_PROGRAM_FUNCTIONAL_OK")
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
