"""The real coordinator. Same /store API as fake.py (DESIGN.md section 8), so
the app services can't tell the difference, plus the node-facing calls
(section 8.1) and a live /internal/status.

Run locally (with storage nodes pointing COORDINATOR_URL here):
    uvicorn coordinator.app.main:app --app-dir services --port 8000

High availability (roadmap task 3): with LEADER_ELECTION=kubernetes, run two
replicas. They elect a leader through a Kubernetes Lease (election.py); only
the leader serves, and it labels its pod so the Service routes to it
(leadership.py). Every call to a storage node carries the leader's epoch
(X-Coordinator-Epoch), and nodes refuse an older one: fencing. With
LEADER_ELECTION=off (the default) it is a single coordinator, as before.
"""

import asyncio
import contextlib
import logging
import os
import socket
from typing import Any, Literal

import httpx
from fastapi import Body, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from common import config

from .election import KubernetesLeaseBackend, LeaderElector, LeaseBackend
from .leadership import Leadership
from .recovery import recover
from .registry import RECOVERING, Registry, RegistrationError
from .router import AmbiguousWrite, Fenced, NoLiveNode, NodeAnswer, Router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("coordinator")

EPOCH_HEADER = "X-Coordinator-Epoch"
# Paths a standby (or a settling leader) still answers.
ALWAYS_OPEN = ("/health", "/livez", "/ready", "/internal/leader", "/docs", "/openapi.json")


class RegisterBody(BaseModel):
    node_id: str
    shard: int
    role: Literal["primary", "replica"]
    url: str
    position: dict[str, Any] | None = None  # the node's operation-log position (for takeovers)


class HeartbeatBody(BaseModel):
    node_id: str


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _elector_from_env() -> LeaderElector | None:
    mode = os.environ.get("LEADER_ELECTION", "off")
    if mode == "off":
        return None
    if mode != "kubernetes":
        raise ValueError(f"LEADER_ELECTION must be off or kubernetes, got {mode!r}")
    return LeaderElector(
        KubernetesLeaseBackend(os.environ.get("LEASE_NAME", "coordinator-leader")),
        identity=os.environ.get("POD_NAME") or socket.gethostname(),
        lease_duration_s=_env_float("LEASE_DURATION_S", 6.0),
        renew_deadline_s=_env_float("RENEW_DEADLINE_S", 4.0),
        retry_period_s=_env_float("RETRY_PERIOD_S", 1.0),
    )


def create_app(
    num_shards: int | None = None,
    heartbeat_timeout: float | None = None,
    sweep_interval: float = 1.0,
    node_transport: httpx.AsyncBaseTransport | None = None,
    node_timeout: float = 2.0,
    recovery_mode: str | None = None,
    elector: LeaderElector | None | Literal["env"] = "env",
    settle_timeout: float | None = None,
) -> FastAPI:
    """`node_transport` lets tests route node calls to in-process apps.
    `recovery_mode`: "auto" (catch up from the operation log when possible) or
    "full" (always compare everything; for benchmarks). Default: $RECOVERY_MODE or auto.
    `elector`: "env" builds it from LEADER_ELECTION (off by default); tests pass
    one on a shared in-memory lease, or None for a single coordinator."""
    recovery_mode = recovery_mode or os.environ.get("RECOVERY_MODE", "auto")
    if recovery_mode not in ("auto", "full"):
        raise ValueError(f"RECOVERY_MODE must be auto or full, got {recovery_mode!r}")
    if elector == "env":
        elector = _elector_from_env()
    registry = Registry(
        num_shards if num_shards is not None else config.NUM_SHARDS,
        heartbeat_timeout if heartbeat_timeout is not None else config.HEARTBEAT_TIMEOUT_S,
    )
    holder: dict[str, Router] = {}
    recoveries: set[asyncio.Task] = set()

    def router() -> Router:
        return holder["router"]

    async def admit(reg: dict[str, Any]) -> str:
        """Put a registered node into the registry; start its catch-up if needed."""
        node = registry.register(reg["node_id"], reg["shard"], reg["role"], reg["url"])
        if node.state == RECOVERING:
            # Catch up in the background; the node is told it's RECOVERING
            # and keeps heartbeating meanwhile.
            task = asyncio.create_task(
                recover(registry, router().http, node.node_id, node.generation, app.state.recovery_mode)
            )
            recoveries.add(task)  # keep a reference so the task isn't garbage-collected
            task.add_done_callback(recoveries.discard)
        return node.state

    def cancel_recoveries() -> None:
        for task in list(recoveries):
            task.cancel()

    leadership = Leadership(
        registry,
        elector,
        settle_timeout if settle_timeout is not None else 2 * config.HEARTBEAT_INTERVAL_S + 1,
        admit,
        cancel_recoveries,
    )

    async def sweeper() -> None:
        # Active failure detection: check heartbeat ages every second.
        while True:
            registry.sweep()
            await asyncio.sleep(sweep_interval)

    # Every call to a storage node carries our epoch, and a "fenced" answer means
    # a newer leader exists: step down at once, whatever the call was.
    async def add_epoch(request: httpx.Request) -> None:
        if leadership.epoch is not None:
            request.headers[EPOCH_HEADER] = str(leadership.epoch)

    async def check_fenced(response: httpx.Response) -> None:
        if response.status_code != 409 or not leadership.enabled:
            return
        await response.aread()
        try:
            detail = response.json().get("detail")
        except ValueError:
            return
        if isinstance(detail, dict) and detail.get("error") == "fenced":
            await leadership.fenced(detail)
            raise Fenced(f"{response.request.url.host} refused epoch {leadership.epoch}: {detail}")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # Connecting inside the cluster network takes milliseconds, so a short
        # connect limit lets us fail over to the partner quickly when a node is
        # dead. Waiting for an ANSWER keeps the longer limit (a busy node is slow).
        timeout = httpx.Timeout(node_timeout, connect=min(node_timeout, 0.5))
        hooks = {"request": [add_epoch], "response": [check_fenced]}
        async with httpx.AsyncClient(timeout=timeout, transport=node_transport, event_hooks=hooks) as http:
            holder["router"] = Router(registry, http)
            tasks = [asyncio.create_task(sweeper())]
            if elector is not None:
                tasks.append(asyncio.create_task(elector.run()))
                tasks.append(asyncio.create_task(leadership.label_keeper()))
            yield
            if elector is not None:
                await elector.release()  # a clean shutdown hands over at once
                await leadership.sync_label()  # and stop attracting traffic
            for t in [*tasks, *recoveries]:
                t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, *recoveries, return_exceptions=True)

    app = FastAPI(title="Coordinator", lifespan=lifespan)
    app.state.registry = registry
    app.state.leadership = leadership
    app.state.recovery_mode = recovery_mode  # read at each registration, so a benchmark can switch it

    @app.middleware("http")
    async def leader_gate(request: Request, call_next):
        path = request.url.path
        if not leadership.enabled or path in ALWAYS_OPEN:
            return await call_next(request)
        if not leadership.is_leader():
            return JSONResponse({"detail": "standby coordinator: not the leader"}, status_code=503)
        if leadership.settling and path.startswith("/store"):
            return JSONResponse({"detail": "new leader is settling, retry shortly"}, status_code=503)
        return await call_next(request)

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

    @app.exception_handler(Fenced)
    async def _fenced(_: Request, exc: Fenced):
        return JSONResponse({"detail": f"no longer the leader: {exc}"}, status_code=503)

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
    async def register(body: RegisterBody) -> dict[str, Any]:
        reg = body.model_dump()
        try:
            if leadership.offer(reg):  # a new leader collects first, admits later
                return {"ok": True, "state": "SETTLING", "epoch": leadership.epoch}
            state = await admit(reg)
        except RegistrationError as exc:
            raise HTTPException(exc.status_code, detail=str(exc))
        return {"ok": True, "state": state, "epoch": leadership.epoch}

    @app.post("/internal/nodes/heartbeat")
    def heartbeat(body: HeartbeatBody) -> dict[str, Any]:
        if not (leadership.is_pending(body.node_id) or registry.heartbeat(body.node_id)):
            raise HTTPException(404, detail=f"{body.node_id} unknown or DOWN: register again")
        return {"ok": True, "epoch": leadership.epoch}

    # -- leadership ------------------------------------------------------------------------

    @app.get("/ready")
    def ready() -> Response:
        """200 only on the leader (for people and scripts). Not the readiness
        probe: that is /livez, so rolling updates work; routing to the leader is
        done with a pod label (election.py)."""
        if leadership.is_leader():
            return JSONResponse({"ready": True, **leadership.describe()})
        return JSONResponse({"ready": False, **leadership.describe()}, status_code=503)

    @app.get("/livez")
    def livez() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/internal/leader")
    def leader_info() -> dict[str, Any]:
        return leadership.describe()

    # -- observability --------------------------------------------------------------------

    @app.get("/internal/status")
    async def status() -> dict[str, Any]:
        """Every node, its state, who leads each shard, and how many keys each holds."""
        nodes = registry.snapshot()

        async def key_count(node: dict[str, Any]) -> int | None:
            if node["state"] == "DOWN":
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
                "recovering": [n.node_id for n in registry.shard_nodes(s) if n.state == RECOVERING],
                "available": leader is not None,
            }
        keys_per_shard = {
            s: next((n["keys"] for n in nodes if n["node_id"] == info["acting_primary"]), None)
            for s, info in shards.items()
        }
        return {
            "mode": "real",
            "coordinator": leadership.describe(),
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
