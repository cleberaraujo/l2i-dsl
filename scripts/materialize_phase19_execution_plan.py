#!/usr/bin/env python3
"""Materialize one deterministic Phase 19 execution plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from l2i.execution_plan import (
    execution_plan_sha256,
    materialize_execution_plan_v1,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"INVALID_MANIFEST_FILE: {path}: {exc}") from exc


def main() -> None:
    arguments = _arguments()
    plan = materialize_execution_plan_v1(_read_json(arguments.manifest))
    serialized = (
        json.dumps(plan, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\n"
    )
    output = arguments.output
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            created = True
            handle.write(serialized)
            handle.flush()
    except FileExistsError as exc:
        raise SystemExit(f"OUTPUT_ALREADY_EXISTS: {output}") from exc
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise

    print(f"PHASE19_EXECUTION_PLAN_OUTPUT={output}")
    print(f"PHASE19_EXECUTION_PLAN_ID={plan['execution_plan_id']}")
    print(
        "PHASE19_EXECUTION_PLAN_RANDOMIZATION_MANIFEST_SHA256="
        f"{plan['randomization_manifest_sha256']}"
    )
    print(f"PHASE19_EXECUTION_PLAN_SHA256={execution_plan_sha256(plan)}")
    print(f"PHASE19_EXECUTION_PLAN_BLOCK_COUNT={plan['total_block_count']}")
    print(f"PHASE19_EXECUTION_PLAN_RUN_SLOT_COUNT={plan['total_run_slot_count']}")
    print("PHASE19_EXECUTION_PLAN_MATERIALIZATION_OK")


if __name__ == "__main__":
    main()
