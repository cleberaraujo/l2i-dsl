"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0
"""

# l2i/ac5_telemetry.py
# Geração de telemetria mínima (RTT percentílica e throughput) + avaliação de SLO

from __future__ import annotations
import subprocess, shlex, time, json, statistics, re, signal
from typing import Dict, List, Optional, Tuple

def _run(cmd: str, timeout: Optional[int]=30) -> Tuple[int,str,str]:
    p = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr

def measure_ping_rtts(dst_ip: str, count: int=40, interval: float=0.1, iface: Optional[str]=None) -> Dict[str, float]:
    """
    Executa 'ping' e extrai amostras individuais ('time=XX ms'), permitindo percentis.
    """
    cmd = f"ping -n -i {interval} -c {count} {dst_ip}"
    if iface:
        cmd = f"ping -I {iface} -n -i {interval} -c {count} {dst_ip}"
    rc,out,err = _run(cmd)
    if rc != 0:
        return {"p50": float("inf"), "p95": float("inf"), "p99": float("inf")}
    rtts = []
    for line in out.splitlines():
        m = re.search(r"time=([0-9.]+)\s*ms", line)
        if m:
            rtts.append(float(m.group(1)))
    if not rtts:
        return {"p50": float("inf"), "p95": float("inf"), "p99": float("inf")}
    rtts.sort()
    def pct(p): 
        k = int(round((p/100.0)*len(rtts)+0.5))-1
        k = max(0, min(k, len(rtts)-1))
        return rtts[k]
    return {"p50": pct(50), "p95": pct(95), "p99": pct(99)}

def _spawn_iperf3_server(bind: Optional[str]=None) -> subprocess.Popen:
    args = ["iperf3","-s","-1"]  # -1: encerra após um cliente
    if bind: args += ["-B", bind]
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

def measure_iperf3_throughput(dst_ip: str, time_s: int=5, reverse: bool=False, json_out: bool=True) -> Optional[float]:
    """
    Executa cliente iperf3 (necessita servidor ativo em dst_ip).
    Retorna throughput médio (Mbps).
    """
    args = ["iperf3","-c", dst_ip, "-t", str(time_s)]
    if json_out: args += ["-J"]
    if reverse: args += ["-R"]
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        return None
    if json_out:
        try:
            data = json.loads(p.stdout)
            # Preferimos recebimento no destino (sentido direto)
            bps = data["end"]["sum_received"]["bits_per_second"]
            return bps / 1e6
        except Exception:
            return None
    # Fallback (não JSON): tentar regex simples
    m = re.search(r"([\d.]+)\s+Mbits/sec", p.stdout)
    return float(m.group(1)) if m else None

def evaluate_slo(lat_stats: Dict[str,float], thr_mbps: Optional[float], constraints: Dict[str,dict]) -> Dict[str, any]:
    """
    Compara métricas medidas com invariantes do plano (latency/bandwidth).
    """
    report = {"violations": [], "metrics": {"latency": lat_stats, "throughput_mbps": thr_mbps}}
    # Latência
    if "latency" in constraints:
        bound = constraints["latency"]["max_ms"]
        perc  = constraints["latency"].get("percentile","P99")
        key = perc.lower()
        val = lat_stats.get(key, float("inf"))
        if val > bound:
            report["violations"].append({"type":"latency", "percentile": perc, "value_ms": val, "bound_ms": bound})
    # Largura de banda (min)
    if "bandwidth" in constraints and "min_mbps" in constraints["bandwidth"] and thr_mbps is not None:
        min_req = constraints["bandwidth"]["min_mbps"]
        if thr_mbps + 1e-6 < min_req:
            report["violations"].append({"type":"throughput", "value_mbps": thr_mbps, "min_mbps": min_req})
    return report
