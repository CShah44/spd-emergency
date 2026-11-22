"""Autoscaling demo (upgrade task 6): a load spike takes the matchers from 1 to 5 and back to 1.

    python scripts/demo_k8s_keda.py                       # 20 requests per second for 60 s
    python scripts/demo_k8s_keda.py --rate 30 --duration 90

KEDA watches the RabbitMQ queue and sets the number of matching-service pods (about one per
20 waiting requests, between 1 and 5: k8s/keda/matching-scaledobject.yaml). This runs the load
test (loadtest/run.py) as the spike and, next to it, notes every 2 seconds how many requests are
waiting and how many matcher pods there are. Afterwards it checks that:
  * the matchers scaled up while the queue was deep
  * they scaled back down to the minimum once it was empty
  * no request was lost (the load test's own check)

The same lines are on the Grafana dashboard (http://localhost:3000): "Matching pods and the queue".
Needs the Kubernetes stack (scripts/k8s_up.py), which installs KEDA.
"""

import argparse
import math
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from _demo_app import API_URL, check, check_api, morph_banner, ok, queue_depth, step

NAMESPACE = "emergency"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from loadtest.run import seed_responders  # noqa: E402


def kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", "-n", NAMESPACE, *args], capture_output=True, text=True).stdout.strip()


def pods() -> tuple[int, int]:
    """(pods wanted, pods ready) for the matching service."""
    wanted = kubectl("get", "deploy", "matching-service", "-o", "jsonpath={.spec.replicas}")
    ready = kubectl("get", "deploy", "matching-service", "-o", "jsonpath={.status.readyReplicas}")
    return int(wanted or 0), int(ready or 0)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rate", type=float, default=20, help="requests per second during the spike (default 20)")
    p.add_argument("--duration", type=int, default=60, help="seconds of load (default 60)")
    p.add_argument("--settle", type=int, default=420, help="most seconds to wait afterwards for the pods to go back down")
    args = p.parse_args()
    check_api()
    morph_banner("Autoscaling: matching-service on queue depth (KEDA)")

    if not kubectl("get", "scaledobject", "matching-service"):
        sys.exit("No KEDA ScaledObject 'matching-service' found. Run scripts/k8s_up.py (it installs KEDA), "
                 "or: kubectl apply -f k8s/keda/")
    minimum = int(kubectl("get", "scaledobject", "matching-service", "-o", "jsonpath={.spec.minReplicaCount}") or 1)
    maximum = int(kubectl("get", "scaledobject", "matching-service", "-o", "jsonpath={.spec.maxReplicaCount}") or 5)

    step(f"BEFORE: wait until the matchers are at the minimum ({minimum})")
    deadline = time.monotonic() + 300
    while pods()[0] != minimum and time.monotonic() < deadline:
        time.sleep(3)
    ok(f"{pods()[1]} matcher pod(s) ready, queue depth {queue_depth()}")

    responders = math.ceil(args.rate * args.duration * 1.5)
    step(f"Registering {responders} responders, so the requests have someone to be matched with")
    seed_responders(API_URL, responders, random.Random())

    step(f"THE SPIKE: {args.rate:g} requests/s for {args.duration} s (loadtest/run.py)")
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    load = subprocess.Popen(
        [sys.executable, str(ROOT / "loadtest" / "run.py"), "--rate", str(args.rate), "--duration", str(args.duration),
         "--label", "keda-spike", "--skip-seed"],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
    )

    started = time.monotonic()
    timeline: list[tuple[float, int, int]] = []  # (seconds, queue depth, pods wanted)
    scaled_up_at = back_down_at = None
    peak_queue = peak_pods = 0
    load_over_at = None
    while True:
        wanted, ready = pods()
        depth = queue_depth() or 0
        now = time.monotonic() - started
        timeline.append((now, depth, wanted))
        peak_queue, peak_pods = max(peak_queue, depth), max(peak_pods, wanted)
        if wanted > minimum and scaled_up_at is None:
            scaled_up_at = now
        if load.poll() is not None and load_over_at is None:
            load_over_at = now
        if scaled_up_at is not None and wanted == minimum and depth == 0 and back_down_at is None:
            back_down_at = now
        if load_over_at is not None and (back_down_at is not None or now - load_over_at > args.settle):
            break
        time.sleep(2)

    step("Timeline, seconds since the spike started  [queue depth | matcher pods]")
    for t, depth, wanted in timeline[::3]:
        print(f"   {t:5.0f}s  queue {depth:5d}  pods {wanted}  " + "#" * wanted + "." * (maximum - wanted)
              + "  " + "=" * min(40, depth // 10))

    step("Result")
    good = check(peak_pods > minimum, f"scaled up: {minimum} -> {peak_pods} pods (queue peaked at {peak_queue} waiting)")
    if scaled_up_at is not None:
        ok(f"the first extra pod was wanted {scaled_up_at:.0f} s after the spike started")
    good &= check(back_down_at is not None, "scaled back down to " + str(minimum) + " pod(s) once the queue was empty"
                  + (f" (after {back_down_at:.0f} s in all)" if back_down_at else f" (still not after {args.settle} s)"))
    log.seek(0)
    output = log.read()
    lost_free = load.returncode == 0
    good &= check(lost_free, "no request lost or double-assigned (loadtest/run.py verified every one)")
    for line in output.splitlines():
        if line.startswith("| keda-spike"):
            print("   " + line)
    if not lost_free:
        print(output[-1500:])
    print()
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
