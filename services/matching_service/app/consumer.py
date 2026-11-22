"""Takes requests off the RabbitMQ queue and matches them. DESIGN.md section 7.

Runs in a background thread of the matching service. One message at a time
(prefetch 1), so when the service is scaled to several pods RabbitMQ hands each
free pod the next request: the load spreads by itself.

A message is acknowledged ONLY after matching finished, so if this pod dies
half-way RabbitMQ gives the message to another pod. What happens when matching
fails:
  * storage is down  -> wait a moment and put the message back (not the
                        message's fault, so it doesn't use up an attempt)
  * any other error  -> publish it again with attempt+1; after
                        MAX_DELIVERY_ATTEMPTS it goes to the dead-letter queue
  * unreadable message -> straight to the dead-letter queue
"""

import logging
import threading

import pika

from common import config, metrics
from common.messaging import (
    EXCHANGE, PUBLISH_PROPERTIES, QUEUE, ROUTING_KEY, declare_topology, decode_message, encode_message,
)
from common.storage_client import StorageClient, StorageUnavailable

from .matcher import match_request

log = logging.getLogger("consumer")

RECONNECT_DELAY_S = 3.0
CONNECT_TIMEOUT_S = 2.0


class Consumer:
    def __init__(
        self,
        store: StorageClient,
        url: str | None = None,
        retry_delay_s: float = 2.0,
        max_attempts: int | None = None,
    ):
        self._store = store
        self._url = url or config.RABBITMQ_URL
        self._retry_delay_s = retry_delay_s
        self._max_attempts = max_attempts or config.MAX_DELIVERY_ATTEMPTS
        self._stop = threading.Event()
        self.connected = False  # shown by /health

    def run(self) -> None:
        """Consume until stop() is called, reconnecting whenever RabbitMQ goes away
        (it may simply not be up yet when this pod starts)."""
        while not self._stop.is_set():
            try:
                self._consume()
            except (pika.exceptions.AMQPError, OSError) as exc:
                log.warning("RabbitMQ unavailable (%r), retrying in %.0fs", exc, RECONNECT_DELAY_S)
            self.connected = False
            self._stop.wait(RECONNECT_DELAY_S)

    def stop(self) -> None:
        self._stop.set()

    def _consume(self) -> None:
        params = pika.URLParameters(self._url)
        params.socket_timeout = CONNECT_TIMEOUT_S
        connection = pika.BlockingConnection(params)
        try:
            channel = connection.channel()
            declare_topology(channel)
            channel.confirm_delivery()  # a retry we publish below is only "sent" once RabbitMQ confirms it
            channel.basic_qos(prefetch_count=1)
            self.connected = True
            log.info("connected to RabbitMQ, waiting for requests on %s", QUEUE)
            # inactivity_timeout makes the loop wake up every second even when the
            # queue is empty, so stop() is noticed quickly.
            for method, _properties, body in channel.consume(QUEUE, inactivity_timeout=1):
                if self._stop.is_set():
                    break  # a message we were just handed goes back to the queue when we disconnect
                if method is not None:
                    self.handle(channel, method.delivery_tag, body)
        finally:
            if connection.is_open:
                connection.close()

    def handle(self, channel, delivery_tag: int, body: bytes) -> None:
        """Process one message and acknowledge it the right way (see the module docstring)."""
        try:
            request_id, attempt = decode_message(body)
        except ValueError as exc:
            log.error("unreadable message, sending to the dead-letter queue: %s", exc)
            metrics.DEAD_LETTERED.inc()
            channel.basic_nack(delivery_tag, requeue=False)
            return

        try:
            result = match_request(self._store, request_id)
        except StorageUnavailable as exc:
            log.warning("%s: storage unavailable (%s), will retry", request_id, exc)
            self._stop.wait(self._retry_delay_s)
            channel.basic_nack(delivery_tag, requeue=True)
        except Exception:  # noqa: BLE001  (a worker must survive any single bad message)
            log.exception("%s: matching failed (attempt %d of %d)", request_id, attempt, self._max_attempts)
            self._retry_or_dead_letter(channel, delivery_tag, request_id, attempt)
        else:
            log.info("%s: %s", request_id, result.outcome)
            channel.basic_ack(delivery_tag)

    def _retry_or_dead_letter(self, channel, delivery_tag: int, request_id: str, attempt: int) -> None:
        if attempt >= self._max_attempts:
            log.error("%s: giving up after %d attempts, sending to the dead-letter queue", request_id, attempt)
            metrics.DEAD_LETTERED.inc()
            channel.basic_nack(delivery_tag, requeue=False)
            return
        # Publish the retry BEFORE acknowledging the old copy: a crash in between
        # leaves two copies (harmless, matching is safe to repeat) instead of none.
        channel.basic_publish(
            EXCHANGE, ROUTING_KEY, encode_message(request_id, attempt + 1),
            pika.BasicProperties(**PUBLISH_PROPERTIES),
        )
        channel.basic_ack(delivery_tag)
