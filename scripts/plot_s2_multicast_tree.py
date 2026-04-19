#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
plot_multicast_tree.py
Desenha a árvore multicast do S2 a partir do sumário S2_<timestamp>.json e dos dom_*.json referenciados.

Uso:
  python3 scripts/plot_multicast_tree.py results/json/S2_20251008T124508Z.json
  # opcional: --no-svg para não tentar gerar SVG
"""

import argparse
import json
import shutil
from pathlib import Path
import subprocess
import sys

def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))

def guess_domain_label(dom_json: dict, domain_name: str) -> str:
    """Gera rótulo por domínio com base em backends/artifacts."""
    backends = dom_json.get("backends", [])
    artifacts = dom_json.get("artifacts", {})
    labels = [f"Domínio {domain_name}"]

    if backends:
        labels.append("backends=" + ",".join(backends))

    # heurísticas simples
    if "p4runtime_like" in backends or "p4runtime" in artifacts:
        labels.append("L2MC estático (P4)")
    elif "netconf_like" in backends or "netconf" in artifacts:
        labels.append("VLAN flood / L2MC estático (NETCONF-like)")
    else:
        labels.append("QoS/replicação na origem")

    return "\\n".join(labels)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("summary_json", help="Caminho para S2_<timestamp>.json")
    ap.add_argument("--no-svg", action="store_true", help="Não tenta renderizar SVG (apenas DOT)")
    args = ap.parse_args()

    sum_path = Path(args.summary_json)
    if not sum_path.exists():
        print(f"Erro: não encontrei {sum_path}", file=sys.stderr)
        sys.exit(2)

    summary = load_json(sum_path)
    ts = summary.get("timestamp_utc", "unknown")
    mode = summary.get("mode", "baseline")
    arts = summary.get("artifacts", {})
    dom_paths = arts.get("domains", {})

    out_dir = Path("results/graphs")
    out_dir.mkdir(parents=True, exist_ok=True)
    dot_path = out_dir / f"S2_{ts}_tree.dot"
    svg_path = out_dir / f"S2_{ts}_tree.svg"

    # Carrega domínios se presentes
    domJ = {}
    for d in ("A", "B", "C"):
        p = dom_paths.get(d)
        if p:
            fp = Path(p)
            if fp.exists():
                domJ[d] = load_json(fp)
            else:
                domJ[d] = {}
        else:
            domJ[d] = {}

    # Monta DOT (h1@A → {h3@B, h5@C})
    labA = guess_domain_label(domJ["A"], "A")
    labB = guess_domain_label(domJ["B"], "B")
    labC = guess_domain_label(domJ["C"], "C")

    dot = []
    dot.append('digraph S2Tree {')
    dot.append('  rankdir=LR;')
    dot.append('  node [shape=box, style="rounded,filled", fillcolor="#eef5ff"];')
    dot.append(f'  label="S2 multicast tree ({mode})\\n{ts}"; labelloc=top; fontsize=16;')

    # Domínios
    dot.append(f'  A [label="{labA}"];')
    dot.append(f'  B [label="{labB}"];')
    dot.append(f'  C [label="{labC}"];')

    # Hosts (fonte/receptores)
    dot.append('  h1 [shape=oval, fillcolor="#eaffea", label="h1@A (fonte)"];')
    dot.append('  h3 [shape=oval, fillcolor="#fff7e6", label="h3@B (receptor)"];')
    dot.append('  h5 [shape=oval, fillcolor="#fff7e6", label="h5@C (receptor)"];')

    # Ligações
    dot.append('  h1 -> A [label="QoS origem"];')
    dot.append('  A -> B [label="overlay (VXLAN/GRE)"];')
    dot.append('  A -> C [label="overlay (VXLAN/GRE)"];')
    dot.append('  B -> h3 [label="deliver"];')
    dot.append('  C -> h5 [label="deliver"];')

    dot.append('}')

    dot_path.write_text("\n".join(dot), encoding="utf-8")
    print(f"[ok] DOT salvo em: {dot_path}")

    if not args.no_svg and shutil.which("dot"):
        try:
            subprocess.run(["dot", "-Tsvg", str(dot_path), "-o", str(svg_path)],
                           check=True, capture_output=True, text=True)
            print(f"[ok] SVG salvo em: {svg_path}")
        except subprocess.CalledProcessError as e:
            print("[warn] Falha ao gerar SVG via graphviz/dot:", e.stderr.strip(), file=sys.stderr)
    elif not args.no_svg:
        print("[info] Graphviz 'dot' não encontrado — gerado apenas o DOT.")

if __name__ == "__main__":
    main()
