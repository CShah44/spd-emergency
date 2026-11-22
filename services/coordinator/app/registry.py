"""Which storage nodes exist, which are alive, and who leads each shard.
DESIGN.md section 8.1.

Node states:
    UP          serves reads and client writes, receives replication
    RECOVERING  catching up from its partner; receives replication (so it
                can't miss new writes) but serves nothing until caught up
    DOWN        ignored; must register again (-> RECOVERING) to come back

Failure detection works two ways:
  * Active (heartbeat timeout): sweep() marks a node DOWN after
    HEARTBEAT_TIMEOUT_S of silence. It runs every second.
  * Passive (on error): the router marks a node DOWN the moment a call fails.
Over a network "dead" and "slow" look the same; we choose the safe mistake
(treat slow as dead and make it catch up).

Leadership is STICKY: once the replica has taken over a shard, it keeps
leading even after the old primary recovers. Handing leadership back
(failback) while writes are in flight could let both nodes accept a
compare-and-set on the same version, which is exactly the double assignment
we are preventing. Leadership moves only when the leader fails.
"""

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from common.models import utc_now_iso

log = logging.getLogger("coordinator.registry")

UP = "UP"
RECOVERING = "RECOVERING"
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
    role: str  # the slot it was configured for; who LEADS is decided by acting_primary()
    url: str
    state: str = UP
    generation: int = 0  # bumps on every registration, so a stale recovery can't finish
    last_seen: float = 0.0  # monotonic clock, for timeouts
    last_heartbeat: str | None = None  # wall clock, for humans
    registered_at: str = field(default_factory=utc_now_iso)
    down_reason: str | None = None
    last_recovery: dict[str, Any] | None = None


class Registry:
    def __init__(self, num_shards: int, timeout_s: float, clock: Callable[[], float] = time.monotonic):
        self.num_shards = num_shards
        self.timeout_s = timeout_s
        # Monotonic clock: never jumps backwards when the system time is changed.
        self._clock = clock
        self._nodes: dict[str, Node] = {}
        self._leader: dict[int, str] = {}
        self._generation = 0
        self._lock = threading.Lock()

    # -- membership --------------------------------------------------------------------

    def validate(self, shard: int, role: str) -> None:
        if not 0 <= shard < self.num_shards:
            raise RegistrationError(f"shard must be 0..{self.num_shards - 1}, got {shard}", 400)
        if role not in ROLES:
            raise RegistrationError(f"role must be one of {ROLES}, got {role!r}", 400)

    def clear(self) -> None:
        """Forget every node and leader (this coordinator stopped or started leading)."""
        with self._lock:
            self._nodes.clear()
            self._leader.clear()

    def register(self, node_id: str, shard: int, role: str, url: str) -> Node:
        """Add or re-add a node. It starts RECOVERING if its partner is UP (there
        is something to catch up from), otherwise UP (its own data is the best
        copy that exists)."""
        self.validate(shard, role)
        with self._lock:
            # One primary slot and one replica slot per shard. A second node
            # claiming a taken slot is a configuration mistake: refuse it loudly.
            for other in self._nodes.values():
                if other.node_id != node_id and other.shard == shard and other.role == role:
                    raise RegistrationError(f"shard {shard} {role} slot already belongs to {other.node_id}", 409)
            partner_up = any(
                o.shard == shard and o.node_id != node_id and o.state == UP for o in self._nodes.values()
            )
            self._generation += 1
            previous = self._nodes.get(node_id)
            node = Node(
                node_id, shard, role, url,
                state=RECOVERING if partner_up else UP,
                generation=self._generation,
                last_seen=self._clock(),
                last_heartbeat=utc_now_iso(),
                last_recovery=previous.last_recovery if previous else None,
            )
            self._nodes[node_id] = node
        log.info("%s %s (shard %d %s) at %s -> %s",
                 "re-registered" if previous else "registered", node_id, shard, role, url, node.state)
        return node

    def get(self, node_id: str) -> Node | None:
        with self._lock:
            return self._nodes.get(node_id)

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

    def finish_recovery(self, node_id: str, generation: int, report: dict[str, Any]) -> bool:
        """RECOVERING -> UP, but only for the registration that started this
        recovery. If the node restarted again meanwhile, this result is stale."""
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None or node.generation != generation or node.state != RECOVERING:
                return False
            node.state = UP
            node.last_recovery = report
        log.info("%s caught up and is UP: %s", node_id, report)
        return True

    def sweep(self) -> list[str]:
        """Mark DOWN every node whose last heartbeat is too old. Returns their ids."""
        now = self._clock()
        with self._lock:
            stale = [
                n for n in self._nodes.values()
                if n.state in (UP, RECOVERING) and now - n.last_seen > self.timeout_s
            ]
        for n in stale:
            self.mark_down(n.node_id, f"no heartbeat for {now - n.last_seen:.1f}s")
        return [n.node_id for n in stale]

    # -- who does what ---------------------------------------------------------------------

    def shard_nodes(self, shard: int) -> list[Node]:
        """All nodes of a shard, configured primary first."""
        with self._lock:
            nodes = [n for n in self._nodes.values() if n.shard == shard]
        return sorted(nodes, key=lambda n: ROLES.index(n.role))

    def acting_primary(self, shard: int) -> Node | None:
        """The node that takes client writes for this shard right now.

        Keep the current leader while it is UP (sticky). If it isn't, pick the
        first UP node, preferring the configured primary. Only the coordinator
        decides this, so two nodes never both think they lead (no split brain).
        """
        with self._lock:
            current = self._nodes.get(self._leader.get(shard, ""))
            if current is not None and current.state == UP:
                return current
            live = sorted(
                (n for n in self._nodes.values() if n.shard == shard and n.state == UP),
                key=lambda n: ROLES.index(n.role),
            )
            new = live[0] if live else None
            if new is None:
                self._leader.pop(shard, None)
            else:
                self._leader[shard] = new.node_id
        if new is not None and (current is None or current.node_id != new.node_id):
            log.warning("shard %d is now led by %s", shard, new.node_id)
        return new

    def live_nodes(self, shard: int) -> list[Node]:
        """UP nodes in the order to try them: leader first."""
        leader = self.acting_primary(shard)
        if leader is None:
            return []
        return [leader] + [n for n in self.shard_nodes(shard) if n.state == UP and n.node_id != leader.node_id]

    def replication_targets(self, shard: int, written_on: str) -> list[Node]:
        """Everyone else who must receive a copy: UP and RECOVERING nodes."""
        return [n for n in self.shard_nodes(shard) if n.node_id != written_on and n.state in (UP, RECOVERING)]

    def snapshot(self) -> list[dict]:
        leaders = {}
        for s in range(self.num_shards):
            leader = self.acting_primary(s)
            leaders[s] = leader.node_id if leader else None
        now = self._clock()
        with self._lock:
            nodes = sorted(self._nodes.values(), key=lambda n: (n.shard, ROLES.index(n.role)))
            out = []
            for n in nodes:
                d = asdict(n)
                d.pop("last_seen")
                d["seconds_since_heartbeat"] = round(now - n.last_seen, 1)
                d["is_leader"] = leaders[n.shard] == n.node_id
                out.append(d)
        return out
