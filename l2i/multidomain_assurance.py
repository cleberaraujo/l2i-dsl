"""Coordinate desired-state assurance across heterogeneous L2I domains.

The Phase 16 controller intentionally owns only the reconciliation state
machine. This module composes multiple technology-specific adapters behind the
same observation and remediation callbacks. It preserves per-domain evidence,
prefixes drift classifications with stable domain identifiers, remediates only
divergent domains, and declares health only when every managed domain is
converged in the same aggregate observation.

The coordinator does not implement distributed transactions or rollback.
Converged domains are preserved while divergent domains are retried. This
selective best-effort policy is appropriate for autonomous desired-state
reconciliation and keeps unsupported atomicity claims outside the validated
scope.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from l2i.assurance import RemediationOutcome, StateObservation


DomainObservationCallback = Callable[[], StateObservation]
DomainRemediationCallback = Callable[
    [StateObservation, int, int],
    RemediationOutcome,
]


@dataclass(frozen=True)
class DomainAssuranceBinding:
    """Bind one stable domain identifier to observation and remediation hooks."""

    domain_id: str
    technology: str
    desired_state: Mapping[str, Any]
    observe_fn: DomainObservationCallback
    remediate_fn: DomainRemediationCallback

    def validate(self) -> None:
        """Reject ambiguous identifiers before the assurance loop starts."""

        if not self.domain_id.strip():
            raise ValueError("domain_id cannot be empty")
        if ":" in self.domain_id:
            raise ValueError("domain_id cannot contain ':'")
        if not self.technology.strip():
            raise ValueError("technology cannot be empty")


class MultiDomainAssuranceAdapter:
    """Aggregate heterogeneous domain adapters for one MAD assurance loop.

    The adapter stores the most recent typed observation for each domain so a
    remediation callback receives the exact state that triggered the aggregate
    decision. The public snapshot is JSON-safe and contains no callback or
    connection objects.
    """

    def __init__(
        self,
        bindings: Sequence[DomainAssuranceBinding],
        *,
        synthetic_rejection_budget: Mapping[str, int] | None = None,
    ) -> None:
        if not bindings:
            raise ValueError("at least one domain binding is required")

        ordered = tuple(bindings)
        seen: set[str] = set()
        for binding in ordered:
            binding.validate()
            if binding.domain_id in seen:
                raise ValueError(
                    f"duplicate domain_id: {binding.domain_id}"
                )
            seen.add(binding.domain_id)

        self.bindings = ordered
        self._binding_by_id = {
            binding.domain_id: binding
            for binding in ordered
        }

        rejection_budget = {
            str(domain_id): int(count)
            for domain_id, count in dict(
                synthetic_rejection_budget or {}
            ).items()
        }
        unknown_rejection_domains = sorted(
            set(rejection_budget) - set(self._binding_by_id)
        )
        if unknown_rejection_domains:
            raise ValueError(
                "synthetic rejection budget contains unmanaged domains: "
                + ",".join(unknown_rejection_domains)
            )
        negative_rejection_domains = sorted(
            domain_id
            for domain_id, count in rejection_budget.items()
            if count < 0
        )
        if negative_rejection_domains:
            raise ValueError(
                "synthetic rejection budget cannot be negative: "
                + ",".join(negative_rejection_domains)
            )

        self._lock = threading.Lock()
        self._latest: dict[str, StateObservation] = {}
        self._observation_count = 0
        self._remediation_count = 0
        self._domain_remediation_counts = {
            binding.domain_id: 0
            for binding in ordered
        }
        self._domain_backend_remediation_counts = {
            binding.domain_id: 0
            for binding in ordered
        }
        self._domain_synthetic_rejection_counts = {
            binding.domain_id: 0
            for binding in ordered
        }
        self._synthetic_rejection_initial = {
            binding.domain_id: rejection_budget.get(binding.domain_id, 0)
            for binding in ordered
        }
        self._synthetic_rejection_remaining = dict(
            self._synthetic_rejection_initial
        )
        self._remediation_history: list[dict[str, Any]] = []

    @property
    def desired_state(self) -> dict[str, Any]:
        """Return the aggregate immutable desired-state description."""

        return {
            "domains": {
                binding.domain_id: {
                    "technology": binding.technology,
                    "desired_state": dict(binding.desired_state),
                }
                for binding in self.bindings
            },
            "global_convergence_rule": (
                "all_managed_domains_healthy_in_same_observation"
            ),
            "remediation_policy": (
                "selective_best_effort_without_cross_domain_rollback"
            ),
            "synthetic_test_rejection_budget": dict(
                self._synthetic_rejection_initial
            ),
        }

    @staticmethod
    def _prefixed_drift(
        domain_id: str,
        drift_kind: str,
    ) -> str:
        """Create an unambiguous cross-domain drift identifier."""

        return f"{domain_id}:{drift_kind}"

    def observe(self) -> StateObservation:
        """Read every domain and produce one aggregate convergence decision."""

        domain_records: dict[str, dict[str, Any]] = {}
        typed: dict[str, StateObservation] = {}
        aggregate_drifts: list[str] = []
        observed_times: list[int] = []

        for binding in self.bindings:
            try:
                observation = binding.observe_fn()
            except Exception as exc:  # pragma: no cover - defensive boundary
                observation = StateObservation(
                    read_ok=False,
                    healthy=False,
                    drift_kinds=("observation_exception",),
                    observed={
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    },
                    message="domain observation callback raised an exception",
                )

            typed[binding.domain_id] = observation
            observed_times.append(observation.observed_monotonic_ns)
            drift_kinds = tuple(observation.drift_kinds)
            if not drift_kinds and not observation.healthy:
                drift_kinds = ("unspecified_drift",)

            aggregate_drifts.extend(
                self._prefixed_drift(binding.domain_id, drift_kind)
                for drift_kind in drift_kinds
            )
            domain_records[binding.domain_id] = {
                "technology": binding.technology,
                "desired_state": dict(binding.desired_state),
                **observation.to_dict(),
            }

        healthy_domains = [
            domain_id
            for domain_id, observation in typed.items()
            if observation.healthy
        ]
        drifted_domains = [
            domain_id
            for domain_id, observation in typed.items()
            if not observation.healthy
        ]
        read_ok = all(
            observation.read_ok
            for observation in typed.values()
        )
        healthy = all(
            observation.healthy
            for observation in typed.values()
        )
        observed_ns = max(observed_times) if observed_times else time.monotonic_ns()

        with self._lock:
            self._latest = dict(typed)
            self._observation_count += 1
            observation_sequence = self._observation_count

        return StateObservation(
            read_ok=read_ok,
            healthy=healthy,
            drift_kinds=tuple(aggregate_drifts),
            observed={
                "observation_sequence": observation_sequence,
                "domains": domain_records,
                "healthy_domains": healthy_domains,
                "drifted_domains": drifted_domains,
                "managed_domain_count": len(self.bindings),
                "globally_converged": healthy,
            },
            observed_monotonic_ns=observed_ns,
            message=(
                "all managed domains are converged"
                if healthy
                else "one or more managed domains diverged"
            ),
        )

    def remediate(
        self,
        observation: StateObservation,
        incident_id: int,
        attempt: int,
    ) -> RemediationOutcome:
        """Remediate only domains that remain divergent in the latest readback."""

        observed_payload = dict(observation.observed)
        requested_domains = list(
            observed_payload.get("drifted_domains") or []
        )

        with self._lock:
            latest = dict(self._latest)

        results: dict[str, dict[str, Any]] = {}
        all_accepted = True
        started_ns = time.monotonic_ns()

        for domain_id in requested_domains:
            binding = self._binding_by_id.get(domain_id)
            domain_observation = latest.get(domain_id)
            if binding is None or domain_observation is None:
                all_accepted = False
                results[domain_id] = {
                    "accepted": False,
                    "error": "missing_domain_binding_or_observation",
                }
                continue

            # A bounded synthetic rejection is injected above the real backend
            # callback. It is explicitly test-only: no backend write is attempted,
            # the domain remains divergent, and the generic controller must later
            # retry only the domains still reported as unhealthy.
            with self._lock:
                remaining_rejections = (
                    self._synthetic_rejection_remaining.get(domain_id, 0)
                )
                if remaining_rejections > 0:
                    self._synthetic_rejection_remaining[domain_id] = (
                        remaining_rejections - 1
                    )

            synthetic_rejection = remaining_rejections > 0
            if synthetic_rejection:
                outcome = RemediationOutcome(
                    accepted=False,
                    details={
                        "synthetic_test_rejection": True,
                        "domain": domain_id,
                        "incident_id": incident_id,
                        "attempt": attempt,
                        "remaining_synthetic_rejections": (
                            remaining_rejections - 1
                        ),
                        "backend_callback_invoked": False,
                    },
                )
            else:
                try:
                    outcome = binding.remediate_fn(
                        domain_observation,
                        incident_id,
                        attempt,
                    )
                except Exception as exc:  # pragma: no cover - defensive boundary
                    outcome = RemediationOutcome(
                        accepted=False,
                        details={
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                            "backend_callback_invoked": True,
                        },
                    )

            all_accepted = all_accepted and outcome.accepted
            results[domain_id] = {
                "technology": binding.technology,
                "remediation_mode": (
                    "synthetic_test_rejection"
                    if synthetic_rejection
                    else "backend_callback"
                ),
                **outcome.to_dict(),
            }
            with self._lock:
                self._domain_remediation_counts[domain_id] += 1
                if synthetic_rejection:
                    self._domain_synthetic_rejection_counts[domain_id] += 1
                else:
                    self._domain_backend_remediation_counts[domain_id] += 1

        completed_ns = time.monotonic_ns()
        record = {
            "incident_id": incident_id,
            "attempt": attempt,
            "requested_domains": requested_domains,
            "accepted": all_accepted,
            "started_monotonic_ns": started_ns,
            "completed_monotonic_ns": completed_ns,
            "elapsed_ms": (completed_ns - started_ns) / 1_000_000.0,
            "domains": results,
        }

        with self._lock:
            self._remediation_count += 1
            self._remediation_history.append(record)

        return RemediationOutcome(
            accepted=all_accepted,
            details=record,
            completed_monotonic_ns=completed_ns,
        )

    def snapshot(self) -> dict[str, Any]:
        """Return aggregate counters and per-domain remediation history."""

        with self._lock:
            return {
                "managed_domains": [
                    {
                        "domain_id": binding.domain_id,
                        "technology": binding.technology,
                        "desired_state": dict(binding.desired_state),
                    }
                    for binding in self.bindings
                ],
                "observation_count": self._observation_count,
                "remediation_count": self._remediation_count,
                "domain_remediation_counts": dict(
                    self._domain_remediation_counts
                ),
                "domain_backend_remediation_counts": dict(
                    self._domain_backend_remediation_counts
                ),
                "domain_synthetic_rejection_counts": dict(
                    self._domain_synthetic_rejection_counts
                ),
                "synthetic_rejection_initial": dict(
                    self._synthetic_rejection_initial
                ),
                "synthetic_rejection_remaining": dict(
                    self._synthetic_rejection_remaining
                ),
                "remediation_history": [
                    dict(item)
                    for item in self._remediation_history
                ],
            }


__all__ = [
    "DomainAssuranceBinding",
    "MultiDomainAssuranceAdapter",
]
