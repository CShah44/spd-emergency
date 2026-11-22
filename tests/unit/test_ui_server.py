"""The web page's server: the snapshot it streams to the browser, the polling hub, and the
actions it passes on to the request service. The request service is faked; the Svelte page
itself is not loaded (a stand-in index.html is enough to test serving it)."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from ui_service.app import model
from ui_service.app.api import ApiError
from ui_service.app.hub import Hub
from ui_service.app.main import create_app
from ui_service.app.state import State, geo_cell

WHEN = "10:00:00"


def req(rid="req-tdr-aaaaaaaaaaaa1111", status="PENDING", **extra):
    return {"id": rid, "type": "water", "status": status, "created_at": "2026-11-22T06:00:37Z",
            "location": {"lat": 12.97, "lon": 77.59}, **extra}


def res(rid="res-tdr-bbbbbbbbbbbb2222", status="AVAILABLE", name="Tanker", **extra):
    return {"id": rid, "name": name, "type": "water", "status": status, "location": {"lat": 12.9, "lon": 77.6}, **extra}


CLUSTER = {
    "mode": "real", "controls_enabled": True, "total_keys": 4, "queue": {"waiting": 0, "consumers": 1, "dead_lettered": 0},
    "shards": {"0": {"available": True, "acting_primary": "s0-primary"}},
    "nodes": [
        {"node_id": "s0-primary", "shard": 0, "role": "primary", "state": "UP", "keys": 2},
        {"node_id": "s0-replica", "shard": 0, "role": "replica", "state": "UP", "keys": 2},
    ],
}


class FakeApi:
    def __init__(self):
        self.requests_list = [req()]
        self.resources_list = [res()]
        self.calls = []
        self.fail_create = None

    async def requests(self, limit=100):
        return self.requests_list

    async def request_summary(self):
        return {"PENDING": len(self.requests_list), "total": len(self.requests_list)}

    async def resources(self):
        return self.resources_list

    async def cluster(self):
        return CLUSTER

    async def create_request(self, body):
        self.calls.append(("create_request", body))
        if self.fail_create:
            raise ApiError(self.fail_create)
        return {"id": "req-tdr-cccccccccccc3333", **body}

    async def register_resource(self, body):
        self.calls.append(("register_resource", body))
        return {"id": "res-tdr-dddddddddddd4444", **body}

    async def set_request_status(self, request_id, status):
        self.calls.append(("request_status", request_id, status))
        return {"id": request_id, "status": status}

    async def set_resource_status(self, resource_id, status):
        self.calls.append(("resource_status", resource_id, status))
        return {"id": resource_id, "name": "Tanker", "status": status}

    async def match(self, request_id):
        self.calls.append(("match", request_id))
        return {"outcome": "matched"}

    async def set_node_paused(self, node_id, paused):
        self.calls.append(("pause" if paused else "resume", node_id))
        return {"node_id": node_id}

    async def close(self):
        pass


# -- the snapshot ---------------------------------------------------------------------------------

def test_geo_cell_is_read_from_new_ids_only():
    assert geo_cell("req-tdr-3da406093be30aec") == "tdr"
    assert geo_cell("req-3f9c2a1b7d4e4f0a") is None


def test_a_request_carries_its_shard_buttons_and_responder_name():
    state = State()
    state.apply_resources([res()], WHEN)
    state.apply_requests([req(status="MATCHED", assigned_resource_id=res()["id"])], WHEN)

    row = state.snapshot()["requests"][0]

    assert row["shard"] == model.shard_of("request", row["id"])
    assert row["cell"] == "tdr" and row["lat"] == 12.97
    assert row["assigned"] == {"id": res()["id"], "name": "Tanker"}
    assert [a["label"] for a in row["actions"]] == ["Start", "Cancel"]
    assert row["lifecycle"] == ["done", "current", "todo", "todo"]


def test_a_responder_shows_the_stage_of_the_request_it_serves():
    state = State()
    state.apply_requests([req(status="IN_PROGRESS")], WHEN)
    state.apply_resources([res(status="ASSIGNED", assigned_request_id=req()["id"])], WHEN)

    row = state.snapshot()["resources"][0]

    assert row["serving"]["status"] == "IN_PROGRESS"
    assert row["actions"] == []  # freed only when its request ends


def test_responders_are_listed_assigned_then_available_then_offline():
    state = State()
    state.apply_resources([res("a", "OFFLINE", "A"), res("b", "AVAILABLE", "B"), res("c", "ASSIGNED", "C")], WHEN)
    assert [r["name"] for r in state.snapshot()["resources"]] == ["C", "B", "A"]


def test_feed_events_get_ids_newest_first_and_the_part_that_did_them():
    state = State()
    state.apply_requests([req()], WHEN)  # first answer: nothing to compare with
    state.apply_requests([req(status="MATCHED"), req("req-tdr-eeeeeeeeeeee5555")], WHEN)
    state.apply_requests([req(status="IN_PROGRESS"), req("req-tdr-eeeeeeeeeeee5555")], WHEN)

    events = state.snapshot()["events"]

    assert [e["kind"] for e in events] == ["request", "request", "matching"]  # newest first
    assert events[0]["id"] > events[1]["id"] > events[2]["id"]
    assert "MATCHED -> IN_PROGRESS" in events[0]["text"]  # a move made by a person, not the matcher


def test_an_unchanged_answer_changes_nothing():
    state = State()
    assert state.apply_requests([req()], WHEN)
    assert not state.apply_requests([req()], WHEN)
    assert state.apply_cluster(CLUSTER, WHEN)
    assert not state.apply_cluster(CLUSTER, WHEN)


# -- the hub ---------------------------------------------------------------------------------------

def test_the_hub_bumps_its_version_only_when_something_changed():
    async def scenario():
        api = FakeApi()
        hub = Hub(api)
        await hub.refresh()
        first = hub.version
        await hub.refresh()
        unchanged = hub.version
        api.requests_list = [req(status="MATCHED")]
        await hub.refresh()
        return first, unchanged, hub.version

    first, unchanged, changed = asyncio.run(scenario())
    assert first == 1 and unchanged == 1 and changed == 2


def test_the_hub_reports_a_lost_link_and_keeps_the_last_picture():
    class Down(FakeApi):
        async def requests(self, limit=100):
            raise ApiError("cannot reach Delta Command (ConnectError)")

    async def scenario():
        hub = Hub(FakeApi())
        await hub.refresh()
        hub.api = Down()
        await hub.refresh()
        return hub.state.snapshot()

    snap = asyncio.run(scenario())
    assert snap["link"] == {"ok": False, "note": "cannot reach Delta Command (ConnectError)"}
    assert len(snap["requests"]) == 1


def test_the_hub_polls_only_while_someone_watches():
    hub = Hub(FakeApi())
    assert not hub.watched()
    hub.watchers = 1
    assert hub.watched()
    hub.watchers = 0
    hub.seen()  # a recent GET /api/state
    assert hub.watched()


# -- the HTTP API -----------------------------------------------------------------------------------

@pytest.fixture
def page(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>Delta Command</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)")
    api = FakeApi()
    with TestClient(create_app(api=api, static_dir=tmp_path, poll=False)) as client:
        yield client, api


def test_the_page_and_its_assets_are_served_with_the_right_caching(page):
    client, _ = page
    index = client.get("/")
    assert index.status_code == 200 and "Delta Command" in index.text
    assert index.headers["cache-control"] == "no-cache"
    asset = client.get("/assets/index-abc123.js")
    assert "immutable" in asset.headers["cache-control"]
    assert client.get("/health").json() == {"status": "ok"}


def test_state_returns_a_snapshot(page):
    client, _ = page
    snap = client.get("/api/state").json()
    assert snap["link"]["ok"] and snap["requests"][0]["id"] == req()["id"]
    assert snap["cluster"]["shards"][0]["leader"] == "s0-primary"


def test_config_names_grafana_and_the_cities(page):
    client, _ = page
    config = client.get("/api/config").json()
    assert config["grafana_url"].startswith("http") and config["grafana_dashboard"] == "spd-overview"
    assert "Bengaluru" in config["cities"] and config["types"] == model.TYPES


def test_a_new_request_is_passed_on_and_the_answer_names_its_shard(page):
    client, api = page
    body = {"type": "water", "location": {"lat": 12.97, "lon": 77.59}}
    answer = client.post("/api/requests", json=body).json()
    shard = model.shard_of("request", "req-tdr-cccccccccccc3333")
    assert api.calls == [("create_request", body)]
    assert answer["shard"] == shard and f"shard {shard}" in answer["message"]


def test_a_refused_action_comes_back_as_a_readable_error(page):
    client, api = page
    api.fail_create = "400: location.lat: Input should be less than or equal to 90"
    r = client.post("/api/requests", json={"type": "water", "location": {"lat": 99, "lon": 0}})
    assert r.status_code == 400 and r.json()["detail"].startswith("400: location.lat")
    api.fail_create = "cannot reach Delta Command (ConnectError)"
    assert client.post("/api/requests", json={}).status_code == 502


def test_status_moves_and_matching_reach_the_right_calls(page):
    client, api = page
    client.put("/api/requests/req-1/status", json={"status": "CANCELLED"})
    client.put("/api/resources/res-1/status", json={"status": "OFFLINE"})
    client.post("/api/requests/req-1/match")
    assert api.calls == [("request_status", "req-1", "CANCELLED"), ("resource_status", "res-1", "OFFLINE"),
                         ("match", "req-1")]


def test_node_controls_accept_only_pause_and_resume(page):
    client, api = page
    assert "paused" in client.post("/api/nodes/s0-primary/pause").json()["message"]
    assert "resumed" in client.post("/api/nodes/s0-primary/resume").json()["message"]
    assert client.post("/api/nodes/s0-primary/explode").status_code == 422
    assert api.calls == [("pause", "s0-primary"), ("resume", "s0-primary")]


def test_a_burst_sends_twenty_requests_and_counts_the_failures(page):
    client, api = page
    answer = client.post("/api/burst").json()
    assert answer["sent"] == 20 and answer["failed"] == 0
    assert len(api.calls) == 20 and all(c[1]["details"] == "stress test" for c in api.calls)
