"""Autonomous desired-state assurance primitives for the L2I MAD.

This module provides a small reconciliation engine that continuously compares
an expected state with independent readback observations. It has no dependency
on P4Runtime or on a specific scenario. Callers provide observation and
remediation callbacks, while the controller owns drift confirmation, retry,
backoff, convergence confirmation, event ordering, and stop semantics.

The controller deliberately separates three concerns:

* desired state: the immutable state the MAD expects to remain materialized;
* observed state: a timestamped readback result produced by the AC/backend;
* reconciliation policy: the evidence required before drift or convergence is
  accepted, plus bounded retry and exponential-backoff behavior.

A remediation callback is invoked only after drift is independently confirmed.
The callback is never told when or how an external fault was injected. This
property allows experiments to demonstrate autonomous detection and recovery
without sharing a fault schedule with the reconciliation loop.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import threading
import time
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class AssurancePolicy:
    """Bound observation, drift confirmation, retry, and convergence behavior."""

    poll_interval_s: float = 0.02
    drift_confirmations: int = 2
    convergence_confirmations: int = 2
    maximum_remediation_attempts: int = 3
    initial_backoff_s: float = 0.01
    backoff_multiplier: float = 2.0
    maximum_backoff_s: float = 0.10
    maximum_consecutive_observation_errors: int = 3

    def validate(self) -> None:
        """Reject policies that could create busy loops or unbounded retries."""

        if self.poll_interval_s <= 0.0:
            raise ValueError("poll_interval_s must be positive")
        if self.drift_confirmations < 1:
            raise ValueError("drift_confirmations must be at least one")
        if self.convergence_confirmations < 1:
            raise ValueError("convergence_confirmations must be at least one")
        if self.maximum_remediation_attempts < 1:
            raise ValueError("maximum_remediation_attempts must be at least one")
        if self.initial_backoff_s < 0.0:
            raise ValueError("initial_backoff_s cannot be negative")
        if self.backoff_multiplier < 1.0:
            raise ValueError("backoff_multiplier must be at least one")
        if self.maximum_backoff_s < self.initial_backoff_s:
            raise ValueError(
                "maximum_backoff_s cannot be smaller than initial_backoff_s"
            )
        if self.maximum_consecutive_observation_errors < 0:
            raise ValueError(
                "maximum_consecutive_observation_errors cannot be negative"
            )


@dataclass(frozen=True)
class StateObservation:
    """Describe one backend readback and its comparison with desired state."""

    read_ok: bool
    healthy: bool
    drift_kinds: tuple[str, ...] = ()
    observed: Mapping[str, Any] = field(default_factory=dict)
    observed_monotonic_ns: int = field(default_factory=time.monotonic_ns)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation used by archived evidence."""

        return {
            "read_ok": bool(self.read_ok),
            "healthy": bool(self.healthy),
            "drift_kinds": list(self.drift_kinds),
            "observed": dict(self.observed),
            "observed_monotonic_ns": int(self.observed_monotonic_ns),
            "message": str(self.message),
        }


@dataclass(frozen=True)
class RemediationOutcome:
    """Record whether one bounded remediation attempt was accepted by a backend."""

    accepted: bool
    details: Mapping[str, Any] = field(default_factory=dict)
    completed_monotonic_ns: int = field(default_factory=time.monotonic_ns)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation used by archived evidence."""

        return {
            "accepted": bool(self.accepted),
            "details": dict(self.details),
            "completed_monotonic_ns": int(self.completed_monotonic_ns),
        }


@dataclass(frozen=True)
class AssuranceEvent:
    """Represent one ordered controller transition or decision."""

    sequence: int
    event_type: str
    monotonic_ns: int
    incident_id: int | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe event dictionary."""

        return {
            "sequence": int(self.sequence),
            "event_type": str(self.event_type),
            "monotonic_ns": int(self.monotonic_ns),
            "incident_id": self.incident_id,
            "details": dict(self.details),
        }


ObservationCallback = Callable[[], StateObservation]
RemediationCallback = Callable[[StateObservation, int, int], RemediationOutcome]


class MADAssuranceController:
    """Continuously reconcile observed backend state with MAD desired state.

    The controller is intentionally callback-driven. A scenario can use one
    P4Runtime adapter, one NETCONF adapter, or another backend without changing
    the state machine. The controller exposes synchronization events only for
    experimental orchestration; those events are emitted by the assurance loop
    itself and do not carry fault-injection timing into the controller.
    """

    def __init__(
        self,
        *,
        controller_id: str,
        desired_state: Mapping[str, Any],
        observe_fn: ObservationCallback,
        remediate_fn: RemediationCallback,
        policy: AssurancePolicy,
    ) -> None:
        policy.validate()
        if not controller_id.strip():
            raise ValueError("controller_id cannot be empty")

        self.controller_id = controller_id
        self.desired_state = dict(desired_state)
        self.observe_fn = observe_fn
        self.remediate_fn = remediate_fn
        self.policy = policy

        self.initial_convergence_event = threading.Event()
        self.drift_detected_event = threading.Event()
        self.first_recovery_event = threading.Event()
        self.failed_event = threading.Event()

        self._lock = threading.Lock()
        self._events: list[AssuranceEvent] = []
        self._observations: list[dict[str, Any]] = []
        self._state = "created"
        self._failure: str | None = None
        self._incident_count = 0
        self._remediation_attempt_count = 0
        self._successful_convergence_count = 0
        self._loop_started_ns: int | None = None
        self._loop_stopped_ns: int | None = None

    def _emit(
        self,
        event_type: str,
        *,
        incident_id: int | None = None,
        details: Mapping[str, Any] | None = None,
        monotonic_ns: int | None = None,
    ) -> AssuranceEvent:
        """Append one ordered event under a lock and return it."""

        with self._lock:
            event = AssuranceEvent(
                sequence=len(self._events) + 1,
                event_type=event_type,
                monotonic_ns=int(monotonic_ns or time.monotonic_ns()),
                incident_id=incident_id,
                details=dict(details or {}),
            )
            self._events.append(event)
            return event

    def _record_observation(
        self,
        observation: StateObservation,
        *,
        state: str,
        incident_id: int | None,
    ) -> None:
        """Preserve every poll with a compact controller-state annotation."""

        record = observation.to_dict()
        with self._lock:
            record.update(
                {
                    "sequence": len(self._observations) + 1,
                    "controller_state": state,
                    "incident_id": incident_id,
                }
            )
            self._observations.append(record)

    def _set_state(self, value: str) -> None:
        """Update the externally visible controller state."""

        with self._lock:
            self._state = value

    def _set_failure(self, message: str) -> None:
        """Enter a terminal failure state and unblock waiting orchestrators."""

        with self._lock:
            self._failure = message
            self._state = "failed"
        self.failed_event.set()
        self._emit("loop_failed", details={"message": message})

    def _backoff_for_attempt(self, attempt: int) -> float:
        """Return the bounded exponential backoff preceding a retry."""

        if attempt <= 1:
            return 0.0
        value = self.policy.initial_backoff_s * (
            self.policy.backoff_multiplier ** (attempt - 2)
        )
        return min(value, self.policy.maximum_backoff_s)

    def _attempt_remediation(
        self,
        observation: StateObservation,
        *,
        incident_id: int,
        attempt: int,
        stop_event: threading.Event,
    ) -> bool:
        """Execute one callback attempt and record its complete outcome."""

        backoff_s = self._backoff_for_attempt(attempt)
        if backoff_s > 0.0:
            self._emit(
                "remediation_backoff_started",
                incident_id=incident_id,
                details={"attempt": attempt, "backoff_s": backoff_s},
            )
            if stop_event.wait(backoff_s):
                return False

        self._remediation_attempt_count += 1
        started_ns = time.monotonic_ns()
        self._emit(
            "remediation_attempt_started",
            incident_id=incident_id,
            monotonic_ns=started_ns,
            details={
                "attempt": attempt,
                "reason": "confirmed_desired_state_drift",
                "drift_kinds": list(observation.drift_kinds),
            },
        )

        try:
            outcome = self.remediate_fn(observation, incident_id, attempt)
        except Exception as exc:  # pragma: no cover - scenario evidence records it
            outcome = RemediationOutcome(
                accepted=False,
                details={
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
            )

        self._emit(
            "remediation_attempt_completed",
            incident_id=incident_id,
            monotonic_ns=outcome.completed_monotonic_ns,
            details={
                "attempt": attempt,
                "elapsed_ms": (
                    outcome.completed_monotonic_ns - started_ns
                )
                / 1_000_000.0,
                **outcome.to_dict(),
            },
        )
        return bool(outcome.accepted)

    def run(
        self,
        stop_event: threading.Event,
        *,
        maximum_runtime_s: float | None = None,
    ) -> None:
        """Run until stopped, failed, or a maximum runtime is reached."""

        if maximum_runtime_s is not None and maximum_runtime_s <= 0.0:
            raise ValueError("maximum_runtime_s must be positive when provided")

        self._loop_started_ns = time.monotonic_ns()
        self._set_state("observing_initial_state")
        self._emit(
            "loop_started",
            monotonic_ns=self._loop_started_ns,
            details={
                "controller_id": self.controller_id,
                "desired_state": self.desired_state,
                "policy": asdict(self.policy),
            },
        )

        deadline = (
            time.monotonic() + maximum_runtime_s
            if maximum_runtime_s is not None
            else None
        )
        healthy_streak = 0
        drift_streak = 0
        observation_error_streak = 0
        active_incident: int | None = None
        remediation_attempt = 0
        awaiting_convergence = False
        initial_converged = False
        last_observation: StateObservation | None = None

        while not stop_event.is_set() and not self.failed_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                self._set_failure("maximum assurance runtime exceeded")
                break

            try:
                observation = self.observe_fn()
            except Exception as exc:  # pragma: no cover - backend-specific
                observation = StateObservation(
                    read_ok=False,
                    healthy=False,
                    drift_kinds=("observation_exception",),
                    observed={
                        "exception_type": type(exc).__name__,
                        "exception": str(exc),
                    },
                    message=str(exc),
                )

            last_observation = observation
            with self._lock:
                state_snapshot = self._state
            self._record_observation(
                observation,
                state=state_snapshot,
                incident_id=active_incident,
            )

            if not observation.read_ok:
                observation_error_streak += 1
                healthy_streak = 0
                drift_streak = 0
                self._emit(
                    "observation_error",
                    incident_id=active_incident,
                    monotonic_ns=observation.observed_monotonic_ns,
                    details={
                        "consecutive_errors": observation_error_streak,
                        "message": observation.message,
                        "observed": dict(observation.observed),
                    },
                )
                if (
                    observation_error_streak
                    > self.policy.maximum_consecutive_observation_errors
                ):
                    self._set_failure(
                        "maximum consecutive observation errors exceeded"
                    )
                    break
                stop_event.wait(self.policy.poll_interval_s)
                continue

            observation_error_streak = 0

            if observation.healthy:
                drift_streak = 0
                healthy_streak += 1

                if (
                    not initial_converged
                    and healthy_streak >= self.policy.convergence_confirmations
                ):
                    initial_converged = True
                    self._set_state("in_sync")
                    self.initial_convergence_event.set()
                    self._emit(
                        "initial_convergence_confirmed",
                        monotonic_ns=observation.observed_monotonic_ns,
                        details={
                            "confirmations": healthy_streak,
                            "observed": dict(observation.observed),
                        },
                    )

                elif (
                    awaiting_convergence
                    and active_incident is not None
                    and healthy_streak >= self.policy.convergence_confirmations
                ):
                    self._successful_convergence_count += 1
                    self._set_state("in_sync")
                    self._emit(
                        "convergence_confirmed",
                        incident_id=active_incident,
                        monotonic_ns=observation.observed_monotonic_ns,
                        details={
                            "confirmations": healthy_streak,
                            "remediation_attempts": remediation_attempt,
                            "observed": dict(observation.observed),
                        },
                    )
                    self.first_recovery_event.set()
                    active_incident = None
                    remediation_attempt = 0
                    awaiting_convergence = False

                stop_event.wait(self.policy.poll_interval_s)
                continue

            healthy_streak = 0
            drift_streak += 1
            self._emit(
                "drift_observed",
                incident_id=active_incident,
                monotonic_ns=observation.observed_monotonic_ns,
                details={
                    "consecutive_drift_observations": drift_streak,
                    "drift_kinds": list(observation.drift_kinds),
                    "observed": dict(observation.observed),
                },
            )

            if not initial_converged:
                if drift_streak >= self.policy.drift_confirmations:
                    self._set_failure(
                        "desired state was not initially converged before drift"
                    )
                    break
                stop_event.wait(self.policy.poll_interval_s)
                continue

            if active_incident is None:
                if drift_streak < self.policy.drift_confirmations:
                    stop_event.wait(self.policy.poll_interval_s)
                    continue

                self._incident_count += 1
                active_incident = self._incident_count
                remediation_attempt = 0
                awaiting_convergence = False
                self.drift_detected_event.set()
                self._set_state("drift_confirmed")
                self._emit(
                    "drift_confirmed",
                    incident_id=active_incident,
                    monotonic_ns=observation.observed_monotonic_ns,
                    details={
                        "confirmations": drift_streak,
                        "drift_kinds": list(observation.drift_kinds),
                        "observed": dict(observation.observed),
                    },
                )

            if awaiting_convergence and drift_streak < self.policy.drift_confirmations:
                stop_event.wait(self.policy.poll_interval_s)
                continue

            if remediation_attempt >= self.policy.maximum_remediation_attempts:
                self._set_failure(
                    "desired state remained divergent after bounded remediation"
                )
                break

            remediation_attempt += 1
            self._set_state("remediating")
            accepted = self._attempt_remediation(
                observation,
                incident_id=active_incident,
                attempt=remediation_attempt,
                stop_event=stop_event,
            )
            if accepted:
                awaiting_convergence = True
                drift_streak = 0
                self._set_state("verifying_convergence")
            else:
                awaiting_convergence = False
                self._set_state("remediation_retry_pending")

            stop_event.wait(self.policy.poll_interval_s)

        self._loop_stopped_ns = time.monotonic_ns()
        if not self.failed_event.is_set():
            self._set_state("stopped")
            self._emit(
                "loop_stopped",
                monotonic_ns=self._loop_stopped_ns,
                details={
                    "stop_requested": stop_event.is_set(),
                    "last_observation": (
                        last_observation.to_dict()
                        if last_observation is not None
                        else None
                    ),
                },
            )

    def snapshot(self) -> dict[str, Any]:
        """Return a thread-safe JSON-ready controller snapshot."""

        with self._lock:
            events = [event.to_dict() for event in self._events]
            observations = [dict(item) for item in self._observations]
            return {
                "controller_id": self.controller_id,
                "desired_state": dict(self.desired_state),
                "policy": asdict(self.policy),
                "state": self._state,
                "failure": self._failure,
                "loop_started_monotonic_ns": self._loop_started_ns,
                "loop_stopped_monotonic_ns": self._loop_stopped_ns,
                "incident_count": self._incident_count,
                "remediation_attempt_count": self._remediation_attempt_count,
                "successful_convergence_count": (
                    self._successful_convergence_count
                ),
                "events": events,
                "observations": observations,
            }
