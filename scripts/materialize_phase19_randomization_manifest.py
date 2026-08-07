#!/usr/bin/env python3
"""Materialize one Phase 19 deterministic randomization manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from l2i.experiment_plan import (
    materialize_randomization_manifest_v1,
    randomization_manifest_sha256,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _read_request(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"INVALID_REQUEST_FILE: {path}: {exc}") from exc


def main() -> None:
    arguments = _arguments()
    manifest = materialize_randomization_manifest_v1(
        _read_request(arguments.request)
    )
    output = arguments.output
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    try:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
    except FileExistsError as exc:
        raise SystemExit(f"OUTPUT_ALREADY_EXISTS: {output}") from exc

    print(f"PHASE19_RANDOMIZATION_MANIFEST_OUTPUT={output}")
    print(
        "PHASE19_RANDOMIZATION_MANIFEST_SHA256="
        f"{randomization_manifest_sha256(manifest)}"
    )
    print(
        "PHASE19_RANDOMIZATION_MANIFEST_CONFIGURATION_COUNT="
        f"{len(manifest['configurations'])}"
    )
    print(
        "PHASE19_RANDOMIZATION_MANIFEST_BLOCK_COUNT="
        f"{manifest['total_block_count']}"
    )
    print("PHASE19_RANDOMIZATION_MANIFEST_MATERIALIZATION_OK")


if __name__ == "__main__":
    main()
