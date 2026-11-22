"""In-memory fake coordinator. DESIGN.md section 8.

Same HTTP API as the real coordinator, but everything lives in one Python dict
in one process: no shards, no replicas, no heartbeats. It exists so Person B
can build the request and matching services against the real contract from
day 1. At Sync 1 we point COORDINATOR_URL at the real coordinator and nothing
on B's side changes.

Run locally:
    uvicorn coordinator.app.fake:app --app-dir services --port 8000
Swagger UI: http://localhost:8000/docs

Version rules (identical in the real coordinator):
  * The coordinator owns `version`. Whatever the client sends in the body is
    ignored and replaced.
  * New record -> version 1. Every successful save -> previous version + 1.
  * PUT with `If-Match: <n>` saves only if the stored version is exactly n,
    otherwise 409. If the key does not exist at all, 404.
"""

import copy
import threading
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Request, Response

from common.config import NUM_SHARDS
from common.sharding import CELLS_PARAM, key_in_cells, parse_cells, shard_for

app = FastAPI(title="Coordinator (FAKE, in-memory)")

_records: dict[str, dict[str, Any]] = {}

# FastAPI runs plain `def` endpoints in a thread pool, so two requests can run
# at the same moment. The lock makes "check the version, then write" one
# indivisible (atomic) step. Without it, two matchers could both see version 4
# and both succeed: exactly the double assignment we are trying to prevent.
_lock = threading.Lock()


def _parse_if_match(value: str) -> int:
    # Accept 4, "4" and W/"4" so ETag-style clients also work.
    raw = value.strip()
    if raw.startswith("W/"):
        raw = raw[2:]
    raw = raw.strip('"')
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(400, detail=f"If-Match must be an integer version, got {value!r}")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "fake"}


@app.put("/store/{key}")
def put_record(
    key: str,
    record: dict[str, Any] = Body(...),
    if_match: str | None = Header(default=None),
) -> dict[str, Any]:
    # Check and write under ONE lock hold, so no one can change the record in between.
    with _lock:
        current = _records.get(key)

        # Conditional save (compare-and-set): only if nobody saved since the caller read it.
        if if_match is not None:
            expected = _parse_if_match(if_match)
            if current is None:
                raise HTTPException(404, detail=f"{key} not found")
            if current["version"] != expected:
                raise HTTPException(
                    409,
                    detail={
                        "error": "version_conflict",
                        "key": key,
                        "expected_version": expected,
                        "current_version": current["version"],
                    },
                )

        # Deep copies everywhere: callers must never hold a reference into our store.
        saved = copy.deepcopy(record)
        # The coordinator, not the client, decides the version.
        saved["version"] = 1 if current is None else current["version"] + 1
        _records[key] = saved
        return copy.deepcopy(saved)


@app.get("/store/{key}")
def get_record(key: str) -> dict[str, Any]:
    with _lock:
        record = _records.get(key)
        if record is None:
            raise HTTPException(404, detail=f"{key} not found")
        return copy.deepcopy(record)


@app.delete("/store/{key}", status_code=204)
def delete_record(key: str) -> Response:
    with _lock:
        if _records.pop(key, None) is None:
            raise HTTPException(404, detail=f"{key} not found")
    return Response(status_code=204)


@app.get("/store")
def query_records(request: Request, prefix: str = "") -> list[dict[str, Any]]:
    """Every record whose key starts with `prefix`. Any other query parameter
    is an exact-match filter on a top-level field, e.g. ?type=water&status=AVAILABLE.

    Results are sorted by key so the output is stable. Callers must not rely on
    any particular order (the real coordinator merges 3 shards).
    """
    # The real coordinator will send this same query to all 3 shards and merge
    # the answers (scatter-gather), because records are spread by key, not by type.
    # With ?cells= it asks only the shards owning those cells; here (one store) that
    # just means keeping the records in those cells (common/sharding.key_in_cells).
    filters = {k: v for k, v in request.query_params.items() if k not in ("prefix", CELLS_PARAM)}
    try:
        cells = parse_cells(request.query_params.get(CELLS_PARAM))
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    with _lock:
        matches = [
            copy.deepcopy(rec)
            for key, rec in sorted(_records.items())
            if key.startswith(prefix)
            and key_in_cells(key, cells)
            and all(str(rec.get(field)) == value for field, value in filters.items())
        ]
    return matches


@app.get("/internal/status")
def status() -> dict[str, Any]:
    """Same shape as the real coordinator's status, filled with made-up nodes.

    keys_per_shard uses the real shard rule, so the distribution demo already
    works against the fake.
    """
    with _lock:
        keys = list(_records)
    keys_per_shard = {str(s): 0 for s in range(NUM_SHARDS)}
    for key in keys:
        keys_per_shard[str(shard_for(key, NUM_SHARDS))] += 1

    nodes = [
        {
            "node_id": f"fake-s{shard}-{role}",
            "shard": shard,
            "role": role,
            "state": "UP",
            "last_heartbeat": None,
        }
        for shard in range(NUM_SHARDS)
        for role in ("primary", "replica")
    ]
    return {"mode": "fake", "nodes": nodes, "keys_per_shard": keys_per_shard, "total_keys": len(keys)}


@app.post("/internal/nodes/register")
def register_node(node: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Accepted and ignored: lets a real storage node run against the fake
    without logging errors. The real coordinator keeps a registry."""
    return {"ok": True, "mode": "fake"}


@app.post("/internal/nodes/heartbeat")
def node_heartbeat(beat: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return {"ok": True, "mode": "fake"}


def reset() -> None:
    """Wipe all data. For tests only."""
    with _lock:
        _records.clear()
