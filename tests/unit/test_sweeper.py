"""The sweeper: requests that were saved but never queued still get matched.
Time is controlled by passing a fake clock, so nothing here waits."""

import calendar
import time

import pytest

from common.sharding import request_key
from common.storage_client import StorageUnavailable, VersionConflict
from matching_service.app import sweeper as sweeper_module
from matching_service.app.sweeper import Sweeper

SAVED_AT = calendar.timegm(time.strptime("2026-11-09T10:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))


def sweeper_at(store, seconds_after_save, stale_after_s=60.0):
    return Sweeper(store, interval_s=0, stale_after_s=stale_after_s, clock=lambda: SAVED_AT + seconds_after_save)


def status_of(store, rid):
    return store.get(request_key(rid))["status"]


def test_an_old_pending_request_is_matched(fake_store, add_request, add_resource):
    add_resource("res-1")
    add_request("req-1")

    assert sweeper_at(fake_store, 120).sweep_once() == 1

    request = fake_store.get(request_key("req-1"))
    assert request["status"] == "MATCHED" and request["assigned_resource_id"] == "res-1"


def test_a_recent_pending_request_is_left_to_the_queue(fake_store, add_request, add_resource):
    add_resource("res-1")
    add_request("req-1")

    assert sweeper_at(fake_store, 10).sweep_once() == 0
    assert status_of(fake_store, "req-1") == "PENDING"


def test_only_pending_requests_are_touched(fake_store, add_request, add_resource):
    add_resource("res-1")
    add_request("req-matched", status="MATCHED")
    add_request("req-cancelled", status="CANCELLED")
    add_request("req-unmatched", status="UNMATCHED")

    assert sweeper_at(fake_store, 600).sweep_once() == 0
    assert [status_of(fake_store, r) for r in ("req-matched", "req-cancelled", "req-unmatched")] == [
        "MATCHED", "CANCELLED", "UNMATCHED",
    ]


def test_with_nobody_to_help_the_request_becomes_unmatched(fake_store, add_request):
    add_request("req-1")

    assert sweeper_at(fake_store, 120).sweep_once() == 1
    assert status_of(fake_store, "req-1") == "UNMATCHED"


def test_a_request_without_a_readable_time_is_skipped(fake_store, add_request):
    add_request("req-1", updated_at="not a time")

    assert sweeper_at(fake_store, 600).sweep_once() == 0
    assert status_of(fake_store, "req-1") == "PENDING"


def test_one_failing_request_does_not_stop_the_others(fake_store, add_request, add_resource, monkeypatch):
    add_resource("res-1")
    add_request("req-1")
    add_request("req-2")
    real_match = sweeper_module.match_request

    def flaky(store, request_id):
        if request_id == "req-1":
            raise RuntimeError("boom")
        return real_match(store, request_id)

    monkeypatch.setattr(sweeper_module, "match_request", flaky)

    assert sweeper_at(fake_store, 120).sweep_once() == 1
    assert status_of(fake_store, "req-1") == "PENDING"
    assert status_of(fake_store, "req-2") == "MATCHED"


def test_a_conflict_means_someone_else_is_handling_it(fake_store, add_request, monkeypatch):
    add_request("req-1")

    def conflict(store, request_id):
        raise VersionConflict("changed", 409)

    monkeypatch.setattr(sweeper_module, "match_request", conflict)

    assert sweeper_at(fake_store, 120).sweep_once() == 0


def test_the_loop_survives_a_storage_outage_and_stops_on_request():
    class DownStore:
        calls = 0

        def query(self, *args, **kwargs):
            DownStore.calls += 1
            if DownStore.calls == 3:
                sweeper.stop()
            raise StorageUnavailable("storage is down")

    sweeper = Sweeper(DownStore(), interval_s=0, stale_after_s=60)
    sweeper.run()  # returns by itself once stop() has been called

    assert DownStore.calls == 3


@pytest.mark.parametrize("name", ["SWEEP_INTERVAL_S", "PENDING_STALE_AFTER_S"])
def test_settings_have_sensible_defaults(name):
    from common import config

    assert getattr(config, name) > 0
