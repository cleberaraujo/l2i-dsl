#!/usr/bin/env python3
"""Materialize and describe one Phase 19 paired unit without executing it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from l2i.runtime_adapter import dry_run_description, materialize_paired_unit


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--block-id", required=True)
    parser.add_argument("--repetition", required=True, type=int)
    parser.add_argument("--runtime-parameters", required=True, type=Path)
    parser.add_argument(
        "--dry-run",
        required=True,
        action="store_true",
        help="Required structural-only mode; execution is not enabled in this increment.",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    plan = _read_json(arguments.plan)
    unit = materialize_paired_unit(
        plan,
        block_id=arguments.block_id,
        repetition=arguments.repetition,
        runtime_parameters=_read_json(arguments.runtime_parameters),
    )
    description = dry_run_description(unit, plan)
    print(json.dumps({"unit": unit, "dispatch": description}, sort_keys=True, indent=2))
    print("PHASE19_RUNTIME_ADAPTER_DRY_RUN=True")
    print("PHASE19_RUNTIME_ADAPTER_EXECUTION_ENABLED=False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
