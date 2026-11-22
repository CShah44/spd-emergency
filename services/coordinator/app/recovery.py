"""Catch up a node that comes back. DESIGN.md section 8.1.

When a node registers while its partner is UP, it starts RECOVERING:

  1. From that moment the router replicates every new write to it, so it
     can't miss anything that happens during the catch-up.
  2. The two nodes catch up with each other, in BOTH directions, one of two ways:
       * Log catch-up (the normal case). Each side sends only ITS OWN writes
         after the other side's watermark, which is exactly what the other side
         missed (storage_node/app/db.py, "operation log"). The work grows with
         how long the node was gone, not with how much data the shard holds.
       * Full sync (the fallback). Compare both nodes' full state (GET
         /internal/sync, tombstones included) and copy each side's newer
         entries to the other. Used when the two nodes have no watermark for
         each other's current log: the very first sync of a new cluster, a node
         whose disk was wiped (it gets a new log id), or RECOVERY_MODE=full.
         It sets the watermarks, so the next catch-up can use the log.
     Either way, entries go in batches (POST /internal/replicate-batch) and are
     applied with "newer version wins".
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
from .router import Fenced

log = logging.getLogger("coordinator.recovery")

LOG_PAGE = 1000  # log entries per request (and per batch applied on the other side)


class _NodeFailed(Exception):
    def __init__(self, node: Node, exc: Exception):
        super().__init__(f"{node.node_id}: {exc!r}")
        self.node = node


async def recover(
    registry: Registry, http: httpx.AsyncClient, node_id: str, generation: int, mode: str = "auto"
) -> None:
    """Runs as a background task started by /internal/nodes/register.
    mode: "auto" (log catch-up when possible) or "full" (always full sync)."""
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
            report = await _sync_pair(http, node, partner, mode)
        except Fenced:
            return  # we are no longer the leader: the new one handles this node
        except _NodeFailed as exc:
            registry.mark_down(exc.node.node_id, f"failed during recovery of {node_id}: {exc}")
            if exc.node.node_id == node_id:
                return  # the recovering node itself is gone; it will register again
            continue  # the partner is gone; look again (probably finish with our own data)
        registry.finish_recovery(node_id, generation, report)
        return


async def _sync_pair(http: httpx.AsyncClient, node: Node, partner: Node, mode: str) -> dict[str, Any]:
    started = time.monotonic()
    # Positions FIRST: a full sync must know each log's head from before it reads the data.
    mine = await _position(http, node)
    theirs = await _position(http, partner)

    why_full = _full_sync_reason(mode, mine, theirs)
    if why_full is None:
        result = await _log_catch_up(http, node, partner, mine, theirs)
    else:
        result = await _full_sync(http, node, partner, mine, theirs)
        result["why_full"] = why_full

    if result["conflicts"]:
        log.error("%s/%s: %d keys with the same version but different data",
                  node.node_id, partner.node_id, result["conflicts"])
    return {"partner": partner.node_id, **result, "seconds": round(time.monotonic() - started, 3)}


def _full_sync_reason(mode: str, mine: dict | None, theirs: dict | None) -> str | None:
    """None if a log catch-up is possible, else why we fall back to a full sync."""
    if mode == "full":
        return "RECOVERY_MODE=full"
    if mine is None or theirs is None:
        return "a node has no operation log"
    if theirs["log_id"] not in mine["watermarks"] or mine["log_id"] not in theirs["watermarks"]:
        return "first catch-up between these two logs"
    return None


# -- log catch-up ------------------------------------------------------------------------

async def _log_catch_up(
    http: httpx.AsyncClient, node: Node, partner: Node, mine: dict, theirs: dict
) -> dict[str, Any]:
    pull = await _copy_log(http, partner, node, theirs["log_id"], after=mine["watermarks"][theirs["log_id"]])
    push = await _copy_log(http, node, partner, mine["log_id"], after=theirs["watermarks"][mine["log_id"]])
    return {
        "mode": "log",
        "pulled": pull["applied"],
        "pushed": push["applied"],
        "conflicts": pull["conflicts"] + push["conflicts"],
        "entries_read": pull["read"] + push["read"],
    }


async def _copy_log(http: httpx.AsyncClient, src: Node, dst: Node, log_id: str, after: int) -> dict[str, int]:
    """Send `src`'s own writes after seq `after` to `dst`, page by page.

    Each page also moves dst's watermark for src's log. A middle page covers up to
    its last seq; the last (short) page held every entry left in that snapshot,
    so it covers up to the head the same response reported. A seq skipped in
    between belonged to a key that was written again later, and that later write
    has a larger seq, so it is in a later page (or arrives by live replication).
    """
    totals = {"applied": 0, "conflicts": 0, "read": 0}
    while True:
        page = await _get(http, src, "/internal/oplog", params={"after": after, "limit": LOG_PAGE})
        if page["log_id"] != log_id:  # its disk was wiped mid-recovery: give up, it re-registers
            raise _NodeFailed(src, RuntimeError("operation log id changed"))
        entries = page["entries"]
        last_page = len(entries) < LOG_PAGE
        if entries:
            after = entries[-1]["seq"]
        through = max(after, page["head"]) if last_page else after
        result = await _apply(http, dst, entries, {"log_id": log_id, "through": through})
        totals["applied"] += result["applied"]
        totals["conflicts"] += result.get("conflicts", 0)
        totals["read"] += len(entries)
        if last_page:
            return totals


# -- full sync -----------------------------------------------------------------------------

async def _full_sync(
    http: httpx.AsyncClient, node: Node, partner: Node, mine_pos: dict | None, theirs_pos: dict | None
) -> dict[str, Any]:
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
        log.error("conflicting keys (first 5): %s", conflicts[:5])

    # After this, each side holds everything the other's log had at the moment we
    # read its position (before reading the data), so the watermarks start there.
    def origin(pos: dict | None) -> dict | None:
        return None if pos is None else {"log_id": pos["log_id"], "through": pos["head"]}

    pulled = await _apply(http, node, pull, origin(theirs_pos) if mine_pos else None)
    pushed = await _apply(http, partner, push, origin(mine_pos) if theirs_pos else None)
    return {
        "mode": "full",
        "pulled": pulled["applied"],
        "pushed": pushed["applied"],
        "conflicts": len(conflicts),
        "entries_read": len(mine) + len(theirs),
    }


async def _entries(http: httpx.AsyncClient, node: Node) -> dict[str, dict[str, Any]]:
    resp = await _get(http, node, "/internal/sync")
    return {e["key"]: e for e in resp["entries"]}


# -- calls to the nodes ----------------------------------------------------------------------

async def _position(http: httpx.AsyncClient, node: Node) -> dict[str, Any] | None:
    """The node's log position, or None if it is an older node without an operation log."""
    try:
        resp = await http.get(node.url + "/internal/oplog/position", timeout=30.0)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise _NodeFailed(node, exc)
    return resp.json()


async def _get(http: httpx.AsyncClient, node: Node, path: str, **kwargs: Any) -> dict[str, Any]:
    try:
        resp = await http.get(node.url + path, timeout=30.0, **kwargs)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise _NodeFailed(node, exc)
    return resp.json()


async def _apply(
    http: httpx.AsyncClient, node: Node, entries: list[dict[str, Any]], origin: dict | None
) -> dict[str, Any]:
    """POST a batch. With `origin`, sent even when empty: it still moves the watermark."""
    if not entries and origin is None:
        return {"applied": 0, "conflicts": 0}
    body: dict[str, Any] = {"entries": [{k: v for k, v in e.items() if k != "seq"} for e in entries]}
    if origin is not None:
        body["origin"] = origin
    try:
        resp = await http.post(node.url + "/internal/replicate-batch", json=body, timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise _NodeFailed(node, exc)
    return resp.json()
