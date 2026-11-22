"""Shut the Kubernetes setup down.

    python scripts/k8s_down.py               # stop the port-forward and pause the cluster; data is kept
    python scripts/k8s_down.py --wipe        # also delete the whole system and all its data
    python scripts/k8s_down.py --keep-cluster  # only stop the port-forward; leave the cluster running

"Pause" (minikube stop) frees the CPU and memory but keeps everything, so the next
k8s_up.py is quick and the stored data is still there. --wipe is for a clean slate.
"""

import argparse
import sys

import _k8s as k


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wipe", action="store_true", help="delete the namespace and all stored data")
    p.add_argument("--keep-cluster", action="store_true", help="leave the minikube cluster running")
    args = p.parse_args()

    try:
        k.step("Stopping the port-forward")
        print("   stopped" if k.stop_forward() else "   it was not running")

        if k.docker_running() and k.minikube_running():
            if args.wipe:
                k.step("Deleting the system and all its stored data")
                k.wipe_data()
            if not args.keep_cluster:
                k.step("Pausing the cluster (minikube stop)")
                k.run("minikube", "stop")
        else:
            print("\n   the cluster is not running: nothing more to stop")
    except k.Fail as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1

    print("\nDone." + ("" if args.wipe else " Everything is kept: run scripts/k8s_up.py to bring it back."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
