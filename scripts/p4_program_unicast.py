#!/usr/bin/env python3
"""Program and verify one ingress-to-egress unicast rule through P4Runtime.

The current minimal L2I pipeline forwards unicast traffic by matching the BMv2
``standard_metadata.ingress_port`` field. This helper is intentionally narrow:
it installs one exact ingress-port rule, confirms the action parameter through
readback, and can remove the same rule after the experiment.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NoReturn

# Allow direct execution from the repository without requiring installation as
# a Python package.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from google.protobuf import text_format
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2

from l2i.third_party.p4rt_min.client import P4RTClient


TABLE_NAME = "MyIngress.unicast_table"
MATCH_NAME = "ingress_port"
MATCH_NAME_CANDIDATES = {"standard_metadata.ingress_port", "stdmd.ingress_port"}
ACTION_NAME = "MyIngress.set_output_port"
PARAM_NAME = "port"


def fail(message: str) -> NoReturn:
    """Emit a stable failure marker and terminate the helper."""

    print(f"P4_UNICAST_FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_p4info(path: Path) -> p4info_pb2.P4Info:
    """Load the text-format P4Info generated for the running pipeline."""

    if not path.is_file():
        fail(f"P4Info not found: {path}")

    p4info = p4info_pb2.P4Info()
    text_format.Merge(path.read_text(encoding="utf-8"), p4info)
    return p4info


def encode_uint(value: int, bitwidth: int) -> bytes:
    """Encode an unsigned integer using the byte width expected by P4Runtime."""

    if value < 0 or value >= (1 << bitwidth):
        fail(f"value {value} is outside bit<{bitwidth}> range")
    return value.to_bytes(max(1, (bitwidth + 7) // 8), "big")


def decode_uint(value: bytes) -> int:
    """Decode a P4Runtime byte string as an unsigned integer."""

    return int.from_bytes(value, "big")


def find_required_objects(p4info: p4info_pb2.P4Info):
    """Resolve the exact table, match field, action, and action parameter."""

    table = next(
        (item for item in p4info.tables if item.preamble.name == TABLE_NAME),
        None,
    )
    if table is None:
        fail(f"table not found: {TABLE_NAME}")

    match_def = next(
        (
            item
            for item in table.match_fields
            if item.name in MATCH_NAME_CANDIDATES
            or item.name.endswith(".ingress_port")
            or item.name == MATCH_NAME
        ),
        None,
    )
    if match_def is None:
        observed = ",".join(item.name for item in table.match_fields)
        fail(
            f"ingress-port match field not found; observed fields: {observed}"
        )

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

    if match_type != "EXACT":
        fail(f"expected EXACT match for {MATCH_NAME}, got {match_type}")

    return table, match_def, action, param


def build_entry(
    *,
    table,
    match_def,
    action,
    param,
    ingress_port: int,
    egress_port: int,
    include_action: bool,
) -> p4runtime_pb2.TableEntry:
    """Build either the full rule or its action-free DELETE key."""

    entry = p4runtime_pb2.TableEntry()
    entry.table_id = int(table.preamble.id)

    field = entry.match.add()
    field.field_id = int(match_def.id)
    field.exact.value = encode_uint(ingress_port, int(match_def.bitwidth))

    if include_action:
        entry.action.action.action_id = int(action.preamble.id)
        action_param = entry.action.action.params.add()
        action_param.param_id = int(param.id)
        action_param.value = encode_uint(egress_port, int(param.bitwidth))

    return entry


def table_entity(entry: p4runtime_pb2.TableEntry) -> p4runtime_pb2.Entity:
    """Wrap a table entry in the P4Runtime Entity message."""

    entity = p4runtime_pb2.Entity()
    entity.table_entry.CopyFrom(entry)
    return entity


def write_upsert(client: P4RTClient, entity: p4runtime_pb2.Entity) -> None:
    """Use INSERT first and MODIFY as an idempotent fallback."""

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

    print(f"P4_UNICAST_WRITE_OK={ok} mode={mode} message={message}")
    if not ok:
        fail("INSERT and MODIFY both failed")


def write_delete(client: P4RTClient, entity: p4runtime_pb2.Entity) -> None:
    """Delete the exact unicast-table key."""

    update = p4runtime_pb2.Update()
    update.type = p4runtime_pb2.Update.DELETE
    update.entity.CopyFrom(entity)
    ok, message = client.write([update])
    print(f"P4_UNICAST_DELETE_OK={ok} message={message}")
    if not ok:
        fail("DELETE failed")


def read_table(client: P4RTClient, table_id: int):
    """Read every entry currently installed in the unicast table."""

    query = p4runtime_pb2.Entity()
    query.table_entry.table_id = table_id
    ok, message, responses = client.read([query])
    print(f"P4_UNICAST_READ_OK={ok} message={message}")
    if not ok:
        fail("unicast table read failed")

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
    ingress_port: int,
    egress_port: int,
) -> bool:
    """Return whether a readback entry matches the requested forwarding rule."""

    if int(entry.table_id) != int(table.preamble.id):
        return False

    observed_match = next(
        (
            field
            for field in entry.match
            if int(field.field_id) == int(match_def.id)
            and field.HasField("exact")
        ),
        None,
    )
    if observed_match is None:
        return False
    if decode_uint(observed_match.exact.value) != ingress_port:
        return False

    if not entry.action.HasField("action"):
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
        and decode_uint(observed_param.value) == egress_port
    )


def parse_args() -> argparse.Namespace:
    """Parse the small, explicit command-line interface."""

    parser = argparse.ArgumentParser(
        description="Program and verify one BMv2 ingress-to-egress rule."
    )
    parser.add_argument("--addr", default="127.0.0.1:9559")
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--outdir", default="/tmp/l2i_minimal")
    parser.add_argument("--ingress-port", type=int, required=True)
    parser.add_argument("--egress-port", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete the rule and confirm its absence",
    )
    return parser.parse_args()


def main() -> None:
    """Program, verify, and optionally remove the unicast forwarding rule."""

    args = parse_args()

    if not 0 <= args.ingress_port <= 0x1FF:
        fail("ingress port must fit the pipeline bit<9> field")
    if not 0 <= args.egress_port <= 0x1FF:
        fail("egress port must fit the pipeline bit<9> field")

    p4info_path = Path(args.outdir) / "l2i_minimal.p4info.txtpb"
    p4info = load_p4info(p4info_path)
    table, match_def, action, param = find_required_objects(p4info)

    print(f"P4_UNICAST_P4INFO_OK file={p4info_path}")
    print(
        f"P4_UNICAST_INTERFACE_OK table={TABLE_NAME} match={MATCH_NAME} "
        f"field={match_def.name} action={ACTION_NAME} param={PARAM_NAME}"
    )

    entry = build_entry(
        table=table,
        match_def=match_def,
        action=action,
        param=param,
        ingress_port=args.ingress_port,
        egress_port=args.egress_port,
        include_action=True,
    )
    delete_key = build_entry(
        table=table,
        match_def=match_def,
        action=action,
        param=param,
        ingress_port=args.ingress_port,
        egress_port=args.egress_port,
        include_action=False,
    )

    client = P4RTClient(
        address=args.addr,
        device_id=args.device_id,
        election_id=(0, 1401),
        timeout_s=args.timeout,
    )

    try:
        client.connect()
        arbitration = client.ensure_primary(wait_s=2.0)
        print(
            f"P4_UNICAST_ARBITRATION_OK={arbitration.ok} "
            f"P4_UNICAST_IS_PRIMARY={arbitration.is_primary}"
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
                ingress_port=args.ingress_port,
                egress_port=args.egress_port,
            )
            for item in entries
        )
        print(f"P4_UNICAST_READBACK_OK={found}")
        if not found:
            fail("programmed unicast entry was not found in readback")

        if args.cleanup:
            write_delete(client, table_entity(delete_key))
            entries_after = read_table(client, int(table.preamble.id))
            still_present = any(
                entry_matches(
                    item,
                    table=table,
                    match_def=match_def,
                    action=action,
                    param=param,
                    ingress_port=args.ingress_port,
                    egress_port=args.egress_port,
                )
                for item in entries_after
            )
            print(f"P4_UNICAST_CLEAN_OK={not still_present}")
            if still_present:
                fail("unicast entry remained after DELETE")

        print("P4_UNICAST_PROGRAM_OK")
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
