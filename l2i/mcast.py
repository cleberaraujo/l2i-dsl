"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

Arquivo: l2i/mcast.py
Descrição: (AC3/AC4 multicast) expande InstallSourceTree em ReplicationPlan por dispositivo (emissão NETCONF-like por switch)
Autor: Cleber Araújo (antoniocleber@ifba.edu.br)
Criação: 01/09/2025
Versão: 0.0.0
"""

# Expansão de multicast: gera plano de replicação por switch com base em um Topology (grafo)
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple
from .topo import Topology

# Estruturas de replicação
@dataclass
class ReplicationEntry:
    switch: str
    replicate_to: Set[str] = field(default_factory=set)  # Conjunto de next-hops

@dataclass
class ReplicationPlan:
    group_id: str
    entries: Dict[str, ReplicationEntry] = field(default_factory=dict)

    def add_edge(self, sw: str, nh: str):
        """Adiciona uma replicação: switch 'sw' envia para next-hop 'nh'"""
        ent = self.entries.setdefault(sw, ReplicationEntry(switch=sw))
        ent.replicate_to.add(nh)

    def as_dict(self) -> Dict[str, List[str]]:
        """Retorna o plano em formato simples (dict para serialização)"""
        return {sw: sorted(list(ent.replicate_to)) for sw, ent in self.entries.items()}

# Construção do plano
def build_replication_plan(topo: Topology, src: str, receivers: List[str], group_id: str,
                           switches: List[str] | None = None, hosts: List[str] | None = None) -> ReplicationPlan:
    """
    Constrói plano de replicação multicast (SPT baseado em Dijkstra)
    - src: origem
    - receivers: lista de receptores
    - switches/hosts: listas opcionais para distinguir papéis
    """
    rp = ReplicationPlan(group_id=group_id)

    # 1) Caminhos mínimos via Dijkstra
    _, prev = topo.dijkstra(src)

    # 2) Função auxiliar para reconstruir caminho até dst
    def path_to(dst: str) -> List[str] | None:
        if dst not in prev and dst != src:
            return None
        path = []
        cur = dst
        while cur is not None and cur != src:
            path.append(cur)
            cur = prev[cur]
        if cur == src:
            path.append(src)
            path.reverse()
            return path
        return None

    # 3) Marca replicações em switches ao longo dos caminhos
    for r in receivers:
        p = path_to(r)
        if not p or len(p) < 2:
            continue
        for i in range(len(p)-1):
            u, v = p[i], p[i+1]
            if (switches and u in switches) or (not switches and u.startswith("s")):
                rp.add_edge(u, v)

    return rp

# Emissão em XML NETCONF-like
def emit_netconf_per_device(rp: ReplicationPlan) -> str:
    """
    Converte ReplicationPlan em um XML NETCONF-like.
    Lista dispositivos e suas réplicas configuradas.
    """
    gid = rp.group_id
    lines = []
    lines.append(f'<!-- L2I multicast replication (group {gid}) -->')
    lines.append('<rpc>')
    lines.append('  <edit-config>')
    lines.append('    <target><candidate/></target>')
    lines.append('    <config>')
    for sw, ent in sorted(rp.entries.items(), key=lambda kv: kv[0]):
        lines.append(f'      <device name="{sw}">')
        lines.append(f'        <multicast>')
        lines.append(f'          <group id="{gid}" state="enable"/>')
        for nh in sorted(ent.replicate_to):
            lines.append(f'          <replicate to="{nh}"/>')
        lines.append(f'        </multicast>')
        lines.append(f'      </device>')
    lines.append('    </config>')
    lines.append('  </edit-config>')
    lines.append('  <commit/>')
    lines.append('</rpc>')
    return "\n".join(lines)
