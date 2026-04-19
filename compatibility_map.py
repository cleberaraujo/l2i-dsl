#!/usr/bin/env python3
"""
compatibility_map.py - Ferramenta auxiliar para verificar quais specs
são aceitos (allow), ajustados (adjust) ou negados (deny) em cada domínio.

Uso:
    python3 compatibility_map.py

Saída:
    Um JSON com o mapa de compatibilidade.
"""

import json, dataclasses
from pathlib import Path

from l2i.validator import validate_spec
from l2i.capabilities import check_capabilities
from l2i.policies import apply_policies

# Helper para serializar dataclasses/objetos customizados (ex: ErrorMsg)
def json_default(o):
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)

BASE = Path(__file__).resolve().parents[0]  # pasta atual
spec_dir = BASE / "v0/specs/valid"
profiles = {
    "A": json.load(open(BASE / "profiles/domA.json")),
    "B": json.load(open(BASE / "profiles/domB.json")),
    "C": json.load(open(BASE / "profiles/domC.json")),
}

results = {}

for spec_file in sorted(spec_dir.glob("*.json")):
    spec_name = spec_file.name
    spec = json.load(open(spec_file))
    cspec, errs = validate_spec(spec)

    if errs:
        # se houver erros de validação, só registra eles
        results[spec_name] = {"validation": [e.code for e in errs]}
        continue

    results[spec_name] = {}
    for dom, prof in profiles.items():
        # aplicar políticas
        status, spec2, msgs = apply_policies(cspec)
        if status == "deny":
            if msgs and hasattr(msgs[0], "code"):
                results[spec_name][dom] = f"deny ({msgs[0].code})"
            else:
                results[spec_name][dom] = "deny"
            continue

        # checar capacidades
        status, spec3, msgs = check_capabilities(spec2, prof)
        if status == "deny":
            if msgs and hasattr(msgs[0], "code"):
                results[spec_name][dom] = f"deny ({msgs[0].code})"
            else:
                results[spec_name][dom] = "deny"
        elif status == "adjust":
            results[spec_name][dom] = f"adjust ({msgs[0]})"
        else:
            results[spec_name][dom] = "allow"

print(json.dumps(results, indent=2, default=json_default))

