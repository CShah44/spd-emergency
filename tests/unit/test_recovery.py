"""Phase 5: a node that comes back catches up before it serves anything."""

import threading

from common.sharding import shard_for


def _key_on_shard(shard, tag="k"):
    i = 0
    while shard_for(f"resource:{tag}{i}") != shard:
        i += 1
    return f"resource:{tag}{i}"


def _keys_on_shard(shard, n):
    out, i = [], 0
    while len(out) < n:
        k = f"resource:many{i}"
        if shard_for(k) == shard:
            out.append(k)
        i += 1
    return out


def _leader(coord, shard):
    return coord.get("/internal/status").json()["shards"][str(shard)]["acting_primary"]


def _fail(coord, cluster, node_id, shard):
    """Stop a node and let the coordinator notice (first failed call marks it DOWN)."""
    cluster.net.dead.add(node_id)
    coord.get(f"/store/{_key_on_shard(shard, 'probe')}")
    assert cluster.states(coord)[node_id] == "DOWN"


def _restart(coord, cluster, node_id):
    cluster.net.dead.discard(node_id)
    return cluster.register(coord, node_id)


def test_returning_primary_catches_up(coord, cluster):
    """The exact gap we saw in Docker at the end of Phase 3, now closed."""
    key, gone, new = _keys_on_shard(0, 3)
    coord.put(f"/store/{key}", json={"status": "PENDING"})
    coord.put(f"/store/{gone}", json={"x": 1})

    _fail(coord, cluster, "s0-primary", 0)
    coord.put(f"/store/{key}", json={"status": "MATCHED"}, headers={"If-Match": "1"})  # v2 on replica only
    coord.delete(f"/store/{gone}")
    coord.put(f"/store/{new}", json={"y": 1})

    assert _restart(coord, cluster, "s0-primary") == "RECOVERING"
    cluster.wait_state(coord, "s0-primary", "UP")

    p = cluster.stores["s0-primary"]
    assert p.get(key) == {"status": "MATCHED", "version": 2}
    assert p.get(gone) is None and p.version_of(gone) == 2  # the delete arrived too
    assert p.get(new) == {"y": 1, "version": 1}
    assert p.sync_entries() == cluster.stores["s0-replica"].sync_entries()

    report = next(n for n in coord.get("/internal/status").json()["nodes"] if n["node_id"] == "s0-primary")
    assert report["last_recovery"]["pulled"] == 3 and report["last_recovery"]["partner"] == "s0-replica"


def test_leadership_stays_with_replica_after_recovery(coord, cluster):
    _fail(coord, cluster, "s1-primary", 1)
    assert _leader(coord, 1) == "s1-replica"
    _restart(coord, cluster, "s1-primary")
    cluster.wait_state(coord, "s1-primary", "UP")
    assert _leader(coord, 1) == "s1-replica"  # sticky: no failback
    # And writes still reach both.
    key = _key_on_shard(1)
    coord.put(f"/store/{key}", json={"a": 1})
    assert cluster.stores["s1-primary"].get(key) == cluster.stores["s1-replica"].get(key)


def test_writes_during_catch_up_are_not_lost(coord, cluster):
    """Hold the catch-up AFTER both snapshots were read, and write meanwhile.
    That write is in neither snapshot, so only live replication to the
    RECOVERING node can deliver it."""
    before, during = _keys_on_shard(2, 2)
    _fail(coord, cluster, "s2-primary", 2)
    coord.put(f"/store/{before}", json={"when": "before"})

    gate = threading.Event()
    cluster.net.gates[("s2-primary", "/internal/replicate-batch")] = gate  # applying the pull waits
    assert _restart(coord, cluster, "s2-primary") == "RECOVERING"
    cluster.net.wait_until_held("s2-primary", "/internal/replicate-batch")  # snapshots are taken

    coord.put(f"/store/{during}", json={"when": "during"})  # leader is s2-replica
    assert cluster.states(coord)["s2-primary"] == "RECOVERING"
    # Not served while recovering: reads come from the leader only.
    assert coord.get(f"/store/{before}").status_code == 200

    gate.set()
    cluster.wait_state(coord, "s2-primary", "UP")
    p = cluster.stores["s2-primary"]
    assert p.get(before)["when"] == "before"  # from the catch-up
    assert p.get(during)["when"] == "during"  # from live replication


def test_fresher_node_returning_second_repairs_the_leader(coord, cluster):
    """Whole shard down. The STALER node comes back first and leads; when the
    fresher one returns, its newer entries are pushed to the leader."""
    key = _key_on_shard(0)
    coord.put(f"/store/{key}", json={"v": 1})  # v1 on both

    _fail(coord, cluster, "s0-primary", 0)
    coord.put(f"/store/{key}", json={"v": 2})  # v2 on replica only
    _fail(coord, cluster, "s0-replica", 0)
    assert coord.get(f"/store/{key}").status_code == 503

    assert _restart(coord, cluster, "s0-primary") == "UP"  # no live partner: own data
    assert coord.get(f"/store/{key}").json()["v"] == 1  # stale, unavoidably, for now

    _restart(coord, cluster, "s0-replica")
    cluster.wait_state(coord, "s0-replica", "UP")
    assert coord.get(f"/store/{key}").json() == {"v": 2, "version": 2}


def test_partner_dies_during_recovery(coord, cluster):
    _fail(coord, cluster, "s1-primary", 1)
    cluster.net.dead.add("s1-replica")  # leader dies; coordinator doesn't know yet
    _restart(coord, cluster, "s1-primary")
    # Catch-up fails on the partner -> partner marked DOWN -> best copy left is our own.
    cluster.wait_state(coord, "s1-primary", "UP")
    assert cluster.states(coord)["s1-replica"] == "DOWN"
    assert _leader(coord, 1) == "s1-primary"


def test_pause_and_resume_flow(coord, cluster):
    """The demo path: pause a node (503), write, resume, register again, catch up."""
    key = _key_on_shard(2)
    cluster.pause("s2-replica")
    coord.put(f"/store/{key}", json={"a": 1})  # replication fails -> replica DOWN
    assert cluster.states(coord)["s2-replica"] == "DOWN"
    cluster.node_apps["s2-replica"].state.node.paused = False
    cluster.register(coord, "s2-replica")
    cluster.wait_state(coord, "s2-replica", "UP")
    assert cluster.stores["s2-replica"].get(key) == {"a": 1, "version": 1}


def test_batch_endpoint_rules(cluster):
    from fastapi.testclient import TestClient

    with TestClient(cluster.node_apps["s0-primary"]) as node:
        r = node.post("/internal/replicate-batch", json={"entries": [
            {"key": "a", "version": 2, "record": {"x": 1}},
            {"key": "b", "version": 1, "deleted": True},
        ]})
        assert r.json() == {"received": 2, "applied": 2}
        r = node.post("/internal/replicate-batch", json={"entries": [
            {"key": "a", "version": 1, "record": {"x": 0}},  # older: ignored
            {"key": "a", "version": 3, "record": {"x": 3}},
        ]})
        assert r.json() == {"received": 2, "applied": 1}
        assert node.post("/internal/replicate-batch",
                         json={"entries": [{"key": "c", "version": 1}]}).status_code == 400
