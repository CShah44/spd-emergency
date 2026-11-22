"""The matching rule (DESIGN.md section 4) and "only if still free" (section 5)."""

import threading

import pytest

from common.sharding import request_key, resource_key
from common.storage_client import NotFound, VersionConflict
from matching_service.app.geo import haversine_km
from matching_service.app.matcher import match_request, rank_candidates

BENGALURU = {"lat": 12.9716, "lon": 77.5946}
MUMBAI = {"lat": 19.0760, "lon": 72.8777}


def _res(rid, lat, lon, created_at="2026-11-09T09:00:00Z"):
    return {"id": rid, "location": {"lat": lat, "lon": lon}, "created_at": created_at}


# -- distance and ranking (pure functions) -----------------------------------------------------

def test_haversine_known_distance():
    # Bengaluru to Mumbai is about 845 km as the crow flies.
    assert haversine_km(BENGALURU, MUMBAI) == pytest.approx(845, abs=10)


def test_haversine_same_point_is_zero_and_symmetric():
    assert haversine_km(BENGALURU, BENGALURU) == 0
    assert haversine_km(BENGALURU, MUMBAI) == pytest.approx(haversine_km(MUMBAI, BENGALURU))


def test_rank_sorts_nearest_first():
    far, near, mid = _res("far", 13.30, 77.59), _res("near", 12.98, 77.59), _res("mid", 13.10, 77.59)
    ranked = rank_candidates(BENGALURU, [far, near, mid], max_km=50)
    assert [r["id"] for r in ranked] == ["near", "mid", "far"]


def test_rank_drops_anything_beyond_the_limit():
    near, too_far = _res("near", 12.98, 77.59), _res("too-far", 13.50, 77.59)  # about 58 km
    assert [r["id"] for r in rank_candidates(BENGALURU, [near, too_far], max_km=50)] == ["near"]
    assert [r["id"] for r in rank_candidates(BENGALURU, [near, too_far], max_km=100)] == ["near", "too-far"]


def test_rank_tie_goes_to_the_earliest_registered():
    late = _res("late", 13.0, 77.6, created_at="2026-11-09T12:00:00Z")
    early = _res("early", 13.0, 77.6, created_at="2026-11-09T08:00:00Z")
    assert [r["id"] for r in rank_candidates(BENGALURU, [late, early], max_km=50)] == ["early", "late"]


# -- match_request against the storage contract --------------------------------------------------

def test_matches_the_nearest_resource_of_the_right_type(fake_store, add_resource, add_request):
    add_resource("res-far", lat=13.10)
    add_resource("res-near", lat=12.98)
    add_resource("res-food", type="food", lat=12.9716, lon=77.5946)  # closest, but the wrong type
    add_request("req-1")

    result = match_request(fake_store, "req-1")

    assert result.outcome == "matched"
    assert result.request["status"] == "MATCHED"
    assert result.request["assigned_resource_id"] == "res-near"
    assert result.resource["id"] == "res-near"
    stored = fake_store.get(resource_key("res-near"))
    assert stored["status"] == "ASSIGNED" and stored["assigned_request_id"] == "req-1"
    assert fake_store.get(resource_key("res-far"))["status"] == "AVAILABLE"
    assert fake_store.get(request_key("req-1"))["status"] == "MATCHED"


def test_unmatched_when_nothing_suitable(fake_store, add_resource, add_request):
    add_resource("res-offline", status="OFFLINE")
    add_resource("res-taken", status="ASSIGNED", assigned_request_id="req-other")
    add_resource("res-far-away", lat=19.07, lon=72.87)  # Mumbai
    add_request("req-1")

    result = match_request(fake_store, "req-1")

    assert result.outcome == "unmatched" and result.resource is None
    assert fake_store.get(request_key("req-1"))["status"] == "UNMATCHED"


def test_unmatched_request_is_matched_by_a_later_retry(fake_store, add_resource, add_request):
    add_request("req-1")
    assert match_request(fake_store, "req-1").outcome == "unmatched"

    add_resource("res-1")  # a helper registers afterwards
    result = match_request(fake_store, "req-1")

    assert result.outcome == "matched"
    assert fake_store.get(request_key("req-1"))["status"] == "MATCHED"


def test_request_that_is_not_waiting_is_left_alone(fake_store, add_resource, add_request):
    add_resource("res-1")
    for status in ("MATCHED", "IN_PROGRESS", "FULFILLED", "CANCELLED"):
        stored = add_request("req-1", status=status)
        result = match_request(fake_store, "req-1")
        assert result.outcome == "skipped"
        assert fake_store.get(request_key("req-1"))["version"] == stored["version"]  # not even re-saved
    assert fake_store.get(resource_key("res-1"))["status"] == "AVAILABLE"


def test_unknown_request_raises_not_found(fake_store):
    with pytest.raises(NotFound):
        match_request(fake_store, "req-ghost")


def test_max_km_is_configurable(fake_store, add_resource, add_request):
    add_resource("res-1", lat=13.50)  # about 58 km away
    add_request("req-1")
    assert match_request(fake_store, "req-1", max_km=50).outcome == "unmatched"
    assert match_request(fake_store, "req-1", max_km=100).outcome == "matched"


class _Racer:
    """Wraps a store and runs `before(method, key)` ahead of every call, to make
    "someone else got there first" happen at an exact moment."""

    def __init__(self, store, before):
        self._store, self._before = store, before

    def __getattr__(self, name):
        method = getattr(self._store, name)

        def call(*args, **kwargs):
            self._before(name, args)
            return method(*args, **kwargs)
        return call


def test_loses_the_race_for_a_resource_and_takes_the_next_one(fake_store, add_resource, add_request):
    add_resource("res-near", lat=12.98)
    add_resource("res-next", lat=13.05)
    add_request("req-1")

    def steal_nearest(method, args):
        # Another matcher claims res-near after we looked but before we claim it.
        if method == "put_if_version" and args[0] == resource_key("res-near"):
            if fake_store.get(resource_key("res-near"))["status"] == "AVAILABLE":
                near = fake_store.get(resource_key("res-near"))
                fake_store.put(resource_key("res-near"), {**near, "status": "ASSIGNED", "assigned_request_id": "req-other"})

    result = match_request(_Racer(fake_store, steal_nearest), "req-1")

    assert result.outcome == "matched" and result.resource["id"] == "res-next"
    assert fake_store.get(resource_key("res-near"))["assigned_request_id"] == "req-other"  # theirs, untouched


def test_resource_is_given_back_if_the_request_changed_meanwhile(fake_store, add_resource, add_request):
    add_resource("res-1")
    add_request("req-1")

    def cancel_request(method, args):
        # The client cancels after we claimed the resource but before we save the request.
        if method == "put_if_version" and args[0] == request_key("req-1"):
            current = fake_store.get(request_key("req-1"))
            fake_store.put(request_key("req-1"), {**current, "status": "CANCELLED"})

    with pytest.raises(VersionConflict):
        match_request(_Racer(fake_store, cancel_request), "req-1")

    assert fake_store.get(resource_key("res-1"))["status"] == "AVAILABLE"  # released
    assert fake_store.get(resource_key("res-1"))["assigned_request_id"] is None
    assert fake_store.get(request_key("req-1"))["status"] == "CANCELLED"


def _assert_matched_and_assigned_to_each_other(fake_store, request_id, resource_id):
    request = fake_store.get(request_key(request_id))
    resource = fake_store.get(resource_key(resource_id))
    assert request["status"] == "MATCHED" and request["assigned_resource_id"] == resource_id
    assert resource["status"] == "ASSIGNED" and resource["assigned_request_id"] == request_id


def test_same_request_matched_twice_loser_found_the_winners_resource(fake_store, add_resource, add_request):
    # Two matchers on ONE request (queue consumer + POST /match, sweeper, or a
    # duplicate message). B has read the request; A then matches it completely.
    # B finds A's resource as "already assigned", loses the save, and must NOT
    # give A's resource back.
    add_resource("res-1")
    add_request("req-1")
    ran = []

    def let_a_finish_first(method, args):
        if method == "query" and not ran:
            ran.append(1)
            assert match_request(fake_store, "req-1").outcome == "matched"  # A

    with pytest.raises(VersionConflict):
        match_request(_Racer(fake_store, let_a_finish_first), "req-1")  # B

    _assert_matched_and_assigned_to_each_other(fake_store, "req-1", "res-1")


def test_same_request_matched_twice_loser_claimed_the_resource(fake_store, add_resource, add_request):
    # The other order: A claims res-1, then B finds it as "already assigned" and
    # saves the request first. A loses the save and must NOT release res-1.
    add_resource("res-1")
    add_request("req-1")
    ran = []

    def let_b_save_first(method, args):
        if method == "put_if_version" and args[0] == request_key("req-1") and not ran:
            ran.append(1)
            assert match_request(fake_store, "req-1").outcome == "matched"  # B

    with pytest.raises(VersionConflict):
        match_request(_Racer(fake_store, let_b_save_first), "req-1")  # A

    _assert_matched_and_assigned_to_each_other(fake_store, "req-1", "res-1")


def test_retry_after_a_crash_reuses_the_resource_it_already_claimed(fake_store, add_resource, add_request):
    # The matcher died after claiming res-1 but before saving the request.
    add_resource("res-1", lat=13.05, status="ASSIGNED", assigned_request_id="req-1")
    add_resource("res-2", lat=12.98)  # nearer, and free
    add_request("req-1")

    result = match_request(fake_store, "req-1")

    assert result.outcome == "matched" and result.request["assigned_resource_id"] == "res-1"
    assert fake_store.get(resource_key("res-2"))["status"] == "AVAILABLE"  # no second resource was taken


def test_concurrent_matchers_never_assign_a_resource_twice(fake_store, add_resource, add_request):
    for i in range(3):
        add_resource(f"res-{i}")
    for i in range(8):
        add_request(f"req-{i}")
    results, errors = [], []

    def run(i):
        try:
            results.append(match_request(fake_store, f"req-{i}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    matched = [r for r in results if r.outcome == "matched"]
    assert len(matched) == 3 and len(results) - len(matched) == 5  # 3 resources: exactly 3 winners
    assert len({r.resource["id"] for r in matched}) == 3  # and no resource handed out twice
    for r in matched:
        assert fake_store.get(resource_key(r.resource["id"]))["assigned_request_id"] == r.request["id"]
