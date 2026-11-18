"""Shared helpers for the app-side scripts: seed.py, demo_matching.py, demo_async.py,
demo_k8s_recovery.py, demo_k8s_scaling.py.

Everything talks to the request service through its public REST API, the same
way a real client would:
  request service -> API_URL (default http://localhost:8080)
  RabbitMQ UI/API -> http://localhost:15672 (optional: only used to show queue depth)
Output is plain ASCII so it works in any Windows terminal. Reuses the printing
and waiting helpers of the storage demos (_demo.py), so all demos look alike.
"""

import os
import random
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _demo import CITIES, banner, fail, ok, pause_for_audience, step, wait_for  # noqa: E402, F401  (re-exported)

API_URL = os.environ.get("API_URL", "http://localhost:8080")
RABBITMQ_API = os.environ.get("RABBITMQ_API", "http://localhost:15672/api")
QUEUE = "match.requests"

# A spot far from every seeded city, so a demo's results never depend on the seed data.
PORT_BLAIR = (11.62, 92.73)

api = httpx.Client(base_url=API_URL, timeout=20.0)


def morph_banner(title: str) -> None:
    """The demo banner, with the call that starts every SPD morph."""
    banner(f"S.P.D. EMERGENCY!  {title}")


def check_api() -> None:
    try:
        api.get("/health").raise_for_status()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Request service not reachable at {API_URL} ({exc}).\n"
                 f"Start the stack first:  docker compose up -d --build   (or Kubernetes + scripts/k8s_forward.py)")


def jitter(center: tuple[float, float], spread_deg: float = 0.05) -> dict[str, float]:
    """A point within a few km of `center` (0.05 degrees is about 5 km)."""
    lat, lon = center
    return {"lat": round(lat + random.uniform(-spread_deg, spread_deg), 4),
            "lon": round(lon + random.uniform(-spread_deg, spread_deg), 4)}


def register(type_: str, name: str, location: dict[str, float], **extra) -> dict:
    r = api.post("/resources", json={"type": type_, "name": name, "location": location, **extra})
    r.raise_for_status()
    return r.json()


def create_request(type_: str, location: dict[str, float], **extra) -> dict:
    r = api.post("/requests", json={"type": type_, "location": location, **extra})
    r.raise_for_status()
    return r.json()


def get_request(request_id: str) -> dict:
    r = api.get(f"/requests/{request_id}")
    r.raise_for_status()
    return r.json()


def spot(dlat: float = 0.0) -> dict[str, float]:
    """A point `dlat` degrees north of PORT_BLAIR (1 degree is about 111 km)."""
    return {"lat": round(PORT_BLAIR[0] + dlat, 4), "lon": PORT_BLAIR[1]}


def retire_leftovers() -> None:
    """Take responders left over from an earlier demo run out of play (OFFLINE),
    so every run starts from the same situation. Only touches the Port Blair area."""
    old = [r for r in api.get("/resources", params={"status": "AVAILABLE"}).json()
           if abs(r["location"]["lat"] - PORT_BLAIR[0]) < 1 and abs(r["location"]["lon"] - PORT_BLAIR[1]) < 1]
    for r in old:
        api.put(f"/resources/{r['id']}/status", json={"status": "OFFLINE"}).raise_for_status()
    if old:
        ok(f"{len(old)} responder(s) from an earlier run taken offline")


def check(condition: bool, text: str) -> bool:
    (ok if condition else fail)(text)
    return condition


def settle(request_id: str, timeout: float = 15.0) -> dict:
    """Wait for the matching service to process a request and return it. Exits with
    a hint if nothing happens (usually: the matching service or RabbitMQ is down)."""
    if wait_until_processed([request_id], timeout) is None:
        sys.exit(f"   [!!] {request_id} is still PENDING after {timeout:.0f}s. Is the matching service running?\n"
                 f"        docker compose logs matching-service     (RabbitMQ UI: http://localhost:15672)")
    return get_request(request_id)


def submit_batch(n_resources: int, n_requests: int, workers: int = 10) -> tuple[list[dict], list[str], float]:
    """Register `n_resources` water responders and fire `n_requests` water requests at the
    API from several threads at once (like many clients). Returns (resources, request ids,
    seconds the submission took). The API answers 201 as soon as a request is saved and queued."""
    from concurrent.futures import ThreadPoolExecutor
    import time

    resources = [register("water", f"Water unit {i + 1}", jitter(PORT_BLAIR)) for i in range(n_resources)]
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        requests = list(pool.map(lambda _: create_request("water", jitter(PORT_BLAIR)), range(n_requests)))
    return resources, [r["id"] for r in requests], time.monotonic() - start


def wait_until_processed(request_ids: list[str], timeout: float = 60.0) -> float | None:
    """Seconds until none of the requests is still PENDING (each is MATCHED or
    UNMATCHED), or None on timeout. The matching service does this by itself."""
    return wait_for(lambda: all(get_request(i)["status"] != "PENDING" for i in request_ids), timeout)


def queue_depth() -> int | None:
    """Messages waiting in the RabbitMQ queue, or None if the management API isn't reachable."""
    try:
        r = httpx.get(f"{RABBITMQ_API}/queues/%2F/{QUEUE}", auth=("guest", "guest"), timeout=3.0)
        return r.json()["messages"] if r.status_code == 200 else None
    except (httpx.HTTPError, KeyError, ValueError):
        return None


def describe_queue() -> str:
    depth = queue_depth()
    return "queue depth: n/a (RabbitMQ UI not reachable)" if depth is None else f"messages waiting in RabbitMQ: {depth}"
