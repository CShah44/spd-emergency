"""The extra calls the Delta Command web page needs: the request list, the cluster
overview, and the demo controls that pause and resume a storage node. Also the
publisher's queue statistics (no RabbitMQ needed: its channel is a stand-in)."""

import httpx
import pika
import pytest
from fastapi.testclient import TestClient

from common import config
from request_service.app import publisher as publisher_module
from request_service.app.main import create_app
from request_service.app.publisher import Publisher

REQUEST = {"type": "water", "location": {"lat": 12.97, "lon": 77.59}}

NODES = [
    {"node_id": "s0-primary", "shard": 0, "role": "primary", "state": "DOWN", "url": "http://shard0-0.shard0:9000"},
    {"node_id": "s0-replica", "shard": 0, "role": "replica", "state": "UP", "url": "http://shard0-1.shard0:9000"},
]


class StubPublisher:
    def __init__(self, stats):
        self.stats = stats

    def publish(self, request_id):
        pass

    def queue_stats(self):
        return self.stats

    def close(self):
        pass


class NodeRecorder:
    """Stands in for the storage nodes' /admin endpoints."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.fail = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            raise httpx.ConnectError("node is gone")
        self.calls.append((request.method, str(request.url)))
        return httpx.Response(200, json={"paused": request.url.path.endswith("pause")})


@pytest.fixture
def nodes():
    return NodeRecorder()


@pytest.fixture
def api(fake_store, nodes, monkeypatch):
    monkeypatch.setattr(config, "DEMO_CONTROLS", True)
    fake_store.status = lambda: {"mode": "real", "nodes": NODES, "shards": {"0": {"acting_primary": "s0-replica"}},
                                 "total_keys": 7}
    stats = {"waiting": 3, "consumers": 2, "dead_lettered": 0}
    return TestClient(create_app(
        store=fake_store, publisher=StubPublisher(stats), matching_client=httpx.Client(base_url="http://unused"),
        admin_client=httpx.Client(transport=httpx.MockTransport(nodes)),
    ))


# -- GET /requests ---------------------------------------------------------------------------------

def test_requests_are_listed_newest_first(api, add_request):
    for n, created in enumerate(("2026-11-09T10:00:00Z", "2026-11-09T10:00:02Z", "2026-11-09T10:00:01Z")):
        add_request(f"req-{n}", created_at=created)

    assert [r["id"] for r in api.get("/requests").json()] == ["req-1", "req-2", "req-0"]


def test_the_list_can_be_filtered_and_limited(api, add_request):
    add_request("req-1", type="water", status="PENDING", created_at="2026-11-09T10:00:01Z")
    add_request("req-2", type="food", status="MATCHED", created_at="2026-11-09T10:00:02Z")
    add_request("req-3", type="water", status="MATCHED", created_at="2026-11-09T10:00:03Z")

    assert [r["id"] for r in api.get("/requests", params={"type": "water"}).json()] == ["req-3", "req-1"]
    assert [r["id"] for r in api.get("/requests", params={"status": "MATCHED"}).json()] == ["req-3", "req-2"]
    assert [r["id"] for r in api.get("/requests", params={"limit": 1}).json()] == ["req-3"]


def test_active_first_puts_finished_requests_below_the_active_ones(api, add_request):
    add_request("req-old-active", status="MATCHED", created_at="2026-11-09T09:00:00Z")
    add_request("req-new-active", status="PENDING", created_at="2026-11-09T11:00:00Z")
    add_request("req-done-early", status="FULFILLED", created_at="2026-11-09T08:00:00Z",
                updated_at="2026-11-09T12:00:00Z")
    add_request("req-done-late", status="CANCELLED", created_at="2026-11-09T07:00:00Z",
                updated_at="2026-11-09T13:00:00Z")

    ids = [r["id"] for r in api.get("/requests", params={"active_first": "true"}).json()]

    # active ones newest first, then finished ones, the most recently finished first
    assert ids == ["req-new-active", "req-old-active", "req-done-late", "req-done-early"]


def test_a_request_that_finishes_drops_below_the_last_active_one(api, add_request):
    add_request("req-1", status="IN_PROGRESS", created_at="2026-11-09T10:00:03Z")
    add_request("req-2", status="IN_PROGRESS", created_at="2026-11-09T10:00:02Z")
    add_request("req-3", status="IN_PROGRESS", created_at="2026-11-09T10:00:01Z")
    first = lambda: [r["id"] for r in api.get("/requests", params={"active_first": "true"}).json()]  # noqa: E731
    assert first() == ["req-1", "req-2", "req-3"]

    assert api.put("/requests/req-1/status", json={"status": "FULFILLED"}).status_code == 200

    assert first() == ["req-2", "req-3", "req-1"]


def test_active_first_never_lets_finished_requests_push_active_ones_out(api, add_request):
    for n in range(3):
        add_request(f"req-done-{n}", status="FULFILLED", created_at=f"2026-11-09T10:00:0{n}Z")
    add_request("req-live", status="PENDING", created_at="2026-11-09T01:00:00Z")  # the oldest

    ids = [r["id"] for r in api.get("/requests", params={"active_first": "true", "limit": 2}).json()]

    assert ids[0] == "req-live" and len(ids) == 2


def test_request_summary_counts_every_status(api, add_request):
    add_request("req-1", status="PENDING")
    add_request("req-2", status="PENDING")
    add_request("req-3", status="FULFILLED")

    counts = api.get("/requests/summary").json()

    assert counts["PENDING"] == 2 and counts["FULFILLED"] == 1 and counts["total"] == 3
    assert counts["MATCHED"] == counts["CANCELLED"] == counts["UNMATCHED"] == counts["IN_PROGRESS"] == 0


def test_summary_is_not_mistaken_for_a_request_id(api):
    assert api.get("/requests/summary").status_code == 200 and api.get("/requests/req-nope").status_code == 404


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"status": "NOPE"}, {"type": "pizza"}])
def test_the_list_rejects_bad_parameters(api, params):
    assert api.get("/requests", params=params).status_code == 400


def test_an_empty_list_is_fine(api):
    assert api.get("/requests").json() == []


# -- GET /cluster --------------------------------------------------------------------------------------

def test_cluster_overview_combines_storage_and_queue(api):
    body = api.get("/cluster").json()

    assert [n["node_id"] for n in body["nodes"]] == ["s0-primary", "s0-replica"]
    assert body["shards"]["0"]["acting_primary"] == "s0-replica" and body["total_keys"] == 7
    assert body["queue"] == {"waiting": 3, "consumers": 2, "dead_lettered": 0}
    assert body["controls_enabled"] is True


def test_cluster_overview_still_works_when_rabbitmq_is_down(fake_store, monkeypatch):
    fake_store.status = lambda: {"mode": "real", "nodes": NODES, "shards": {}, "total_keys": 0}
    client = TestClient(create_app(store=fake_store, publisher=StubPublisher(None),
                                   matching_client=httpx.Client(base_url="http://unused")))

    body = client.get("/cluster").json()

    assert body["queue"] is None and len(body["nodes"]) == 2


# -- pausing and resuming nodes ------------------------------------------------------------------------

def test_pause_and_resume_call_the_nodes_admin_endpoint(api, nodes):
    assert api.post("/cluster/nodes/s0-replica/pause").json() == {"node_id": "s0-replica", "paused": True}
    assert api.post("/cluster/nodes/s0-primary/resume").json() == {"node_id": "s0-primary", "paused": False}

    assert nodes.calls == [("POST", "http://shard0-1.shard0:9000/admin/pause"),
                           ("POST", "http://shard0-0.shard0:9000/admin/resume")]


def test_an_unknown_node_is_a_404(api, nodes):
    assert api.post("/cluster/nodes/s9-primary/pause").status_code == 404
    assert nodes.calls == []


def test_the_controls_can_be_switched_off(api, nodes, monkeypatch):
    monkeypatch.setattr(config, "DEMO_CONTROLS", False)

    answer = api.post("/cluster/nodes/s0-replica/pause")

    assert answer.status_code == 403 and "DEMO_CONTROLS" in answer.json()["detail"]
    assert nodes.calls == [] and api.get("/cluster").json()["controls_enabled"] is False


def test_an_unreachable_node_is_a_502(api, nodes):
    nodes.fail = True

    assert api.post("/cluster/nodes/s0-replica/pause").status_code == 502


def test_the_controls_default_to_off():
    assert config._env_bool("SOME_SETTING_THAT_IS_NOT_SET", False) is False


# -- queue statistics ----------------------------------------------------------------------------------

class StatsChannel:
    """A pika channel that answers queue_declare(passive=True) with fixed counts."""

    def __init__(self):
        self.asked: list[str] = []

    def queue_declare(self, queue, passive=False, **kwargs):
        assert passive, "the page must only look at the queues, never create them"
        self.asked.append(queue)
        counts = {"match.requests": (4, 2), "match.requests.dlq": (1, 0)}[queue]
        method = type("Method", (), {"message_count": counts[0], "consumer_count": counts[1]})()
        return type("Frame", (), {"method": method})()


def test_queue_stats_reports_exact_counts(monkeypatch):
    publisher = Publisher(url="amqp://unused")
    channel = StatsChannel()
    monkeypatch.setattr(publisher, "_ready_channel", lambda: channel)

    assert publisher.queue_stats() == {"waiting": 4, "consumers": 2, "dead_lettered": 1}
    assert channel.asked == ["match.requests", "match.requests.dlq"]


def test_queue_stats_gives_up_quietly_and_backs_off(monkeypatch):
    publisher = Publisher(url="amqp://unused")
    attempts = []
    clock = [1000.0]

    def broker_down():
        attempts.append(1)
        raise pika.exceptions.AMQPConnectionError("down")

    monkeypatch.setattr(publisher, "_ready_channel", broker_down)
    monkeypatch.setattr(publisher_module.time, "monotonic", lambda: clock[0])

    assert publisher.queue_stats() is None and len(attempts) == 1
    clock[0] += 1
    assert publisher.queue_stats() is None and len(attempts) == 1  # inside the back-off: no new attempt
    clock[0] += publisher_module.STATS_RETRY_AFTER_S
    assert publisher.queue_stats() is None and len(attempts) == 2  # back-off over: tries again
