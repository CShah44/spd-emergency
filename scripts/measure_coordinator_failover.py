"""Measure coordinator failover on Kubernetes (roadmap task 3).

    python scripts/measure_coordinator_failover.py delete   # clean: kubectl delete pod (lease released)
    python scripts/measure_coordinator_failover.py kill     # crash: docker kill the leader's container
    python scripts/measure_coordinator_failover.py freeze   # freeze 10 s (docker pause), then resume

A writer runs INSIDE the cluster (in a request-service pod, so it uses the
coordinator Service like the real services do) and writes a new record every
50 ms. Five seconds in, the leading coordinator is failed. At the end the
writer reads back every write that was acknowledged (200).

Reports:
  outage       the longest gap between two successful writes, as a client sees it
  lost writes  acknowledged writes that can't be read back afterwards (must be 0)
  failed       writes answered with an error or not answered (the client knows:
               they were never acknowledged, so a real client retries them)

`kill` and `freeze` need Docker Desktop's Kubernetes (pods are Docker
containers there); `delete` works on any cluster.
"""

import argparse
import json
import subprocess
import sys
import threading
import time

NS = "emergency"

# Runs inside the cluster: python - <seconds> <run_id>
PROBE = r'''
import httpx, json, sys, time
seconds, run = float(sys.argv[1]), sys.argv[2]
http = httpx.Client(base_url="http://coordinator:8000", timeout=2.0)
log, acked, i = [], [], 0
start = time.monotonic()
print("START", flush=True)
while time.monotonic() - start < seconds:
    key = f"probe:failover-{run}-{i}"  # not "resource:"/"request:": the app never sees these
    t = time.monotonic() - start
    try:
        r = http.put(f"/store/{key}", json={"id": key, "i": i, "probe": True})
        ok = r.status_code == 200
        status = r.status_code
    except httpx.HTTPError as e:
        ok, status = False, type(e).__name__
    log.append((round(t, 3), ok, status))
    if ok:
        acked.append((key, i))
    i += 1
    time.sleep(0.05)
oks = [t for t, ok, _ in log if ok]
gaps = [(b - a, a) for a, b in zip(oks, oks[1:])]
outage, outage_at = max(gaps) if gaps else (seconds, 0.0)
lost = []
for key, n in acked:
    for _ in range(20):  # the cluster may still be settling: retry reads a little
        try:
            r = http.get(f"/store/{key}")
            if r.status_code == 200:
                if r.json().get("i") != n:
                    lost.append(key)
                break
            if r.status_code == 404:
                lost.append(key)
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    else:
        lost.append(key)
errors = {}
for _, ok, status in log:
    if not ok:
        errors[str(status)] = errors.get(str(status), 0) + 1
print(json.dumps({"writes": len(log), "acked": len(acked), "failed": len(log) - len(acked),
                  "lost": len(lost), "lost_keys": lost[:5], "outage_s": round(outage, 2),
                  "outage_started_at_s": outage_at, "errors": errors}), flush=True)
'''


def sh(*cmd: str, check: bool = True) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"failed: {' '.join(cmd)}\n{r.stderr}")
    return r.stdout.strip()


def lease() -> tuple[str, int]:
    """(leader's pod name, epoch). The holder is "<pod>_<process suffix>"."""
    out = sh("kubectl", "-n", NS, "get", "lease", "coordinator-leader",
             "-o", "jsonpath={.spec.holderIdentity} {.spec.leaseTransitions}")
    holder, transitions = (out.split() + ["0"])[:2]
    return holder.rsplit("_", 1)[0], int(transitions or 0)


def container_of(pod: str) -> str:
    cid = sh("docker", "ps", "-q", "--filter", f"name=k8s_coordinator_{pod}_")
    if not cid:
        raise SystemExit(f"no Docker container for {pod}: 'kill' and 'freeze' need Docker Desktop's Kubernetes")
    return cid.splitlines()[0]


def fail(how: str, pod: str) -> None:
    if how == "delete":
        sh("kubectl", "-n", NS, "delete", "pod", pod, "--wait=false")
    elif how == "kill":
        sh("docker", "kill", container_of(pod))
    elif how == "freeze":
        cid = container_of(pod)
        sh("docker", "pause", cid)
        time.sleep(10)
        sh("docker", "unpause", cid)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("how", choices=["delete", "kill", "freeze"])
    p.add_argument("--seconds", type=float, default=35.0, help="how long the writer runs")
    args = p.parse_args()

    leader, epoch = lease()
    print(f"leader before: {leader} (epoch {epoch})")
    run = f"{args.how}-{int(time.time())}"
    probe = subprocess.Popen(
        ["kubectl", "-n", NS, "exec", "-i", "deploy/request-service", "--", "python", "-", str(args.seconds), run],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    probe.stdin.write(PROBE)
    probe.stdin.close()
    assert probe.stdout.readline().strip() == "START", "the writer did not start"
    time.sleep(5)
    print(f"t=5s: {args.how} {leader}", flush=True)
    failer = threading.Thread(target=fail, args=(args.how, leader))
    failer.start()
    result = json.loads(probe.stdout.read().strip().splitlines()[-1])
    failer.join()
    probe.wait()

    new_leader, new_epoch = lease()
    print(f"leader after:  {new_leader} (epoch {new_epoch})")
    print(json.dumps(result, indent=1))
    print(f"\n| {args.how} | {result['outage_s']} s | {result['acked']} of {result['writes']} acknowledged "
          f"| {result['lost']} lost | {leader[-5:]} -> {new_leader[-5:]}, epoch {epoch} -> {new_epoch} |")
    return 0 if result["lost"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
