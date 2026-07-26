# Phase 19 canonical S1 design

## Scope

`scenarios/multidomain_s1.py` is the only canonical S1 entrypoint. The scenario
evaluates one latency-sensitive unicast flow and one best-effort flow under
simultaneous contention. It also records heterogeneous control-domain
materialization evidence.

The measured forwarding path is implemented with Linux network namespaces,
bridges, veth pairs, and `tc`/HTB. NETCONF and P4Runtime targets are not on this
forwarding path. Their evidence is therefore limited to successful
materialization and readback. S1 does not causally attribute RTT or throughput
changes to NETCONF or P4Runtime.

## Canonical topology

The sensitive flow originates at `h1`, the best-effort flow originates at `h2`,
and both terminate at `h3`. The A, B, and C segments are connected sequentially.
The `s1-bc-b` egress is the shared and uniquely lowest-capacity bottleneck.

The same environmental capacities and delay are applied in `baseline` and
`adapt`. In `baseline`, all measured traffic uses the default best-effort HTB
class. In `adapt`, TCP traffic to destination port 5201 and ICMP latency probes
to `h3` are both classified into the same protected class. HTB scheduler
priorities are explicit; class identifiers are never interpreted as scheduler
precedence.

## Measurement validity

The sensitive TCP flow, best-effort TCP flow, and ICMP probes start through a
shared synchronization barrier. Their observed start and end times are stored,
and at least 90 percent of the configured traffic duration must be simultaneous.

Every subprocess exit code is recorded. Iperf output must contain a complete,
positive receiver summary. RTT samples must be non-empty and complete:
transmitted, received, requested, and parsed sample counts must match. Missing
probes are included in delivery ratio and invalidate the measurement rather
than being silently excluded from conformance.

## Bandwidth semantics

`requirements.bandwidth.min_mbps` is mandatory. The observed sensitive
throughput must meet this lower bound within the recorded measurement
tolerance. `max_mbps` is optional. When absent, no intent-level upper bound is
invented. The physical HTB ceiling remains a realization constraint recorded
separately. When `max_mbps` is present, both the lower and upper bounds are
evaluated.

## Persistence and failure behavior

Each execution consumes the Phase 19 immutable experiment contract. It records
scenario, execution, profile, mode, backend, repetition, Git provenance,
specification hash, canonical configuration hash, and an exclusive artifact
directory. The repository must be a clean, synchronized `develop` checkout.

Backend application, readback, preflight, process exit, sample completeness,
and simultaneous-window checks fail closed. A failure produces an atomic
failure summary and a non-zero process exit. Control-plane, data-plane,
preparation, and total script timings are stored separately.
