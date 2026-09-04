#!/usr/bin/env python3
"""Fail-closed compatibility gate for the retired S2 comparison workflow."""
raise SystemExit(
    "S2_COMPARE_MIGRATION_REQUIRED: use the official entrypoint "
    "`python -m scenarios.multidomain_s2`; comparison is not authorized by OC-R2"
)
