# -*- coding: utf-8 -*-
"""
S2: Multicast "source-oriented" multi-domínios, com métricas por fase e recovery.

Objetivo deste cenário (científico e metodológico)
--------------------------------------------------
O cenário S2 foi desenhado para permitir (i) comparar baseline vs adapt e (ii) mock vs real
*sem ambiguidade metodológica*, incluindo a Figura de recovery: tempo para retornar à
conformidade após um evento (ex.: join).

Princípios importantes:
- --spec descreve a INTENÇÃO (o que camadas superiores pedem).
- --bwA/--bwB/--bwC/--delay-ms descrevem o AMBIENTE experimental (o que o testbed oferece),
  tipicamente configurado pelo administrador/operador. Eles NÃO sobrescrevem a intenção.
- A adaptação (mode=adapt) ocorre *durante* o experimento, no instante do evento, para que
  seja possível observar custo/impacto e medir recovery.

Artefatos gerados (sem remover nada do que já existe):
- preflight (ping) por destino
- iperf3 (JSON do cliente) por destino
- RTT CSV contínuo com timestamp relativo (t_ms,seq,rtt_ms) por destino
- dom_{A,B,C}.json (com evidências, comandos aplicados, erros)
- tc_dump_A (read-back do estado do tc)
- netconf_dump_B (read-back do running config)
- p4_dump_C (read-back de tabela/estado)
- summary S2_{TS}.json (inclui phase_plan, phases_metrics e recovery)

Domínios:
- A: Linux tc/htb (local, netns do host)
- B: NETCONF (ncclient)
- C: P4Runtime (gRPC)

Obs. sobre conformidade:
- Conformidade é avaliada por fase a partir de duas dimensões:
  (i) RTT (percentil P99) <= limite e (ii) delivery_ratio estimado >= limite.
- O limite de RTT por padrão vem do spec (requirements.latency.max_ms, ou latency.max_ms no spec S2).
- O limite de delivery_ratio padrão é 0.99 (configurável).

Compatibilidade:
- Este arquivo foi feito para ser substituído integralmente no repositório.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import math
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import inspect

# ---------------- util ----------------

def utc_ts() -> str:
    """Timestamp UTC timezone-aware (evita DeprecationWarning)."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


ROOT = Path(__file__).resolve().parents[1]
RES_DIR = ROOT / "results" / "S2"
RES_DIR.mkdir(parents=True, exist_ok=True)


def run(cmd: List[str],
        check: bool = True,
        capture: bool = False,
        ns: str | None = None) -> str | None:
    """
    Executa um comando (opcionalmente dentro de um namespace).
    - capture=True retorna stdout (texto).
    - check=True levanta exceção se rc != 0.
    """
    if ns:
        cmd = ["ip", "netns", "exec", ns, *cmd]
    p = subprocess.run(cmd, text=True, capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError(
            f"cmd failed: {' '.join(cmd)}\nRC={p.returncode}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}"
        )
    return p.stdout if capture else None


def safe_dump_json(path: Path, obj: Any) -> None:
    """Escreve JSON robustamente (nunca deixa arquivo vazio)."""
    try:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    except Exception as e:
        path.write_text(json.dumps({"error": f"dump_failed: {e}"}, ensure_ascii=False, indent=2))


def load_json_or_fail(path: Path) -> Dict[str, Any]:
    """
    Consistência obrigatória:
    - se o spec não existir -> falha
    - se não for JSON válido -> falha
    """
    if not path.exists():
        raise FileNotFoundError(f"--spec file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"Invalid JSON in spec: {path} ({e})") from e


def _mask_secret(d: Dict[str, Any], keys: Tuple[str, ...] = ("password", "pass", "secret")) -> Dict[str, Any]:
    out = dict(d)
    for k in keys:
        if k in out and out[k] is not None:
            out[k] = "***"
    return out


def clamp_int(x: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(x)))


# ---------------- Fases e timeline ----------------

class PhaseTracker:
    """
    Relógio relativo (ms) baseado em time.monotonic_ns().

    Observação importante:
    - Este tracker é usado para "timeline operacional" (fase do experimento e fase_plan).
    - O RTT contínuo grava timestamps t_ms usando este mesmo tracker, garantindo vínculo
      operacional entre phase_plan e métricas coletadas.
    """
    def __init__(self) -> None:
        self._t0_ns: Optional[int] = None

    def start(self) -> None:
        self._t0_ns = time.monotonic_ns()

    def now_ms(self) -> int:
        if self._t0_ns is None:
            raise RuntimeError("PhaseTracker not started")
        return int((time.monotonic_ns() - self._t0_ns) / 1e6)

    def sleep_until_ms(self, t_ms: int) -> None:
        """Dormir até t_ms relativo ao início do tracker."""
        while True:
            now = self.now_ms()
            if now >= t_ms:
                return
            # dormir em fatias pequenas para reduzir drift e permitir interrupções
            time.sleep(min(0.05, max(0.0, (t_ms - now) / 1000.0)))


def build_phase_plan(duration_s: int,
                     splits_s: List[float],
                     event_name: str) -> List[Dict[str, Any]]:
    """
    Constrói um phase_plan simples:
      - pre_event: [0, splits[0])
      - event:     [splits[0], splits[1]) (nome dado por --event-name)
      - post_event:[splits[1], duration]

    splits_s deve conter 2 valores (ex.: 10 15).
    """
    if len(splits_s) != 2:
        raise ValueError("--phase-splits precisa ter exatamente 2 valores (ex.: 10 15)")

    s0 = max(0.0, float(splits_s[0]))
    s1 = max(s0, float(splits_s[1]))
    dur = max(s1, float(duration_s))

    return [
        {"name": "pre_event", "start_ms": 0, "end_ms": int(s0 * 1000)},
        {"name": event_name,  "start_ms": int(s0 * 1000), "end_ms": int(s1 * 1000)},
        {"name": "post_event","start_ms": int(s1 * 1000), "end_ms": int(dur * 1000)},
    ]


# ---------------- Métricas & coleta (RTT contínuo) ----------------

def _parse_ping_line(line: str) -> Tuple[Optional[int], Optional[float]]:
    """
    Extrai (icmp_seq, rtt_ms) de uma linha de saída do ping.

    Exemplos esperados:
      "64 bytes from 10.0.0.3: icmp_seq=12 ttl=64 time=0.123 ms"
    """
    if "icmp_seq=" not in line or "time=" not in line:
        return None, None

    seq = None
    rtt = None
    try:
        # icmp_seq=12
        m = line.split("icmp_seq=")[1]
        seq_str = ""
        for ch in m:
            if ch.isdigit():
                seq_str += ch
            else:
                break
        seq = int(seq_str) if seq_str else None
    except Exception:
        seq = None

    try:
        # time=0.123 ms
        m = line.split("time=")[1]
        num = ""
        for ch in m:
            if ch.isdigit() or ch == ".":
                num += ch
            else:
                break
        rtt = float(num) if num else None
    except Exception:
        rtt = None

    return seq, rtt


class RTTContinuousSampler:
    """
    Coletor contínuo de RTT via ping, gravando CSV com timestamp relativo.

    CSV: t_ms,seq,rtt_ms

    Por que isso existe?
    - Permite calcular RTT por fase (pre/event/post) sem "achismo".
    - Permite derivar tempo de recovery (time_to_conformance_ms) com base em bins após o evento.
    """
    def __init__(self,
                 tracker: PhaseTracker,
                 ns_src: str,
                 dst_ip: str,
                 interval_s: float,
                 duration_s: int,
                 out_csv: Path) -> None:
        self.tracker = tracker
        self.ns_src = ns_src
        self.dst_ip = dst_ip
        self.interval_s = interval_s
        self.duration_s = duration_s
        self.out_csv = out_csv

        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen[str]] = None

        # Armazena amostras em memória para estatísticas por fase/recovery (poucos KB/MB).
        self.samples: List[Dict[str, Any]] = []
        self.meta: Dict[str, Any] = {
            "interval_ms": int(interval_s * 1000),
            "duration_s": duration_s,
            "dst_ip": dst_ip,
            "ns_src": ns_src,
        }

    def start(self) -> None:
        # ping com saída em linha (stdbuf) e intervalo configurável
        # -n: não resolve nomes
        # -i: intervalo
        # -w: timeout total (segundos)
        # -O: reporta "no answer yet" em alguns casos; não é universal. Mantemos simples.
        cmd = ["bash", "-lc",
               f"stdbuf -oL ping -n -i {self.interval_s:.3f} -w {int(self.duration_s)} {self.dst_ip}"]
        if self.ns_src:
            cmd = ["ip", "netns", "exec", self.ns_src, *cmd]

        self._proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass

        if self._thread:
            self._thread.join(timeout=2.0)

        # dump CSV
        self._write_csv()

    def _reader_loop(self) -> None:
        assert self._proc and self._proc.stdout

        for line in self._proc.stdout:
            if self._stop_evt.is_set():
                break

            seq, rtt = _parse_ping_line(line.strip())
            if seq is None or rtt is None:
                continue

            t_ms = self.tracker.now_ms()
            self.samples.append({"t_ms": t_ms, "seq": seq, "rtt_ms": float(rtt)})

        # coletar stderr para diagnóstico, se houver
        try:
            if self._proc and self._proc.stderr:
                serr = self._proc.stderr.read().strip()
                if serr:
                    self.meta["stderr_tail"] = serr[-800:]
        except Exception:
            pass

    def _write_csv(self) -> None:
        try:
            with self.out_csv.open("w", encoding="utf-8", newline="") as f:
                wr = csv.writer(f)
                wr.writerow(["t_ms", "seq", "rtt_ms"])
                for s in self.samples:
                    wr.writerow([s["t_ms"], s["seq"], f'{s["rtt_ms"]:.3f}'])
        except Exception as e:
            # não quebrar experimento por falha no dump
            self.out_csv.write_text(f"[rtt_dump_error] {e}\n")


def _percentile(vals: List[float], p: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = (len(vals) - 1) * p
    i = int(k)
    d = k - i
    if i + 1 < len(vals):
        return vals[i] * (1 - d) + vals[i + 1] * d
    return vals[i]


def rtt_stats_from_samples(samples: List[Dict[str, Any]],
                           start_ms: Optional[int] = None,
                           end_ms: Optional[int] = None) -> Dict[str, float]:
    """
    Estatísticas de RTT a partir de amostras (timestamped).
    """
    vals: List[float] = []
    for s in samples:
        t = int(s.get("t_ms", 0))
        if start_ms is not None and t < start_ms:
            continue
        if end_ms is not None and t >= end_ms:
            continue
        try:
            vals.append(float(s["rtt_ms"]))
        except Exception:
            pass

    if not vals:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "n": 0}

    return {
        "p50": round(_percentile(vals, 0.50), 1),
        "p95": round(_percentile(vals, 0.95), 1),
        "p99": round(_percentile(vals, 0.99), 1),
        "n": int(len(vals)),
    }


def delivery_ratio_estimate(samples: List[Dict[str, Any]],
                            start_ms: int,
                            end_ms: int,
                            interval_ms: int) -> float:
    """
    Estima delivery_ratio (0..1) em uma janela [start_ms, end_ms) pela razão:
      received / expected

    onde expected é derivado do intervalo configurado do ping (interval_ms).

    Observação:
    - É uma estimativa conservadora e suficiente para consistência metodológica,
      pois usa o mesmo procedimento para todas as execuções e fases.
    """
    if end_ms <= start_ms:
        return 0.0

    expected = max(1, int(math.floor((end_ms - start_ms) / max(1, interval_ms))))
    received = 0
    for s in samples:
        t = int(s.get("t_ms", -1))
        if start_ms <= t < end_ms:
            received += 1
    return round(min(1.0, received / expected), 3)


# ---------------- iperf3 ----------------

def run_iperf3_unicast(duration: int,
                       be_mbps: float,
                       out_json: Path,
                       ns_cli: str,
                       ns_srv: str,
                       srv_ip: str,
                       port: int = 5201) -> None:
    """
    Executa iperf3 TCP (cliente no ns_cli -> servidor no ns_srv) e grava JSON do CLIENTE.

    Robustez:
    - inicia server com bind no IP do namespace e aguarda socket LISTEN para evitar "Connection refused".
    """
    srv_cmd = ["ip", "netns", "exec", ns_srv, "iperf3", "-s", "-1", "-B", srv_ip, "-p", str(port)]
    srv = subprocess.Popen(srv_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    ready = False
    for _ in range(30):
        time.sleep(0.05)
        try:
            out = run(["ss", "-lntp"], ns=ns_srv, check=False, capture=True) or ""
            if f":{port}" in out and "iperf3" in out:
                ready = True
                break
        except Exception:
            pass

    if not ready:
        try:
            serr = srv.stderr.read() if srv.stderr else ""
        except Exception:
            serr = ""
        safe_dump_json(out_json, {
            "error": "iperf3 server not ready",
            "server_cmd": " ".join(srv_cmd),
            "server_stderr": serr,
        })
        try:
            srv.kill()
        except Exception:
            pass
        return

    cli = subprocess.run(
        ["ip", "netns", "exec", ns_cli, "iperf3",
         "-c", srv_ip, "-p", str(port),
         "-t", str(duration), "-b", f"{be_mbps}M", "--json"],
        text=True,
        capture_output=True,
    )

    if cli.returncode == 0 and cli.stdout.strip().startswith("{"):
        out_json.write_text(cli.stdout)
    else:
        safe_dump_json(out_json, {"error": "iperf3 client failed", "stdout": cli.stdout, "stderr": cli.stderr})

    try:
        srv.wait(timeout=2)
    except Exception:
        try:
            srv.kill()
        except Exception:
            pass


def parse_iperf3_mbps(path: Path) -> float | None:
    """
    Extrai Mbps do JSON do iperf3 (client).
    Se falhar, retorna None (o que vira null no sumário).
    """
    try:
        j = json.loads(path.read_text())
    except Exception:
        return None

    if isinstance(j, dict) and "error" in j and "end" not in j:
        return None
    if isinstance(j, dict) and j.get("error"):
        return None

    try:
        bps = j["end"]["sum_received"]["bits_per_second"]
        return round(float(bps) / 1e6, 3) if bps else None
    except Exception:
        return None


# ---------------- TC (Domínio A) ----------------

def tc_env_setup(ns: str, dev: str, bw_mbps: float) -> Dict[str, Any]:
    """
    Configuração do AMBIENTE (operador/admin) no Domínio A:
    - limita link via HTB em um classid "1:1" com rate=ceil=bw_mbps.
    - NÃO instala filtro/priority class (isso é papel da adaptação).
    """
    cmds: List[List[str]] = []

    def _r(c: List[str], check: bool = True):
        cmds.append(c)
        run(c, check=check, ns=ns)

    bw = max(1, int(bw_mbps))

    _r(["tc", "qdisc", "del", "dev", dev, "root"], check=False)
    _r(["tc", "qdisc", "add", "dev", dev, "root", "handle", "1:", "htb"])
    _r(["tc", "class", "add", "dev", dev, "parent", "1:", "classid", "1:1", "htb",
        "rate", f"{bw}mbit", "ceil", f"{bw}mbit"])

    qdisc = run(["tc", "qdisc", "show", "dev", dev], check=False, capture=True, ns=ns) or ""
    classes = run(["tc", "class", "show", "dev", dev], check=False, capture=True, ns=ns) or ""
    filters = run(["tc", "filter", "show", "dev", dev, "parent", "1:"], check=False, capture=True, ns=ns) or ""

    return {
        "commands": [" ".join(c) for c in cmds],
        "readback": {"qdisc": qdisc.strip(), "class": classes.strip(), "filter": filters.strip()},
    }


def tc_apply_adaptation(ns: str, dev: str, intent: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adaptação no Domínio A:
    - cria uma classe adicional (ex.: 1:10) e instala filtro (ICMP) apontando para ela.
    - usa intent[min_mbps/max_mbps] para rate/ceil (com arredondamento seguro).
    - registra comandos e read-back.

    Nota:
    - este "evento" tende a causar alguma perturbação (replace/insert de classes/filtros),
      permitindo observar deltas baseline vs adapt em RTT/throughput, sem artificialidade.
    """
    cmds: List[List[str]] = []
    prio_cls = "10"  # classid 1:10

    def _r(c: List[str], check: bool = True):
        cmds.append(c)
        run(c, check=check, ns=ns)

    min_mbps = max(1, int(float(intent.get("min_mbps", 1))))
    max_mbps = max(min_mbps, int(float(intent.get("max_mbps", min_mbps))))

    # remove filtro anterior (se existir) para evitar duplicação e ruído
    _r(["tc", "filter", "del", "dev", dev, "parent", "1:", "protocol", "ip", "prio", "2"], check=False)

    # garante classe de adaptação (1:10)
    _r(["tc", "class", "del", "dev", dev, "classid", f"1:{prio_cls}"], check=False)
    _r(["tc", "class", "add", "dev", dev, "parent", "1:1", "classid", f"1:{prio_cls}", "htb",
        "rate", f"{min_mbps}mbit", "ceil", f"{max_mbps}mbit"])

    # exemplo: trata ICMP como fluxo sensível (apenas para termos efeito mensurável)
    _r(["tc", "filter", "add", "dev", dev, "protocol", "ip", "parent", "1:", "prio", "2",
        "u32", "match", "ip", "protocol", "1", "0xff", "flowid", f"1:{prio_cls}"])

    qdisc = run(["tc", "qdisc", "show", "dev", dev], check=False, capture=True, ns=ns) or ""
    classes = run(["tc", "class", "show", "dev", dev], check=False, capture=True, ns=ns) or ""
    filters = run(["tc", "filter", "show", "dev", dev, "parent", "1:"], check=False, capture=True, ns=ns) or ""

    return {
        "commands": [" ".join(c) for c in cmds],
        "readback": {"qdisc": qdisc.strip(), "class": classes.strip(), "filter": filters.strip()},
    }


# ---------------- backends dinâmicos (A/B/C) ----------------

from l2i.backends.router import get_backends  # noqa: E402


def _call_apply_qos(mod: Any,
                    domain_ctx: Dict[str, Any],
                    intent: Dict[str, Any],
                    target: Optional[Dict[str, Any]]) -> Any:
    """Compatibilidade de assinaturas para apply_qos()."""
    fn = getattr(mod, "apply_qos")
    sig = inspect.signature(fn)
    nparams = len(sig.parameters)

    if nparams >= 3:
        try:
            return fn(domain_ctx, intent, target)
        except TypeError:
            return fn(domain_ctx.get("name", "X"), intent, target)

    return fn(domain_ctx, intent)


def _normalize_backend_result(raw: Any) -> Tuple[bool, Any]:
    """
    Normaliza o retorno do backend para:
      (applied_bool, info_any)
    """
    if isinstance(raw, (list, tuple)) and len(raw) == 2 and isinstance(raw[0], bool):
        return raw[0], raw[1]

    if isinstance(raw, dict) and isinstance(raw.get("applied"), bool):
        return raw["applied"], raw

    # compat: assume aplicado se backend retornou algo sem sinalizador explícito
    return True, raw


def _apply_backend_chain(mod_or_list: Union[Any, List[Any]],
                         domain_ctx: Dict[str, Any],
                         intent: Dict[str, Any],
                         target: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Aplica uma cadeia de backends e interpreta applied corretamente.
    """
    mods = mod_or_list if isinstance(mod_or_list, (list, tuple)) else [mod_or_list]
    responses = []
    all_applied = True

    for m in mods:
        bname = getattr(m, "__name__", str(m))
        try:
            raw = _call_apply_qos(m, domain_ctx=domain_ctx, intent=intent, target=target)
            applied, info = _normalize_backend_result(raw)
            if not applied:
                all_applied = False
            responses.append({"backend": bname, "applied": applied, "response": info})
        except Exception as e:
            all_applied = False
            responses.append({"backend": bname, "applied": False, "error": str(e)})

    return {"applied": all_applied, "responses": responses}


def _load_real_targets_yaml() -> Dict[str, Any]:
    """
    targets reais em: dsl/l2i/backends/backends_real.yaml
    separa ambiente (credenciais, endereços) da intenção (spec).
    """
    import yaml  # PyYAML no venv
    ypath = ROOT / "l2i" / "backends" / "backends_real.yaml"
    if not ypath.exists():
        raise FileNotFoundError(f"Missing real-backends config: {ypath}")
    return yaml.safe_load(ypath.read_text(encoding="utf-8")) or {}


# ---------------- Recovery ----------------

def compute_phases_metrics(phase_plan: List[Dict[str, Any]],
                           samples_B: List[Dict[str, Any]],
                           samples_C: List[Dict[str, Any]],
                           interval_ms: int,
                           rtt_limit_ms: float,
                           delivery_limit: float) -> List[Dict[str, Any]]:
    """
    Calcula métricas por fase:
      - rtt_p99_ms_B/C
      - delivery_ratio_B/C (estimado)
      - intent_ok (conformidade) por fase
    """
    out = []
    for ph in phase_plan:
        name = str(ph["name"])
        s = int(ph["start_ms"])
        e = int(ph["end_ms"])

        stB = rtt_stats_from_samples(samples_B, start_ms=s, end_ms=e)
        stC = rtt_stats_from_samples(samples_C, start_ms=s, end_ms=e)

        delB = delivery_ratio_estimate(samples_B, start_ms=s, end_ms=e, interval_ms=interval_ms)
        delC = delivery_ratio_estimate(samples_C, start_ms=s, end_ms=e, interval_ms=interval_ms)

        ok = (stB["p99"] <= rtt_limit_ms) and (stC["p99"] <= rtt_limit_ms) and (delB >= delivery_limit) and (delC >= delivery_limit)

        out.append({
            "name": name,
            "start_ms": s,
            "end_ms": e,
            "rtt_p99_ms_B": stB["p99"],
            "rtt_p99_ms_C": stC["p99"],
            "delivery_ratio_B": delB,
            "delivery_ratio_C": delC,
            "intent_ok": bool(ok),
        })
    return out


def compute_recovery(event_name: str,
                     phase_plan: List[Dict[str, Any]],
                     samples_B: List[Dict[str, Any]],
                     samples_C: List[Dict[str, Any]],
                     interval_ms: int,
                     rtt_limit_ms: float,
                     delivery_limit: float,
                     bin_ms: int) -> Dict[str, Any]:
    """
    Deriva time_to_conformance_ms (recovery) a partir de bins após o evento.

    Método:
    - identifica [event_start_ms, event_end_ms] pelo phase_plan.
    - a partir de event_end_ms, varre bins de tamanho bin_ms:
        calcula RTT p99 em cada bin (por domínio) e delivery_ratio estimado no bin.
      o primeiro bin que satisfaz conformidade -> recovery.

    Retorna:
      {
        event_name, event_start_ms, event_end_ms,
        bin_ms,
        time_to_conformance_ms,
        series: [{t_ms, rtt_p99_B, rtt_p99_C, del_B, del_C, intent_ok}]
      }
    """
    ev = next((p for p in phase_plan if str(p["name"]) == event_name), None)
    if not ev:
        return {
            "event_name": event_name,
            "error": "event phase not found in phase_plan",
            "time_to_conformance_ms": None,
        }

    ev_s = int(ev["start_ms"])
    ev_e = int(ev["end_ms"])
    total_ms = int(phase_plan[-1]["end_ms"])

    series = []
    ttc = None

    # varre bins desde ev_e até total_ms
    for t in range(ev_e, total_ms, max(1, int(bin_ms))):
        b_s = t
        b_e = min(total_ms, t + int(bin_ms))

        stB = rtt_stats_from_samples(samples_B, start_ms=b_s, end_ms=b_e)
        stC = rtt_stats_from_samples(samples_C, start_ms=b_s, end_ms=b_e)

        delB = delivery_ratio_estimate(samples_B, start_ms=b_s, end_ms=b_e, interval_ms=interval_ms)
        delC = delivery_ratio_estimate(samples_C, start_ms=b_s, end_ms=b_e, interval_ms=interval_ms)

        ok = (stB["p99"] <= rtt_limit_ms) and (stC["p99"] <= rtt_limit_ms) and (delB >= delivery_limit) and (delC >= delivery_limit)

        series.append({
            "start_ms": b_s,
            "end_ms": b_e,
            "rtt_p99_ms_B": stB["p99"],
            "rtt_p99_ms_C": stC["p99"],
            "delivery_ratio_B": delB,
            "delivery_ratio_C": delC,
            "intent_ok": bool(ok),
        })

        if ttc is None and ok:
            ttc = int(b_s - ev_e)

    return {
        "event_name": event_name,
        "event_start_ms": ev_s,
        "event_end_ms": ev_e,
        "bin_ms": int(bin_ms),
        "time_to_conformance_ms": ttc,
        "series": series,
    }


# ---------------- Cenário S2 ----------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--be-mbps", type=float, default=30.0)
    ap.add_argument("--mode", choices=["baseline", "adapt"], default="baseline")
    ap.add_argument("--backend", choices=["mock", "real"], default="mock")

    # AMBIENTE (defaults como antes)
    ap.add_argument("--bwA", type=float, default=100.0, help="(env) bandwidth domain A (mbps)")
    ap.add_argument("--bwB", type=float, default=100.0, help="(env) bandwidth domain B (mbps)")
    ap.add_argument("--bwC", type=float, default=100.0, help="(env) bandwidth domain C (mbps)")
    ap.add_argument("--delay-ms", type=float, default=1.0, help="(env) delay placeholder (ms)")

    # Fases / evento
    ap.add_argument("--phase-splits", nargs=2, type=float, default=[10.0, 15.0],
                    metavar=("PRE_END_S", "EVENT_END_S"),
                    help="splits em segundos: pre_event termina em PRE_END_S, evento termina em EVENT_END_S")
    ap.add_argument("--event-name", type=str, default="join", help="nome da fase de evento (ex.: join)")

    # RTT contínuo / recovery
    ap.add_argument("--rtt-interval-ms", type=int, default=50,
                    help="intervalo do ping (ms) para RTT contínuo (default 50ms)")
    ap.add_argument("--recovery-bin-ms", type=int, default=500,
                    help="tamanho do bin (ms) para calcular time_to_conformance_ms (default 500ms)")
    ap.add_argument("--conformance-rtt-ms", type=float, default=-1.0,
                    help="limite de RTT P99 (ms). Se <0, usa max_ms do spec.")
    ap.add_argument("--conformance-delivery", type=float, default=0.99,
                    help="limite de delivery_ratio (0..1) por fase/bin (default 0.99)")

    args = ap.parse_args()

    # --- carrega spec (consistência obrigatória) ---
    spec_path = Path(args.spec)
    spec = load_json_or_fail(spec_path)

    # --- intenção vinda do spec ---
    # Suporta dois formatos:
    # 1) S2 antigo: {"bandwidth": {...}, "latency": {...}, "priority": ...}
    # 2) L2i (S1): {"requirements": {"bandwidth": {...}, "latency": {...}, "priority": {"level":...}}}
    bw_obj = (spec.get("bandwidth") or (spec.get("requirements", {}) or {}).get("bandwidth") or {})
    lat_obj = (spec.get("latency") or (spec.get("requirements", {}) or {}).get("latency") or {})
    prio_obj = (spec.get("priority") or (spec.get("requirements", {}) or {}).get("priority") or "medium")

    intent_min_mbps = float(bw_obj.get("min_mbps", 0))
    intent_max_mbps = float(bw_obj.get("max_mbps", 0))

    # prioridade pode vir como string ("high") ou dict {"level":"high"}
    if isinstance(prio_obj, dict):
        prio_level = str(prio_obj.get("level", "medium"))
    else:
        prio_level = str(prio_obj)

    prio = prio_level.lower().strip()
    prio_class = {"low": "prio30", "medium": "prio20", "high": "prio10"}.get(prio, "prio20")

    intent = {"class": prio_class, "min_mbps": intent_min_mbps, "max_mbps": intent_max_mbps}

    # limite RTT padrão vem do spec, se possível
    spec_rtt_limit = float(lat_obj.get("max_ms", 0)) if isinstance(lat_obj, dict) else 0.0
    rtt_limit = args.conformance_rtt_ms if args.conformance_rtt_ms >= 0 else spec_rtt_limit

    # --- endpoints (S2 style) ---
    # Mapeamento do testbed (ajuste se necessário)
    host_ip = {"h1": "10.0.0.1", "h2": "10.0.0.2", "h3": "10.0.0.3", "h4": "10.0.0.4", "h5": "10.0.0.4"}

    src_host = (spec.get("endpoints", {}) or {}).get("source", {}).get("host", "h1")
    recv_list = (spec.get("endpoints", {}) or {}).get("receivers", [])

    recv_B = next((r for r in recv_list if r.get("domain") == "B"), None)
    recv_C = next((r for r in recv_list if r.get("domain") == "C"), None)

    ns_src = str(src_host or "h1")
    ns_B = str(recv_B.get("host", "h3") if recv_B else "h3")
    ns_C = str(recv_C.get("host", "h4") if recv_C else "h4")  # por padrão, h4 (compat com ip netns list)

    ip_B = host_ip.get(ns_B, "10.0.0.3")
    ip_C = host_ip.get(ns_C, "10.0.0.4")

    # --- backends e targets ---
    backends = get_backends(args.backend)
    B_mods = backends["B"]
    C_mods = backends["C"]

    real_cfg = {}
    if args.backend == "real":
        real_cfg = _load_real_targets_yaml()

    target_B = (real_cfg.get("B", {}) or {}).get("target") if args.backend == "real" else None
    target_C = (real_cfg.get("C", {}) or {}).get("target") if args.backend == "real" else None

    # garante dst_ip no target_C (P4)
    if isinstance(target_C, dict) and "dst_ip" not in target_C:
        target_C = dict(target_C)
        target_C["dst_ip"] = ip_C

    # --- artefatos ---
    ts = utc_ts()

    pre_B = RES_DIR / f"S2_{ts}_preflight_B.txt"
    pre_C = RES_DIR / f"S2_{ts}_preflight_C.txt"

    ipf_B = RES_DIR / f"S2_{ts}_iperf_B.json"
    ipf_C = RES_DIR / f"S2_{ts}_iperf_C.json"

    rtt_B = RES_DIR / f"S2_{ts}_rtt_B.csv"
    rtt_C = RES_DIR / f"S2_{ts}_rtt_C.csv"

    domA = RES_DIR / f"S2_{ts}_dom_A.json"
    domB = RES_DIR / f"S2_{ts}_dom_B.json"
    domC = RES_DIR / f"S2_{ts}_dom_C.json"

    tc_dump_A = RES_DIR / f"S2_{ts}_tc_dump_A.txt"
    netconf_dump_B = RES_DIR / f"S2_{ts}_netconf_dump_B.txt"
    p4_dump_C = RES_DIR / f"S2_{ts}_p4_dump_C.txt"

    summary = RES_DIR / f"S2_{ts}.json"

    # --- phase plan ---
    phase_plan = build_phase_plan(duration_s=args.duration,
                                  splits_s=list(args.phase_splits),
                                  event_name=str(args.event_name))

    # --- sumário base ---
    s2: Dict[str, Any] = {
        "scenario": "S2_multicast_source_oriented",
        "flow_id": spec.get("flow_id") or (spec.get("flow", {}) or {}).get("id") or "S2",
        "timestamp_utc": ts,
        "mode": args.mode,
        "backend_mode": args.backend,
        "spec_path": str(spec_path),
        "spec_exists": True,

        # Ambiente experimental (admin/operator)
        "environment": {
            "bwA_mbps": args.bwA,
            "bwB_mbps": args.bwB,
            "bwC_mbps": args.bwC,
            "delay_ms": args.delay_ms,
            "be_mbps": args.be_mbps,
        },

        # Intenção (camadas superiores)
        "intent": intent,

        "endpoints": {"source": ns_src, "B": {"ns": ns_B, "ip": ip_B}, "C": {"ns": ns_C, "ip": ip_C}},
        "targets": {
            "B": _mask_secret(target_B or {}) if target_B else None,
            "C": _mask_secret(target_C or {}) if target_C else None,
        },

        "conformance_rule": {
            "rtt_p99_ms_max": rtt_limit,
            "delivery_ratio_min": float(args.conformance_delivery),
            "rtt_interval_ms": int(args.rtt_interval_ms),
            "notes": "delivery_ratio é estimado como received/expected no intervalo configurado do ping",
        },

        "phase_plan": phase_plan,
        "backend_apply": {"A": False, "B": False, "C": False},

        "artifacts": {
            "domA_json": str(domA),
            "domB_json": str(domB),
            "domC_json": str(domC),
            "preflight_B": str(pre_B),
            "preflight_C": str(pre_C),
            "iperf_B_json": str(ipf_B),
            "iperf_C_json": str(ipf_C),
            "rtt_B_csv": str(rtt_B),
            "rtt_C_csv": str(rtt_C),
            "tc_dump_A_txt": str(tc_dump_A),
            "netconf_dump_B_txt": str(netconf_dump_B),
            "p4_dump_C_txt": str(p4_dump_C),
            "summary_json": str(summary),
        },
    }

    # --- preflight (antes de qualquer tráfego longo) ---
    try:
        out = run(["ping", "-n", "-c", "3", ip_B], ns=ns_src, check=False, capture=True) or ""
        pre_B.write_text(out)
    except Exception as e:
        pre_B.write_text(f"[preflight_error] {e}")

    try:
        out = run(["ping", "-n", "-c", "3", ip_C], ns=ns_src, check=False, capture=True) or ""
        pre_C.write_text(out)
    except Exception as e:
        pre_C.write_text(f"[preflight_error] {e}")

    # --- Domínio A: SEMPRE aplica o ambiente (baseline e adapt), para consistência ---
    domA_info: Dict[str, Any] = {
        "backend": "linux_tc_local",
        "applied_env": False,
        "applied_adaptation": False,
        "env_params": {"bwA_mbps": args.bwA},
        "intent_seen": intent,
        "commands_env": [],
        "commands_adapt": [],
        "readback_env": {},
        "readback_adapt": {},
    }

    try:
        env_ev = tc_env_setup(ns=ns_src, dev=f"{ns_src}-eth0", bw_mbps=args.bwA)
        domA_info["applied_env"] = True
        domA_info["commands_env"] = env_ev["commands"]
        domA_info["readback_env"] = env_ev["readback"]
    except Exception as e:
        domA_info["applied_env"] = False
        domA_info["env_error"] = str(e)

    # --- timeline do experimento (t0) e coleta contínua ---
    tracker = PhaseTracker()
    tracker.start()

    # RTT contínuo começa no t=0 do tracker
    interval_s = max(1, int(args.rtt_interval_ms)) / 1000.0
    sampler_B = RTTContinuousSampler(tracker, ns_src=ns_src, dst_ip=ip_B,
                                     interval_s=interval_s, duration_s=args.duration, out_csv=rtt_B)
    sampler_C = RTTContinuousSampler(tracker, ns_src=ns_src, dst_ip=ip_C,
                                     interval_s=interval_s, duration_s=args.duration, out_csv=rtt_C)
    sampler_B.start()
    sampler_C.start()

    # iperf3 roda em paralelo (TCP) por duration
    # Nota: em baseline/adapt e mock/real, iperf é independente dos backends; os efeitos entram via A/B/C.
    iperf_thr = {"B": None, "C": None}
    iperf_err = {"B": None, "C": None}

    def _iperf_job(tag: str, out_path: Path, ns_srv: str, srv_ip: str):
        try:
            run_iperf3_unicast(args.duration, args.be_mbps, out_path, ns_cli=ns_src, ns_srv=ns_srv, srv_ip=srv_ip)
        except Exception as e:
            iperf_err[tag] = str(e)

    thB = threading.Thread(target=_iperf_job, args=("B", ipf_B, ns_B, ip_B), daemon=True)
    thC = threading.Thread(target=_iperf_job, args=("C", ipf_C, ns_C, ip_C), daemon=True)
    thB.start()
    thC.start()

    # --- evento/adaptação no meio do experimento ---
    event_phase = phase_plan[1]
    ev_start_ms = int(event_phase["start_ms"])
    ev_end_ms = int(event_phase["end_ms"])

    # espera até o começo do evento
    tracker.sleep_until_ms(ev_start_ms)

    # aplica adaptação somente se mode=adapt
    domB_info: Dict[str, Any] = {
        "backend_chain": [getattr(m, "__name__", str(m)) for m in (B_mods if isinstance(B_mods, (list, tuple)) else [B_mods])],
        "applied": False,
        "env_params": {"bwB_mbps": args.bwB},
        "target": _mask_secret(target_B or {}) if target_B else None,
        "responses": [],
    }
    domC_info: Dict[str, Any] = {
        "backend_chain": [getattr(m, "__name__", str(m)) for m in (C_mods if isinstance(C_mods, (list, tuple)) else [C_mods])],
        "applied": False,
        "env_params": {"bwC_mbps": args.bwC},
        "target": _mask_secret(target_C or {}) if target_C else None,
        "readback_dump": None,
        "responses": [],
    }

    if args.mode == "adapt":
        # A: aplica adaptação (intenção)
        try:
            adapt_ev = tc_apply_adaptation(ns=ns_src, dev=f"{ns_src}-eth0", intent=intent)
            domA_info["applied_adaptation"] = True
            domA_info["commands_adapt"] = adapt_ev["commands"]
            domA_info["readback_adapt"] = adapt_ev["readback"]
        except Exception as e:
            domA_info["applied_adaptation"] = False
            domA_info["adapt_error"] = str(e)

        # B: backend(s)
        try:
            aggB = _apply_backend_chain(B_mods, domain_ctx={"name": "B"}, intent=intent, target=target_B)
            domB_info.update(aggB)
        except Exception as e:
            domB_info["applied"] = False
            domB_info["responses"] = [{"backend": "chain", "applied": False, "error": str(e)}]

        # Dump NETCONF (padronização)
        dumped = False
        for r in domB_info.get("responses", []):
            info = r.get("response")
            if isinstance(info, dict) and info.get("running_snapshot"):
                netconf_dump_B.write_text(str(info.get("running_snapshot")))
                dumped = True
                break
        if not dumped:
            netconf_dump_B.write_text("[no_dump] backend returned empty running_snapshot\n")

        # C: backend(s)
        try:
            aggC = _apply_backend_chain(C_mods, domain_ctx={"name": "C"}, intent=intent, target=target_C)
            domC_info.update(aggC)
        except Exception as e:
            domC_info["applied"] = False
            domC_info["responses"] = [{"backend": "chain", "applied": False, "error": str(e)}]

        # Dump P4 readback
        dump_txt = None
        for r in domC_info.get("responses", []):
            info = r.get("response")
            if isinstance(info, dict) and info.get("readback_dump"):
                dump_txt = info.get("readback_dump")
                break
        if dump_txt:
            domC_info["readback_dump"] = "[embedded]"
            p4_dump_C.write_text(str(dump_txt))
        else:
            domC_info["readback_dump"] = "[no_dump]"
            p4_dump_C.write_text("[no_dump] backend returned empty readback_dump\n")
    else:
        # baseline: não aplica backends
        netconf_dump_B.write_text("[baseline] no netconf changes applied\n")
        p4_dump_C.write_text("[baseline] no p4 changes applied\n")

    # após o evento, aguarda o fim do experimento
    tracker.sleep_until_ms(int(phase_plan[-1]["end_ms"]))

    # para coletores e garante iperf finalizado
    sampler_B.stop()
    sampler_C.stop()

    thB.join(timeout=2.0)
    thC.join(timeout=2.0)

    # escreve dumps do A (sempre, para rastreabilidade)
    try:
        rb = domA_info.get("readback_adapt") or domA_info.get("readback_env") or {}
        tc_dump_A.write_text(
            "### tc qdisc\n" + (rb.get("qdisc") or "") +
            "\n\n### tc class\n" + (rb.get("class") or "") +
            "\n\n### tc filter\n" + (rb.get("filter") or "") + "\n"
        )
    except Exception as e:
        tc_dump_A.write_text(f"[tc_dump_error] {e}\n")

    # salva dom_*.json
    safe_dump_json(domA, domA_info)
    safe_dump_json(domB, domB_info)
    safe_dump_json(domC, domC_info)

    s2["backend_apply"]["A"] = bool(domA_info.get("applied_adaptation", False) if args.mode == "adapt" else domA_info.get("applied_env", False))
    s2["backend_apply"]["B"] = bool(domB_info.get("applied", False))
    s2["backend_apply"]["C"] = bool(domC_info.get("applied", False))

    # métricas globais (compat com versões anteriores)
    s2["metrics"] = {
        "throughput_B_mbps": parse_iperf3_mbps(ipf_B),
        "throughput_C_mbps": parse_iperf3_mbps(ipf_C),
        "rtt_B": rtt_stats_from_samples(sampler_B.samples),
        "rtt_C": rtt_stats_from_samples(sampler_C.samples),
    }

    # métricas por fase (para Figura de recovery, sem ambiguidade)
    phases_metrics = compute_phases_metrics(
        phase_plan=phase_plan,
        samples_B=sampler_B.samples,
        samples_C=sampler_C.samples,
        interval_ms=int(args.rtt_interval_ms),
        rtt_limit_ms=rtt_limit,
        delivery_limit=float(args.conformance_delivery),
    )
    s2["phases_metrics"] = phases_metrics

    # recovery (tempo para retornar à conformidade após o evento)
    s2["recovery"] = compute_recovery(
        event_name=str(args.event_name),
        phase_plan=phase_plan,
        samples_B=sampler_B.samples,
        samples_C=sampler_C.samples,
        interval_ms=int(args.rtt_interval_ms),
        rtt_limit_ms=rtt_limit,
        delivery_limit=float(args.conformance_delivery),
        bin_ms=int(args.recovery_bin_ms),
    )

    # contexto do RTT contínuo (para auditoria)
    s2["rtt_continuous"] = {
        "B": sampler_B.meta,
        "C": sampler_C.meta,
        "format": "csv:t_ms,seq,rtt_ms (t_ms relativo ao início do experimento)",
        "note": "t_ms foi amostrado no recebimento da resposta ICMP no namespace do source",
    }

    safe_dump_json(summary, s2)

    # Feedback no terminal
    print(json.dumps(s2, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
