"""Break something on purpose, then time how long the system takes to be whole again.

    python scripts/chaos.py list
    python scripts/chaos.py kill-matcher       # crash one matching-service pod
    python scripts/chaos.py kill-rabbitmq      # crash the message broker

Meant to run WHILE a load test is running (loadtest/run.py --scenario kill-matcher does
that for you): the load shows what the failure costs, and "lost requests" shows whether
anything was dropped. Run on its own it just breaks the thing and reports the recovery time.

A crash is a crash, not a polite shutdown: on Kubernetes the pod is force-deleted with no
grace period; on Docker Compose the container is killed.

"Recovered" means the matchers are consuming from the queue again, in the same number as
before the failure (read from RabbitMQ's own metrics through Prometheus), and on Kubernetes
that every pod of the service is Ready.

Each scenario is one entry in SCENARIOS. To add one (for example killing a storage primary),
write a function that breaks it and returns what to wait for, and register it there.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

import httpx

NAMESPACE = "emergency"
PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
CONSUMERS_QUERY = 'sum(rabbitmq_detailed_queue_consumers{queue="match.requests"})'
COMPOSE_DOWN_FOR_S = 10  # Compose does not bring a killed container back by itself: we do, after this long


@dataclass
class Outcome:
    scenario: str
    what: str  # what was broken
    recovered_after_s: float | None  # None: it did not recover within the timeout
    detail: str = ""


class Platform:
    """Where the system runs: knows how to crash a service and to see it healthy again."""

    name = ""

    def crash(self, service: str) -> str:
        """Crash ONE instance of the service. Returns its name."""
        raise NotImplementedError

    def all_ready(self, service: str) -> bool:
        raise NotImplementedError


def _run(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


class Kubernetes(Platform):
    name = "kubernetes"

    def _pods(self, service: str) -> list[dict]:
        out = _run("kubectl", "-n", NAMESPACE, "get", "pods", "-l", f"app={service}", "-o", "json")
        return json.loads(out.stdout)["items"] if out.returncode == 0 else []

    def crash(self, service: str) -> str:
        pods = [p["metadata"]["name"] for p in self._pods(service) if not p["metadata"].get("deletionTimestamp")]
        if not pods:
            raise SystemExit(f"no pod of '{service}' is running")
        victim = sorted(pods)[0]
        _run("kubectl", "-n", NAMESPACE, "delete", "pod", victim, "--grace-period=0", "--force")
        return victim

    def all_ready(self, service: str) -> bool:
        pods = self._pods(service)
        return bool(pods) and all(
            not p["metadata"].get("deletionTimestamp")
            and any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))
            for p in pods
        )


class Compose(Platform):
    name = "docker compose"

    def _containers(self, service: str) -> list[str]:
        return _run("docker", "compose", "ps", "-q", service).stdout.split()

    def crash(self, service: str) -> str:
        containers = self._containers(service)
        if not containers:
            raise SystemExit(f"no container of '{service}' is running")
        _run("docker", "kill", containers[0])
        time.sleep(COMPOSE_DOWN_FOR_S)  # the broker has no restart policy in docker-compose.yml: bring it back
        _run("docker", "compose", "start", service)
        return f"{service} (container {containers[0][:12]})"

    def all_ready(self, service: str) -> bool:
        ids = self._containers(service)
        return bool(ids) and all(_run("docker", "inspect", "-f", "{{.State.Running}}", i).stdout.strip() == "true" for i in ids)


def detect_platform() -> Platform:
    """Kubernetes if the emergency namespace has pods, otherwise Docker Compose."""
    if shutil.which("kubectl"):
        out = _run("kubectl", "-n", NAMESPACE, "get", "pods", "-o", "name")
        if out.returncode == 0 and out.stdout.strip():
            return Kubernetes()
    return Compose()


def consumers(prometheus: str = PROMETHEUS) -> int | None:
    """How many matchers are listening on the queue right now (None: RabbitMQ or Prometheus not answering)."""
    try:
        result = httpx.get(f"{prometheus}/api/v1/query", params={"query": CONSUMERS_QUERY}, timeout=5).json()["data"]["result"]
        return int(float(result[0]["value"][1])) if result else None
    except (httpx.HTTPError, KeyError, ValueError, IndexError):
        return None


# -- the scenarios -----------------------------------------------------------------------------------------

@dataclass
class Scenario:
    service: str  # what gets crashed
    description: str


SCENARIOS: dict[str, Scenario] = {
    "kill-matcher": Scenario("matching-service", "crash one matching-service pod: the others keep matching, the pod comes back"),
    "kill-rabbitmq": Scenario("rabbitmq", "crash the broker: requests are saved but not queued until it is back (the sweeper rescues them)"),
}


def run(
    name: str, platform: Platform, read_consumers: Callable[[], int | None] = consumers, timeout_s: float = 240.0,
    interval_s: float = 1.0, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> Outcome:
    scenario = SCENARIOS[name]
    before = read_consumers()
    started = clock()
    victim = platform.crash(scenario.service)
    log(f"   chaos: crashed {victim} ({platform.name}); waiting for recovery")
    while clock() - started < timeout_s:
        listening = read_consumers()
        matchers_back = before is None or (listening is not None and listening >= before)
        if matchers_back and platform.all_ready(scenario.service):
            return Outcome(name, victim, round(clock() - started, 1), f"{listening} matcher(s) consuming again")
        sleep(interval_s)
    return Outcome(name, victim, None, f"not recovered after {timeout_s:.0f} s")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenario", choices=["list", *SCENARIOS])
    p.add_argument("--timeout", type=float, default=240, help="seconds to wait for recovery")
    args = p.parse_args()
    if args.scenario == "list":
        for name, scenario in SCENARIOS.items():
            print(f"{name:<16} {scenario.description}")
        return 0
    outcome = run(args.scenario, detect_platform(), timeout_s=args.timeout)
    print(f"{outcome.scenario}: broke {outcome.what}; "
          + (f"recovered after {outcome.recovered_after_s} s ({outcome.detail})" if outcome.recovered_after_s is not None else outcome.detail))
    return 0 if outcome.recovered_after_s is not None else 1


if __name__ == "__main__":
    sys.exit(main())
