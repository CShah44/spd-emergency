"""Roadmap task 3: two coordinators, one leader, fencing, and a safe takeover."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from common.sharding import shard_for
from conftest import Cluster
from coordinator.app.election import LeaderElector, LeaseConflict, MemoryLeaseBackend
from coordinator.app.leadership import fresher_first
from coordinator.app.main import create_app as create_coordinator
from storage_node.app.db import Store


# -- the election on its own (fake clock, in-memory lease) ---------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _elector(backend, name, clock):
    return LeaderElector(backend, name, lease_duration_s=6, renew_deadline_s=4, retry_period_s=1, clock=clock)


def _tick(*electors):
    for e in electors:
        asyncio.run(e.tick())


def test_one_leader_and_the_standby_waits_for_the_lease_to_expire():
    lease, clock = MemoryLeaseBackend(), Clock()
    a, b = _elector(lease, "a", clock), _elector(lease, "b", clock)
    _tick(a, b)
    assert a.is_leader() and not b.is_leader() and a.epoch == 0

    clock.now += 5  # a has died (stops renewing); 5 s < 6 s lease: b still waits
    _tick(b)
    assert not b.is_leader()
    assert not a.is_leader()  # a's own view: no renew for 5 s > 4 s deadline -> not leading

    clock.now += 1.5
    _tick(b)
    assert b.is_leader() and b.epoch == 1  # epoch = handovers so far


def test_renewing_keeps_leadership():
    lease, clock = MemoryLeaseBackend(), Clock()
    a, b = _elector(lease, "a", clock), _elector(lease, "b", clock)
    for _ in range(20):
        _tick(a, b)
        clock.now += 1
    assert a.is_leader() and not b.is_leader() and a.epoch == 0


def test_release_hands_over_without_waiting():
    lease, clock = MemoryLeaseBackend(), Clock()
    a, b = _elector(lease, "a", clock), _elector(lease, "b", clock)
    _tick(a, b)
    asyncio.run(a.release())
    _tick(b)
    assert b.is_leader() and b.epoch == 1


def test_leader_cut_off_from_the_lease_api_steps_down_before_the_lease_expires():
    lease, clock = MemoryLeaseBackend(), Clock()
    a = _elector(lease, "a", clock)
    _tick(a)
    lease.available = False
    clock.now += 3
    _tick(a)
    assert a.is_leader()  # 3 s < 4 s renew deadline: still leading
    clock.now += 1.5
    _tick(a)
    assert not a.is_leader()  # past the deadline, and the lease (6 s) hasn't even expired yet


def test_a_leader_that_froze_announces_it_lost():
    """Found on Kubernetes (docker pause): after waking up, is_leader() was already
    False by the clock, so 'before vs after' saw no change and the coordinator never
    dropped its registry or its leader label."""
    lease, clock = MemoryLeaseBackend(), Clock()
    events = []
    a, b = _elector(lease, "a", clock), _elector(lease, "b", clock)
    a.on_change = lambda leading, epoch: events.append((leading, epoch))
    _tick(a, b)
    clock.now += 7  # a is frozen; b takes over
    _tick(b)
    assert b.is_leader()
    _tick(a)  # a wakes up
    assert events == [(True, 0), (False, 0)]


def test_two_standbys_cannot_both_take_an_expired_lease():
    lease, clock = MemoryLeaseBackend(), Clock()
    a, b, c = (_elector(lease, n, clock) for n in "abc")
    _tick(a, b, c)
    clock.now += 7
    stale = asyncio.run(lease.get())
    asyncio.run(b.tick())  # b takes it
    with pytest.raises(LeaseConflict):  # c tries to write over the version it read before
        asyncio.run(lease.update(stale))
    _tick(c)
    assert b.is_leader() and not c.is_leader()


# -- choosing who leads a shard after a takeover ------------------------------------------

def _reg(node_id, role, log_id, head, watermarks):
    return {"node_id": node_id, "role": role, "position": {"log_id": log_id, "head": head, "watermarks": watermarks}}


def test_fresher_node_leads():
    p = _reg("p", "primary", "LP", 10, {"LR": 0})
    r = _reg("r", "replica", "LR", 0, {"LP": 7})  # missed p's writes 8..10
    assert [x["node_id"] for x in fresher_first(r, p)] == ["p", "r"]
    r_ahead = _reg("r", "replica", "LR", 3, {"LP": 10})  # r led meanwhile; p missed r's 1..3
    p_behind = _reg("p", "primary", "LP", 10, {"LR": 0})
    assert [x["node_id"] for x in fresher_first(p_behind, r_ahead)] == ["r", "p"]


def test_in_sync_or_unknown_prefers_the_configured_primary():
    p = _reg("p", "primary", "LP", 10, {"LR": 2})
    r = _reg("r", "replica", "LR", 2, {"LP": 10})
    assert fresher_first(r, p)[0]["node_id"] == "p"
    assert fresher_first({"node_id": "r", "role": "replica"}, {"node_id": "p", "role": "primary"})[0]["node_id"] == "p"


# -- fencing on a storage node ------------------------------------------------------------

def test_node_refuses_an_older_epoch_and_remembers_across_restarts(tmp_path):
    from storage_node.app.main import NodeSettings, create_app as create_node

    settings = NodeSettings(node_id="s0-primary", shard_id=0, role="primary", data_dir=str(tmp_path),
                            coordinator_url="http://c", node_url="http://n", heartbeat_enabled=False)
    with TestClient(create_node(settings)) as node:
        assert node.put("/internal/data/k", json={"a": 1}, headers={"X-Coordinator-Epoch": "3"}).status_code == 200
        r = node.put("/internal/data/k", json={"a": 2}, headers={"X-Coordinator-Epoch": "2"})
        assert r.status_code == 409 and r.json()["detail"] == {"error": "fenced", "epoch": 3, "yours": 2}
        assert node.get("/internal/data/k").json()["a"] == 1  # the old leader changed nothing
        assert node.put("/internal/data/k", json={"a": 3}).status_code == 200  # no header: no election
        assert node.get("/health").status_code == 200  # not fenced
    with TestClient(create_node(settings)) as node:  # restarted: still knows epoch 3
        assert node.put("/internal/data/k", json={"a": 4}, headers={"X-Coordinator-Epoch": "2"}).status_code == 409


def test_store_epoch_check(tmp_path):
    s = Store(tmp_path / "n.db")
    try:
        assert s.check_epoch(0) is None and s.check_epoch(5) is None
        assert s.check_epoch(4) == 5 and s.check_epoch(5) is None
    finally:
        s.close()


# -- two real coordinators on one in-memory lease -------------------------------------------

class LeaseView:
    """One coordinator's connection to the shared lease (can be cut on its own)."""

    def __init__(self, shared: MemoryLeaseBackend):
        self.shared, self.available = shared, True

    def _check(self):
        if not self.available:
            raise ConnectionError("lease API unreachable from this pod")

    async def get(self):
        self._check()
        return await self.shared.get()

    async def create(self, lease):
        self._check()
        return await self.shared.create(lease)

    async def update(self, lease):
        self._check()
        return await self.shared.update(lease)


def _coordinator(cluster, view, name):
    elector = LeaderElector(view, name, lease_duration_s=0.6, renew_deadline_s=0.4, retry_period_s=0.05)
    return create_coordinator(num_shards=3, heartbeat_timeout=1000.0, sweep_interval=0.05,
                              node_transport=cluster.net, elector=elector, settle_timeout=2.0)


def _wait(condition, timeout=5.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _register(cluster, http, node_id):
    """What a node's heartbeat loop does: register (with its log position) and
    adopt the epoch from the answer."""
    node = cluster.node_apps[node_id].state.node
    resp = http.post("/internal/nodes/register", json=node.registration())
    assert resp.status_code == 200, resp.text
    node.learn_epoch(resp.json())
    return resp.json()["state"]


def _key_on_shard(shard, tag):
    i = 0
    while shard_for(f"resource:{tag}{i}") != shard:
        i += 1
    return f"resource:{tag}{i}"


@pytest.fixture
def ha(tmp_path):
    cluster = Cluster(tmp_path)
    shared = MemoryLeaseBackend()
    view_a, view_b = LeaseView(shared), LeaseView(shared)
    app_a = _coordinator(cluster, view_a, "coord-a")
    app_b = _coordinator(cluster, view_b, "coord-b")
    with TestClient(app_a) as a:
        _wait(lambda: a.get("/ready").status_code == 200, what="coord-a to lead")
        with TestClient(app_b) as b:
            yield cluster, a, b, app_a, app_b, view_a
    cluster.close()


def _register_all(cluster, http):
    for node_id in cluster.node_apps:
        assert _register(cluster, http, node_id) == "SETTLING"
    _wait(lambda: not http.get("/internal/leader").json()["settling"], what="settling")
    _wait(lambda: set(cluster.states(http).values()) == {"UP"}, what="all nodes UP")


def test_only_the_leader_serves(ha):
    cluster, a, b, *_ = ha
    _register_all(cluster, a)
    key = _key_on_shard(0, "k")
    assert a.put(f"/store/{key}", json={"x": 1}).status_code == 200
    assert b.get("/ready").status_code == 503 and b.get("/internal/leader").json()["is_leader"] is False
    assert b.put(f"/store/{key}", json={"x": 2}).status_code == 503
    assert b.post("/internal/nodes/register", json={}).status_code == 503
    assert a.get(f"/store/{key}").json()["x"] == 1


def test_settling_leader_holds_data_calls_until_nodes_are_back(ha):
    cluster, a, *_ = ha
    assert _register(cluster, a, "s0-primary") == "SETTLING"
    assert a.get(f"/store/{_key_on_shard(0, 'k')}").status_code == 503  # not serving yet
    assert a.post("/internal/nodes/heartbeat", json={"node_id": "s0-primary"}).status_code == 200  # held


def test_takeover_fences_the_old_leader_and_picks_the_freshest_node(ha):
    cluster, a, b, app_a, app_b, view_a = ha
    _register_all(cluster, a)
    k_old, k_missed = _key_on_shard(0, "old"), _key_on_shard(0, "missed")
    assert a.put(f"/store/{k_old}", json={"v": 1}).status_code == 200

    # s0-PRIMARY fails and misses a write (the replica takes it): the configured
    # primary is now the STALE node, so "primary first" would be the wrong choice.
    cluster.net.dead.add("s0-primary")
    app_a.state.registry.mark_down("s0-primary", "test: killed")
    assert a.put(f"/store/{k_missed}", json={"v": 1}).status_code == 200

    # coord-a is cut off from the Kubernetes API (or frozen): it stops renewing.
    view_a.available = False
    _wait(lambda: b.get("/ready").status_code == 200, what="coord-b to take over")
    assert a.get("/ready").status_code == 503  # a stepped down by itself (renew deadline)
    assert b.get("/internal/leader").json()["epoch"] == 1

    # Every node registers with the new leader. The STALE primary happens to come first:
    # without settling it would lead the shard and serve old data.
    cluster.net.dead.discard("s0-primary")
    order = ["s0-primary", "s0-replica", "s1-primary", "s1-replica", "s2-primary", "s2-replica"]
    for node_id in order:
        assert _register(cluster, b, node_id) == "SETTLING"
    _wait(lambda: set(cluster.states(b).values()) == {"UP"}, what="all nodes UP under coord-b")
    status = b.get("/internal/status").json()
    assert status["shards"]["0"]["acting_primary"] == "s0-replica"  # the fresh one leads
    assert status["coordinator"]["last_takeover"]["leaders"]["0"] == "s0-replica"
    assert status["shards"]["1"]["acting_primary"] == "s1-primary"  # in sync: the configured primary
    assert cluster.stores["s0-primary"].get(k_missed) == {"v": 1, "version": 1}  # caught up
    assert b.put(f"/store/{k_old}", json={"v": 2}, headers={"If-Match": "1"}).status_code == 200

    # coord-a wakes up believing it still leads (it froze mid-way) and tries a write
    # with its old epoch 0. The node knows epoch 1 and refuses it.
    leader_a = app_a.state.leadership
    leader_a.elector._leading, leader_a.elector._last_renew = True, leader_a.elector._clock()
    for node_id in order:
        app_a.state.registry.register(node_id, int(node_id[1]), node_id.split("-")[1], f"http://{node_id}")
    r = a.put(f"/store/{k_old}", json={"v": 99}, headers={"If-Match": "2"})
    assert r.status_code == 503 and "no longer the leader" in r.json()["detail"]
    assert leader_a.is_leader() is False  # and it stepped down
    assert b.get(f"/store/{k_old}").json() == {"v": 2, "version": 2}  # nothing changed
    assert cluster.stores["s0-replica"].get(k_old)["v"] == 2
    assert cluster.stores["s0-primary"].get(k_old)["v"] == 2


def test_clean_shutdown_hands_over_quickly(tmp_path):
    cluster = Cluster(tmp_path)
    shared = MemoryLeaseBackend()
    a = TestClient(_coordinator(cluster, LeaseView(shared), "coord-a"))
    b = TestClient(_coordinator(cluster, LeaseView(shared), "coord-b"))
    try:
        a.__enter__()
        _wait(lambda: a.get("/ready").status_code == 200, what="coord-a to lead")
        b.__enter__()
        time.sleep(0.2)
        assert b.get("/ready").status_code == 503

        a.__exit__(None, None, None)  # clean shutdown (a pod being deleted): releases the lease
        started = time.monotonic()
        _wait(lambda: b.get("/ready").status_code == 200, timeout=2.0, what="coord-b to take over")
        assert time.monotonic() - started < 0.3  # no waiting for the 0.6 s lease to run out
        assert b.get("/internal/leader").json()["epoch"] == 1
    finally:
        b.__exit__(None, None, None)
        cluster.close()
