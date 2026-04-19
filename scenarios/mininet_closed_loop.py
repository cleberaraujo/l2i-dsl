#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Mininet testbed headless para S1/S2.

- Sobe 3 domínios com hosts h1 (A), h3 (B), h5 (C) e switches s1, s2, s3.
- Links com TCLink parametrizados por --bwA/--bwB/--bwC e --delay-ms.
- Sem controlador externo (inicia switches com start([])).
- Modo --headless: imprime "READY h1,h3,h5", cria symlinks de netns em /var/run/netns
  e fica em execução até SIGTERM/SIGINT, limpando tudo ao sair.

IPs (compatíveis com S1/S2):
  h1 = 10.0.0.1/24
  h3 = 10.0.0.3/24
  h5 = 10.0.0.5/24
"""

import argparse
import os
import signal
import sys
import time
import subprocess
from pathlib import Path

from mininet.net import Mininet
from mininet.node import Host, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info

# --- util: symlinks /var/run/netns/<name> -> /proc/<pid>/ns/net ----------------

NETNS_DIR = Path("/var/run/netns")

def _ensure_netns_dir():
    try:
        NETNS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        info(f"*** Aviso: não foi possível criar {NETNS_DIR}: {e}\n")

def link_host_netns(host):
    """
    Cria/atualiza o symlink /var/run/netns/<host.name> apontando para /proc/<pid>/ns/net
    para permitir 'ip netns exec <name> ...' a partir de outros processos.
    """
    _ensure_netns_dir()
    link = NETNS_DIR / host.name
    target = Path(f"/proc/{host.pid}/ns/net")
    try:
        if link.is_symlink() or link.exists():
            link.unlink(missing_ok=True)
        # usar -T behavior: em Python, symlink cria direto
        os.symlink(target, link)
    except Exception as e:
        info(f"*** Aviso: falhou ao criar symlink de netns para {host.name}: {e}\n")

def unlink_host_netns(hostname: str):
    link = NETNS_DIR / hostname
    try:
        if link.is_symlink() or link.exists():
            link.unlink(missing_ok=True)
    except Exception as e:
        info(f"*** Aviso: falhou ao remover symlink {link}: {e}\n")

def link_all_hosts(net: Mininet):
    for hn in ("h1", "h3", "h5"):
        try:
            h = net.get(hn)
            link_host_netns(h)
        except Exception as e:
            info(f"*** Aviso: não consegui linkar netns de {hn}: {e}\n")

def unlink_all_hosts():
    for hn in ("h1", "h3", "h5"):
        unlink_host_netns(hn)

# --- topo ----------------------------------------------------------------------

def build_topo(bwA: int, bwB: int, bwC: int, delay_ms: int) -> Mininet:
    net = Mininet(link=TCLink, switch=OVSSwitch, controller=None, autoSetMacs=True)

    # Switches por domínio
    s1 = net.addSwitch("s1")
    s2 = net.addSwitch("s2")
    s3 = net.addSwitch("s3")

    # Hosts por domínio
    h1 = net.addHost("h1", ip="10.0.0.1/24")
    h3 = net.addHost("h3", ip="10.0.0.3/24")
    h5 = net.addHost("h5", ip="10.0.0.5/24")

    # Conexões host <-> switch
    net.addLink(h1, s1, bw=bwA, delay=f"{delay_ms}ms")
    net.addLink(h3, s2, bw=bwB, delay=f"{delay_ms}ms")
    net.addLink(h5, s3, bw=bwC, delay=f"{delay_ms}ms")

    # Backbone entre domínios (s1-s2-s3)
    net.addLink(s1, s2, bw=min(bwA, bwB), delay=f"{delay_ms}ms")
    net.addLink(s2, s3, bw=min(bwB, bwC), delay=f"{delay_ms}ms")

    net.build()

    # Iniciar switches sem controlador
    for sw in (s1, s2, s3):
        sw.start([])

    return net

def main():
    ap = argparse.ArgumentParser(description="Mininet Closed Loop headless/CLI para S1/S2")
    ap.add_argument("--bwA", type=int, default=100, help="Mbps no domínio A (h1-s1)")
    ap.add_argument("--bwB", type=int, default=50, help="Mbps no domínio B (h3-s2)")
    ap.add_argument("--bwC", type=int, default=100, help="Mbps no domínio C (h5-s3)")
    ap.add_argument("--delay-ms", type=int, default=1, help="Atraso (ms) em todos os enlaces")
    ap.add_argument("--headless", action="store_true", help="Roda sem CLI e fica em loop até SIGTERM")
    args = ap.parse_args()

    setLogLevel("info")

    info("*** Construindo topologia...\n")
    net = build_topo(args.bwA, args.bwB, args.bwC, args.delay_ms)
    info("*** Iniciando rede...\n")
    net.start()

    # Evitar forward no host
    h1, h3, h5 = net.get("h1", "h3", "h5")
    for h in (h1, h3, h5):
        h.cmd("sysctl -w net.ipv4.ip_forward=0 >/dev/null 2>&1")

    # Materializa symlinks para ip netns exec funcionar em outros processos
    link_all_hosts(net)

    def cleanup_and_exit(signum=None, frame=None):
        info("\n*** Encerrando testbed (signal: %s) ...\n" % str(signum))
        try:
            # remove symlinks antes de derrubar PIDs
            unlink_all_hosts()
        finally:
            try:
                net.stop()
            finally:
                sys.exit(0)

    # handlers para encerrar limpo
    signal.signal(signal.SIGTERM, cleanup_and_exit)
    signal.signal(signal.SIGINT, cleanup_and_exit)

    if args.headless:
        print("READY h1,h3,h5", flush=True)
        # bloqueia até sinal
        while True:
            time.sleep(1.0)
    else:
        CLI(net)
        cleanup_and_exit()

if __name__ == "__main__":
    main()
