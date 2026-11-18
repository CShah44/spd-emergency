"""A whole storage cluster in one process: 6 storage node apps + the real
coordinator, with no sockets. Node calls are routed by hostname to the
in-process apps, and a test can "kill" a node (connection refused) or make it
"hang" (read timeout) to exercise failover."""

import asyncio
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from common.sharding import request_key, resource_key
from common.storage_client import StorageClient
from coordinator.app import fake
from coordinator.app.main import create_app as create_coordinator
from storage_node.app.db import Store
from storage_node.app.main import NodeSettings
from storage_node.app.main import create_app as create_node

NODE_IDS = [f"s{shard}-{role}" for shard in range(3) for role in ("primary", "replica")]


class FakeNetwork(httpx.AsyncBaseTransport):
    def __init__(self, apps):
        self.apps = apps  # hostname -> ASGI app
        self.dead: set[str] = set()  # connection refused
        self.hung: set[str] = set()  # request sent, no answer
        # (host, path) -> threading.Event: the call waits until the test sets it.
        self.gates: dict[tuple[str, str], threading.Event] = {}
        self.held: set[tuple[str, str]] = set()

    def wait_until_held(self, host: str, path: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while (host, path) not in self.held:
            if time.monotonic() > deadline:
                raise AssertionError(f"nothing reached the gate {host}{path}")
            time.sleep(0.01)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        gate = self.gates.get((host, request.url.path))
        if gate is not None:
            self.held.add((host, request.url.path))  # lets the test know we're parked here
            await asyncio.to_thread(gate.wait, 5)
        if host in self.dead:
            raise httpx.ConnectError("connection refused", request=request)
        if host in self.hung:
            raise httpx.ReadTimeout("no answer", request=request)
        return await httpx.ASGITransport(app=self.apps[host]).handle_async_request(request)


class Cluster:
    def __init__(self, tmp_path, heartbeat_timeout=1000.0):
        self.node_apps = {}
        self.stores = {}
        for node_id in NODE_IDS:
            shard, role = int(node_id[1]), node_id.split("-")[1]
            settings = NodeSettings(
                node_id=node_id, shard_id=shard, role=role, data_dir=str(tmp_path),
                coordinator_url="http://coordinator", node_url=f"http://{node_id}",
                heartbeat_enabled=False,
            )
            app = create_node(settings)
            # Open the store directly: ASGITransport doesn't run lifespan events.
            store = Store(tmp_path / f"{node_id}.db")
            app.state.node.store = store
            self.node_apps[node_id] = app
            self.stores[node_id] = store
        self.net = FakeNetwork(self.node_apps)
        self.coordinator = create_coordinator(
            num_shards=3, heartbeat_timeout=heartbeat_timeout, sweep_interval=0.05, node_transport=self.net,
        )

    def register_all(self, http):
        for node_id in NODE_IDS:
            self.register(http, node_id)
        for node_id in NODE_IDS:
            self.wait_state(http, node_id, "UP")

    def register(self, http, node_id):
        shard, role = int(node_id[1]), node_id.split("-")[1]
        r = http.post("/internal/nodes/register",
                      json={"node_id": node_id, "shard": shard, "role": role, "url": f"http://{node_id}"})
        assert r.status_code == 200, r.text
        return r.json()["state"]

    @staticmethod
    def states(http):
        return {n["node_id"]: n["state"] for n in http.get("/internal/status").json()["nodes"]}

    def wait_state(self, http, node_id, state, timeout=5.0):
        """Recovery runs in the background: poll until it's done."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.states(http).get(node_id) == state:
                return
            time.sleep(0.02)
        raise AssertionError(f"{node_id} never became {state}: {self.states(http)}")

    def pause(self, node_id):
        self.node_apps[node_id].state.node.paused = True

    def close(self):
        for s in self.stores.values():
            s.close()


@pytest.fixture
def cluster(tmp_path):
    c = Cluster(tmp_path)
    yield c
    c.close()


@pytest.fixture
def coord(cluster):
    """TestClient for the real coordinator with all 6 nodes registered."""
    with TestClient(cluster.coordinator) as http:
        cluster.register_all(http)
        yield http


# -- app side (request + matching service) ------------------------------------------------------
# These run against the in-memory fake coordinator: same /store API as the real one
# (test_storage_contract.py proves they behave the same), so there is no cluster to start.

@pytest.fixture
def fake_store():
    """A StorageClient on the fake coordinator, emptied before each test."""
    fake.reset()
    with TestClient(fake.app) as http:
        yield StorageClient(client=http)


@pytest.fixture
def add_resource(fake_store):
    """add_resource("res-1", type="water", lat=..., lon=..., status=...) stores a resource."""
    def add(rid="res-1", type="water", lat=12.97, lon=77.59, status="AVAILABLE",
            created_at="2026-11-09T09:00:00Z", **extra):
        record = {
            "id": rid, "type": type, "name": rid, "location": {"lat": lat, "lon": lon},
            "status": status, "assigned_request_id": None,
            "created_at": created_at, "updated_at": created_at, **extra,
        }
        return fake_store.put(resource_key(rid), record)
    return add


@pytest.fixture
def add_request(fake_store):
    """add_request("req-1", type="water", lat=..., lon=..., status=...) stores a request."""
    def add(rid="req-1", type="water", lat=12.97, lon=77.59, status="PENDING", **extra):
        record = {
            "id": rid, "type": type, "location": {"lat": lat, "lon": lon},
            "status": status, "assigned_resource_id": None,
            "created_at": "2026-11-09T10:00:00Z", "updated_at": "2026-11-09T10:00:00Z", **extra,
        }
        return fake_store.put(request_key(rid), record)
    return add
