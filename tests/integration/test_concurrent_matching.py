"""Spec section 11 against the live stack: multiple simultaneous requests, and
RabbitMQ delivery and processing. Run it against a stack with several matching
services (docker compose up -d --scale matching-service=3) to prove that no
responder is given to two requests even when matchers race."""

import time

import pytest

pytestmark = pytest.mark.integration


def test_many_simultaneous_requests_never_share_a_responder(app):
    resources, request_ids, _ = app.submit_batch(n_resources=15, n_requests=40)

    assert app.wait_until_processed(request_ids, 120) is not None, "the queue did not drain in time"

    requests = [app.get_request(i) for i in request_ids]
    matched = [r for r in requests if r["status"] == "MATCHED"]
    assert len(matched) == 15  # exactly as many matches as there were responders
    assert len([r for r in requests if r["status"] == "UNMATCHED"]) == 25
    assigned = [r["assigned_resource_id"] for r in matched]
    assert len(set(assigned)) == len(assigned), "a responder was assigned to two requests"
    assert set(assigned) <= {r["id"] for r in resources}


def test_every_message_is_consumed(app):
    _, request_ids, _ = app.submit_batch(n_resources=5, n_requests=25)
    assert app.wait_until_processed(request_ids, 120) is not None

    depth = app.queue_depth()  # None if the RabbitMQ management API is not reachable
    if depth is not None:
        # RabbitMQ's management API refreshes its counters only every few seconds, so
        # the first reading can still show messages that were just consumed.
        deadline = time.monotonic() + 20
        while depth and time.monotonic() < deadline:
            time.sleep(1)
            depth = app.queue_depth()
        assert depth == 0, "messages are left in the queue"
