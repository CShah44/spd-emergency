"""Break something on purpose, then time how long the system takes to be whole again.

    python scripts/chaos.py list
    python scripts/chaos.py kill-matcher             # crash one matching-service pod
    python scripts/chaos.py kill-rabbitmq            # crash the message broker
    python scripts/chaos.py kill-storage-leader      # crash the storage node leading shard 0
    python scripts/chaos.py pause-storage-node       # freeze a shard-1 storage node for 15 s
    python scripts/chaos.py kill-coordinator-leader  # crash the leading coordinator

Meant to run WHILE a load test is running (loadtest/run.py --scenario kill-matcher does
that for you): the load shows what the failure costs, and "lost requests" shows whether
anything was dropped. Run on its own it just breaks the thing and reports the recovery time.

A crash is a crash, not a polite shutdown: on Kubernetes the pod is force-deleted with no
grace period; on Docker Compose the container is killed.

"Recovered" means the matchers are consuming from the queue again, in the same number as
before the failure (read from RabbitMQ's own metrics through Prometheus), and on Kubernetes
that every pod of the service is Ready.

The storage and coordinator scenarios [A] bring their own "recovered", read from the
coordinator's /internal/status (through the Kubernetes API's service proxy, so no
port-forward is needed):
  * kill-storage-leader, pause-storage-node: the node has registered again (a new
    registration generation), it is UP after catching up, and all six nodes are UP. The
    detail says how it caught up (operation log or full sync) and how much it copied.
  * kill-coordinator-leader: a NEW leader (higher epoch) has finished settling and all six
    nodes are UP. On Docker Compose (one coordinator, no election) the restarted coordinator
    answering with all six nodes UP.
On Docker Desktop's Kubernetes a crash is `docker kill` of the container: a force-deleted pod
still gets a moment to shut down, and a coordinator would use it to hand its lease over.

Each scenario is one entry in SCENARIOS. To add one, write a function that breaks it and
returns what to wait for (a Breakage), and register it there.
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
COORDINATOR_URL = os.environ.get("COORDINATOR_URL", "http://localhost:8000")  # Compose only
STORAGE_NODES = [f"s{shard}-{role}" for shard in range(3) for role in ("primary", "replica")]
PAUSE_FOR_S = 15  # pause-storage-node: how long the node stays frozen


def storage_pod(node_id: str) -> str:
    """'s1-replica' -> 'shard1-1' (StatefulSet ordinal 0 is the primary, 1 the replica)."""
    shard, role = node_id[1], node_id.split("-", 1)[1]
    return f"shard{shard}-{0 if role == 'primary' else 1}"
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

    # -- storage side [A] --

    def coordinator_status(self) -> dict | None:
        """The leading coordinator's /internal/status, or None if it can't be reached."""
        raise NotImplementedError

    def crash_storage_node(self, node_id: str) -> str:
        """Crash one storage node (e.g. 's0-primary'). Returns what was crashed."""
        raise NotImplementedError

    def set_paused(self, node_id: str, paused: bool) -> None:
        """POST /admin/pause or /admin/resume on a storage node."""
        raise NotImplementedError

    def crash_coordinator_leader(self) -> str:
        """Crash the coordinator that leads now. Returns what was crashed."""
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

    def coordinator_status(self) -> dict | None:
        out = _run("kubectl", "get", "--raw",
                   f"/api/v1/namespaces/{NAMESPACE}/services/coordinator:8000/proxy/internal/status")
        try:
            return json.loads(out.stdout) if out.returncode == 0 else None
        except ValueError:
            return None

    def _hard_kill(self, pod: str, container: str) -> str:
        """docker kill when the pod is a container of the local Docker (Docker Desktop):
        a real crash. Otherwise force-delete the pod."""
        if shutil.which("docker"):
            ids = _run("docker", "ps", "-q", "--filter", f"name=k8s_{container}_{pod}_").stdout.split()
            if ids:
                _run("docker", "kill", ids[0])
                return f"{pod} (docker kill)"
        _run("kubectl", "-n", NAMESPACE, "delete", "pod", pod, "--grace-period=0", "--force")
        return f"{pod} (force-deleted)"

    def crash_storage_node(self, node_id: str) -> str:
        return f"{node_id}: " + self._hard_kill(storage_pod(node_id), "storage-node")

    def set_paused(self, node_id: str, paused: bool) -> None:
        action = "pause" if paused else "resume"
        code = ("import urllib.request as u; u.urlopen(u.Request("
                f"'http://localhost:9000/admin/{action}', method='POST'), timeout=5)")
        out = _run("kubectl", "-n", NAMESPACE, "exec", storage_pod(node_id), "--", "python", "-c", code)
        if out.returncode != 0:
            raise SystemExit(f"could not {action} {node_id}: {out.stderr.strip()}")

    def crash_coordinator_leader(self) -> str:
        holder = _run("kubectl", "-n", NAMESPACE, "get", "lease", "coordinator-leader",
                      "-o", "jsonpath={.spec.holderIdentity}").stdout.strip()
        if not holder:
            raise SystemExit("no coordinator leader (is LEADER_ELECTION on?)")
        return "coordinator leader " + self._hard_kill(holder.rsplit("_", 1)[0], "coordinator")


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

    def coordinator_status(self) -> dict | None:
        try:
            return httpx.get(f"{COORDINATOR_URL}/internal/status", timeout=3).json()
        except (httpx.HTTPError, ValueError):
            return None

    def crash_storage_node(self, node_id: str) -> str:
        return f"{node_id}: " + self.crash(f"storage-{node_id}")

    def set_paused(self, node_id: str, paused: bool) -> None:
        port = 9001 + STORAGE_NODES.index(node_id)  # docker-compose.yml maps the nodes to 9001-9006
        httpx.post(f"http://localhost:{port}/admin/{'pause' if paused else 'resume'}", timeout=5).raise_for_status()

    def crash_coordinator_leader(self) -> str:
        return "the coordinator: " + self.crash("coordinator")  # one coordinator on Compose, no election


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
class Breakage:
    """What a storage-side scenario broke, and how to tell it is whole again."""
    what: str
    check: Callable[[], tuple[bool, str]]  # -> (recovered?, detail)


@dataclass
class Scenario:
    service: str  # what gets crashed
    description: str
    # None: crash one instance of `service`, then wait for the matchers and pods (app services).
    # Otherwise: break it with this, and wait for the Breakage's own check (storage side).
    breaker: Callable[[Platform, Callable[[float], None]], Breakage] | None = None


# -- storage side [A] --------------------------------------------------------------------------------------

def _nodes(status: dict | None) -> dict[str, dict]:
    return {n["node_id"]: n for n in (status or {}).get("nodes", [])}


def _all_up(status: dict | None) -> bool:
    nodes = _nodes(status)
    return len(nodes) == len(STORAGE_NODES) and all(n["state"] == "UP" for n in nodes.values())


def _caught_up(node: dict) -> str:
    report = node.get("last_recovery") or {}
    if report.get("partner") is None:
        return "back UP on its own data (no live partner)"
    how = "from the operation log" if report.get("mode") == "log" else f"by full sync ({report.get('why_full', '')})"
    return f"caught up {how}: {report.get('pulled', 0)} write(s) copied in {report.get('seconds', 0)} s"


def _node_returns(platform: Platform, node_id: str, before: dict | None) -> Callable[[], tuple[bool, str]]:
    """Recovered once the node has registered again (its generation moved on) and is UP,
    with every node UP. The generation, not "seen DOWN": a pod that restarts quickly can
    re-register before the coordinator ever noticed it was gone."""
    old_generation = _nodes(before).get(node_id, {}).get("generation", -1)

    def check() -> tuple[bool, str]:
        status = platform.coordinator_status()
        node = _nodes(status).get(node_id)
        if node is None or node["generation"] <= old_generation or not _all_up(status):
            return False, ""
        leader = status["shards"][node_id[1]]["acting_primary"]
        return True, f"{node_id} {_caught_up(node)}; shard {node_id[1]} led by {leader}"

    return check


def _kill_storage_leader(platform: Platform, sleep: Callable[[float], None]) -> Breakage:
    before = platform.coordinator_status()
    if before is None:
        raise SystemExit("the coordinator is not answering")
    victim = before["shards"]["0"]["acting_primary"] or "s0-primary"
    return Breakage(platform.crash_storage_node(victim), _node_returns(platform, victim, before))


def _pause_storage_node(platform: Platform, sleep: Callable[[float], None]) -> Breakage:
    before = platform.coordinator_status()
    if before is None:
        raise SystemExit("the coordinator is not answering")
    leader = before["shards"]["1"]["acting_primary"]
    victim = next(n for n in ("s1-replica", "s1-primary") if n != leader)  # not the leader: a frozen follower
    platform.set_paused(victim, True)
    sleep(PAUSE_FOR_S)
    platform.set_paused(victim, False)
    return Breakage(f"{victim}: paused {PAUSE_FOR_S} s", _node_returns(platform, victim, before))


def _kill_coordinator_leader(platform: Platform, sleep: Callable[[float], None]) -> Breakage:
    before = platform.coordinator_status()
    old = (before or {}).get("coordinator") or {}
    old_epoch, election = old.get("epoch"), old.get("election") == "on"
    what = platform.crash_coordinator_leader()

    def check() -> tuple[bool, str]:
        status = platform.coordinator_status()
        now = (status or {}).get("coordinator") or {}
        if status is None or now.get("settling") or not _all_up(status):
            return False, ""
        if election:
            if now.get("epoch") is None or old_epoch is None or now["epoch"] <= old_epoch:
                return False, ""
            took = (now.get("last_takeover") or {}).get("settle_seconds")
            return True, f"new leader {now.get('identity')} (epoch {old_epoch} -> {now['epoch']}), settled in {took} s"
        return True, "coordinator back, all six storage nodes registered again"

    return Breakage(what, check)


SCENARIOS: dict[str, Scenario] = {
    "kill-matcher": Scenario("matching-service", "crash one matching-service pod: the others keep matching, the pod comes back"),
    "kill-rabbitmq": Scenario("rabbitmq", "crash the broker: requests are saved but not queued until it is back (the sweeper rescues them)"),
    "kill-storage-leader": Scenario(
        "storage-node", "crash the storage node leading shard 0: its partner takes over, it comes back and catches up",
        _kill_storage_leader),
    "pause-storage-node": Scenario(
        "storage-node", f"freeze a shard-1 storage node that is not leading for {PAUSE_FOR_S} s, then resume it: "
        "it catches up from the operation log", _pause_storage_node),
    "kill-coordinator-leader": Scenario(
        "coordinator", "crash the leading coordinator: the standby takes over, settles, and serves", _kill_coordinator_leader),
}


def run(
    name: str, platform: Platform, read_consumers: Callable[[], int | None] = consumers, timeout_s: float = 240.0,
    interval_s: float = 1.0, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> Outcome:
    scenario = SCENARIOS[name]
    if scenario.breaker is not None:
        return _run_breaker(name, scenario, platform, timeout_s, interval_s, clock, sleep, log)
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


def _run_breaker(
    name: str, scenario: Scenario, platform: Platform, timeout_s: float, interval_s: float,
    clock: Callable[[], float], sleep: Callable[[float], None], log: Callable[[str], None],
) -> Outcome:
    started = clock()
    broken = scenario.breaker(platform, sleep)
    log(f"   chaos: broke {broken.what} ({platform.name}); waiting for recovery")
    while clock() - started < timeout_s:
        recovered, detail = broken.check()
        if recovered:
            return Outcome(name, broken.what, round(clock() - started, 1), detail)
        sleep(interval_s)
    return Outcome(name, broken.what, None, f"not recovered after {timeout_s:.0f} s")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenario", choices=["list", *SCENARIOS])
    p.add_argument("--timeout", type=float, default=240, help="seconds to wait for recovery")
    args = p.parse_args()
    if args.scenario == "list":
        for name, scenario in SCENARIOS.items():
            print(f"{name:<24} {scenario.description}")
        return 0
    outcome = run(args.scenario, detect_platform(), timeout_s=args.timeout)
    print(f"{outcome.scenario}: broke {outcome.what}; "
          + (f"recovered after {outcome.recovered_after_s} s ({outcome.detail})" if outcome.recovered_after_s is not None else outcome.detail))
    return 0 if outcome.recovered_after_s is not None else 1


if __name__ == "__main__":
    sys.exit(main())
