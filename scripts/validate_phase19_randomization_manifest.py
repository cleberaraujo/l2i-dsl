#!/usr/bin/env python3
"""Validate the Phase 19 deterministic randomization-manifest fixtures."""

from __future__ import annotations

import copy
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from l2i.experiment_contract import (
    ExperimentContractError,
    canonical_json_bytes,
    sha256_json,
)
from l2i.experiment_plan import (
    REQUEST_VERSION,
    materialize_randomization_manifest_v1,
    randomization_manifest_sha256,
    validate_randomization_manifest_v1,
)


FIXTURE_CONTRACT = "phase19-randomization-manifest-v1-fixture-v1"
VALID_FIXTURE_COUNT = 4
INVALID_FIXTURE_COUNT = 17
_SEED_A = "00" * 32
_SEED_B = "ff" * 32


def _require(name: str, condition: bool) -> None:
    print(f"PHASE19_RANDOMIZATION_CHECK_{name}={condition}")
    if not condition:
        raise SystemExit(1)


def _marker(path: Path) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", path.stem.upper()).strip("_")


def _stable_error_code(message: str) -> str | None:
    """Extract a contract code exclusively from the message prefix."""

    match = re.match(r"^([A-Z][A-Z0-9_]*): ", message)
    return match.group(1) if match else None


def _request_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_version": REQUEST_VERSION,
        "manifest_id": manifest["manifest_id"],
        "campaign_id": manifest["campaign_id"],
        "campaign_stage": manifest["campaign_stage"],
        "rq_id": manifest["rq_id"],
        "scenario_id": manifest["scenario_id"],
        "backend_mode": manifest["backend_mode"],
        "repository_commit": manifest["repository_commit"],
        "seed_hex": manifest["randomization"]["seed_hex"],
        "configurations": copy.deepcopy(manifest["configurations"]),
    }


def _has_forbidden_hash_key(value: Any) -> bool:
    if isinstance(value, dict):
        if {"manifest_sha256", "randomization_manifest_sha256"}.intersection(value):
            return True
        return any(_has_forbidden_hash_key(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_forbidden_hash_key(item) for item in value)
    return False


def _exercise_valid_manifest(manifest: dict[str, Any]) -> None:
    validate_randomization_manifest_v1(manifest)
    request = _request_from_manifest(manifest)
    rebuilt = materialize_randomization_manifest_v1(request)
    _require(
        "VALID_RECONSTRUCTION",
        canonical_json_bytes(rebuilt) == canonical_json_bytes(manifest),
    )

    reordered_request = copy.deepcopy(request)
    reordered_request["configurations"].reverse()
    for configuration in reordered_request["configurations"]:
        configuration["source_artifacts"].reverse()
    reordered = materialize_randomization_manifest_v1(reordered_request)
    _require(
        "INPUT_REORDERING_INVARIANT",
        canonical_json_bytes(reordered) == canonical_json_bytes(manifest),
    )

    blocks = manifest["blocks"]
    configurations = manifest["configurations"]
    scenario_specs = [
        source
        for configuration in configurations
        for source in configuration["source_artifacts"]
        if source["role"] == "scenario_spec"
    ]
    _require("EXACTLY_ONE_SCENARIO_SPEC", len(scenario_specs) == 1)
    for configuration in configurations:
        configuration_id = configuration["configuration_id"]
        matching = [
            block for block in blocks if block["configuration_id"] == configuration_id
        ]
        orders = Counter(block["order"] for block in matching)
        expected_half = configuration["block_count"] // 2
        _require(
            "EXACT_CONFIGURATION_BALANCE",
            orders == Counter({"AB": expected_half, "BA": expected_half}),
        )
        _require(
            "BIJECTIVE_BLOCK_COVERAGE",
            {block["block_index"] for block in matching}
            == set(range(1, configuration["block_count"] + 1))
            and len(matching) == configuration["block_count"],
        )

    _require("SELF_HASH_ABSENT", not _has_forbidden_hash_key(manifest))
    digest = randomization_manifest_sha256(manifest)
    _require("CANONICAL_SHA256_STABLE", digest == sha256_json(manifest))
    first = materialize_randomization_manifest_v1(request)
    second = materialize_randomization_manifest_v1(copy.deepcopy(request))
    _require(
        "REPEATED_MATERIALIZATION_BYTES_IDENTICAL",
        canonical_json_bytes(first) == canonical_json_bytes(second),
    )
    _require(
        "REPEATED_MATERIALIZATION_DIGEST_IDENTICAL",
        randomization_manifest_sha256(first)
        == randomization_manifest_sha256(second),
    )


def _exercise_source_role_contract(manifest: dict[str, Any], validator: Any) -> None:
    request = _request_from_manifest(manifest)

    def observed(mutator: Any) -> str | None:
        candidate = copy.deepcopy(request)
        mutator(candidate["configurations"][0]["source_artifacts"])
        try:
            materialize_randomization_manifest_v1(candidate)
        except ExperimentContractError as exc:
            return _stable_error_code(str(exc))
        return None

    def schema_rejected(mutator: Any) -> bool:
        candidate = copy.deepcopy(manifest)
        mutator(candidate["configurations"][0]["source_artifacts"])
        return bool(list(validator.iter_errors(candidate)))

    _require(
        "SOURCE_ROLE_REQUIRED",
        observed(lambda sources: sources[0].pop("role")) == "MISSING_REQUIRED_FIELD",
    )
    _require(
        "SCENARIO_SPEC_REQUIRED",
        observed(lambda sources: sources[0].__setitem__("role", "auxiliary_source"))
        == "SCENARIO_SPEC_CARDINALITY",
    )
    _require(
        "SCENARIO_SPEC_UNIQUE",
        observed(
            lambda sources: sources.append(
                {"role": "scenario_spec", "path": "auxiliary/source.json", "sha256": "0" * 64}
            )
        )
        == "SCENARIO_SPEC_CARDINALITY",
    )
    _require(
        "SOURCE_ROLE_SAFE",
        observed(lambda sources: sources[0].__setitem__("role", "../scenario_spec"))
        == "INVALID_SOURCE_ROLE",
    )
    _require(
        "SOURCE_ROLE_REQUIRED_BY_SCHEMA",
        schema_rejected(lambda sources: sources[0].pop("role")),
    )
    _require(
        "SCENARIO_SPEC_REQUIRED_BY_SCHEMA",
        schema_rejected(
            lambda sources: sources[0].__setitem__("role", "auxiliary_source")
        ),
    )
    _require(
        "SCENARIO_SPEC_UNIQUE_BY_SCHEMA",
        schema_rejected(
            lambda sources: sources.append(
                {"role": "scenario_spec", "path": "auxiliary/source.json", "sha256": "0" * 64}
            )
        ),
    )
    _require(
        "SOURCE_ROLE_SAFE_BY_SCHEMA",
        schema_rejected(
            lambda sources: sources[0].__setitem__("role", "../scenario_spec")
        ),
    )

    hash_request = copy.deepcopy(request)
    configuration_sources = hash_request["configurations"][0]["source_artifacts"]
    configuration_sources.append(
        {"role": "auxiliary_source", "path": "auxiliary/source.json", "sha256": "0" * 64}
    )
    original = materialize_randomization_manifest_v1(hash_request)
    changed_request = copy.deepcopy(hash_request)
    changed_sources = changed_request["configurations"][0]["source_artifacts"]
    changed_sources[0]["role"], changed_sources[1]["role"] = (
        changed_sources[1]["role"],
        changed_sources[0]["role"],
    )
    changed = materialize_randomization_manifest_v1(changed_request)
    _require(
        "SOURCE_ROLE_PARTICIPATES_IN_HASH",
        randomization_manifest_sha256(original) != randomization_manifest_sha256(changed),
    )


def _validate_envelope(fixture: Any, path: Path) -> dict[str, Any]:
    if not isinstance(fixture, dict):
        raise SystemExit(f"{path}: fixture must be an object")
    allowed = {
        "fixture_contract",
        "valid",
        "expect_schema_rejection",
        "manifest",
        "expected_error",
    }
    if set(fixture).difference(allowed):
        raise SystemExit(f"{path}: fixture has unknown envelope properties")
    if fixture.get("fixture_contract") != FIXTURE_CONTRACT:
        raise SystemExit(f"{path}: unsupported fixture_contract")
    if not isinstance(fixture.get("valid"), bool):
        raise SystemExit(f"{path}: valid must be boolean")
    if not isinstance(fixture.get("expect_schema_rejection"), bool):
        raise SystemExit(f"{path}: expect_schema_rejection must be boolean")
    if not isinstance(fixture.get("manifest"), dict):
        raise SystemExit(f"{path}: manifest must be an object")
    if fixture["valid"] is False:
        if not isinstance(fixture.get("expected_error"), str):
            raise SystemExit(f"{path}: invalid fixture lacks expected_error")
    elif "expected_error" in fixture:
        raise SystemExit(f"{path}: valid fixture must not declare expected_error")
    return fixture


def _exercise_noncanonical_block_sequence(
    valid_manifests: list[dict[str, Any]],
) -> None:
    original = copy.deepcopy(
        max(valid_manifests, key=lambda item: item["total_block_count"])
    )
    validate_randomization_manifest_v1(original)
    _require("NONCANONICAL_SEQUENCE_ORIGINAL_ACCEPTED", True)
    original_digest = randomization_manifest_sha256(original)

    permuted = copy.deepcopy(original)
    permuted["blocks"].reverse()
    _require(
        "NONCANONICAL_SEQUENCE_BLOCK_SET_PRESERVED",
        Counter(canonical_json_bytes(block) for block in permuted["blocks"])
        == Counter(canonical_json_bytes(block) for block in original["blocks"]),
    )
    try:
        validate_randomization_manifest_v1(permuted)
    except ExperimentContractError as exc:
        actual_error = _stable_error_code(str(exc))
    else:
        actual_error = None
    _require(
        "NONCANONICAL_BLOCK_SEQUENCE_REJECTED",
        actual_error == "NONCANONICAL_BLOCK_SEQUENCE",
    )
    _require(
        "NONCANONICAL_SEQUENCE_ORIGINAL_SHA256_STABLE",
        randomization_manifest_sha256(original) == original_digest,
    )


def validate_fixtures() -> None:
    from jsonschema import Draft202012Validator

    repository = Path(__file__).resolve().parents[1]
    schema_path = (
        repository / "schemas" / "phase19" / "randomization-manifest-v1.schema.json"
    )
    fixtures_path = (
        repository
        / "schemas"
        / "phase19"
        / "fixtures"
        / "randomization-manifest-v1"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    _require("SCHEMA_DRAFT_2020_12_VALID", True)
    validator = Draft202012Validator(schema)

    fixture_paths = sorted(fixtures_path.glob("*.json"))
    valid_count = 0
    invalid_count = 0
    valid_manifests: list[dict[str, Any]] = []
    invalid_codes: set[str] = set()
    for fixture_path in fixture_paths:
        fixture = _validate_envelope(
            json.loads(fixture_path.read_text(encoding="utf-8")),
            fixture_path,
        )
        manifest = fixture["manifest"]
        schema_rejected = bool(list(validator.iter_errors(manifest)))
        if schema_rejected != fixture["expect_schema_rejection"]:
            raise SystemExit(
                f"{fixture_path}: expect_schema_rejection="
                f"{fixture['expect_schema_rejection']}, observed {schema_rejected}"
            )

        if fixture["valid"]:
            if schema_rejected:
                raise SystemExit(f"{fixture_path}: valid fixture failed schema")
            _exercise_valid_manifest(manifest)
            valid_manifests.append(manifest)
            valid_count += 1
        else:
            expected_error = fixture["expected_error"]
            try:
                validate_randomization_manifest_v1(manifest)
            except ExperimentContractError as exc:
                actual_error = _stable_error_code(str(exc))
                if actual_error != expected_error:
                    raise SystemExit(
                        f"{fixture_path}: expected {expected_error}, received "
                        f"{actual_error!r} from {exc}"
                    ) from exc
            else:
                raise SystemExit(
                    f"{fixture_path}: invalid fixture was unexpectedly accepted"
                )
            invalid_codes.add(expected_error)
            invalid_count += 1
        _require(f"FIXTURE_{_marker(fixture_path)}", True)

    _require("VALID_FIXTURE_COUNT", valid_count == VALID_FIXTURE_COUNT)
    _require("INVALID_FIXTURE_COUNT", invalid_count == INVALID_FIXTURE_COUNT)
    _exercise_source_role_contract(valid_manifests[0], validator)
    _exercise_noncanonical_block_sequence(valid_manifests)

    coverage = {
        "rq": {manifest["rq_id"] for manifest in valid_manifests},
        "stage": {manifest["campaign_stage"] for manifest in valid_manifests},
        "backend": {manifest["backend_mode"] for manifest in valid_manifests},
        "configuration_counts": {
            len(manifest["configurations"]) for manifest in valid_manifests
        },
        "block_counts": {
            configuration["block_count"]
            for manifest in valid_manifests
            for configuration in manifest["configurations"]
        },
    }
    _require("VALID_FIXTURE_RQ_COVERAGE", coverage["rq"] == {"RQ3", "RQ4"})
    _require(
        "VALID_FIXTURE_STAGE_COVERAGE",
        coverage["stage"] == {"foundation", "pilot", "confirmatory"},
    )
    _require(
        "VALID_FIXTURE_BACKEND_COVERAGE",
        coverage["backend"] == {"mock", "real"},
    )
    _require(
        "VALID_FIXTURE_CONFIGURATION_CARDINALITY",
        1 in coverage["configuration_counts"]
        and any(count > 1 for count in coverage["configuration_counts"]),
    )
    _require("VALID_FIXTURE_BLOCK_COUNT_COVERAGE", coverage["block_counts"] == {2, 4})

    required_adversarial_codes = {
        "MISSING_BLOCK",
        "ADDITIONAL_BLOCK",
        "ORDER_ASSIGNMENT_MISMATCH",
        "GLOBAL_ORDER_MISMATCH",
    }
    _require(
        "EXPLICIT_ADVERSARIAL_FIXTURES",
        required_adversarial_codes.issubset(invalid_codes),
    )
    _require(
        "ERROR_CODE_PREFIX_ONLY",
        _stable_error_code(
            "WRAPPER_ERROR: incidental ORDER_ASSIGNMENT_MISMATCH: substring"
        )
        == "WRAPPER_ERROR"
        and _stable_error_code(
            "incidental ORDER_ASSIGNMENT_MISMATCH: substring without prefix"
        )
        is None,
    )

    seed_request = _request_from_manifest(
        max(valid_manifests, key=lambda item: item["total_block_count"])
    )
    seed_request["seed_hex"] = _SEED_A
    seed_a_manifest = materialize_randomization_manifest_v1(seed_request)
    seed_request["seed_hex"] = _SEED_B
    seed_b_manifest = materialize_randomization_manifest_v1(seed_request)
    projection = lambda value: [
        (
            block["configuration_id"],
            block["block_index"],
            block["order"],
        )
        for block in value["blocks"]
    ]
    _require(
        "CHOSEN_SEEDS_PRODUCE_DIFFERENT_SCHEDULES",
        projection(seed_a_manifest) != projection(seed_b_manifest),
    )
    _require(
        "BLOCK_IDS_INDEPENDENT_OF_SEED",
        {block["block_id"] for block in seed_a_manifest["blocks"]}
        == {block["block_id"] for block in seed_b_manifest["blocks"]},
    )
    print("PHASE19_RANDOMIZATION_MANIFEST_FIXTURES_OK")


if __name__ == "__main__":
    if sys.argv[1:] == ["--validate-fixtures"]:
        validate_fixtures()
    else:
        raise SystemExit(
            "usage: validate_phase19_randomization_manifest.py "
            "--validate-fixtures"
        )
