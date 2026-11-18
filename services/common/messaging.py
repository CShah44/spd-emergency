"""The RabbitMQ contract between the request service (sends) and the matching
service (receives). DESIGN.md section 7.

Both sides call declare_topology() when they connect. Declaring is safe to
repeat as long as the settings are identical, and it means whichever service
starts first creates the queues, so start order never matters.

This file has no pika import on purpose: it only needs the channel object it
is handed, so every service can import it without extra dependencies.
"""

import json

EXCHANGE = "emergency"  # direct exchange: delivers to the queue bound with ROUTING_KEY
ROUTING_KEY = "match"
QUEUE = "match.requests"

# Messages that fail for good are moved here by RabbitMQ itself, so they can be
# inspected (and replayed) instead of looping forever.
DEAD_LETTER_EXCHANGE = "emergency.dlx"
DEAD_LETTER_ROUTING_KEY = "dead"
DEAD_LETTER_QUEUE = "match.requests.dlq"

# Properties for every message we publish: "persistent" makes RabbitMQ write it
# to disk, so a queued message survives a RabbitMQ restart. Use as
# pika.BasicProperties(**PUBLISH_PROPERTIES).
PUBLISH_PROPERTIES = {"delivery_mode": 2, "content_type": "application/json"}


def declare_topology(channel) -> None:
    """Create the exchanges and queues if they don't exist yet. All durable, so
    they survive a RabbitMQ restart."""
    channel.exchange_declare(DEAD_LETTER_EXCHANGE, exchange_type="direct", durable=True)
    channel.queue_declare(DEAD_LETTER_QUEUE, durable=True)
    channel.queue_bind(DEAD_LETTER_QUEUE, DEAD_LETTER_EXCHANGE, routing_key=DEAD_LETTER_ROUTING_KEY)

    channel.exchange_declare(EXCHANGE, exchange_type="direct", durable=True)
    channel.queue_declare(
        QUEUE,
        durable=True,
        # A nack(requeue=False) on this queue sends the message to the DLQ.
        arguments={
            "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
            "x-dead-letter-routing-key": DEAD_LETTER_ROUTING_KEY,
        },
    )
    channel.queue_bind(QUEUE, EXCHANGE, routing_key=ROUTING_KEY)


def encode_message(request_id: str, attempt: int = 1) -> bytes:
    """{"request_id": "req-...", "attempt": 1}"""
    return json.dumps({"request_id": request_id, "attempt": attempt}).encode("utf-8")


def decode_message(body: bytes) -> tuple[str, int]:
    """Returns (request_id, attempt). Raises ValueError for anything malformed."""
    try:
        data = json.loads(body)
        request_id, attempt = data["request_id"], int(data.get("attempt", 1))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f"malformed queue message: {body[:100]!r}") from exc
    if not isinstance(request_id, str) or not request_id:
        raise ValueError(f"malformed queue message: {body[:100]!r}")
    return request_id, attempt
