"""What a coordinator does as leader, standby, or new leader. Roadmap task 3.

With leader election off (Docker Compose, unit tests) there is one coordinator
and it is always the leader: nothing here changes how it behaves.

With it on (Kubernetes, 2 replicas), election.py decides who leads, and this
file decides what that means:

  * STANDBY: serves nothing but /health, /livez, /ready (503: not the leader)
    and /internal/leader. The Service doesn't send it traffic anyway: only the
    leader's pod carries the coordinator-role=leader label it selects on.
  * NEW LEADER, settling: the node registry lives in memory, so a new leader
    starts with an empty one. Every storage node finds out within one heartbeat
    (it gets 404 and registers again), and each registration carries the node's
    operation-log position. For a few seconds (until all nodes have registered,
    or SETTLE_TIMEOUT_S) the leader only COLLECTS registrations and answers
    /store with 503. Then, per shard, it admits the FRESHEST node first (it
    becomes UP and leads) and the other second (it becomes RECOVERING and
    catches up). Without this, whichever node happened to register first would
    lead, even a stale one, and serve old data.
  * LEADER: serves everything, like a single coordinator.
  * LOST LEADERSHIP (lease taken, or a storage node said our epoch is old):
    forget the registry and go back to standby.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .election import LeaderElector
from .registry import ROLES, Registry

log = logging.getLogger("coordinator.leadership")


def fresher_first(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    """Order a shard's two registrations: the node to lead first.

    Node X has everything node Y has when X's watermark for Y's log reaches Y's
    head (Y's own writes are all that X could be missing; see storage_node db.py).
    If exactly one of them has everything, it leads. If both do (in sync) or
    neither does (both took writes alone during a whole-shard outage; the
    catch-up then copies both ways and counts conflicts), the configured
    primary leads.
    """

    def has_all_of(x: dict, y: dict) -> bool:
        px, py = x.get("position"), y.get("position")
        if not px or not py:
            return False  # an older node without a log: can't tell
        if py["head"] == 0:
            return True
        return px["watermarks"].get(py["log_id"], -1) >= py["head"]

    a_ok, b_ok = has_all_of(a, b), has_all_of(b, a)
    if a_ok != b_ok:
        return [a, b] if a_ok else [b, a]
    return sorted([a, b], key=lambda r: ROLES.index(r["role"]))


class Leadership:
    def __init__(
        self,
        registry: Registry,
        elector: LeaderElector | None,
        settle_timeout_s: float,
        admit: Callable[[dict[str, Any]], Awaitable[str]],
        on_reset: Callable[[], None],
    ):
        self.registry = registry
        self.elector = elector
        self.settle_timeout_s = settle_timeout_s
        self._admit = admit  # registry.register + start recovery; returns the state
        self._on_reset = on_reset  # cancel running recoveries
        self.settling = False
        self.pending: dict[str, dict[str, Any]] = {}
        self._all_here = asyncio.Event()
        self._settle_task: asyncio.Task | None = None
        self.last_takeover: dict[str, Any] | None = None
        if elector is not None:
            elector.on_change = self.on_change

    @property
    def enabled(self) -> bool:
        return self.elector is not None

    @property
    def epoch(self) -> int | None:
        return self.elector.epoch if self.elector else None

    def is_leader(self) -> bool:
        return self.elector is None or self.elector.is_leader()

    def can_serve_data(self) -> bool:
        return self.is_leader() and not self.settling

    # -- leadership changes ---------------------------------------------------------------

    async def on_change(self, leading: bool, epoch: int | None) -> None:
        self._stop_settling()
        self._reset()
        await self.sync_label()
        if leading:
            log.warning("now LEADER (epoch %s): collecting node registrations before serving", epoch)
            self.settling = True
            self.pending = {}
            self._all_here = asyncio.Event()
            self._settle_task = asyncio.create_task(self._settle(epoch))
        else:
            log.warning("now STANDBY")

    async def sync_label(self) -> None:
        """Make our pod's leader label match our role, so the Service routes to the
        leader. Best effort: label_keeper() repeats it in case the API was down."""
        set_label = getattr(self.elector.backend, "set_leader_label", None) if self.elector else None
        if set_label is None:
            return  # not on Kubernetes (tests): nothing to label
        try:
            await set_label(self.elector.identity, self.is_leader())
        except Exception as exc:  # noqa: BLE001
            log.warning("could not update the leader label: %r", exc)

    async def label_keeper(self, every_s: float = 5.0) -> None:
        while True:
            await asyncio.sleep(every_s)
            await self.sync_label()

    async def fenced(self, detail: Any) -> None:
        """A storage node refused our epoch: someone newer leads. Stop at once."""
        if self.elector is not None:
            await self.elector.step_down(f"a storage node knows a newer epoch: {detail}")

    def _reset(self) -> None:
        self._on_reset()
        self.registry.clear()

    def _stop_settling(self) -> None:
        if self._settle_task is not None and not self._settle_task.done():
            self._settle_task.cancel()
        self._settle_task = None
        self.settling = False

    # -- settling -------------------------------------------------------------------------

    def offer(self, registration: dict[str, Any]) -> bool:
        """During settling, hold a registration instead of admitting it. True if held."""
        if not self.settling:
            return False
        self.registry.validate(registration["shard"], registration["role"])
        self.pending[registration["node_id"]] = registration
        if len(self.pending) >= 2 * self.registry.num_shards:
            self._all_here.set()
        return True

    def is_pending(self, node_id: str) -> bool:
        return self.settling and node_id in self.pending

    async def _settle(self, epoch: int | None) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with_everyone = True
        try:
            await asyncio.wait_for(self._all_here.wait(), self.settle_timeout_s)
        except asyncio.TimeoutError:
            with_everyone = False
        leaders = {}
        for shard in range(self.registry.num_shards):
            regs = [r for r in self.pending.values() if r["shard"] == shard]
            ordered = fresher_first(*regs) if len(regs) == 2 else regs
            for reg in ordered:
                try:
                    await self._admit(reg)
                except Exception as exc:  # noqa: BLE001  (a bad registration must not block the rest)
                    log.error("could not admit %s after takeover: %r", reg.get("node_id"), exc)
            if ordered:
                leaders[shard] = ordered[0]["node_id"]
        self.settling = False
        self.last_takeover = {
            "epoch": epoch,
            "nodes_registered": len(self.pending),
            "all_nodes": with_everyone,
            "settle_seconds": round(loop.time() - started, 2),
            "leaders": leaders,
        }
        log.warning("settled after takeover, serving now: %s", self.last_takeover)

    def describe(self) -> dict[str, Any]:
        return {
            "election": "on" if self.enabled else "off",
            "identity": self.elector.identity if self.elector else None,
            "is_leader": self.is_leader(),
            "epoch": self.epoch,
            "lease_holder": self.elector.holder() if self.elector else None,
            "settling": self.settling,
            "last_takeover": self.last_takeover,
        }
