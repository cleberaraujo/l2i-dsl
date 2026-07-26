# Phase 19 canonical audit of S1 and S2

## Status and decision

The Phase 19 audit was performed at commit
`5b56376dee45c481f429a058040f9b69d21492d0`, tagged as
`phase18-selective-multidomain-assurance-v1.0.0`. The historical S1 and S2
outputs remain preserved for traceability, but they are not approved as final
article evidence. No result file is deleted or rewritten by this decision.

The final campaign must use explicit execution directories, immutable run
identity, specification and configuration hashes, Git provenance, fail-closed
operational gates, synchronized traffic windows, and analysis code that never
selects an artifact by modification time.

## S1 findings

`scenarios/multidomain_s1.py` remains the target canonical S1 entry point, but
its current traffic and conformance path is not suitable for the final
campaign.

The sensitive and best-effort iperf3 clients execute sequentially. RTT samples
are collected only after both clients terminate. Consequently, the current
output does not measure latency, isolation, or survival under simultaneous
contention. The flow offered load is derived from `max_mbps`; the active
specification omits that optional field, which reduces the offered load to
`0.1 Mbit/s`. Bandwidth conformance checks only an upper bound and does not
validate the declared minimum. An empty RTT sample set is represented by zero,
which can incorrectly satisfy a latency bound. The value named total
control-plane time spans traffic generation and measurement, so it is not a
control-plane metric.

The current Linux classifier selects ICMP, whereas the iperf3 sensitive flow is
TCP. The NETCONF and P4Runtime domains are materialized and read back, but the
plain Linux bridge topology does not put either domain in the measured data
path. These domains may support control-plane materialization claims, but they
must not be credited with an observed data-plane improvement unless the final
topology actually traverses them.

## S2 findings

The repository does not currently have a single canonical S2 implementation.
The file named `scenarios/multicast_s2.py` is not the module invoked by
`setup_all.sh` or the experiment documentation. It also generates sequential
unicast TCP traffic and no end-to-end multicast data-plane traffic.

The dispatched module, `scenarios/multicast_s2_recovery_stable5.py`, explicitly
limits its multicast evidence to P4Runtime control-plane programming and
readback. Its support TCP and ICMP traffic crosses a Linux bridge rather than
BMv2. NETCONF is likewise a datastore control endpoint rather than an in-path
data-plane element. Therefore support-traffic changes cannot be interpreted as
multicast forwarding recovery caused by the P4 event.

The recovery delivery ratio is computed over received RTT records rather than
expected probes, so a bin with only successful received records can report
`1.0` despite missing probes. Recovery is referenced to the configured end of
the event phase rather than the observed completion timestamp of P4
programming. These defects invalidate that metric for the final campaign.

The validated Phase 12--18 P4 multicast data-plane, contention, state recovery,
and assurance primitives are the approved implementation basis for the new S2
canonical scenario. The `stable*` modules remain historical sources until the
validated mechanisms are integrated into a new single entry point.

## Legacy tooling

The current batch, sweep, comparison, and aggregation scripts are not approved
for the final campaign. Some select the newest result by modification time,
some parse only the final stdout line although scenarios emit multiline JSON,
and some expect obsolete result schemas. They will be replaced or formally
marked as legacy after the canonical scenario contracts stabilize.

## Required implementation order

1. Introduce and validate the shared experiment identity and artifact contract.
2. Reconstruct S1 with concurrent sensitive and best-effort traffic, RTT during
   the same contention interval, correct optional min/max semantics, explicit
   delivery requirements, separate control/data-plane timing, and fail-closed
   backend/readback gates.
3. Validate S1 in a small foundation matrix before any article campaign.
4. Reconstruct S2 around the already validated BMv2 multicast data path and
   measure delivery and recovery from observed event timestamps.
5. Replace the batch, aggregation, statistical, table, and figure pipeline.
6. Run a new counterbalanced campaign whose outputs alone feed the article.

## Scope discipline

The audit does not claim that prior results were fabricated. It establishes
that their current implementation and provenance are insufficient for the
strong empirical claims intended for the final manuscript. Corrections must
improve measurement validity and traceability; they must never tune code to
produce predetermined favorable values.
