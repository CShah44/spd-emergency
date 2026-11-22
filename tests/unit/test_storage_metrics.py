"""Roadmap task 1, storage side [A]: what the coordinator and storage nodes report."""

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from common import metrics
from common.sharding import shard_for
from coordinator.app.registry import Registry
from coordinator.app.state_metrics import StateMetrics
from storage_node.app.main import NodeSettings, create_app as create_node


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


def value(name, **labels):
    return sample(name, **labels) or 0.0


def _keys_on_shard(shard, n, tag):
    out, i = [], 0
    while len(out) < n:
        if shard_for(f"resource:{tag}{i}") == shard:
            out.append(f"resource:{tag}{i}")
        i += 1
    return out


def _refresh(coord):
    """Run the coordinator's gauge refresh now, in its own event loop (normally every 2 s)."""
    sm = coord.app.state.state_metrics
    sm.refresh()
    coord.portal.call(sm.refresh_lag)


# -- storage node ------------------------------------------------------------------------------

@pytest.fixture
def node(tmp_path):
    settings = NodeSettings(node_id="m-node", shard_id=0, role="primary", data_dir=str(tmp_path),
                            coordinator_url="http://c", node_url="http://n", heartbeat_enabled=False)
    with TestClient(create_node(settings)) as client:
        yield client


def test_storage_node_serves_metrics_and_counts_its_keys(node):
    node.put("/internal/data/a", json={"x": 1})
    node.put("/internal/data/b", json={"x": 1})
    metrics.STORAGE_KEYS.labels("m-node").set(node.app.state.node.store.count())  # what the 5 s loop does
    text = node.get("/metrics").text
    assert 'spd_storage_keys{node="m-node"} 2.0' in text
    assert 'spd_http_requests_total{route="/internal/data/{key}",service="storage-node",status="200"}' in text


def test_storage_node_counts_calls_it_fenced(node):
    before = value("spd_fenced_total", node="m-node")
    node.put("/internal/data/a", json={"x": 1}, headers={"X-Coordinator-Epoch": "5"})
    assert node.put("/internal/data/a", json={"x": 2}, headers={"X-Coordinator-Epoch": "4"}).status_code == 409
    assert value("spd_fenced_total", node="m-node") - before == 1


# -- coordinator ---------------------------------------------------------------------------------

def test_node_states_and_shards(coord, cluster):
    _refresh(coord)
    for node_id in cluster.node_apps:
        shard = node_id[1]
        assert sample("spd_node_state", node=node_id, shard=shard, state="UP") == 1
        assert sample("spd_node_state", node=node_id, shard=shard, state="DOWN") == 0
    assert all(sample("spd_shard_available", shard=s) == 1 for s in "012")
    assert sample("spd_coordinator_is_leader", pod=coord.app.state.state_metrics.pod) == 1

    cluster.net.dead.update({"s1-primary", "s1-replica"})
    coord.app.state.registry.mark_down("s1-primary", "test")
    coord.app.state.registry.mark_down("s1-replica", "test")
    _refresh(coord)
    assert sample("spd_node_state", node="s1-primary", shard="1", state="DOWN") == 1
    assert sample("spd_shard_available", shard="1") == 0


def test_replication_failure_is_counted(coord, cluster):
    before = value("spd_replication_failures_total", shard="2")
    cluster.net.dead.add("s2-replica")
    coord.put(f"/store/{_keys_on_shard(2, 1, 'rf')[0]}", json={"a": 1})  # replica can't confirm
    assert value("spd_replication_failures_total", shard="2") - before == 1


def test_replication_lag_shows_a_write_the_replica_never_got(coord, cluster):
    lost, later = _keys_on_shard(0, 2, "lag")
    coord.put(f"/store/{later}", json={"a": 1})
    _refresh(coord)
    assert sample("spd_replication_lag_writes", shard="0", node="s0-replica") == 0
    assert sample("spd_replication_lag_writes", shard="0", node="s0-primary") == 0

    cluster.stores["s0-primary"].put(lost, {"never": "replicated"})  # straight to the node
    _refresh(coord)
    assert sample("spd_replication_lag_writes", shard="0", node="s0-replica") == 1

    cluster.net.dead.add("s0-replica")
    coord.app.state.registry.mark_down("s0-replica", "test")
    _refresh(coord)  # a shard with one live node: no lag to show, the series goes away
    assert sample("spd_replication_lag_writes", shard="0", node="s0-replica") is None


def test_catch_up_is_counted_with_its_mode(coord, cluster):
    before_log = value("spd_recovery_total", node="s2-primary", mode="log")
    before_count = value("spd_recovery_seconds_count", node="s2-primary")
    before_copied = value("spd_recovery_entries_copied_total", node="s2-primary")
    cluster.net.dead.add("s2-primary")
    coord.app.state.registry.mark_down("s2-primary", "test")
    for k in _keys_on_shard(2, 3, "rc"):
        coord.put(f"/store/{k}", json={"a": 1})
    cluster.net.dead.discard("s2-primary")
    cluster.register(coord, "s2-primary")
    cluster.wait_state(coord, "s2-primary", "UP")

    assert value("spd_recovery_total", node="s2-primary", mode="log") - before_log == 1
    assert value("spd_recovery_seconds_count", node="s2-primary") - before_count == 1
    assert value("spd_recovery_entries_copied_total", node="s2-primary") - before_copied == 3


def test_counters_exist_at_zero_before_the_first_event(coord, cluster):
    """Found under chaos load: Prometheus' increase() can't see the first step of a
    series born at 1, so the first catch-up of a node was missing from the dashboard."""
    for node_id in cluster.node_apps:
        assert sample("spd_recovery_total", node=node_id, mode="full") is not None
        assert sample("spd_recovery_entries_copied_total", node=node_id) is not None
    assert all(sample("spd_replication_failures_total", shard=s) is not None for s in "012")


def test_queries_record_how_many_shards_they_asked(coord):
    before = value("spd_shards_per_query_count")
    coord.get("/store", params={"prefix": "resource:"})
    assert value("spd_shards_per_query_count") - before == 1


def test_coordinator_serves_metrics(coord):
    text = coord.get("/metrics").text
    assert "spd_node_state" in text and "spd_coordinator_is_leader" in text


# -- a standby reports no cluster state -------------------------------------------------------

class _Leadership:
    def __init__(self):
        self.leading, self.epoch = True, 7

    def is_leader(self):
        return self.leading

    def can_serve_data(self):
        return self.leading


def test_a_coordinator_that_stops_leading_removes_its_node_gauges():
    registry = Registry(num_shards=1, timeout_s=100)
    registry.register("x-primary", 0, "primary", "http://x")
    leadership = _Leadership()
    sm = StateMetrics(registry, leadership, http=lambda: None, pod="pod-x")

    sm.refresh()
    assert sample("spd_node_state", node="x-primary", shard="0", state="UP") == 1
    assert sample("spd_coordinator_epoch", pod="pod-x") == 7

    leadership.leading = False
    sm.refresh()
    assert sample("spd_node_state", node="x-primary", shard="0", state="UP") is None  # gone, not stale
    assert sample("spd_coordinator_is_leader", pod="pod-x") == 0
