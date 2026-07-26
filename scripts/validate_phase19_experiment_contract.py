#!/usr/bin/env python3
"""Validate the Phase 19 canonical experiment persistence contract."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
from pathlib import Path

from l2i.experiment_contract import (
    ExperimentContractError,
    ExperimentIdentity,
    ExperimentRunDirectory,
    RepositoryProvenance,
    atomic_write_json,
    build_run_manifest,
    generate_execution_id,
    sha256_json,
)


def require(name: str, condition: bool) -> None:
    """Emit one stable marker and stop on a failed contract property."""

    print(f"PHASE19_CONTRACT_CHECK_{name}={condition}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    """Exercise identity, collision, hashing, provenance, and atomic output."""

    repository = Path(__file__).resolve().parents[1]
    provenance = RepositoryProvenance.capture(repository)

    require("GIT_COMMIT_FORMAT", len(provenance.commit) == 40)
    require("GIT_BRANCH_DEVELOP", provenance.branch == "develop")
    require(
        "GIT_ORIGIN_SYNCHRONIZED",
        provenance.origin_commit == provenance.commit,
    )
    require("GIT_WORKTREE_CLEAN", provenance.worktree_clean)

    first_hash = sha256_json({"b": 2, "a": 1})
    second_hash = sha256_json({"a": 1, "b": 2})
    require("CANONICAL_JSON_HASH", first_hash == second_hash)

    execution_id = generate_execution_id("S1")
    identity = ExperimentIdentity(
        scenario_id="S1",
        execution_id=execution_id,
        profile_id="phase19-contract-self-test-v1",
        mode="baseline",
        backend_mode="mock",
        repetition=1,
    )

    with tempfile.TemporaryDirectory(prefix="phase19-contract-") as temporary:
        root = Path(temporary)
        specification = root / "spec.json"
        specification.write_text('{"requirements": {}}\n', encoding="utf-8")

        run_directory = ExperimentRunDirectory.create(root / "runs", identity)
        require("RUN_DIRECTORY_CREATED", run_directory.path.is_dir())

        collision_rejected = False
        try:
            ExperimentRunDirectory.create(root / "runs", identity)
        except ExperimentContractError:
            collision_rejected = True
        require("COLLISION_REJECTED", collision_rejected)

        escape_rejected = False
        try:
            run_directory.artifact("../escape.json")
        except ExperimentContractError:
            escape_rejected = True
        require("PATH_ESCAPE_REJECTED", escape_rejected)

        manifest = build_run_manifest(
            identity=identity,
            provenance=provenance,
            specification_path=specification,
            configuration={"duration_s": 1, "offered_load_mbps": 2.0},
            started_at=dt.datetime(2026, 7, 26, tzinfo=dt.UTC),
        )
        manifest_path = run_directory.artifact("manifest.json")
        atomic_write_json(manifest_path, manifest)
        persisted = json.loads(manifest_path.read_text(encoding="utf-8"))

        require("ATOMIC_JSON_PRESENT", manifest_path.is_file())
        require(
            "IDENTITY_ROUND_TRIP",
            persisted.get("identity", {}).get("execution_id") == execution_id,
        )
        require(
            "SPECIFICATION_HASH_PRESENT",
            len(persisted.get("specification", {}).get("sha256", "")) == 64,
        )
        require(
            "CONFIGURATION_HASH_PRESENT",
            len(persisted.get("configuration_sha256", "")) == 64,
        )

    print("PHASE19_EXPERIMENT_CONTRACT_VALIDATION_OK")


if __name__ == "__main__":
    main()
