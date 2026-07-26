#!/usr/bin/env python3
"""Validate the preregistered Phase 19 S1 experimental plan."""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "config" / "phase19_s1_experiment_plan_v1.json"


def require(name: str, condition: bool) -> None:
    """Emit one stable marker and stop at the first violated invariant."""

    print(f"PHASE19_S1_PLAN_CHECK_{name}={condition}")
    if not condition:
        raise SystemExit(1)


def canonical_json_sha256(value: Any) -> str:
    """Hash a JSON-compatible value using the experiment contract encoding."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    """Hash one local file without changing it."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expand_final_schedule(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand the deterministic final-campaign rule into execution slots."""

    final = plan["campaign"]["final"]
    cycle = final["profile_order_cycle"]
    offsets = final["mode_order_rule"]["profile_offsets"]
    pair_count = int(final["pair_count_per_profile"])
    rows: list[dict[str, Any]] = []
    ordinal = 0

    for pair_index in range(1, pair_count + 1):
        profile_order = cycle[(pair_index - 1) % len(cycle)]
        for profile_id in profile_order:
            baseline_first = (
                pair_index + int(offsets[profile_id])
            ) % 2 == 0
            modes = (
                ["baseline", "adapt"]
                if baseline_first
                else ["adapt", "baseline"]
            )
            pair_id = (
                f"S1-FINAL-{profile_id.upper()}-P{pair_index:02d}"
            )
            for mode in modes:
                ordinal += 1
                rows.append(
                    {
                        "execution_ordinal": ordinal,
                        "pair_id": pair_id,
                        "profile_id": profile_id,
                        "pair_index": pair_index,
                        "mode": mode,
                        "run_slot_id": f"{pair_id}-{mode}",
                    }
                )
    return rows


def main() -> None:
    """Validate sources, profiles, schedules, retention, and analysis scope."""

    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))

    require(
        "VERSION_AND_STATUS",
        plan["contract_version"] == "phase19-s1-experiment-plan-v1"
        and plan["preregistration"]["status"]
        == "frozen-before-real-observation"
        and plan["preregistration"]["observation_boundary"]
        == "before_first_real_backend_preflight",
    )
    require(
        "DESIGN_BASE",
        plan["preregistration"]["design_base_commit"]
        == "ea524a2a6810d250dd9c60b9984b40b46ae3c248",
    )

    source_hashes = plan["preregistration"]["source_hashes"]
    observed_hashes = {
        relative_path: file_sha256(ROOT / relative_path)
        for relative_path in source_hashes
    }
    require("SOURCE_HASHES", observed_hashes == source_hashes)

    scenario = plan["scenario"]
    require(
        "CANONICAL_SCENARIO",
        scenario
        == {
            "entrypoint": "scenarios/multidomain_s1.py",
            "scenario_id": "S1",
            "specification": "specs/valid/s1_unicast_qos.json",
        },
    )

    configuration = plan["fixed_configuration"]
    intent = configuration["intent"]
    require(
        "FIXED_CONFIGURATION",
        configuration["sensitive_offered_mbps"] == 8
        and configuration["capacities_mbps"]
        == {"A": 100, "B": 50, "C": 100}
        and configuration["delay_ms_per_shaped_egress"] == 1
        and configuration["rtt_interval_ms"] == 50
        and configuration["bandwidth_tolerance_mbps"] == 0.25
        and intent
        == {
            "bandwidth_min_mbps": 4,
            "delivery_min_ratio": 0.99,
            "latency_max_ms": 30,
            "latency_percentile": "P99",
        },
    )

    profiles = configuration["best_effort_profiles"]
    profile_by_id = {
        profile["profile_id"]: profile
        for profile in profiles
    }
    capacities = configuration["capacities_mbps"]
    flow_mbps = configuration["sensitive_offered_mbps"]
    profile_math_valid = len(profile_by_id) == len(profiles) == 3
    for profile_id, expected_best_effort in (
        ("light", 45),
        ("nominal", 60),
        ("severe", 90),
    ):
        profile = profile_by_id.get(profile_id, {})
        total = flow_mbps + expected_best_effort
        excess_ratio = (total - capacities["B"]) / capacities["B"]
        profile_math_valid = (
            profile_math_valid
            and profile.get("best_effort_mbps") == expected_best_effort
            and profile.get("offered_total_mbps") == total
            and math.isclose(
                float(profile.get("bottleneck_excess_ratio", -1)),
                excess_ratio,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            and total > capacities["B"]
        )
    require("CONTENTION_PROFILES", profile_math_valid)

    mock_preflight = plan["preflight"]["dynamic_mock"]
    real_preflight = plan["preflight"]["dynamic_real"]
    require(
        "PREFLIGHT_EXCLUDED",
        mock_preflight["approved"] is True
        and mock_preflight["scientific_use"] == "excluded"
        and mock_preflight["report_sha256"]
        == "058848fcabcde03da15ffc2a4e4700f92b25bc0826e2d66d67bcb1734ec7386d"
        and real_preflight["scientific_use"] == "excluded"
        and real_preflight["backend"] == "real"
        and real_preflight["duration_s"] == 3
        and real_preflight["best_effort_profile"] == "nominal"
        and real_preflight["mode_order"] == ["baseline", "adapt"]
        and real_preflight["services_must_be_stopped_afterward"] is True,
    )
    require(
        "REAL_PREFLIGHT_DOMAINS",
        real_preflight["required_adapt_domains"]
        == ["A_environment", "A_intent_overlay", "B", "C"],
    )

    foundation = plan["campaign"]["foundation"]
    foundation_schedule = foundation["schedule"]
    foundation_profiles = Counter(
        pair["profile_id"]
        for pair in foundation_schedule
    )
    foundation_orders: dict[str, Counter[tuple[str, ...]]] = defaultdict(
        Counter
    )
    for pair in foundation_schedule:
        foundation_orders[pair["profile_id"]][
            tuple(pair["mode_order"])
        ] += 1
    require(
        "FOUNDATION_SIZE_AND_EXCLUSION",
        foundation["backend"] == "real"
        and foundation["duration_s"] == 30
        and foundation["pair_count_per_profile"] == 2
        and foundation["run_count"] == 12
        and foundation["scientific_use"] == "excluded"
        and len(foundation_schedule) == 6,
    )
    require(
        "FOUNDATION_BALANCE",
        foundation_profiles
        == Counter({"light": 2, "nominal": 2, "severe": 2})
        and all(
            foundation_orders[profile_id]
            == Counter(
                {
                    ("baseline", "adapt"): 1,
                    ("adapt", "baseline"): 1,
                }
            )
            for profile_id in profile_by_id
        ),
    )
    require(
        "FOUNDATION_IDENTITIES_UNIQUE",
        len(
            {
                pair["pair_id"]
                for pair in foundation_schedule
            }
        )
        == len(foundation_schedule),
    )

    final = plan["campaign"]["final"]
    expanded = expand_final_schedule(plan)
    expanded_profiles = Counter(
        row["profile_id"]
        for row in expanded
    )
    first_modes: dict[str, Counter[str]] = defaultdict(Counter)
    final_pairs_valid = True
    for index in range(0, len(expanded), 2):
        first = expanded[index]
        second = expanded[index + 1]
        final_pairs_valid = (
            final_pairs_valid
            and first["pair_id"] == second["pair_id"]
            and first["profile_id"] == second["profile_id"]
            and {first["mode"], second["mode"]}
            == {"baseline", "adapt"}
        )
        first_modes[first["profile_id"]][first["mode"]] += 1

    require("FINAL_PAIRS_WELL_FORMED", final_pairs_valid)
    require(
        "FINAL_SIZE",
        final["backend"] == "real"
        and final["duration_s"] == 30
        and final["pair_count_per_profile"] == 30
        and final["expanded_run_count"] == 180
        and final["scientific_use"] == "included"
        and len(expanded) == 180,
    )
    require(
        "FINAL_PROFILE_COUNTS",
        expanded_profiles
        == Counter({"light": 60, "nominal": 60, "severe": 60}),
    )
    require(
        "FINAL_ORDER_BALANCE",
        all(
            first_modes[profile_id]
            == Counter({"baseline": 15, "adapt": 15})
            for profile_id in profile_by_id
        ),
    )
    require(
        "FINAL_SCHEDULE_HASH",
        canonical_json_sha256(expanded)
        == final["expanded_schedule_sha256"],
    )
    require(
        "FINAL_PREREQUISITES",
        final["prerequisites"]
        == [
            "canonical_s2_implementation_certified",
            "statistical_pipeline_certified",
            "final_campaign_runner_certified",
        ]
        and final["early_stopping_prohibited"] is True,
    )

    policy = plan["execution_policy"]
    invalid_attempt = policy["invalid_attempt"]
    require(
        "RETENTION_AND_REPLACEMENT",
        policy["nonconforming_but_valid_attempt_must_be_retained"] is True
        and policy["outcome_based_repetition_or_exclusion_prohibited"] is True
        and policy["topology_recreated_for_every_execution"] is True
        and policy["unique_run_slot_and_attempt_identity_required"] is True
        and invalid_attempt["original_attempt_must_be_preserved"] is True
        and invalid_attempt[
            "replacement_attempt_must_increment_attempt_number"
        ]
        is True,
    )
    require(
        "INVALIDITY_CRITERIA",
        set(invalid_attempt["criteria"])
        == {
            "repository_provenance_gate_failed",
            "artifact_integrity_gate_failed",
            "backend_apply_or_readback_gate_failed",
            "data_plane_process_exit_gate_failed",
            "measurement_completeness_gate_failed",
            "simultaneous_window_gate_failed",
        },
    )

    analysis = plan["analysis"]
    require(
        "PAIRED_PRIMARY_OUTCOME",
        analysis["inference_unit"] == "paired execution"
        and analysis["estimand"] == "within-pair adapt minus baseline"
        and analysis["primary_metric"]
        == {
            "field": "metrics.rtt_p99_ms",
            "name": "rtt_p99_ms",
            "preferred_direction": "negative",
        }
        and analysis["within_run_probe_pseudoreplication_prohibited"]
        is True,
    )
    require(
        "CAUSAL_SCOPE",
        plan["causal_scope"]
        == {
            "forwarding_path": "linux-bridge-veth",
            "netconf": "materialization-and-readback-only",
            "p4runtime": "materialization-and-readback-only",
            "traffic_effect_backend": "linux-tc",
        },
    )

    print(
        "PHASE19_S1_EXPERIMENT_PLAN_SHA256="
        + file_sha256(PLAN_PATH)
    )
    print("PHASE19_S1_EXPERIMENT_PLAN_VALIDATION_OK")


if __name__ == "__main__":
    main()
