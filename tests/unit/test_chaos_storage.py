"""The storage and coordinator chaos scenarios [A]: what they break and when they call it
recovered. Nothing is really broken: the platform replays a script of coordinator answers."""

import chaos

NODES = chaos.STORAGE_NODES


def status(states=None, generations=None, leaders=None, recovery=None, coordinator=None):
    """A minimal /internal/status. Every node UP at generation 1 unless told otherwise."""
    states, generations, recovery = states or {}, generations or {}, recovery or {}
    leaders = leaders or {"0": "s0-primary", "1": "s1-primary", "2": "s2-primary"}
    return {
        "coordinator": coordinator or {"election": "off", "epoch": None, "settling": False},
        "nodes": [{"node_id": n, "state": states.get(n, "UP"), "generation": generations.get(n, 1),
                   "last_recovery": recovery.get(n)} for n in NODES],
        "shards": {s: {"acting_primary": leader} for s, leader in leaders.items()},
    }


class ScriptedPlatform(chaos.Platform):
    """Each coordinator_status() call returns the next answer (the last one repeats)."""

    name = "scripted"

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def coordinator_status(self):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def crash_storage_node(self, node_id):
        self.calls.append(("crash", node_id))
        return node_id

    def set_paused(self, node_id, paused):
        self.calls.append(("pause" if paused else "resume", node_id))

    def crash_coordinator_leader(self):
        self.calls.append(("crash-coordinator",))
        return "leader"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(name, platform, timeout_s=60.0):
    clock = Clock()
    return chaos.run(name, platform, timeout_s=timeout_s, interval_s=1.0, clock=clock, sleep=clock.sleep,
                     log=lambda *_: None)


LOG_CATCH_UP = {"partner": "s0-replica", "mode": "log", "pulled": 12, "seconds": 0.04}


def test_kill_storage_leader_crashes_the_node_leading_shard_0():
    # Shard 0 is led by its replica (after an earlier failover): that is the one to crash.
    before = status(leaders={"0": "s0-replica", "1": "s1-primary", "2": "s2-primary"})
    platform = ScriptedPlatform([before, before])
    run("kill-storage-leader", platform, timeout_s=3)
    assert platform.calls == [("crash", "s0-replica")]


def test_storage_node_is_recovered_only_after_it_registered_again_and_everyone_is_up():
    platform = ScriptedPlatform([
        status(),                                                   # before the crash
        status(),                                                   # not noticed yet: NOT recovered
        status(states={"s0-primary": "DOWN"}),                      # noticed
        status(states={"s0-primary": "RECOVERING"}, generations={"s0-primary": 2}),
        status(generations={"s0-primary": 2}, recovery={"s0-primary": LOG_CATCH_UP},
               leaders={"0": "s0-replica", "1": "s1-primary", "2": "s2-primary"}),
    ])
    outcome = run("kill-storage-leader", platform)
    assert outcome.recovered_after_s == 3.0
    assert "from the operation log: 12 write(s)" in outcome.detail and "led by s0-replica" in outcome.detail


def test_a_quick_restart_the_coordinator_never_saw_still_counts():
    # The pod came back and re-registered before the coordinator noticed it was gone:
    # never DOWN, but a new generation. That is a recovery.
    platform = ScriptedPlatform([status(), status(generations={"s0-primary": 2}, recovery={"s0-primary": LOG_CATCH_UP})])
    assert run("kill-storage-leader", platform).recovered_after_s == 0.0


def test_another_node_down_means_not_recovered():
    platform = ScriptedPlatform([status(), status(generations={"s0-primary": 2}, states={"s2-replica": "DOWN"})])
    outcome = run("kill-storage-leader", platform, timeout_s=5)
    assert outcome.recovered_after_s is None


def test_pause_freezes_a_follower_of_shard_1_then_resumes_it():
    before = status(leaders={"0": "s0-primary", "1": "s1-primary", "2": "s2-primary"})
    after = status(generations={"s1-replica": 2}, recovery={"s1-replica": {**LOG_CATCH_UP, "partner": "s1-primary"}})
    platform = ScriptedPlatform([before, after])
    outcome = run("pause-storage-node", platform)
    assert platform.calls == [("pause", "s1-replica"), ("resume", "s1-replica")]
    assert outcome.recovered_after_s == chaos.PAUSE_FOR_S  # frozen time counts: it is the outage
    assert "s1-replica caught up from the operation log" in outcome.detail


def test_full_sync_is_reported_as_such():
    full = {"partner": "s0-replica", "mode": "full", "why_full": "first catch-up between these two logs",
            "pulled": 700, "seconds": 0.9}
    platform = ScriptedPlatform([status(), status(generations={"s0-primary": 2}, recovery={"s0-primary": full})])
    assert "by full sync (first catch-up" in run("kill-storage-leader", platform).detail


def test_coordinator_leader_is_recovered_when_a_newer_leader_has_settled():
    on = lambda epoch, settling=False: {"election": "on", "epoch": epoch, "identity": f"coord-{epoch}",  # noqa: E731
                                        "settling": settling, "last_takeover": {"settle_seconds": 3.1}}
    platform = ScriptedPlatform([
        status(coordinator=on(4)),                     # before
        None,                                          # nobody answering
        status(coordinator=on(4)),                     # still the old term (not taken over yet)
        status(coordinator=on(5, settling=True)),      # new leader, settling
        status(coordinator=on(5)),                     # serving
    ])
    outcome = run("kill-coordinator-leader", platform)
    assert platform.calls == [("crash-coordinator",)]
    assert outcome.recovered_after_s == 3.0 and "epoch 4 -> 5" in outcome.detail and "3.1 s" in outcome.detail


def test_every_scenario_is_listed_with_a_description():
    assert {"kill-storage-leader", "pause-storage-node", "kill-coordinator-leader"} <= set(chaos.SCENARIOS)


def test_storage_pod_names():
    assert chaos.storage_pod("s0-primary") == "shard0-0" and chaos.storage_pod("s2-replica") == "shard2-1"
