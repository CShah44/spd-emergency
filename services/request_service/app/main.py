"""The request service: the front desk. Client REST API from the spec (section 5).

Clients talk only to this service. It stores records through the coordinator,
queues new requests for the matching service, and forwards POST /match/{id}.
It also offers the few extra calls the Delta Command web page (services/ui_service)
needs: GET /requests, /cluster and the node pause/resume demo controls.

Run locally (needs a coordinator, and RabbitMQ to queue requests):
    uvicorn request_service.app.main:app --app-dir services --port 8080
Swagger UI: http://localhost:8080/docs
"""

import contextlib
import logging
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from common import config
from common.models import RequestStatus, ResourceStatus, Type
from common.storage_client import NotFound, StorageClient, StorageError, StorageUnavailable, VersionConflict

from .cluster import ClusterView
from .publisher import Publisher
from .schemas import RequestCreate, RequestStatusUpdate, ResourceCreate, ResourceStatusUpdate
from .service import Service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def create_app(
    store: StorageClient | None = None,
    publisher: Publisher | None = None,
    matching_client: httpx.Client | None = None,
    admin_client: httpx.Client | None = None,
) -> FastAPI:
    """Each argument lets tests swap in a stand-in (a StorageClient on the fake
    coordinator, a recording publisher, the matching app in-process, a client for
    pausing storage nodes)."""
    store = store or StorageClient()
    publisher = publisher or Publisher()
    matching = matching_client or httpx.Client(
        base_url=config.MATCHING_SERVICE_URL, timeout=config.MATCH_TIMEOUT_S
    )
    service = Service(store, publisher)
    cluster = ClusterView(store, publisher, admin_client)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        publisher.close()
        matching.close()
        cluster.close()
        store.close()

    app = FastAPI(
        title="SPD Emergency: Delta Command (Request Service)",
        description=(
            "Emergency! Report a need, register a helper, and Delta Command dispatches the "
            "nearest available help."
        ),
        lifespan=lifespan,
    )

    # -- errors become HTTP answers in one place --------------------------------------------------

    @app.exception_handler(RequestValidationError)
    async def _bad_request(_: Request, exc: RequestValidationError):
        # The spec wants 400 for an invalid request (FastAPI's default is 422).
        detail = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"detail": detail}, status_code=400)

    def _storage_error(status_code: int):
        async def handler(_: Request, exc: StorageError):
            return JSONResponse({"detail": str(exc)}, status_code=status_code)
        return handler

    app.add_exception_handler(NotFound, _storage_error(404))
    app.add_exception_handler(VersionConflict, _storage_error(409))
    app.add_exception_handler(StorageUnavailable, _storage_error(503))
    app.add_exception_handler(StorageError, _storage_error(502))

    # -- requests --------------------------------------------------------------------------------

    @app.post("/requests", status_code=201)
    def create_request(body: RequestCreate) -> dict[str, Any]:
        """Report a need. It is saved as PENDING and queued for matching."""
        return service.create_request(body)

    @app.get("/requests")
    def list_requests(
        type: Type | None = None,
        status: RequestStatus | None = None,
        limit: int = Query(100, ge=1, le=500),
        active_first: bool = False,
    ) -> list[dict[str, Any]]:
        """The newest requests first (at most `limit`). With active_first=true, requests that can
        still change come first and finished ones follow. Used by the web page's live board."""
        return service.list_requests(type, status, limit, active_first)

    @app.get("/requests/summary")
    def request_summary() -> dict[str, int]:
        """How many requests are in each status, and the total. Used by the web page."""
        return service.request_counts()

    @app.get("/requests/{request_id}")
    def get_request(request_id: str) -> dict[str, Any]:
        return service.get_request(request_id)

    @app.put("/requests/{request_id}/status")
    def update_request_status(request_id: str, body: RequestStatusUpdate) -> dict[str, Any]:
        """Clients set IN_PROGRESS, FULFILLED or CANCELLED, or PENDING to retry an
        UNMATCHED request. MATCHED and UNMATCHED are set by the matching service."""
        return service.update_request_status(request_id, body.status)

    # -- resources -------------------------------------------------------------------------------

    @app.post("/resources", status_code=201)
    def create_resource(body: ResourceCreate) -> dict[str, Any]:
        """Register something or someone that can help. Starts AVAILABLE."""
        return service.create_resource(body)

    @app.get("/resources")
    def list_resources(type: Type | None = None, status: ResourceStatus | None = None) -> list[dict[str, Any]]:
        return service.list_resources(type, status)

    @app.put("/resources/{resource_id}/status")
    def update_resource_status(resource_id: str, body: ResourceStatusUpdate) -> dict[str, Any]:
        """Clients switch a resource between AVAILABLE and OFFLINE. ASSIGNED is set
        by the matching service and cleared when the request ends."""
        return service.update_resource_status(resource_id, body.status)

    # -- matching --------------------------------------------------------------------------------

    @app.post("/match/{request_id}")
    def match(request_id: str) -> JSONResponse:
        """Match one request right now, without waiting for the queue. Forwarded to
        the matching service; its answer (200, 404, 409...) is passed back unchanged."""
        try:
            answer = matching.post(f"/match/{request_id}")
        except httpx.TransportError as exc:
            raise HTTPException(503, detail=f"matching service unreachable: {exc!r}")
        try:
            body = answer.json()
        except ValueError:  # e.g. an HTML error page from a proxy in between
            body = {"detail": answer.text[:200]}
        return JSONResponse(body, status_code=answer.status_code)

    # -- what the web page needs ----------------------------------------------------------------------

    @app.get("/cluster")
    def cluster_overview() -> dict[str, Any]:
        """Storage nodes, shard leaders and queue depth, for the page's cluster panel."""
        return cluster.overview()

    @app.post("/cluster/nodes/{node_id}/pause")
    def pause_node(node_id: str) -> dict[str, Any]:
        """Demo control: simulate a storage-node failure. Needs DEMO_CONTROLS=true."""
        return cluster.set_node_paused(node_id, True)

    @app.post("/cluster/nodes/{node_id}/resume")
    def resume_node(node_id: str) -> dict[str, Any]:
        """Demo control: bring a paused node back; it catches up before serving again."""
        return cluster.set_node_paused(node_id, False)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
