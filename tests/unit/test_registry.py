import pytest

from coordinator.app.registry import DOWN, RECOVERING, UP, Registry, RegistrationError


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def reg():
    clock = Clock()
    r = Registry(num_shards=3, timeout_s=10, clock=clock)
    r.clock = clock
    return r


def _both(reg, shard=0):
    """Register a shard's primary and replica and finish the replica's catch-up."""
    reg.register(f"s{shard}-p", shard, "primary", "http://p")
    r = reg.register(f"s{shard}-r", shard, "replica", "http://r")
    assert r.state == RECOVERING  # partner was UP -> must catch up first
    assert reg.finish_recovery(r.node_id, r.generation, {}) is True


def _states(reg):
    return {n["node_id"]: n["state"] for n in reg.snapshot()}


def test_first_node_of_a_shard_starts_up(reg):
    assert reg.register("s0-p", 0, "primary", "http://p").state == UP


def test_recovering_node_gets_replication_but_no_traffic(reg):
    reg.register("s0-p", 0, "primary", "http://p")
    reg.register("s0-r", 0, "replica", "http://r")
    assert [n.node_id for n in reg.live_nodes(0)] == ["s0-p"]
    assert [n.node_id for n in reg.replication_targets(0, "s0-p")] == ["s0-r"]


def test_register_and_acting_primary(reg):
    _both(reg)
    assert reg.acting_primary(0).node_id == "s0-p"
    assert reg.acting_primary(1) is None  # nobody registered there


def test_failover_is_sticky(reg):
    _both(reg)
    reg.mark_down("s0-p", "test")
    assert reg.acting_primary(0).node_id == "s0-r"
    back = reg.register("s0-p", 0, "primary", "http://p")
    assert back.state == RECOVERING
    reg.finish_recovery("s0-p", back.generation, {})
    # Caught up and UP, but the replica keeps leading: no failback.
    assert _states(reg)["s0-p"] == UP
    assert reg.acting_primary(0).node_id == "s0-r"
    assert [n.node_id for n in reg.live_nodes(0)] == ["s0-r", "s0-p"]
    # Leadership moves only when the leader fails.
    reg.mark_down("s0-r", "test")
    assert reg.acting_primary(0).node_id == "s0-p"


def test_stale_recovery_cannot_finish(reg):
    _both(reg)
    reg.mark_down("s0-p", "x")
    first = reg.register("s0-p", 0, "primary", "http://p")
    second = reg.register("s0-p", 0, "primary", "http://p")  # restarted again mid-recovery
    assert reg.finish_recovery("s0-p", first.generation, {}) is False
    assert _states(reg)["s0-p"] == RECOVERING
    assert reg.finish_recovery("s0-p", second.generation, {}) is True


def test_no_live_node(reg):
    _both(reg)
    reg.mark_down("s0-p", "x")
    reg.mark_down("s0-r", "x")
    assert reg.acting_primary(0) is None
    assert reg.live_nodes(0) == []


def test_heartbeat_timeout(reg):
    _both(reg)
    reg.clock.t += 5
    assert reg.heartbeat("s0-r")
    reg.clock.t += 6  # primary silent for 11 s, replica for 6 s
    assert reg.sweep() == ["s0-p"]
    assert _states(reg) == {"s0-p": DOWN, "s0-r": UP}


def test_recovering_node_can_time_out(reg):
    reg.register("s0-p", 0, "primary", "http://p")
    reg.register("s0-r", 0, "replica", "http://r")
    reg.clock.t += 5
    reg.heartbeat("s0-p")
    reg.clock.t += 6
    assert reg.sweep() == ["s0-r"]


def test_down_node_must_reregister(reg):
    _both(reg)
    reg.mark_down("s0-p", "x")
    assert reg.heartbeat("s0-p") is False  # -> 404 -> node registers again
    assert reg.heartbeat("unknown") is False


def test_slot_taken_and_bad_input(reg):
    _both(reg)
    with pytest.raises(RegistrationError) as exc:
        reg.register("intruder", 0, "primary", "http://x")
    assert exc.value.status_code == 409
    with pytest.raises(RegistrationError) as exc:
        reg.register("x", 3, "primary", "http://x")
    assert exc.value.status_code == 400
    with pytest.raises(RegistrationError):
        reg.register("x", 1, "leader", "http://x")
    reg.register("s1-p", 1, "primary", "http://old-ip")
    reg.register("s1-p", 1, "primary", "http://new-ip")  # same node, new address: fine
    assert reg.acting_primary(1).url == "http://new-ip"
