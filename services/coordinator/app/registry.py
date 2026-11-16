"""Which storage nodes exist, and which are alive. DESIGN.md section 8.1.

Failure detection works two ways:
  * Active (heartbeat timeout): a node that hasn't sent a heartbeat for
    HEARTBEAT_TIMEOUT_S is marked DOWN by sweep(), which runs every second.
  * Passive (on error): when the router can't reach a node, it marks it DOWN
    immediately instead of waiting up to 10 s.

A DOWN node may have missed writes, so it can't quietly become UP again. Its
next heartbeat gets 404, which makes it register again; registering is where
recovery (catch-up from its partner, Phase 5) will hook in.

Over a network, "dead" and "slow" look the same. A timeout is a guess, so we
choose the safe mistake: treat a slow node as dead and make it catch up.
"""

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from common.models import utc_now_iso

log = logging.getLogger("coordinator.registry")

UP = "UP"
DOWN = "DOWN"

ROLES = ("primary", "replica")


class RegistrationError(ValueError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class Node:
    node_id: str
    shard: int
    role: str  # the role it was configured with; see acting_primary() for who leads now
    url: str
    state: str = UP
    last_seen: float = 0.0  # monotonic clock, for timeouts
    last_heartbeat: str | None = None  # wall clock, for humans
    registered_at: str = field(default_factory=utc_now_iso)
    down_reason: str | None = None


class Registry:
    def __init__(self, num_shards: int, timeout_s: float, clock: Callable[[], float] = time.monotonic):
        self.num_shards = num_shards
        self.timeout_s = timeout_s
        # Monotonic clock: never jumps backwards when the system time is changed.
        self._clock = clock
        self._nodes: dict[str, Node] = {}
        self._lock = threading.Lock()

    def register(self, node_id: str, shard: int, role: str, url: str) -> Node:
        if not 0 <= shard < self.num_shards:
            raise RegistrationError(f"shard must be 0..{self.num_shards - 1}, got {shard}", 400)
        if role not in ROLES:
            raise RegistrationError(f"role must be one of {ROLES}, got {role!r}", 400)
        with self._lock:
            # One primary slot and one replica slot per shard. A second node
            # claiming a taken slot is a configuration mistake: refuse it loudly.
            for other in self._nodes.values():
                if other.node_id != node_id and other.shard == shard and other.role == role:
                    raise RegistrationError(
                        f"shard {shard} {role} slot already belongs to {other.node_id}", 409
                    )
            now = self._clock()
            node = Node(node_id, shard, role, url, state=UP, last_seen=now, last_heartbeat=utc_now_iso())
            previous = self._nodes.get(node_id)
            self._nodes[node_id] = node
        log.info(
            "%s %s (shard %d %s) at %s",
            "re-registered" if previous else "registered", node_id, shard, role, url,
        )
        return node

    def heartbeat(self, node_id: str) -> bool:
        """False means 'unknown or DOWN: register again' (the endpoint answers 404)."""
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None or node.state == DOWN:
                return False
            node.last_seen = self._clock()
            node.last_heartbeat = utc_now_iso()
            return True

    def mark_down(self, node_id: str, reason: str) -> None:
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None or node.state == DOWN:
                return
            node.state = DOWN
            node.down_reason = reason
        log.warning("%s marked DOWN: %s", node_id, reason)

    def sweep(self) -> list[str]:
        """Mark DOWN every UP node whose last heartbeat is too old. Returns their ids."""
        now = self._clock()
        with self._lock:
            stale = [n for n in self._nodes.values() if n.state == UP and now - n.last_seen > self.timeout_s]
        for n in stale:
            self.mark_down(n.node_id, f"no heartbeat for {now - n.last_seen:.1f}s")
        return [n.node_id for n in stale]

    def shard_nodes(self, shard: int) -> list[Node]:
        """All nodes of a shard, configured primary first."""
        with self._lock:
            nodes = [n for n in self._nodes.values() if n.shard == shard]
        return sorted(nodes, key=lambda n: ROLES.index(n.role))

    def live_nodes(self, shard: int) -> list[Node]:
        """UP nodes of a shard, in the order we should try them."""
        return [n for n in self.shard_nodes(shard) if n.state == UP]

    def acting_primary(self, shard: int) -> Node | None:
        """Failover rule: the configured primary if it is UP, otherwise the replica.

        Only the coordinator decides this, so two nodes can never both think
        they lead the same shard (no split brain).
        """
        live = self.live_nodes(shard)
        return live[0] if live else None

    def snapshot(self) -> list[dict]:
        now = self._clock()
        with self._lock:
            nodes = sorted(self._nodes.values(), key=lambda n: (n.shard, ROLES.index(n.role)))
            out = []
            for n in nodes:
                d = asdict(n)
                d.pop("last_seen")
                d["seconds_since_heartbeat"] = round(now - n.last_seen, 1)
                out.append(d)
        return out
