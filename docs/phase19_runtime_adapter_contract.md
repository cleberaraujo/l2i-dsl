# Phase 19 minimal runtime-adapter contract

## Boundary

The adapter consumes a validated `phase19-execution-plan-v1` but does not alter
that plan. It materializes one immutable paired unit and describes dispatch for
exactly `pilot` / `RQ3` / `S1` / `mock`. S2, RQ4, and real backends fail closed.
The command-line entry point is structural dry-run only in this increment.

Fixtures, execution plans, materialized units, attempts, physical executions,
and scenario artifacts remain distinct objects. `intent_name` and `intent_id`
are deferred until a normative source exists; neither is inferred from
`flow.id`.

## Materialized paired unit

`phase19-materialized-experimental-unit-v1` selects exactly one plan block and
preserves its two slots in physical plan order. Period and arm never determine
treatment by position: RQ3 arm A is `baseline` and arm B is `adapt`. The unit
freezes repetition 1, runtime parameters, source hashes, plan and manifest
references, both full assignments, and their canonical hashes.

`materialized_unit_sha256` is `sha256_json(unit_without_materialized_unit_sha256)`.
Every other derivable value is recalculated from the complete validated plan.
No caller-supplied derived identifier or digest is authoritative.

The embedded manifest contains exactly one source artifact whose declared
`role` is `scenario_spec`. That role alone selects the normative `(path,
sha256)` pair. Position, filename, extension, directory, and `scenario_id`
have no selection authority. The unit preserves every source's `role`, `path`,
and `sha256`; `runtime_parameters.spec_path` is only a mandatory assertion that
must equal the role-selected path.

Before reservation and again immediately before dispatch, the adapter opens
the role-selected path component by component without following symlinks,
hashes the bytes read from the opened descriptor, and compares them with the
normative SHA-256. Dispatch copies those verified bytes into a sealed Linux
`memfd` and passes `/proc/self/fd/<FD>` plus `pass_fds` to the injected executor.
Consequently the scenario consumes the same immutable bytes that were
verified, rather than reopening a mutable source path.

## Attempts and identity

An attempt belongs to one `run_slot_id`. `attempt_number` starts at 1 and is
contiguous per slot. Each attempt obtains a new `execution_id` from the existing
generator when its exclusive directory is reserved. A retry never changes the
unit, assignment, treatment, repetition, or scientific parameters and is never
automatic. ULIDs are not introduced.

The existing generator produces
`<scenario_id>-<UTC timestamp YYYYmmddTHHMMSSffffffZ>-<12 lowercase hex>`;
the suffix is the first twelve hexadecimal characters of `uuid.uuid4()`.
Generation occurs only after all normative inputs and source bytes validate.
The timestamp and UUID provide operational uniqueness, not scientific
identity. A factory remains injectable for deterministic tests.

Global uniqueness is enforced by the first persistent mutation under
`RESULTS_ROOT`: exclusive creation of
`phase19-execution-id-<EXECUTION_ID>`. This reservation is shared by every
campaign/plan/block/slot below the root. An existing reservation fails closed;
it is never removed or reused automatically. A failure or crash after this
first mutation may leave the reservation as a durable collision marker; the
same execution identity remains consumed even if no attempt sidecar was
completed.

The global reservation is also a mandatory local capability for dispatch. Its
root is derived only from the validated `RESULTS_ROOT`, its filename only from
the validated sidecar `execution_id`, and its exact bytes are
`<EXECUTION_ID>\n`. Immediately before dispatch the adapter opens the root and
marker without following symlinks, requires a regular file, validates its
bounded content, and compares the opened file identity with the authoritative
directory entry. It keeps that descriptor open across the final contextual
checkpoint, `RUNNING`, the injected executor, and terminal persistence, and
revalidates the directory entry, identity, type, and content immediately before
`RUNNING`. Absence, malformed content, a non-regular object, or synchronized
removal/replacement fails with `INVALID_ATTEMPT_RESERVATION` without rewriting
the sidecar or invoking the executor. All reservation descriptors are closed on
every path and the reservation descriptor is never inherited by the executor;
only the verified spec and results descriptors appear in `pass_fds`.

The marker is not executable input and supplies no argv or path authority. This
additional operational precondition does not change the attempt schema or
expand the accepted sidecar set: a structurally and semantically valid sidecar
without its congruent reservation is simply not dispatchable. Correct behavior
requires descriptor-relative operations, `O_NOFOLLOW`, `O_CLOEXEC`, stable
device/inode identity, and regular-file semantics from the host filesystem.

A valid reservation alone does not grant authority to persist `RUNNING`.
`transition_attempt` is a generic administrative transition API and always
rejects a public request for `RUNNING` with
`RUNNING_REQUIRES_DISPATCH_AUTHORITY`, after the temporal, schema, and semantic
validation of the current sidecar. `run_attempt_with_executor` is the sole
public route authorized to enter `RUNNING`, because only its reservation
context receives the opaque, live dispatch capability.

The internal persistence boundary requires that capability whenever its
candidate state is `RUNNING`; prior caller validation is insufficient. The
capability is bound to its internal issuer, validated `execution_id`,
authoritative results-root device/inode, reservation device/inode and open
descriptors, and it becomes inactive when the reservation context closes. The
boundary rejects a missing, forged, closed, cross-attempt, cross-root, or
replaced capability before persistence. This check performs the reservation
revalidation itself, so a future internal caller cannot omit it. Rejections
preserve the existing sidecar bytes and never create a reservation.

The capability remains local to the adapter across persistence of `RUNNING`,
the injected executor call, and terminal persistence. It is never represented
by an invocation field, path, boolean, or caller-supplied descriptor, and its
reservation descriptor is not included in `pass_fds`. The schema and the set of
structurally accepted sidecars are unchanged. `RUNNING` denotes an execution
actually entering the dispatch path, not an externally requested
administrative state change.

The states are:

```text
MATERIALIZED -> PREFLIGHT_PASSED -> RUNNING
RUNNING -> SUCCEEDED | FAILED | INTERRUPTED
```

Terminal states cannot transition. `RUNNING` is atomically persisted before an
injected executor is called. A record found in `RUNNING` is preserved; recovery
may mark it `INTERRUPTED`, but must never overwrite or reuse it.

An attempt sidecar is an untrusted record, never execution authority. Before
`RUNNING`, the adapter validates its JSON Schema and state semantics, including
every timestamp as the exact UTC form `YYYY-MM-DDTHH:MM:SS.ffffffZ`, revalidates
the complete ExecutionPlan, rematerializes the immutable unit, recalculates all
plan, manifest, assignment, unit, and slot identities, verifies current Git
provenance, reconstructs the canonical argv, checks the sidecar's canonical
directory, and revalidates the scenario-spec bytes. Any mismatch leaves the
attempt at `PREFLIGHT_PASSED` and the executor is not called. The persisted
logical argv contains `<VERIFIED_SCENARIO_SPEC_FD>` and
`<VERIFIED_NATIVE_RESULTS_ROOT_FD>`; only after reconstruction does execution
bind them to `/proc/self/fd/<FD>` capabilities and include both descriptors in
`pass_fds`. The persisted argv is never executable authority.

After descriptor binding, a final pre-execution checkpoint re-reads the
untrusted sidecar and repeats schema, state, timestamp, plan/unit identity,
canonical invocation, path, and provenance validation. `RUNNING` is built from
that exact validated record rather than from a further partial re-read. A
post-binding divergence therefore closes both capabilities, leaves the
persisted attempt at `PREFLIGHT_PASSED`, and does not call the executor. This
checkpoint prevents adapter-mediated stale-record dispatch; it is not described
as authentication against a non-cooperating writer with the same filesystem
privileges.

The adapter locates `execution-attempt-v1.schema.json` relative to its own
module, never from the current directory or from a sidecar-supplied path. Every
sidecar read that can feed a public or internal transition is validated first
by deterministic complementary layers. Present non-null contractual timestamp
values first receive strict semantic UTC classification so malformed or
calendrically impossible values retain `INVALID_ATTEMPT_TIMESTAMP`; the complete
normative schema is then mandatory for all structural and conditional rules;
finally the runtime repeats timestamp, state, and outcome semantics. This error
precedence does not expand the accepted record set. After constructing a
candidate state, the adapter applies the same complete layered validation to the
exact candidate immediately before its descriptor-relative atomic persistence.
A rejected record is not normalized or rewritten.

## Result isolation

The proposed hierarchy is:

```text
<RESULTS_ROOT>/<CAMPAIGN_ID>/<EXECUTION_PLAN_ID>/<BLOCK_ID>/<RUN_SLOT_ID>/
  attempt-<ATTEMPT_NUMBER>/<EXECUTION_ID>/
    attempt.json
    native/
      S1/<EXECUTION_ID>/...
```

Every attempt directory is created exclusively. The adapter opens the real
`native/` directory without following symlinks, confirms its descriptor and
`/proc/self/fd/<FD>` capability identify the same directory, keeps that
descriptor open throughout the executor call, and supplies the capability as
S1's `--results-root`. A later replacement of the textual `native/` name cannot
redirect writes. There is no textual fallback; absence of Linux
`/proc/self/fd` fails closed. The descriptor is closed on every normal,
exceptional, and interrupted return. Native artifacts remain in the validated
directory object under S1's existing layout.

All existing components of `RESULTS_ROOT` and all managed descendants are
opened descriptor-relatively with `O_NOFOLLOW`/`O_DIRECTORY`; `mkdir`, sidecar
creation, and atomic replacement are relative to validated directory
descriptors. Intermediate symlinks and unexpected component replacement fail
closed. No check-then-open path resolution is used as the authority for a
write.

The S1 consumer preserves exact `/proc/self/fd/<FD>` arguments instead of
resolving them to backing names. Its specification loader and opening manifest
read the inherited sealed descriptor directly. `ExperimentRunDirectory`
likewise retains the inherited results-directory capability when constructing
the native S1 hierarchy and artifact paths; ordinary filesystem paths keep
their prior normalization semantics, and absolute or traversal-bearing
artifact names remain rejected.

The sealed-spec guarantee additionally requires Linux `memfd_create`, seal
support, and `/proc/self/fd`; the result-directory capability requires
`O_NOFOLLOW`, `O_DIRECTORY`, descriptor inheritance, and `/proc/self/fd`.
Unsupported environments fail closed instead of reverting to mutable paths.

## Structural dry-run

Dry-run validates and materializes the unit, reports both slots in plan order,
and emits descriptive argv containing `<EXECUTION_ID>` and
`<ABSOLUTE_RESULTS_ROOT>`. It creates no execution identity, attempt, result,
process, scenario import, or backend import. This contract does not authorize
an experiment.

S2 integration remains deferred until a canonical S2 entry point explicitly
accepts `observation_only` and `selective_assurance`, execution identity,
repetition, and an exclusive results root. Those treatments must never be
silently translated to the historical `baseline` and `adapt` modes.

## Phase 4R S2/RQ4 integration

The canonical S2 entry point is `scenarios.multidomain_s2`. It exposes
`execution_mode`, `rq4_assurance_mode`, backend, profile, repetition,
execution identity and results root as distinct fields. Planned invocations
validate ExecutionPlan V1 and select AssignmentV2 only by its derived
`run_slot_id`; AssignmentV2 treatment has precedence over caller assertions.
Qualification-only invocations use no scientific slot.

The S2 runner persists an exclusive manifest and durable `RUNNING` attempt
record before fault injection, uses atomic no-follow artifact writes, produces
summary/readbacks/hashes and Git provenance pre/post, and terminalizes the
attempt explicitly. Baseline/adapt and unknown RQ4 modes fail without fallback.
