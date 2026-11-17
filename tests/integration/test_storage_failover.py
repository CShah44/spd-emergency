"""Spec section 11 against the live stack: shard selection and distribution,
simultaneous requests, and replica availability during a storage-node failure."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from common.sharding import shard_for
from common.storage_client import StorageUnavailable, VersionConflict

pytestmark = pytest.mark.integration

NODES = [f"s{s}-{r}" for s in range(3) for r in ("primary", "replica")]


# -- normal operation -------------------------------------------------------------------------

def test_each_record_lives_on_exactly_its_shards_two_nodes(stack):
    """The client never says where data goes; the coordinator picks sha1(key) % 3,
    and the record must end up on that shard's primary AND replica, nowhere else."""
    keys = []
    for _ in range(15):
        for make in (stack.new_request, stack.new_resource):
            key, rec = make()
            stack.store.put(key, rec)
            keys.append(key)
    assert {shard_for(k) for k in keys} == {0, 1, 2}, "30 random keys should touch all 3 shards"

    for key in keys:
        holders = {n for n in NODES if stack.node_direct(n, f"/internal/data/{key}").status_code == 200}
        s = shard_for(key)
        assert holders == {f"s{s}-primary", f"s{s}-replica"}, key


def test_replicas_hold_identical_data(stack):
    for shard in range(3):
        assert stack.entries(f"s{shard}-primary") == stack.entries(f"s{shard}-replica"), f"shard {shard}"


def test_simultaneous_creates_all_land(stack):
    records = [stack.new_request() for _ in range(60)]
    with ThreadPoolExecutor(max_workers=20) as pool:
        saved = list(pool.map(lambda kr: stack.store.put(*kr), records))
    assert all(s["version"] == 1 for s in saved)
    for key, rec in records:
        assert stack.store.get(key)["id"] == rec["id"]


def test_simultaneous_conditional_updates_have_one_winner(stack):
    """Twenty matchers try to claim the same resource at once. Compare-and-set on
    the version must let exactly one through; the rest get 409 and would retry
    with another candidate. This is what prevents double assignment."""
    key, rec = stack.new_resource()
    stack.store.put(key, rec)

    def claim(i):
        try:
            stack.store.put_if_version(key, {**rec, "status": "ASSIGNED", "assigned_request_id": f"req-{i}"}, 1)
            return "won"
        except VersionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(claim, range(20)))
    assert results.count("won") == 1 and results.count("conflict") == 19
    final = stack.store.get(key)
    assert final["version"] == 2 and final["status"] == "ASSIGNED"
    s = shard_for(key)
    assert stack.node_direct(f"s{s}-replica", f"/internal/data/{key}").json() == final


# -- one node fails ----------------------------------------------------------------------------

def test_leader_fails_shard_keeps_serving_from_replica(stack):
    shard = 0
    leader = stack.leader_of(shard)
    key, rec = stack.new_resource(shard)
    stack.store.put(key, rec)

    stack.fail(leader)
    # Reads, conditional updates, new records and cross-shard search all still work.
    got = stack.store.get(key)
    assert got["version"] == 1
    updated = stack.store.put_if_version(key, {**got, "status": "ASSIGNED"}, 1)
    assert updated["version"] == 2
    new_key, new_rec = stack.new_request(shard)
    stack.store.put(new_key, new_rec)
    assert key in {f"resource:{r['id']}" for r in stack.store.query("resource:", status="ASSIGNED")}

    # The first failed call marked the node DOWN; the partner now leads.
    assert stack.node_state(leader) == "DOWN"
    assert stack.leader_of(shard) == stack.partner(leader)
    assert stack.store.get(new_key)["id"] == new_rec["id"]


def test_replica_fails_writes_still_succeed(stack):
    shard = 1
    leader = stack.leader_of(shard)
    follower = stack.partner(leader)
    stack.fail(follower)
    key, rec = stack.new_request(shard)
    assert stack.store.put(key, rec)["version"] == 1  # degraded mode: one copy for now
    assert stack.node_direct(leader, f"/internal/data/{key}").status_code == 200
    assert stack.node_state(follower) == "DOWN"
    assert stack.leader_of(shard) == leader  # a replica failing does not move leadership


def test_silent_node_detected_by_missing_heartbeats(stack):
    """No traffic touches the shard, so only the heartbeat timeout (10 s) can notice."""
    shard = 2
    victim = stack.partner(stack.leader_of(shard))
    stack.fail(victim)
    t = time.monotonic()
    stack.wait_state(victim, "DOWN", timeout=20)
    assert time.monotonic() - t <= 15


def test_other_shards_unaffected_by_a_failure(stack):
    stack.fail(stack.leader_of(0))
    for shard in (1, 2):
        key, rec = stack.new_resource(shard)
        stack.store.put(key, rec)
        assert stack.store.get(key)["version"] == 1


# -- a whole shard fails -----------------------------------------------------------------------

def test_whole_shard_down_is_503_never_wrong_data(stack):
    """With both copies gone the data is unavailable: we answer 503 rather than
    'not found' or a partial search (consistency over availability)."""
    key, rec = stack.new_request(2)
    stack.store.put(key, rec)
    other_key, other = stack.new_request(0)
    stack.store.put(other_key, other)

    stack.fail("s2-primary")
    stack.fail("s2-replica")
    with pytest.raises(StorageUnavailable):
        stack.store.get(key)
    with pytest.raises(StorageUnavailable):
        stack.store.put(*stack.new_request(2))
    with pytest.raises(StorageUnavailable):
        stack.store.query("request:")
    assert stack.store.get(other_key)["id"] == other["id"]  # shard 0 is fine

    # Bring both back; the first to return leads, the other catches up from it.
    stack.revive("s2-primary")
    stack.revive("s2-replica")
    stack.wait_state("s2-primary", "UP")
    stack.wait_state("s2-replica", "UP")
    assert stack.store.get(key)["id"] == rec["id"]
