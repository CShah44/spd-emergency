"""The operator's view of the system, for the web page: which storage nodes are up,
who leads each shard, how full the queue is, and (for demos) pausing a node.

Pausing is the same simulated failure the demo scripts use (POST /admin/pause on
the node): it stops answering and stops heartbeating, the coordinator marks it
DOWN and the shard's other node takes over. Resuming makes it register again and
catch up. Anyone who can reach the API could do this, so it only works when
DEMO_CONTROLS is switched on (see common/config.py).
"""

from typing import Any

import httpx
from fastapi import HTTPException

from common import config
from common.storage_client import StorageClient

from .publisher import Publisher

NODE_ADMIN_TIMEOUT_S = 3.0


class ClusterView:
    def __init__(self, store: StorageClient, publisher: Publisher, admin: httpx.Client | None = None):
        self._store = store
        self._publisher = publisher
        self._admin = admin or httpx.Client(timeout=NODE_ADMIN_TIMEOUT_S)

    def close(self) -> None:
        self._admin.close()

    def overview(self) -> dict[str, Any]:
        """Everything the page's cluster panel shows. Storage errors become HTTP
        answers in main.py; RabbitMQ being down is not an error here, it is shown as `queue: null`."""
        status = self._store.status()
        return {
            "mode": status.get("mode"),
            "nodes": status.get("nodes", []),
            "shards": status.get("shards", {}),
            "total_keys": status.get("total_keys"),
            "queue": self._publisher.queue_stats(),
            "controls_enabled": config.DEMO_CONTROLS,
        }

    def set_node_paused(self, node_id: str, paused: bool) -> dict[str, Any]:
        if not config.DEMO_CONTROLS:
            raise HTTPException(403, detail="node controls are switched off (set DEMO_CONTROLS=true)")
        node = next((n for n in self._store.status().get("nodes", []) if n.get("node_id") == node_id), None)
        if node is None or not node.get("url"):
            raise HTTPException(404, detail=f"unknown storage node {node_id!r}")
        action = "pause" if paused else "resume"
        try:
            answer = self._admin.post(f"{node['url']}/admin/{action}")
            answer.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(502, detail=f"could not {action} {node_id}: {exc!r}")
        return {"node_id": node_id, "paused": paused}
