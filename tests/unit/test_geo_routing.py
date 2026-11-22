"""Roadmap task 5, routing side [A]: records placed by region, cells= queries."""

import hashlib
import itertools

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from common import geohash
from common.sharding import (
    key_in_cells, parse_cells, resource_key, shard_for, shard_for_cell, shards_for_cells,
)
from common.storage_client import StorageClient
from coordinator.app import fake

MUMBAI, CHENNAI, BENGALURU = geohash.encode(19.0760, 72.8777), geohash.encode(13.0827, 80.2707), \
    geohash.encode(12.9716, 77.5946)
ALL_REGIONS = ["".join(p) for p in itertools.product("0123456789bcdefghjkmnpqrstuvwxyz", repeat=2)]


def rid(cell, n=0):
    return f"res-{cell}-{n:016x}"


# -- placement -----------------------------------------------------------------------------------

def test_a_geo_key_goes_to_its_regions_shard():
    assert shard_for(resource_key(rid(MUMBAI))) == shard_for_cell(MUMBAI)
    assert shard_for(f"request:req-{MUMBAI}-{0:016x}") == shard_for_cell(MUMBAI)  # requests too


def test_every_cell_of_a_region_shares_a_shard():
    region = MUMBAI[:2]
    assert len({shard_for_cell(region + c) for c in "0123456789bcdefghjkmnpqrstuvwxyz"}) == 1


def test_old_style_keys_keep_the_original_rule():
    for key in ("resource:res-8b1e0c55a2f94d17", "request:req-3f9c2a1b7d4e4f0a", "resource:k1", "anything"):
        assert shard_for(key) == int(hashlib.sha1(key.encode()).hexdigest(), 16) % 3


def test_the_three_test_cities_are_on_three_different_shards():
    # The tests below rely on it (and it shows the rule really spreads regions).
    assert len({shard_for_cell(c) for c in (MUMBAI, CHENNAI, BENGALURU)}) == 3


def test_regions_spread_over_all_shards():
    counts = [0, 0, 0]
    for region in ALL_REGIONS:
        counts[shard_for_cell(region + "0")] += 1
    assert min(counts) > 0.2 * len(ALL_REGIONS)  # each shard owns a fair share of the map


def test_adding_a_fourth_shard_moves_only_what_the_new_shard_takes():
    """The point of the ring: with 4 shards, a region either stays where it was or
    moves to the NEW shard, and only about a quarter move. With "mod N" most
    regions would change shard and all that data would have to move."""
    before = {r: shard_for_cell(r + "0", 3) for r in ALL_REGIONS}
    after = {r: shard_for_cell(r + "0", 4) for r in ALL_REGIONS}
    moved = [r for r in ALL_REGIONS if before[r] != after[r]]
    assert all(after[r] == 3 for r in moved)  # only ever onto the new shard
    assert 0.15 < len(moved) / len(ALL_REGIONS) < 0.35

    by_mod = lambda r, n: int(hashlib.sha1(r.encode()).hexdigest(), 16) % n  # noqa: E731
    moved_by_mod = sum(by_mod(r, 3) != by_mod(r, 4) for r in ALL_REGIONS)
    assert moved_by_mod > 2 * len(moved)


# -- the cells= filter ---------------------------------------------------------------------------

def test_parse_cells():
    assert parse_cells(None) is None and parse_cells("") is None
    assert parse_cells("te7, tf3") == {"te7", "tf3"}
    with pytest.raises(ValueError):
        parse_cells("te7,not-a-cell")


def test_key_in_cells():
    assert key_in_cells(resource_key(rid(MUMBAI)), {MUMBAI})
    assert not key_in_cells(resource_key(rid(CHENNAI)), {MUMBAI})
    assert key_in_cells("resource:res-8b1e0c55a2f94d17", {MUMBAI})  # old-style: never hidden
    assert key_in_cells(resource_key(rid(CHENNAI)), None)  # no filter


def test_fake_coordinator_and_client_support_cells():
    fake.reset()
    with TestClient(fake.app) as http:
        store = StorageClient(client=http)
        store.put(resource_key(rid(MUMBAI)), {"id": rid(MUMBAI), "type": "water"})
        store.put(resource_key(rid(CHENNAI)), {"id": rid(CHENNAI), "type": "water"})
        store.put("resource:res-8b1e0c55a2f94d17", {"id": "res-8b1e0c55a2f94d17", "type": "water"})
        found = {r["id"] for r in store.query("resource:", cells=[MUMBAI], type="water")}
        assert found == {rid(MUMBAI), "res-8b1e0c55a2f94d17"}
        assert len(store.query("resource:", type="water")) == 3
        assert http.get("/store", params={"cells": "nope!"}).status_code == 400


# -- through the real coordinator and storage nodes -------------------------------------------

def _shards_asked(fn):
    count = lambda: REGISTRY.get_sample_value("spd_shards_per_query_count") or 0  # noqa: E731
    total = lambda: REGISTRY.get_sample_value("spd_shards_per_query_sum") or 0  # noqa: E731
    c0, t0 = count(), total()
    result = fn()
    assert count() - c0 == 1
    return result, total() - t0


def _put(coord, record_id, **fields):
    assert coord.put(f"/store/{resource_key(record_id)}", json={"id": record_id, **fields}).status_code == 200


def test_cells_query_asks_only_the_owning_shards(coord, cluster):
    _put(coord, rid(MUMBAI, 1), type="water")
    _put(coord, rid(MUMBAI, 2), type="food")
    _put(coord, rid(CHENNAI, 1), type="water")
    assert cluster.stores[f"s{shard_for_cell(MUMBAI)}-primary"].get(resource_key(rid(MUMBAI, 1))) is not None

    answer, asked = _shards_asked(lambda: coord.get("/store", params={"prefix": "resource:", "cells": MUMBAI,
                                                                         "type": "water"}))
    assert asked == 1
    assert [r["id"] for r in answer.json()] == [rid(MUMBAI, 1)]

    answer, asked = _shards_asked(lambda: coord.get("/store", params={"prefix": "resource:",
                                                                         "cells": f"{MUMBAI},{CHENNAI}"}))
    assert asked == 2 and len(answer.json()) == 3

    _, asked = _shards_asked(lambda: coord.get("/store", params={"prefix": "resource:"}))
    assert asked == 3  # no cells: every shard, as before


def test_a_shard_that_is_down_does_not_block_searches_elsewhere(coord, cluster):
    """A bonus of asking only the owning shard: a Mumbai search still works while
    Chennai's whole shard is down (without cells= it would be a 503)."""
    _put(coord, rid(MUMBAI, 3), type="water")
    chennai = shard_for_cell(CHENNAI)
    for node in (f"s{chennai}-primary", f"s{chennai}-replica"):
        cluster.net.dead.add(node)
        coord.app.state.registry.mark_down(node, "test")
    assert coord.get("/store", params={"prefix": "resource:", "cells": MUMBAI}).status_code == 200
    assert coord.get("/store", params={"prefix": "resource:"}).status_code == 503


def test_bad_cells_are_a_400(coord):
    assert coord.get("/store", params={"prefix": "resource:", "cells": "te7,??"}).status_code == 400


def test_shards_for_cells_near_a_city():
    around = geohash.cells_around(19.0760, 72.8777)  # Mumbai and its 8 neighbours
    assert shards_for_cells(around) == {shard_for_cell(MUMBAI)}  # all in Mumbai's region
