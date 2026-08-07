"""Deterministic Phase 19 randomization-manifest contract.

This module defines only the Phase 19.7B-2a catalog and block randomization.
Expansion into periods, run slots, physical executions, attempts, and results
belongs to later phases.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from l2i.experiment_contract import (
    ExperimentContractError,
    canonical_json_bytes,
    sha256_json,
)


REQUEST_VERSION = "phase19-randomization-request-v1"
MANIFEST_VERSION = "phase19-randomization-manifest-v1"
ALGORITHM_ID = "sha256-ranked-balanced-v1"
ORDER_RANK_NAMESPACE = "phase19-randomization-order-rank-v1"
GLOBAL_RANK_NAMESPACE = "phase19-randomization-global-rank-v1"
BLOCK_ID_NAMESPACE = "phase19-randomization-block-id-v1"

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_CAMPAIGN_STAGES = frozenset({"foundation", "pilot", "confirmatory"})
_RQ_IDS = frozenset({"RQ3", "RQ4"})
_BACKEND_MODES = frozenset({"mock", "real"})
_ORDERS = frozenset({"AB", "BA"})

_REQUEST_FIELDS = frozenset(
    {
        "request_version",
        "manifest_id",
        "campaign_id",
        "campaign_stage",
        "rq_id",
        "scenario_id",
        "backend_mode",
        "repository_commit",
        "seed_hex",
        "configurations",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "contract_version",
        "manifest_id",
        "campaign_id",
        "campaign_stage",
        "rq_id",
        "scenario_id",
        "backend_mode",
        "repository_commit",
        "randomization",
        "configurations",
        "total_block_count",
        "blocks",
    }
)
_CONFIGURATION_FIELDS = frozenset(
    {"configuration_id", "profile_id", "block_count", "source_artifacts"}
)
_SOURCE_FIELDS = frozenset({"path", "sha256"})
_RANDOMIZATION_FIELDS = frozenset({"algorithm_id", "seed_hex"})
_BLOCK_FIELDS = frozenset(
    {"sequence_index", "block_id", "block_index", "configuration_id", "order"}
)


def _fail(code: str, message: str) -> NoReturn:
    """Raise one contract error with a machine-stable prefix."""

    raise ExperimentContractError(f"{code}: {message}")


def _object(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("INVALID_TYPE", f"{field_name} must be an object")
    return value


def _array(value: Any, field_name: str) -> Sequence[Any]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        _fail("INVALID_TYPE", f"{field_name} must be an array")
    return value


def _fields(
    value: Mapping[str, Any],
    expected: frozenset[str],
    field_name: str,
) -> None:
    unknown = set(value).difference(expected)
    if unknown:
        rendered_unknown = sorted(repr(key) for key in unknown)
        _fail(
            "UNKNOWN_PROPERTY",
            f"{field_name} has unknown properties: {', '.join(rendered_unknown)}",
        )
    missing = sorted(expected.difference(value))
    if missing:
        _fail(
            "MISSING_REQUIRED_FIELD",
            f"{field_name} is missing: {', '.join(missing)}",
        )


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        _fail(
            "INVALID_IDENTIFIER",
            f"{field_name} must match {_IDENTIFIER_PATTERN.pattern!r}",
        )
    return value


def _source_path(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("UNSAFE_SOURCE_PATH", f"{field_name} must be a non-empty string")
    if (
        value.startswith("/")
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _fail("UNSAFE_SOURCE_PATH", f"{field_name} must be a relative POSIX path")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        _fail(
            "UNSAFE_SOURCE_PATH",
            f"{field_name} contains an empty, dot, or dot-dot segment",
        )
    return value


def _configuration_catalog(
    value: Any,
) -> list[dict[str, Any]]:
    configurations = _array(value, "configurations")
    if not configurations:
        _fail("EMPTY_CONFIGURATION_CATALOG", "at least one configuration is required")

    normalized: list[dict[str, Any]] = []
    seen_configuration_ids: set[str] = set()
    for configuration_position, raw_configuration in enumerate(configurations):
        field_name = f"configurations[{configuration_position}]"
        configuration = _object(raw_configuration, field_name)
        _fields(configuration, _CONFIGURATION_FIELDS, field_name)
        configuration_id = _identifier(
            configuration["configuration_id"],
            f"{field_name}.configuration_id",
        )
        if configuration_id in seen_configuration_ids:
            _fail(
                "DUPLICATE_CONFIGURATION_ID",
                f"configuration_id {configuration_id!r} is repeated",
            )
        seen_configuration_ids.add(configuration_id)
        profile_id = _identifier(
            configuration["profile_id"], f"{field_name}.profile_id"
        )
        block_count = configuration["block_count"]
        if (
            not isinstance(block_count, int)
            or isinstance(block_count, bool)
            or block_count < 2
            or block_count % 2 != 0
        ):
            _fail(
                "INVALID_BLOCK_COUNT",
                f"{field_name}.block_count must be an even integer >= 2",
            )

        raw_sources = _array(
            configuration["source_artifacts"], f"{field_name}.source_artifacts"
        )
        if not raw_sources:
            _fail(
                "EMPTY_SOURCE_ARTIFACTS",
                f"{field_name}.source_artifacts must not be empty",
            )
        sources: list[dict[str, str]] = []
        seen_paths: set[str] = set()
        for source_position, raw_source in enumerate(raw_sources):
            source_name = f"{field_name}.source_artifacts[{source_position}]"
            source = _object(raw_source, source_name)
            _fields(source, _SOURCE_FIELDS, source_name)
            path = _source_path(source["path"], f"{source_name}.path")
            if path in seen_paths:
                _fail(
                    "DUPLICATE_SOURCE_PATH",
                    f"source path {path!r} is repeated in {configuration_id!r}",
                )
            seen_paths.add(path)
            digest = source["sha256"]
            if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
                _fail(
                    "INVALID_SOURCE_SHA256",
                    f"{source_name}.sha256 must be 64 lowercase hexadecimal characters",
                )
            sources.append({"path": path, "sha256": digest})

        sources.sort(key=lambda item: (item["path"], item["sha256"]))
        normalized.append(
            {
                "configuration_id": configuration_id,
                "profile_id": profile_id,
                "block_count": block_count,
                "source_artifacts": sources,
            }
        )

    normalized.sort(key=lambda item: item["configuration_id"])
    return normalized


def _request_identity(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize fields shared by request and manifest."""

    manifest_id = _identifier(request["manifest_id"], "manifest_id")
    campaign_id = _identifier(request["campaign_id"], "campaign_id")
    campaign_stage = request["campaign_stage"]
    if not isinstance(campaign_stage, str) or campaign_stage not in _CAMPAIGN_STAGES:
        _fail(
            "UNSUPPORTED_CAMPAIGN_STAGE",
            f"unsupported campaign_stage: {campaign_stage!r}",
        )
    rq_id = request["rq_id"]
    if not isinstance(rq_id, str) or rq_id not in _RQ_IDS:
        _fail("UNSUPPORTED_RQ", f"unsupported rq_id: {rq_id!r}")
    scenario_id = _identifier(request["scenario_id"], "scenario_id")
    backend_mode = request["backend_mode"]
    if not isinstance(backend_mode, str) or backend_mode not in _BACKEND_MODES:
        _fail(
            "UNSUPPORTED_BACKEND_MODE",
            f"unsupported backend_mode: {backend_mode!r}",
        )
    if campaign_stage == "confirmatory" and backend_mode != "real":
        _fail(
            "CONFIRMATORY_REQUIRES_REAL",
            "confirmatory manifests require backend_mode 'real'",
        )
    repository_commit = request["repository_commit"]
    if (
        not isinstance(repository_commit, str)
        or not _COMMIT_PATTERN.fullmatch(repository_commit)
    ):
        _fail(
            "INVALID_REPOSITORY_COMMIT",
            "repository_commit must be 40 lowercase hexadecimal characters",
        )
    return {
        "manifest_id": manifest_id,
        "campaign_id": campaign_id,
        "campaign_stage": campaign_stage,
        "rq_id": rq_id,
        "scenario_id": scenario_id,
        "backend_mode": backend_mode,
        "repository_commit": repository_commit,
    }


def _validate_request(request: Any) -> tuple[dict[str, Any], bytes]:
    request_object = _object(request, "request")
    _fields(request_object, _REQUEST_FIELDS, "request")
    if (
        not isinstance(request_object["request_version"], str)
        or request_object["request_version"] != REQUEST_VERSION
    ):
        _fail(
            "UNSUPPORTED_REQUEST_VERSION",
            f"request_version must be {REQUEST_VERSION!r}",
        )
    identity = _request_identity(request_object)
    seed_hex = request_object["seed_hex"]
    if not isinstance(seed_hex, str) or not _SHA256_PATTERN.fullmatch(seed_hex):
        _fail(
            "INVALID_SEED_HEX",
            "seed_hex must encode exactly 32 bytes as lowercase hexadecimal",
        )
    configurations = _configuration_catalog(request_object["configurations"])
    return (
        {
            "request_version": REQUEST_VERSION,
            **identity,
            "seed_hex": seed_hex,
            "configurations": configurations,
        },
        bytes.fromhex(seed_hex),
    )


def _rank(seed_bytes: bytes, namespace: str, payload: Mapping[str, Any]) -> bytes:
    """Rank one canonical payload using the specified domain separator."""

    return hashlib.sha256(
        seed_bytes
        + b"\x00"
        + namespace.encode("ascii")
        + b"\x00"
        + canonical_json_bytes(payload)
    ).digest()


def _block_id(
    identity: Mapping[str, Any],
    configuration_id: str,
    block_index: int,
) -> str:
    payload = {
        "manifest_id": identity["manifest_id"],
        "campaign_id": identity["campaign_id"],
        "campaign_stage": identity["campaign_stage"],
        "rq_id": identity["rq_id"],
        "scenario_id": identity["scenario_id"],
        "backend_mode": identity["backend_mode"],
        "configuration_id": configuration_id,
        "block_index": block_index,
    }
    digest = hashlib.sha256(
        BLOCK_ID_NAMESPACE.encode("ascii")
        + b"\x00"
        + canonical_json_bytes(payload)
    ).hexdigest()
    return f"block-{digest}"


def materialize_randomization_manifest_v1(request: Any) -> dict[str, Any]:
    """Materialize one canonical, deterministic randomization manifest."""

    normalized, seed_bytes = _validate_request(request)
    identity = {
        key: normalized[key]
        for key in (
            "manifest_id",
            "campaign_id",
            "campaign_stage",
            "rq_id",
            "scenario_id",
            "backend_mode",
            "repository_commit",
        )
    }

    blocks: list[dict[str, Any]] = []
    for configuration in normalized["configurations"]:
        configuration_id = configuration["configuration_id"]
        candidates = [
            {
                "configuration_id": configuration_id,
                "block_index": block_index,
            }
            for block_index in range(1, configuration["block_count"] + 1)
        ]
        candidates.sort(
            key=lambda candidate: (
                _rank(seed_bytes, ORDER_RANK_NAMESPACE, candidate),
                canonical_json_bytes(candidate),
            )
        )
        midpoint = configuration["block_count"] // 2
        for position, candidate in enumerate(candidates):
            blocks.append(
                {
                    "block_id": _block_id(
                        identity,
                        configuration_id,
                        candidate["block_index"],
                    ),
                    "block_index": candidate["block_index"],
                    "configuration_id": configuration_id,
                    "order": "AB" if position < midpoint else "BA",
                }
            )

    blocks.sort(
        key=lambda block: (
            _rank(
                seed_bytes,
                GLOBAL_RANK_NAMESPACE,
                {
                    "block_id": block["block_id"],
                    "block_index": block["block_index"],
                    "configuration_id": block["configuration_id"],
                    "order": block["order"],
                },
            ),
            block["block_id"],
        )
    )
    sequenced_blocks = [
        {"sequence_index": sequence_index, **block}
        for sequence_index, block in enumerate(blocks, start=1)
    ]
    return {
        "contract_version": MANIFEST_VERSION,
        **identity,
        "randomization": {
            "algorithm_id": ALGORITHM_ID,
            "seed_hex": normalized["seed_hex"],
        },
        "configurations": normalized["configurations"],
        "total_block_count": len(sequenced_blocks),
        "blocks": sequenced_blocks,
    }


def _manifest_request(manifest: Mapping[str, Any]) -> dict[str, Any]:
    randomization = _object(manifest["randomization"], "randomization")
    _fields(randomization, _RANDOMIZATION_FIELDS, "randomization")
    if (
        not isinstance(randomization["algorithm_id"], str)
        or randomization["algorithm_id"] != ALGORITHM_ID
    ):
        _fail(
            "UNSUPPORTED_RANDOMIZATION_ALGORITHM",
            f"algorithm_id must be {ALGORITHM_ID!r}",
        )
    return {
        "request_version": REQUEST_VERSION,
        "manifest_id": manifest["manifest_id"],
        "campaign_id": manifest["campaign_id"],
        "campaign_stage": manifest["campaign_stage"],
        "rq_id": manifest["rq_id"],
        "scenario_id": manifest["scenario_id"],
        "backend_mode": manifest["backend_mode"],
        "repository_commit": manifest["repository_commit"],
        "seed_hex": randomization["seed_hex"],
        "configurations": manifest["configurations"],
    }


def _validated_blocks(value: Any) -> list[dict[str, Any]]:
    raw_blocks = _array(value, "blocks")
    blocks: list[dict[str, Any]] = []
    for position, raw_block in enumerate(raw_blocks):
        field_name = f"blocks[{position}]"
        block = _object(raw_block, field_name)
        _fields(block, _BLOCK_FIELDS, field_name)
        sequence_index = block["sequence_index"]
        block_index = block["block_index"]
        if (
            not isinstance(sequence_index, int)
            or isinstance(sequence_index, bool)
            or sequence_index < 1
        ):
            _fail(
                "INVALID_SEQUENCE_INDEX",
                f"{field_name}.sequence_index must be a positive integer",
            )
        if (
            not isinstance(block_index, int)
            or isinstance(block_index, bool)
            or block_index < 1
        ):
            _fail(
                "INVALID_BLOCK_INDEX",
                f"{field_name}.block_index must be a positive integer",
            )
        order = block["order"]
        if not isinstance(order, str) or order not in _ORDERS:
            _fail("UNSUPPORTED_ORDER", f"{field_name}.order must be AB or BA")
        blocks.append(
            {
                "sequence_index": sequence_index,
                "block_id": _identifier(block["block_id"], f"{field_name}.block_id"),
                "block_index": block_index,
                "configuration_id": _identifier(
                    block["configuration_id"],
                    f"{field_name}.configuration_id",
                ),
                "order": order,
            }
        )
    return blocks


def validate_randomization_manifest_v1(manifest: Any) -> None:
    """Validate structure and reconstruct every deterministic invariant."""

    manifest_object = _object(manifest, "manifest")
    _fields(manifest_object, _MANIFEST_FIELDS, "manifest")
    if (
        not isinstance(manifest_object["contract_version"], str)
        or manifest_object["contract_version"] != MANIFEST_VERSION
    ):
        _fail(
            "UNSUPPORTED_MANIFEST_VERSION",
            f"contract_version must be {MANIFEST_VERSION!r}",
        )

    request = _manifest_request(manifest_object)
    normalized_request, _ = _validate_request(request)
    expected = materialize_randomization_manifest_v1(normalized_request)
    blocks = _validated_blocks(manifest_object["blocks"])

    total = manifest_object["total_block_count"]
    if not isinstance(total, int) or isinstance(total, bool) or total < 1:
        _fail(
            "INVALID_TOTAL_BLOCK_COUNT",
            "total_block_count must be a positive integer",
        )
    expected_total = expected["total_block_count"]
    if total != expected_total:
        _fail(
            "TOTAL_BLOCK_COUNT_MISMATCH",
            f"total_block_count must be {expected_total}, received {total}",
        )
    if len(blocks) < expected_total:
        _fail("MISSING_BLOCK", "blocks contains fewer entries than the catalog requires")
    if len(blocks) > expected_total:
        _fail(
            "ADDITIONAL_BLOCK",
            "blocks contains more entries than the catalog requires",
        )

    block_ids = [block["block_id"] for block in blocks]
    if len(set(block_ids)) != len(block_ids):
        _fail("DUPLICATE_BLOCK_ID", "block_id values must be unique")
    sequence_indexes = [block["sequence_index"] for block in blocks]
    if len(set(sequence_indexes)) != len(sequence_indexes):
        _fail(
            "DUPLICATE_SEQUENCE_INDEX",
            "sequence_index values must be unique",
        )
    if sorted(sequence_indexes) != list(range(1, expected_total + 1)):
        _fail(
            "NONCONTIGUOUS_SEQUENCE_INDEX",
            "sequence_index must cover 1..total_block_count exactly",
        )

    configuration_pairs = [
        (block["configuration_id"], block["block_index"]) for block in blocks
    ]
    if len(set(configuration_pairs)) != len(configuration_pairs):
        _fail(
            "DUPLICATE_CONFIGURATION_BLOCK",
            "(configuration_id, block_index) pairs must be unique",
        )

    expected_by_pair = {
        (block["configuration_id"], block["block_index"]): block
        for block in expected["blocks"]
    }
    actual_pairs = set(configuration_pairs)
    expected_pairs = set(expected_by_pair)
    if actual_pairs != expected_pairs:
        _fail(
            "BLOCK_COVERAGE_MISMATCH",
            "blocks must cover 1..block_count for every configuration",
        )

    for block in blocks:
        pair = (block["configuration_id"], block["block_index"])
        if block["block_id"] != expected_by_pair[pair]["block_id"]:
            _fail(
                "INCORRECT_BLOCK_ID",
                f"block_id is not derived correctly for {pair!r}",
            )

    block_count_by_configuration = {
        configuration["configuration_id"]: configuration["block_count"]
        for configuration in normalized_request["configurations"]
    }
    for configuration_id, block_count in block_count_by_configuration.items():
        orders = [
            block["order"]
            for block in blocks
            if block["configuration_id"] == configuration_id
        ]
        if (
            orders.count("AB") != block_count // 2
            or orders.count("BA") != block_count // 2
        ):
            _fail(
                "ORDER_IMBALANCE",
                f"{configuration_id!r} must contain equal AB and BA counts",
            )

    for block in blocks:
        pair = (block["configuration_id"], block["block_index"])
        if block["order"] != expected_by_pair[pair]["order"]:
            _fail(
                "ORDER_ASSIGNMENT_MISMATCH",
                f"order is incompatible with seed for {pair!r}",
            )

    actual_by_sequence = sorted(blocks, key=lambda block: block["sequence_index"])
    expected_sequence = [
        (block["block_id"], block["configuration_id"], block["block_index"])
        for block in expected["blocks"]
    ]
    actual_sequence = [
        (block["block_id"], block["configuration_id"], block["block_index"])
        for block in actual_by_sequence
    ]
    if actual_sequence != expected_sequence:
        _fail(
            "GLOBAL_ORDER_MISMATCH",
            "sequence_index ordering is incompatible with the global seed ranking",
        )

    if manifest_object["configurations"] != expected["configurations"]:
        _fail(
            "NONCANONICAL_CONFIGURATION_CATALOG",
            "configurations and source_artifacts must use canonical ordering",
        )

    if blocks != expected["blocks"]:
        _fail(
            "NONCANONICAL_BLOCK_SEQUENCE",
            "blocks must be materialized in canonical sequence_index order",
        )


def randomization_manifest_sha256(manifest: Any) -> str:
    """Validate and externally hash a complete manifest without self-hash."""

    validate_randomization_manifest_v1(manifest)
    return sha256_json(manifest)


__all__ = [
    "ALGORITHM_ID",
    "BLOCK_ID_NAMESPACE",
    "GLOBAL_RANK_NAMESPACE",
    "MANIFEST_VERSION",
    "ORDER_RANK_NAMESPACE",
    "REQUEST_VERSION",
    "materialize_randomization_manifest_v1",
    "randomization_manifest_sha256",
    "validate_randomization_manifest_v1",
]
