"""The web page's logic that doesn't need a browser: which buttons each status gets,
what the cluster panel draws, the "what just happened" feed, and the API client's
error messages. (The page itself is built with NiceGUI and is not imported here.)"""

import asyncio

import httpx
import pytest

from common.models import REQUEST_TRANSITIONS, RESOURCE_TRANSITIONS, RequestStatus, ResourceStatus
from ui_service.app import model
from ui_service.app.api import ApiError, DeltaApi

WHEN = "10:00:00"


def req(rid="req-aaaaaaaaaaaa1111", status="PENDING", **extra):
    return {"id": rid, "type": "water", "status": status, "created_at": "2026-11-22T06:00:37Z", **extra}


def res(rid="res-1", status="AVAILABLE", name="Tanker"):
    return {"id": rid, "name": name, "type": "water", "status": status}


# -- buttons -----------------------------------------------------------------------------------------

def test_every_button_is_a_move_the_api_accepts():
    """The page may only offer what the request service will allow."""
    for status in RequestStatus:
        for action in model.request_actions(status.value):
            if action.kind == "status":
                assert RequestStatus(action.target) in REQUEST_TRANSITIONS[status], (status, action)
    for status in ResourceStatus:
        for action in model.resource_actions(status.value):
            assert ResourceStatus(action.target) in RESOURCE_TRANSITIONS[status], (status, action)


def test_clients_are_never_offered_matcher_only_statuses():
    targets = {a.target for s in RequestStatus for a in model.request_actions(s.value)}
    assert not targets & {"MATCHED", "UNMATCHED"}
    assert "ASSIGNED" not in {a.target for s in ResourceStatus for a in model.resource_actions(s.value)}


def test_finished_requests_and_assigned_responders_have_no_buttons():
    assert model.request_actions("FULFILLED") == model.request_actions("CANCELLED") == []
    assert model.resource_actions("ASSIGNED") == []


def test_every_status_has_a_color():
    assert {s.value for s in RequestStatus} <= {s.value for s in model.REQUEST_COLORS}
    assert {s.value for s in ResourceStatus} <= {s.value for s in model.RESOURCE_COLORS}


# -- formatting ----------------------------------------------------------------------------------------

def test_clock_and_short_id():
    assert model.clock("2026-11-22T06:00:37Z") == "06:00:37"
    assert model.clock(None) == "-" and model.clock("bad") == "-"
    assert model.short_id("req-3f9c2a1b7d4e4f0a") == "req-3f9c2a1b"


# -- the cluster panel ------------------------------------------------------------------------------------

def cluster(**overrides):
    status = {
        "nodes": [
            {"node_id": "s0-primary", "shard": 0, "role": "primary", "state": "UP", "keys": 10,
             "seconds_since_heartbeat": 1.2, "last_recovery": None},
            {"node_id": "s0-replica", "shard": 0, "role": "replica", "state": "UP", "keys": 10,
             "seconds_since_heartbeat": 0.4,
             "last_recovery": {"partner": "s0-primary", "pulled": 10, "pushed": 0, "seconds": 0.028}},
            {"node_id": "s1-primary", "shard": 1, "role": "primary", "state": "DOWN", "keys": None,
             "last_recovery": None},
        ],
        "shards": {"0": {"acting_primary": "s0-primary", "available": True},
                   "1": {"acting_primary": None, "available": False}},
        "queue": {"waiting": 1, "consumers": 2, "dead_lettered": 0}, "controls_enabled": True, "total_keys": 20,
    }
    return {**status, **overrides}


def test_cluster_view_marks_leaders_and_summarises_recovery():
    view = model.cluster_view(cluster())
    shard0, shard1 = view["shards"]

    assert shard0["leader"] == "s0-primary" and [n["leader"] for n in shard0["nodes"]] == [True, False]
    assert shard0["nodes"][1]["recovery"] == "caught up: copied 10 from s0-primary in 28 ms"
    assert shard1["available"] is False and shard1["leader"] is None
    assert view["queue"]["consumers"] == 2 and view["controls"] is True


def test_cluster_view_ignores_the_heartbeat_clock():
    """It changes every second: it must not make the panel redraw."""
    a = cluster()
    b = cluster()
    b["nodes"][0]["seconds_since_heartbeat"] = 99.0
    assert model.cluster_view(a) == model.cluster_view(b)


def test_cluster_view_copes_with_the_fake_coordinators_thin_status():
    thin = {"mode": "fake", "nodes": [{"node_id": "fake-s0-primary", "shard": 0, "role": "primary", "state": "UP"}]}
    view = model.cluster_view(thin)
    assert view["shards"][0]["nodes"][0]["keys"] is None and view["queue"] is None and view["controls"] is False
    assert view["shards"][0]["nodes"][0]["silent"] is False  # the fake reports no key counts at all


def silent_cluster():
    status = cluster()
    status["mode"] = "real"
    status["nodes"][0]["keys"] = None  # s0-primary is UP but no longer answers its health check
    return status


def test_a_node_that_stops_answering_is_flagged_but_only_after_two_checks():
    streaks = {}
    first = model.confirm_silent(model.cluster_view(silent_cluster()), streaks)
    second = model.confirm_silent(model.cluster_view(silent_cluster()), streaks)

    assert first["shards"][0]["nodes"][0]["silent"] is False  # one missed check: maybe just busy
    assert second["shards"][0]["nodes"][0]["silent"] is True
    assert second["shards"][0]["nodes"][1]["silent"] is False  # its partner still answers


def test_an_answering_node_resets_the_streak():
    streaks = {}
    model.confirm_silent(model.cluster_view(silent_cluster()), streaks)
    healthy = cluster()
    healthy["mode"] = "real"
    model.confirm_silent(model.cluster_view(healthy), streaks)

    assert streaks["s0-primary"] == 0


def test_a_node_already_marked_down_is_not_called_silent():
    status = silent_cluster()
    status["nodes"][0]["state"] = "DOWN"
    assert model.cluster_view(status)["shards"][0]["nodes"][0]["silent"] is False


def test_the_feed_reports_a_node_going_quiet_once():
    streaks = {}
    quiet = lambda: model.confirm_silent(model.cluster_view(silent_cluster()), streaks)  # noqa: E731
    before = model.confirm_silent(model.cluster_view({**cluster(), "mode": "real"}), {})
    quiet()  # first missed check: not flagged yet
    flagged = quiet()

    events = model.diff_cluster(before, flagged, WHEN)
    assert [e.text for e in events] == ["storage node s0-primary stopped answering (not marked DOWN yet)"]
    assert model.diff_cluster(flagged, quiet(), WHEN) == []  # still quiet: nothing new to say


# -- the feed -----------------------------------------------------------------------------------------------------

def test_nothing_is_reported_on_the_first_refresh():
    assert model.diff_requests(None, {"r": req()}, {}, WHEN) == []
    assert model.diff_resources(None, {"r": res()}, WHEN) == []
    assert model.diff_cluster(None, model.cluster_view(cluster()), WHEN) == []


def test_a_new_request_and_a_status_change_are_reported():
    old = {"req-1": req("req-1")}
    new = {"req-1": req("req-1", "MATCHED", assigned_resource_id="res-1"), "req-2": req("req-2")}

    events = model.diff_requests(old, new, {"res-1": "Tanker NEAR"}, WHEN)

    assert [e.subject for e in events] == ["req-1", "req-2"]
    assert events[0].text == "req-1  PENDING -> MATCHED  (assigned: Tanker NEAR)"
    assert events[1].text == "req-2  new water request"


def test_a_request_first_seen_already_matched_says_so():
    """Matching can finish before the page's next refresh."""
    new = {"req-1": req("req-1", "MATCHED", assigned_resource_id="res-1"), "req-2": req("req-2", "UNMATCHED")}

    events = model.diff_requests({}, new, {"res-1": "Tanker NEAR"}, WHEN)

    assert [e.text for e in events] == [
        "req-1  new water request  ->  MATCHED  (assigned: Tanker NEAR)",
        "req-2  new water request  ->  UNMATCHED",
    ]


def test_an_unchanged_request_reports_nothing():
    same = {"req-1": req("req-1")}
    assert model.diff_requests(same, dict(same), {}, WHEN) == []


def test_a_responder_changing_status_is_reported():
    events = model.diff_resources({"r": res()}, {"r": res(status="ASSIGNED"), "s": res("s", name="New kitchen")}, WHEN)
    assert [e.text for e in events] == ["Tanker  AVAILABLE -> ASSIGNED", "new responder: New kitchen (water)"]


def test_a_node_failure_and_the_leader_change_are_reported():
    before = model.cluster_view(cluster())
    status = cluster()
    status["nodes"][0]["state"] = "DOWN"
    status["shards"]["0"]["acting_primary"] = "s0-replica"

    events = model.diff_cluster(before, model.cluster_view(status), WHEN)

    assert [e.text for e in events] == ["storage node s0-primary  UP -> DOWN", "shard 0 is now led by s0-replica"]
    assert events[0].color == model.RED


# -- the API client ------------------------------------------------------------------------------------------------

def client_for(handler) -> DeltaApi:
    return DeltaApi(client=httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(handler)))


def test_a_successful_call_returns_the_json():
    api = client_for(lambda request: httpx.Response(200, json=[{"id": "req-1"}]))
    assert asyncio.run(api.requests(5)) == [{"id": "req-1"}]


def test_validation_errors_are_turned_into_a_readable_message():
    detail = [{"field": "location.lat", "message": "must be at most 90"}, {"field": "type", "message": "invalid"}]
    api = client_for(lambda request: httpx.Response(400, json={"detail": detail}))

    with pytest.raises(ApiError, match=r"400: location.lat: must be at most 90; type: invalid"):
        asyncio.run(api.create_request({}))


def test_a_plain_error_keeps_the_servers_wording():
    api = client_for(lambda request: httpx.Response(409, json={"detail": "cannot move a request from PENDING to FULFILLED"}))

    with pytest.raises(ApiError, match="409: cannot move a request from PENDING to FULFILLED"):
        asyncio.run(api.set_request_status("req-1", "FULFILLED"))


def test_a_non_json_error_page_does_not_crash_the_client():
    api = client_for(lambda request: httpx.Response(502, text="<html>bad gateway</html>"))

    with pytest.raises(ApiError, match="502"):
        asyncio.run(api.cluster())


def test_an_unreachable_service_is_reported_plainly():
    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ApiError, match="cannot reach Delta Command"):
        asyncio.run(client_for(down).resources())


def test_node_controls_call_the_right_paths():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={})

    api = client_for(handler)
    asyncio.run(api.set_node_paused("s0-primary", True))
    asyncio.run(api.set_node_paused("s0-primary", False))
    asyncio.run(api.match("req-1"))

    assert seen == [("POST", "/cluster/nodes/s0-primary/pause"), ("POST", "/cluster/nodes/s0-primary/resume"),
                    ("POST", "/match/req-1")]


# -- shards, shard health, the life of a request ------------------------------------------------------------------

def test_a_records_shard_is_the_one_the_coordinator_would_pick():
    from common.sharding import request_key, resource_key, shard_for

    for n in range(30):
        assert model.shard_of("request", f"req-{n}") == shard_for(request_key(f"req-{n}"), 3)
        assert model.shard_of("resource", f"res-{n}") == shard_for(resource_key(f"res-{n}"), 3)


def test_records_spread_over_all_three_shards_and_each_has_a_color():
    shards = {model.shard_of("request", f"req-{n:04d}") for n in range(60)}
    assert shards == {0, 1, 2}
    assert len({model.shard_color(s) for s in shards}) == 3


def test_shard_health_tells_ok_degraded_and_down_apart():
    status = silent_cluster()
    status["shards"]["1"] = {"acting_primary": None, "available": False}
    streaks = {}
    model.confirm_silent(model.cluster_view(status), streaks)  # one missed check: not flagged yet
    view = model.confirm_silent(model.cluster_view(status), streaks)  # two: s0-primary is silent

    assert model.shard_health(view) == {0: "degraded", 1: "down"}
    assert model.shard_health(model.confirm_silent(model.cluster_view({**cluster(), "mode": "real"}), {}))[0] == "ok"
    assert model.shard_health(None) == {}


def test_finished_means_no_moves_left():
    assert [s.value for s in RequestStatus if model.is_finished(s.value)] == ["FULFILLED", "CANCELLED"]


def steps(status):
    return [(s["state"], s["color"]) for s in model.lifecycle(status)]


def test_the_lifecycle_bar_follows_the_happy_path():
    assert [s for s, _ in steps("PENDING")] == ["current", "todo", "todo", "todo"]
    assert [s for s, _ in steps("MATCHED")] == ["done", "current", "todo", "todo"]
    assert [s for s, _ in steps("IN_PROGRESS")] == ["done", "done", "current", "todo"]
    assert [s for s, _ in steps("FULFILLED")] == ["done"] * 4


def test_the_lifecycle_bar_shows_unmatched_and_cancelled_differently():
    assert steps("UNMATCHED")[0] == ("current", model.ORANGE)
    assert steps("CANCELLED") == [("todo", model.GREY)] * 4


def test_every_button_has_a_known_tone():
    tones = {a.tone for s in RequestStatus for a in model.request_actions(s.value)}
    tones |= {a.tone for s in ResourceStatus for a in model.resource_actions(s.value)}
    assert tones <= {"primary", "positive", "warning", "negative"}
    assert [a.label for a in model.request_actions("IN_PROGRESS")] == ["Fulfil", "Cancel"]


def test_the_request_list_is_asked_for_active_first_and_the_summary_has_its_own_call():
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={} if request.url.path.endswith("summary") else [])

    api = client_for(handler)
    asyncio.run(api.requests(40))
    asyncio.run(api.request_summary())

    assert seen == [("/requests", {"limit": "40", "active_first": "true"}), ("/requests/summary", {})]
