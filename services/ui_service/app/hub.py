"""Polls the request service and tells every open browser tab when something changed.

    every 1 s  the requests, their counts per status, the storage cluster and the queue
    every 3 s  the list of responders

It polls only while someone is looking (a tab is connected, or /api/state was asked for
in the last few seconds), so an unwatched page adds no load to the system. After an
action the page calls poke() and the next refresh happens at once instead of within 1 s.
"""

import asyncio
import time
from datetime import datetime, timezone

from .api import DeltaApi
from .state import State

REQUESTS_SHOWN = 100
POLL_FAST_S = 1.0
POLL_SLOW_S = 3.0
IDLE_AFTER_S = 10.0


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


class Hub:
    def __init__(self, api: DeltaApi) -> None:
        self.api = api
        self.state = State()
        self.version = 0  # goes up by one each time the snapshot changes
        self.watchers = 0
        self._last_seen = 0.0
        self._last_slow = 0.0
        self._changed = asyncio.Condition()
        self._poke = asyncio.Event()

    def seen(self) -> None:
        self._last_seen = time.monotonic()

    def watched(self) -> bool:
        return self.watchers > 0 or time.monotonic() - self._last_seen < IDLE_AFTER_S

    def poke(self, slow: bool = False) -> None:
        """Refresh now (after an action). slow=True also re-reads the responders."""
        if slow:
            self._last_slow = 0.0
        self._poke.set()

    async def run(self) -> None:
        while True:
            if self.watched():
                await self.refresh()
            try:
                await asyncio.wait_for(self._poke.wait(), POLL_FAST_S)
            except asyncio.TimeoutError:
                pass
            self._poke.clear()

    async def refresh(self) -> None:
        when = now()
        slow = time.monotonic() - self._last_slow >= POLL_SLOW_S
        calls = [self.api.requests(REQUESTS_SHOWN), self.api.request_summary(), self.api.cluster()]
        if slow:
            calls.append(self.api.resources())
        answers = await asyncio.gather(*calls, return_exceptions=True)
        requests, summary, cluster = answers[:3]
        state, changed = self.state, False
        # Responders first: the request feed names the responder a request was matched to.
        if slow and not isinstance(answers[3], Exception):
            self._last_slow = time.monotonic()
            changed |= state.apply_resources(answers[3], when)
        if isinstance(requests, Exception):
            changed |= state.set_link(False, str(requests))
        else:
            changed |= state.set_link(True)
            changed |= state.apply_requests(requests, when)
        if not isinstance(summary, Exception):
            changed |= state.apply_summary(summary)
        if not isinstance(cluster, Exception):
            changed |= state.apply_cluster(cluster, when)
        if changed:
            async with self._changed:
                self.version += 1
                self._changed.notify_all()

    async def wait_for_change(self, version: int, timeout: float) -> bool:
        """Wait until the snapshot is newer than `version`. False on timeout."""
        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait_for(lambda: self.version > version), timeout)
                return True
            except asyncio.TimeoutError:
                return False
