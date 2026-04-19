# -*- coding: utf-8 -*-
"""
S1: Unicast QoS multi-domínios (A/B/C), com foco em **mensurar conformidade** e **custo operacional**
da adaptação (baseline × adapt, mock × real) sem ambiguidade metodológica.

--------------------------------------------------------------------------------
Princípio científico (importante)
--------------------------------------------------------------------------------
- --spec descreve a INTENÇÃO (o que as camadas superiores pedem).
- --bwA/--bwB/--bwC/--delay-ms descrevem o AMBIENTE experimental (o que o testbed oferece),
  tipicamente configurado pelo administrador/operador.
- Logo: bw*/delay NÃO sobrescrevem o spec. Eles configuram o ambiente e são registrados no sumário.

--------------------------------------------------------------------------------
Topologia (S1)
--------------------------------------------------------------------------------
Este cenário assume 3 namespaces (criadas por scripts/s1_topology_setup.sh):

- h1: origem do fluxo sob intenção (domínio A)
- h2: destino do tráfego concorrente BE (domínio B)
- h3: destino do fluxo sob intenção (domínio C)

IPs padrão (ajuste se o setup mudar):
- h1: 10.0.0.1
- h2: 10.0.0.2
- h3: 10.0.0.3

--------------------------------------------------------------------------------
Artefatos (S1_{TS}_*)
--------------------------------------------------------------------------------
- preflight_flow.txt / preflight_be.txt (ping)
- iperf_flow.json / iperf_be.json (iperf3 client JSON)
- rtt_flow.csv (amostragem com timestamp relativo em ms)
- dom_{A,B,C}.json (evidências, comandos, erros)
- tc_dump_A.txt (read-back do estado no domínio A)
- netconf_dump_B.txt (read-back do running config no domínio B)
- p4_dump_C.txt (read-back do estado no domínio C)
- summary S1_{TS}.json (todos os caminhos + métricas + conformance + timing)

--------------------------------------------------------------------------------
Contagem de tempo (IMPORTANTE para paper)
--------------------------------------------------------------------------------
Este arquivo produz:
- t_wall_start / t_wall_end: timestamps de parede (ISO)
- duration_ms: tempo total do experimento (parede)
- timing.control_plane_ms.total: tempo **apenas** do plano de controle (parse spec + computação da intenção
  + chamadas de backends + dumps/readback). **Não inclui** a duração do tráfego.
- timing.data_plane_ms: tempo aproximado do plano de dados (o bloco de tráfego/medições).
  OBS: esta métrica é operacional; o "ground truth" de tráfego é --duration (segundos).

Isto corrige o erro clássico:
- control_plane_ms.total ≈ duration_ms  (INCORRETO)
que ocorreu quando o total era calculado a partir de t_wall_start/t_wall_end.

--------------------------------------------------------------------------------
Obs
--------------------------------------------------------------------------------
- Não removemos features/artefatos existentes: sempre adicionamos.
- Para baseline, não aplicamos alterações de backends (mas ainda geramos dumps com indicação baseline).
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import inspect
import threading

# ---------------- util ----------------

def utc_ts() -> str:
    # timezone-aware (evita DeprecationWarning)
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def iso_utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")


ROOT = Path(__file__).resolve().parents[1]
RES_DIR = ROOT / "results" / "S1"
RES_DIR.mkdir(parents=True, exist_ok=True)


def run(cmd: List[str],
        check: bool = True,
        capture: bool = False,
        ns: str | None = None) -> str | None:
    """
    Executa comando (opcionalmente dentro de um namespace).
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


# ----------- RTT sampling (com timestamp) -----------

def _parse_ping_time_ms(line: str) -> Optional[float]:
    # Ex: "64 bytes from 10.0.0.3: icmp_seq=1 ttl=64 time=0.123 ms"
    if "time=" not in line:
        return None
    try:
        frag = line.split("time=", 1)[1]
        # pode vir "0.123 ms" ou "0.123 ms\n"
        val = frag.split()[0].strip()
        return float(val)
    except Exception:
        return None


def sample_rtt(ns_src: str,
               dst_ip: str,
               duration_s: int,
               interval_ms: int,
               out_csv: Path,
               stop_evt: threading.Event) -> Dict[str, Any]:
    """
    Amostra RTT por ping ao longo do tempo e grava CSV com timestamp relativo em ms:
      t_ms, seq, rtt_ms, ok

    - t_ms é relativo ao início da coleta (t0).
    - ok=1 se houve resposta; ok=0 caso contrário.

    Retorna metadados úteis (n_ok, n_req, interval_ms).
    """
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    seq = 0
    n_ok = 0
    n_req = 0

    with out_csv.open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["t_ms", "seq", "rtt_ms", "ok"])

        while not stop_evt.is_set():
            now = time.perf_counter()
            if (now - t0) >= duration_s:
                break

            seq += 1
            n_req += 1

            # ping "unitário" para ter timestamp controlado pelo Python
            p = subprocess.run(
                ["ip", "netns", "exec", ns_src, "ping", "-n", "-c", "1", "-W", "1", dst_ip],
                text=True,
                capture_output=True,
            )

            t_ms = int((time.perf_counter() - t0) * 1000)

            if p.returncode == 0:
                rtt_ms = None
                for ln in (p.stdout or "").splitlines():
                    v = _parse_ping_time_ms(ln)
                    if v is not None:
                        rtt_ms = v
                        break
                if rtt_ms is not None:
                    n_ok += 1
                    wr.writerow([t_ms, seq, f"{rtt_ms:.3f}", 1])
                else:
                    wr.writerow([t_ms, seq, "", 0])
            else:
                wr.writerow([t_ms, seq, "", 0])

            # intervalo (best-effort)
            sleep_s = max(0.0, interval_ms / 1000.0)
            time.sleep(sleep_s)

    return {"n_ok": n_ok, "n_req": n_req, "interval_ms": interval_ms}


def rtt_percentile_from_csv(csv_path: Path, pctl: str) -> Tuple[float, int]:
    """
    Retorna (rtt_pctl_ms, n_samples_ok) a partir do CSV t_ms,seq,rtt_ms,ok.

    pctl: "P50"|"P95"|"P99" (case-insensitive).
    """
    pctl = pctl.strip().upper()
    p_map = {"P50": 0.50, "P95": 0.95, "P99": 0.99}
    p = p_map.get(pctl, 0.99)

    vals: List[float] = []
    with csv_path.open() as f:
        rd = csv.DictReader(f)
        for r in rd:
            try:
                if str(r.get("ok", "0")).strip() == "1":
                    vals.append(float(r["rtt_ms"]))
            except Exception:
                pass

    if not vals:
        return (0.0, 0)

    vals.sort()

    # interpolação linear simples (estável para n pequeno)
    k = (len(vals) - 1) * p
    i = int(k)
    d = k - i
    if i + 1 < len(vals):
        val = vals[i] * (1 - d) + vals[i + 1] * d
    else:
        val = vals[i]

    return (round(val, 3), len(vals))


# ----------- iperf3 (flow e BE) -----------

def run_iperf3(duration: int,
               out_json: Path,
               ns_cli: str,
               ns_srv: str,
               srv_ip: str,
               rate_mbps: float,
               port: int,
               udp: bool = True) -> Dict[str, Any]:
    """
    Executa iperf3 (cliente ns_cli -> servidor ns_srv) e grava JSON do CLIENTE.

    - Se udp=True: usa -u -b {rate}M (taxa alvo controlada)
    - Se udp=False: TCP "best-effort" (pode saturar).
    """
    # 1) sobe servidor (um-shot)
    srv_cmd = ["ip", "netns", "exec", ns_srv, "iperf3", "-s", "-1", "-B", srv_ip, "-p", str(port)]
    srv = subprocess.Popen(srv_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    # 2) aguarda LISTEN (robustez)
    ready = False
    for _ in range(40):
        time.sleep(0.05)
        try:
            out = run(["ss", "-lntp"], ns=ns_srv, check=False, capture=True) or ""
            if f":{port}" in out and "iperf3" in out:
                ready = True
                break
        except Exception:
            pass

    if not ready:
        serr = ""
        try:
            serr = srv.stderr.read() if srv.stderr else ""
        except Exception:
            pass
        safe_dump_json(out_json, {
            "error": "iperf3 server not ready",
            "server_cmd": " ".join(srv_cmd),
            "server_stderr": serr,
        })
        try:
            srv.kill()
        except Exception:
            pass
        return {"ok": False, "error": "server_not_ready"}

    # 3) cliente
    cli_cmd = ["ip", "netns", "exec", ns_cli, "iperf3", "-c", srv_ip, "-p", str(port), "-t", str(duration)]
    if udp:
        cli_cmd += ["-u", "-b", f"{rate_mbps}M"]
    cli_cmd += ["--json"]

    cli = subprocess.run(cli_cmd, text=True, capture_output=True)

    if cli.returncode == 0 and cli.stdout.strip().startswith("{"):
        out_json.write_text(cli.stdout)
        ok = True
        err = ""
    else:
        safe_dump_json(out_json, {"error": "iperf3 client failed", "stdout": cli.stdout, "stderr": cli.stderr, "cmd": " ".join(cli_cmd)})
        ok = False
        err = "client_failed"

    # 4) encerra server
    try:
        srv.wait(timeout=2)
    except Exception:
        try:
            srv.kill()
        except Exception:
            pass

    return {"ok": ok, "error": err, "cmd": " ".join(cli_cmd), "server_cmd": " ".join(srv_cmd)}


def parse_iperf3_mbps(path: Path) -> Optional[float]:
    """
    Extrai Mbps do JSON do iperf3 (client).
    - UDP: usa end.sum.bits_per_second (iperf3 costuma fornecer)
    - TCP: tenta end.sum_received.bits_per_second
    Retorna None se falhar (permite detectar resultados "não observáveis").
    """
    try:
        j = json.loads(path.read_text())
    except Exception:
        return None

    if isinstance(j, dict) and j.get("error") and "end" not in j:
        return None

    try:
        end = j.get("end", {})
        if "sum" in end and isinstance(end["sum"], dict) and end["sum"].get("bits_per_second") is not None:
            bps = float(end["sum"]["bits_per_second"])
            return round(bps / 1e6, 3)
        if "sum_received" in end and isinstance(end["sum_received"], dict):
            bps = float(end["sum_received"].get("bits_per_second") or 0.0)
            return round(bps / 1e6, 3) if bps else None
    except Exception:
        return None

    return None


# ------------- TC (Domínio A) -------------

def tc_apply_h1(rate_mbps: float, ns: str, dev: str, prio_cls: str) -> Dict[str, Any]:
    """
    Aplica QoS local (tc/htb) no domínio A e retorna evidências:
    - lista de comandos executados
    - dumps de read-back (qdisc/class/filter)
    """
    cmds: List[List[str]] = []

    def _r(c: List[str], check: bool = True):
        cmds.append(c)
        run(c, check=check, ns=ns)

    # Limpa e recria HTB simples com uma classe "prioritária".
    _r(["tc", "qdisc", "del", "dev", dev, "root"], check=False)
    _r(["tc", "qdisc", "add", "dev", dev, "root", "handle", "1:", "htb"])
    _r(["tc", "class", "add", "dev", dev, "parent", "1:", "classid", "1:1", "htb",
        "rate", f"{int(rate_mbps)}mbit", "ceil", f"{int(rate_mbps)}mbit"])
    _r(["tc", "class", "add", "dev", dev, "parent", "1:1", "classid", f"1:{prio_cls}", "htb",
        "rate", f"{int(rate_mbps)}mbit", "ceil", f"{int(rate_mbps)}mbit"])

    # Exemplo: prende ICMP na classe; para tráfego real, o filtro deve casar o fluxo prioritário.
    _r(["tc", "filter", "add", "dev", dev, "protocol", "ip", "parent", "1:", "prio", "2",
        "u32", "match", "ip", "protocol", "1", "0xff", "flowid", f"1:{prio_cls}"])

    qdisc = run(["tc", "qdisc", "show", "dev", dev], check=False, capture=True, ns=ns) or ""
    classes = run(["tc", "class", "show", "dev", dev], check=False, capture=True, ns=ns) or ""
    filters = run(["tc", "filter", "show", "dev", dev, "parent", "1:"], check=False, capture=True, ns=ns) or ""

    return {
        "commands": [" ".join(c) for c in cmds],
        "readback": {"qdisc": qdisc.strip(), "class": classes.strip(), "filter": filters.strip()},
    }


# ------------- backends dinâmicos (B/C) -------------

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

    Regras:
    - (bool, info) / [bool, info] -> usa bool
    - dict com "applied": bool -> usa esse bool
    - caso contrário -> assume applied=True (compat), mantendo info=raw
    """
    if isinstance(raw, (list, tuple)) and len(raw) == 2 and isinstance(raw[0], bool):
        return raw[0], raw[1]

    if isinstance(raw, dict) and isinstance(raw.get("applied"), bool):
        return raw["applied"], raw

    return True, raw


def _apply_backend_chain(mod_or_list: Union[Any, List[Any]],
                         domain_ctx: Dict[str, Any],
                         intent: Dict[str, Any],
                         target: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Aplica cadeia de backends e interpreta applied corretamente."""
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
    Targets reais em: dsl/l2i/backends/backends_real.yaml
    Separa ambiente (credenciais, endereços) da intenção (spec).
    """
    import yaml
    ypath = ROOT / "l2i" / "backends" / "backends_real.yaml"
    if not ypath.exists():
        raise FileNotFoundError(f"Missing real-backends config: {ypath}")
    return yaml.safe_load(ypath.read_text(encoding="utf-8")) or {}


# ------------------- main -------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--duration", type=int, default=30)

    # tráfego concorrente (BE): taxa alvo (Mbps)
    ap.add_argument("--be-mbps", type=float, default=30.0)

    ap.add_argument("--mode", choices=["baseline", "adapt"], default="baseline")
    ap.add_argument("--backend", choices=["mock", "real"], default="mock")

    # Parâmetros do AMBIENTE (defaults)
    ap.add_argument("--bwA", type=float, default=100.0, help="(env) bandwidth domain A (mbps)")
    ap.add_argument("--bwB", type=float, default=100.0, help="(env) bandwidth domain B (mbps)")
    ap.add_argument("--bwC", type=float, default=100.0, help="(env) bandwidth domain C (mbps)")
    ap.add_argument("--delay-ms", type=float, default=1.0, help="(env) delay placeholder (ms)")

    # Amostragem RTT (para conformance)
    ap.add_argument("--rtt-interval-ms", type=int, default=50, help="RTT sampling interval (ms)")

    args = ap.parse_args()

    # ---------------------------------------------------------------------
    # WALL CLOCK (tempo de parede total)
    # ---------------------------------------------------------------------
    t_wall_start = iso_utc_now()
    wall_t0 = time.time()  # segundos epoch (parede)

    # ---------------------------------------------------------------------
    # CONTROL PLANE CLOCK (tempo de CP *somente*)
    # ---------------------------------------------------------------------
    cp_t0 = time.perf_counter()

    # --- carrega spec (consistência obrigatória) ---
    spec_path = Path(args.spec)
    spec = load_json_or_fail(spec_path)

    # ---------------------------------------------------------------------
    # SPEC → INTENT (S1)
    # ---------------------------------------------------------------------
    req = (spec.get("requirements") or {})
    lat = (req.get("latency") or {})
    bw = (req.get("bandwidth") or {})

    latency_pctl = str(lat.get("percentile", "P99")).strip().upper()
    latency_max_ms = float(lat.get("max_ms", 0))

    bandwidth_min_mbps = float(bw.get("min_mbps", 0))
    bandwidth_max_mbps = float(bw.get("max_mbps", 0))

    # Prioridade (nível) — aqui apenas registramos.
    prio = ((req.get("priority") or {}).get("level") or "medium")
    prio = str(prio).strip().lower()

    intent = {
        "latency_pctl": latency_pctl,
        "latency_max_ms": latency_max_ms,
        "bandwidth_min_mbps": bandwidth_min_mbps,
        "bandwidth_max_mbps": bandwidth_max_mbps,
        "priority_level": prio,
    }

    # ---------------------------------------------------------------------
    # ENDPOINTS FIXOS DO TESTBED (C1/C2)
    # ---------------------------------------------------------------------
    # Fluxo sob intenção: h1 -> h3 (domínio C)
    ns_src = "h1"
    ns_be_dst = "h2"
    ns_flow_dst = "h3"

    host_ip = {"h1": "10.0.0.1", "h2": "10.0.0.2", "h3": "10.0.0.3"}
    ip_src = host_ip[ns_src]
    ip_be = host_ip[ns_be_dst]
    ip_flow = host_ip[ns_flow_dst]

    endpoints = {
        "flow": {"src": ns_src, "dst": ns_flow_dst, "dst_ip": ip_flow},
        "be": {"src": ns_src, "dst": ns_be_dst, "dst_ip": ip_be},
    }

    # ---------------------------------------------------------------------
    # BACKENDS / TARGETS
    # ---------------------------------------------------------------------
    backends = get_backends(args.backend)
    B_mods = backends["B"]
    C_mods = backends["C"]

    real_cfg = {}
    if args.backend == "real":
        real_cfg = _load_real_targets_yaml()

    target_B = (real_cfg.get("B", {}) or {}).get("target") if args.backend == "real" else None
    target_C = (real_cfg.get("C", {}) or {}).get("target") if args.backend == "real" else None

    # C2: unificar dst_ip de target_C com o dst_ip do FLOW (sem ambiguidade).
    # Se seu backend usa dst_ip como “parâmetro de match do fluxo”, ele deve ser o destino do fluxo.
    if isinstance(target_C, dict):
        target_C = dict(target_C)
        target_C["dst_ip"] = ip_flow

    # ---------------------------------------------------------------------
    # ARTEFATOS
    # ---------------------------------------------------------------------
    ts = utc_ts()

    pre_flow = RES_DIR / f"S1_{ts}_preflight_flow.txt"
    pre_be = RES_DIR / f"S1_{ts}_preflight_be.txt"

    ipf_flow = RES_DIR / f"S1_{ts}_iperf_flow.json"
    ipf_be = RES_DIR / f"S1_{ts}_iperf_be.json"

    rtt_flow = RES_DIR / f"S1_{ts}_rtt_flow.csv"

    domA = RES_DIR / f"S1_{ts}_dom_A.json"
    domB = RES_DIR / f"S1_{ts}_dom_B.json"
    domC = RES_DIR / f"S1_{ts}_dom_C.json"

    tc_dump_A = RES_DIR / f"S1_{ts}_tc_dump_A.txt"
    netconf_dump_B = RES_DIR / f"S1_{ts}_netconf_dump_B.txt"
    p4_dump_C = RES_DIR / f"S1_{ts}_p4_dump_C.txt"

    summary = RES_DIR / f"S1_{ts}.json"

    # Estrutura base do sumário (será completada ao final).
    s1: Dict[str, Any] = {
        "scenario": "S1_unicast_qos",
        "flow_id": (spec.get("flow") or {}).get("id", "S1"),
        "timestamp_utc": ts,
        "mode": args.mode,
        "backend_mode": args.backend,
        "spec_path": str(spec_path),
        "spec_exists": True,
        "environment": {
            "bwA_mbps": args.bwA,
            "bwB_mbps": args.bwB,
            "bwC_mbps": args.bwC,
            "delay_ms": args.delay_ms,
            "be_mbps": args.be_mbps,  # pedido explícito: registrar no sumário
        },
        "intent": {
            "latency_pctl": latency_pctl,
            "latency_max_ms": latency_max_ms,
            "bandwidth_min_mbps": bandwidth_min_mbps,
            "bandwidth_max_mbps": bandwidth_max_mbps,
        },
        "endpoints": endpoints,
        "targets": {
            "B": _mask_secret(target_B or {}) if target_B else None,
            "C": _mask_secret(target_C or {}) if target_C else None,
        },
        "backend_apply": {"A": False, "B": False, "C": False},
        "artifacts": {
            "domA_json": str(domA),
            "domB_json": str(domB),
            "domC_json": str(domC),
            "preflight_flow": str(pre_flow),
            "preflight_be": str(pre_be),
            "iperf_flow_json": str(ipf_flow),
            "iperf_be_json": str(ipf_be),
            "rtt_flow_csv": str(rtt_flow),
            "tc_dump_A_txt": str(tc_dump_A),
            "netconf_dump_B_txt": str(netconf_dump_B),
            "p4_dump_C_txt": str(p4_dump_C),
            "summary_json": str(summary),
        },
        # timing será preenchido ao final
        "timing": {},
        "metrics": {},
        "conformance": {},
    }

    # ---------------------------------------------------------------------
    # PREFLIGHT (CP, mas barato)
    # ---------------------------------------------------------------------
    try:
        out = run(["ping", "-n", "-c", "3", ip_flow], ns=ns_src, check=False, capture=True) or ""
        pre_flow.write_text(out)
    except Exception as e:
        pre_flow.write_text(f"[preflight_error] {e}")

    try:
        out = run(["ping", "-n", "-c", "3", ip_be], ns=ns_src, check=False, capture=True) or ""
        pre_be.write_text(out)
    except Exception as e:
        pre_be.write_text(f"[preflight_error] {e}")

    # ---------------------------------------------------------------------
    # A (tc) — domínio local
    # ---------------------------------------------------------------------
    domA_info: Dict[str, Any] = {
        "backend": "linux_tc_local",
        "applied": False,
        "env_params": {"bwA_mbps": args.bwA},
        "commands": [],
        "readback": {},
    }

    t_tc0 = time.perf_counter()
    if args.mode == "adapt":
        evA = tc_apply_h1(rate_mbps=args.bwA, ns=ns_src, dev=f"{ns_src}-eth0", prio_cls="10")
        domA_info["applied"] = True
        domA_info["commands"] = evA["commands"]
        domA_info["readback"] = evA["readback"]
        tc_dump_A.write_text(
            "### tc qdisc\n" + (evA["readback"].get("qdisc") or "") +
            "\n\n### tc class\n" + (evA["readback"].get("class") or "") +
            "\n\n### tc filter\n" + (evA["readback"].get("filter") or "") + "\n"
        )
    else:
        tc_dump_A.write_text("[baseline] no tc changes applied\n")
    tc_apply_ms = int((time.perf_counter() - t_tc0) * 1000)

    safe_dump_json(domA, domA_info)
    s1["backend_apply"]["A"] = bool(domA_info["applied"])

    # ---------------------------------------------------------------------
    # B (NETCONF) — domínio B
    # ---------------------------------------------------------------------
    domB_info: Dict[str, Any] = {
        "backend_chain": [getattr(m, "__name__", str(m)) for m in (B_mods if isinstance(B_mods, (list, tuple)) else [B_mods])],
        "applied": False,
        "env_params": {"bwB_mbps": args.bwB},
        "target": _mask_secret(target_B or {}) if target_B else None,
        "responses": [],
    }

    t_b0 = time.perf_counter()
    if args.mode == "adapt":
        aggB = _apply_backend_chain(B_mods, domain_ctx={"name": "B"}, intent=intent, target=target_B)
        domB_info.update(aggB)

        dumped = False
        for r in domB_info.get("responses", []):
            info = r.get("response")
            if isinstance(info, dict) and info.get("running_snapshot"):
                netconf_dump_B.write_text(str(info.get("running_snapshot")))
                dumped = True
                break
        if not dumped:
            netconf_dump_B.write_text("[no_dump] backend returned empty running_snapshot\n")
    else:
        netconf_dump_B.write_text("[baseline] no netconf changes applied\n")
    b_apply_ms = int((time.perf_counter() - t_b0) * 1000)

    safe_dump_json(domB, domB_info)
    s1["backend_apply"]["B"] = bool(domB_info.get("applied", False))

    # ---------------------------------------------------------------------
    # C (P4) — domínio C
    # ---------------------------------------------------------------------
    domC_info: Dict[str, Any] = {
        "backend_chain": [getattr(m, "__name__", str(m)) for m in (C_mods if isinstance(C_mods, (list, tuple)) else [C_mods])],
        "applied": False,
        "env_params": {"bwC_mbps": args.bwC},
        "target": _mask_secret(target_C or {}) if target_C else None,
        "readback_dump": None,
        "responses": [],
    }

    t_c0 = time.perf_counter()
    if args.mode == "adapt":
        aggC = _apply_backend_chain(C_mods, domain_ctx={"name": "C"}, intent=intent, target=target_C)
        domC_info.update(aggC)

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
        p4_dump_C.write_text("[baseline] no p4 changes applied\n")
    c_apply_ms = int((time.perf_counter() - t_c0) * 1000)

    safe_dump_json(domC, domC_info)
    s1["backend_apply"]["C"] = bool(domC_info.get("applied", False))

    # ---------------------------------------------------------------------
    # FINALIZA CONTROL PLANE CLOCK *ANTES* DO TRÁFEGO/DATA-PLANE
    # ---------------------------------------------------------------------
    cp_end = time.perf_counter()
    control_plane_total_ms = int((cp_end - cp_t0) * 1000)

    # ---------------------------------------------------------------------
    # DATA PLANE (tráfego + amostragem RTT)
    # ---------------------------------------------------------------------
    dp_t0 = time.perf_counter()

    # 1) inicia amostragem RTT em paralelo (fluxo sob intenção)
    stop_evt = threading.Event()
    rtt_meta: Dict[str, Any] = {}
    def _rtt_worker():
        nonlocal rtt_meta
        rtt_meta = sample_rtt(ns_src=ns_src, dst_ip=ip_flow, duration_s=args.duration, interval_ms=args.rtt_interval_ms, out_csv=rtt_flow, stop_evt=stop_evt)

    th = threading.Thread(target=_rtt_worker, daemon=True)
    th.start()

    # 2) executa iperf BE e FLOW (sequencial para evitar interferência de portas/servidores)
    #    Observação: para "contenção real", ideal é rodar em paralelo.
    #    Aqui preferimos robustez e reprodutibilidade; se necessário, paralelize depois.
    #    Portas distintas evitam colisões.
    #    FLOW: taxa alvo = 2x do teto (força o mecanismo de QoS a atuar)
    flow_rate = max(1.0, float(bandwidth_max_mbps) * 2.0) if bandwidth_max_mbps > 0 else 10.0

    run_iperf3(duration=args.duration, out_json=ipf_be, ns_cli=ns_src, ns_srv=ns_be_dst, srv_ip=ip_be,
              rate_mbps=float(args.be_mbps), port=5201, udp=True)

    run_iperf3(duration=args.duration, out_json=ipf_flow, ns_cli=ns_src, ns_srv=ns_flow_dst, srv_ip=ip_flow,
              rate_mbps=flow_rate, port=5202, udp=True)

    # 3) encerra RTT sampler (caso o tempo tenha passado)
    stop_evt.set()
    th.join(timeout=2)

    data_plane_ms = int((time.perf_counter() - dp_t0) * 1000)

    # ---------------------------------------------------------------------
    # METRICS (flow vs BE) + CONFORMANCE
    # ---------------------------------------------------------------------
    flow_thr = parse_iperf3_mbps(ipf_flow)
    be_thr = parse_iperf3_mbps(ipf_be)

    rtt_pctl_ms, n_rtt_ok = rtt_percentile_from_csv(rtt_flow, latency_pctl)
    delivery_ratio = round((rtt_meta.get("n_ok", 0) / rtt_meta.get("n_req", 1)), 6) if rtt_meta else 0.0

    # ---- Conformance components (pedido explícito)
    latency_ok = (rtt_pctl_ms <= latency_max_ms) if latency_max_ms > 0 else True
    bandwidth_ok = True
    if flow_thr is None:
        # se não mediu, não podemos afirmar conformidade de banda
        bandwidth_ok = False
    else:
        if bandwidth_min_mbps > 0:
            bandwidth_ok = bandwidth_ok and (flow_thr >= bandwidth_min_mbps)
        if bandwidth_max_mbps > 0:
            bandwidth_ok = bandwidth_ok and (flow_thr <= bandwidth_max_mbps)

    intent_ok = bool(latency_ok and bandwidth_ok)

    s1["metrics"] = {
        # Nomenclatura "real": reporta qual percentil foi usado (pctl) e o valor (ms)
        "rtt_pctl": latency_pctl,
        "rtt_pctl_ms": rtt_pctl_ms,
        "rtt_n_ok": int(n_rtt_ok),
        "delivery_ratio": delivery_ratio,

        # Vazões separadas (A recomendada): fluxo sob intenção e tráfego concorrente
        "flow_throughput_mbps": flow_thr,
        "be_throughput_mbps": be_thr,
    }

    s1["conformance"] = {
        "latency_ok": bool(latency_ok),
        "bandwidth_ok": bool(bandwidth_ok),
        "intent_ok": bool(intent_ok),
    }

    # ---------------------------------------------------------------------
    # TIMING (corrigido)
    # ---------------------------------------------------------------------
    t_wall_end = iso_utc_now()
    duration_ms = int((time.time() - wall_t0) * 1000)

    s1["timing"] = {
        "t_wall_start": t_wall_start,
        "t_wall_end": t_wall_end,
        "duration_ms": duration_ms,

        "control_plane_ms": {
            "total": control_plane_total_ms,
            "tc_apply": tc_apply_ms,
            "netconf_apply": b_apply_ms,
            "p4_apply": c_apply_ms,
        },

        # Medida operacional do data-plane.
        "data_plane_ms": data_plane_ms,
    }

    # ---------------------------------------------------------------------
    # Persistência e feedback
    # ---------------------------------------------------------------------
    safe_dump_json(summary, s1)
    print(json.dumps(s1, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
