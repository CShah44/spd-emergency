"""A whole storage cluster in one process: 6 storage node apps + the real
coordinator, with no sockets. Node calls are routed by hostname to the
in-process apps, and a test can "kill" a node (connection refused) or make it
"hang" (read timeout) to exercise failover."""

import httpx
import pytest
from fastapi.testclient import TestClient

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

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
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

    def register(self, http, node_id):
        shard, role = int(node_id[1]), node_id.split("-")[1]
        r = http.post("/internal/nodes/register",
                      json={"node_id": node_id, "shard": shard, "role": role, "url": f"http://{node_id}"})
        assert r.status_code == 200, r.text

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
