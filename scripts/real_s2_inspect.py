#!/usr/bin/env python3
import argparse, json, os, re, glob, datetime as dt
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
RES_JSON = BASE / "results" / "json"
RES_INSPECT = BASE / "results" / "inspect"
RES_INSPECT.mkdir(parents=True, exist_ok=True)

def ts_now():
    try:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except Exception:
        return dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

def pick_latest_ts(prefix="S2_"):
    files = sorted(RES_JSON.glob(f"{prefix}*"), key=os.path.getmtime, reverse=True)
    if not files:
        return None
    for f in files:
        m = re.search(r"(\d{8}T\d{6}Z)", f.name)
        if m:
            return m.group(1)
    return None

def load_json(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def read_domain(ts, dom):
    return load_json(RES_JSON / f"dom_{dom}_{ts}.json")

def find_summary(ts):
    # S2 gera um “sumário” principal S2_<TS>.json
    p = RES_JSON / f"S2_{ts}.json"
    return load_json(p) if p.exists() else None

def safe_get(d, path, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

def summarize_s2(ts):
    a = read_domain(ts, "A") or {}
    b = read_domain(ts, "B") or {}
    c = read_domain(ts, "C") or {}
    summ = find_summary(ts) or {}

    mB = safe_get(summ, ["metrics","B"], {})
    mC = safe_get(summ, ["metrics","C"], {})

    out = {
        "timestamp": ts,
        "scenario": "S2_multicast_source_oriented",
        "backend_mode": safe_get(summ, ["backend_mode"], "unknown"),
        "metrics": {
            "B": {
                "throughput_mbps": mB.get("throughput_mbps"),
                "jitter_ms": mB.get("jitter_ms"),
                "delivery_ratio": mB.get("delivery_ratio"),
                "join_time_s": mB.get("join_time_s"),
                "rtt_p50_ms": mB.get("rtt_p50_ms"),
                "rtt_p95_ms": mB.get("rtt_p95_ms"),
                "rtt_p99_ms": mB.get("rtt_p99_ms"),
                "rtt_samples": mB.get("rtt_samples"),
            },
            "C": {
                "throughput_mbps": mC.get("throughput_mbps"),
                "jitter_ms": mC.get("jitter_ms"),
                "delivery_ratio": mC.get("delivery_ratio"),
                "join_time_s": mC.get("join_time_s"),
                "rtt_p50_ms": mC.get("rtt_p50_ms"),
                "rtt_p95_ms": mC.get("rtt_p95_ms"),
                "rtt_p99_ms": mC.get("rtt_p99_ms"),
                "rtt_samples": mC.get("rtt_samples"),
            },
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

def write_csv(ts, s):
    csv = RES_INSPECT / f"S2_REAL_{ts}.csv"
    caps = s["B"]["capabilities"] or []
    with open(csv, "w", encoding="utf-8") as f:
        f.write(",".join([
            "timestamp","backend_mode",
            "A.backend","A.applied",
            "B.backend","B.applied","B.connected","B.caps_count",
            "C.backend","C.applied","C.connected","C.pipeline_loaded",
            "B.thr","B.jitter","B.deliv","B.join","B.rtt50","B.rtt95","B.rtt99","B.samples",
            "C.thr","C.jitter","C.deliv","C.join","C.rtt50","C.rtt95","C.rtt99","C.samples"
        ])+"\n")
        f.write(",".join(map(str,[
            s["timestamp"], s["backend_mode"],
            s["A"]["backend"], s["A"]["applied"],
            s["B"]["backend"], s["B"]["applied"], s["B"]["connected"] or False, len(caps),
            s["C"]["backend"], s["C"]["applied"], s["C"]["connected"] or False, s["C"]["pipeline_loaded"],
            s["metrics"]["B"]["throughput_mbps"], s["metrics"]["B"]["jitter_ms"], s["metrics"]["B"]["delivery_ratio"], s["metrics"]["B"]["join_time_s"], s["metrics"]["B"]["rtt_p50_ms"], s["metrics"]["B"]["rtt_p95_ms"], s["metrics"]["B"]["rtt_p99_ms"], s["metrics"]["B"]["rtt_samples"],
            s["metrics"]["C"]["throughput_mbps"], s["metrics"]["C"]["jitter_ms"], s["metrics"]["C"]["delivery_ratio"], s["metrics"]["C"]["join_time_s"], s["metrics"]["C"]["rtt_p50_ms"], s["metrics"]["C"]["rtt_p95_ms"], s["metrics"]["C"]["rtt_p99_ms"], s["metrics"]["C"]["rtt_samples"],
        ]))+"\n")
    return csv

def write_md(ts, s):
    md = RES_INSPECT / f"S2_REAL_{ts}.md"
    caps = s["B"]["capabilities"] or []
    with open(md, "w", encoding="utf-8") as f:
        f.write(f"# S2 (real) – inspeção {ts}\n\n")
        f.write(f"- **backend_mode**: `{s['backend_mode']}`\n\n")
        def write_dom(dom):
            f.write(f"## Domínio {dom}\n")
            f.write(f"- backend: `{s[dom]['backend']}`\n")
            f.write(f"- applied: `{s[dom]['applied']}`\n")
            if dom in ("B","C"):
                f.write(f"- connected: `{s[dom]['connected']}`\n")
                if dom == "C":
                    f.write(f"- pipeline_loaded: `{s['C']['pipeline_loaded']}`\n")
                if dom == "B" and caps:
                    f.write(f"- caps_count: `{len(caps)}`\n")
            f.write("\n")
        write_dom("A"); write_dom("B"); write_dom("C")
        f.write("## Métricas (receptores B e C)\n")
        for r in ("B","C"):
            m = s["metrics"][r]
            f.write(f"- {r}: thr={m['throughput_mbps']} Mbps, jitter={m['jitter_ms']} ms, delivery={m['delivery_ratio']}, join={m['join_time_s']} s, rtt50/95/99={m['rtt_p50_ms']}/{m['rtt_p95_ms']}/{m['rtt_p99_ms']} ms, samples={m['rtt_samples']}\n")
        if caps:
            f.write("\n<details><summary>NETCONF capabilities (domínio B)</summary>\n\n```\n")
            for c in caps: f.write(c+"\n")
            f.write("```\n\n</details>\n")
    return md

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ts", help="timestamp S2, ex: 20251105T131619Z", default=None)
    args = ap.parse_args()
    ts = args.ts or pick_latest_ts("S2_")
    if not ts:
        print("[erro] Não encontrei TS para S2 em results/json/")
        return
    s = summarize_s2(ts)
    csv = write_csv(ts, s)
    md  = write_md(ts, s)
    print(json.dumps(s, indent=2))
    print(f"[ok] CSV: {csv}")
    print(f"[ok] MD : {md}")

if __name__ == "__main__":
    main()
