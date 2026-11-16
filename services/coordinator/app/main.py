"""The real coordinator. Same /store API as fake.py (DESIGN.md section 8), so
the app services can't tell the difference, plus the node-facing calls
(section 8.1) and a live /internal/status.

Run locally (with storage nodes pointing COORDINATOR_URL here):
    uvicorn coordinator.app.main:app --app-dir services --port 8000
"""

import asyncio
import contextlib
import logging
from typing import Any, Literal

import httpx
from fastapi import Body, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from common import config

from .registry import UP, Registry, RegistrationError
from .router import AmbiguousWrite, NoLiveNode, NodeAnswer, Router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("coordinator")


class RegisterBody(BaseModel):
    node_id: str
    shard: int
    role: Literal["primary", "replica"]
    url: str


class HeartbeatBody(BaseModel):
    node_id: str


def create_app(
    num_shards: int | None = None,
    heartbeat_timeout: float | None = None,
    sweep_interval: float = 1.0,
    node_transport: httpx.AsyncBaseTransport | None = None,
    node_timeout: float = 2.0,
) -> FastAPI:
    """`node_transport` lets tests route node calls to in-process apps."""
    registry = Registry(
        num_shards if num_shards is not None else config.NUM_SHARDS,
        heartbeat_timeout if heartbeat_timeout is not None else config.HEARTBEAT_TIMEOUT_S,
    )
    holder: dict[str, Router] = {}

    async def sweeper() -> None:
        # Active failure detection: check heartbeat ages every second.
        while True:
            registry.sweep()
            await asyncio.sleep(sweep_interval)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(timeout=node_timeout, transport=node_transport) as http:
            holder["router"] = Router(registry, http)
            task = asyncio.create_task(sweeper())
            yield
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Coordinator", lifespan=lifespan)
    app.state.registry = registry

    def router() -> Router:
        return holder["router"]

    # Translate router errors into HTTP answers, in one place.
    @app.exception_handler(NodeAnswer)
    async def _node_answer(_: Request, exc: NodeAnswer):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(NoLiveNode)
    async def _no_live(_: Request, exc: NoLiveNode):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(AmbiguousWrite)
    async def _ambiguous(_: Request, exc: AmbiguousWrite):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    # -- client API (section 8) ---------------------------------------------------------

    @app.put("/store/{key}")
    async def put_record(
        key: str, record: dict[str, Any] = Body(...), if_match: str | None = Header(default=None)
    ) -> dict[str, Any]:
        return await router().put(key, record, if_match)

    @app.get("/store/{key}")
    async def get_record(key: str) -> dict[str, Any]:
        return await router().get(key)

    @app.delete("/store/{key}", status_code=204)
    async def delete_record(key: str) -> Response:
        await router().delete(key)
        return Response(status_code=204)

    @app.get("/store")
    async def query_records(request: Request) -> list[dict[str, Any]]:
        records = await router().query(dict(request.query_params))
        return sorted(records, key=lambda r: str(r.get("id", "")))

    # -- node API (section 8.1) ---------------------------------------------------------

    @app.post("/internal/nodes/register")
    def register(body: RegisterBody) -> dict[str, Any]:
        try:
            node = registry.register(body.node_id, body.shard, body.role, body.url)
        except RegistrationError as exc:
            raise HTTPException(exc.status_code, detail=str(exc))
        return {"ok": True, "state": node.state}

    @app.post("/internal/nodes/heartbeat")
    def heartbeat(body: HeartbeatBody) -> dict[str, Any]:
        if not registry.heartbeat(body.node_id):
            raise HTTPException(404, detail=f"{body.node_id} unknown or DOWN: register again")
        return {"ok": True}

    # -- observability --------------------------------------------------------------------

    @app.get("/internal/status")
    async def status() -> dict[str, Any]:
        """Every node, its state, who leads each shard, and how many keys each holds."""
        nodes = registry.snapshot()

        async def key_count(node: dict[str, Any]) -> int | None:
            if node["state"] != UP:
                return None
            try:
                resp = await router().http.get(node["url"] + "/health", timeout=1.0)
                return resp.json()["keys"] if resp.is_success else None
            except httpx.HTTPError:
                return None

        counts = await asyncio.gather(*(key_count(n) for n in nodes))
        for node, keys in zip(nodes, counts):
            node["keys"] = keys

        shards = {}
        for s in range(registry.num_shards):
            leader = registry.acting_primary(s)
            shards[str(s)] = {
                "acting_primary": leader.node_id if leader else None,
                "live_nodes": [n.node_id for n in registry.live_nodes(s)],
                "available": leader is not None,
            }
        keys_per_shard = {
            s: next((n["keys"] for n in nodes if n["node_id"] == info["acting_primary"]), None)
            for s, info in shards.items()
        }
        return {
            "mode": "real",
            "nodes": nodes,
            "shards": shards,
            "keys_per_shard": keys_per_shard,
            "total_keys": sum(v for v in keys_per_shard.values() if v is not None),
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "real"}

    return app


app = create_app()
