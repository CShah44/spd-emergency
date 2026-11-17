"""Catch up a node that comes back. DESIGN.md section 8.1.

When a node registers while its partner is UP, it starts RECOVERING:

  1. From that moment the router replicates every new write to it, so it
     can't miss anything that happens during the catch-up.
  2. We compare both nodes' full state (GET /internal/sync, tombstones
     included) and copy each side's newer entries to the other side
     (POST /internal/replicate-batch, "newer version wins").
  3. Then it becomes UP.

Steps 1 and 2 overlap, and that's safe: applying an entry twice changes
nothing (idempotent), and an older entry can never overwrite a newer one. So it
doesn't matter whether a write arrives by live replication, by the catch-up,
or both, in any order.

Why copy BOTH ways? If both nodes of a shard were down, the staler one may
come back first and become UP on its own data. When the fresher one returns,
pushing its newer entries to the leader repairs that.

Known limit: if both nodes changed the same key to the same version number
independently (possible only after a whole-shard outage), "newer wins" can't
choose. We count and log those as conflicts instead of guessing.
"""

import logging
import time
from typing import Any

import httpx

from .registry import RECOVERING, UP, Node, Registry

log = logging.getLogger("coordinator.recovery")


class _NodeFailed(Exception):
    def __init__(self, node: Node, exc: Exception):
        super().__init__(f"{node.node_id}: {exc!r}")
        self.node = node


async def recover(registry: Registry, http: httpx.AsyncClient, node_id: str, generation: int) -> None:
    """Runs as a background task started by /internal/nodes/register."""
    while True:
        node = registry.get(node_id)
        if node is None or node.generation != generation or node.state != RECOVERING:
            return  # superseded: the node failed or registered again
        partner = next(
            (n for n in registry.shard_nodes(node.shard) if n.node_id != node_id and n.state == UP), None
        )
        if partner is None:
            # The partner died meanwhile: our own data is the best copy left.
            registry.finish_recovery(node_id, generation, {"partner": None, "note": "no live partner"})
            return
        try:
            report = await _sync_pair(http, node, partner)
        except _NodeFailed as exc:
            registry.mark_down(exc.node.node_id, f"failed during recovery of {node_id}: {exc}")
            if exc.node.node_id == node_id:
                return  # the recovering node itself is gone; it will register again
            continue  # the partner is gone; look again (probably finish with our own data)
        registry.finish_recovery(node_id, generation, report)
        return


async def _sync_pair(http: httpx.AsyncClient, node: Node, partner: Node) -> dict[str, Any]:
    started = time.monotonic()
    mine = await _entries(http, node)
    theirs = await _entries(http, partner)

    pull = [e for k, e in theirs.items() if e["version"] > mine.get(k, {}).get("version", 0)]
    push = [e for k, e in mine.items() if e["version"] > theirs.get(k, {}).get("version", 0)]
    conflicts = [
        k for k, e in mine.items()
        if k in theirs and theirs[k]["version"] == e["version"]
        and (theirs[k]["deleted"], theirs[k]["record"]) != (e["deleted"], e["record"])
    ]
    if conflicts:
        log.error("%s/%s: %d keys with the same version but different data: %s",
                  node.node_id, partner.node_id, len(conflicts), conflicts[:5])

    pulled = await _apply(http, node, pull)
    pushed = await _apply(http, partner, push)
    return {
        "partner": partner.node_id,
        "pulled": pulled,
        "pushed": pushed,
        "conflicts": len(conflicts),
        "seconds": round(time.monotonic() - started, 3),
    }


async def _entries(http: httpx.AsyncClient, node: Node) -> dict[str, dict[str, Any]]:
    try:
        resp = await http.get(node.url + "/internal/sync", timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise _NodeFailed(node, exc)
    return {e["key"]: e for e in resp.json()["entries"]}


async def _apply(http: httpx.AsyncClient, node: Node, entries: list[dict[str, Any]]) -> int:
    if not entries:
        return 0
    try:
        resp = await http.post(node.url + "/internal/replicate-batch", json={"entries": entries}, timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise _NodeFailed(node, exc)
    return resp.json()["applied"]
