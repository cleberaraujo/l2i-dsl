"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0
"""

import math, heapq
from typing import Dict, List, Optional, Tuple

class Topology:
    def __init__(self):
        self.adj: Dict[str, Dict[str, float]] = {}

    def add_link(self, u: str, v: str, w: float=1.0):
        self.adj.setdefault(u, {})[v] = w
        self.adj.setdefault(v, {})[u] = w

    def nodes(self) -> List[str]:
        return list(self.adj.keys())

    def dijkstra(self, src: str) -> Tuple[Dict[str,float], Dict[str, Optional[str]]]:
        dist: Dict[str, float] = {n: math.inf for n in self.adj}
        prev: Dict[str, Optional[str]] = {n: None for n in self.adj}
        dist[src] = 0.0
        pq: List[Tuple[float,str]] = [(0.0, src)]
        while pq:
            d,u = heapq.heappop(pq)
            if d != dist[u]: continue
            for v,w in self.adj[u].items():
                nd = d + w
                if nd < dist[v]:
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        return dist, prev

    def spt_from_source(self, src: str, receivers: List[str]) -> Dict[str, List[str]]:
        _, prev = self.dijkstra(src)
        tree: Dict[str, List[str]] = {}
        for r in receivers:
            if r not in prev and r != src:
                continue
            path = []
            cur = r
            while cur is not None and cur != src:
                path.append(cur)
                cur = prev[cur]
            if cur == src:
                path.append(src)
                path.reverse()
                tree[r] = path
        return tree
