"""Minimal fail-closed runtime adapter for Phase 19 paired execution units.

The module materializes immutable paired units from a validated execution plan
and manages isolated attempt records.  It never imports scenarios or backends.
Actual scenario execution is available only through an explicitly injected
executor, which keeps structural tests independent from experimental runtime.
"""

from __future__ import annotations

import copy
import contextlib
import datetime as dt
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import subprocess
from typing import Any, Callable, Iterator, Mapping, NoReturn, Sequence

from jsonschema import Draft202012Validator, FormatChecker

from l2i.execution_plan import execution_plan_sha256, validate_execution_plan_v1
from l2i.experiment_contract import (
    ExperimentContractError,
    generate_execution_id,
    sha256_bytes,
    sha256_json,
    utc_rfc3339,
)


MATERIALIZED_UNIT_VERSION = "phase19-materialized-experimental-unit-v1"
ATTEMPT_VERSION = "phase19-execution-attempt-v1"
SUPPORTED_TUPLE = ("pilot", "RQ3", "S1", "mock")
ATTEMPT_STATES = frozenset(
    {"MATERIALIZED", "PREFLIGHT_PASSED", "RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED"}
)
TERMINAL_ATTEMPT_STATES = frozenset({"SUCCEEDED", "FAILED", "INTERRUPTED"})

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ATTEMPT_DIRECTORY = re.compile(r"^attempt-([0-9]{6,})$")
_RUNTIME_FIELDS = frozenset(
    {
        "spec_path",
        "duration_s",
        "flow_mbps",
        "best_effort_mbps",
        "bandwidths_mbps",
        "delay_ms",
        "rtt_interval_ms",
        "rtt_samples",
        "bandwidth_tolerance_mbps",
    }
)
_UNIT_FIELDS = frozenset(
    {
        "contract_version",
        "execution_plan_id",
        "execution_plan_sha256",
        "repository_commit",
        "campaign_id",
        "randomization_manifest_id",
        "randomization_manifest_sha256",
        "campaign_stage",
        "rq_id",
        "scenario_id",
        "backend_mode",
        "block_id",
        "block_sequence_index",
        "order",
        "repetition",
        "configuration_id",
        "profile_id",
        "source_artifacts",
        "runtime_parameters",
        "slots",
        "materialized_unit_sha256",
    }
)
_ATTEMPT_FIELDS = frozenset(
    {
        "contract_version",
        "materialized_unit_sha256",
        "campaign_id",
        "execution_plan_id",
        "block_id",
        "run_slot_id",
        "execution_id",
        "attempt_number",
        "state",
        "provenance",
        "timestamps",
        "invocation",
        "results_directory",
        "native_results_root",
        "outcome",
    }
)
_TRANSITIONS = {
    "MATERIALIZED": frozenset({"PREFLIGHT_PASSED"}),
    "PREFLIGHT_PASSED": frozenset({"RUNNING"}),
    "RUNNING": TERMINAL_ATTEMPT_STATES,
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
    "INTERRUPTED": frozenset(),
}

_SCENARIO_SPEC_ROLE = "scenario_spec"
_VERIFIED_SPEC_PLACEHOLDER = "<VERIFIED_SCENARIO_SPEC_FD>"
_VERIFIED_RESULTS_ROOT_PLACEHOLDER = "<VERIFIED_NATIVE_RESULTS_ROOT_FD>"
_UTC_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_ATTEMPT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "schemas/phase19/execution-attempt-v1.schema.json"
)
_RUNNING_AUTHORITY_ISSUER = object()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen = _FrozenDict()
        for key, item in value.items():
            dict.__setitem__(frozen, key, _freeze(item))
        return frozen
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


class _FrozenDict(dict[str, Any]):
    """JSON-serializable recursively immutable mapping."""

    def _immutable(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        _fail("IMMUTABLE_MATERIALIZED_UNIT", "materialized units cannot be mutated")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __copy__(self) -> "_FrozenDict":
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> "_FrozenDict":
        return self

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self)


class RuntimeAdapterError(ExperimentContractError):
    """Raised when materialization or attempt handling fails closed."""


def _fail(code: str, message: str) -> NoReturn:
    raise RuntimeAdapterError(f"{code}: {message}")


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        _fail("UNSAFE_IDENTIFIER", f"{field} must be a path-safe identifier")
    return value


def _positive_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        _fail("INVALID_RUNTIME_PARAMETERS", f"{field} must be a positive integer")
    return value


def _utc_timestamp(value: Any, field: str) -> str:
    """Require the contract's exact RFC 3339 UTC representation."""

    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        _fail("INVALID_ATTEMPT_TIMESTAMP", f"{field} must use YYYY-MM-DDTHH:MM:SS.ffffffZ")
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.UTC)
    except ValueError:
        _fail("INVALID_ATTEMPT_TIMESTAMP", f"{field} is not a valid UTC timestamp")
    if utc_rfc3339(parsed) != value:
        _fail("INVALID_ATTEMPT_TIMESTAMP", f"{field} is not canonical UTC")
    return value


def _finite_number(value: Any, field: str, *, positive: bool) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _fail("INVALID_RUNTIME_PARAMETERS", f"{field} must be numeric")
    number = float(value)
    if not (number == number and abs(number) != float("inf")):
        _fail("INVALID_RUNTIME_PARAMETERS", f"{field} must be finite")
    if positive and number <= 0:
        _fail("INVALID_RUNTIME_PARAMETERS", f"{field} must be positive")
    if not positive and number < 0:
        _fail("INVALID_RUNTIME_PARAMETERS", f"{field} must be nonnegative")
    return number


def _safe_relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("UNSAFE_PATH", f"{field} must be a nonempty relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _fail("UNSAFE_PATH", f"{field} must not be absolute or contain dot segments")
    if "\\" in value or any(ord(character) < 32 for character in value):
        _fail("UNSAFE_PATH", f"{field} contains unsafe characters")
    return value


def validate_runtime_parameters(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("INVALID_RUNTIME_PARAMETERS", "runtime_parameters must be an object")
    unknown = sorted(set(value).difference(_RUNTIME_FIELDS))
    missing = sorted(_RUNTIME_FIELDS.difference(value))
    if unknown or missing:
        _fail(
            "INVALID_RUNTIME_PARAMETERS",
            f"unknown={unknown!r}, missing={missing!r}",
        )
    bandwidths = value["bandwidths_mbps"]
    if not isinstance(bandwidths, Mapping) or set(bandwidths) != {"A", "B", "C"}:
        _fail("INVALID_RUNTIME_PARAMETERS", "bandwidths_mbps must contain exactly A, B, C")
    rtt_samples = value["rtt_samples"]
    if rtt_samples is not None:
        rtt_samples = _positive_integer(rtt_samples, "rtt_samples")
        if rtt_samples < 2:
            _fail("INVALID_RUNTIME_PARAMETERS", "rtt_samples must be at least 2")
    duration = _positive_integer(value["duration_s"], "duration_s")
    if duration < 2:
        _fail("INVALID_RUNTIME_PARAMETERS", "duration_s must be at least 2")
    interval = _positive_integer(value["rtt_interval_ms"], "rtt_interval_ms")
    if interval < 10:
        _fail("INVALID_RUNTIME_PARAMETERS", "rtt_interval_ms must be at least 10")
    return {
        "spec_path": _safe_relative_path(value["spec_path"], "spec_path"),
        "duration_s": duration,
        "flow_mbps": _finite_number(value["flow_mbps"], "flow_mbps", positive=True),
        "best_effort_mbps": _finite_number(
            value["best_effort_mbps"], "best_effort_mbps", positive=True
        ),
        "bandwidths_mbps": {
            domain: _finite_number(bandwidths[domain], f"bandwidths_mbps.{domain}", positive=True)
            for domain in ("A", "B", "C")
        },
        "delay_ms": _finite_number(value["delay_ms"], "delay_ms", positive=False),
        "rtt_interval_ms": interval,
        "rtt_samples": rtt_samples,
        "bandwidth_tolerance_mbps": _finite_number(
            value["bandwidth_tolerance_mbps"],
            "bandwidth_tolerance_mbps",
            positive=False,
        ),
    }


def _scenario_spec_from_sources(configurations: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    matches: list[dict[str, str]] = []
    for configuration in configurations:
        for source in configuration["source_artifacts"]:
            if source.get("role") == _SCENARIO_SPEC_ROLE:
                matches.append(
                    {
                        "role": source["role"],
                        "path": _safe_relative_path(source["path"], "scenario_spec.path"),
                        "sha256": source["sha256"],
                    }
                )
    if len(matches) != 1:
        _fail(
            "SCENARIO_SPEC_CARDINALITY",
            f"expected exactly one scenario_spec globally, observed {len(matches)}",
        )
    if not _SHA256.fullmatch(matches[0]["sha256"]):
        _fail("INVALID_SCENARIO_SPEC_HASH", "scenario_spec.sha256 is malformed")
    return matches[0]


def _scenario_spec_from_unit(unit: Mapping[str, Any]) -> dict[str, str]:
    return _scenario_spec_from_sources(
        [{"source_artifacts": list(unit["source_artifacts"])}]
    )


def _require_supported_plan(plan: Mapping[str, Any]) -> None:
    actual = (
        plan["campaign_stage"],
        plan["rq_id"],
        plan["scenario_id"],
        plan["backend_mode"],
    )
    if actual != SUPPORTED_TUPLE:
        _fail(
            "UNSUPPORTED_EXECUTION_TUPLE",
            "this increment supports only pilot/RQ3/S1/mock; " f"received {actual!r}",
        )


def materialized_unit_sha256(unit: Mapping[str, Any]) -> str:
    payload = dict(unit)
    payload.pop("materialized_unit_sha256", None)
    return sha256_json(payload)


def materialize_paired_unit(
    plan: Any,
    *,
    block_id: str,
    repetition: int,
    runtime_parameters: Any,
) -> dict[str, Any]:
    """Materialize one immutable two-slot unit without changing slot order."""

    validate_execution_plan_v1(plan)
    if not isinstance(plan, Mapping):
        _fail("INVALID_EXECUTION_PLAN", "plan must be an object")
    _require_supported_plan(plan)
    selected_block_id = _identifier(block_id, "block_id")
    normalized_repetition = _positive_integer(repetition, "repetition")
    if normalized_repetition != 1:
        _fail("UNSUPPORTED_REPETITION", "this increment supports only repetition 1")
    normalized_runtime = validate_runtime_parameters(runtime_parameters)

    selected = [
        copy.deepcopy(slot)
        for slot in plan["run_slots"]
        if slot["assignment"]["block_id"] == selected_block_id
    ]
    if len(selected) != 2:
        _fail("INCOMPLETE_MATERIALIZED_BLOCK", "selected block must contain exactly two slots")
    if [slot["assignment"]["period"] for slot in selected] != [1, 2]:
        _fail("NONCANONICAL_UNIT_SLOT_SEQUENCE", "selected slots must remain in periods 1, 2")
    if selected != sorted(selected, key=lambda item: item["slot_index"]):
        _fail("NONCANONICAL_UNIT_SLOT_SEQUENCE", "slot order must match the validated plan")

    manifest = plan["randomization_manifest"]
    scenario_spec = _scenario_spec_from_sources(manifest["configurations"])
    if normalized_runtime["spec_path"] != scenario_spec["path"]:
        _fail(
            "SCENARIO_SPEC_ASSERTION_MISMATCH",
            "runtime_parameters.spec_path must equal the normative scenario_spec path",
        )
    block = next((item for item in manifest["blocks"] if item["block_id"] == selected_block_id), None)
    if block is None:
        _fail("UNKNOWN_BLOCK", "block_id does not exist in the embedded manifest")
    configuration = next(
        (
            item
            for item in manifest["configurations"]
            if item["configuration_id"] == block["configuration_id"]
        ),
        None,
    )
    if configuration is None:
        _fail("UNKNOWN_CONFIGURATION", "block configuration is missing from the manifest")

    slots: list[dict[str, Any]] = []
    seed = manifest["randomization"]["seed_hex"]
    for slot in selected:
        assignment = slot["assignment"]
        expected_treatment = {"A": "baseline", "B": "adapt"}[assignment["arm"]]
        if assignment["treatment"] != expected_treatment:
            _fail("ARM_TREATMENT_MISMATCH", "RQ3 treatment must be derived from arm")
        slots.append(
            {
                "slot_index": slot["slot_index"],
                "run_slot_id": slot["run_slot_id"],
                "assignment_sha256": sha256_json(assignment),
                "period": assignment["period"],
                "arm": assignment["arm"],
                "treatment": assignment["treatment"],
                "randomization_seed_hex": seed,
                "assignment": copy.deepcopy(assignment),
            }
        )

    all_sources = sorted(
        (
            copy.deepcopy(source)
            for item in manifest["configurations"]
            for source in item["source_artifacts"]
        ),
        key=lambda source: (source["path"], source["sha256"], source["role"]),
    )
    unit: dict[str, Any] = {
        "contract_version": MATERIALIZED_UNIT_VERSION,
        "execution_plan_id": plan["execution_plan_id"],
        "execution_plan_sha256": execution_plan_sha256(plan),
        "repository_commit": plan["repository_commit"],
        "campaign_id": plan["campaign_id"],
        "randomization_manifest_id": plan["randomization_manifest_id"],
        "randomization_manifest_sha256": plan["randomization_manifest_sha256"],
        "campaign_stage": plan["campaign_stage"],
        "rq_id": plan["rq_id"],
        "scenario_id": plan["scenario_id"],
        "backend_mode": plan["backend_mode"],
        "block_id": selected_block_id,
        "block_sequence_index": selected[0]["block_sequence_index"],
        "order": block["order"],
        "repetition": normalized_repetition,
        "configuration_id": configuration["configuration_id"],
        "profile_id": configuration["profile_id"],
        "source_artifacts": all_sources,
        "runtime_parameters": normalized_runtime,
        "slots": slots,
    }
    unit["materialized_unit_sha256"] = materialized_unit_sha256(unit)
    return _freeze(unit)


def validate_materialized_unit(unit: Any, plan: Any) -> None:
    if not isinstance(unit, Mapping):
        _fail("INVALID_MATERIALIZED_UNIT", "unit must be an object")
    if set(unit) != _UNIT_FIELDS:
        _fail("INVALID_MATERIALIZED_UNIT", "unit properties do not match the frozen contract")
    if unit["contract_version"] != MATERIALIZED_UNIT_VERSION:
        _fail("UNSUPPORTED_MATERIALIZED_UNIT_VERSION", "contract_version is unsupported")
    digest = unit.get("materialized_unit_sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        _fail("INVALID_MATERIALIZED_UNIT_HASH", "materialized_unit_sha256 is malformed")
    if digest != materialized_unit_sha256(unit):
        _fail("MATERIALIZED_UNIT_HASH_MISMATCH", "unit hash differs from canonical content")
    expected = materialize_paired_unit(
        plan,
        block_id=unit["block_id"],
        repetition=unit["repetition"],
        runtime_parameters=unit["runtime_parameters"],
    )
    if _thaw(unit) != _thaw(expected):
        _fail("MATERIALIZED_UNIT_PLAN_MISMATCH", "unit is not derived exactly from the plan")


def describe_s1_argv(unit: Mapping[str, Any], slot: Mapping[str, Any]) -> list[str]:
    return _s1_argv(
        unit,
        slot,
        execution_id="<EXECUTION_ID>",
        results_root="<ABSOLUTE_RESULTS_ROOT>",
        validate_runtime_values=False,
    )


def build_s1_argv(
    unit: Mapping[str, Any],
    slot: Mapping[str, Any],
    *,
    execution_id: str,
    results_root: Path,
    spec_path: str | None = None,
) -> list[str]:
    _identifier(execution_id, "execution_id")
    normalized_root = validate_results_root(results_root)
    return _s1_argv(
        unit,
        slot,
        execution_id=execution_id,
        results_root=str(normalized_root),
        spec_path=spec_path,
        validate_runtime_values=True,
    )


def _s1_argv(
    unit: Mapping[str, Any],
    slot: Mapping[str, Any],
    *,
    execution_id: str,
    results_root: str,
    validate_runtime_values: bool,
    spec_path: str | None = None,
) -> list[str]:
    if (unit["campaign_stage"], unit["rq_id"], unit["scenario_id"], unit["backend_mode"]) != SUPPORTED_TUPLE:
        _fail("UNSUPPORTED_DISPATCH", "only pilot/RQ3/S1/mock may be described")
    if slot not in unit["slots"]:
        _fail("UNKNOWN_RUN_SLOT", "slot does not belong to the materialized unit")
    expected_mode = {"A": "baseline", "B": "adapt"}.get(slot["arm"])
    if expected_mode is None or slot["treatment"] != expected_mode:
        _fail("UNSUPPORTED_TREATMENT", "S1 mode must be derived from the RQ3 arm")
    runtime = validate_runtime_parameters(unit["runtime_parameters"])
    normative_spec = _scenario_spec_from_unit(unit)
    if runtime["spec_path"] != normative_spec["path"]:
        _fail("SCENARIO_SPEC_ASSERTION_MISMATCH", "runtime spec assertion is not normative")
    selected_spec = spec_path if spec_path is not None else normative_spec["path"]
    if validate_runtime_values:
        _identifier(unit["profile_id"], "profile_id")
    bandwidths = runtime["bandwidths_mbps"]
    argv = [
        "/usr/bin/python3",
        "-m",
        "scenarios.multidomain_s1",
        "--spec",
        selected_spec,
        "--duration",
        str(runtime["duration_s"]),
        "--flow-mbps",
        str(runtime["flow_mbps"]),
        "--be-mbps",
        str(runtime["best_effort_mbps"]),
        "--mode",
        expected_mode,
        "--backend",
        "mock",
        "--bwA",
        str(bandwidths["A"]),
        "--bwB",
        str(bandwidths["B"]),
        "--bwC",
        str(bandwidths["C"]),
        "--delay-ms",
        str(runtime["delay_ms"]),
        "--rtt-interval-ms",
        str(runtime["rtt_interval_ms"]),
        "--bandwidth-tolerance-mbps",
        str(runtime["bandwidth_tolerance_mbps"]),
        "--profile-id",
        unit["profile_id"],
        "--repetition",
        str(unit["repetition"]),
        "--execution-id",
        execution_id,
        "--results-root",
        results_root,
    ]
    if runtime["rtt_samples"] is not None:
        argv.extend(["--rtt-samples", str(runtime["rtt_samples"])])
    return argv


def dry_run_description(unit: Mapping[str, Any], plan: Any) -> dict[str, Any]:
    validate_materialized_unit(unit, plan)
    return {
        "dry_run": True,
        "materialized_unit_sha256": unit["materialized_unit_sha256"],
        "block_id": unit["block_id"],
        "order": unit["order"],
        "slots": [
            {
                "slot_index": slot["slot_index"],
                "run_slot_id": slot["run_slot_id"],
                "period": slot["period"],
                "arm": slot["arm"],
                "treatment": slot["treatment"],
                "argv": describe_s1_argv(unit, slot),
            }
            for slot in unit["slots"]
        ],
    }


def capture_pre_execution_provenance(repository: Path) -> dict[str, Any]:
    """Capture local provenance without network access."""

    root = repository.resolve()

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            _fail("PROVENANCE_CAPTURE_FAILED", (completed.stderr or completed.stdout).strip())
        return completed.stdout.strip()

    commit = git("rev-parse", "HEAD")
    return {
        "commit": commit,
        "tree": git("rev-parse", "HEAD^{tree}"),
        "branch": git("branch", "--show-current") or "DETACHED",
        "worktree_clean": git("status", "--porcelain") == "",
        "origin_commit": git("rev-parse", "origin/develop"),
        "hostname": socket.gethostname(),
        "user": getpass.getuser(),
    }


def validate_pre_execution_provenance(provenance: Any, unit: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "commit",
        "tree",
        "branch",
        "worktree_clean",
        "origin_commit",
        "hostname",
        "user",
    }
    if not isinstance(provenance, Mapping) or set(provenance) != required:
        _fail("INVALID_PROVENANCE", "provenance fields do not match the frozen envelope")
    if not isinstance(provenance["commit"], str) or not _COMMIT.fullmatch(provenance["commit"]):
        _fail("INVALID_PROVENANCE", "commit must be lowercase Git SHA-1")
    if not isinstance(provenance["tree"], str) or not _COMMIT.fullmatch(provenance["tree"]):
        _fail("INVALID_PROVENANCE", "tree must be lowercase Git SHA-1")
    if provenance["branch"] != "develop" or provenance["worktree_clean"] is not True:
        _fail("INCOMPATIBLE_PROVENANCE", "execution requires a clean develop worktree")
    if provenance["origin_commit"] != provenance["commit"]:
        _fail("INCOMPATIBLE_PROVENANCE", "HEAD must equal origin/develop")
    if provenance["commit"] != unit["repository_commit"]:
        _fail("INCOMPATIBLE_PROVENANCE", "execution commit must equal the plan repository_commit")
    _identifier(provenance["hostname"], "hostname")
    _identifier(provenance["user"], "user")
    return dict(provenance)


def validate_results_root(results_root: Path) -> Path:
    raw = Path(results_root)
    if not raw.is_absolute() or ".." in raw.parts:
        _fail("UNSAFE_RESULTS_ROOT", "results_root must be an absolute path without traversal")
    if raw == Path(raw.anchor):
        _fail("UNSAFE_RESULTS_ROOT", "filesystem root is not an allowed results_root")
    descriptor = _open_absolute_directory(raw)
    os.close(descriptor)
    return raw


def _open_absolute_directory(path: Path) -> int:
    """Open an existing absolute directory without following any symlink component."""

    raw = Path(path)
    if not raw.is_absolute():
        _fail("UNSAFE_PATH", "directory must be absolute")
    descriptor = os.open("/", os.O_RDONLY | _DIRECTORY)
    try:
        for component in raw.parts[1:]:
            if not component or component in {".", ".."}:
                _fail("UNSAFE_PATH", "directory contains an unsafe component")
            child = os.open(
                component,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            _fail("UNSAFE_PATH", "opened path is not a directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _invalid_attempt_reservation(message: str) -> NoReturn:
    _fail("INVALID_ATTEMPT_RESERVATION", message)


def _running_requires_dispatch_authority(message: str) -> NoReturn:
    _fail("RUNNING_REQUIRES_DISPATCH_AUTHORITY", message)


def _validate_reservation_capability(
    directory_fd: int,
    reservation_fd: int,
    reservation_name: str,
    expected_content: bytes,
) -> None:
    """Validate an opened reservation and its current authoritative directory entry."""

    try:
        opened = os.fstat(reservation_fd)
        current = os.stat(reservation_name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
            _invalid_attempt_reservation("reservation marker must be a regular file")
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            _invalid_attempt_reservation("reservation marker was replaced")
        if opened.st_size != len(expected_content):
            _invalid_attempt_reservation("reservation marker content is inconsistent")
        os.lseek(reservation_fd, 0, os.SEEK_SET)
        observed = os.read(reservation_fd, len(expected_content) + 1)
    except RuntimeAdapterError:
        raise
    except OSError as exc:
        _invalid_attempt_reservation(str(exc))
    if observed != expected_content:
        _invalid_attempt_reservation("reservation marker content is inconsistent")


class _RunningDispatchAuthority:
    """Opaque, live authority binding one validated reservation to RUNNING."""

    __slots__ = (
        "_active",
        "_execution_id",
        "_expected_content",
        "_issuer",
        "_reservation_fd",
        "_reservation_name",
        "_root_fd",
        "_root_identity",
    )

    def __init__(
        self,
        *,
        issuer: object,
        execution_id: str,
        root_fd: int,
        reservation_fd: int,
        reservation_name: str,
        expected_content: bytes,
    ) -> None:
        if issuer is not _RUNNING_AUTHORITY_ISSUER:
            _running_requires_dispatch_authority("authority issuer is invalid")
        root_metadata = os.fstat(root_fd)
        self._issuer = issuer
        self._execution_id = execution_id
        self._root_fd = root_fd
        self._root_identity = (root_metadata.st_dev, root_metadata.st_ino)
        self._reservation_fd = reservation_fd
        self._reservation_name = reservation_name
        self._expected_content = expected_content
        self._active = True

    def _validate_for(self, record: Mapping[str, Any], results_root: Path) -> None:
        if self._issuer is not _RUNNING_AUTHORITY_ISSUER or not self._active:
            _running_requires_dispatch_authority("dispatch authority is not active")
        if record.get("execution_id") != self._execution_id:
            _running_requires_dispatch_authority("authority belongs to another execution")
        current_root_fd = -1
        try:
            current_root_fd = _open_absolute_directory(validate_results_root(results_root))
            current_root = os.fstat(current_root_fd)
            held_root = os.fstat(self._root_fd)
        except OSError as exc:
            _running_requires_dispatch_authority(str(exc))
        finally:
            if current_root_fd >= 0:
                os.close(current_root_fd)
        current_identity = (current_root.st_dev, current_root.st_ino)
        held_identity = (held_root.st_dev, held_root.st_ino)
        if current_identity != self._root_identity or held_identity != self._root_identity:
            _running_requires_dispatch_authority("authority belongs to another results root")
        _validate_reservation_capability(
            self._root_fd,
            self._reservation_fd,
            self._reservation_name,
            self._expected_content,
        )

    def _deactivate(self) -> None:
        self._active = False


def _require_running_dispatch_authority(
    authority: Any, record: Mapping[str, Any], results_root: Path
) -> None:
    if not isinstance(authority, _RunningDispatchAuthority):
        _running_requires_dispatch_authority("a live reservation capability is required")
    try:
        authority._validate_for(record, results_root)
    except (AttributeError, TypeError):
        _running_requires_dispatch_authority("dispatch authority is malformed")


@contextlib.contextmanager
def _verified_attempt_reservation(
    results_root: Path, execution_id: str
) -> Iterator[_RunningDispatchAuthority]:
    """Hold and revalidate the global reservation capability for one dispatch."""

    normalized_execution_id = _identifier(execution_id, "execution_id")
    root = validate_results_root(results_root)
    reservation_name = f"phase19-execution-id-{normalized_execution_id}"
    expected_content = (normalized_execution_id + "\n").encode("ascii")
    root_fd = _open_absolute_directory(root)
    reservation_fd = -1
    authority: _RunningDispatchAuthority | None = None
    try:
        try:
            reservation_fd = os.open(
                reservation_name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                dir_fd=root_fd,
            )
        except OSError as exc:
            _invalid_attempt_reservation(str(exc))

        _validate_reservation_capability(
            root_fd, reservation_fd, reservation_name, expected_content
        )
        authority = _RunningDispatchAuthority(
            issuer=_RUNNING_AUTHORITY_ISSUER,
            execution_id=normalized_execution_id,
            root_fd=root_fd,
            reservation_fd=reservation_fd,
            reservation_name=reservation_name,
            expected_content=expected_content,
        )
        yield authority
    finally:
        if authority is not None:
            authority._deactivate()
        if reservation_fd >= 0:
            os.close(reservation_fd)
        os.close(root_fd)


def _open_dir_at(parent: int, name: str, *, create: bool) -> int:
    _identifier(name, "directory component")
    if create:
        try:
            os.mkdir(name, 0o755, dir_fd=parent)
        except FileExistsError:
            pass
    descriptor = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=parent)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        _fail("UNSAFE_PATH", f"{name!r} is not a directory")
    return descriptor


def _atomic_write_json_at(
    directory_fd: int,
    filename: str,
    value: Mapping[str, Any],
    *,
    results_root: Path | None = None,
    running_authority: _RunningDispatchAuthority | None = None,
) -> None:
    if value.get("state") == "RUNNING":
        if results_root is None:
            _running_requires_dispatch_authority("RUNNING persistence requires results_root")
        _require_running_dispatch_authority(running_authority, value, results_root)
    _identifier(filename, "sidecar filename")
    temporary = f".{filename}.tmp-{os.getpid()}-{id(value)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
    try:
        payload = json.dumps(_thaw(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        payload = (payload + "\n").encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
        raise


def _read_json_at(directory_fd: int, filename: str) -> dict[str, Any]:
    descriptor = os.open(filename, os.O_RDONLY | _NOFOLLOW, dir_fd=directory_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _fail("INVALID_ATTEMPT_RECORD", "sidecar is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                _fail("INVALID_ATTEMPT_JSON", f"duplicate key: {key}")
            result[key] = item
        return result

    def reject_constant(value: str) -> NoReturn:
        _fail("INVALID_ATTEMPT_JSON", f"non-finite constant: {value}")

    try:
        value = json.loads(
            b"".join(chunks),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        _fail("INVALID_ATTEMPT_RECORD", str(exc))
    if not isinstance(value, dict):
        _fail("INVALID_ATTEMPT_RECORD", "sidecar must be an object")
    return value


def _read_verified_spec(source_root: Path, artifact: Mapping[str, str]) -> bytes:
    root_fd = _open_absolute_directory(source_root)
    current = root_fd
    try:
        parts = PurePosixPath(_safe_relative_path(artifact["path"], "scenario_spec.path")).parts
        for component in parts[:-1]:
            child = os.open(
                component,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                dir_fd=current,
            )
            if current != root_fd:
                os.close(current)
            current = child
        descriptor = os.open(parts[-1], os.O_RDONLY | _NOFOLLOW, dir_fd=current)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                _fail("INVALID_SCENARIO_SPEC", "scenario_spec must be a regular file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(descriptor)
    except OSError as exc:
        _fail("SCENARIO_SPEC_OPEN_FAILED", str(exc))
    finally:
        if current != root_fd:
            os.close(current)
        os.close(root_fd)
    content = b"".join(chunks)
    if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
        _fail("SCENARIO_SPEC_HASH_MISMATCH", "opened scenario_spec bytes do not match the plan")
    return content


@contextlib.contextmanager
def _sealed_verified_spec(source_root: Path, artifact: Mapping[str, str]) -> Iterator[int]:
    content = _read_verified_spec(source_root, artifact)
    if not hasattr(os, "memfd_create"):
        _fail("SEALED_SPEC_UNAVAILABLE", "Linux memfd_create is required")
    descriptor = os.memfd_create("phase19-scenario-spec", getattr(os, "MFD_ALLOW_SEALING", 0))
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        yield descriptor
    finally:
        os.close(descriptor)


def _slot_root(results_root: Path, unit: Mapping[str, Any], run_slot_id: str) -> Path:
    for field in ("campaign_id", "execution_plan_id", "block_id"):
        _identifier(unit[field], field)
    _identifier(run_slot_id, "run_slot_id")
    return (
        results_root
        / unit["campaign_id"]
        / unit["execution_plan_id"]
        / unit["block_id"]
        / run_slot_id
    )


def _next_attempt_number_fd(slot_fd: int) -> int:
    observed: list[tuple[int, str]] = []
    for name in os.listdir(slot_fd):
        match = _ATTEMPT_DIRECTORY.fullmatch(name)
        metadata = os.stat(name, dir_fd=slot_fd, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or match is None:
            _fail("UNEXPECTED_ATTEMPT_ENTRY", f"unexpected entry in slot directory: {name}")
        number = int(match.group(1))
        observed.append((number, name))
    observed.sort(key=lambda item: item[0])
    numbers = [number for number, _name in observed]
    if numbers != list(range(1, len(numbers) + 1)):
        _fail("NONCONTIGUOUS_ATTEMPTS", "attempt directories must be contiguous from 1")
    if observed:
        attempt_fd = _open_dir_at(slot_fd, observed[-1][1], create=False)
        try:
            names = os.listdir(attempt_fd)
            if len(names) != 1:
                _fail("INVALID_ATTEMPT_DIRECTORY", "attempt must contain exactly one execution directory")
            execution_fd = _open_dir_at(attempt_fd, names[0], create=False)
            try:
                previous = _read_json_at(execution_fd, "attempt.json")
                _validate_attempt_layers(previous)
                if previous["state"] not in TERMINAL_ATTEMPT_STATES:
                    _fail("PREVIOUS_ATTEMPT_NOT_TERMINAL", "a new attempt requires a terminal predecessor")
            finally:
                os.close(execution_fd)
        finally:
            os.close(attempt_fd)
    return len(observed) + 1


def _existing_next_attempt(root_fd: int, components: Sequence[str]) -> int:
    current = os.dup(root_fd)
    try:
        for component in components:
            try:
                child = _open_dir_at(current, component, create=False)
            except FileNotFoundError:
                return 1
            os.close(current)
            current = child
        return _next_attempt_number_fd(current)
    finally:
        os.close(current)


def reserve_attempt(
    unit: Mapping[str, Any],
    plan: Any,
    *,
    run_slot_id: str,
    results_root: Path,
    source_root: Path,
    provenance: Any,
    expected_attempt_number: int | None = None,
    execution_id_factory: Callable[[str], str] = generate_execution_id,
    now: Callable[[], dt.datetime] | None = None,
) -> Path:
    """Reserve the next attempt exclusively; no retry is performed automatically."""

    validate_materialized_unit(unit, plan)
    slot = next((item for item in unit["slots"] if item["run_slot_id"] == run_slot_id), None)
    if slot is None:
        _fail("UNKNOWN_RUN_SLOT", "run_slot_id does not belong to the unit")
    normalized_provenance = validate_pre_execution_provenance(provenance, unit)
    root = validate_results_root(results_root)
    scenario_spec = _scenario_spec_from_unit(unit)
    _read_verified_spec(source_root, scenario_spec)
    root_fd = _open_absolute_directory(root)
    execution_id = _identifier(execution_id_factory(unit["scenario_id"]), "execution_id")
    slot_components = (
        unit["campaign_id"],
        unit["execution_plan_id"],
        unit["block_id"],
        run_slot_id,
    )
    attempt_number = _existing_next_attempt(root_fd, slot_components)
    if expected_attempt_number is not None and expected_attempt_number != attempt_number:
        os.close(root_fd)
        _fail(
            "ATTEMPT_NUMBER_MISMATCH",
            f"next attempt is {attempt_number}, received {expected_attempt_number}",
        )
    slot_root = _slot_root(root, unit, run_slot_id)
    attempt_root = slot_root / f"attempt-{attempt_number:06d}"
    execution_root = attempt_root / execution_id
    native_root = execution_root / "native"
    invocation = _s1_argv(
        unit,
        slot,
        execution_id=execution_id,
        results_root=_VERIFIED_RESULTS_ROOT_PLACEHOLDER,
        validate_runtime_values=True,
        spec_path=_VERIFIED_SPEC_PLACEHOLDER,
    )
    instant = utc_rfc3339((now or (lambda: dt.datetime.now(dt.UTC)))())
    record = {
        "contract_version": ATTEMPT_VERSION,
        "materialized_unit_sha256": unit["materialized_unit_sha256"],
        "campaign_id": unit["campaign_id"],
        "execution_plan_id": unit["execution_plan_id"],
        "block_id": unit["block_id"],
        "run_slot_id": run_slot_id,
        "execution_id": execution_id,
        "attempt_number": attempt_number,
        "state": "MATERIALIZED",
        "provenance": normalized_provenance,
        "timestamps": {
            "materialized_at_utc": instant,
            "preflight_passed_at_utc": None,
            "running_at_utc": None,
            "completed_at_utc": None,
        },
        "invocation": invocation,
        "results_directory": str(execution_root),
        "native_results_root": str(native_root),
        "outcome": None,
    }
    _validate_attempt_layers(record)
    # The global exclusive reservation is deliberately the first persistent mutation.
    reservation_name = f"phase19-execution-id-{execution_id}"
    try:
        reservation_fd = os.open(
            reservation_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
            0o600,
            dir_fd=root_fd,
        )
    except FileExistsError:
        os.close(root_fd)
        _fail("DUPLICATE_EXECUTION_ID", "execution_id is already reserved globally")
    reservation_content = (execution_id + "\n").encode("ascii")
    reservation_offset = 0
    while reservation_offset < len(reservation_content):
        reservation_offset += os.write(reservation_fd, reservation_content[reservation_offset:])
    os.fsync(reservation_fd)
    os.close(reservation_fd)
    campaign_fd = plan_fd = block_fd = slot_fd = -1
    try:
        campaign_fd = _open_dir_at(root_fd, unit["campaign_id"], create=True)
        plan_fd = _open_dir_at(campaign_fd, unit["execution_plan_id"], create=True)
        block_fd = _open_dir_at(plan_fd, unit["block_id"], create=True)
        slot_fd = _open_dir_at(block_fd, run_slot_id, create=True)
    finally:
        for descriptor in (block_fd, plan_fd, campaign_fd):
            if descriptor >= 0:
                os.close(descriptor)
    try:
        os.mkdir(f"attempt-{attempt_number:06d}", 0o755, dir_fd=slot_fd)
    except FileExistsError as exc:
        os.close(slot_fd)
        os.close(root_fd)
        _fail("ATTEMPT_ALREADY_EXISTS", str(exc))
    attempt_fd = _open_dir_at(slot_fd, f"attempt-{attempt_number:06d}", create=False)
    os.mkdir(execution_id, 0o755, dir_fd=attempt_fd)
    execution_fd = _open_dir_at(attempt_fd, execution_id, create=False)
    os.mkdir("native", 0o755, dir_fd=execution_fd)
    sidecar = execution_root / "attempt.json"
    _atomic_write_json_at(execution_fd, "attempt.json", record)
    for descriptor in (execution_fd, attempt_fd, slot_fd, root_fd):
        os.close(descriptor)
    return sidecar


def _validate_attempt_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _ATTEMPT_FIELDS:
        _fail("INVALID_ATTEMPT_RECORD", "attempt properties do not match the frozen contract")
    if value["contract_version"] != ATTEMPT_VERSION or value["state"] not in ATTEMPT_STATES:
        _fail("INVALID_ATTEMPT_RECORD", "attempt version or state is invalid")
    timestamps = value["timestamps"]
    timestamp_fields = {
        "materialized_at_utc",
        "preflight_passed_at_utc",
        "running_at_utc",
        "completed_at_utc",
    }
    if not isinstance(timestamps, Mapping) or set(timestamps) != timestamp_fields:
        _fail("INVALID_ATTEMPT_RECORD", "timestamps must be an object")
    _utc_timestamp(timestamps["materialized_at_utc"], "timestamps.materialized_at_utc")
    for name in timestamp_fields - {"materialized_at_utc"}:
        if timestamps[name] is not None:
            _utc_timestamp(timestamps[name], f"timestamps.{name}")
    state = value["state"]
    expected_presence = {
        "MATERIALIZED": (False, False, False, False),
        "PREFLIGHT_PASSED": (True, False, False, False),
        "RUNNING": (True, True, False, False),
        "SUCCEEDED": (True, True, True, True),
        "FAILED": (True, True, True, True),
        "INTERRUPTED": (True, True, True, True),
    }[state]
    observed = tuple(
        timestamps[name] is not None
        for name in ("preflight_passed_at_utc", "running_at_utc", "completed_at_utc")
    ) + (value["outcome"] is not None,)
    if observed != expected_presence:
        _fail("INVALID_ATTEMPT_STATE", "timestamps/outcome are incompatible with state")
    if state == "SUCCEEDED" and value["outcome"]["return_code"] != 0:
        _fail("INVALID_ATTEMPT_OUTCOME", "SUCCEEDED requires return_code 0")
    if state == "FAILED" and value["outcome"]["return_code"] in {None, 0}:
        _fail("INVALID_ATTEMPT_OUTCOME", "FAILED requires a nonzero return_code")
    return value


def _sidecar_parent_fd(sidecar: Path, results_root: Path) -> tuple[int, int]:
    root = validate_results_root(results_root)
    try:
        relative = sidecar.relative_to(root)
    except ValueError:
        _fail("UNSAFE_ATTEMPT_PATH", "sidecar is outside results_root")
    if relative.name != "attempt.json" or any(part in {"", ".", ".."} for part in relative.parts):
        _fail("UNSAFE_ATTEMPT_PATH", "sidecar path is not canonical")
    root_fd = _open_absolute_directory(root)
    current = os.dup(root_fd)
    try:
        for component in relative.parts[:-1]:
            child = _open_dir_at(current, component, create=False)
            os.close(current)
            current = child
        return root_fd, current
    except BaseException:
        os.close(current)
        os.close(root_fd)
        raise


def _read_attempt(sidecar: Path, results_root: Path) -> dict[str, Any]:
    root_fd, parent_fd = _sidecar_parent_fd(sidecar, results_root)
    try:
        record = _read_json_at(parent_fd, "attempt.json")
        return _validate_attempt_layers(record)
    finally:
        os.close(parent_fd)
        os.close(root_fd)


def transition_attempt(
    sidecar: Path,
    new_state: str,
    *,
    results_root: Path,
    return_code: int | None = None,
    artifact_hashes: Mapping[str, str] | None = None,
    readbacks: Sequence[str] | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> dict[str, Any]:
    record = _read_attempt(sidecar, results_root)
    if new_state == "RUNNING":
        _running_requires_dispatch_authority(
            "transition_attempt cannot request RUNNING; use dispatch"
        )
    return _transition_validated_attempt(
        record,
        sidecar,
        new_state,
        results_root=results_root,
        return_code=return_code,
        artifact_hashes=artifact_hashes,
        readbacks=readbacks,
        now=now,
    )


def _transition_validated_attempt(
    validated_record: Mapping[str, Any],
    sidecar: Path,
    new_state: str,
    *,
    results_root: Path,
    return_code: int | None = None,
    artifact_hashes: Mapping[str, str] | None = None,
    readbacks: Sequence[str] | None = None,
    now: Callable[[], dt.datetime] | None = None,
    running_authority: _RunningDispatchAuthority | None = None,
) -> dict[str, Any]:
    """Persist a transition using exactly the record validated by its caller."""

    record = copy.deepcopy(dict(validated_record))
    _validate_attempt_layers(record)
    current = record["state"]
    if new_state not in _TRANSITIONS[current]:
        _fail("ILLEGAL_ATTEMPT_TRANSITION", f"{current} -> {new_state} is forbidden")
    if new_state == "RUNNING":
        _require_running_dispatch_authority(running_authority, record, results_root)
    instant = utc_rfc3339((now or (lambda: dt.datetime.now(dt.UTC)))())
    if new_state == "PREFLIGHT_PASSED":
        record["timestamps"]["preflight_passed_at_utc"] = instant
    elif new_state == "RUNNING":
        record["timestamps"]["running_at_utc"] = instant
    else:
        if (
            return_code is not None
            and (not isinstance(return_code, int) or isinstance(return_code, bool))
        ):
            _fail("INVALID_ATTEMPT_OUTCOME", "return_code must be an integer or null")
        if new_state in TERMINAL_ATTEMPT_STATES and return_code is None:
            _fail("INVALID_ATTEMPT_OUTCOME", f"{new_state} requires an integer return_code")
        if new_state == "SUCCEEDED" and return_code != 0:
            _fail("INVALID_ATTEMPT_OUTCOME", "SUCCEEDED requires return_code 0")
        if new_state == "FAILED" and return_code == 0:
            _fail("INVALID_ATTEMPT_OUTCOME", "FAILED requires a nonzero return_code")
        hashes = dict(artifact_hashes or {})
        if any(not isinstance(value, str) or not _SHA256.fullmatch(value) for value in hashes.values()):
            _fail("INVALID_ATTEMPT_OUTCOME", "artifact hashes must be lowercase SHA-256")
        record["timestamps"]["completed_at_utc"] = instant
        record["outcome"] = {
            "return_code": return_code,
            "artifact_hashes": hashes,
            "readbacks": list(readbacks or []),
        }
    record["state"] = new_state
    _validate_attempt_layers(record)
    root_fd, parent_fd = _sidecar_parent_fd(sidecar, results_root)
    try:
        _atomic_write_json_at(
            parent_fd,
            "attempt.json",
            record,
            results_root=results_root,
            running_authority=running_authority,
        )
    finally:
        os.close(parent_fd)
        os.close(root_fd)
    return record


def _final_preexecution_checkpoint(
    sidecar: Path,
    *,
    plan: Any,
    unit: Mapping[str, Any],
    results_root: Path,
    expected_provenance: Mapping[str, Any],
    expected_record: Mapping[str, Any],
    expected_invocation: Sequence[str],
) -> tuple[dict[str, Any], list[str]]:
    """Re-read and fully validate the untrusted sidecar immediately before RUNNING."""

    current = _read_attempt(sidecar, results_root)
    if current["state"] != "PREFLIGHT_PASSED":
        _fail("ATTEMPT_NOT_READY", "attempt must remain PREFLIGHT_PASSED before dispatch")
    _slot, canonical_invocation = _validate_attempt_against_context(
        current,
        unit,
        plan,
        sidecar=sidecar,
        results_root=validate_results_root(results_root),
    )
    if current["provenance"] != expected_provenance:
        _fail("ATTEMPT_PROVENANCE_MISMATCH", "sidecar provenance changed before dispatch")
    if current != expected_record:
        _fail("ATTEMPT_CHANGED_BEFORE_RUNNING", "sidecar changed after contextual validation")
    if canonical_invocation != list(expected_invocation):
        _fail("ATTEMPT_INVOCATION_MISMATCH", "canonical invocation changed before dispatch")
    return current, canonical_invocation


def _validate_attempt_schema(record: Mapping[str, Any]) -> None:
    """Apply the adapter-owned normative structural schema independent of cwd/sidecar."""

    try:
        schema = json.loads(_ATTEMPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail("ATTEMPT_SCHEMA_UNAVAILABLE", str(exc))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(record),
        key=lambda error: list(error.path),
    )
    if errors:
        _fail("INVALID_ATTEMPT_SCHEMA", errors[0].message)


def _validate_attempt_timestamp_values(value: Any) -> None:
    """Classify recognizable timestamp violations before structural schema errors.

    This layer deliberately makes no structural acceptance decision. Missing or
    malformed containers continue to be classified by the complete normative
    schema; only present, non-null values of the four contractual timestamp
    fields receive their stable semantic error code here.
    """

    if not isinstance(value, Mapping):
        return
    timestamps = value.get("timestamps")
    if not isinstance(timestamps, Mapping):
        return
    for name in (
        "materialized_at_utc",
        "preflight_passed_at_utc",
        "running_at_utc",
        "completed_at_utc",
    ):
        if name in timestamps and timestamps[name] is not None:
            _utc_timestamp(timestamps[name], f"timestamps.{name}")


def _validate_attempt_layers(value: Any) -> dict[str, Any]:
    """Apply stable temporal classification, full schema, then runtime semantics."""

    _validate_attempt_timestamp_values(value)
    _validate_attempt_schema(value)
    return _validate_attempt_record(value)


def _validate_attempt_against_context(
    record: Mapping[str, Any],
    unit: Mapping[str, Any],
    plan: Any,
    *,
    sidecar: Path,
    results_root: Path,
) -> tuple[dict[str, Any], list[str]]:
    validate_materialized_unit(unit, plan)
    slot = next(
        (item for item in unit["slots"] if item["run_slot_id"] == record["run_slot_id"]),
        None,
    )
    if slot is None:
        _fail("ATTEMPT_CONTEXT_MISMATCH", "run_slot_id is not present in the unit")
    expected_scalars = {
        "materialized_unit_sha256": unit["materialized_unit_sha256"],
        "campaign_id": unit["campaign_id"],
        "execution_plan_id": unit["execution_plan_id"],
        "block_id": unit["block_id"],
    }
    if any(record[key] != value for key, value in expected_scalars.items()):
        _fail("ATTEMPT_CONTEXT_MISMATCH", "sidecar identity differs from the normative unit")
    expected_root = (
        results_root
        / unit["campaign_id"]
        / unit["execution_plan_id"]
        / unit["block_id"]
        / record["run_slot_id"]
        / f"attempt-{record['attempt_number']:06d}"
        / record["execution_id"]
    )
    if sidecar != expected_root / "attempt.json":
        _fail("ATTEMPT_PATH_MISMATCH", "sidecar path differs from its normative identity")
    if record["results_directory"] != str(expected_root):
        _fail("ATTEMPT_PATH_MISMATCH", "results_directory differs from its normative identity")
    native_root = expected_root / "native"
    if record["native_results_root"] != str(native_root):
        _fail("ATTEMPT_PATH_MISMATCH", "native_results_root differs from its normative identity")
    expected_invocation = _s1_argv(
        unit,
        slot,
        execution_id=record["execution_id"],
        results_root=_VERIFIED_RESULTS_ROOT_PLACEHOLDER,
        validate_runtime_values=True,
        spec_path=_VERIFIED_SPEC_PLACEHOLDER,
    )
    if record["invocation"] != expected_invocation:
        _fail("ATTEMPT_INVOCATION_MISMATCH", "persisted invocation is not canonical")
    return slot, expected_invocation


@contextlib.contextmanager
def _verified_native_results_root(path: Path) -> Iterator[int]:
    """Hold a no-follow capability for the validated native results directory."""

    descriptor = _open_absolute_directory(path)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            _fail("INVALID_NATIVE_RESULTS_ROOT", "native_results_root must be a directory")
        capability = f"/proc/self/fd/{descriptor}"
        try:
            capability_metadata = os.stat(capability)
        except OSError as exc:
            _fail("DIRECTORY_CAPABILITY_UNAVAILABLE", str(exc))
        if (metadata.st_dev, metadata.st_ino) != (
            capability_metadata.st_dev,
            capability_metadata.st_ino,
        ):
            _fail("DIRECTORY_CAPABILITY_MISMATCH", "descriptor capability changed identity")
        yield descriptor
    finally:
        os.close(descriptor)


def _bind_verified_descriptors(
    logical_invocation: Sequence[str], *, spec_fd: int, results_fd: int
) -> list[str]:
    """Bind the two canonical logical placeholders to already verified capabilities."""

    bound = list(logical_invocation)
    bindings = (
        ("--spec", _VERIFIED_SPEC_PLACEHOLDER, f"/proc/self/fd/{spec_fd}"),
        (
            "--results-root",
            _VERIFIED_RESULTS_ROOT_PLACEHOLDER,
            f"/proc/self/fd/{results_fd}",
        ),
    )
    for option, placeholder, capability in bindings:
        if bound.count(option) != 1 or bound.count(placeholder) != 1:
            _fail("INVALID_DESCRIPTOR_BINDING", f"{option} placeholder is not canonical")
        index = bound.index(option)
        if index + 1 >= len(bound) or bound[index + 1] != placeholder:
            _fail("INVALID_DESCRIPTOR_BINDING", f"{option} is not bound to its placeholder")
        bound[index + 1] = capability
    if _VERIFIED_SPEC_PLACEHOLDER in bound or _VERIFIED_RESULTS_ROOT_PLACEHOLDER in bound:
        _fail("INVALID_DESCRIPTOR_BINDING", "an unbound descriptor placeholder remains")
    return bound


def run_attempt_with_executor(
    sidecar: Path,
    *,
    executor: Callable[..., Any],
    cwd: Path,
    plan: Any,
    unit: Mapping[str, Any],
    results_root: Path,
    source_root: Path,
    provenance_provider: Callable[[Path], Mapping[str, Any]] = capture_pre_execution_provenance,
) -> dict[str, Any]:
    """Dispatch through an injected subprocess-compatible executor.

    The attempt must already have passed preflight.  RUNNING is persisted before
    the injected callable is invoked.
    """

    record = _read_attempt(sidecar, results_root)
    if record["state"] != "PREFLIGHT_PASSED":
        _fail("ATTEMPT_NOT_READY", "attempt must be PREFLIGHT_PASSED before dispatch")
    _slot, canonical_invocation = _validate_attempt_against_context(
        record, unit, plan, sidecar=sidecar, results_root=validate_results_root(results_root)
    )
    cwd_fd = _open_absolute_directory(cwd)
    source_fd = _open_absolute_directory(source_root)
    try:
        cwd_meta = os.fstat(cwd_fd)
        source_meta = os.fstat(source_fd)
        if (cwd_meta.st_dev, cwd_meta.st_ino) != (source_meta.st_dev, source_meta.st_ino):
            _fail("EXECUTION_CWD_MISMATCH", "cwd must be the verified source_root")
    finally:
        os.close(source_fd)
        os.close(cwd_fd)
    observed_provenance = validate_pre_execution_provenance(provenance_provider(cwd), unit)
    if observed_provenance != record["provenance"]:
        _fail("ATTEMPT_PROVENANCE_MISMATCH", "current provenance differs from the sidecar")
    scenario_spec = _scenario_spec_from_unit(unit)
    with _verified_attempt_reservation(
        results_root, record["execution_id"]
    ) as running_authority:
        with _sealed_verified_spec(source_root, scenario_spec) as spec_fd:
            with _verified_native_results_root(Path(record["native_results_root"])) as results_fd:
                executable_invocation = _bind_verified_descriptors(
                    canonical_invocation, spec_fd=spec_fd, results_fd=results_fd
                )
                final_record, final_invocation = _final_preexecution_checkpoint(
                    sidecar,
                    plan=plan,
                    unit=unit,
                    results_root=results_root,
                    expected_provenance=observed_provenance,
                    expected_record=record,
                    expected_invocation=canonical_invocation,
                )
                if final_invocation != canonical_invocation:
                    _fail("ATTEMPT_INVOCATION_MISMATCH", "final invocation is not canonical")
                _transition_validated_attempt(
                    final_record,
                    sidecar,
                    "RUNNING",
                    results_root=results_root,
                    running_authority=running_authority,
                )
                try:
                    completed = executor(
                        executable_invocation,
                        cwd=str(cwd),
                        check=False,
                        capture_output=True,
                        text=True,
                        pass_fds=(spec_fd, results_fd),
                    )
                except (KeyboardInterrupt, SystemExit):
                    return transition_attempt(
                        sidecar, "INTERRUPTED", results_root=results_root, return_code=130
                    )
                except BaseException:
                    return transition_attempt(
                        sidecar, "FAILED", results_root=results_root, return_code=1
                    )
        stdout = str(getattr(completed, "stdout", "") or "")
        stderr = str(getattr(completed, "stderr", "") or "")
        return_code = int(getattr(completed, "returncode"))
        hashes = {
            "stdout": sha256_bytes(stdout.encode("utf-8")),
            "stderr": sha256_bytes(stderr.encode("utf-8")),
        }
        terminal = "SUCCEEDED" if return_code == 0 else "FAILED"
        return transition_attempt(
            sidecar,
            terminal,
            results_root=results_root,
            return_code=return_code,
            artifact_hashes=hashes,
        )


__all__ = [
    "ATTEMPT_STATES",
    "ATTEMPT_VERSION",
    "MATERIALIZED_UNIT_VERSION",
    "RuntimeAdapterError",
    "build_s1_argv",
    "capture_pre_execution_provenance",
    "describe_s1_argv",
    "dry_run_description",
    "materialize_paired_unit",
    "materialized_unit_sha256",
    "reserve_attempt",
    "run_attempt_with_executor",
    "transition_attempt",
    "validate_materialized_unit",
    "validate_pre_execution_provenance",
    "validate_results_root",
    "validate_runtime_parameters",
]
