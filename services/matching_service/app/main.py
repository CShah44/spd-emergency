"""The matching service: the matchmaker. DESIGN.md sections 4, 5 and 7.

Two ways in, one matching function behind both:
  * RabbitMQ: a background consumer matches every new request automatically
    (a sweeper also picks up any request that was saved but never queued)
  * POST /match/{id}: match one request right now (the request service forwards
    the client's POST /match/{id} here)

Run locally (needs a coordinator, and RabbitMQ for the consumer):
    uvicorn matching_service.app.main:app --app-dir services --port 8001
Swagger UI: http://localhost:8001/docs
"""

import contextlib
import logging
import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common import metrics
from common.storage_client import NotFound, StorageClient, StorageError, StorageUnavailable, VersionConflict

from .consumer import Consumer
from .matcher import match_request
from .sweeper import Sweeper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def create_app(store: StorageClient | None = None, consume: bool = True) -> FastAPI:
    """`store` lets tests inject a StorageClient wired to a fake coordinator;
    `consume=False` skips the RabbitMQ thread (tests, or HTTP-only use)."""
    store = store or StorageClient()
    consumer = Consumer(store)
    sweeper = Sweeper(store)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        threads = []
        if consume:
            threads = [
                threading.Thread(target=consumer.run, name="consumer", daemon=True),
                threading.Thread(target=sweeper.run, name="sweeper", daemon=True),
            ]
            for thread in threads:
                thread.start()
        yield
        consumer.stop()
        sweeper.stop()
        for thread in threads:
            thread.join(timeout=5)
        store.close()

    app = FastAPI(
        title="SPD Emergency: Matching Service",
        description="Pairs each emergency request with the nearest free resource of the right type.",
        lifespan=lifespan,
    )

    metrics.instrument(app, "matching-service")

    # Storage errors become HTTP answers in one place.
    def _error(status_code: int):
        async def handler(_: Request, exc: StorageError):
            return JSONResponse({"detail": str(exc)}, status_code=status_code)
        return handler

    app.add_exception_handler(NotFound, _error(404))
    app.add_exception_handler(VersionConflict, _error(409))  # changed while matching: try again
    app.add_exception_handler(StorageUnavailable, _error(503))
    app.add_exception_handler(StorageError, _error(502))

    @app.post("/match/{request_id}")
    def match(request_id: str) -> dict[str, Any]:
        """Match one request now. `outcome` is "matched", "unmatched", or
        "skipped" (the request was not waiting for a match)."""
        result = match_request(store, request_id)
        return {"outcome": result.outcome, "request": result.request, "resource": result.resource}

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "consumer_connected": consumer.connected}

    return app


app = create_app()
