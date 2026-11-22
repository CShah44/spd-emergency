"""Key -> shard -> node(s). DESIGN.md sections 6 and 8.

Writes (synchronous replication):
  1. Send the client write to the shard's acting primary. It runs the
     compare-and-set and picks the new version.
  2. Copy the result to the shard's other node (UP or RECOVERING) with
     /internal/replicate ("store exactly version N if newer").
  3. Answer the client only after both steps. If the replica can't be reached
     it is marked DOWN; the write is still safe on the primary and the replica
     catches up when it comes back (recovery). This is "degraded mode".
  The copy also carries the write's place in the primary's operation log
  (origin = log id + seq), so the replica knows how far it is caught up.

Reads go to the acting primary, falling back to the replica.

Failover: if a node can't be reached we mark it DOWN and try the next one.
That's only safe when we KNOW the write didn't happen:
  * connection refused / connect timeout / node answered 503  -> never reached
    the data, so retrying on the partner is safe.
  * read timeout (the request was sent, no answer came back)  -> AMBIGUOUS: the
    write may or may not have been applied. Retrying a compare-and-set could
    report 409 for a claim that actually succeeded, so for writes we answer 503
    and let the caller decide. Reads are always safe to retry.
"""

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from common import metrics
from common.sharding import CELLS_PARAM, parse_cells, shard_for, shards_for_cells

from .registry import Node, Registry

log = logging.getLogger("coordinator.router")

# Errors where the request provably never reached the node's handler.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout)


class NoLiveNode(Exception):
    """Every node of the shard is DOWN -> 503."""

    def __init__(self, shard: int):
        super().__init__(f"no live storage node for shard {shard}")
        self.shard = shard


class AmbiguousWrite(Exception):
    """The node may or may not have applied the write -> 503."""


class Fenced(Exception):
    """A storage node refused our leadership epoch: a newer coordinator leads -> 503.
    Raised by the HTTP client's response hook (main.py), whatever the call was."""


class NodeAnswer(Exception):
    """A normal 4xx answer from a node (404, 409, 400), passed through to the client."""

    def __init__(self, status_code: int, detail: Any):
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class Router:
    def __init__(self, registry: Registry, http: httpx.AsyncClient):
        self.registry = registry
        self.http = http

    def shard_of(self, key: str) -> int:
        return shard_for(key, self.registry.num_shards)

    # -- client operations ---------------------------------------------------------------

    async def put(self, key: str, record: dict[str, Any], if_match: str | None) -> dict[str, Any]:
        shard = self.shard_of(key)
        headers = {} if if_match is None else {"If-Match": if_match}
        node, resp = await self._first_live(
            shard, "PUT", _data_path(key), json=record, headers=headers, is_write=True
        )
        saved = _ok_json(resp)
        await self._replicate(
            shard, key, written_on=node, payload={"version": saved["version"], "record": saved, **_origin(resp)}
        )
        return saved

    async def get(self, key: str) -> dict[str, Any]:
        _, resp = await self._first_live(self.shard_of(key), "GET", _data_path(key), is_write=False)
        return _ok_json(resp)

    async def delete(self, key: str) -> None:
        shard = self.shard_of(key)
        node, resp = await self._first_live(shard, "DELETE", _data_path(key), is_write=True)
        tombstone = _ok_json(resp)
        await self._replicate(
            shard, key, written_on=node, payload={"version": tombstone["version"], "deleted": True, **_origin(resp)}
        )

    async def query(self, params: dict[str, str]) -> list[dict[str, Any]]:
        """Scatter-gather: ask the shards at the same time, then merge.

        Without ?cells= every shard is asked: records are spread by key, not by
        type or status. With ?cells= (geo-sharding, docs/GEO_SHARDING.md) only
        the shards owning those cells are asked, and each returns only records
        in them, so "free water near X" usually touches one shard.

        If an asked shard has no live node we fail (503) rather than return a
        partial list: a matcher seeing "no water resources" when one shard is
        just unreachable would wrongly mark requests UNMATCHED.
        """
        cells = parse_cells(params.get(CELLS_PARAM))  # ValueError -> 400 (main.py)
        shards = range(self.registry.num_shards) if cells is None else sorted(
            shards_for_cells(sorted(cells), self.registry.num_shards)
        )

        async def one(shard: int) -> list[dict[str, Any]]:
            _, resp = await self._first_live(shard, "GET", "/internal/data", params=params, is_write=False)
            return _ok_json(resp)

        metrics.SHARDS_PER_QUERY.observe(len(shards))
        per_shard = await asyncio.gather(*(one(s) for s in shards))
        return [record for records in per_shard for record in records]

    # -- internals ---------------------------------------------------------------------------

    async def _first_live(
        self, shard: int, method: str, path: str, *, is_write: bool, **kwargs: Any
    ) -> tuple[Node, httpx.Response]:
        """Send to the acting primary; on a node failure mark it DOWN and try the next."""
        tried: set[str] = set()
        while True:
            candidates = [n for n in self.registry.live_nodes(shard) if n.node_id not in tried]
            if not candidates:
                raise NoLiveNode(shard)
            node = candidates[0]
            tried.add(node.node_id)
            try:
                resp = await self.http.request(method, node.url + path, **kwargs)
            except _NOT_SENT as exc:
                self.registry.mark_down(node.node_id, f"unreachable: {exc!r}")
                continue
            except httpx.TransportError as exc:
                self.registry.mark_down(node.node_id, f"no answer: {exc!r}")
                if is_write:
                    raise AmbiguousWrite(f"{node.node_id} did not answer; the write may or may not be saved")
                continue
            if resp.status_code == 503:  # paused or unhealthy: did not touch the data
                self.registry.mark_down(node.node_id, "answered 503")
                continue
            return node, resp

    async def _replicate(self, shard: int, key: str, written_on: Node, payload: dict[str, Any]) -> None:
        # RECOVERING nodes get copies too, so they can't miss writes made during catch-up.
        for target in self.registry.replication_targets(shard, written_on.node_id):
            try:
                resp = await self.http.put(target.url + _replicate_path(key), json=payload)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                # Don't fail the client: the write is durable on `written_on`.
                # Marking the replica DOWN forces it to catch up before it is used again.
                metrics.REPLICATION_FAILURES.labels(str(shard)).inc()
                self.registry.mark_down(target.node_id, f"replication of {key} failed: {exc!r}")


def _origin(resp: httpx.Response) -> dict[str, Any]:
    """The write's place in the writing node's log, to send along with the replica
    copy (storage_node/app/db.py, "operation log"). Empty if the node didn't say."""
    log_id, seq = resp.headers.get("X-Log-Id"), resp.headers.get("X-Log-Seq")
    if not log_id or not seq or not seq.isdigit():
        return {}
    return {"origin": {"log_id": log_id, "seq": int(seq)}}


def _data_path(key: str) -> str:
    return "/internal/data/" + quote(key, safe=":")


def _replicate_path(key: str) -> str:
    return "/internal/replicate/" + quote(key, safe=":")


def _ok_json(resp: httpx.Response) -> Any:
    if resp.is_success:
        return resp.json()
    try:
        detail = resp.json().get("detail")
    except Exception:
        detail = resp.text
    raise NodeAnswer(resp.status_code, detail)
