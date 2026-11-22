"""The Delta Command web page's server.

The page itself is a small Svelte app (../web, built into app/static). This server:
  - serves it,
  - streams a live snapshot of the system to it (GET /api/stream, Server-Sent Events),
  - passes its actions on to the request service, the same REST API the CLI uses.

It talks to nothing but the request service, and stores nothing: restart it at any time.

Run locally (needs the request service on API_URL, and `npm run build` in ../web once):
    uvicorn app.main:app --port 8081      # from services/ui_service, with ../ on PYTHONPATH
"""

import asyncio
import contextlib
import json
import os
import random
from pathlib import Path
from typing import Literal

from fastapi import Body, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import model
from .api import ApiError, DeltaApi
from .hub import Hub

STATIC_DIR = Path(__file__).parent / "static"
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3000")  # as the *browser* reaches it
GRAFANA_DASHBOARD = os.environ.get("GRAFANA_DASHBOARD", "spd-overview")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
RABBITMQ_URL = os.environ.get("RABBITMQ_UI_URL", "http://localhost:15672")
API_DOCS_URL = os.environ.get("API_DOCS_URL", "http://localhost:8080/docs")
BURST_SIZE = 20
KEEPALIVE_S = 15.0


def create_app(api: DeltaApi | None = None, static_dir: Path = STATIC_DIR, poll: bool = True) -> FastAPI:
    api = api or DeltaApi()
    hub = Hub(api)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(hub.run()) if poll else None
        yield
        if task:
            task.cancel()
        await api.close()

    app = FastAPI(title="Delta Command web page", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.hub = hub

    @app.middleware("http")
    async def cache_rules(request: Request, call_next):
        """Built assets have a content hash in their name, so they can be kept forever; the page
        itself must be checked every time, or a browser could ask for assets a rebuild removed."""
        response = await call_next(request)
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif not request.url.path.startswith("/api/stream"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502 if "cannot reach" in str(exc) else 400)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # -- reading ---------------------------------------------------------------------------------

    @app.get("/api/config")
    def config() -> dict:
        return {
            "grafana_url": GRAFANA_URL.rstrip("/"), "grafana_dashboard": GRAFANA_DASHBOARD,
            "prometheus_url": PROMETHEUS_URL, "rabbitmq_url": RABBITMQ_URL, "api_docs_url": API_DOCS_URL,
            "burst_size": BURST_SIZE, "types": model.TYPES, "cities": model.CITIES,
        }

    @app.get("/api/state")
    async def state() -> dict:
        """One snapshot. The page uses /api/stream; this is for scripts and for a page whose
        stream cannot connect."""
        if not hub.watched():
            await hub.refresh()
        hub.seen()
        return {"version": hub.version, **hub.state.snapshot()}

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        async def events():
            hub.watchers += 1
            hub.poke()
            try:
                version = -1
                while not await request.is_disconnected():
                    if hub.version != version:
                        version = hub.version
                        data = json.dumps({"version": version, **hub.state.snapshot()}, separators=(",", ":"))
                        yield f"data: {data}\n\n"
                    elif not await hub.wait_for_change(version, KEEPALIVE_S):
                        yield ": keep-alive\n\n"
            finally:
                hub.watchers -= 1

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -- acting: each answers {"message": ...} for the toast, and refreshes the snapshot at once ---------

    def done(message: str, slow: bool = False, **extra) -> dict:
        hub.poke(slow)
        return {"message": message, **extra}

    @app.post("/api/requests")
    async def create_request(body: dict = Body(...)) -> dict:
        created = await api.create_request(body)
        shard = model.shard_of("request", created["id"])
        return done(f"Request {model.short_id(created['id'])} stored on shard {shard} "
                    f"(s{shard}-primary + s{shard}-replica) and queued for matching", record=created, shard=shard)

    @app.post("/api/resources")
    async def register_resource(body: dict = Body(...)) -> dict:
        created = await api.register_resource(body)
        shard = model.shard_of("resource", created["id"])
        return done(f"{created['name']} is AVAILABLE, stored on shard {shard}", slow=True, record=created, shard=shard)

    @app.post("/api/requests/{request_id}/match")
    async def match(request_id: str) -> dict:
        result = await api.match(request_id)
        return done(f"{model.short_id(request_id)}: {result['outcome']}", slow=True, result=result)

    @app.put("/api/requests/{request_id}/status")
    async def request_status(request_id: str, body: dict = Body(...)) -> dict:
        result = await api.set_request_status(request_id, body.get("status"))
        return done(f"{model.short_id(request_id)} is now {result['status'].replace('_', ' ')}", slow=True)

    @app.put("/api/resources/{resource_id}/status")
    async def resource_status(resource_id: str, body: dict = Body(...)) -> dict:
        result = await api.set_resource_status(resource_id, body.get("status"))
        return done(f"{result.get('name', model.short_id(resource_id))} is now {result['status']}", slow=True)

    @app.post("/api/nodes/{node_id}/{move}")
    async def node(node_id: str, move: Literal["pause", "resume"]) -> dict:
        await api.set_node_paused(node_id, move == "pause")
        if move == "pause":
            return done(f"{node_id} paused: it stops answering now. The coordinator marks it DOWN within "
                        "about 10 s, or at once if a request reaches it.")
        return done(f"{node_id} resumed: it re-registers and catches up from its partner")

    @app.post("/api/burst")
    async def burst() -> dict:
        async def one():
            lat, lon = random.choice(list(model.CITIES.values()))
            return await api.create_request({
                "type": random.choice(model.TYPES), "details": "stress test",
                "location": {"lat": round(lat + random.uniform(-0.1, 0.1), 4),
                             "lon": round(lon + random.uniform(-0.1, 0.1), 4)},
            })

        results = await asyncio.gather(*(one() for _ in range(BURST_SIZE)), return_exceptions=True)
        failed = sum(isinstance(r, Exception) for r in results)
        return done(f"Sent {BURST_SIZE - failed} requests" + (f", {failed} failed" if failed else ""),
                    sent=BURST_SIZE - failed, failed=failed)

    # -- the page ---------------------------------------------------------------------------------

    if (static_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="page")
    else:
        @app.get("/", response_class=HTMLResponse)
        def not_built() -> str:
            return ("<p>The page is not built yet: run <code>npm ci && npm run build</code> in "
                    "services/ui_service/web, or build the Docker image.</p>")

    return app


app = create_app()
