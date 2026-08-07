#!/usr/bin/env python3
"""Validate Phase 19 deterministic execution-plan fixtures."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from l2i.execution_plan import (
    EXECUTION_PLAN_ID_NAMESPACE,
    RUN_SLOT_ID_NAMESPACE,
    materialize_execution_plan_v1,
    validate_execution_plan_v1,
)
from l2i.experiment_contract import (
    ExperimentContractError,
    canonical_json_bytes,
    sha256_json,
)


FIXTURE_CONTRACT = "phase19-execution-plan-v1-fixture-v1"
VALID_FIXTURE_COUNT = 5
INVALID_FIXTURE_COUNT = 39
VALID_NAMES = {
    "valid-foundation-rq3-mock.json",
    "valid-pilot-rq3-real.json",
    "valid-pilot-rq4-mock-multiconfiguration.json",
    "valid-confirmatory-rq4-real.json",
    "valid-synthetic-confirmatory-rq4-real-50-blocks.json",
}
INVALID_EXPECTED = {
    "invalid-self-hash.json": "PLAN_SELF_HASH_FORBIDDEN",
    "invalid-execution-id.json": "EXECUTION_STATE_FORBIDDEN",
    "invalid-incidental-substring.json": "UNKNOWN_PROPERTY",
    "invalid-contract-version.json": "UNSUPPORTED_EXECUTION_PLAN_VERSION",
    "invalid-missing-run-slots-field.json": "MISSING_REQUIRED_FIELD",
    "invalid-slot-index-type.json": "INVALID_PLAN_STRUCTURE",
    "invalid-embedded-manifest-missing-required-field.json": "INVALID_EMBEDDED_RANDOMIZATION_MANIFEST",
    "invalid-embedded-manifest-noncanonical-block-sequence.json": "INVALID_EMBEDDED_RANDOMIZATION_MANIFEST",
    "invalid-manifest-hash.json": "RANDOMIZATION_MANIFEST_SHA256_MISMATCH",
    "invalid-randomization-manifest-id.json": "PLAN_MANIFEST_FIELD_MISMATCH",
    "invalid-root-repository-commit.json": "PLAN_MANIFEST_FIELD_MISMATCH",
    "invalid-root-campaign-id.json": "PLAN_MANIFEST_FIELD_MISMATCH",
    "invalid-root-campaign-stage.json": "PLAN_MANIFEST_FIELD_MISMATCH",
    "invalid-root-rq-id.json": "PLAN_MANIFEST_FIELD_MISMATCH",
    "invalid-root-scenario-id.json": "PLAN_MANIFEST_FIELD_MISMATCH",
    "invalid-root-backend-mode.json": "PLAN_MANIFEST_FIELD_MISMATCH",
    "invalid-execution-plan-id.json": "EXECUTION_PLAN_ID_MISMATCH",
    "invalid-expansion-algorithm.json": "UNSUPPORTED_EXPANSION_ALGORITHM",
    "invalid-periods-per-block.json": "INVALID_PERIODS_PER_BLOCK",
    "invalid-total-block-count.json": "TOTAL_BLOCK_COUNT_MISMATCH",
    "invalid-total-run-slot-count.json": "TOTAL_RUN_SLOT_COUNT_MISMATCH",
    "invalid-duplicate-run-slot-id.json": "DUPLICATE_RUN_SLOT_ID",
    "invalid-duplicate-slot-index.json": "DUPLICATE_SLOT_INDEX",
    "invalid-duplicate-period-semantic-key.json": "DUPLICATE_SEMANTIC_SLOT",
    "invalid-noncontiguous-slot-index.json": "NONCONTIGUOUS_SLOT_INDEX",
    "invalid-missing-run-slot.json": "MISSING_RUN_SLOT",
    "invalid-additional-run-slot.json": "ADDITIONAL_RUN_SLOT",
    "invalid-semantic-slot-substitution.json": "MISSING_RUN_SLOT",
    "invalid-execution-requirements-scenario.json": "EXECUTION_REQUIREMENTS_MISMATCH",
    "invalid-execution-requirements-backend.json": "EXECUTION_REQUIREMENTS_MISMATCH",
    "invalid-configuration-profile.json": "PROFILE_ASSIGNMENT_MISMATCH",
    "invalid-block-sequence-index.json": "BLOCK_SEQUENCE_INDEX_MISMATCH",
    "invalid-assignment-block-index.json": "SLOT_ASSIGNMENT_MISMATCH",
    "invalid-assignment-manifest-hash.json": "SLOT_ASSIGNMENT_MISMATCH",
    "invalid-arm-treatment.json": "ARM_TREATMENT_MISMATCH",
    "invalid-order-period-arm.json": "ORDER_PERIOD_ARM_MISMATCH",
    "invalid-slot-index-assignment.json": "SLOT_INDEX_ASSIGNMENT_MISMATCH",
    "invalid-run-slot-id.json": "RUN_SLOT_ID_MISMATCH",
    "invalid-noncanonical-run-slot-sequence.json": "NONCANONICAL_RUN_SLOT_SEQUENCE",
}
CAUSE_EXPECTED = {
    "invalid-embedded-manifest-missing-required-field.json": "MISSING_REQUIRED_FIELD",
    "invalid-embedded-manifest-noncanonical-block-sequence.json": "NONCANONICAL_BLOCK_SEQUENCE",
}


def _require(name: str, condition: bool) -> None:
    print(f"PHASE19_EXECUTION_PLAN_CHECK_{name}={condition}")
    if not condition:
        raise SystemExit(1)


def _code(message: str) -> str | None:
    match = re.match(r"^([A-Z][A-Z0-9_]*): ", message)
    return match.group(1) if match else None


def _cause_code(message: str) -> str | None:
    match = re.match(
        r"^INVALID_EMBEDDED_RANDOMIZATION_MANIFEST: ([A-Z][A-Z0-9_]*): ",
        message,
    )
    return match.group(1) if match else None


def _independent_plan_id(manifest_hash: str) -> str:
    return "execution-plan-" + hashlib.sha256(
        EXECUTION_PLAN_ID_NAMESPACE.encode("ascii")
        + b"\x00"
        + manifest_hash.encode("ascii")
    ).hexdigest()


def _independent_slot_id(manifest_hash: str, block_id: str, period: int) -> str:
    return "run-slot-" + hashlib.sha256(
        RUN_SLOT_ID_NAMESPACE.encode("ascii")
        + b"\x00"
        + canonical_json_bytes(
            {
                "randomization_manifest_sha256": manifest_hash,
                "block_id": block_id,
                "period": period,
            }
        )
    ).hexdigest()


def _valid_plan_checks(plan: dict[str, Any]) -> None:
    validate_execution_plan_v1(plan)
    rebuilt = materialize_execution_plan_v1(plan["randomization_manifest"])
    _require("VALID_RECONSTRUCTION", canonical_json_bytes(rebuilt) == canonical_json_bytes(plan))
    manifest_hash = sha256_json(plan["randomization_manifest"])
    _require(
        "SCENARIO_SPEC_PROPAGATED",
        sum(
            source["role"] == "scenario_spec"
            for configuration in plan["randomization_manifest"]["configurations"]
            for source in configuration["source_artifacts"]
        )
        == 1,
    )
    _require("INDEPENDENT_MANIFEST_SHA256", plan["randomization_manifest_sha256"] == manifest_hash)
    _require("INDEPENDENT_EXECUTION_PLAN_ID", plan["execution_plan_id"] == _independent_plan_id(manifest_hash))
    _require(
        "INDEPENDENT_RUN_SLOT_IDS",
        all(
            slot["run_slot_id"]
            == _independent_slot_id(
                manifest_hash,
                slot["assignment"]["block_id"],
                slot["assignment"]["period"],
            )
            for slot in plan["run_slots"]
        ),
    )
    _require(
        "CANONICAL_SLOT_INDEX",
        all(slot["slot_index"] == position for position, slot in enumerate(plan["run_slots"], start=1)),
    )


def _exercise_source_role_binding(fixtures_path: Path) -> None:
    fixture = json.loads(
        (fixtures_path / "valid-pilot-rq4-mock-multiconfiguration.json").read_text(
            encoding="utf-8"
        )
    )
    original = fixture["plan"]
    changed_manifest = copy.deepcopy(original["randomization_manifest"])
    sources = next(
        configuration["source_artifacts"]
        for configuration in changed_manifest["configurations"]
        if len(configuration["source_artifacts"]) > 1
    )
    scenario_spec = next(source for source in sources if source["role"] == "scenario_spec")
    auxiliary = next(source for source in sources if source["role"] != "scenario_spec")
    scenario_spec["role"], auxiliary["role"] = auxiliary["role"], scenario_spec["role"]

    rebuilt = materialize_execution_plan_v1(changed_manifest)
    _require(
        "SOURCE_ROLE_COPIED_WITHOUT_REINTERPRETATION",
        rebuilt["randomization_manifest"]["configurations"]
        == changed_manifest["configurations"],
    )
    _require(
        "SOURCE_ROLE_PARTICIPATES_IN_PLAN_HASH",
        sha256_json(rebuilt) != sha256_json(original),
    )

    mismatched = copy.deepcopy(original)
    mismatched["randomization_manifest"] = changed_manifest
    try:
        validate_execution_plan_v1(mismatched)
    except ExperimentContractError as exc:
        observed = _code(str(exc))
    else:
        observed = None
    _require(
        "SOURCE_ROLE_DIVERGENCE_REJECTED",
        observed == "RANDOMIZATION_MANIFEST_SHA256_MISMATCH",
    )


def validate_fixtures() -> None:
    from jsonschema import Draft202012Validator

    repository = Path(__file__).resolve().parents[1]
    schema_path = repository / "schemas/phase19/execution-plan-v1.schema.json"
    fixtures_path = repository / "schemas/phase19/fixtures/execution-plan-v1"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    _require("SCHEMA_DRAFT_2020_12_VALID", True)

    paths = sorted(fixtures_path.glob("*.json"))
    names = {path.name for path in paths}
    _require("FIXTURE_NAME_SET", names == VALID_NAMES.union(INVALID_EXPECTED))
    valid_count = 0
    invalid_count = 0
    for path in paths:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        allowed = {"fixture_contract", "valid", "plan", "expected_error", "expected_cause"}
        if set(fixture).difference(allowed):
            raise SystemExit(f"{path}: unknown fixture envelope properties")
        if fixture.get("fixture_contract") != FIXTURE_CONTRACT:
            raise SystemExit(f"{path}: unsupported fixture_contract")
        plan = fixture.get("plan")
        if not isinstance(plan, dict):
            raise SystemExit(f"{path}: plan must be an object")
        schema_errors = list(validator.iter_errors(plan))
        before = canonical_json_bytes(plan)
        if fixture.get("valid") is True:
            if path.name not in VALID_NAMES or schema_errors:
                raise SystemExit(f"{path}: valid fixture failed schema or name contract")
            _valid_plan_checks(plan)
            observed = "ACCEPTED"
            valid_count += 1
        elif fixture.get("valid") is False:
            expected = INVALID_EXPECTED.get(path.name)
            if fixture.get("expected_error") != expected:
                raise SystemExit(f"{path}: expected_error envelope mismatch")
            try:
                validate_execution_plan_v1(plan)
            except ExperimentContractError as exc:
                message = str(exc)
                observed = _code(message)
                if observed != expected:
                    raise SystemExit(
                        f"{path}: expected {expected}, received {observed!r} from {message}"
                    ) from exc
                expected_cause = CAUSE_EXPECTED.get(path.name)
                if expected_cause is not None:
                    if fixture.get("expected_cause") != expected_cause or _cause_code(message) != expected_cause:
                        raise SystemExit(f"{path}: embedded manifest cause mismatch")
            else:
                raise SystemExit(f"{path}: invalid fixture was accepted")
            invalid_count += 1
        else:
            raise SystemExit(f"{path}: valid must be boolean")
        _require("NO_SILENT_NORMALIZATION", canonical_json_bytes(plan) == before)
        print(f"PHASE19_EXECUTION_PLAN_FIXTURE={path.name}")
        print(f"PHASE19_EXECUTION_PLAN_FIXTURE_SCHEMA_REJECTED={bool(schema_errors)}")
        print(f"PHASE19_EXECUTION_PLAN_FIXTURE_OBSERVED={observed}")
        print("PHASE19_EXECUTION_PLAN_FIXTURE_RESULT=PASS")

    _require("VALID_FIXTURE_COUNT", valid_count == VALID_FIXTURE_COUNT)
    _require("INVALID_FIXTURE_COUNT", invalid_count == INVALID_FIXTURE_COUNT)
    _require("TOTAL_FIXTURE_COUNT", len(paths) == VALID_FIXTURE_COUNT + INVALID_FIXTURE_COUNT)
    _exercise_source_role_binding(fixtures_path)

    synthetic_path = fixtures_path / "valid-synthetic-confirmatory-rq4-real-50-blocks.json"
    synthetic = json.loads(synthetic_path.read_text(encoding="utf-8"))["plan"]
    manifest = synthetic["randomization_manifest"]
    orders = Counter(block["order"] for block in manifest["blocks"])
    arms = Counter(slot["assignment"]["arm"] for slot in synthetic["run_slots"])
    _require("SYNTHETIC_BLOCK_COUNT", manifest["total_block_count"] == 50)
    _require("SYNTHETIC_AB_COUNT", orders["AB"] == 25)
    _require("SYNTHETIC_BA_COUNT", orders["BA"] == 25)
    _require("SYNTHETIC_RUN_SLOT_COUNT", synthetic["total_run_slot_count"] == 100)
    _require("SYNTHETIC_ARM_A_COUNT", arms["A"] == 50)
    _require("SYNTHETIC_ARM_B_COUNT", arms["B"] == 50)
    _require("SYNTHETIC_BACKEND_REAL", synthetic["backend_mode"] == "real")
    _require(
        "EMBEDDED_MANIFEST_SCHEMA_BOUNDARY",
        not list(
            validator.iter_errors(
                json.loads(
                    (fixtures_path / "invalid-embedded-manifest-missing-required-field.json").read_text(encoding="utf-8")
                )["plan"]
            )
        ),
    )
    _require(
        "INCIDENTAL_SUBSTRING_PREFIX_ONLY",
        _code("WRAPPER_ERROR: incidental EXECUTION_STATE_FORBIDDEN: substring")
        == "WRAPPER_ERROR",
    )
    print("PHASE19_EXECUTION_PLAN_FIXTURES_OK")


if __name__ == "__main__":
    if sys.argv[1:] == ["--validate-fixtures"]:
        validate_fixtures()
    else:
        raise SystemExit("usage: validate_phase19_execution_plan.py --validate-fixtures")
