"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

# l2i/legacy_exec.py
# Executor "legacy" para aplicar QoS com tc/htb dentro do namespace do host Mininet.
# Usa o 'run' injetado (ex.: run=lambda cmd: h1.cmd(cmd)) – NUNCA chama tc no namespace global.
"""

from typing import Callable, Dict, Any, List

def _run_capture(run: Callable[[str], str], cmd: str) -> Dict[str, Any]:
    out = run(cmd)  # host.cmd(...) retorna stdout+stderr
    rc = 0
    err = ""
    # Heurísticas simples para detectar erro reportado em stdout
    bad_fragments = [
        "Cannot find device", "RTNETLINK answers", "Command not found", "No such file or directory"
    ]
    if any(x in out for x in bad_fragments):
        rc = 1
        err = out
    return {"cmd": cmd, "rc": rc, "out": "" if rc == 0 else "", "err": err}

def apply_tc_plan(ifname: str, ir_plan: Dict[str, Any], run: Callable[[str], str]) -> List[Any]:
    """
    Aplica um plano IR com HTB/SFQ/u32 em 'ifname', usando o 'run' do host Mininet.
    Retorna [lista_de_cmds_str, lista_de_resultados].
    - Ajusta r2q para reduzir 'quantum big' warnings.
    - Usa defaults conservadores (min=5 Mbps, max=8 Mbps) se IR não trouxer ações.
    """
    cmds: List[str] = []
    # Limpa raiz e cria HTB root com default 30 (fila best-effort)
    cmds.append(f"tc qdisc del dev {ifname} root || true")
    cmds.append(f"tc qdisc add dev {ifname} root handle 1: htb default 30 r2q 250")
    cmds.append(f"tc class add dev {ifname} parent 1: classid 1:30 htb rate 100mbit ceil 100mbit prio 7")
    cmds.append(f"tc qdisc add dev {ifname} parent 1:30 handle 30: sfq perturb 10")

    # Defaults caso o IR não tenha ações explícitas
    rate_min_kbit = 5000    # 5 Mbps
    rate_ceil_kbit = 8000   # 8 Mbps
    leaf_class = "1:10"

    # Mapeia ações do IR (ReserveMinRate/CapMaxRate)
    for act in ir_plan.get("actions", []):
        if act.get("type") == "ReserveMinRate":
            try:
                rate_min_kbit = int(float(act["params"]["min_mbps"]) * 1000)
            except Exception:
                pass
        elif act.get("type") == "CapMaxRate":
            try:
                rate_ceil_kbit = int(float(act["params"]["max_mbps"]) * 1000)
            except Exception:
                pass

    # Classe do fluxo de interesse (prioritária)
    cmds.append(
        f"tc class add dev {ifname} parent 1: classid {leaf_class} htb "
        f"rate {rate_min_kbit}kbit ceil {rate_ceil_kbit}kbit prio 1 quantum 1514"
    )
    cmds.append(f"tc qdisc add dev {ifname} parent {leaf_class} handle 10: sfq perturb 10")

    # Classificador genérico (pode evoluir para 5-tupla)
    cmds.append(
        f"tc filter add dev {ifname} protocol ip parent 1: prio 2 u32 "
        f"match ip protocol 0x0000 flowid {leaf_class}"
    )

    results = [_run_capture(run, c) for c in cmds]
    return [cmds, results]
