"""Spec demo (Kubernetes scaling): add replicas of a microservice and show the work is
shared and the system stays available.

    python scripts/demo_k8s_scaling.py                          # matching-service 2 -> 4
    python scripts/demo_k8s_scaling.py --replicas 6 --requests 200
    python scripts/demo_k8s_scaling.py --restore                # scale back to the original count at the end

A batch of requests goes through RabbitMQ; the matching pods' logs then show how
many of those requests each pod handled. Needs the Kubernetes stack running and
scripts/k8s_forward.py in another terminal.
"""

import argparse
import re
import subprocess
import sys
import time

from _demo_app import check, check_api, describe_queue, morph_banner, ok, retire_leftovers, step, submit_batch, \
    wait_for, wait_until_processed

NAMESPACE = "emergency"
APP = "matching-service"


def kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", "-n", NAMESPACE, *args], check=True, capture_output=True, text=True).stdout


def replicas() -> tuple[int, int]:
    """(desired, ready)"""
    desired = int(kubectl("get", "deploy", APP, "-o", "jsonpath={.spec.replicas}"))
    return desired, int(kubectl("get", "deploy", APP, "-o", "jsonpath={.status.readyReplicas}") or 0)


def handled_per_pod(request_ids: set[str]) -> dict[str, int]:
    """How many of OUR requests each matching pod processed, counted from the pod logs
    (the consumer logs one line per message: '... consumer INFO req-xxxx: matched')."""
    logs = kubectl("logs", "-l", f"app={APP}", "--prefix", "--tail=-1", "--max-log-requests=20")
    counts: dict[str, int] = {}
    for pod, request_id in re.findall(r"^\[pod/([^/\]]+)/[^\]]+\].* (req-[0-9a-f]{16}): (?:matched|unmatched)", logs, re.M):
        if request_id in request_ids:
            counts[pod] = counts.get(pod, 0) + 1
    return counts


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--replicas", type=int, default=4, help="matching-service replicas to scale to (default 4)")
    p.add_argument("--requests", type=int, default=100)
    p.add_argument("--restore", action="store_true", help="scale back to the original count at the end")
    args = p.parse_args()
    check_api()

    morph_banner("Kubernetes scaling: matching-service")
    retire_leftovers()
    original, _ = replicas()

    step(f"BEFORE: {original} replicas of {APP}")
    print("   " + kubectl("get", "pods", "-l", f"app={APP}").rstrip().replace("\n", "\n   "))

    step(f"SCALING to {args.replicas} replicas")
    kubectl("scale", f"deploy/{APP}", f"--replicas={args.replicas}")
    took = wait_for(lambda: replicas() == (args.replicas, args.replicas), 120)
    good = check(took is not None, f"{args.replicas} pods ready" + (f" after {took:.1f}s" if took else ""))
    print("   " + kubectl("get", "pods", "-l", f"app={APP}").rstrip().replace("\n", "\n   "))
    time.sleep(3)  # let every new pod's consumer connect to RabbitMQ before the batch arrives

    step(f"Sending {args.requests} requests; RabbitMQ hands each free pod the next one")
    _, request_ids, sent_in = submit_batch(args.requests, args.requests)
    ok(f"{args.requests} requests accepted in {sent_in:.2f}s.  {describe_queue()}")
    drained = wait_until_processed(request_ids, 120)
    good &= check(drained is not None, "every request was processed" + (f" ({drained:.1f}s after submission)" if drained else ""))

    step("Who did the work (from each pod's logs)")
    counts = handled_per_pod(set(request_ids))
    for pod, n in sorted(counts.items()):
        print(f"   {pod:<34} handled {n:>4} requests  " + "#" * round(40 * n / args.requests))
    good &= check(len(counts) > 1, f"{len(counts)} different pods shared the work")

    if args.restore:
        step(f"RESTORING {original} replicas")
        kubectl("scale", f"deploy/{APP}", f"--replicas={original}")

    print()
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
