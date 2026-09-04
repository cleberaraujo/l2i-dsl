#!/usr/bin/env python3
"""Fail-closed compatibility gate for the retired S2 sweep workflow."""
raise SystemExit(
    "S2_SWEEP_MIGRATION_REQUIRED: use the official entrypoint "
    "`python -m scenarios.multidomain_s2`; sweep execution is not authorized by OC-R2"
)
