# Phase 19 S1 preregistered experiment protocol

## Purpose and observation boundary

This protocol freezes the S1 experimental design before the first execution
that exercises the real NETCONF and P4Runtime backends. Its machine-readable
counterpart is `config/phase19_s1_experiment_plan_v1.json`. The frozen plan is
bound to the canonical S1 scenario, specification, and design document by
SHA-256. Any subsequent change to those sources, to this protocol, or to the
machine-readable plan constitutes a protocol amendment and must be committed
and justified before the affected campaign begins.

The previously approved dynamic mock preflight establishes only that the
baseline and adaptation paths execute, that packet loss is explicitly
accounted for, that evidence ownership is restored, and that topology cleanup
is effective. Its measurements are operational evidence and cannot be reused as
scientific observations. The short real preflight defined here has the same
exclusion: it evaluates backend health, application, readback, persistence, and
cleanup, but it does not belong to either the foundation matrix or the final
campaign.

## Protocol amendment 001: primary-metric accessor

After approval of the excluded real preflight and before any foundation
execution, a static comparison between the machine-readable plan and the
canonical summary schema identified that the plan named the primary outcome
correctly but pointed to a nonexistent flat field, `metrics.rtt_p99_ms`. The
canonical scenario records that same P99 RTT value at `metrics.rtt_ms.p99` and
also exposes it as the selected percentile at `metrics.rtt_percentile_ms`.

Amendment 001 changes only the machine-readable accessor from
`metrics.rtt_p99_ms` to `metrics.rtt_ms.p99`. The primary outcome remains the
within-pair difference `adapt - baseline` in P99 RTT, with negative values
preferred. The correction does not change the scenario source, offered loads,
topology, treatment, measurement procedure, conformance thresholds, schedule,
retention rules, or causal scope. Its basis is the static output schema rather
than any favorable, unfavorable, or null preflight measurement. The preceding
real preflight remains operational evidence excluded from scientific analysis;
its report was sealed as
`51a07ed8edabaaf8fe14dd475361698e09e0e841433e8c251c0b10db25421157`.

## Fixed experimental configuration

Every foundation or final S1 execution uses the canonical sensitive load of
8 Mbit/s, domain capacities of 100, 50, and 100 Mbit/s for A, B, and C,
respectively, a 1 ms delay on each shaped egress, an RTT probe interval of
50 ms, a duration of 30 s, and the real backend. The declared intent requires
P99 RTT no greater than 30 ms, delivery of at least 0.99, and sensitive
throughput of at least 4 Mbit/s. The measurement tolerance for the bandwidth
lower bound is fixed at 0.25 Mbit/s.

The only planned load factor is the best-effort offered rate. The three
profiles are fixed as follows.

| Profile | Best effort | Total offered load | Excess over domain B |
|---|---:|---:|---:|
| light | 45 Mbit/s | 53 Mbit/s | 6% |
| nominal | 60 Mbit/s | 68 Mbit/s | 36% |
| severe | 90 Mbit/s | 98 Mbit/s | 96% |

## Real preflight

The real preflight consists of one short nominal pair in the fixed order
`baseline` followed by `adapt`, with 3 s of traffic per execution. Both modes
must pass repository provenance, Linux environment readback, data-plane process
status, measurement completeness, and simultaneous-window gates. The
adaptation execution must additionally prove Linux intent-overlay application,
NETCONF application and running-state readback, and P4Runtime application and
table readback. The runner must start from an inactive service state and must
stop Netopeer2, BMv2/P4Runtime, residual `iperf3` processes, and the S1 topology
before reporting approval.

A favorable latency or throughput contrast is neither required nor interpreted
in this preflight. Conversely, a valid nonconforming observation does not make
the preflight operationally invalid. Approval depends on integrity and backend
gates, not on the direction of the measured effect.

## Foundation matrix

The foundation matrix contains two paired baseline/adaptation comparisons per
profile, for a total of six pairs and twelve executions. Each profile has one
pair in each mode order. Profile position and mode order are interleaved
according to the explicit schedule in the machine-readable plan. The matrix is
preserved as diagnostic evidence but excluded from the inferential results of
the article. Its purpose is to evaluate operational repeatability, artifact
integrity, service stability, and behavior across the three predetermined
contention intensities. No profile may be removed, replaced, or retuned because
its observed contrast is small, absent, adverse, or favorable.

## Final campaign and schedule

The final campaign remains blocked until the canonical S2 implementation, the
statistical pipeline, and the final campaign runner have been independently
certified. Once those gates are satisfied, S1 contains 30 pairs per profile,
equivalent to 90 pairs and 180 executions. For each pair index, the three
profiles follow a fixed rotating order. Within each profile, the first mode is
determined by the parity rule recorded in the machine-readable plan, yielding
exactly 15 `baseline`-first and 15 `adapt`-first pairs per profile. Expansion of
that deterministic rule produces a 180-row schedule whose canonical JSON hash
is also frozen in the plan.

Topology is recreated for every execution. Run slots and attempt numbers are
distinct, and artifact directories cannot be overwritten. Early stopping based
on favorable or unfavorable results is prohibited.

## Validity, replacement, and retention

Packet loss is a measured outcome. When transmitted probes are completely
accounted for as received or lost, the execution remains a valid observation;
delivery conformance is evaluated separately. An execution may receive a new
attempt only when a prespecified integrity gate fails, including provenance,
artifact integrity, backend application or readback, subprocess status,
measurement completeness, or simultaneous-window validity. The original
attempt is always retained, and a replacement uses an incremented attempt
number. Repetition or exclusion based on latency, delivery, throughput, or
overall intent conformance is prohibited.

## Analysis and causal scope

The statistical unit is the paired execution, not an individual ICMP probe.
The primary outcome is the within-pair difference `adapt - baseline` in P99 RTT;
a negative value represents lower latency under adaptation. Delivery ratio,
sensitive throughput, best-effort throughput, and global intent conformance are
secondary outcomes. Probe-level observations may characterize a run but cannot
be treated as independent experimental replicates.

The forwarding path is implemented with Linux bridges and veth pairs. Causal
claims about RTT and throughput are therefore restricted to the Linux
`tc`/HTB realization. NETCONF and P4Runtime support claims of heterogeneous
materialization and readback only; their participation does not establish that
they caused a measured traffic effect in S1.
