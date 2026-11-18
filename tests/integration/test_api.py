"""Spec section 11 against the live stack: request creation, retrieval and status
updates; resource registration and availability; correct matching through RabbitMQ.

Uses a made-up area (Port Blair) far from the seed data, so results are predictable."""

import pytest

pytestmark = pytest.mark.integration


# -- requests ---------------------------------------------------------------------------------------

def test_create_and_retrieve_a_request(app):
    created = app.api.post("/requests", json={"type": "shelter", "location": app.spot(), "quantity": 3})
    assert created.status_code == 201
    body = created.json()
    assert body["id"].startswith("req-") and body["version"] == 1

    got = app.api.get(f"/requests/{body['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == body["id"] and got.json()["quantity"] == 3


def test_invalid_input_and_unknown_ids(app):
    assert app.api.post("/requests", json={"type": "pizza", "location": app.spot()}).status_code == 400
    assert app.api.post("/requests", json={"type": "water", "location": {"lat": 99, "lon": 0}}).status_code == 400
    assert app.api.post("/resources", json={"type": "water", "location": app.spot()}).status_code == 400  # no name
    assert app.api.get("/requests/req-doesnotexist").status_code == 404
    assert app.api.put("/requests/req-doesnotexist/status", json={"status": "CANCELLED"}).status_code == 404
    assert app.api.post("/match/req-doesnotexist").status_code == 404


def test_request_status_follows_the_state_machine(app):
    request = app.create_request("shelter", app.spot())
    app.settle(request["id"])  # nobody offers shelter here: UNMATCHED

    assert app.api.put(f"/requests/{request['id']}/status", json={"status": "FULFILLED"}).status_code == 409
    assert app.api.put(f"/requests/{request['id']}/status", json={"status": "MATCHED"}).status_code == 409
    cancelled = app.api.put(f"/requests/{request['id']}/status", json={"status": "CANCELLED"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
    assert app.api.put(f"/requests/{request['id']}/status", json={"status": "PENDING"}).status_code == 409


# -- resources --------------------------------------------------------------------------------------

def test_register_resource_and_change_availability(app):
    resource = app.register("food", "Test kitchen", app.spot(0.4))
    assert resource["status"] == "AVAILABLE"

    def listed(status):
        return [r["id"] for r in app.api.get("/resources", params={"type": "food", "status": status}).json()]

    assert resource["id"] in listed("AVAILABLE")
    assert app.api.put(f"/resources/{resource['id']}/status", json={"status": "OFFLINE"}).status_code == 200
    assert resource["id"] in listed("OFFLINE") and resource["id"] not in listed("AVAILABLE")
    assert app.api.put(f"/resources/{resource['id']}/status", json={"status": "AVAILABLE"}).status_code == 200
    assert app.api.put(f"/resources/{resource['id']}/status", json={"status": "ASSIGNED"}).status_code == 409


# -- matching through RabbitMQ ----------------------------------------------------------------------

def test_matching_picks_the_nearest_responder_of_the_right_type(app):
    near = app.register("water", "near", app.spot(0.03))
    app.register("water", "far", app.spot(0.22))
    app.register("food", "wrong type, closest", app.spot(0.001))

    matched = app.settle(app.create_request("water", app.spot())["id"])

    assert matched["status"] == "MATCHED" and matched["assigned_resource_id"] == near["id"]
    assigned = [r for r in app.api.get("/resources", params={"status": "ASSIGNED"}).json() if r["id"] == near["id"]]
    assert assigned and assigned[0]["assigned_request_id"] == matched["id"]


def test_responders_beyond_the_distance_limit_are_not_used(app):
    app.register("medicine", "too far (about 100 km)", app.spot(0.9))
    assert app.settle(app.create_request("medicine", app.spot())["id"])["status"] == "UNMATCHED"


def test_manual_match_endpoint(app):
    request = app.settle(app.create_request("transport", app.spot())["id"])
    assert request["status"] == "UNMATCHED"  # nothing to drive yet

    driver = app.register("transport", "driver", app.spot(0.02))
    result = app.api.post(f"/match/{request['id']}")  # no waiting for the queue

    assert result.status_code == 200 and result.json()["outcome"] == "matched"
    assert result.json()["request"]["assigned_resource_id"] == driver["id"]
    assert app.api.post(f"/match/{request['id']}").json()["outcome"] == "skipped"  # nothing more to do


def test_finishing_a_request_frees_its_responder(app):
    tanker = app.register("water", "tanker", app.spot(0.03))
    first = app.settle(app.create_request("water", app.spot())["id"])
    assert first["assigned_resource_id"] == tanker["id"]
    second = app.settle(app.create_request("water", app.spot())["id"])
    assert second["status"] == "UNMATCHED"  # the only tanker is busy

    for status in ("IN_PROGRESS", "FULFILLED"):
        assert app.api.put(f"/requests/{first['id']}/status", json={"status": status}).status_code == 200

    assert app.api.post(f"/match/{second['id']}").json()["request"]["assigned_resource_id"] == tanker["id"]
