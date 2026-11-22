"""Drops a "please match this request" slip into RabbitMQ. DESIGN.md section 7."""

import logging
import threading
import time

import pika

from common import config
from common.messaging import (
    DEAD_LETTER_QUEUE, EXCHANGE, PUBLISH_PROPERTIES, QUEUE, ROUTING_KEY, declare_topology, encode_message,
)

log = logging.getLogger("publisher")

CONNECT_TIMEOUT_S = 2.0
STATS_RETRY_AFTER_S = 5.0  # after a failed queue_stats(), don't try again for this long


class PublishError(Exception):
    """RabbitMQ did not take the message."""


class Publisher:
    """Keeps one RabbitMQ connection and reuses it.

    pika connections are not thread-safe and FastAPI runs endpoints on several
    threads, so every publish goes through a lock. The connection is opened on
    the first publish, so the service starts fine even if RabbitMQ is not up yet.
    """

    def __init__(self, url: str | None = None):
        self._url = url or config.RABBITMQ_URL
        self._lock = threading.Lock()
        self._connection: pika.BlockingConnection | None = None
        self._channel = None
        self._stats_retry_at = 0.0

    def publish(self, request_id: str) -> None:
        body = encode_message(request_id)
        with self._lock:
            # An idle connection may have been dropped by RabbitMQ since the last
            # publish, and the first try is what discovers that: reconnect and try
            # once more. A connection we JUST opened that fails is not worth retrying.
            for attempt in (1, 2):
                reused = self._channel is not None and not self._channel.is_closed
                try:
                    self._ready_channel().basic_publish(
                        EXCHANGE, ROUTING_KEY, body, pika.BasicProperties(**PUBLISH_PROPERTIES),
                        mandatory=True,  # error if no queue would receive it, instead of silently dropping it
                    )
                    return  # with confirms on, we only get here once RabbitMQ has accepted the message
                except (pika.exceptions.AMQPError, OSError) as exc:
                    self._disconnect()
                    if attempt == 2 or not reused:
                        raise PublishError(f"could not publish {request_id}: {exc!r}") from exc

    def queue_stats(self) -> dict[str, int] | None:
        """How many messages wait in the queue and the dead-letter queue, and how many
        matching services are consuming. None if RabbitMQ can't be reached.

        Asked over AMQP (exact, unlike the management UI's counters, which lag a few
        seconds). The web page polls this, so a failure is remembered for a few
        seconds instead of making every poll wait for a connection timeout.
        """
        with self._lock:
            if time.monotonic() < self._stats_retry_at:
                return None
            try:
                channel = self._ready_channel()
                main = channel.queue_declare(QUEUE, passive=True).method  # passive: only look, never create
                dead = channel.queue_declare(DEAD_LETTER_QUEUE, passive=True).method
                return {"waiting": main.message_count, "consumers": main.consumer_count,
                        "dead_lettered": dead.message_count}
            except (pika.exceptions.AMQPError, OSError):
                self._disconnect()
                self._stats_retry_at = time.monotonic() + STATS_RETRY_AFTER_S
                return None

    def close(self) -> None:
        with self._lock:
            self._disconnect()

    def _ready_channel(self):
        if self._channel is None or self._channel.is_closed:
            params = pika.URLParameters(self._url)
            params.socket_timeout = CONNECT_TIMEOUT_S  # the default (10 s) would stall every request while RabbitMQ is down
            self._connection = pika.BlockingConnection(params)
            self._channel = self._connection.channel()
            declare_topology(self._channel)
            self._channel.confirm_delivery()  # publish() now waits for RabbitMQ to confirm each message
        return self._channel

    def _disconnect(self) -> None:
        try:
            if self._connection is not None and self._connection.is_open:
                self._connection.close()
        except (pika.exceptions.AMQPError, OSError):
            pass  # already broken: nothing to close
        self._connection = self._channel = None
