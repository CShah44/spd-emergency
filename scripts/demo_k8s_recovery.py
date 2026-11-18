"""Spec demo (Kubernetes recovery): kill a running application pod and watch Kubernetes
bring it back, while the system keeps working.

    python scripts/demo_k8s_recovery.py                        # kills a matching-service pod
    python scripts/demo_k8s_recovery.py --app request-service

Needs the Kubernetes stack running and scripts/k8s_forward.py in another terminal
(the demo sends its requests through localhost:8080). While the pod is down, the
other replica keeps serving: requests sent during the failure are still processed.
"""

import argparse
import subprocess
import sys
import time

from _demo_app import (
    PORT_BLAIR, check, check_api, create_request, jitter, morph_banner, ok, register, retire_leftovers, step,
    wait_for, wait_until_processed,
)

NAMESPACE = "emergency"


def kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", "-n", NAMESPACE, *args], check=True, capture_output=True, text=True).stdout


def pod_names(app: str) -> list[str]:
    return kubectl("get", "pods", "-l", f"app={app}", "-o", "name").split()


def show_pods(app: str) -> None:
    print("   " + kubectl("get", "pods", "-l", f"app={app}").rstrip().replace("\n", "\n   "))


def ready_replicas(app: str) -> int:
    return int(kubectl("get", "deploy", app, "-o", "jsonpath={.status.readyReplicas}") or 0)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--app", choices=["matching-service", "request-service"], default="matching-service")
    args = p.parse_args()
    check_api()

    morph_banner(f"Kubernetes recovery: {args.app}")
    retire_leftovers()
    desired = int(kubectl("get", "deploy", args.app, "-o", "jsonpath={.spec.replicas}"))
    for _ in range(20):
        register("water", "Water unit", jitter(PORT_BLAIR))

    step(f"BEFORE: {desired} pods of {args.app}")
    show_pods(args.app)
    victim = pod_names(args.app)[0]

    step(f"DURING: deleting {victim} and sending 10 requests while Kubernetes replaces it")
    kubectl("delete", victim, "--wait=false")
    sent, refused = [], 0
    for _ in range(10):
        try:
            sent.append(create_request("water", jitter(PORT_BLAIR))["id"])
        except Exception:  # noqa: BLE001  (the port-forward may drop for a moment if it pointed at the deleted pod)
            refused += 1
            time.sleep(1)
        time.sleep(0.3)
    ok(f"{len(sent)} requests accepted, {refused} refused while the pod was down")

    step("AFTER: waiting for Kubernetes to restore the pods")
    took = wait_for(lambda: ready_replicas(args.app) == desired and victim not in pod_names(args.app), 120)
    good = check(took is not None, f"{desired} pods ready again, {victim} replaced" + (f" (after {took:.1f}s)" if took else ""))
    show_pods(args.app)
    good &= check(wait_until_processed(sent, 60) is not None, "every request sent during the failure was processed")

    print()
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
