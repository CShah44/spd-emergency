"""Replication and failover through the real coordinator (in-process cluster)."""

import time

from fastapi.testclient import TestClient

from common.sharding import shard_for
from conftest import Cluster


def _key_on_shard(shard, tag="k"):
    i = 0
    while shard_for(f"resource:{tag}{i}") != shard:
        i += 1
    return f"resource:{tag}{i}"


def _states(coord):
    return {n["node_id"]: n["state"] for n in coord.get("/internal/status").json()["nodes"]}


def test_write_reaches_primary_and_replica(coord, cluster):
    key = _key_on_shard(1)
    coord.put(f"/store/{key}", json={"type": "water"})
    coord.put(f"/store/{key}", json={"type": "food"})
    assert cluster.stores["s1-primary"].get(key) == {"type": "food", "version": 2}
    assert cluster.stores["s1-replica"].get(key) == {"type": "food", "version": 2}
    # Other shards don't hold it.
    assert cluster.stores["s0-primary"].get(key) is None


def test_delete_replicates_tombstone(coord, cluster):
    key = _key_on_shard(2)
    coord.put(f"/store/{key}", json={"a": 1})
    assert coord.delete(f"/store/{key}").status_code == 204
    assert cluster.stores["s2-replica"].get(key) is None
    assert cluster.stores["s2-replica"].version_of(key) == 2  # tombstone, not a hole


def test_primary_dead_reads_and_writes_use_replica(coord, cluster):
    key = _key_on_shard(0)
    coord.put(f"/store/{key}", json={"v": 1})

    cluster.net.dead.add("s0-primary")  # container stopped: connection refused
    assert coord.get(f"/store/{key}").json() == {"v": 1, "version": 1}
    r = coord.put(f"/store/{key}", json={"v": 2}, headers={"If-Match": "1"})
    assert r.status_code == 200 and r.json()["version"] == 2

    assert cluster.stores["s0-replica"].get(key)["version"] == 2
    assert _states(coord)["s0-primary"] == "DOWN"
    status = coord.get("/internal/status").json()
    assert status["shards"]["0"]["acting_primary"] == "s0-replica"
    # Shards 1 and 2 are untouched.
    assert status["shards"]["1"]["acting_primary"] == "s1-primary"


def test_paused_primary_fails_over(coord, cluster):
    key = _key_on_shard(1)
    cluster.pause("s1-primary")  # answers 503, like our demo pause
    assert coord.put(f"/store/{key}", json={"a": 1}).status_code == 200
    assert cluster.stores["s1-replica"].get(key) == {"a": 1, "version": 1}
    assert _states(coord)["s1-primary"] == "DOWN"


def test_replica_dead_write_still_succeeds(coord, cluster):
    key = _key_on_shard(2)
    cluster.net.dead.add("s2-replica")
    assert coord.put(f"/store/{key}", json={"a": 1}).status_code == 200  # degraded mode
    assert cluster.stores["s2-primary"].get(key) == {"a": 1, "version": 1}
    assert _states(coord)["s2-replica"] == "DOWN"


def test_whole_shard_down_is_503(coord, cluster):
    key = _key_on_shard(0)
    cluster.net.dead.update({"s0-primary", "s0-replica"})
    assert coord.get(f"/store/{key}").status_code == 503
    assert coord.put(f"/store/{key}", json={}).status_code == 503
    # Search must not quietly return a partial list.
    assert coord.get("/store", params={"prefix": "resource:"}).status_code == 503
    # Keys on other shards still work.
    assert coord.put(f"/store/{_key_on_shard(1)}", json={}).status_code == 200


def test_hung_primary_write_is_503_not_retried(coord, cluster):
    """Ambiguous failure: the write may have landed, so we must not retry it."""
    key = _key_on_shard(1)
    coord.put(f"/store/{key}", json={"a": 1})
    cluster.net.hung.add("s1-primary")
    r = coord.put(f"/store/{key}", json={"a": 2}, headers={"If-Match": "1"})
    assert r.status_code == 503 and "may or may not" in r.json()["detail"]
    # Reads are safe to retry, and the replica now leads the shard.
    assert coord.get(f"/store/{key}").json()["version"] == 1


def test_down_node_heartbeat_is_404_until_it_registers(coord, cluster):
    cluster.net.dead.add("s0-primary")
    coord.put(f"/store/{_key_on_shard(0)}", json={})  # router notices, marks it DOWN
    assert coord.post("/internal/nodes/heartbeat", json={"node_id": "s0-primary"}).status_code == 404
    cluster.net.dead.discard("s0-primary")
    cluster.register(coord, "s0-primary")
    cluster.wait_state(coord, "s0-primary", "UP")
    assert coord.post("/internal/nodes/heartbeat", json={"node_id": "s0-primary"}).status_code == 200


def test_register_rejects_slot_conflict(coord):
    r = coord.post("/internal/nodes/register",
                   json={"node_id": "imposter", "shard": 0, "role": "primary", "url": "http://x"})
    assert r.status_code == 409


def test_silent_node_marked_down_by_sweeper(tmp_path):
    """One node keeps heartbeating, the other goes silent: only the silent one
    is marked DOWN. Only two nodes, and no waiting for every node to be UP
    first: with a 0.2 s timeout that setup could itself take too long and
    (correctly) get every node marked DOWN, which made this test flaky."""
    cluster = Cluster(tmp_path, heartbeat_timeout=0.2)
    try:
        with TestClient(cluster.coordinator) as http:
            cluster.register(http, "s0-primary")
            cluster.register(http, "s0-replica")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                http.post("/internal/nodes/heartbeat", json={"node_id": "s0-replica"})  # only this one
                states = _states(http)
                if states["s0-primary"] == "DOWN" and states["s0-replica"] == "UP":
                    break
                time.sleep(0.03)
            states = _states(http)
            assert states["s0-primary"] == "DOWN"
            assert states["s0-replica"] == "UP"  # kept alive by its heartbeats
    finally:
        cluster.close()


def test_keys_spread_over_shards_and_replicas_match(coord, cluster):
    for i in range(60):
        coord.put(f"/store/request:req-{i:016x}", json={"i": i})
    status = coord.get("/internal/status").json()
    counts = {n["node_id"]: n["keys"] for n in status["nodes"]}
    for shard in range(3):
        assert counts[f"s{shard}-primary"] == counts[f"s{shard}-replica"] > 5
    assert status["total_keys"] == 60
