import pytest

from coordinator.app.registry import DOWN, UP, Registry, RegistrationError


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
    reg.register(f"s{shard}-p", shard, "primary", "http://p")
    reg.register(f"s{shard}-r", shard, "replica", "http://r")


def test_register_and_acting_primary(reg):
    _both(reg)
    assert reg.acting_primary(0).node_id == "s0-p"
    assert reg.acting_primary(1) is None  # nobody registered there


def test_failover_to_replica_and_back(reg):
    _both(reg)
    reg.mark_down("s0-p", "test")
    assert reg.acting_primary(0).node_id == "s0-r"
    reg.register("s0-p", 0, "primary", "http://p")  # comes back
    assert reg.acting_primary(0).node_id == "s0-p"


def test_no_live_node(reg):
    _both(reg)
    reg.mark_down("s0-p", "x")
    reg.mark_down("s0-r", "x")
    assert reg.acting_primary(0) is None


def test_heartbeat_timeout(reg):
    _both(reg)
    reg.clock.t += 5
    assert reg.heartbeat("s0-r")
    reg.clock.t += 6  # primary silent for 11 s, replica for 6 s
    assert reg.sweep() == ["s0-p"]
    states = {n["node_id"]: n["state"] for n in reg.snapshot()}
    assert states == {"s0-p": DOWN, "s0-r": UP}


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
    reg.register("s0-p", 0, "primary", "http://new-ip")  # same node, new address: fine
    assert reg.acting_primary(0).url == "http://new-ip"
