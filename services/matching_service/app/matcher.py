"""The matching engine. DESIGN.md sections 4 and 5.

match_request() is the one function behind both ways of matching: the RabbitMQ
consumer calls it for every message, and POST /match/{id} calls it on demand.

It talks to storage only through StorageClient, so it runs the same against the
fake coordinator, the real one, or a test double.

Order of writes, and why:
  1. claim the resource   (AVAILABLE -> ASSIGNED, only if its version is unchanged)
  2. update the request   (PENDING -> MATCHED, only if its version is unchanged)
If step 2 fails we give the resource back, so nothing is left promised to a
request that never got it. If the process dies between the two steps, the
resource stays ASSIGNED to this request: the next attempt finds it again
(_already_assigned) and finishes the job instead of grabbing a second one.
"""

import calendar
import logging
import time
from dataclasses import dataclass
from typing import Any

from common import config, metrics
from common.models import RequestStatus, ResourceStatus, can_move_request, utc_now_iso
from common.sharding import RESOURCE_PREFIX, request_key, resource_key
from common.storage_client import NotFound, StorageClient, StorageError, VersionConflict

from .geo import haversine_km

log = logging.getLogger("matcher")


@dataclass
class MatchResult:
    outcome: str  # "matched", "unmatched", or "skipped" (the request was not waiting for a match)
    request: dict[str, Any]
    resource: dict[str, Any] | None = None


def rank_candidates(
    origin: dict[str, float], resources: list[dict[str, Any]], max_km: float
) -> list[dict[str, Any]]:
    """Section 4, steps 3-4: drop everything further than max_km, then sort
    nearest first. A tie goes to the resource registered earliest."""
    within = [(haversine_km(origin, r["location"]), r) for r in resources]
    within = [(km, r) for km, r in within if km <= max_km]
    within.sort(key=lambda pair: (pair[0], pair[1].get("created_at", ""), pair[1]["id"]))
    return [r for _, r in within]


def match_request(store: StorageClient, request_id: str, max_km: float | None = None) -> MatchResult:
    """Find and claim the nearest free resource of the right type.

    Raises NotFound (no such request), VersionConflict (the request changed
    while we worked: safe to try again) or StorageUnavailable.

    Every call is counted in spd_match_outcomes_total (matched, unmatched, skipped,
    conflict, not_found, or error for anything else) and timed in spd_match_seconds.
    """
    started = time.perf_counter()
    outcome = "error"
    try:
        result = _match(store, request_id, max_km)
        outcome = result.outcome
        return result
    except VersionConflict:
        outcome = "conflict"
        raise
    except NotFound:
        outcome = "not_found"
        raise
    finally:
        metrics.MATCH_OUTCOMES.labels(outcome=outcome).inc()
        metrics.MATCH_SECONDS.observe(time.perf_counter() - started)


def _match(store: StorageClient, request_id: str, max_km: float | None) -> MatchResult:
    max_km = config.MAX_MATCH_KM if max_km is None else max_km
    request = store.get(request_key(request_id))

    # Only a waiting request is matched. Anything else (already matched,
    # cancelled...) means a duplicate message or a late call: do nothing.
    if request["status"] not in (RequestStatus.PENDING, RequestStatus.UNMATCHED):
        return MatchResult("skipped", request)
    first_try = request["status"] == RequestStatus.PENDING  # a retry's age says nothing about matching speed
    if request["status"] == RequestStatus.UNMATCHED:  # a retry: the state machine says go via PENDING
        request = _save_request(store, request, RequestStatus.PENDING)

    resource = _already_assigned(store, request_id) or _claim_nearest(store, request, max_km)
    if resource is None:
        saved = _save_request(store, request, RequestStatus.UNMATCHED)
        _observe_end_to_end(request, first_try)
        return MatchResult("unmatched", saved)

    try:
        saved = _save_request(store, request, RequestStatus.MATCHED, assigned_resource_id=resource["id"])
    except VersionConflict:
        # The request changed under us (cancelled? matched by a duplicate?). Give the
        # resource back, unless another matcher already saved the request with it.
        _release_unless_used(store, request_id, resource)
        raise
    _observe_end_to_end(request, first_try)
    return MatchResult("matched", saved, resource)


def _observe_end_to_end(request: dict[str, Any], first_try: bool) -> None:
    """spd_match_e2e_seconds: how long the request waited from creation to its answer."""
    if not first_try:
        return
    created = request.get("created_ts")  # set by the request service, with sub-second precision
    if not isinstance(created, (int, float)):
        try:  # records from before created_ts: whole seconds only
            created = calendar.timegm(time.strptime(request["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
        except (KeyError, TypeError, ValueError):
            return
    metrics.MATCH_E2E_SECONDS.observe(max(0.0, time.time() - created))


def _claim_nearest(store: StorageClient, request: dict[str, Any], max_km: float) -> dict[str, Any] | None:
    """Section 4 steps 1-5 and section 5: try the nearest candidate first and
    claim it only if its version is still the one we read."""
    candidates = store.query(RESOURCE_PREFIX, type=request["type"], status=ResourceStatus.AVAILABLE)
    for resource in rank_candidates(request["location"], candidates, max_km):
        claimed = {
            **resource,
            "status": ResourceStatus.ASSIGNED,
            "assigned_request_id": request["id"],
            "updated_at": utc_now_iso(),
        }
        try:
            return store.put_if_version(resource_key(resource["id"]), claimed, resource["version"])
        except (VersionConflict, NotFound):
            log.info("%s was taken first, trying the next candidate", resource["id"])
    return None


def _already_assigned(store: StorageClient, request_id: str) -> dict[str, Any] | None:
    """A resource a previous attempt claimed for this request before dying."""
    found = store.query(RESOURCE_PREFIX, status=ResourceStatus.ASSIGNED, assigned_request_id=request_id)
    return found[0] if found else None


def _release_unless_used(store: StorageClient, request_id: str, resource: dict[str, Any]) -> None:
    """Called after losing the race to save the request. Two matchers on the same
    request can end up holding the SAME resource (one claimed it, the other found
    it via _already_assigned), so the loser must check what the winner saved:
    releasing a resource the request now points at would make it look free while
    it is still promised, and it could then be given to a second request."""
    try:
        current = store.get_or_none(request_key(request_id))
    except StorageError as exc:
        # Can't tell: leaving it ASSIGNED is safe; freeing it could double-assign it.
        log.warning("could not re-read %s, leaving %s assigned: %s", request_id, resource["id"], exc)
        return
    if current is not None and current.get("assigned_resource_id") == resource["id"]:
        return  # the winner used this resource: it is correctly assigned
    _release(store, resource)


def _release(store: StorageClient, resource: dict[str, Any]) -> None:
    released = {
        **resource,
        "status": ResourceStatus.AVAILABLE,
        "assigned_request_id": None,
        "updated_at": utc_now_iso(),
    }
    try:
        store.put_if_version(resource_key(resource["id"]), released, resource["version"])
    except (VersionConflict, NotFound):
        log.warning("could not release %s: it changed or vanished", resource["id"])


def _save_request(
    store: StorageClient, request: dict[str, Any], status: RequestStatus, **fields: Any
) -> dict[str, Any]:
    if not can_move_request(request["status"], status):  # a bug in this file, not a client error
        raise ValueError(f"illegal request move {request['status']} -> {status}")
    updated = {**request, **fields, "status": status, "updated_at": utc_now_iso()}
    return store.put_if_version(request_key(request["id"]), updated, request["version"])
