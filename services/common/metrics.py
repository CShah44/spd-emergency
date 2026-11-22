"""Metrics every service exposes the same way: GET /metrics in Prometheus text format.

Every metric the system has is defined HERE, with its name, type and labels, so the
services (both sides) and the Grafana dashboard cannot disagree about a name. The
list is the "Agreed metric names" table in docs/UPGRADE_PLAN.md. All names start
with `spd_`. A service imports the ones it updates and calls instrument() once:

    from common import metrics

    app = FastAPI(...)
    metrics.instrument(app, "request-service")      # adds GET /metrics and the HTTP metrics
    metrics.REQUESTS_CREATED.labels(type="water").inc()

Every service defines every metric (a metric it never touches simply stays at 0 or
empty), which keeps this module the single place to look.
"""

import time

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# -- HTTP, every service (filled in by instrument()) --------------------------------------------

HTTP_REQUESTS = Counter("spd_http_requests_total", "HTTP calls handled", ["service", "route", "status"])
HTTP_SECONDS = Histogram(
    "spd_http_request_seconds", "Time to answer an HTTP call", ["service", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

# -- storage side [A] -----------------------------------------------------------------------------

NODE_STATE = Gauge("spd_node_state", "1 for the state a storage node is in, 0 for the others", ["node", "shard", "state"])
SHARD_AVAILABLE = Gauge("spd_shard_available", "1 if the shard has a live node, else 0", ["shard"])
REPLICATION_FAILURES = Counter("spd_replication_failures_total", "Writes the replica did not confirm", ["shard"])
RECOVERY_SECONDS = Histogram(
    "spd_recovery_seconds", "How long a returning node took to catch up", ["node"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
)
RECOVERY_ENTRIES = Counter("spd_recovery_entries_copied_total", "Records copied to a returning node", ["node"])
STORAGE_KEYS = Gauge("spd_storage_keys", "Records held by a storage node", ["node"])
COORDINATOR_IS_LEADER = Gauge("spd_coordinator_is_leader", "1 if this coordinator pod is the leader", ["pod"])
COORDINATOR_EPOCH = Gauge("spd_coordinator_epoch", "The leader term this coordinator pod last saw", ["pod"])
SHARDS_PER_QUERY = Histogram("spd_shards_per_query", "Shards asked to answer one query", buckets=(1, 2, 3))

# -- app side [B] ------------------------------------------------------------------------------------

REQUESTS_CREATED = Counter("spd_requests_created_total", "Emergency requests accepted", ["type"])
PUBLISH_FAILURES = Counter("spd_publish_failures_total", "Requests saved but not queued (RabbitMQ unreachable)")
MATCH_OUTCOMES = Counter("spd_match_outcomes_total", "Results of match_request()", ["outcome"])
MATCH_SECONDS = Histogram(
    "spd_match_seconds", "Time spent inside match_request()",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
MATCH_E2E_SECONDS = Histogram(
    "spd_match_e2e_seconds", "From the request being created to it being MATCHED or UNMATCHED",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)
DEAD_LETTERED = Counter("spd_dead_lettered_total", "Queue messages sent to the dead-letter queue")
SWEEPER_RESCUED = Counter("spd_sweeper_rescued_total", "Requests the sweeper matched because they were never queued")

# -- exposing them ---------------------------------------------------------------------------------------

METRICS_PATH = "/metrics"


class _HttpMetrics:
    """Counts and times every HTTP call. A plain ASGI middleware, so it adds no thread hop."""

    def __init__(self, app, service: str):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = {"code": 500}  # stays 500 if the app raises before answering

        async def remember_status(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, remember_status)
        finally:
            # The route TEMPLATE ("/requests/{request_id}"), never the real path: one label
            # value per record id would make the number of time series grow without limit.
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            if route != METRICS_PATH:
                HTTP_REQUESTS.labels(self.service, route, str(status["code"])).inc()
                HTTP_SECONDS.labels(self.service, route).observe(time.perf_counter() - started)


def instrument(app: FastAPI, service: str) -> None:
    """Add GET /metrics and the HTTP metrics to a FastAPI app. `service` is the label value."""

    @app.get(METRICS_PATH, include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.add_middleware(_HttpMetrics, service=service)
