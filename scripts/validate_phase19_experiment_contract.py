#!/usr/bin/env python3
"""Validate the Phase 19 canonical experiment persistence contract."""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import tempfile
from pathlib import Path

from l2i.experiment_contract import (
    ExperimentAssignmentV2,
    ExperimentContractError,
    ExperimentIdentity,
    ExperimentIdentityV2,
    ExperimentRunDirectory,
    RepositoryProvenance,
    atomic_write_json,
    build_run_manifest,
    generate_execution_id,
    sha256_json,
    validate_experiment_v2,
    validate_paired_block_v2,
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


def _v2_require(name: str, condition: bool) -> None:
    """Emit a stable v2 fixture marker and stop when it is false."""

    print(f"PHASE19_CONTRACT_V2_CHECK_{name}={condition}")
    if not condition:
        raise SystemExit(1)


def _fixture_marker(path: Path) -> str:
    """Return a deterministic marker component for one fixture filename."""

    return re.sub(r"[^A-Z0-9]+", "_", path.stem.upper()).strip("_")


def _stable_error_code(message: str) -> str | None:
    """Extract a stable error code only from the start of a contract message."""

    match = re.match(r"^([A-Z][A-Z0-9_]*): ", message)
    return match.group(1) if match else None


def _exercise_v2_fixture(
    fixture: dict[str, object],
    assignment_validator: object,
    assignment_required_fields: set[str],
) -> None:
    """Exercise schema, Python objects, combined gates, and paired gates."""

    identity_payload = fixture.get("identity")
    assignment_payloads = fixture.get("assignments")
    if not isinstance(identity_payload, dict) or not isinstance(
        assignment_payloads, list
    ):
        raise ExperimentContractError(
            "INVALID_FIXTURE_ENVELOPE: identity and assignments are required"
        )

    schema_errors: list[object] = []
    for payload in assignment_payloads:
        if not isinstance(payload, dict):
            raise ExperimentContractError(
                "INVALID_FIXTURE_ENVELOPE: assignments must be objects"
            )
        schema_errors.extend(assignment_validator.iter_errors(payload))

    expected_error = fixture.get("expected_error")
    schema_error_codes = {
        "ARM_TREATMENT_MISMATCH",
        "ORDER_PERIOD_ARM_MISMATCH",
        "MISSING_REQUIRED_FIELD",
        "INVALID_RANDOMIZATION_MANIFEST_SHA256",
    }
    if fixture.get("valid") is True:
        if schema_errors:
            raise ExperimentContractError(
                f"UNEXPECTED_SCHEMA_REJECTION: {schema_errors[0].message}"
            )
    elif expected_error in schema_error_codes and not schema_errors:
        raise ExperimentContractError(
            "EXPECTED_SCHEMA_REJECTION_MISSING: invalid assignment passed schema"
        )
    elif expected_error not in schema_error_codes and schema_errors:
        raise ExperimentContractError(
            f"UNEXPECTED_SCHEMA_REJECTION: {schema_errors[0].message}"
        )

    identity = ExperimentIdentityV2(**identity_payload)
    assignments: list[ExperimentAssignmentV2] = []
    for payload in assignment_payloads:
        missing = assignment_required_fields.difference(payload)
        if missing:
            raise ExperimentContractError(
                "MISSING_REQUIRED_FIELD: missing assignment fields: "
                + ", ".join(sorted(missing))
            )
        assignment = ExperimentAssignmentV2(**payload)
        validate_experiment_v2(identity, assignment)
        assignments.append(assignment)
    validate_paired_block_v2(assignments)


def validate_v2_fixtures() -> None:
    """Validate the Draft 2020-12 schema and every assignment v2 fixture."""

    from jsonschema import Draft202012Validator

    repository = Path(__file__).resolve().parents[1]
    schema_path = (
        repository
        / "schemas"
        / "phase19"
        / "experiment-assignment-v2.schema.json"
    )
    fixtures_path = (
        repository / "schemas" / "phase19" / "fixtures" / "assignment-v2"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    _v2_require("SCHEMA_DRAFT_2020_12_VALID", True)
    validator = Draft202012Validator(schema)
    required_fields = set(schema["required"])

    fixture_paths = sorted(fixtures_path.glob("*.json"))
    _v2_require("FIXTURES_PRESENT", bool(fixture_paths))
    valid_count = 0
    invalid_count = 0
    for fixture_path in fixture_paths:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        declared_valid = fixture.get("valid")
        if declared_valid is True:
            _exercise_v2_fixture(fixture, validator, required_fields)
            valid_count += 1
        elif declared_valid is False:
            expected_error = fixture.get("expected_error")
            if not isinstance(expected_error, str) or not expected_error:
                raise SystemExit(
                    f"{fixture_path}: invalid fixture lacks expected_error"
                )
            try:
                _exercise_v2_fixture(fixture, validator, required_fields)
            except ExperimentContractError as exc:
                actual_error_code = _stable_error_code(str(exc))
                if actual_error_code != expected_error:
                    raise SystemExit(
                        f"{fixture_path}: expected {expected_error}, received "
                        f"{actual_error_code!r} from {exc}"
                    ) from exc
            else:
                raise SystemExit(
                    f"{fixture_path}: invalid fixture was unexpectedly accepted"
                )
            invalid_count += 1
        else:
            raise SystemExit(f"{fixture_path}: fixture valid flag must be boolean")
        _v2_require(f"FIXTURE_{_fixture_marker(fixture_path)}", True)

    _v2_require("VALID_FIXTURE_COUNT", valid_count == 7)
    _v2_require("INVALID_FIXTURE_COUNT", invalid_count == 9)
    print("PHASE19_EXPERIMENT_CONTRACT_V2_FIXTURES_OK")


if __name__ == "__main__":
    if sys.argv[1:] == ["--validate-v2-fixtures"]:
        validate_v2_fixtures()
    elif not sys.argv[1:]:
        main()
    else:
        raise SystemExit(
            "usage: validate_phase19_experiment_contract.py "
            "[--validate-v2-fixtures]"
        )
