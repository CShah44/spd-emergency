"""Roadmap task 4: catch-up from the operation log instead of comparing everything."""

import pytest
from fastapi.testclient import TestClient

from common.sharding import shard_for
from storage_node.app.db import Conflict, Store

from conftest import Cluster


# -- the log inside one node (db.py) -------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    store = Store(tmp_path / "n.db")
    yield store
    store.close()


def test_local_writes_get_consecutive_seqs(db):
    assert db.put_logged("a", {"x": 1})[1] == 1
    assert db.put_logged("b", {"x": 1})[1] == 2
    assert db.delete_logged("b") == (2, 3)  # (tombstone version, seq)
    with pytest.raises(Conflict):
        db.put_logged("a", {"x": 9}, expected_version=7)
    assert db.put_logged("a", {"x": 2})[1] == 4  # the failed write used no seq
    assert db.position()["head"] == 4


def test_log_keeps_each_keys_latest_write_with_its_current_value(db):
    db.put("a", {"x": 1})
    db.put("b", {"x": 1})
    db.put("a", {"x": 2})  # a's seq 1 is replaced by seq 3
    db.delete("b")         # b's seq 2 is replaced by seq 4

    log = db.oplog_since(0)
    assert [(e["seq"], e["key"]) for e in log["entries"]] == [(3, "a"), (4, "b")]
    assert log["entries"][0]["record"] == {"x": 2, "version": 2}
    assert log["entries"][1]["deleted"] is True and log["entries"][1]["record"] is None
    assert [e["key"] for e in db.oplog_since(3)["entries"]] == ["b"]
    assert db.oplog_since(4)["entries"] == [] and db.oplog_since(4)["head"] == 4


def test_replica_writes_are_not_logged_as_ours(db):
    db.apply_replica("a", 5, {"x": 1}, origin=("partner-log", 1))
    assert db.position()["head"] == 0 and db.oplog_since(0)["entries"] == []


def test_log_id_survives_restart_but_not_a_wiped_disk(tmp_path):
    first = Store(tmp_path / "n.db")
    log_id = first.log_id
    first.close()
    again = Store(tmp_path / "n.db")
    assert again.log_id == log_id
    again.close()
    (tmp_path / "n.db").unlink()
    assert Store(tmp_path / "wiped.db").log_id != log_id


def test_watermark_only_moves_over_seqs_actually_received(db):
    db.apply_batch([], "P", through_seq=10)  # caught up with log P up to 10
    db.apply_replica("k12", 1, {"x": 1}, origin=("P", 12))  # 11 missing: wait
    assert db.position()["watermarks"]["P"] == 10
    db.apply_replica("k11", 1, {"x": 1}, origin=("P", 11))  # gap filled: absorbs 12
    assert db.position()["watermarks"]["P"] == 12
    db.apply_replica("k14", 1, {"x": 1}, origin=("P", 14))  # 13 never arrives
    assert db.position()["watermarks"]["P"] == 12  # stays behind the hole: safe


def test_seqs_before_the_first_sync_are_kept_for_it(db):
    db.apply_replica("k6", 1, {"x": 1}, origin=("P", 6))  # no watermark for P yet
    assert "P" not in db.position()["watermarks"]
    db.apply_batch([], "P", through_seq=5)  # the full sync covered up to 5
    assert db.position()["watermarks"]["P"] == 6


# -- recovery through the coordinator --------------------------------------------------------

def _keys_on_shard(shard, n, tag="k"):
    out, i = [], 0
    while len(out) < n:
        if shard_for(f"resource:{tag}{i}") == shard:
            out.append(f"resource:{tag}{i}")
        i += 1
    return out


def _fail(coord, cluster, node_id, shard):
    """Kill a node and have the coordinator notice (directly, so no extra write is
    needed to discover a dead replica: reads never touch the replica)."""
    cluster.net.dead.add(node_id)
    cluster.coordinator.state.registry.mark_down(node_id, "test: killed")
    assert cluster.states(coord)[node_id] == "DOWN"


def _restart_and_recover(coord, cluster, node_id):
    cluster.net.dead.discard(node_id)
    assert cluster.register(coord, node_id) == "RECOVERING"
    cluster.wait_state(coord, node_id, "UP")
    return next(n for n in coord.get("/internal/status").json()["nodes"] if n["node_id"] == node_id)["last_recovery"]


def test_first_sync_is_full_then_catch_ups_read_only_the_log(coord, cluster):
    # register_all already made each replica do its first (full) sync with its primary.
    keys = _keys_on_shard(0, 53)
    for k in keys[:50]:
        coord.put(f"/store/{k}", json={"a": 1})

    _fail(coord, cluster, "s0-primary", 0)
    for k in keys[50:]:
        coord.put(f"/store/{k}", json={"a": 2})  # 3 writes the primary misses

    report = _restart_and_recover(coord, cluster, "s0-primary")
    assert report["mode"] == "log"
    assert report["entries_read"] == 3 and report["pulled"] == 3 and report["pushed"] == 0
    assert cluster.stores["s0-primary"].sync_entries() == cluster.stores["s0-replica"].sync_entries()


def test_full_mode_reads_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("RECOVERY_MODE", "full")
    cluster = Cluster(tmp_path)
    try:
        with TestClient(cluster.coordinator) as coord:
            cluster.register_all(coord)
            keys = _keys_on_shard(1, 53)
            for k in keys[:50]:
                coord.put(f"/store/{k}", json={"a": 1})
            _fail(coord, cluster, "s1-primary", 1)
            for k in keys[50:]:
                coord.put(f"/store/{k}", json={"a": 2})

            report = _restart_and_recover(coord, cluster, "s1-primary")
            assert report["mode"] == "full" and report["why_full"] == "RECOVERY_MODE=full"
            assert report["pulled"] == 3
            assert report["entries_read"] == 50 + 53  # both nodes' entire data
    finally:
        cluster.close()


def test_wiped_node_falls_back_to_full_sync(coord, cluster, tmp_path):
    keys = _keys_on_shard(2, 20)
    for k in keys:
        coord.put(f"/store/{k}", json={"a": 1})
    _fail(coord, cluster, "s2-replica", 2)

    # The replica's disk is lost: a brand-new, empty database (and log id).
    cluster.stores["s2-replica"].close()
    fresh = Store(tmp_path / "s2-replica-wiped.db")
    cluster.node_apps["s2-replica"].state.node.store = fresh
    cluster.stores["s2-replica"] = fresh

    report = _restart_and_recover(coord, cluster, "s2-replica")
    assert report["mode"] == "full" and report["why_full"] == "first catch-up between these two logs"
    assert report["pulled"] == 20
    assert fresh.sync_entries() == cluster.stores["s2-primary"].sync_entries()

    # ...and the next catch-up can use the log again.
    _fail(coord, cluster, "s2-replica", 2)
    coord.put(f"/store/{keys[0]}", json={"a": 2})
    report = _restart_and_recover(coord, cluster, "s2-replica")
    assert report["mode"] == "log" and report["entries_read"] == 1


def test_a_write_that_was_never_replicated_is_still_caught_up(coord, cluster):
    """A write lands on the primary but its replica copy never goes out (say the
    coordinator crashed in between). Later writes ARE replicated, but the
    replica's watermark must stay behind the missing one, so the next catch-up
    still fetches it."""
    lost, later = _keys_on_shard(0, 2, "gap")
    cluster.stores["s0-primary"].put(lost, {"never": "replicated"})  # straight to the node
    coord.put(f"/store/{later}", json={"a": 1})  # replicated normally

    replica = cluster.stores["s0-replica"]
    primary_log = cluster.stores["s0-primary"].log_id
    assert replica.position()["watermarks"][primary_log] < cluster.stores["s0-primary"].position()["head"]
    assert replica.get(lost) is None

    _fail(coord, cluster, "s0-replica", 0)
    report = _restart_and_recover(coord, cluster, "s0-replica")
    assert report["mode"] == "log"
    assert replica.get(lost) == {"never": "replicated", "version": 1}
    assert replica.position()["watermarks"][primary_log] == cluster.stores["s0-primary"].position()["head"]


def test_fresher_node_returning_second_pushes_its_log(coord, cluster):
    """Whole shard down: the staler node leads first; the fresher one returns
    and its own log writes are pushed to the leader."""
    key = _keys_on_shard(1, 1, "fresh")[0]
    coord.put(f"/store/{key}", json={"v": 1})
    _fail(coord, cluster, "s1-replica", 1)
    coord.put(f"/store/{key}", json={"v": 2})  # on the primary only
    _fail(coord, cluster, "s1-primary", 1)

    cluster.net.dead.discard("s1-replica")
    assert cluster.register(coord, "s1-replica") == "UP"  # no partner: leads with v1
    report = _restart_and_recover(coord, cluster, "s1-primary")
    assert report["mode"] == "log" and report["pushed"] == 1
    assert coord.get(f"/store/{key}").json() == {"v": 2, "version": 2}
