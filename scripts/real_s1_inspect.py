#!/usr/bin/env python3
import argparse, json, os, re, glob, datetime as dt
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # .../dsl
RES_JSON = BASE / "results" / "json"
RES_INSPECT = BASE / "results" / "inspect"
RES_INSPECT.mkdir(parents=True, exist_ok=True)

def ts_now():
    try:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except Exception:
        # fallback TZ-naive (evita Deprecation no Python 3.12?)
        return dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

def pick_latest_ts(prefix="S1_"):
    files = sorted(RES_JSON.glob(f"{prefix}*"), key=os.path.getmtime, reverse=True)
    if not files:
        return None
    # extrai primeiro TS que bater com padrão
    for f in files:
        m = re.search(r"(\d{8}T\d{6}Z)", f.name)
        if m:
            return m.group(1)
    return None

def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def read_domain(ts, dom):
    return load_json(RES_JSON / f"dom_{dom}_{ts}.json")

def find_summary(ts):
    # S1 pode ou não ter o sumário. Procuramos por padrão.
    cand = RES_JSON / f"S1_{ts}_iperf3_intent.json"
    if cand.exists():
        return load_json(cand)
    # ou o stdout “compactado” que você já mostrou:
    for p in RES_JSON.glob(f"S1_{ts}*.json"):
        if "iperf3_intent" not in p.name and "dom_" not in p.name:
            return load_json(p)
    return None

def safe_get(d, path, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

def summarize_s1(ts):
    a = read_domain(ts, "A") or {}
    b = read_domain(ts, "B") or {}
    c = read_domain(ts, "C") or {}
    summ = find_summary(ts) or {}

    out = {
        "timestamp": ts,
        "scenario": "S1_unicast_multidomain_qos",
        "backend_mode": safe_get(summ, ["backend_mode"], "unknown"),
        "metrics": {
            "throughput_mbps": safe_get(summ, ["metrics","throughput_mbps"]),
            "rtt_p50_ms": safe_get(summ, ["metrics","rtt_p50_ms"]),
            "rtt_p95_ms": safe_get(summ, ["metrics","rtt_p95_ms"]),
            "rtt_p99_ms": safe_get(summ, ["metrics","rtt_p99_ms"]),
            "rtt_samples": safe_get(summ, ["metrics","rtt_samples"]),
        },
        "A": {
            "backend": safe_get(a, ["backend"]) or safe_get(a, ["backend_chain",0]),
            "applied": safe_get(a, ["applied"], False),
            "params": safe_get(a, ["params"], {}),
        },
        "B": {
            "backend": safe_get(b, ["backend"]) or safe_get(b, ["backend_chain",0]),
            "applied": safe_get(b, ["applied"], False),
            "connected": safe_get(b, ["responses",0,"response","connected"]),
            "capabilities": safe_get(b, ["responses",0,"response","capabilities"], []),
        },
        "C": {
            "backend": safe_get(c, ["backend"]) or safe_get(c, ["backend_chain",0]),
            "applied": safe_get(c, ["applied"], False),
            "connected": safe_get(c, ["responses",0,"response","connected"]),
            "pipeline_loaded": safe_get(c, ["responses",0,"response","pipeline_loaded"]),
        },
    }
    return out

def write_csv(ts, summary):
    csv_path = RES_INSPECT / f"S1_REAL_{ts}.csv"
    # linha única com campos chave p/ fácil ingestão
    caps = summary["B"]["capabilities"]
    caps_str = "|".join(caps) if caps else ""
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(",".join([
            "timestamp","backend_mode",
            "A.backend","A.applied",
            "B.backend","B.applied","B.connected","B.caps_count",
            "C.backend","C.applied","C.connected","C.pipeline_loaded",
            "thr","rtt50","rtt95","rtt99","samples"
        ])+"\n")
        f.write(",".join([
            summary["timestamp"], str(summary["backend_mode"]),
            str(summary["A"]["backend"]), str(summary["A"]["applied"]),
            str(summary["B"]["backend"]), str(summary["B"]["applied"]), str(summary["B"]["connected"] or False), str(len(caps)),
            str(summary["C"]["backend"]), str(summary["C"]["applied"]), str(summary["C"]["connected"] or False), str(summary["C"]["pipeline_loaded"]),
            str(summary["metrics"]["throughput_mbps"]), str(summary["metrics"]["rtt_p50_ms"]), str(summary["metrics"]["rtt_p95_ms"]), str(summary["metrics"]["rtt_p99_ms"]), str(summary["metrics"]["rtt_samples"])
        ])+"\n")
    return csv_path

def write_md(ts, summary):
    md_path = RES_INSPECT / f"S1_REAL_{ts}.md"
    caps = summary["B"]["capabilities"] or []
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# S1 (real) – inspeção {ts}\n\n")
        f.write(f"- **backend_mode**: `{summary['backend_mode']}`\n")
        f.write(f"- **throughput_mbps**: `{summary['metrics']['throughput_mbps']}`\n")
        f.write(f"- **rtt** p50/p95/p99 (ms): `{summary['metrics']['rtt_p50_ms']}` / `{summary['metrics']['rtt_p95_ms']}` / `{summary['metrics']['rtt_p99_ms']}`\n")
        f.write(f"- **samples**: `{summary['metrics']['rtt_samples']}`\n\n")
        f.write("## Domínio A (linux tc)\n")
        f.write(f"- backend: `{summary['A']['backend']}`\n")
        f.write(f"- applied: `{summary['A']['applied']}`\n")
        f.write(f"- params: `{summary['A']['params']}`\n\n")
        f.write("## Domínio B (NETCONF)\n")
        f.write(f"- backend: `{summary['B']['backend']}`\n")
        f.write(f"- applied: `{summary['B']['applied']}`\n")
        f.write(f"- connected: `{summary['B']['connected']}`\n")
        f.write(f"- caps_count: `{len(caps)}`\n")
        if caps:
            f.write("<details><summary>capabilities</summary>\n\n```\n")
            for c in caps:
                f.write(c + "\n")
            f.write("```\n\n</details>\n\n")
        f.write("## Domínio C (P4Runtime)\n")
        f.write(f"- backend: `{summary['C']['backend']}`\n")
        f.write(f"- applied: `{summary['C']['applied']}`\n")
        f.write(f"- connected: `{summary['C']['connected']}`\n")
        f.write(f"- pipeline_loaded: `{summary['C']['pipeline_loaded']}`\n")
    return md_path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ts", help="timestamp como 20251105T131619Z; se omitido, pega o último S1_*", default=None)
    args = ap.parse_args()

    ts = args.ts or pick_latest_ts("S1_")
    if not ts:
        print("[erro] Não encontrei TS para S1 em results/json/")
        return

    summary = summarize_s1(ts)
    csv_path = write_csv(ts, summary)
    md_path  = write_md(ts, summary)

    print(json.dumps(summary, indent=2))
    print(f"[ok] CSV: {csv_path}")
    print(f"[ok] MD : {md_path}")

if __name__ == "__main__":
    main()
