"""Start the whole system on Kubernetes with one command.

    python scripts/k8s_up.py                 # build, start the cluster, deploy, forward ports
    python scripts/k8s_up.py --skip-build    # reuse the images from last time (faster)
    python scripts/k8s_up.py --seed          # also register sample responders
    python scripts/k8s_up.py --fresh --seed  # throw away all old data first: a clean demo start

It does what you would otherwise type by hand:
  1. check Docker, kubectl and minikube are there, and the local ports are free
  2. build the five service images and pull RabbitMQ
  3. start minikube (a Kubernetes cluster that runs inside one Docker container)
  4. (--fresh only) delete the old system and its stored data
  5. load the images into it and `kubectl apply -k k8s`
  6. wait until every pod is ready and all six storage nodes are UP
  7. start the port-forward in the background (the same ports as Docker Compose)

Safe to run again: whatever is already done is skipped. Stop with scripts/k8s_down.py.
"""

import argparse
import subprocess
import sys
import time

import _k8s as k


def check_prerequisites() -> None:
    k.step("Checking prerequisites")
    for tool in ("docker", "kubectl", "minikube"):
        k.need(tool)
    if not k.docker_running():
        raise k.Fail("Docker is not running: start Docker Desktop and wait until it says 'Engine running'")
    if k.forward_running():
        print("   an earlier port-forward is still running: restarting it")
        k.stop_forward()
    busy = [p for p in k.FORWARDED_PORTS if k.port_in_use(p)]
    if busy:
        raise k.Fail(
            f"local ports already in use: {busy}. Is Docker Compose still running? "
            "Stop it with: docker compose down"
        )
    print("   ok")


def build_images() -> None:
    k.step("Building the service images (docker compose build)")
    k.run("docker", "compose", "build")
    k.run("docker", "pull", k.RABBITMQ_IMAGE, quiet=True)


def start_cluster() -> None:
    k.step("Starting the Kubernetes cluster (minikube, inside a Docker container)")
    if k.minikube_running():
        print("   already running")
        return
    k.run("minikube", "start", "--driver=docker", "--cpus=4", "--memory=4096")


def load_images() -> None:
    k.step("Loading the images into the cluster")
    for image in k.BUILT_IMAGES + [k.RABBITMQ_IMAGE]:
        k.run("minikube", "image", "load", image)
        print(f"   {image}")


def deploy(rebuilt: bool) -> None:
    k.step("Deploying (kubectl apply -k k8s)")
    existed = k.kubectl("get", "deploy", "request-service", check=False, quiet=True).returncode == 0
    # RabbitMQ used to be a Deployment; it is a StatefulSet now (it needs a disk). Remove the
    # old one so a cluster created before that change doesn't end up with two brokers.
    k.kubectl("delete", "deploy", "rabbitmq", "--ignore-not-found", quiet=True)
    k.run("kubectl", "apply", "-k", "k8s")
    if existed and rebuilt:
        # Same image tag, new content: Kubernetes won't notice, so restart our own pods.
        print("   restarting the service pods to pick up the rebuilt images", flush=True)
        k.kubectl("rollout", "restart", *(f"deploy/{d}" for d in k.DEPLOYMENTS), "statefulset/shard0",
                  "statefulset/shard1", "statefulset/shard2")


def wait_ready() -> None:
    k.step("Waiting for every pod to be ready")
    for kind, names in (("statefulset", k.STATEFULSETS), ("deploy", k.DEPLOYMENTS)):
        for name in names:
            k.kubectl("rollout", "status", f"{kind}/{name}", "--timeout=300s", quiet=True)
            print(f"   {name}")


def wait_cluster_healthy(timeout_s: float = 90.0) -> None:
    k.step("Waiting for the six storage nodes to register with the coordinator")
    deadline = time.monotonic() + timeout_s
    last = "no answer yet"
    while time.monotonic() < deadline:
        try:
            status = k.http_json("http://localhost:8000/internal/status")
            states = [n["state"] for n in status["nodes"]]
            last = f"{len(states)} nodes, states: {sorted(set(states))}"
            if len(states) == 6 and set(states) == {"UP"}:
                print("   6 nodes UP, 3 shards available")
                return
        except Exception as exc:  # the forward may need a moment
            last = repr(exc)
        time.sleep(2)
    raise k.Fail(f"the storage cluster did not become healthy in {timeout_s:.0f}s ({last})")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--skip-build", action="store_true", help="don't rebuild images or reload them")
    p.add_argument("--fresh", action="store_true", help="delete all stored data first (clean demo start)")
    p.add_argument("--seed", action="store_true", help="register sample responders when everything is up")
    args = p.parse_args()

    try:
        check_prerequisites()
        if not args.skip_build:
            build_images()
        start_cluster()
        if args.fresh:
            k.step("Starting fresh: deleting the old system and all its stored data")
            k.wipe_data()
        if not args.skip_build:
            load_images()
        deploy(rebuilt=not args.skip_build)
        wait_ready()
        k.step("Starting the port-forward in the background")
        k.start_forward()
        wait_cluster_healthy()
        if args.seed:
            k.step("Registering sample responders")
            k.run(sys.executable, "scripts/seed.py")
    except k.Fail as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1

    print(
        "\nS.P.D. EMERGENCY! Delta Command is online.\n"
        "   Delta Command web page  : http://localhost:8081\n"
        "   Client API + Swagger UI : http://localhost:8080/docs\n"
        "   Coordinator status      : http://localhost:8000/internal/status\n"
        "   RabbitMQ UI             : http://localhost:15672   (guest / guest)\n"
        "   Pods                    : kubectl -n emergency get pods\n"
        "   Stop everything         : python scripts/k8s_down.py"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
