#!/usr/bin/env python3
import argparse, subprocess, json, csv, datetime, os, glob, re
from pathlib import Path

# ---------- util ----------
def utc_ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def run(cmd, capture=True, check=False):
    p = subprocess.run(cmd,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.STDOUT if capture else None,
                       text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"cmd failed: {' '.join(cmd)}\nRC={p.returncode}\nOUT:\n{p.stdout or ''}")
    return p

def extract_last_json_block(s: str):
    last_open = s.rfind("{")
    if last_open == -1:
        return None
    for start in range(last_open, -1, -1):
        if s[start] != "{":
            continue
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    chunk = s[start:i+1]
                    try:
                        return json.loads(chunk)
                    except Exception:
                        break
    return None

def latest_summary_from_disk():
    paths = sorted(Path("results/json").glob("S2_*.json"), key=os.path.getmtime, reverse=True)
    for p in paths:
        try:
            return json.loads(p.read_text(encoding="utf-8")), str(p)
        except Exception:
            continue
    return None, None

def run_and_get_summary(cmd):
    """
    Executa o cenário S2 e retorna (json_summary, summary_path_ou_hint).

    ORDEM (ajustada):
    1) Após a execução, procura o S2_*.json MAIS RECENTE em results/json (preferência).
    2) Se não achar, tenta a linha "Summary saved to <path>" no stdout.
    3) Se não achar, tenta extrair o último bloco JSON do stdout.
    4) Como último recurso, tenta novamente o arquivo mais recente em disco.
    """
    p = run(cmd, capture=True, check=False)
    out = p.stdout or ""

    # 1) Preferir arquivo em disco (o cenário costuma sempre salvar)
    data, path = latest_summary_from_disk()
    if data is not None:
        return data, path

    # 2) Linha com caminho explícito (se existir nesta sua versão)
    summary_path = None
    for line in out.splitlines():
        if "Summary saved to " in line:
            summary_path = line.split("Summary saved to ",1)[1].strip()
            break
    if summary_path:
        data = json.loads(Path(summary_path).read_text(encoding="utf-8"))
        return data, summary_path

    # 3) Último bloco JSON do stdout
    data = extract_last_json_block(out)
    if data is not None:
        return data, "<captured-from-stdout>"

    # 4) Última tentativa em disco
    data, path = latest_summary_from_disk()
    if data is not None:
        return data, path

    raise RuntimeError("Não foi possível localizar o summary (arquivo ou bloco JSON no stdout).\n" + out)

# ---------- limpeza entre rodadas ----------
def cleanup_testbed():
    cmds = [
        ["sudo","ip","netns","del","h1"],
        ["sudo","ip","netns","del","h3"],
        ["sudo","ip","netns","del","h5"],
        ["sudo","ip","link","del","brA"],
        ["sudo","ip","link","del","brB"],
        ["sudo","ip","link","del","brC"],
        ["sudo","ip","link","del","A-B"],
        ["sudo","ip","link","del","B-A"],
        ["sudo","ip","link","del","A-C"],
        ["sudo","ip","link","del","C-A"],
        ["sudo","ip","link","del","B-C"],
        ["sudo","ip","link","del","C-B"],
        ["sudo","ip","link","del","tapA1"],
        ["sudo","ip","link","del","tapB3"],
        ["sudo","ip","link","del","tapC5"],
    ]
    for c in cmds:
        try:
            run(c, capture=True, check=False)
        except Exception:
            pass

# ---------- comparação ----------
def fmt_or_blank(val, fmt):
    try:
        if val is None:
            return ""
        return fmt.format(float(val))
    except Exception:
        return "" if val in (None, "") else str(val)

def add_metric(rows, receptor, mb, ma, metric, fmt):
    vb = mb.get(metric, None)
    va = ma.get(metric, None)
    sb = fmt_or_blank(vb, fmt)
    sa = fmt_or_blank(va, fmt)
    try:
        db = None if vb is None else float(vb)
        da = None if va is None else float(va)
        sd = "" if (db is None or da is None) else fmt.format(da - db)
    except Exception:
        sd = ""
    rows.append([receptor, metric, sb, sa, sd])

def main():
    ap = argparse.ArgumentParser(description="Comparador S2 baseline vs adapt")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--be-mbps", type=float, default=30.0)
    ap.add_argument("--bwA", type=int, default=100)
    ap.add_argument("--bwB", type=int, default=50)
    ap.add_argument("--bwC", type=int, default=100)
    ap.add_argument("--delay-ms", type=int, default=1)
    ap.add_argument("--no-cleanup-between", action="store_true",
                    help="Não executar limpeza entre baseline e adapt (por padrão limpa).")
    args = ap.parse_args()

    ts = utc_ts()

    base_cmd = ["sudo", "/usr/bin/python3", "-m", "scenarios.multicast_s2",
                "--spec", args.spec,
                "--duration", str(args.duration),
                "--be-mbps", str(args.be_mbps),
                "--bwA", str(args.bwA), "--bwB", str(args.bwB), "--bwC", str(args.bwC),
                "--delay-ms", str(args.delay_ms)]

    print("[S2] baseline…")
    j_base, path_base = run_and_get_summary(base_cmd + ["--mode", "baseline"])

    if not args.no_cleanup_between:
        print("[S2] limpeza intermediária…")
        cleanup_testbed()

    print("[S2] adapt…")
    j_adpt, path_adpt = run_and_get_summary(base_cmd + ["--mode", "adapt"])

    comp_dir = Path("results/compare")
    comp_dir.mkdir(parents=True, exist_ok=True)
    csv_path = comp_dir / f"CMP_S2_{ts}.csv"
    md_path  = comp_dir / f"CMP_S2_{ts}.md"

    headers = ["receptor","metric","baseline","adapt","delta"]
    rows = []
    for r in ["B","C"]:
        # caminho canônico conforme scenarios.multicast_s2.py
        mb = j_base.get("metrics",{}).get(r,{})
        ma = j_adpt.get("metrics",{}).get(r,{})
        add_metric(rows, r, mb, ma, "delivery_ratio",  "{:.3f}")
        add_metric(rows, r, mb, ma, "throughput_mbps", "{:.3f}")
        add_metric(rows, r, mb, ma, "jitter_ms",       "{:.3f}")
        add_metric(rows, r, mb, ma, "join_time_s",     "{:.3f}")
        add_metric(rows, r, mb, ma, "rtt_p50_ms",      "{:.1f}")
        add_metric(rows, r, mb, ma, "rtt_p95_ms",      "{:.1f}")
        add_metric(rows, r, mb, ma, "rtt_p99_ms",      "{:.1f}")
        rows.append([])

    # CSV
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        for row in rows:
            if row:
                w.writerow(row)
            else:
                w.writerow([])

    # MD
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# S2 baseline vs adapt ({ts})\n\n")
        f.write(f"- baseline: `{path_base}`\n- adapt: `{path_adpt}`\n\n")
        f.write("| receptor | metric | baseline | adapt | Δ |\n")
        f.write("|---|---|---:|---:|---:|\n")
        for row in rows:
            if row:
                f.write(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} |\n")
            else:
                f.write("|  |  |  |  |  |\n")

    print(f"[S2] Comparativo salvo em:\n - {csv_path}\n - {md_path}")

if __name__ == "__main__":
    main()
