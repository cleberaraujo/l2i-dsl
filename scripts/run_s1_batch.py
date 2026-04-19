# scripts/run_s1_batch.py
import subprocess, json, time, pathlib, statistics, glob

SPEC = "specs/valid/s1_unicast_qos.json"
RUNS = 10
DURATION = 20
BE = 30

def main():
    results = []
    for i in range(RUNS):
        print(f"[S1] Run {i+1}/{RUNS} ...")
        cmd = ["sudo","python3","scenarios/multidomain_s1.py","--spec",SPEC,
               "--duration",str(DURATION),"--be-mbps",str(BE)]
        out = subprocess.check_output(cmd, text=True)
        # a última linha impressa é o JSON do resumo
        js = json.loads(out.splitlines()[-1])
        results.append(js)
        time.sleep(1)

    outdir = pathlib.Path("results/agg"); outdir.mkdir(parents=True, exist_ok=True)
    # salva bruto
    pathlib.Path(outdir/"s1_runs.json").write_text(json.dumps(results, indent=2))

    # estatística simples
    thr = [r["metrics"]["throughput_mbps"] for r in results if r["metrics"]["throughput_mbps"] is not None]
    p99 = [r["metrics"]["rtt_p99_ms"] for r in results]
    def ic95(xs):
        n=len(xs); 
        if n<2: return 0.0
        import math
        s=statistics.stdev(xs); return 1.96*s/(n**0.5)
    summary = {
        "scenario":"S1",
        "runs": len(results),
        "thr_mean_mbps": statistics.mean(thr) if thr else None,
        "thr_ic95_mbps": ic95(thr) if thr else None,
        "p99_mean_ms": statistics.mean(p99) if p99 else None,
        "p99_ic95_ms": ic95(p99) if p99 else None
    }
    pathlib.Path(outdir/"s1_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()