"""Drops a "please match this request" slip into RabbitMQ. DESIGN.md section 7."""

import logging
import threading

import pika

from common import config
from common.messaging import EXCHANGE, PUBLISH_PROPERTIES, ROUTING_KEY, declare_topology, encode_message

log = logging.getLogger("publisher")

CONNECT_TIMEOUT_S = 2.0


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
