"""The app side's metrics (docs/UPGRADE_PLAN.md, "Agreed metric names"): what the request
service, the matcher, the consumer and the sweeper count and time. Counters only ever go up
and are shared by every test in the process, so each test looks at how much a number moved."""

import time

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from common.sharding import request_key
from common.storage_client import NotFound, VersionConflict
from matching_service.app import matcher as matcher_module
from matching_service.app.consumer import Consumer
from matching_service.app.main import create_app as create_matching_app
from matching_service.app.matcher import match_request
from matching_service.app.sweeper import Sweeper
from request_service.app.main import create_app
from request_service.app.publisher import PublishError

REQUEST = {"type": "water", "location": {"lat": 12.9716, "lon": 77.5946}}


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Publisher:
    def __init__(self, fail=False):
        self.fail = fail

    def publish(self, request_id):
        if self.fail:
            raise PublishError("RabbitMQ is down")

    def close(self):
        pass


def api_with(fake_store, publisher):
    return TestClient(create_app(store=fake_store, publisher=publisher,
                                 matching_client=httpx.Client(base_url="http://unused")))


# -- request service ---------------------------------------------------------------------------------

def test_creating_a_request_is_counted_by_type(fake_store):
    api = api_with(fake_store, Publisher())
    before = value("spd_requests_created_total", type="water")

    api.post("/requests", json=REQUEST)
    api.post("/requests", json={**REQUEST, "type": "food"})

    assert value("spd_requests_created_total", type="water") - before == 1
    assert value("spd_requests_created_total", type="food") >= 1


def test_a_rejected_request_is_not_counted(fake_store):
    api = api_with(fake_store, Publisher())
    before = value("spd_requests_created_total", type="water")

    assert api.post("/requests", json={"type": "water"}).status_code == 400

    assert value("spd_requests_created_total", type="water") == before


def test_a_request_gets_a_precise_creation_time(fake_store):
    api = api_with(fake_store, Publisher())
    before = time.time()

    created = api.post("/requests", json=REQUEST).json()

    assert before <= created["created_ts"] <= time.time()


def test_a_request_that_could_not_be_queued_is_counted(fake_store):
    api = api_with(fake_store, Publisher(fail=True))
    before = value("spd_publish_failures_total")

    assert api.post("/requests", json=REQUEST).status_code == 201  # saved all the same

    assert value("spd_publish_failures_total") - before == 1


def test_both_app_services_serve_metrics(fake_store):
    request_api = api_with(fake_store, Publisher())
    matching_api = TestClient(create_matching_app(store=fake_store, consume=False))

    request_api.post("/requests", json=REQUEST)
    matching_api.get("/health")

    for api, service in ((request_api, "request-service"), (matching_api, "matching-service")):
        body = api.get("/metrics").text
        assert f'service="{service}"' in body and "spd_http_requests_total" in body


# -- matcher ---------------------------------------------------------------------------------------------

def test_a_match_is_counted_and_timed(fake_store, add_request, add_resource):
    add_resource("res-1")
    add_request("req-1")
    matched, timed = value("spd_match_outcomes_total", outcome="matched"), value("spd_match_seconds_count")

    assert match_request(fake_store, "req-1").outcome == "matched"

    assert value("spd_match_outcomes_total", outcome="matched") - matched == 1
    assert value("spd_match_seconds_count") - timed == 1


def test_unmatched_and_skipped_are_counted(fake_store, add_request):
    add_request("req-1")
    add_request("req-2", status="CANCELLED")
    unmatched, skipped = value("spd_match_outcomes_total", outcome="unmatched"), value("spd_match_outcomes_total", outcome="skipped")

    match_request(fake_store, "req-1")
    match_request(fake_store, "req-2")

    assert value("spd_match_outcomes_total", outcome="unmatched") - unmatched == 1
    assert value("spd_match_outcomes_total", outcome="skipped") - skipped == 1


def test_a_missing_request_and_a_conflict_have_their_own_outcomes(fake_store, add_request, add_resource, monkeypatch):
    not_found, conflict = value("spd_match_outcomes_total", outcome="not_found"), value("spd_match_outcomes_total", outcome="conflict")
    with pytest.raises(NotFound):
        match_request(fake_store, "req-nope")

    add_resource("res-1")
    add_request("req-1")
    monkeypatch.setattr(matcher_module, "_save_request", lambda *a, **k: (_ for _ in ()).throw(VersionConflict("x", 409)))
    with pytest.raises(VersionConflict):
        match_request(fake_store, "req-1")

    assert value("spd_match_outcomes_total", outcome="not_found") - not_found == 1
    assert value("spd_match_outcomes_total", outcome="conflict") - conflict == 1


def test_a_failure_is_counted_as_an_error_and_still_timed(fake_store, add_request, monkeypatch):
    add_request("req-1")
    errors, timed = value("spd_match_outcomes_total", outcome="error"), value("spd_match_seconds_count")
    monkeypatch.setattr(matcher_module, "_claim_nearest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        match_request(fake_store, "req-1")

    assert value("spd_match_outcomes_total", outcome="error") - errors == 1
    assert value("spd_match_seconds_count") - timed == 1


def test_end_to_end_time_is_measured_from_the_precise_creation_time(fake_store, add_request, add_resource):
    add_resource("res-1")
    add_request("req-1", created_ts=time.time() - 2.5)
    count, total = value("spd_match_e2e_seconds_count"), value("spd_match_e2e_seconds_sum")

    match_request(fake_store, "req-1")

    assert value("spd_match_e2e_seconds_count") - count == 1
    assert value("spd_match_e2e_seconds_sum") - total == pytest.approx(2.5, abs=0.5)


def test_an_unmatched_answer_also_counts_for_end_to_end_time(fake_store, add_request):
    add_request("req-1", created_ts=time.time() - 1)
    count = value("spd_match_e2e_seconds_count")

    assert match_request(fake_store, "req-1").outcome == "unmatched"

    assert value("spd_match_e2e_seconds_count") - count == 1


def test_an_old_request_without_a_precise_time_falls_back_to_created_at(fake_store, add_request, add_resource):
    add_resource("res-1")
    add_request("req-1")  # the fixture's created_at, no created_ts
    count = value("spd_match_e2e_seconds_count")

    match_request(fake_store, "req-1")

    assert value("spd_match_e2e_seconds_count") - count == 1


def test_a_retry_is_not_counted_as_end_to_end_time(fake_store, add_request, add_resource):
    add_resource("res-1")
    add_request("req-1", status="UNMATCHED", created_ts=time.time() - 600)
    count = value("spd_match_e2e_seconds_count")

    assert match_request(fake_store, "req-1").outcome == "matched"

    assert value("spd_match_e2e_seconds_count") == count


def test_a_skipped_request_is_not_counted_as_end_to_end_time(fake_store, add_request):
    add_request("req-1", status="MATCHED")
    count = value("spd_match_e2e_seconds_count")

    match_request(fake_store, "req-1")

    assert value("spd_match_e2e_seconds_count") == count


# -- consumer and sweeper ------------------------------------------------------------------------------------

class Channel:
    def __init__(self):
        self.calls = []

    def basic_ack(self, tag):
        self.calls.append("ack")

    def basic_nack(self, tag, requeue):
        self.calls.append(("nack", requeue))

    def basic_publish(self, *args):
        self.calls.append("publish")


def test_dead_lettered_messages_are_counted(fake_store, monkeypatch):
    consumer = Consumer(fake_store, url="amqp://unused", retry_delay_s=0, max_attempts=2)
    monkeypatch.setattr("matching_service.app.consumer.match_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    channel = Channel()
    before = value("spd_dead_lettered_total")

    consumer.handle(channel, 1, b"not json")  # unreadable: straight to the dead-letter queue
    consumer.handle(channel, 2, b'{"request_id": "req-1", "attempt": 1}')  # fails, retried: not dead yet
    assert value("spd_dead_lettered_total") - before == 1
    consumer.handle(channel, 3, b'{"request_id": "req-1", "attempt": 2}')  # out of attempts

    assert value("spd_dead_lettered_total") - before == 2


def test_requests_the_sweeper_rescues_are_counted(fake_store, add_request, add_resource):
    add_resource("res-1")
    add_request("req-1")
    before = value("spd_sweeper_rescued_total")

    swept = Sweeper(fake_store, interval_s=0, stale_after_s=60, clock=lambda: time.time()).sweep_once()

    assert swept == 1 and value("spd_sweeper_rescued_total") - before == 1
    assert fake_store.get(request_key("req-1"))["status"] == "MATCHED"
