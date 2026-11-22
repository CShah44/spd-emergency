"""How chaos.py decides the system has recovered. Nothing is really broken: the platform and
the clock are stand-ins."""

import chaos


class FakePlatform(chaos.Platform):
    name = "fake"

    def __init__(self, ready_after_polls=0):
        self.crashed = []
        self.polls = 0
        self.ready_after_polls = ready_after_polls

    def crash(self, service):
        self.crashed.append(service)
        return f"{service}-0"

    def all_ready(self, service):
        self.polls += 1
        return self.polls > self.ready_after_polls


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(platform, listening, name="kill-matcher", timeout_s=60.0):
    """`listening` is the list of consumer counts successive reads return (the last one repeats)."""
    clock, reads = Clock(), iter(listening)
    last = [listening[-1]]

    def read():
        last[0] = next(reads, last[0])
        return last[0]

    return chaos.run(name, platform, read_consumers=read, timeout_s=timeout_s, interval_s=1.0, clock=clock,
                     sleep=clock.sleep, log=lambda *_: None)


def test_the_scenario_crashes_the_right_service():
    platform = FakePlatform()

    run(platform, [2, 2])
    run(platform, [1, 1], name="kill-rabbitmq")

    assert platform.crashed == ["matching-service", "rabbitmq"]


def test_recovery_is_when_the_matchers_listen_again_in_the_same_number():
    # before: 2 matchers. After the crash: 1, 1, 0 (the broker restarting), then 1, then 2 again.
    outcome = run(FakePlatform(), [2, 1, 1, 0, 1, 2])

    assert outcome.recovered_after_s == 4.0 and "2 matcher" in outcome.detail


def test_all_pods_must_be_ready_as_well():
    outcome = run(FakePlatform(ready_after_polls=3), [2, 2, 2, 2, 2, 2])

    assert outcome.recovered_after_s == 3.0


def test_a_system_that_never_recovers_reports_none():
    outcome = run(FakePlatform(), [2, 0], timeout_s=10)

    assert outcome.recovered_after_s is None and "not recovered" in outcome.detail


def test_without_metrics_recovery_means_the_pods_are_ready():
    outcome = run(FakePlatform(ready_after_polls=2), [None])

    assert outcome.recovered_after_s == 2.0


def test_every_scenario_names_a_service_and_says_what_it_does():
    assert set(chaos.SCENARIOS) >= {"kill-matcher", "kill-rabbitmq"}
    assert all(s.service and s.description for s in chaos.SCENARIOS.values())
