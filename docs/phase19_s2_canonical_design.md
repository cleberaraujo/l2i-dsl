# Phase 19 S2 canonical design: experimental contract v2

## Scope of this increment

Phase 19.7B-1 defines only the additive experimental contract v2, its
assignment schema, and deterministic validation fixtures. It does not implement
an S2 runner, traffic, assurance behavior, a campaign, or scientific analysis.
It neither executes experiments nor confirms RQ3 or RQ4.

The run plan, event, write, readback, validity, summary, and definitive
randomization-manifest schemas are deliberately deferred to later increments.

## Physical identity and experimental assignment

The v2 contract keeps physical execution identity separate from experimental
assignment. `ExperimentIdentityV2` contains only `scenario_id`, `execution_id`,
`profile_id`, and `backend_mode`. In particular, it contains neither `mode` nor
`repetition`.

`ExperimentAssignmentV2` contains `campaign_id`, `campaign_stage`, `rq_id`,
`configuration_id`, `arm`, `treatment`, `block_id`, `block_index`, `period`,
`order`, and `randomization_manifest_sha256`. Identifiers use the existing
path-safe normalization. A block index is a positive integer, excluding
booleans, and the randomization-manifest reference is exactly one lowercase
SHA-256 hexadecimal digest.

The allowed campaign stages are `foundation`, `pilot`, and `confirmatory`.
Foundation and pilot assignments may use the `mock` backend. Confirmatory
assignments require `real`; there is no automatic fallback from `real` to
`mock`.

## Assignment invariants

The arm and treatment mapping is fixed:

| Research question | Arm A | Arm B |
|---|---|---|
| RQ3 | `baseline` | `adapt` |
| RQ4 | `observation_only` | `selective_assurance` |

The order and period mapping is also fixed:

| Order | Period 1 | Period 2 |
|---|---|---|
| AB | arm A | arm B |
| BA | arm B | arm A |

A paired block contains exactly two assignments. Both share campaign, research
question, configuration, block, order, and `randomization_manifest_sha256`, so
the two periods are bound to the same randomization manifest; periods are
exactly `{1, 2}` and arms exactly `{A, B}`. Duplicate arms, duplicate periods,
incomplete blocks, and inconsistent
RQ--arm--treatment--order--period combinations fail closed with stable contract
error codes.

## Schema and fixture envelope

`schemas/phase19/experiment-assignment-v2.schema.json` is a JSON Schema Draft
2020-12 schema for one assignment. It uses strict required properties,
`additionalProperties: false`, safe identifier patterns, a positive block
index, a lowercase SHA-256 pattern, and conditional mappings for research
question, treatment, arm, order, and period.

Fixtures under `schemas/phase19/fixtures/assignment-v2/` use the deterministic
envelope `phase19-assignment-v2-fixture-v1`. The envelope stores one `identity`
object separately from an `assignments` array. Valid fixtures exercise RQ3 and
RQ4 in both orders and the allowed stage/backend combinations. Invalid fixtures
declare `expected_error` and exercise treatment, backend, order, duplication,
missing-field, hash, and completeness failures. The envelope is test-only and
is not a campaign or persistence schema.

## Compatibility and persistence boundary

The v2 APIs are additive. `ExperimentIdentity`, its `baseline` and `adapt`
modes, its serialization, `build_run_manifest` and its default v1 contract
version, Git provenance, hashing, atomic writes, and existing S1/S2 call sites
remain unchanged.

The existing `ExperimentRunDirectory` continues to create execution paths
exclusively and refuse collisions, so this increment does not weaken
overwrite protection. The v1 validator without arguments also retains its
clean synchronized `develop` gates. Fixture validation is available only
through the explicit `--validate-v2-fixtures` mode, which may run while the
implementation is under review in an uncommitted worktree.
