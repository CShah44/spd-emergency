"""The client REST API (spec section 5): success codes, error codes, status rules,
and the hand-off to RabbitMQ and the matching service."""

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from common.sharding import request_key, resource_key
from common.storage_client import StorageClient
from matching_service.app.main import create_app as create_matching_app
from request_service.app.main import create_app
from request_service.app.publisher import PublishError

REQUEST = {"type": "water", "location": {"lat": 12.9716, "lon": 77.5946}, "quantity": 20,
           "details": "2 kids, ground floor flooded", "contact": "+91-90000-00000"}
RESOURCE = {"type": "water", "name": "City Water Tanker 4", "location": {"lat": 12.99, "lon": 77.60},
            "capacity": 500, "availability": "24x7"}


class RecordingPublisher:
    """Stands in for RabbitMQ: remembers what would have been queued."""

    def __init__(self):
        self.published: list[str] = []
        self.fail = False

    def publish(self, request_id):
        if self.fail:
            raise PublishError("RabbitMQ is down")
        self.published.append(request_id)

    def close(self):
        pass


@pytest.fixture
def publisher():
    return RecordingPublisher()


@pytest.fixture
def matching(fake_store):
    return TestClient(create_matching_app(store=fake_store, consume=False))


@pytest.fixture
def api(fake_store, publisher, matching):
    return TestClient(create_app(store=fake_store, publisher=publisher, matching_client=matching))


# -- requests --------------------------------------------------------------------------------------

def test_create_request(api, publisher, fake_store):
    r = api.post("/requests", json=REQUEST)
    assert r.status_code == 201
    body = r.json()
    assert re.fullmatch(r"req-tdr-[0-9a-f]{16}", body["id"])  # carries the geohash cell of Bengaluru
    assert body["status"] == "PENDING" and body["version"] == 1 and body["assigned_resource_id"] is None
    assert body["type"] == "water" and body["location"] == REQUEST["location"] and body["quantity"] == 20
    assert body["created_at"] == body["updated_at"]
    assert fake_store.get(request_key(body["id"]))["status"] == "PENDING"  # really stored
    assert publisher.published == [body["id"]]  # and queued for matching


def test_create_request_needs_only_type_and_location(api):
    r = api.post("/requests", json={"type": "shelter", "location": {"lat": 0, "lon": 0}})
    assert r.status_code == 201 and r.json()["quantity"] is None


def test_client_cannot_set_system_fields(api):
    r = api.post("/requests", json={**REQUEST, "id": "req-mine", "status": "FULFILLED", "version": 9})
    assert r.status_code == 201
    assert r.json()["id"] != "req-mine" and r.json()["status"] == "PENDING" and r.json()["version"] == 1


@pytest.mark.parametrize("change", [
    {"type": "pizza"},
    {"type": None},
    {"location": None},
    {"location": {"lat": 91, "lon": 0}},
    {"location": {"lat": 0, "lon": -181}},
    {"location": {"lat": "north", "lon": 0}},
    {"quantity": 0},
    {"quantity": -5},
    {"details": "x" * 501},
])
def test_invalid_request_is_400(api, publisher, change):
    r = api.post("/requests", json={**REQUEST, **change})
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], list) and r.json()["detail"][0]["field"]
    assert publisher.published == []  # nothing saved, nothing queued


def test_malformed_json_is_400(api):
    r = api.post("/requests", content="{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_request_is_saved_even_if_rabbitmq_is_down(api, publisher, fake_store):
    publisher.fail = True
    r = api.post("/requests", json=REQUEST)
    assert r.status_code == 201  # the client's request was accepted and stored...
    assert fake_store.get(request_key(r.json()["id"]))["status"] == "PENDING"  # ...and can still be matched by hand


def test_get_request(api):
    created = api.post("/requests", json=REQUEST).json()
    r = api.get(f"/requests/{created['id']}")
    assert r.status_code == 200 and r.json() == created


def test_get_unknown_request_is_404(api):
    assert api.get("/requests/req-ghost").status_code == 404


def test_storage_down_is_503(publisher, matching):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    dead = StorageClient(client=httpx.Client(base_url="http://coordinator", transport=httpx.MockTransport(refuse)))
    api = TestClient(create_app(store=dead, publisher=publisher, matching_client=matching))
    assert api.post("/requests", json=REQUEST).status_code == 503
    assert api.get("/requests/req-1").status_code == 503
    assert publisher.published == []


# -- request status ----------------------------------------------------------------------------------

def _status(api, request_id, status):
    return api.put(f"/requests/{request_id}/status", json={"status": status})


def test_cancel_a_pending_request(api):
    rid = api.post("/requests", json=REQUEST).json()["id"]
    r = _status(api, rid, "CANCELLED")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED" and r.json()["version"] == 2
    assert api.get(f"/requests/{rid}").json()["status"] == "CANCELLED"


@pytest.mark.parametrize("status", ["FULFILLED", "IN_PROGRESS", "UNMATCHED", "MATCHED", "PENDING"])
def test_illegal_moves_from_pending_are_409(api, status):
    rid = api.post("/requests", json=REQUEST).json()["id"]
    r = _status(api, rid, status)
    assert r.status_code == 409
    assert api.get(f"/requests/{rid}").json()["status"] == "PENDING"  # unchanged


def test_final_states_cannot_be_left(api):
    rid = api.post("/requests", json=REQUEST).json()["id"]
    _status(api, rid, "CANCELLED")
    assert _status(api, rid, "PENDING").status_code == 409


def test_status_update_errors(api):
    assert _status(api, "req-ghost", "CANCELLED").status_code == 404
    rid = api.post("/requests", json=REQUEST).json()["id"]
    assert _status(api, rid, "FLYING").status_code == 400
    assert api.put(f"/requests/{rid}/status", json={}).status_code == 400


def test_retrying_an_unmatched_request_queues_it_again(api, publisher):
    rid = api.post("/requests", json=REQUEST).json()["id"]
    api.post(f"/match/{rid}")  # nothing registered: UNMATCHED
    assert api.get(f"/requests/{rid}").json()["status"] == "UNMATCHED"

    r = _status(api, rid, "PENDING")

    assert r.status_code == 200 and r.json()["status"] == "PENDING"
    assert publisher.published == [rid, rid]  # created, then retried


# -- resources ----------------------------------------------------------------------------------------

def test_register_resource(api, fake_store):
    r = api.post("/resources", json=RESOURCE)
    assert r.status_code == 201
    body = r.json()
    assert body["id"].startswith("res-") and body["status"] == "AVAILABLE" and body["assigned_request_id"] is None
    assert body["version"] == 1 and body["capacity"] == 500
    assert fake_store.get(resource_key(body["id"]))["name"] == "City Water Tanker 4"


@pytest.mark.parametrize("change", [{"type": "pizza"}, {"name": ""}, {"name": None}, {"capacity": 0},
                                    {"location": {"lat": 0}}])
def test_invalid_resource_is_400(api, change):
    assert api.post("/resources", json={**RESOURCE, **change}).status_code == 400


def test_list_resources_and_filters(api):
    api.post("/resources", json={**RESOURCE, "name": "first"})
    api.post("/resources", json={**RESOURCE, "name": "second"})
    api.post("/resources", json={**RESOURCE, "type": "food", "name": "kitchen"})
    offline = api.post("/resources", json={**RESOURCE, "name": "sleeping"}).json()["id"]
    api.put(f"/resources/{offline}/status", json={"status": "OFFLINE"})

    assert len(api.get("/resources").json()) == 4
    assert [r["name"] for r in api.get("/resources", params={"type": "food"}).json()] == ["kitchen"]
    assert [r["name"] for r in api.get("/resources", params={"status": "OFFLINE"}).json()] == ["sleeping"]
    water = api.get("/resources", params={"type": "water", "status": "AVAILABLE"}).json()
    assert sorted(r["name"] for r in water) == ["first", "second"]
    assert api.get("/resources", params={"type": "pizza"}).status_code == 400
    assert api.get("/resources", params={"status": "BROKEN"}).status_code == 400


def test_resource_goes_offline_and_back(api):
    rid = api.post("/resources", json=RESOURCE).json()["id"]
    assert api.put(f"/resources/{rid}/status", json={"status": "OFFLINE"}).json()["status"] == "OFFLINE"
    assert api.put(f"/resources/{rid}/status", json={"status": "AVAILABLE"}).json()["status"] == "AVAILABLE"


def test_resource_status_rules(api):
    rid = api.post("/resources", json=RESOURCE).json()["id"]
    assert api.put(f"/resources/{rid}/status", json={"status": "ASSIGNED"}).status_code == 409  # matcher's job
    assert api.put(f"/resources/{rid}/status", json={"status": "AVAILABLE"}).status_code == 409  # already is
    assert api.put("/resources/res-ghost/status", json={"status": "OFFLINE"}).status_code == 404
    assert api.put(f"/resources/{rid}/status", json={"status": "FLYING"}).status_code == 400


def test_assigned_resource_cannot_be_changed_by_hand(api):
    rid = api.post("/resources", json=RESOURCE).json()["id"]
    api.post("/match/" + api.post("/requests", json=REQUEST).json()["id"])  # the resource is now ASSIGNED
    for status in ("AVAILABLE", "OFFLINE"):
        assert api.put(f"/resources/{rid}/status", json={"status": status}).status_code == 409


# -- the whole story: register, request, match, finish ----------------------------------------------

def _story(api):
    resource = api.post("/resources", json=RESOURCE).json()
    request = api.post("/requests", json=REQUEST).json()
    matched = api.post(f"/match/{request['id']}")
    assert matched.status_code == 200 and matched.json()["outcome"] == "matched"
    return resource, request


def test_match_assigns_the_resource(api):
    resource, request = _story(api)
    assert api.get(f"/requests/{request['id']}").json()["status"] == "MATCHED"
    assert api.get(f"/requests/{request['id']}").json()["assigned_resource_id"] == resource["id"]
    assigned = api.get("/resources", params={"status": "ASSIGNED"}).json()
    assert [r["id"] for r in assigned] == [resource["id"]]
    assert assigned[0]["assigned_request_id"] == request["id"]


def test_fulfilling_a_request_frees_its_resource(api):
    resource, request = _story(api)
    assert _status(api, request["id"], "IN_PROGRESS").status_code == 200
    assert _status(api, request["id"], "FULFILLED").status_code == 200
    freed = api.get("/resources", params={"status": "AVAILABLE"}).json()
    assert [r["id"] for r in freed] == [resource["id"]] and freed[0]["assigned_request_id"] is None


def test_cancelling_a_matched_request_frees_its_resource(api):
    resource, request = _story(api)
    assert _status(api, request["id"], "CANCELLED").status_code == 200
    assert api.get("/resources", params={"status": "AVAILABLE"}).json()[0]["id"] == resource["id"]
    # ...so the next person who needs water gets it.
    second = api.post("/requests", json=REQUEST).json()
    assert api.post(f"/match/{second['id']}").json()["outcome"] == "matched"


def test_match_with_nothing_available_is_unmatched(api):
    rid = api.post("/requests", json=REQUEST).json()["id"]
    r = api.post(f"/match/{rid}")
    assert r.status_code == 200 and r.json()["outcome"] == "unmatched" and r.json()["resource"] is None


def test_match_unknown_request_is_404(api):
    assert api.post("/match/req-ghost").status_code == 404


def test_match_when_matching_service_is_down_is_503(fake_store, publisher):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    down = httpx.Client(base_url="http://matching", transport=httpx.MockTransport(refuse))
    api = TestClient(create_app(store=fake_store, publisher=publisher, matching_client=down))
    rid = api.post("/requests", json=REQUEST).json()["id"]
    assert api.post(f"/match/{rid}").status_code == 503


def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}
