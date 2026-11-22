"""The shared /metrics helper: every service exposes metrics the same way."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from common import metrics


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/things/{thing_id}")
    def thing(thing_id: str):
        return {"id": thing_id}

    @app.get("/broken")
    def broken():
        raise HTTPException(409, detail="no")

    @app.get("/crash")
    def crash():
        raise RuntimeError("boom")

    metrics.instrument(app, "demo")
    return TestClient(app, raise_server_exceptions=False)


def test_metrics_are_served_in_prometheus_format(client):
    answer = client.get("/metrics")

    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/plain")
    assert "# TYPE spd_http_requests_total counter" in answer.text


def test_every_call_is_counted_under_its_route_template_not_its_path(client):
    before = sample("spd_http_requests_total", service="demo", route="/things/{thing_id}", status="200")

    client.get("/things/a")
    client.get("/things/b")

    after = sample("spd_http_requests_total", service="demo", route="/things/{thing_id}", status="200")
    assert after - before == 2
    assert sample("spd_http_requests_total", service="demo", route="/things/a", status="200") == 0


def test_the_status_code_is_a_label(client):
    before = sample("spd_http_requests_total", service="demo", route="/broken", status="409")

    client.get("/broken")

    assert sample("spd_http_requests_total", service="demo", route="/broken", status="409") - before == 1


def test_a_crash_is_counted_as_a_500(client):
    before = sample("spd_http_requests_total", service="demo", route="/crash", status="500")

    client.get("/crash")

    assert sample("spd_http_requests_total", service="demo", route="/crash", status="500") - before == 1


def test_unknown_paths_share_one_label(client):
    before = sample("spd_http_requests_total", service="demo", route="unmatched", status="404")

    client.get("/no/such/page/1")
    client.get("/no/such/page/2")

    assert sample("spd_http_requests_total", service="demo", route="unmatched", status="404") - before == 2


def test_durations_are_recorded(client):
    before = sample("spd_http_request_seconds_count", service="demo", route="/things/{thing_id}")

    client.get("/things/x")

    assert sample("spd_http_request_seconds_count", service="demo", route="/things/{thing_id}") - before == 1


def test_reading_the_metrics_is_not_itself_counted(client):
    client.get("/metrics")

    assert sample("spd_http_requests_total", service="demo", route="/metrics", status="200") == 0


def test_every_metric_name_starts_with_spd():
    names = {m.name for m in REGISTRY.collect() if m.name.startswith("spd_")}

    assert {"spd_http_requests", "spd_match_e2e_seconds", "spd_shards_per_query", "spd_coordinator_epoch"} <= names
    own = [v for v in vars(metrics).values() if hasattr(v, "_name") and hasattr(v, "collect")]
    assert own and all(m._name.startswith("spd_") for m in own)
