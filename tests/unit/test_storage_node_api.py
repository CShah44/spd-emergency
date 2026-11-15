import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from storage_node.app.heartbeat import heartbeat_loop
from storage_node.app.main import NodeSettings, NodeState, create_app


def _settings(tmp_path, **over):
    base = dict(
        node_id="s0-primary",
        shard_id=0,
        role="primary",
        data_dir=str(tmp_path),
        coordinator_url="http://coordinator",
        node_url="http://s0-primary:9000",
        heartbeat_interval=0.01,
        heartbeat_enabled=False,
    )
    return NodeSettings(**{**base, **over})


@pytest.fixture
def node(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as http:
        yield http


def test_put_get_delete(node):
    r = node.put("/internal/data/resource:res-1", json={"type": "water"})
    assert r.status_code == 200 and r.json()["version"] == 1
    assert node.get("/internal/data/resource:res-1").json() == {"type": "water", "version": 1}
    assert node.delete("/internal/data/resource:res-1").json() == {
        "key": "resource:res-1", "version": 2, "deleted": True,
    }
    assert node.get("/internal/data/resource:res-1").status_code == 404
    assert node.delete("/internal/data/resource:res-1").status_code == 404


def test_if_match(node):
    node.put("/internal/data/k", json={"s": 1})
    assert node.put("/internal/data/k", json={"s": 2}, headers={"If-Match": "1"}).status_code == 200
    r = node.put("/internal/data/k", json={"s": 3}, headers={"If-Match": "1"})
    assert r.status_code == 409 and r.json()["detail"]["current_version"] == 2
    assert node.put("/internal/data/nope", json={}, headers={"If-Match": "1"}).status_code == 404
    assert node.put("/internal/data/k", json={}, headers={"If-Match": "x"}).status_code == 400


def test_query(node):
    node.put("/internal/data/resource:a", json={"type": "water", "status": "AVAILABLE"})
    node.put("/internal/data/resource:b", json={"type": "food", "status": "AVAILABLE"})
    got = node.get("/internal/data", params={"prefix": "resource:", "type": "water"}).json()
    assert [r["type"] for r in got] == ["water"]


def test_replicate_and_sync(node):
    r = node.put("/internal/replicate/k", json={"version": 4, "record": {"a": 1}})
    assert r.json() == {"key": "k", "applied": True, "version": 4}
    r = node.put("/internal/replicate/k", json={"version": 2, "record": {"a": 0}})
    assert r.json() == {"key": "k", "applied": False, "version": 4}
    node.put("/internal/replicate/k", json={"version": 5, "deleted": True})
    assert node.put("/internal/replicate/k", json={"version": 6}).status_code == 400  # no record
    entries = node.get("/internal/sync").json()["entries"]
    assert entries == [{"key": "k", "version": 5, "deleted": True, "record": None}]


def test_health_and_pause(node):
    h = node.get("/health").json()
    assert h["node_id"] == "s0-primary" and h["shard"] == 0 and h["keys"] == 0
    node.post("/admin/pause")
    assert node.get("/health").status_code == 503
    assert node.get("/internal/data/k").status_code == 503
    node.post("/admin/resume")
    assert node.get("/health").status_code == 200


def test_data_survives_app_restart(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as http:
        http.put("/internal/data/k", json={"a": 1})
    with TestClient(create_app(_settings(tmp_path))) as http:
        assert http.get("/internal/data/k").json() == {"a": 1, "version": 1}


# -- heartbeat loop, against a fake coordinator (httpx.MockTransport) ----------------


def _run_heartbeats(state, handler, seconds=0.15):
    async def main():
        task = asyncio.create_task(heartbeat_loop(state, transport=httpx.MockTransport(handler)))
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())


def test_registers_then_heartbeats(tmp_path):
    calls = []

    def handler(req):
        calls.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"ok": True})

    state = NodeState(_settings(tmp_path))
    _run_heartbeats(state, handler)
    assert calls[0] == (
        "/internal/nodes/register",
        {"node_id": "s0-primary", "shard": 0, "role": "primary", "url": "http://s0-primary:9000"},
    )
    assert len(calls) > 3
    assert all(path == "/internal/nodes/heartbeat" for path, _ in calls[1:])
    assert state.last_heartbeat_ok is True


def test_reregisters_when_coordinator_forgets(tmp_path):
    paths = []

    def handler(req):
        paths.append(req.url.path)
        # Answer 404 to the first heartbeat only: "who are you?"
        if req.url.path.endswith("heartbeat") and paths.count("/internal/nodes/heartbeat") == 1:
            return httpx.Response(404)
        return httpx.Response(200, json={})

    _run_heartbeats(NodeState(_settings(tmp_path)), handler)
    assert paths[:3] == ["/internal/nodes/register", "/internal/nodes/heartbeat", "/internal/nodes/register"]


def test_coordinator_down_does_not_crash(tmp_path):
    def handler(req):
        raise httpx.ConnectError("refused")

    state = NodeState(_settings(tmp_path))
    _run_heartbeats(state, handler)  # must return normally
    assert state.last_heartbeat_ok is False and state.needs_register is True


def test_paused_node_sends_nothing(tmp_path):
    calls = []
    state = NodeState(_settings(tmp_path))
    state.paused = True
    _run_heartbeats(state, lambda req: calls.append(req) or httpx.Response(200, json={}))
    assert calls == []
