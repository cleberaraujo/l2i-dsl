"""Deterministic Phase 19 execution-plan contract.

This additive module expands one validated randomization manifest into exactly
two planned physical run slots per block.  It deliberately defines no physical
execution identity, attempt record, retry, result, timestamp, or artifact.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from l2i.experiment_contract import (
    ExperimentAssignmentV2,
    ExperimentContractError,
    canonical_json_bytes,
    sha256_json,
    validate_paired_block_v2,
)
from l2i.experiment_plan import validate_randomization_manifest_v1


EXECUTION_PLAN_VERSION = "phase19-execution-plan-v1"
EXPANSION_ALGORITHM_ID = "two-period-paired-block-expansion-v1"
EXECUTION_PLAN_ID_NAMESPACE = "phase19-execution-plan-id-v1"
RUN_SLOT_ID_NAMESPACE = "phase19-run-slot-id-v1"
PERIODS_PER_BLOCK = 2

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_PLAN_ID_PATTERN = re.compile(r"^execution-plan-[0-9a-f]{64}$")
_RUN_SLOT_ID_PATTERN = re.compile(r"^run-slot-[0-9a-f]{64}$")
_CAMPAIGN_STAGES = frozenset({"foundation", "pilot", "confirmatory"})
_RQ_IDS = frozenset({"RQ3", "RQ4"})
_BACKEND_MODES = frozenset({"mock", "real"})
_ARMS = frozenset({"A", "B"})
_ORDERS = frozenset({"AB", "BA"})
_TREATMENTS = frozenset(
    {"baseline", "adapt", "observation_only", "selective_assurance"}
)

_PLAN_FIELDS = frozenset(
    {
        "contract_version",
        "execution_plan_id",
        "randomization_manifest_sha256",
        "randomization_manifest",
        "randomization_manifest_id",
        "repository_commit",
        "campaign_id",
        "campaign_stage",
        "rq_id",
        "scenario_id",
        "backend_mode",
        "expansion",
        "total_block_count",
        "total_run_slot_count",
        "run_slots",
    }
)
_EXPANSION_FIELDS = frozenset({"algorithm_id", "periods_per_block"})
_RUN_SLOT_FIELDS = frozenset(
    {
        "slot_index",
        "run_slot_id",
        "block_sequence_index",
        "execution_requirements",
        "assignment",
    }
)
_EXECUTION_REQUIREMENT_FIELDS = frozenset(
    {"scenario_id", "profile_id", "backend_mode"}
)
_ASSIGNMENT_FIELDS = frozenset(
    {
        "campaign_id",
        "campaign_stage",
        "rq_id",
        "configuration_id",
        "arm",
        "treatment",
        "block_id",
        "block_index",
        "period",
        "order",
        "randomization_manifest_sha256",
    }
)
_ROOT_MANIFEST_FIELDS = (
    ("randomization_manifest_id", "manifest_id"),
    ("repository_commit", "repository_commit"),
    ("campaign_id", "campaign_id"),
    ("campaign_stage", "campaign_stage"),
    ("rq_id", "rq_id"),
    ("scenario_id", "scenario_id"),
    ("backend_mode", "backend_mode"),
)
_SLOT_ASSIGNMENT_FIELDS = (
    "campaign_id",
    "campaign_stage",
    "rq_id",
    "configuration_id",
    "block_index",
    "order",
    "randomization_manifest_sha256",
)
_FORBIDDEN_EXECUTION_STATE_FIELDS = frozenset(
    {
        "execution",
        "executions",
        "execution_id",
        "attempt",
        "attempts",
        "attempt_id",
        "attempt_index",
        "attempt_number",
        "retry",
        "retries",
        "retry_index",
        "retry_number",
        "result",
        "results",
        "observation",
        "observations",
        "metric",
        "metrics",
        "started_at",
        "started_at_utc",
        "completed_at",
        "completed_at_utc",
        "timestamp",
        "timestamps",
        "status",
        "run_status",
        "final_status",
        "artifact",
        "artifacts",
        "artifact_sha256",
        "artifact_hashes",
    }
)


def _fail(code: str, message: str) -> NoReturn:
    raise ExperimentContractError(f"{code}: {message}")


def _object(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("INVALID_PLAN_STRUCTURE", f"{field_name} must be an object")
    return value


def _array(value: Any, field_name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail("INVALID_PLAN_STRUCTURE", f"{field_name} must be an array")
    return value


def _fields(value: Mapping[str, Any], expected: frozenset[str], name: str) -> None:
    unknown = sorted(set(value).difference(expected))
    if unknown:
        _fail(
            "UNKNOWN_PROPERTY",
            f"{name} has unknown properties: " + ", ".join(repr(x) for x in unknown),
        )
    missing = sorted(expected.difference(value))
    if missing:
        _fail("MISSING_REQUIRED_FIELD", f"{name} is missing: {', '.join(missing)}")


def _positive_integer(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        _fail("INVALID_PLAN_STRUCTURE", f"{field_name} must be a positive integer")
    return value


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        _fail("INVALID_PLAN_STRUCTURE", f"{field_name} must be a safe identifier")
    return value


def _enum(value: Any, allowed: frozenset[Any], field_name: str) -> Any:
    if value not in allowed:
        _fail("INVALID_PLAN_STRUCTURE", f"{field_name} has an unsupported value")
    return value


def _reserved_names(value: Any, *, root: bool = True) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str):
                found.add(key)
            if root and key == "randomization_manifest":
                continue
            found.update(_reserved_names(child, root=False))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            found.update(_reserved_names(child, root=False))
    return found


def _validate_plan_structure(plan: Any) -> dict[str, Any]:
    plan_object = _object(plan, "plan")
    names = _reserved_names(plan_object)
    if "execution_plan_sha256" in names:
        _fail("PLAN_SELF_HASH_FORBIDDEN", "execution_plan_sha256 is an external hash")
    forbidden = sorted(names.intersection(_FORBIDDEN_EXECUTION_STATE_FIELDS))
    if forbidden:
        _fail(
            "EXECUTION_STATE_FORBIDDEN",
            "execution-state properties are forbidden: " + ", ".join(forbidden),
        )

    _fields(plan_object, _PLAN_FIELDS, "plan")
    version = plan_object["contract_version"]
    if version != EXECUTION_PLAN_VERSION:
        _fail(
            "UNSUPPORTED_EXECUTION_PLAN_VERSION",
            f"contract_version must be {EXECUTION_PLAN_VERSION!r}",
        )
    if not isinstance(plan_object["randomization_manifest"], Mapping):
        _fail("INVALID_PLAN_STRUCTURE", "randomization_manifest must be an object")
    if not isinstance(plan_object["execution_plan_id"], str) or not _PLAN_ID_PATTERN.fullmatch(
        plan_object["execution_plan_id"]
    ):
        _fail("INVALID_PLAN_STRUCTURE", "execution_plan_id has an invalid format")
    if not isinstance(plan_object["randomization_manifest_sha256"], str) or not _SHA256_PATTERN.fullmatch(
        plan_object["randomization_manifest_sha256"]
    ):
        _fail(
            "INVALID_PLAN_STRUCTURE",
            "randomization_manifest_sha256 must be lowercase SHA-256",
        )
    _identifier(plan_object["randomization_manifest_id"], "randomization_manifest_id")
    if not isinstance(plan_object["repository_commit"], str) or not _COMMIT_PATTERN.fullmatch(
        plan_object["repository_commit"]
    ):
        _fail("INVALID_PLAN_STRUCTURE", "repository_commit must be lowercase SHA-1")
    _identifier(plan_object["campaign_id"], "campaign_id")
    _enum(plan_object["campaign_stage"], _CAMPAIGN_STAGES, "campaign_stage")
    _enum(plan_object["rq_id"], _RQ_IDS, "rq_id")
    _identifier(plan_object["scenario_id"], "scenario_id")
    _enum(plan_object["backend_mode"], _BACKEND_MODES, "backend_mode")
    _positive_integer(plan_object["total_block_count"], "total_block_count")
    _positive_integer(plan_object["total_run_slot_count"], "total_run_slot_count")

    expansion = _object(plan_object["expansion"], "expansion")
    _fields(expansion, _EXPANSION_FIELDS, "expansion")
    if not isinstance(expansion["algorithm_id"], str):
        _fail("INVALID_PLAN_STRUCTURE", "expansion.algorithm_id must be a string")
    _positive_integer(expansion["periods_per_block"], "expansion.periods_per_block")

    raw_slots = _array(plan_object["run_slots"], "run_slots")
    if not raw_slots:
        _fail("INVALID_PLAN_STRUCTURE", "run_slots must not be empty")
    slots: list[dict[str, Any]] = []
    for position, raw_slot in enumerate(raw_slots):
        name = f"run_slots[{position}]"
        slot = _object(raw_slot, name)
        _fields(slot, _RUN_SLOT_FIELDS, name)
        slot_index = _positive_integer(slot["slot_index"], f"{name}.slot_index")
        block_sequence_index = _positive_integer(
            slot["block_sequence_index"], f"{name}.block_sequence_index"
        )
        run_slot_id = slot["run_slot_id"]
        if not isinstance(run_slot_id, str) or not _RUN_SLOT_ID_PATTERN.fullmatch(run_slot_id):
            _fail("INVALID_PLAN_STRUCTURE", f"{name}.run_slot_id has an invalid format")

        requirements = _object(slot["execution_requirements"], f"{name}.execution_requirements")
        _fields(requirements, _EXECUTION_REQUIREMENT_FIELDS, f"{name}.execution_requirements")
        normalized_requirements = {
            "scenario_id": _identifier(
                requirements["scenario_id"], f"{name}.execution_requirements.scenario_id"
            ),
            "profile_id": _identifier(
                requirements["profile_id"], f"{name}.execution_requirements.profile_id"
            ),
            "backend_mode": _enum(
                requirements["backend_mode"],
                _BACKEND_MODES,
                f"{name}.execution_requirements.backend_mode",
            ),
        }

        assignment = _object(slot["assignment"], f"{name}.assignment")
        _fields(assignment, _ASSIGNMENT_FIELDS, f"{name}.assignment")
        normalized_assignment = {
            "campaign_id": _identifier(assignment["campaign_id"], f"{name}.assignment.campaign_id"),
            "campaign_stage": _enum(
                assignment["campaign_stage"], _CAMPAIGN_STAGES, f"{name}.assignment.campaign_stage"
            ),
            "rq_id": _enum(assignment["rq_id"], _RQ_IDS, f"{name}.assignment.rq_id"),
            "configuration_id": _identifier(
                assignment["configuration_id"], f"{name}.assignment.configuration_id"
            ),
            "arm": _enum(assignment["arm"], _ARMS, f"{name}.assignment.arm"),
            "treatment": _enum(
                assignment["treatment"], _TREATMENTS, f"{name}.assignment.treatment"
            ),
            "block_id": _identifier(assignment["block_id"], f"{name}.assignment.block_id"),
            "block_index": _positive_integer(
                assignment["block_index"], f"{name}.assignment.block_index"
            ),
            "period": _positive_integer(assignment["period"], f"{name}.assignment.period"),
            "order": _enum(assignment["order"], _ORDERS, f"{name}.assignment.order"),
            "randomization_manifest_sha256": assignment["randomization_manifest_sha256"],
        }
        if normalized_assignment["period"] not in {1, 2}:
            _fail("INVALID_PLAN_STRUCTURE", f"{name}.assignment.period must be 1 or 2")
        digest = normalized_assignment["randomization_manifest_sha256"]
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            _fail(
                "INVALID_PLAN_STRUCTURE",
                f"{name}.assignment.randomization_manifest_sha256 is invalid",
            )
        slots.append(
            {
                "slot_index": slot_index,
                "run_slot_id": run_slot_id,
                "block_sequence_index": block_sequence_index,
                "execution_requirements": normalized_requirements,
                "assignment": normalized_assignment,
            }
        )
    return {**dict(plan_object), "expansion": dict(expansion), "run_slots": slots}


def execution_plan_id(randomization_manifest_sha256: str) -> str:
    """Derive the frozen deterministic execution-plan identifier."""

    if not _SHA256_PATTERN.fullmatch(randomization_manifest_sha256):
        _fail("INVALID_PLAN_STRUCTURE", "randomization manifest hash is invalid")
    digest = hashlib.sha256(
        EXECUTION_PLAN_ID_NAMESPACE.encode("ascii")
        + b"\x00"
        + randomization_manifest_sha256.encode("ascii")
    ).hexdigest()
    return f"execution-plan-{digest}"


def run_slot_id(
    randomization_manifest_sha256: str,
    block_id: str,
    period: int,
) -> str:
    """Derive one deterministic, path-safe planned run-slot identifier."""

    payload = {
        "randomization_manifest_sha256": randomization_manifest_sha256,
        "block_id": block_id,
        "period": period,
    }
    digest = hashlib.sha256(
        RUN_SLOT_ID_NAMESPACE.encode("ascii")
        + b"\x00"
        + canonical_json_bytes(payload)
    ).hexdigest()
    return f"run-slot-{digest}"


def _arm(order: str, period: int) -> str:
    return order[period - 1]


def _treatment(rq_id: str, arm: str) -> str:
    return {
        ("RQ3", "A"): "baseline",
        ("RQ3", "B"): "adapt",
        ("RQ4", "A"): "observation_only",
        ("RQ4", "B"): "selective_assurance",
    }[(rq_id, arm)]


def materialize_execution_plan_v1(randomization_manifest: Any) -> dict[str, Any]:
    """Expand a valid randomization manifest without further randomization."""

    validate_randomization_manifest_v1(randomization_manifest)
    manifest = dict(randomization_manifest)
    manifest_hash = sha256_json(manifest)
    profiles = {
        item["configuration_id"]: item["profile_id"]
        for item in manifest["configurations"]
    }
    slots: list[dict[str, Any]] = []
    for block in manifest["blocks"]:
        for period in (1, 2):
            arm = _arm(block["order"], period)
            assignment = {
                "campaign_id": manifest["campaign_id"],
                "campaign_stage": manifest["campaign_stage"],
                "rq_id": manifest["rq_id"],
                "configuration_id": block["configuration_id"],
                "arm": arm,
                "treatment": _treatment(manifest["rq_id"], arm),
                "block_id": block["block_id"],
                "block_index": block["sequence_index"],
                "period": period,
                "order": block["order"],
                "randomization_manifest_sha256": manifest_hash,
            }
            ExperimentAssignmentV2(**assignment)
            slots.append(
                {
                    "slot_index": 2 * (block["sequence_index"] - 1) + period,
                    "run_slot_id": run_slot_id(manifest_hash, block["block_id"], period),
                    "block_sequence_index": block["sequence_index"],
                    "execution_requirements": {
                        "scenario_id": manifest["scenario_id"],
                        "profile_id": profiles[block["configuration_id"]],
                        "backend_mode": manifest["backend_mode"],
                    },
                    "assignment": assignment,
                }
            )
    return {
        "contract_version": EXECUTION_PLAN_VERSION,
        "execution_plan_id": execution_plan_id(manifest_hash),
        "randomization_manifest_sha256": manifest_hash,
        "randomization_manifest": manifest,
        "randomization_manifest_id": manifest["manifest_id"],
        "repository_commit": manifest["repository_commit"],
        "campaign_id": manifest["campaign_id"],
        "campaign_stage": manifest["campaign_stage"],
        "rq_id": manifest["rq_id"],
        "scenario_id": manifest["scenario_id"],
        "backend_mode": manifest["backend_mode"],
        "expansion": {
            "algorithm_id": EXPANSION_ALGORITHM_ID,
            "periods_per_block": PERIODS_PER_BLOCK,
        },
        "total_block_count": manifest["total_block_count"],
        "total_run_slot_count": PERIODS_PER_BLOCK * manifest["total_block_count"],
        "run_slots": slots,
    }


def validate_execution_plan_v1(plan: Any) -> None:
    """Validate an execution plan using the frozen total precedence."""

    actual = _validate_plan_structure(plan)
    manifest = actual["randomization_manifest"]
    try:
        validate_randomization_manifest_v1(manifest)
    except ExperimentContractError as exc:
        _fail("INVALID_EMBEDDED_RANDOMIZATION_MANIFEST", str(exc))

    manifest_hash = sha256_json(manifest)
    if actual["randomization_manifest_sha256"] != manifest_hash:
        _fail(
            "RANDOMIZATION_MANIFEST_SHA256_MISMATCH",
            "declared hash differs from the canonical embedded manifest hash",
        )

    for plan_field, manifest_field in _ROOT_MANIFEST_FIELDS:
        if actual[plan_field] != manifest[manifest_field]:
            _fail(
                "PLAN_MANIFEST_FIELD_MISMATCH",
                f"{plan_field} must equal randomization_manifest.{manifest_field}",
            )

    if actual["execution_plan_id"] != execution_plan_id(manifest_hash):
        _fail("EXECUTION_PLAN_ID_MISMATCH", "execution_plan_id is not derived correctly")
    expansion = actual["expansion"]
    if expansion["algorithm_id"] != EXPANSION_ALGORITHM_ID:
        _fail(
            "UNSUPPORTED_EXPANSION_ALGORITHM",
            f"algorithm_id must be {EXPANSION_ALGORITHM_ID!r}",
        )
    if expansion["periods_per_block"] != PERIODS_PER_BLOCK:
        _fail("INVALID_PERIODS_PER_BLOCK", "periods_per_block must be 2")

    expected_blocks = manifest["total_block_count"]
    if actual["total_block_count"] != expected_blocks:
        _fail("TOTAL_BLOCK_COUNT_MISMATCH", "total_block_count differs from manifest")
    expected_slot_total = PERIODS_PER_BLOCK * expected_blocks
    if actual["total_run_slot_count"] != expected_slot_total:
        _fail(
            "TOTAL_RUN_SLOT_COUNT_MISMATCH",
            "total_run_slot_count must be twice the manifest block count",
        )

    slots = actual["run_slots"]
    ids = [slot["run_slot_id"] for slot in slots]
    if len(set(ids)) != len(ids):
        _fail("DUPLICATE_RUN_SLOT_ID", "run_slot_id values must be unique")
    indexes = [slot["slot_index"] for slot in slots]
    if len(set(indexes)) != len(indexes):
        _fail("DUPLICATE_SLOT_INDEX", "slot_index values must be unique")
    semantic_keys = [
        (slot["assignment"]["block_id"], slot["assignment"]["period"])
        for slot in slots
    ]
    if len(set(semantic_keys)) != len(semantic_keys):
        _fail(
            "DUPLICATE_SEMANTIC_SLOT",
            "(assignment.block_id, assignment.period) must be unique",
        )
    if sorted(indexes) != list(range(1, len(slots) + 1)):
        _fail("NONCONTIGUOUS_SLOT_INDEX", "slot_index must cover 1..len(run_slots)")

    expected = materialize_execution_plan_v1(manifest)
    expected_by_key = {
        (slot["assignment"]["block_id"], slot["assignment"]["period"]): slot
        for slot in expected["run_slots"]
    }
    expected_keys = set(expected_by_key)
    actual_keys = set(semantic_keys)
    missing = sorted(expected_keys.difference(actual_keys))
    if missing:
        _fail("MISSING_RUN_SLOT", f"expected semantic slots are missing: {missing!r}")
    additional = sorted(actual_keys.difference(expected_keys))
    if additional:
        _fail("ADDITIONAL_RUN_SLOT", f"unexpected semantic slots are present: {additional!r}")

    assignments_by_block: dict[str, list[ExperimentAssignmentV2]] = defaultdict(list)
    for slot in slots:
        assignment_dict = slot["assignment"]
        assignment = ExperimentAssignmentV2(**assignment_dict)
        key = (assignment.block_id, assignment.period)
        expected_slot = expected_by_key[key]
        requirements = slot["execution_requirements"]
        expected_requirements = expected_slot["execution_requirements"]
        if (
            requirements["scenario_id"] != expected_requirements["scenario_id"]
            or requirements["backend_mode"] != expected_requirements["backend_mode"]
        ):
            _fail(
                "EXECUTION_REQUIREMENTS_MISMATCH",
                "slot scenario_id or backend_mode differs from the plan root",
            )
        if requirements["profile_id"] != expected_requirements["profile_id"]:
            _fail(
                "PROFILE_ASSIGNMENT_MISMATCH",
                "profile_id does not match the manifest configuration",
            )
        if slot["block_sequence_index"] != expected_slot["block_sequence_index"]:
            _fail(
                "BLOCK_SEQUENCE_INDEX_MISMATCH",
                "block_sequence_index differs from manifest sequence_index",
            )
        mismatched_assignment_fields = [
            field
            for field in _SLOT_ASSIGNMENT_FIELDS
            if assignment_dict[field] != expected_slot["assignment"][field]
        ]
        if mismatched_assignment_fields:
            _fail(
                "SLOT_ASSIGNMENT_MISMATCH",
                "derived assignment fields differ: "
                + ", ".join(mismatched_assignment_fields),
            )
        if slot["slot_index"] != expected_slot["slot_index"]:
            _fail(
                "SLOT_INDEX_ASSIGNMENT_MISMATCH",
                "slot_index does not equal 2 * (sequence_index - 1) + period",
            )
        if slot["run_slot_id"] != expected_slot["run_slot_id"]:
            _fail("RUN_SLOT_ID_MISMATCH", "run_slot_id is not derived correctly")
        assignments_by_block[assignment.block_id].append(assignment)

    for block_id in sorted(assignments_by_block):
        validate_paired_block_v2(
            sorted(assignments_by_block[block_id], key=lambda item: item.period)
        )

    if slots != expected["run_slots"]:
        _fail(
            "NONCANONICAL_RUN_SLOT_SEQUENCE",
            "run_slots must be physically ordered by sequence_index then period",
        )


def execution_plan_sha256(plan: Any) -> str:
    """Validate and hash a complete plan without adding a self-hash."""

    validate_execution_plan_v1(plan)
    return sha256_json(plan)


__all__ = [
    "EXECUTION_PLAN_ID_NAMESPACE",
    "EXECUTION_PLAN_VERSION",
    "EXPANSION_ALGORITHM_ID",
    "PERIODS_PER_BLOCK",
    "RUN_SLOT_ID_NAMESPACE",
    "execution_plan_id",
    "execution_plan_sha256",
    "materialize_execution_plan_v1",
    "run_slot_id",
    "validate_execution_plan_v1",
]
