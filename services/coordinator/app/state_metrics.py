"""The coordinator's view of the cluster, as Prometheus gauges. Roadmap task 1.

Counters (replication failures, catch-ups...) are counted where things happen.
Gauges describe STATE, so this loop rewrites them every few seconds:

    spd_node_state{node,shard,state}     1 for the state each storage node is in
    spd_shard_available{shard}           1 if the shard has a leading node
    spd_replication_lag_writes{shard,node}
                                         writes of its partner a node hasn't received:
                                         partner's log head - this node's watermark for
                                         that log (storage_node/app/db.py)
    spd_coordinator_is_leader{pod}, spd_coordinator_epoch{pod}

Prometheus scrapes BOTH coordinator pods, so only the leader reports node and
shard gauges: a standby's registry is empty (or was, before it stepped down,
stale), and the dashboard adds these up across pods. When a value has no
meaning any more (a node left, we became standby) its time series is REMOVED,
not left at its last value.
"""

import asyncio
import logging
from typing import Any, Callable

import httpx

from common import metrics

from .leadership import Leadership
from .registry import DOWN, RECOVERING, UP, Registry

log = logging.getLogger("coordinator.metrics")

STATES = (UP, RECOVERING, DOWN)


class StateMetrics:
    def __init__(self, registry: Registry, leadership: Leadership, http: Callable[[], httpx.AsyncClient], pod: str):
        self.registry = registry
        self.leadership = leadership
        self._http = http
        self.pod = pod
        self._shown: dict[Any, set[tuple[str, ...]]] = {}

    async def run(self, every_s: float = 2.0, lag_every: int = 3) -> None:
        """Gauges every `every_s` seconds; replication lag every `lag_every` rounds."""
        rounds = 0
        while True:
            self.refresh()
            if rounds % lag_every == 0:
                await self.refresh_lag()
            rounds += 1
            await asyncio.sleep(every_s)

    def refresh(self) -> None:
        leading = self.leadership.is_leader()
        metrics.COORDINATOR_IS_LEADER.labels(self.pod).set(1 if leading else 0)
        elector = getattr(self.leadership, "elector", None)
        epoch = elector.observed_epoch() if elector is not None else self.leadership.epoch
        metrics.COORDINATOR_EPOCH.labels(self.pod).set(epoch or 0)  # a standby shows the term it sees too

        states: dict[tuple[str, ...], float] = {}
        shards: dict[tuple[str, ...], float] = {}
        if leading:
            for node in self.registry.snapshot():
                for state in STATES:
                    states[(node["node_id"], str(node["shard"]), state)] = 1.0 if node["state"] == state else 0.0
            for shard in range(self.registry.num_shards):
                shards[(str(shard),)] = 1.0 if self.registry.acting_primary(shard) is not None else 0.0
        self._replace(metrics.NODE_STATE, states)
        self._replace(metrics.SHARD_AVAILABLE, shards)

    async def refresh_lag(self) -> None:
        lag: dict[tuple[str, ...], float] = {}
        if self.leadership.can_serve_data():
            for shard in range(self.registry.num_shards):
                nodes = [n for n in self.registry.shard_nodes(shard) if n.state in (UP, RECOVERING)]
                if len(nodes) != 2:
                    continue  # a lone node has nobody to lag behind; a DOWN one can't be asked
                positions = await asyncio.gather(*(self._position(n.url) for n in nodes))
                if None in positions:
                    continue
                for (node, mine), theirs in zip(zip(nodes, positions), reversed(positions)):
                    mark = mine["watermarks"].get(theirs["log_id"])
                    if mark is not None:
                        lag[(str(shard), node.node_id)] = float(max(0, theirs["head"] - mark))
        self._replace(metrics.REPLICATION_LAG, lag)

    async def _position(self, url: str) -> dict[str, Any] | None:
        try:
            resp = await self._http().get(url + "/internal/oplog/position", timeout=1.0)
            return resp.json() if resp.is_success else None
        except Exception:  # noqa: BLE001  (a metric must never break anything)
            return None

    def _replace(self, gauge, values: dict[tuple[str, ...], float]) -> None:
        """Set these label sets, and REMOVE the ones we showed before but not now."""
        for labels, value in values.items():
            gauge.labels(*labels).set(value)
        for gone in self._shown.get(gauge, set()) - values.keys():
            try:
                gauge.remove(*gone)
            except KeyError:
                pass
        self._shown[gauge] = set(values)
