# Phase 19 deterministic execution-plan contract

## Boundary

Phase 19.7B-2b defines the additive persisted contract
`phase19-execution-plan-v1`. It deterministically expands one complete,
validated `phase19-randomization-manifest-v1` into two planned physical run
slots per randomized block. It does not run scenarios, allocate
`execution_id`, create attempt records or retries, collect observations, or
alter S1/S2 runners.

The complete randomization manifest is embedded in the plan. Its canonical
external SHA-256 is recalculated with `sha256_json`; a caller-supplied digest is
never authoritative. The execution plan also has an external canonical hash
and contains no self-hash.

## Deterministic expansion

Blocks retain increasing `sequence_index`. Period 1 immediately precedes
period 2 within every block, and
`slot_index = 2 * (sequence_index - 1) + period`. No PRNG, clock, UUID, global
state, re-ranking, or normalization participates in expansion.

For order `AB`, periods 1 and 2 use arms A and B. For `BA`, they use B and A.
RQ3 maps A to `baseline` and B to `adapt`; RQ4 maps A to
`observation_only` and B to `selective_assurance`.

The manifest's `block_index` and `sequence_index` have distinct roles. Under
`phase19-execution-plan-v1`, both `assignment.block_index` and
`block_sequence_index` are derived from the block's `sequence_index`; the
manifest's original `block_index` is not copied into
`assignment.block_index`.

## Deterministic identities

Let `H = sha256_json(complete_validated_randomization_manifest)`.

```text
execution_plan_id =
  "execution-plan-" +
  SHA256(
    ASCII("phase19-execution-plan-id-v1")
    || 0x00
    || ASCII(H)
  ).hexdigest()
```

```text
run_slot_id =
  "run-slot-" +
  SHA256(
    ASCII("phase19-run-slot-id-v1")
    || 0x00
    || canonical_json_bytes({
         "randomization_manifest_sha256": H,
         "block_id": block_id,
         "period": period
       })
  ).hexdigest()
```

`canonical_json_bytes` and `sha256_json` are the existing primitives in
`l2i.experiment_contract`. Readable persistence uses UTF-8, sorted keys,
two-space indentation, and exactly one final newline. Existing outputs are
rejected.

## Slots, executions, and attempts

A run slot is an immutable planned opportunity. It contains one
`ExperimentAssignmentV2` and the scenario, profile, and backend requirements
needed to construct a future physical identity. It does not contain
`execution_id` or execution state.

Attempt records are deferred. The reserved future ordinal is
`attempt_number`, beginning at 1 and contiguous per `run_slot_id`. Each future
attempt receives a new `execution_id`; retries never change assignment,
`block_id`, period, or `run_slot_id`. `attempt_index`, `attempt_id`,
`retry_index`, and `retry_number` are not aliases.

Results, observations, metrics, execution status, timestamps, retries, future
artifact hashes, and self-hashes are forbidden in the plan.

## Validation boundary and canonicality

The execution-plan schema treats `randomization_manifest` only as an object
envelope. Its internal structure and semantics are validated exclusively by
the certified randomization-manifest validator. Failures are exposed as
`INVALID_EMBEDDED_RANDOMIZATION_MANIFEST: <original-code>: <detail>`.

Semantic slot coverage uses exactly `(assignment.block_id,
assignment.period)`. Missing slots precede additional slots. Duplicate IDs,
indices, and semantic keys, noncontiguous indices, assignments, execution
requirements, paired blocks, and deterministic IDs are checked before the
final physical comparison. Physically permuting intact slots is rejected as
`NONCANONICAL_RUN_SLOT_SEQUENCE`; validators never sort and accept a
noncanonical input.

Confirmatory plans inherit the manifest requirement for backend `real`. The
50-block confirmatory fixture is synthetic contract evidence, not a definitive
campaign plan.
