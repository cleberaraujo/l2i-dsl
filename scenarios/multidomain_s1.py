# -*- coding: utf-8 -*-
"""
S1: Unicast QoS multi-domínios (A/B/C).

Princípios (consistência metodológica):
- --spec descreve a INTENÇÃO (requisitos das camadas superiores).
- --bwA/--bwB/--bwC/--delay-ms descrevem o AMBIENTE experimental (o que o testbed oferece),
  tipicamente configurado pelo administrador/operador.
- bw*/delay NÃO sobrescrevem o spec: configuram ambiente e são registrados no sumário.

Domínios:
- A: Linux tc/htb (local, no namespace do host origem)
- B: NETCONF (real via ncclient ou mock)
- C: P4Runtime (real via gRPC ou mock)

Artefatos (S1_{TS}.json + dumps por domínio):
- dom_{A,B,C}.json (evidências e erros)
- tc_dump_A (read-back do estado)
- netconf_dump_B (read-back do running config)
- p4_dump_C (read-back da tabela)
- preflight_flow / preflight_be (ping)
- iperf_flow / iperf_be (JSON do cliente)
- rtt_flow.csv (amostras) + métricas derivadas

Sumário inclui:
- environment.be_mbps (tráfego concorrente)
- intent (componentes reais, incluindo rtt_pctl)
- metrics (flow_throughput_mbps e be_throughput_mbps separados)
- conformance (latency_ok, bandwidth_ok, intent_ok)
- timing (duration_ms, control_plane_ms.total + por domínio)

Requisito de topologia (critério C1/C2):
- O destino do FLOW é o host no domínio C (default: h3 / 10.0.0.3).
- targets.C.dst_ip (quando existir) deve ser o MESMO IP do destino do flow,
  para evitar ambiguidade metodológica.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import inspect

# ---------------- util ----------------

def utc_ts() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")

def utc_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()

ROOT = Path(__file__).resolve().parents[1]
RES_DIR = ROOT / "results" / "S1"
RES_DIR.mkdir(parents=True, exist_ok=True)

def run(cmd: List[str],
        check: bool = True,
        capture: bool = False,
        ns: str | None = None) -> str | None:
    if ns:
        cmd = ["ip", "netns", "exec", ns, *cmd]
    p = subprocess.run(cmd, text=True, capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError(
            f"cmd failed: {' '.join(cmd)}\nRC={p.returncode}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}"
        )
    return p.stdout if capture else None

def safe_dump_json(path: Path, obj: Any) -> None:
    try:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    except Exception as e:
        path.write_text(json.dumps({"error": f"dump_failed: {e}"}, ensure_ascii=False, indent=2))

def load_json_or_fail(path: Path) -> Dict[str, Any]:
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

def _pctl_to_float(pctl: str) -> float:
    p = str(pctl).strip().upper()
    if p == "P50": return 0.50
    if p == "P95": return 0.95
    return 0.99  # default P99

# ----------- RTT -----------

def collect_rtt_samples(ns: str, dst_ip: str, samples: int, interval_ms: int, out_csv: Path) -> Tuple[int, int]:
    """Coleta RTT via ping e grava CSV: seq,rtt_ms"""
    awk = r"""awk '/time=/{idx++; sub(/time=/, "", $7); printf("%d,%.3f\n", idx, $7)}'"""
    cmd = f"ping -n -i {interval_ms/1000.0:.3f} -c {samples} {dst_ip} | {awk}"
    out = run(["bash", "-lc", cmd], check=True, capture=True, ns=ns) or ""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    out_csv.write_text("\n".join(["seq,rtt_ms", *lines]))
    return (len(lines), samples)

def rtt_percentile_ms(csv_path: Path, pctl: float) -> Tuple[float, int]:
    vals: List[float] = []
    with csv_path.open() as f:
        rd = csv.DictReader(f)
        for r in rd:
            try:
                vals.append(float(r["rtt_ms"]))
            except Exception:
                pass
    if not vals:
        return (0.0, 0)
    vals.sort()
    k = (len(vals) - 1) * pctl
    i = int(k)
    d = k - i
    if i + 1 < len(vals):
        v = vals[i] * (1 - d) + vals[i + 1] * d
    else:
        v = vals[i]
    return (round(v, 1), len(vals))

# ----------- iperf3 -----------

def run_iperf3_unicast(duration: int,
                       mbps: float,
                       out_json: Path,
                       ns_cli: str,
                       ns_srv: str,
                       srv_ip: str,
                       port: int) -> None:
    # server
    srv_cmd = ["ip", "netns", "exec", ns_srv, "iperf3", "-s", "-1", "-B", srv_ip, "-p", str(port)]
    srv = subprocess.Popen(srv_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    ready = False
    for _ in range(50):
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
        safe_dump_json(out_json, {"error": "iperf3 server not ready", "server_cmd": " ".join(srv_cmd), "server_stderr": serr})
        try:
            srv.kill()
        except Exception:
            pass
        return

    cli = subprocess.run(
        ["ip", "netns", "exec", ns_cli, "iperf3",
         "-c", srv_ip, "-p", str(port),
         "-t", str(duration), "-b", f"{mbps}M", "--json"],
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
    try:
        j = json.loads(path.read_text())
    except Exception:
        return None
    if isinstance(j, dict) and j.get("error") and "end" not in j:
        return None
    if isinstance(j, dict) and j.get("error"):
        return None
    try:
        bps = j["end"]["sum_received"]["bits_per_second"]
        return round(float(bps) / 1e6, 3) if bps else None
    except Exception:
        return None

# ----------- TC (Domínio A) -----------

def tc_apply_h1(rate_mbps: float, ns: str, dev: str) -> Dict[str, Any]:
    cmds: List[List[str]] = []

    def _r(c: List[str], check: bool = True):
        cmds.append(c)
        run(c, check=check, ns=ns)

    # Baseline-friendly: sempre recria de forma idempotente
    _r(["tc", "qdisc", "del", "dev", dev, "root"], check=False)
    _r(["tc", "qdisc", "add", "dev", dev, "root", "handle", "1:", "htb"])
    _r(["tc", "class", "add", "dev", dev, "parent", "1:", "classid", "1:1", "htb",
        "rate", f"{int(rate_mbps)}mbit", "ceil", f"{int(rate_mbps)}mbit"])
    # classe default (1:10) para marcar tráfego do flow (ICMP/TCP) no experimento
    _r(["tc", "class", "add", "dev", dev, "parent", "1:1", "classid", "1:10", "htb",
        "rate", f"{int(rate_mbps)}mbit", "ceil", f"{int(rate_mbps)}mbit"])
    _r(["tc", "filter", "add", "dev", dev, "protocol", "ip", "parent", "1:", "prio", "2",
        "u32", "match", "ip", "protocol", "1", "0xff", "flowid", "1:10"], check=False)

    qdisc = run(["tc", "qdisc", "show", "dev", dev], check=False, capture=True, ns=ns) or ""
    classes = run(["tc", "class", "show", "dev", dev], check=False, capture=True, ns=ns) or ""
    filters = run(["tc", "filter", "show", "dev", dev, "parent", "1:"], check=False, capture=True, ns=ns) or ""
    return {
        "commands": [" ".join(c) for c in cmds],
        "readback": {"qdisc": qdisc.strip(), "class": classes.strip(), "filter": filters.strip()},
    }

# ----------- Backends (B/C) -----------

from l2i.backends.router import get_backends  # noqa: E402

def _call_apply_qos(mod: Any,
                    domain_ctx: Dict[str, Any],
                    intent: Dict[str, Any],
                    target: Optional[Dict[str, Any]]) -> Any:
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
    if isinstance(raw, (list, tuple)) and len(raw) == 2 and isinstance(raw[0], bool):
        return raw[0], raw[1]
    if isinstance(raw, dict) and isinstance(raw.get("applied"), bool):
        return raw["applied"], raw
    return True, raw

def _apply_backend_chain(mod_or_list: Any,
                         domain_ctx: Dict[str, Any],
                         intent: Dict[str, Any],
                         target: Optional[Dict[str, Any]]) -> Dict[str, Any]:
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
    import yaml
    ypath = ROOT / "l2i" / "backends" / "backends_real.yaml"
    if not ypath.exists():
        raise FileNotFoundError(f"Missing real-backends config: {ypath}")
    return yaml.safe_load(ypath.read_text(encoding="utf-8")) or {}

# ---------------- main ----------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--be-mbps", type=float, default=30.0)
    ap.add_argument("--mode", choices=["baseline", "adapt"], default="baseline")
    ap.add_argument("--backend", choices=["mock", "real"], default="mock")
    ap.add_argument("--bwA", type=float, default=100.0)
    ap.add_argument("--bwB", type=float, default=100.0)
    ap.add_argument("--bwC", type=float, default=100.0)
    ap.add_argument("--delay-ms", type=float, default=1.0)
    ap.add_argument("--rtt-samples", type=int, default=80)
    ap.add_argument("--rtt-interval-ms", type=int, default=50)
    args = ap.parse_args()

    # --- relógios (wall e monotônico) ---
    t_wall_start_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    t0 = time.perf_counter()  # monotônico para duração total do script

    wall_start_iso = utc_iso()
    t_wall0 = time.time()
    t_cp0 = time.perf_counter()
    cp_breakdown = {
        "parse_ms": 0.0,
        "materialize_ms": 0.0,
        "apply_A_ms": 0.0,
        "apply_B_ms": 0.0,
        "apply_C_ms": 0.0,
        "readback_ms": 0.0,
    }

    # --- load spec (parse_ms) ---
    t_parse0 = time.perf_counter()
    spec_path = Path(args.spec)
    spec = load_json_or_fail(spec_path)
    parse_ms = (time.perf_counter() - t_parse0) * 1000.0

    req = spec.get("requirements", {}) or {}
    lat = req.get("latency", {}) or {}
    bw = req.get("bandwidth", {}) or {}
    pr = req.get("priority", {}) or {}

    rtt_pctl = str(lat.get("percentile", "P99")).upper()
    rtt_p = _pctl_to_float(rtt_pctl)
    latency_max_ms = float(lat.get("max_ms", 0))
    bw_min = float(bw.get("min_mbps", 0))
    bw_max = float(bw.get("max_mbps", 0))
    prio = str(pr.get("level", "high")).lower()

    intent = {
        "rtt_pctl": rtt_pctl,
        "latency_max_ms": latency_max_ms,
        "bandwidth_min_mbps": bw_min,
        "bandwidth_max_mbps": bw_max,
        "priority": prio,
    }

    # --- endpoints (C1/C2): FLOW dst é domínio C ---
    host_ip = {"h1": "10.0.0.1", "h2": "10.0.0.2", "h3": "10.0.0.3", "h4": "10.0.0.4"}
    ns_src = "h1"
    ns_flow_dst = "h3"      # domínio C
    ip_flow_dst = host_ip[ns_flow_dst]
    ns_be_dst = "h2"        # domínio B (tráfego concorrente)
    ip_be_dst = host_ip[ns_be_dst]

    # --- backends and targets ---
    backends = get_backends(args.backend)
    B_mods = backends.get("B")
    C_mods = backends.get("C")

    real_cfg = {}
    if args.backend == "real":
        real_cfg = _load_real_targets_yaml()

    target_B = (real_cfg.get("B", {}) or {}).get("target") if args.backend == "real" else None
    target_C = (real_cfg.get("C", {}) or {}).get("target") if args.backend == "real" else None

    # C2: alinhar dst_ip do target_C com destino do FLOW (evita ambiguidade)
    if isinstance(target_C, dict):
        target_C = dict(target_C)
        target_C["dst_ip"] = ip_flow_dst

    # --- artifacts paths ---
    ts = utc_ts()
    domA = RES_DIR / f"S1_{ts}_dom_A.json"
    domB = RES_DIR / f"S1_{ts}_dom_B.json"
    domC = RES_DIR / f"S1_{ts}_dom_C.json"

    tc_dump_A = RES_DIR / f"S1_{ts}_tc_dump_A.txt"
    netconf_dump_B = RES_DIR / f"S1_{ts}_netconf_dump_B.txt"
    p4_dump_C = RES_DIR / f"S1_{ts}_p4_dump_C.txt"

    pre_flow = RES_DIR / f"S1_{ts}_preflight_flow.txt"
    pre_be = RES_DIR / f"S1_{ts}_preflight_be.txt"

    ipf_flow = RES_DIR / f"S1_{ts}_iperf_flow.json"
    ipf_be = RES_DIR / f"S1_{ts}_iperf_be.json"

    rtt_flow_csv = RES_DIR / f"S1_{ts}_rtt_flow.csv"
    summary = RES_DIR / f"S1_{ts}.json"

    # --- summary skeleton ---
    s1: Dict[str, Any] = {
        "scenario": "S1_unicast_qos",
        "flow_id": (spec.get("flow", {}) or {}).get("id", "S1_UnicastQoS"),
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
            "be_mbps": args.be_mbps,
        },
        "endpoints": {
            "flow": {"src": ns_src, "dst": ns_flow_dst, "dst_ip": ip_flow_dst},
            "be": {"src": ns_src, "dst": ns_be_dst, "dst_ip": ip_be_dst},
        },
        "targets": {
            "B": _mask_secret(target_B or {}) if target_B else None,
            "C": _mask_secret(target_C or {}) if target_C else None,
        },
        "intent": intent,
        "backend_apply": {"A": False, "B": False, "C": False},
        "timing": {
            "t_wall_start_utc": wall_start_iso,
            "t_wall_end_utc": None,
            "duration_ms": None,
            "control_plane_ms": {
                "total": None,
                "parse_ms": round(parse_ms, 3),
                "domains": {"A": 0.0, "B": 0.0, "C": 0.0},
            },
        },
        "artifacts": {
            "domA_json": str(domA),
            "domB_json": str(domB),
            "domC_json": str(domC),
            "tc_dump_A_txt": str(tc_dump_A),
            "netconf_dump_B_txt": str(netconf_dump_B),
            "p4_dump_C_txt": str(p4_dump_C),
            "preflight_flow": str(pre_flow),
            "preflight_be": str(pre_be),
            "iperf_flow": str(ipf_flow),
            "iperf_be": str(ipf_be),
            "rtt_flow_csv": str(rtt_flow_csv),
            "summary_json": str(summary),
        },
    }

    # --- preflight ---
    try:
        out = run(["ping", "-n", "-c", "3", ip_flow_dst], ns=ns_src, check=False, capture=True) or ""
        pre_flow.write_text(out)
    except Exception as e:
        pre_flow.write_text(f"[preflight_error] {e}")
    try:
        out = run(["ping", "-n", "-c", "3", ip_be_dst], ns=ns_src, check=False, capture=True) or ""
        pre_be.write_text(out)
    except Exception as e:
        pre_be.write_text(f"[preflight_error] {e}")

    # --- Domain A (tc) ---
    domA_info = {
        "backend": "linux_tc_local",
        "applied": False,
        "env_params": {"bwA_mbps": args.bwA},
        "intent_seen": intent,
        "commands": [],
        "readback": {},
    }

    tA0 = time.perf_counter()
    # Em S1, tc é parte do AMBIENTE: aplicamos tanto em baseline quanto em adapt para garantir
    # que o testbed reflita bwA_mbps, sem confundir com "adaptação" (que é B/C).
    try:
        evA = tc_apply_h1(rate_mbps=args.bwA, ns=ns_src, dev=f"{ns_src}-eth0")
        domA_info["applied"] = True
        domA_info["commands"] = evA["commands"]
        domA_info["readback"] = evA["readback"]
        tc_dump_A.write_text(
            "### tc qdisc\n" + (evA["readback"].get("qdisc") or "") +
            "\n\n### tc class\n" + (evA["readback"].get("class") or "") +
            "\n\n### tc filter\n" + (evA["readback"].get("filter") or "") + "\n"
        )
    except Exception as e:
        domA_info["error"] = str(e)
        tc_dump_A.write_text(f"[error] {e}\n")
    tA_ms = (time.perf_counter() - tA0) * 1000.0
    s1["timing"]["control_plane_ms"]["domains"]["A"] = round(tA_ms, 3)

    safe_dump_json(domA, domA_info)
    s1["backend_apply"]["A"] = bool(domA_info.get("applied"))

    # --- Domain B (NETCONF) ---
    domB_info = {
        "backend_chain": [getattr(m, "__name__", str(m)) for m in (B_mods if isinstance(B_mods, (list, tuple)) else [B_mods])],
        "applied": False,
        "env_params": {"bwB_mbps": args.bwB},
        "target": _mask_secret(target_B or {}) if target_B else None,
        "responses": [],
    }

    tB_ms = 0.0
    if args.mode == "adapt":
        tB0 = time.perf_counter()
        aggB = _apply_backend_chain(B_mods, domain_ctx={"name": "B"}, intent={"class": "prio10", "min_mbps": bw_min, "max_mbps": bw_max}, target=target_B)
        domB_info.update(aggB)
        tB_ms = (time.perf_counter() - tB0) * 1000.0

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

    s1["timing"]["control_plane_ms"]["domains"]["B"] = round(tB_ms, 3)
    safe_dump_json(domB, domB_info)
    s1["backend_apply"]["B"] = bool(domB_info.get("applied", False))

    # --- Domain C (P4Runtime) ---
    domC_info = {
        "backend_chain": [getattr(m, "__name__", str(m)) for m in (C_mods if isinstance(C_mods, (list, tuple)) else [C_mods])],
        "applied": False,
        "env_params": {"bwC_mbps": args.bwC},
        "target": _mask_secret(target_C or {}) if target_C else None,
        "readback_dump": None,
        "responses": [],
    }

    tC_ms = 0.0
    if args.mode == "adapt":
        tC0 = time.perf_counter()
        aggC = _apply_backend_chain(C_mods, domain_ctx={"name": "C"}, intent={"class": "prio10", "min_mbps": bw_min, "max_mbps": bw_max}, target=target_C)
        domC_info.update(aggC)
        tC_ms = (time.perf_counter() - tC0) * 1000.0

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

    s1["timing"]["control_plane_ms"]["domains"]["C"] = round(tC_ms, 3)
    safe_dump_json(domC, domC_info)
    s1["backend_apply"]["C"] = bool(domC_info.get("applied", False))

    # --- Traffic: flow + BE (ports separados) ---
    # Flow: limitado pelo intent (bw_max) como carga oferecida (evita falso "não conforme" por oversubscription do gerador)
    flow_offered = max(0.1, min(bw_max, args.bwA, args.bwB, args.bwC))
    run_iperf3_unicast(args.duration, flow_offered, ipf_flow, ns_cli=ns_src, ns_srv=ns_flow_dst, srv_ip=ip_flow_dst, port=5201)
    run_iperf3_unicast(args.duration, args.be_mbps, ipf_be, ns_cli=ns_src, ns_srv=ns_be_dst, srv_ip=ip_be_dst, port=5202)

    # RTT do flow (h1 -> h3)
    ok, reqn = collect_rtt_samples(ns=ns_src, dst_ip=ip_flow_dst, samples=args.rtt_samples, interval_ms=args.rtt_interval_ms, out_csv=rtt_flow_csv)
    rtt_val, rtt_n = rtt_percentile_ms(rtt_flow_csv, rtt_p)

    flow_thr = parse_iperf3_mbps(ipf_flow)
    be_thr = parse_iperf3_mbps(ipf_be)

    s1["metrics"] = {
        "rtt_pctl": rtt_pctl,
        "rtt_pctl_ms": rtt_val,
        "rtt_n": rtt_n,
        "delivery_ratio": round(ok / reqn, 3) if reqn else 0.0,
        "flow_throughput_mbps": flow_thr,
        "be_throughput_mbps": be_thr,
    }

    latency_ok = (rtt_val <= latency_max_ms) if latency_max_ms > 0 else True
    bandwidth_ok = (flow_thr is not None and bw_max > 0 and flow_thr <= bw_max)
    intent_ok = bool(latency_ok and bandwidth_ok)

    s1["conformance"] = {
        "latency_ok": bool(latency_ok),
        "bandwidth_ok": bool(bandwidth_ok),
        "intent_ok": bool(intent_ok),
    }

    # --- timing finalize ---
    cp_total_ms = (time.perf_counter() - t_cp0) * 1000.0
    s1["timing"]["control_plane_ms"]["total"] = round(cp_total_ms, 3)
    s1["timing"]["t_wall_end_utc"] = utc_iso()
    s1["timing"]["duration_ms"] = int((time.time() - t_wall0) * 1000)

    safe_dump_json(summary, s1)
    print(json.dumps(s1, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
