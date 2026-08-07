# Phase 19 campaign randomization contract

## Boundary of Phase 19.7B-2a

Phase 19.7B-2a defines the immutable input request, deterministic
randomization-manifest contract, materializer, schema, fixtures, and semantic
validator. One manifest covers exactly one tuple of `campaign_id`,
`campaign_stage`, `rq_id`, `scenario_id`, and `backend_mode`.

Phase 19.7B-2b remains deferred. This contract does not expand blocks into
periods and does not define an execution plan, run slots, `run_slot_id`,
`execution_id`, attempts, retries, traffic, results, or analysis. The manifest
also contains no timestamps.

Phase 19.7B-2b subsequently defines the additive
`phase19-execution-plan-v1` contract. It embeds a complete validated manifest
and expands each block into two deterministic, contiguous run slots without
changing this randomization-manifest contract.

## Declarative request and persisted manifest

The materializer input is a
`phase19-randomization-request-v1` JSON object. It identifies the manifest and
campaign tuple, records the 40-character lowercase repository commit, supplies
a 32-byte seed as 64 lowercase hexadecimal characters, and contains at least
one configuration.

Each configuration binds a safe `configuration_id` to a safe `profile_id`, an
even `block_count` of at least two, and a nonempty set of source artifacts.
Each source has a relative POSIX path and a lowercase SHA-256. Absolute paths,
backslashes, empty path segments, `.` and `..` are forbidden. Configuration
identifiers and paths within one configuration are unique.

The request is not persisted as the manifest. The materializer emits only
`phase19-randomization-manifest-v1`, including:

- the single campaign tuple and repository commit;
- `randomization.algorithm_id` and `randomization.seed_hex`;
- the frozen configuration and source catalog;
- `total_block_count`;
- the deterministic block sequence.

The configuration catalog is sorted by `configuration_id`. Sources are sorted
by `path`, then by `sha256`. Consequently, semantically equivalent request
ordering produces identical canonical content.

Foundation and pilot manifests may select `mock` or `real`. Confirmatory
manifests require `real`, with no fallback to `mock`.

## Byte-level deterministic algorithm

The algorithm identifier is `sha256-ranked-balanced-v1`. It never uses a
runtime PRNG, `hash()`, UUIDs, timestamps, global state, or runtime-dependent
iteration order.

For order and global ranks, define:

```text
rank(seed, namespace, payload) =
  SHA256(
    bytes.fromhex(seed_hex)
    || 0x00
    || ASCII(namespace)
    || 0x00
    || canonical_json_bytes(payload)
  )
```

Digest comparison is unsigned lexicographic byte comparison. Canonical JSON is
the UTF-8, key-sorted, compact representation supplied by
`l2i.experiment_contract.canonical_json_bytes`.

The literal namespaces are:

- `phase19-randomization-order-rank-v1`;
- `phase19-randomization-global-rank-v1`;
- `phase19-randomization-block-id-v1`.

For each configuration, candidates are objects containing
`configuration_id` and local `block_index`, where `block_index` covers
`1..block_count`. Each candidate is ranked in the order namespace. Candidates
are sorted first by digest and then by their canonical JSON bytes. The first
half receives `AB`; the second half receives `BA`. Every configuration
therefore has exactly `block_count / 2` blocks in each order.

`block_id` is `block-` followed by:

```text
SHA256(
  ASCII("phase19-randomization-block-id-v1")
  || 0x00
  || canonical_json_bytes(block_semantic_identity)
).hexdigest()
```

The semantic identity contains `manifest_id`, `campaign_id`,
`campaign_stage`, `rq_id`, `scenario_id`, `backend_mode`,
`configuration_id`, and `block_index`. It deliberately excludes both the seed
and assigned order. Re-randomizing the same logical block therefore preserves
its path-safe, 70-character identity.

After order assignment, every block is ranked with the global namespace. Its
global payload contains `block_id`, `configuration_id`, `block_index`, and
`order`. Blocks are sorted first by digest and then by `block_id`.
`sequence_index` is assigned only after this sort and covers
`1..total_block_count` continuously.

The `blocks` array must be materialized in increasing, canonical
`sequence_index` order. Each element's physical position in the array is part
of the canonical representation. Physically permuting `blocks`, even while
preserving every `sequence_index` and the same set of blocks, makes the
manifest invalid. Validators must not silently sort a noncanonical input
before accepting it.

`block_index` is local and stable within a configuration.
`sequence_index` is the global visit order across all configurations. Neither
field represents a period; period expansion belongs to Phase 19.7B-2b.

## Frozen catalog and external integrity

The manifest freezes the repository commit, profiles, block counts, source
paths, and source SHA-256 values used to define the randomized campaign.
Changing any of them creates different canonical content and must be treated
as a different frozen declaration.

The manifest has no self-hash. In particular, it cannot contain
`manifest_sha256` or `randomization_manifest_sha256`. External consumers
calculate:

```text
sha256_json(complete_manifest)
```

Readable persistence uses UTF-8, sorted keys, two-space indentation, and one
final newline. The readable whitespace is not part of the digest because the
digest uses canonical JSON bytes. Existing output paths are rejected; there is
no force-overwrite mode.

The manifest is preregistration input, not execution output. Results,
observations, `execution_id`, slots, attempts, and timestamps are prohibited.

## Future confirmatory interpretation

The intended future confirmatory design uses 50 paired blocks per
configuration. That means 50 observations under arm A and 50 under arm B,
implemented as 100 physical executions after period expansion in
Phase 19.7B-2b. Exact counterbalancing assigns 25 blocks to `AB` and 25 blocks
to `BA`. This interpretation does not itself create the definitive
confirmatory manifest or its execution plan.
