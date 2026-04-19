#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_s1_topology.py
Cria a topologia L2 para o cenário S1 (unicast QoS) com três namespaces (h1,h2,h3)
e três bridges (brA, brB, brC), conectadas por veth-pairs A-B e B-C.

Idempotente: só cria o que estiver faltando.
"""

import subprocess as sp

# -----------------------
# Parâmetros da topologia
# -----------------------
H = {
    "h1": {"if": "h1-eth0", "tap": "tap_h1-eth0", "ip": "10.0.0.1/24", "br": "brA"},
    "h2": {"if": "h2-eth0", "tap": "tap_h2-eth0", "ip": "10.0.0.2/24", "br": "brB"},
    "h3": {"if": "h3-eth0", "tap": "tap_h3-eth0", "ip": "10.0.0.3/24", "br": "brC"},
}
BRIDGES = ["brA", "brB", "brC"]
# Enlaces inter-domínios
AB = ("A-B", "B-A")
BC = ("B-C", "C-B")

# -------------
# Funções util
# -------------
def run(cmd, check=True, capture=False):
    p = sp.run(cmd, text=True, capture_output=capture)
    if check and p.returncode != 0:
        raise RuntimeError(
            f"cmd failed: {' '.join(cmd)}\nRC={p.returncode}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}"
        )
    return p.stdout if capture else ""

def link_exists(name):
    out = run(["bash", "-lc", f"ip -o link show {name} 2>/dev/null | wc -l"], capture=True)
    return out.strip() != "0"

def ns_exists(name):
    out = run(["bash", "-lc", f"ip netns list | awk '{{print $1}}' | grep -w {name} | wc -l"], capture=True)
    return out.strip() != "0"

def br_has_port(br, ifc):
    out = run(["bash", "-lc", f"bridge link show dev {ifc} 2>/dev/null | wc -l"], capture=True)
    return out.strip() != "0"

def ensure_ns(name):
    if not ns_exists(name):
        run(["ip", "netns", "add", name])
    # loopback up
    run(["ip", "netns", "exec", name, "ip", "link", "set", "lo", "up"], check=False)

def ensure_veth(a, b):
    if not link_exists(a) and not link_exists(b):
        run(["ip", "link", "add", a, "type", "veth", "peer", "name", b])

def ensure_bridge(br):
    if not link_exists(br):
        run(["ip", "link", "add", br, "type", "bridge"])
        # Melhora previsibilidade (opcional)
        run(["ip", "link", "set", br, "type", "bridge", "stp_state", "0"], check=False)
    run(["ip", "link", "set", br, "up"])

def set_up(ifc):
    run(["ip", "link", "set", ifc, "up"])

def add_to_bridge(br, ifc):
    # já está na bridge? deixe quieto
    if br_has_port(br, ifc):
        return
    run(["ip", "link", "set", ifc, "master", br])

def move_to_ns(ifc, ns):
    # só move se ainda não estiver em um netns (ip link show traz "master" ou "state" sem " if ")
    run(["ip", "link", "set", ifc, "netns", ns], check=False)

def ns_set_up(ns, ifc):
    run(["ip", "netns", "exec", ns, "ip", "link", "set", ifc, "up"])

def ns_addr_add(ns, ifc, cidr):
    # idempotente: "replace" troca sem erro
    run(["ip", "netns", "exec", ns, "ip", "addr", "replace", cidr, "dev", ifc])

# -------------------------
# Montagem da topologia S1
# -------------------------
def main():
    print("[S1-setup] criando namespaces…")
    for ns in H:
        ensure_ns(ns)

    print("[S1-setup] criando bridges…")
    for br in BRIDGES:
        ensure_bridge(br)

    print("[S1-setup] criando veths host<->tap…")
    for ns, info in H.items():
        ifc, tap = info["if"], info["tap"]
        ensure_veth(ifc, tap)
        # mover a perna do host para o namespace e subir
        move_to_ns(ifc, ns)
        ns_set_up(ns, ifc)
        # tap fica no namespace root, e sobe
        set_up(tap)
        # prender o tap à sua bridge
        add_to_bridge(info["br"], tap)

    print("[S1-setup] criando enlaces A-B e B-C…")
    ensure_veth(*AB)
    ensure_veth(*BC)
    # subir as pernas nos bridges correspondentes
    set_up(AB[0]); set_up(AB[1]); set_up(BC[0]); set_up(BC[1])
    add_to_bridge("brA", AB[0])  # A-B na brA
    add_to_bridge("brB", AB[1])  # B-A na brB
    add_to_bridge("brB", BC[0])  # B-C na brB
    add_to_bridge("brC", BC[1])  # C-B na brC

    print("[S1-setup] endereçando h1,h2,h3…")
    for ns, info in H.items():
        ns_addr_add(ns, info["if"], info["ip"])

    # Report simples
    print("\n[ok] Topologia S1 pronta:")
    print("  - namespaces: h1(10.0.0.1/24), h2(10.0.0.2/24), h3(10.0.0.3/24)")
    print("  - bridges: brA(h1,towards B), brB(core), brC(h3,towards B)")
    print("  - enlaces: A-B (brA<->brB), B-C (brB<->brC)")
    print("\nPara testar conectividade:")
    print("  sudo ip netns exec h1 ping -c 2 10.0.0.3\n")

if __name__ == "__main__":
    main()
