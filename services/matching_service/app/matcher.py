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

import logging
from dataclasses import dataclass
from typing import Any

from common import config
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
    """
    max_km = config.MAX_MATCH_KM if max_km is None else max_km
    request = store.get(request_key(request_id))

    # Only a waiting request is matched. Anything else (already matched,
    # cancelled...) means a duplicate message or a late call: do nothing.
    if request["status"] not in (RequestStatus.PENDING, RequestStatus.UNMATCHED):
        return MatchResult("skipped", request)
    if request["status"] == RequestStatus.UNMATCHED:  # a retry: the state machine says go via PENDING
        request = _save_request(store, request, RequestStatus.PENDING)

    resource = _already_assigned(store, request_id) or _claim_nearest(store, request, max_km)
    if resource is None:
        saved = _save_request(store, request, RequestStatus.UNMATCHED)
        return MatchResult("unmatched", saved)

    try:
        saved = _save_request(store, request, RequestStatus.MATCHED, assigned_resource_id=resource["id"])
    except VersionConflict:
        # The request changed under us (cancelled? matched by a duplicate?). Give the
        # resource back, unless another matcher already saved the request with it.
        _release_unless_used(store, request_id, resource)
        raise
    return MatchResult("matched", saved, resource)


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
