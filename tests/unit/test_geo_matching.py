"""The matcher's geo-sharding side: new ids carry their cell, and matching looks in the
request's cell and its 8 neighbours first. A recording store stands in for the coordinator
so the tests can see exactly which cells each query asked for."""

import re

import pytest

from common import config, geohash
from common.models import new_request_id, new_resource_id
from common.sharding import request_key, resource_key
from matching_service.app.matcher import match_request

BENGALURU = (12.9716, 77.5946)
MUMBAI = (19.0760, 72.8777)


class Recorder:
    """Wraps a StorageClient and remembers the `cells` of every query."""

    def __init__(self, store):
        self._store = store
        self.queries: list[dict] = []

    def query(self, prefix="", cells=None, **filters):
        self.queries.append({"prefix": prefix, "cells": cells, **filters})
        return self._store.query(prefix, cells=cells, **filters)

    def __getattr__(self, name):
        return getattr(self._store, name)


@pytest.fixture
def store(fake_store):
    return Recorder(fake_store)


def resource_queries(store):
    return [q for q in store.queries if q["prefix"] == "resource:"]


# -- ids ------------------------------------------------------------------------------------------

def test_ids_can_carry_a_cell():
    assert re.fullmatch(r"req-tdr-[0-9a-f]{16}", new_request_id("tdr"))
    assert re.fullmatch(r"res-tdr-[0-9a-f]{16}", new_resource_id("tdr"))


def test_ids_without_a_cell_keep_the_old_shape():
    assert re.fullmatch(r"req-[0-9a-f]{16}", new_request_id())
    assert geohash.cell_of_id(new_resource_id()) is None


def test_the_cell_can_be_read_back_from_the_id():
    assert geohash.cell_of_id(new_resource_id("tdr")) == "tdr"


# -- where the matcher looks -------------------------------------------------------------------------

def test_the_matcher_searches_the_requests_cell_and_its_neighbours(store, add_request, add_resource):
    add_resource("res-1", lat=12.98, lon=77.60)
    add_request("req-1", lat=BENGALURU[0], lon=BENGALURU[1])

    assert match_request(store, "req-1").outcome == "matched"

    expected = geohash.cells_around(*BENGALURU)
    first = resource_queries(store)
    assert all(q["cells"] == expected for q in first) and len(expected) == 9


def test_a_match_found_nearby_does_not_ask_every_shard(store, add_request, add_resource):
    add_resource("res-1", lat=12.98, lon=77.60)
    add_request("req-1", lat=BENGALURU[0], lon=BENGALURU[1])

    match_request(store, "req-1")

    assert all(q["cells"] is not None for q in resource_queries(store))


def test_nobody_nearby_falls_back_to_every_shard(store, add_request, add_resource):
    add_resource("res-far", lat=MUMBAI[0], lon=MUMBAI[1])  # about 845 km away: outside the cells
    add_request("req-1", lat=BENGALURU[0], lon=BENGALURU[1])

    assert match_request(store, "req-1").outcome == "unmatched"

    cells_seen = [q["cells"] for q in resource_queries(store) if q.get("status") == "AVAILABLE"]
    assert cells_seen[0] is not None and cells_seen[-1] is None


def test_a_resource_in_a_neighbouring_cell_is_found(store, add_request, add_resource):
    cell = geohash.encode(*BENGALURU)
    lat_min, lat_max, lon_min, lon_max = geohash.decode(cell)
    # 10 km past the cell's east edge: a different cell, but well within 50 km
    east = (BENGALURU[0], lon_max + 0.09)
    assert geohash.encode(*east) != cell
    add_resource("res-east", lat=east[0], lon=east[1])
    add_request("req-1", lat=BENGALURU[0], lon=lon_max - 0.01)

    result = match_request(store, "req-1")

    assert result.outcome == "matched" and result.resource["id"] == "res-east"
    assert all(q["cells"] is not None for q in resource_queries(store))  # found without asking every shard


def test_the_assigned_lookup_is_also_limited_to_the_cells(store, add_request, add_resource):
    add_resource("res-1", lat=12.98, lon=77.60)
    add_request("req-1", lat=BENGALURU[0], lon=BENGALURU[1])

    match_request(store, "req-1")

    assigned = [q for q in resource_queries(store) if q.get("status") == "ASSIGNED"]
    assert assigned and all(q["cells"] is not None for q in assigned)


def test_with_geo_sharding_off_every_shard_is_asked(store, add_request, add_resource, monkeypatch):
    monkeypatch.setattr(config, "GEO_SHARDING", False)
    add_resource("res-1", lat=12.98, lon=77.60)
    add_request("req-1", lat=BENGALURU[0], lon=BENGALURU[1])

    assert match_request(store, "req-1").outcome == "matched"

    assert all(q["cells"] is None for q in resource_queries(store))


def test_a_radius_too_big_for_the_cells_asks_every_shard(store, add_request, add_resource):
    add_resource("res-1", lat=12.98, lon=77.60)
    add_request("req-1", lat=BENGALURU[0], lon=BENGALURU[1])

    match_request(store, "req-1", max_km=400)

    assert all(q["cells"] is None for q in resource_queries(store))


# -- cells_cover ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("lat, radius, expected", [
    (0, 50, True), (13, 50, True), (45, 50, True), (60, 50, True),
    (72, 50, False),  # cells are narrower than 50 km this far north
    (13, 160, False), (13, 400, False),
])
def test_cells_cover_says_whether_the_nine_cells_reach_the_radius(lat, radius, expected):
    assert geohash.cells_cover(lat, 10, radius) is expected


def test_geo_ids_are_stored_and_found_through_their_cell(fake_store):
    rid = new_resource_id(geohash.encode(*BENGALURU))
    fake_store.put(resource_key(rid), {"id": rid, "type": "water", "status": "AVAILABLE",
                                       "location": {"lat": BENGALURU[0], "lon": BENGALURU[1]}})

    inside = fake_store.query("resource:", cells=geohash.cells_around(*BENGALURU))
    outside = fake_store.query("resource:", cells=geohash.cells_around(*MUMBAI))

    assert [r["id"] for r in inside] == [rid] and outside == []
