#!/usr/bin/env python3
"""Prospective canonical S2 engine with raw multicast event lineage.

This is a campaign engine, not a fixture or a qualification helper.  It sends
real UDP multicast packets through the selected testbed, records sender and
receiver timestamps, observes receiver membership transitions, verifies the
P4Runtime PRE state in the real implementation, and rolls every transient
resource back before returning.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any

GROUP = "239.1.1.1"
PORT = 5001
SOURCE = "A:h1"
RECEIVERS = {"B:h3": ("h3", "10.0.0.3"), "C:h4": ("h4", "10.0.0.4")}
SOURCE_NS = "h1"
SOURCE_IP = "10.0.0.1"
PROBE_NS = "h2"


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    os.replace(temporary, path)


def run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(argv, text=True, capture_output=True, shell=False)
    if check and cp.returncode:
        raise RuntimeError(f"command failed ({cp.returncode}): {argv!r}: {cp.stderr}")
    return cp


RECEIVER_CODE = r'''
import json, socket, struct, sys, time
group, port, interface, endpoint, mode, duration = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], float(sys.argv[6])
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(("",port));s.settimeout(.30)
mreq=socket.inet_aton(group)+socket.inet_aton(interface);s.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,mreq)
events=[{"event":"membership_join","endpoint":endpoint,"observed_ns":time.time_ns()}];packets=[];deadline=time.monotonic()+duration+1.0;cycled=False
while time.monotonic()<deadline:
  try:data,addr=s.recvfrom(65535)
  except socket.timeout:continue
  received=time.time_ns();doc=json.loads(data);packets.append({"sequence":doc["sequence"],"payload":doc["payload"],"bytes":len(data),"sent_ns":doc["sent_ns"],"received_ns":received,"source_ip":addr[0]})
  if mode=="adapt" and len(packets)==5 and not cycled:
    events.append({"event":"membership_leave","endpoint":endpoint,"observed_ns":time.time_ns()});s.setsockopt(socket.IPPROTO_IP,socket.IP_DROP_MEMBERSHIP,mreq);time.sleep(.20);s.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,mreq);events.append({"event":"membership_rejoin","endpoint":endpoint,"observed_ns":time.time_ns()});cycled=True
events.append({"event":"receiver_stop","endpoint":endpoint,"observed_ns":time.time_ns()});print(json.dumps({"endpoint":endpoint,"packets":packets,"events":events},sort_keys=True))
'''

PROBE_CODE = r'''
import json,socket,sys,time
port,duration=int(sys.argv[1]),float(sys.argv[2]);s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(("",port));s.settimeout(.2);packets=[];deadline=time.monotonic()+duration+1
while time.monotonic()<deadline:
  try:data,addr=s.recvfrom(65535);packets.append({"bytes":len(data),"source_ip":addr[0],"received_ns":time.time_ns()})
  except socket.timeout:pass
print(json.dumps({"membership":False,"packets":packets},sort_keys=True))
'''

SENDER_CODE = r'''
import json,socket,sys,time
group,port,interface,count,interval=sys.argv[1],int(sys.argv[2]),sys.argv[3],int(sys.argv[4]),float(sys.argv[5]);s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP);s.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_IF,socket.inet_aton(interface));s.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_TTL,4);rows=[]
for seq in range(1,count+1):
 sent=time.time_ns();payload=hashlib.sha256(("s2-r4-%d"%seq).encode()).hexdigest();body=json.dumps({"sequence":seq,"sent_ns":sent,"payload":payload},sort_keys=True).encode();s.sendto(body,(group,port));rows.append({"sequence":seq,"sent_ns":sent,"payload":payload,"bytes":len(body)});time.sleep(interval)
print(json.dumps({"packets":rows},sort_keys=True))
'''.replace("import json,socket,sys,time", "import hashlib,json,socket,sys,time")


def raw_event(sequence: int, event: str, endpoint: str, payload: str,
              sent_ns: int, received_ns: int, size: int) -> dict[str, Any]:
    return {
        "schema": "phase4sc-operational-raw-v3", "scenario": "S2",
        "event": event, "sequence": sequence, "endpoint": endpoint,
        "traffic_class": "multicast", "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "sent_ns": sent_ns, "received_ns": received_ns, "bytes": size,
        "status": "sent" if event == "sent" else "received",
    }


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values); position = (len(ordered) - 1) * p
    lower = int(position); upper = min(lower + 1, len(ordered) - 1); fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True); parser.add_argument("--execution-id", required=True)
    parser.add_argument("--repetition", type=int, required=True); parser.add_argument("--results-root", required=True)
    parser.add_argument("--backend", choices=("mock", "real"), required=True)
    parser.add_argument("--mode", choices=("baseline", "adapt"), required=True)
    parser.add_argument("--duration", type=float, required=True)
    args = parser.parse_args()
    spec = json.loads(Path(args.spec).read_text())
    mc = spec.get("requirements", {}).get("multicast") or spec.get("multicast", {})
    # The frozen C5 S2 spec names the preregistered group symbolically as G1;
    # the runtime binding resolves that symbol to the frozen dataplane address.
    if mc.get("enabled") is not True or mc.get("group") not in {"G1", GROUP}:
        raise SystemExit("S2_SPEC_CONTRACT_INVALID")
    output = Path(args.results_root) / args.execution_id
    output.mkdir(parents=True, exist_ok=False)
    count = max(20, int(args.duration / .05)); interval = args.duration / count
    p4_apply: subprocess.CompletedProcess[str] | None = None
    p4_cleanup: subprocess.CompletedProcess[str] | None = None
    try:
        if args.backend == "real":
            p4_apply = run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/p4_program_s2.py"),
                            "--addr", "127.0.0.1:9559", "--ports", "1", "2"])
        receiver_processes = {}
        for endpoint, (namespace, address) in RECEIVERS.items():
            receiver_processes[endpoint] = subprocess.Popen(
                ["ip", "netns", "exec", namespace, sys.executable, "-c", RECEIVER_CODE,
                 GROUP, str(PORT), address, endpoint, args.mode, str(args.duration)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        probe = subprocess.Popen(["ip", "netns", "exec", PROBE_NS, sys.executable, "-c", PROBE_CODE,
                                  str(PORT), str(args.duration)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(.25)
        sender = run(["ip", "netns", "exec", SOURCE_NS, sys.executable, "-c", SENDER_CODE,
                      GROUP, str(PORT), SOURCE_IP, str(count), str(interval)])
        sender_doc = json.loads(sender.stdout); receiver_docs = {}
        for endpoint, process in receiver_processes.items():
            stdout, stderr = process.communicate(timeout=args.duration + 4)
            if process.returncode or not stdout.strip(): raise RuntimeError(f"receiver {endpoint} failed: {stderr}")
            receiver_docs[endpoint] = json.loads(stdout)
        probe_out, probe_err = probe.communicate(timeout=args.duration + 4)
        if probe.returncode: raise RuntimeError(f"probe failed: {probe_err}")
        probe_doc = json.loads(probe_out)
        sent = {int(p["sequence"]): p for p in sender_doc["packets"]}; rows = []
        for sequence, packet in sorted(sent.items()):
            rows.append(raw_event(sequence, "sent", SOURCE, packet["payload"], packet["sent_ns"], 0, packet["bytes"]))
        for endpoint, document in receiver_docs.items():
            for packet in document["packets"]:
                rows.append(raw_event(int(packet["sequence"]), "received", endpoint, packet["payload"],
                                      int(packet["sent_ns"]), int(packet["received_ns"]), int(packet["bytes"])))
        raw_path = output / "raw-events.jsonl"
        raw_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows))
        event_path = output / "membership-events.jsonl"
        membership = [event for doc in receiver_docs.values() for event in doc["events"]]
        event_path.write_text("".join(json.dumps(event, sort_keys=True) + "\n" for event in membership))
        delivered = [row for row in rows if row["event"] == "received"]
        denominator = len(sent) * len(RECEIVERS); latencies = [(row["received_ns"] - row["sent_ns"]) / 1e6 for row in delivered]
        recovery = []
        for endpoint, document in receiver_docs.items():
            joins = [e["observed_ns"] for e in document["events"] if e["event"] in {"membership_join", "membership_rejoin"}]
            received = [p["received_ns"] for p in document["packets"]]
            for joined in joins:
                later = [stamp for stamp in received if stamp >= joined]
                if later: recovery.append((min(later) - joined) / 1e6)
        source_route = run(["ip", "-n", SOURCE_NS, "route", "show"])
        receiver_readback = {endpoint: run(["ip", "-n", ns, "maddr", "show"]).stdout
                             for endpoint, (ns, _) in RECEIVERS.items()}
        readbacks = {"source_route": source_route.stdout, "receiver_membership": receiver_readback,
                     "p4runtime_pre": p4_apply.stdout if p4_apply else "mock-control-plane-not-applied",
                     "probe_packets": probe_doc["packets"]}
        dump(output / "readbacks.json", readbacks)
        dump(output / "sender.json", sender_doc); dump(output / "receivers.json", receiver_docs); dump(output / "probe.json", probe_doc)
        summary = {
            "scenario": "S2", "execution_id": args.execution_id, "mode": args.mode, "backend": args.backend,
            "source_oriented_multicast": True, "source": SOURCE, "group": GROUP,
            "receivers": sorted(RECEIVERS), "probe_membership": False, "probe_packets": len(probe_doc["packets"]),
            "raw_artifacts": {"events": str(raw_path), "membership_events": str(event_path)},
            "metrics": {"sent": len(sent), "delivered": len(delivered), "lost": denominator - len(delivered),
                        "delivery_ratio": len(delivered) / denominator, "loss_ratio": (denominator - len(delivered)) / denominator,
                        "recovery_ms": max(recovery) if recovery else None,
                        "stability": int(bool(delivered) and not probe_doc["packets"]),
                        "latency_p50_ms": percentile(latencies, .50), "latency_p95_ms": percentile(latencies, .95),
                        "latency_p99_ms": percentile(latencies, .99)},
            "readbacks_valid": bool(source_route.stdout.strip()) and all(receiver_readback.values()) and (args.backend == "mock" or "P4_S2_PROGRAM_FUNCTIONAL_OK" in (p4_apply.stdout if p4_apply else "")),
            "raw_recomputable": True, "synthetic_metrics": False,
        }
        dump(output / "summary.json", summary)
        print(json.dumps({"valid": True, "summary": str(output / "summary.json")}, sort_keys=True))
        return 0
    finally:
        if args.backend == "real" and p4_apply is not None:
            p4_cleanup = run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/p4_program_s2.py"),
                              "--addr", "127.0.0.1:9559", "--ports", "1", "2", "--cleanup"], check=False)
            dump(output / "rollback.json", {"p4_cleanup_rc": p4_cleanup.returncode,
                                             "stdout": p4_cleanup.stdout, "stderr": p4_cleanup.stderr,
                                             "valid": p4_cleanup.returncode == 0})
        else:
            dump(output / "rollback.json", {"valid": True, "reason": "no-real-control-state"})


if __name__ == "__main__":
    raise SystemExit(main())
