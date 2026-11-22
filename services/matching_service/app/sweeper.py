"""Safety net for requests that were saved but never queued.

The request service saves a request first and publishes it to RabbitMQ second. If
RabbitMQ is down at that moment the request stays PENDING and no message exists
for it. Every SWEEP_INTERVAL_S this looks for requests that have been PENDING for
longer than PENDING_STALE_AFTER_S and matches them directly.

Safe to run in every matching pod at once, and next to the queue consumer:
match_request() does nothing for a request that is no longer waiting, and its
version checks mean two matchers can never claim the same responder.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from common import config, metrics
from common.models import RequestStatus
from common.sharding import REQUEST_PREFIX
from common.storage_client import StorageClient, StorageError, StorageUnavailable, VersionConflict

from .matcher import match_request

log = logging.getLogger("sweeper")


class Sweeper:
    def __init__(
        self,
        store: StorageClient,
        interval_s: float | None = None,
        stale_after_s: float | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._store = store
        self._interval_s = config.SWEEP_INTERVAL_S if interval_s is None else interval_s
        self._stale_after_s = config.PENDING_STALE_AFTER_S if stale_after_s is None else stale_after_s
        self._clock = clock
        self._stop = threading.Event()

    def run(self) -> None:
        """Sweep until stop() is called. The first sweep comes after one interval,
        so a freshly started pod doesn't compete with its own queue consumer."""
        while not self._stop.wait(self._interval_s):
            try:
                self.sweep_once()
            except StorageError as exc:  # storage is down or unreachable: try again next time
                log.warning("sweep skipped: %s", exc)

    def stop(self) -> None:
        self._stop.set()

    def sweep_once(self) -> int:
        """Match every request that has been PENDING too long. Returns how many were handled."""
        handled = 0
        for request in self._store.query(REQUEST_PREFIX, status=RequestStatus.PENDING):
            if not self._is_stale(request):
                continue
            try:
                result = match_request(self._store, request["id"])
            except VersionConflict:
                log.info("%s changed while being swept: someone else is handling it", request["id"])
            except StorageUnavailable:
                raise  # nothing else will work either: give up on this sweep
            except Exception:  # noqa: BLE001  (one bad request must not stop the rest)
                log.exception("%s: sweep failed", request["id"])
            else:
                log.info("%s was never queued: swept, %s", request["id"], result.outcome)
                metrics.SWEEPER_RESCUED.inc()
                handled += 1
        return handled

    def _is_stale(self, request: dict[str, Any]) -> bool:
        stamp = request.get("updated_at") or request.get("created_at")
        try:
            saved_at = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            log.warning("%s has no readable timestamp (%r): not swept", request.get("id"), stamp)
            return False
        return self._clock() - saved_at.timestamp() >= self._stale_after_s
