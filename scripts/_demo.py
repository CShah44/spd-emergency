"""Shared helpers for the storage demos (demo_distribution / _storage_failure / _recovery).

Everything talks to the stack started by `docker compose up -d`:
  coordinator  -> COORDINATOR_URL (default http://localhost:8000)
  storage node -> http://localhost:9001 .. 9006 (only used to look inside a
                  node directly, and for --method pause)
Output is plain ASCII so it works in any Windows terminal.
"""

import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services"))

from common.models import Type, new_request_id, new_resource_id, utc_now_iso  # noqa: E402
from common.sharding import request_key, resource_key, shard_for  # noqa: E402
from common.storage_client import StorageClient  # noqa: E402

COORDINATOR_URL = os.environ.get("COORDINATOR_URL", "http://localhost:8000")
NODE_IDS = [f"s{s}-{r}" for s in range(3) for r in ("primary", "replica")]
NODE_PORTS = {node_id: 9001 + i for i, node_id in enumerate(NODE_IDS)}

store = StorageClient(COORDINATOR_URL)

CITIES = {
    "Bengaluru": (12.9716, 77.5946),
    "Mumbai": (19.0760, 72.8777),
    "Chennai": (13.0827, 80.2707),
    "Kolkata": (22.5726, 88.3639),
    "Ahmedabad": (23.0225, 72.5714),
}


# -- output ------------------------------------------------------------------------------

def banner(text: str) -> None:
    print("\n" + "=" * 78 + f"\n  {text}\n" + "=" * 78)


def step(text: str) -> None:
    print(f"\n>> {text}")


def ok(text: str) -> None:
    print(f"   [ok] {text}")


def fail(text: str) -> None:
    print(f"   [!!] {text}")


def pause_for_audience(enabled: bool) -> None:
    if enabled:
        input("   (press Enter to continue) ")


def print_status(title: str, focus_shard: int | None = None) -> dict[str, Any]:
    """The coordinator's view of every node: the table we show at each demo stage."""
    st = store.status()
    print(f"\n   {title}")
    print("   shard  node          role      state       leader  keys   last heartbeat")
    print("   -----  ------------  --------  ----------  ------  -----  --------------")
    for n in st["nodes"]:
        mark = ">" if focus_shard is not None and n["shard"] == focus_shard else " "
        keys = "-" if n["keys"] is None else str(n["keys"])
        hb = f"{n['seconds_since_heartbeat']:>5.1f}s ago"
        print(f"  {mark}{n['shard']:^5}  {n['node_id']:<12}  {n['role']:<8}  {n['state']:<10}  "
              f"{'yes' if n['is_leader'] else '':^6}  {keys:>5}  {hb}")
    return st


# -- the stack -------------------------------------------------------------------------------

def check_stack() -> None:
    try:
        st = store.status()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Coordinator not reachable at {COORDINATOR_URL} ({exc}).\n"
                 f"Start the stack first:  docker compose up -d --build")
    if st.get("mode") != "real":
        sys.exit("The coordinator is the in-memory fake; these demos need the real one.")


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True)


def service_of(node_id: str) -> str:
    return f"storage-{node_id}"


def node_url(node_id: str) -> str:
    return f"http://localhost:{NODE_PORTS[node_id]}"


def node_direct(node_id: str, path: str, method: str = "GET") -> httpx.Response:
    """Talk to one storage node directly, bypassing the coordinator."""
    return httpx.request(method, node_url(node_id) + path, timeout=3.0)


METHODS = ("stop", "pause", "delete-pod")
METHODS_HELP = ("stop = docker compose stop; pause = POST /admin/pause (Compose or Kubernetes); "
                "delete-pod = kubectl delete pod (Kubernetes restarts it by itself)")


def pod_of(node_id: str) -> str:
    """s1-replica -> shard1-1 (StatefulSet ordinal 0 = primary, 1 = replica)."""
    shard, role = node_id[1], node_id.split("-")[1]
    return f"shard{shard}-{0 if role == 'primary' else 1}"


def fail_node(node_id: str, method: str) -> None:
    if method == "stop":
        compose("stop", service_of(node_id))
    elif method == "pause":
        node_direct(node_id, "/admin/pause", "POST").raise_for_status()
    elif method == "delete-pod":
        subprocess.run(["kubectl", "delete", "pod", "-n", "emergency", pod_of(node_id), "--wait=false"],
                       check=True, capture_output=True)
    else:
        raise ValueError(method)


def revive_node(node_id: str, method: str) -> None:
    if method == "stop":
        compose("start", service_of(node_id))
    elif method == "pause":
        node_direct(node_id, "/admin/resume", "POST").raise_for_status()
    # delete-pod: nothing to do. The StatefulSet controller recreates the pod with
    # the same name and the same volume, and the node registers and catches up.


def on_kubernetes() -> bool:
    """Nodes register with their StatefulSet DNS name (shard0-0.shard0) on
    Kubernetes and with their Compose service name (storage-s0-primary) otherwise."""
    return any("://shard" in n["url"] for n in store.status()["nodes"])


def coordinator_logs_cmd() -> str:
    return ("kubectl logs -n emergency deploy/coordinator" if on_kubernetes()
            else "docker compose logs coordinator")


def node_state(node_id: str) -> str:
    return next(n["state"] for n in store.status()["nodes"] if n["node_id"] == node_id)


def leader_of(shard: int) -> str | None:
    return store.status()["shards"][str(shard)]["acting_primary"]


def wait_for(predicate: Callable[[], bool], timeout: float, interval: float = 0.2) -> float | None:
    """Seconds until predicate() was true, or None on timeout."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            if predicate():
                return time.monotonic() - start
        except Exception:  # noqa: BLE001  (node may be mid-restart)
            pass
        time.sleep(interval)
    return None


# -- sample data -------------------------------------------------------------------------------

def _near(city: str) -> dict[str, float]:
    lat, lon = CITIES[city]
    return {"lat": round(lat + random.uniform(-0.1, 0.1), 4), "lon": round(lon + random.uniform(-0.1, 0.1), 4)}


def new_request(shard: int | None = None) -> tuple[str, dict[str, Any]]:
    """A realistic request record; with `shard`, keep drawing ids until it lands there."""
    while True:
        rid = new_request_id()
        key = request_key(rid)
        if shard is None or shard_for(key) == shard:
            break
    now = utc_now_iso()
    return key, {
        "id": rid, "type": random.choice(list(Type)).value, "location": _near(random.choice(list(CITIES))),
        "quantity": random.randint(1, 50), "details": "demo", "contact": "+91-90000-00000",
        "status": "PENDING", "assigned_resource_id": None, "created_at": now, "updated_at": now,
    }


def new_resource(shard: int | None = None) -> tuple[str, dict[str, Any]]:
    while True:
        rid = new_resource_id()
        key = resource_key(rid)
        if shard is None or shard_for(key) == shard:
            break
    now = utc_now_iso()
    city = random.choice(list(CITIES))
    return key, {
        "id": rid, "type": random.choice(list(Type)).value, "name": f"{city} volunteer {rid[-4:]}",
        "location": _near(city), "capacity": random.randint(10, 500), "availability": "24x7",
        "contact": "+91-90000-00001", "status": "AVAILABLE", "assigned_request_id": None,
        "created_at": now, "updated_at": now,
    }
