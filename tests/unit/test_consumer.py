"""The RabbitMQ side of the matching service: how each message is acknowledged
(DESIGN.md section 7), and the message format itself. No RabbitMQ needed: the
consumer is handed a stand-in channel that records what it was told."""

import httpx
import pytest
from fastapi.testclient import TestClient

from common.messaging import DEAD_LETTER_QUEUE, QUEUE, declare_topology, decode_message, encode_message
from common.sharding import request_key
from common.storage_client import StorageClient
from matching_service.app.consumer import Consumer
from matching_service.app.main import create_app


class FakeChannel:
    def __init__(self):
        self.calls: list[tuple] = []

    def basic_ack(self, delivery_tag):
        self.calls.append(("ack", delivery_tag))

    def basic_nack(self, delivery_tag, requeue):
        self.calls.append(("nack", delivery_tag, requeue))

    def basic_publish(self, exchange, routing_key, body, properties):
        self.calls.append(("publish", exchange, routing_key, decode_message(body), properties.delivery_mode))


@pytest.fixture
def channel():
    return FakeChannel()


@pytest.fixture
def consumer(fake_store):
    return Consumer(fake_store, url="amqp://unused", retry_delay_s=0, max_attempts=3)


# -- message format --------------------------------------------------------------------------------

def test_message_round_trip():
    assert decode_message(encode_message("req-1")) == ("req-1", 1)
    assert decode_message(encode_message("req-1", attempt=3)) == ("req-1", 3)
    assert decode_message(b'{"request_id": "req-1"}') == ("req-1", 1)  # attempt is optional


@pytest.mark.parametrize("body", [b"", b"nope", b"[]", b"{}", b'{"request_id": 5}', b'{"request_id": ""}',
                                  b'{"request_id": "req-1", "attempt": "x"}'])
def test_malformed_messages_are_rejected(body):
    with pytest.raises(ValueError):
        decode_message(body)


def test_topology_declares_the_queue_with_a_dead_letter_queue():
    class Recorder:
        def __init__(self):
            self.queues = {}
            self.bindings = []

        def exchange_declare(self, name, exchange_type, durable):
            assert durable

        def queue_declare(self, name, durable, arguments=None):
            assert durable
            self.queues[name] = arguments

        def queue_bind(self, queue, exchange, routing_key):
            self.bindings.append((queue, exchange, routing_key))

    channel = Recorder()
    declare_topology(channel)
    assert set(channel.queues) == {QUEUE, DEAD_LETTER_QUEUE}
    assert channel.queues[QUEUE]["x-dead-letter-exchange"] == "emergency.dlx"  # failed messages go to the DLQ
    assert (QUEUE, "emergency", "match") in channel.bindings


# -- what happens to each message ---------------------------------------------------------------------

def test_a_matched_message_is_acknowledged(consumer, channel, fake_store, add_resource, add_request):
    add_resource("res-1")
    add_request("req-1")

    consumer.handle(channel, 7, encode_message("req-1"))

    assert channel.calls == [("ack", 7)]
    assert fake_store.get(request_key("req-1"))["status"] == "MATCHED"


def test_an_unmatched_message_is_acknowledged_too(consumer, channel, fake_store, add_request):
    add_request("req-1")  # nothing to match it with

    consumer.handle(channel, 1, encode_message("req-1"))

    assert channel.calls == [("ack", 1)]  # "no help available" is an answer, not a failure
    assert fake_store.get(request_key("req-1"))["status"] == "UNMATCHED"


def test_a_duplicate_message_is_harmless(consumer, channel, fake_store, add_resource, add_request):
    add_resource("res-1")
    add_resource("res-2")
    add_request("req-1")

    consumer.handle(channel, 1, encode_message("req-1"))
    consumer.handle(channel, 2, encode_message("req-1"))  # RabbitMQ delivered it twice

    assert channel.calls == [("ack", 1), ("ack", 2)]
    assert len(fake_store.query("resource:", status="ASSIGNED")) == 1  # still only one resource used


def test_an_unreadable_message_goes_straight_to_the_dead_letter_queue(consumer, channel):
    consumer.handle(channel, 3, b"garbage")
    assert channel.calls == [("nack", 3, False)]


def test_a_failure_is_published_again_with_the_next_attempt(consumer, channel):
    consumer.handle(channel, 4, encode_message("req-ghost", attempt=1))  # no such request: matching fails

    assert channel.calls == [("publish", "emergency", "match", ("req-ghost", 2), 2), ("ack", 4)]
    # published (persistent, delivery_mode 2) BEFORE the old copy is acknowledged


def test_the_last_failure_goes_to_the_dead_letter_queue(consumer, channel):
    consumer.handle(channel, 5, encode_message("req-ghost", attempt=3))
    assert channel.calls == [("nack", 5, False)]


def test_a_storage_outage_puts_the_message_back_without_using_an_attempt(channel):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    dead = StorageClient(client=httpx.Client(base_url="http://coordinator", transport=httpx.MockTransport(refuse)))
    consumer = Consumer(dead, url="amqp://unused", retry_delay_s=0, max_attempts=3)

    consumer.handle(channel, 6, encode_message("req-1", attempt=3))

    assert channel.calls == [("nack", 6, True)]  # requeued, not dead-lettered, even on the last attempt


# -- matching service HTTP app -------------------------------------------------------------------------

def test_matching_service_http(fake_store, add_resource, add_request):
    api = TestClient(create_app(store=fake_store, consume=False))
    assert api.get("/health").json() == {"status": "ok", "consumer_connected": False}

    add_resource("res-1")
    add_request("req-1")
    r = api.post("/match/req-1")
    assert r.status_code == 200
    assert r.json()["outcome"] == "matched" and r.json()["resource"]["id"] == "res-1"

    assert api.post("/match/req-1").json()["outcome"] == "skipped"  # already matched: nothing more to do
    assert api.post("/match/req-ghost").status_code == 404
