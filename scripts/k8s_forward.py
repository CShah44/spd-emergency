"""Make the Kubernetes stack reachable on the same local ports as Docker Compose,
so the demo scripts work unchanged:

    coordinator  -> localhost:8000
    request-service (client API) -> localhost:8080,  web page (ui-service) -> localhost:8081,
    RabbitMQ management UI -> localhost:15672
    shard0-0 (s0-primary) -> 9001,  shard0-1 (s0-replica) -> 9002
    shard1-0 -> 9003, shard1-1 -> 9004, shard2-0 -> 9005, shard2-1 -> 9006

    python scripts/k8s_forward.py        # leave running in its own terminal; Ctrl+C to stop

A port-forward to a pod breaks when that pod is deleted or restarted. This
script notices and starts it again, so `--method delete-pod` demos keep working.
Stop `docker compose` first: both want the same ports.
"""

import subprocess
import sys
import time

NAMESPACE = "emergency"
FORWARDS = {"svc/coordinator": "8000:8000", "svc/request-service": "8080:8000",
            "svc/ui-service": "8081:8000", "svc/rabbitmq": "15672:15672"}
for i, (shard, ordinal) in enumerate((s, o) for s in range(3) for o in range(2)):
    FORWARDS[f"pod/shard{shard}-{ordinal}"] = f"{9001 + i}:9000"


def start(target: str, ports: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["kubectl", "port-forward", "-n", NAMESPACE, target, ports],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main() -> None:
    procs = {t: start(t, p) for t, p in FORWARDS.items()}
    for t, p in FORWARDS.items():
        print(f"  {t:<18} -> localhost:{p.split(':')[0]}")
    print("Forwarding. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
            for target, proc in procs.items():
                if proc.poll() is not None:  # died: pod restarted, or not ready yet
                    procs[target] = start(target, FORWARDS[target])
    except KeyboardInterrupt:
        pass
    finally:
        for proc in procs.values():
            proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
