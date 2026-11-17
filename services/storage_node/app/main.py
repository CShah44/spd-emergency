"""Storage node HTTP API. DESIGN.md section 8.1. Only the coordinator calls it.

    PUT    /internal/data/{key}          client write (optional If-Match -> compare-and-set)
    GET    /internal/data/{key}          one live record
    DELETE /internal/data/{key}          tombstone; returns its version for replication
    GET    /internal/data?prefix=&f=v    search live records on THIS node
    PUT    /internal/replicate/{key}     "store exactly version N" if newer
    POST   /internal/replicate-batch     the same for many entries (recovery)
    GET    /internal/sync?prefix=        every entry incl. tombstones (for recovery)
    GET    /health                       identity + key count (503 while paused): readiness
    GET    /livez                        process is alive (even while paused): liveness
    POST   /admin/pause | /admin/resume  simulate a failure on demand (demos)

Settings come from environment variables (see NodeSettings.from_env).
Run locally:
    set NODE_ID=s0-primary & set DATA_DIR=./data
    uvicorn storage_node.app.main:app --app-dir services --port 9000
"""

import asyncio
import contextlib
import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common import config
from common.models import utc_now_iso

from .db import Conflict, NotFound, Store
from .heartbeat import heartbeat_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@dataclass(frozen=True)
class NodeSettings:
    node_id: str
    shard_id: int
    role: str  # "primary" or "replica": the role this node STARTS with
    data_dir: str
    coordinator_url: str
    node_url: str  # how the coordinator reaches us, e.g. http://storage-s0-primary:9000
    heartbeat_interval: float = 3.0
    heartbeat_enabled: bool = True

    @classmethod
    def from_env(cls) -> "NodeSettings":
        port = os.environ.get("PORT", "9000")
        shard = int(os.environ.get("SHARD_ID", "0"))
        role = os.environ.get("ROLE", "primary")
        if role == "from-ordinal":
            # Kubernetes StatefulSet: pods are named <set>-0, <set>-1, ... and keep
            # that name across restarts. Ordinal 0 is the primary, 1 the replica.
            role = role_from_ordinal(os.environ.get("POD_NAME") or socket.gethostname())
        if role not in ("primary", "replica"):
            raise ValueError(f"ROLE must be primary, replica or from-ordinal, got {role!r}")
        return cls(
            node_id=os.environ.get("NODE_ID") or f"s{shard}-{role}",
            shard_id=shard,
            role=role,
            data_dir=os.environ.get("DATA_DIR", "./data"),
            coordinator_url=config.COORDINATOR_URL,
            node_url=os.environ.get("NODE_URL", f"http://{socket.gethostname()}:{port}"),
            heartbeat_interval=config.HEARTBEAT_INTERVAL_S,
            heartbeat_enabled=os.environ.get("HEARTBEAT_ENABLED", "true").lower() == "true",
        )


def role_from_ordinal(pod_name: str) -> str:
    """'shard0-0' -> 'primary', 'shard0-1' -> 'replica'. A third pod would also say
    'replica'; the coordinator then refuses it (409: slot taken)."""
    try:
        ordinal = int(pod_name.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        raise ValueError(f"ROLE=from-ordinal needs a StatefulSet pod name like shard0-1, got {pod_name!r}")
    return "primary" if ordinal == 0 else "replica"


class NodeState:
    """Everything that changes while the node runs."""

    def __init__(self, settings: NodeSettings):
        self.settings = settings
        self.store: Store | None = None
        self.paused = False
        self.needs_register = True
        self.last_heartbeat_ok: bool | None = None
        self.started_at = utc_now_iso()

    def identity(self) -> dict[str, Any]:
        s = self.settings
        return {"node_id": s.node_id, "shard": s.shard_id, "role": s.role, "url": s.node_url}


class ReplicatePayload(BaseModel):
    version: int = Field(ge=1)
    deleted: bool = False
    record: dict[str, Any] | None = None


class SyncEntry(BaseModel):
    key: str
    version: int = Field(ge=1)
    deleted: bool = False
    record: dict[str, Any] | None = None


class ReplicateBatch(BaseModel):
    entries: list[SyncEntry]


def _parse_if_match(value: str) -> int:
    raw = value.strip().removeprefix("W/").strip('"')
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(400, detail=f"If-Match must be an integer version, got {value!r}")


def create_app(settings: NodeSettings | None = None) -> FastAPI:
    settings = settings or NodeSettings.from_env()
    state = NodeState(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # Open the database at startup (not at import), so importing this
        # module never creates files.
        state.store = Store(Path(settings.data_dir) / f"{settings.node_id}.db")
        task = asyncio.create_task(heartbeat_loop(state)) if settings.heartbeat_enabled else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        state.store.close()

    app = FastAPI(title=f"Storage node {settings.node_id}", lifespan=lifespan)
    app.state.node = state

    def store() -> Store:
        assert state.store is not None, "store is opened in lifespan"
        return state.store

    @app.middleware("http")
    async def pause_gate(request: Request, call_next):
        # A paused node behaves like a dead one: no answers, no heartbeats.
        # /admin stays open so we can resume it, and /livez so Kubernetes
        # doesn't restart a paused pod in the middle of a demo.
        if state.paused and not request.url.path.startswith(("/admin", "/livez")):
            return JSONResponse({"detail": f"{settings.node_id} is paused"}, status_code=503)
        return await call_next(request)

    # -- client data ------------------------------------------------------------------

    @app.put("/internal/data/{key}")
    def put_data(
        key: str,
        record: dict[str, Any] = Body(...),
        if_match: str | None = Header(default=None),
    ) -> dict[str, Any]:
        expected = None if if_match is None else _parse_if_match(if_match)
        try:
            return store().put(key, record, expected)
        except NotFound:
            raise HTTPException(404, detail=f"{key} not found")
        except Conflict as exc:
            raise HTTPException(
                409,
                detail={
                    "error": "version_conflict",
                    "key": key,
                    "expected_version": expected,
                    "current_version": exc.current_version,
                },
            )

    @app.get("/internal/data/{key}")
    def get_data(key: str) -> dict[str, Any]:
        record = store().get(key)
        if record is None:
            raise HTTPException(404, detail=f"{key} not found")
        return record

    @app.delete("/internal/data/{key}")
    def delete_data(key: str) -> dict[str, Any]:
        try:
            version = store().delete(key)
        except NotFound:
            raise HTTPException(404, detail=f"{key} not found")
        # The coordinator forwards this tombstone version to the replica.
        return {"key": key, "version": version, "deleted": True}

    @app.get("/internal/data")
    def query_data(request: Request, prefix: str = "") -> list[dict[str, Any]]:
        filters = {k: v for k, v in request.query_params.items() if k != "prefix"}
        return store().scan(prefix, filters)

    # -- replication and recovery ---------------------------------------------------------

    @app.put("/internal/replicate/{key}")
    def replicate(key: str, payload: ReplicatePayload) -> dict[str, Any]:
        if not payload.deleted and payload.record is None:
            raise HTTPException(400, detail="record is required unless deleted=true")
        applied = store().apply_replica(key, payload.version, payload.record, payload.deleted)
        # applied=False is NOT an error: we already had this version or a newer one.
        return {"key": key, "applied": applied, "version": store().version_of(key)}

    @app.post("/internal/replicate-batch")
    def replicate_batch(payload: ReplicateBatch) -> dict[str, Any]:
        """Recovery: many entries in one call and one transaction, same "newer wins" rule."""
        for e in payload.entries:
            if not e.deleted and e.record is None:
                raise HTTPException(400, detail=f"{e.key}: record is required unless deleted=true")
        applied = store().apply_many([e.model_dump() for e in payload.entries])
        return {"received": len(payload.entries), "applied": applied}

    @app.get("/internal/sync")
    def sync(prefix: str = "") -> dict[str, Any]:
        return {"node_id": settings.node_id, "entries": store().sync_entries(prefix)}

    # -- health and demo controls ---------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            **state.identity(),
            "keys": store().count(),
            "coordinator_reachable": state.last_heartbeat_ok,
            "started_at": state.started_at,
        }

    @app.get("/livez")
    def livez() -> dict[str, str]:
        """Liveness: is the process alive? (Kubernetes restarts the pod if not.)
        Readiness uses /health, which also says 'can serve data'."""
        return {"status": "alive"}

    @app.post("/admin/pause")
    def pause() -> dict[str, Any]:
        state.paused = True
        return {"node_id": settings.node_id, "paused": True}

    @app.post("/admin/resume")
    def resume() -> dict[str, Any]:
        state.paused = False
        state.needs_register = True  # re-register = "I'm back", which triggers catch-up
        return {"node_id": settings.node_id, "paused": False}

    return app


app = create_app()
