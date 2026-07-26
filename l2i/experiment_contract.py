"""Canonical experiment identity, provenance, and artifact utilities.

This module defines the minimum persistence contract that the final S1 and S2
campaigns must share.  It deliberately does not implement scenario semantics,
traffic generation, backend application, or statistical analysis.  Those
responsibilities remain with the canonical scenario and analysis layers.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence


_EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MODE_VALUES = frozenset({"baseline", "adapt"})
_BACKEND_MODE_VALUES = frozenset({"mock", "real"})


class ExperimentContractError(RuntimeError):
    """Raised when an experiment violates the canonical persistence contract."""


def utc_now() -> dt.datetime:
    """Return a timezone-aware UTC timestamp."""

    return dt.datetime.now(dt.UTC)


def utc_rfc3339(value: dt.datetime | None = None) -> str:
    """Serialize one timestamp in UTC with microsecond precision."""

    current = value or utc_now()
    if current.tzinfo is None:
        raise ExperimentContractError("timestamp must be timezone-aware")
    normalized = current.astimezone(dt.UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def generate_execution_id(scenario_id: str) -> str:
    """Generate a collision-resistant execution identifier.

    The value combines a microsecond UTC timestamp with a random suffix.  The
    scenario identifier is included to keep artifacts understandable outside
    their original directory hierarchy.
    """

    normalized_scenario = _normalize_identifier(scenario_id, "scenario_id")
    stamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    suffix = uuid.uuid4().hex[:12]
    return f"{normalized_scenario}-{stamp}-{suffix}"


def canonical_json_bytes(value: Any) -> bytes:
    """Encode JSON deterministically for hashing and integrity checks."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    """Return a lowercase SHA-256 hexadecimal digest."""

    return hashlib.sha256(payload).hexdigest()


def sha256_json(value: Any) -> str:
    """Hash a JSON-compatible value using canonical serialization."""

    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Path) -> str:
    """Hash one file without loading it entirely into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace one UTF-8 text artifact in its target directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    """Atomically write a readable JSON artifact with a final newline."""

    serialized = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )
    atomic_write_text(path, serialized + "\n")


def _normalize_identifier(value: str, field_name: str) -> str:
    """Validate an identifier used in paths and manifests."""

    normalized = str(value).strip()
    if not _EXECUTION_ID_PATTERN.fullmatch(normalized):
        raise ExperimentContractError(
            f"{field_name} must match {_EXECUTION_ID_PATTERN.pattern!r}: {value!r}"
        )
    return normalized


def _run_git(repository: Path, arguments: Sequence[str]) -> str:
    """Run one read-only Git command and return trimmed stdout."""

    completed = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repository}",
            "-C",
            str(repository),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "git command failed").strip()
        raise ExperimentContractError(message)
    return completed.stdout.strip()


@dataclasses.dataclass(frozen=True)
class RepositoryProvenance:
    """Immutable Git provenance attached to every canonical execution."""

    commit: str
    branch: str
    worktree_clean: bool
    origin_commit: str | None
    describe: str

    @classmethod
    def capture(cls, repository: Path) -> "RepositoryProvenance":
        """Capture repository state without changing files or contacting remotes."""

        root = repository.resolve()
        commit = _run_git(root, ["rev-parse", "HEAD"])
        branch = _run_git(root, ["branch", "--show-current"]) or "DETACHED"
        status = _run_git(root, ["status", "--porcelain"])
        describe = _run_git(root, ["describe", "--always", "--dirty", "--tags"])

        origin_commit: str | None
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={root}",
                "-C",
                str(root),
                "rev-parse",
                "origin/develop",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        origin_commit = completed.stdout.strip() if completed.returncode == 0 else None

        return cls(
            commit=commit,
            branch=branch,
            worktree_clean=(status == ""),
            origin_commit=origin_commit,
            describe=describe,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""

        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExperimentIdentity:
    """Identity fields required for every S1 or S2 execution."""

    scenario_id: str
    execution_id: str
    profile_id: str
    mode: str
    backend_mode: str
    repetition: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "scenario_id",
            _normalize_identifier(self.scenario_id, "scenario_id"),
        )
        object.__setattr__(
            self,
            "execution_id",
            _normalize_identifier(self.execution_id, "execution_id"),
        )
        object.__setattr__(
            self,
            "profile_id",
            _normalize_identifier(self.profile_id, "profile_id"),
        )
        if self.mode not in _MODE_VALUES:
            raise ExperimentContractError(f"unsupported mode: {self.mode!r}")
        if self.backend_mode not in _BACKEND_MODE_VALUES:
            raise ExperimentContractError(
                f"unsupported backend_mode: {self.backend_mode!r}"
            )
        if (
            not isinstance(self.repetition, int)
            or isinstance(self.repetition, bool)
            or self.repetition < 1
        ):
            raise ExperimentContractError(
                "repetition must be an integer greater than zero"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""

        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ExperimentRunDirectory:
    """Collision-safe artifact directory for one canonical execution."""

    root: Path
    path: Path

    @classmethod
    def create(
        cls,
        root: Path,
        identity: ExperimentIdentity,
    ) -> "ExperimentRunDirectory":
        """Create a new execution directory and refuse silent overwrites."""

        normalized_root = root.expanduser().resolve()
        normalized_root.mkdir(parents=True, exist_ok=True)
        path = normalized_root / identity.scenario_id / identity.execution_id
        try:
            path.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise ExperimentContractError(
                f"execution directory already exists: {path}"
            ) from exc
        return cls(root=normalized_root, path=path)

    def artifact(self, relative_name: str) -> Path:
        """Resolve a relative artifact path without allowing directory escape."""

        candidate = (self.path / relative_name).resolve()
        try:
            candidate.relative_to(self.path.resolve())
        except ValueError as exc:
            raise ExperimentContractError(
                f"artifact path escapes execution directory: {relative_name!r}"
            ) from exc
        return candidate


def build_run_manifest(
    *,
    identity: ExperimentIdentity,
    provenance: RepositoryProvenance,
    specification_path: Path,
    configuration: Mapping[str, Any],
    started_at: dt.datetime,
    contract_version: str = "phase19-experiment-contract-v1",
) -> dict[str, Any]:
    """Build the immutable opening manifest for one execution.

    A scenario may add observations and completion state to a separate summary,
    but it must not mutate these identity, specification, configuration, and Git
    provenance fields after traffic generation begins.
    """

    spec_path = specification_path.expanduser().resolve()
    if not spec_path.is_file():
        raise ExperimentContractError(f"specification does not exist: {spec_path}")

    normalized_configuration = dict(configuration)
    return {
        "contract_version": contract_version,
        "identity": identity.to_dict(),
        "started_at_utc": utc_rfc3339(started_at),
        "repository": provenance.to_dict(),
        "specification": {
            "path": str(spec_path),
            "sha256": sha256_file(spec_path),
        },
        "configuration": normalized_configuration,
        "configuration_sha256": sha256_json(normalized_configuration),
    }
