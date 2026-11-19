"""What the request service does with a client's call, separate from the HTTP
plumbing in main.py. DESIGN.md sections 1-3 and 5.

Rules enforced here:
  * valid status moves only (models.py), otherwise 409
  * the client may not set a status that belongs to the matching service
  * when a request ends (FULFILLED or CANCELLED) its resource goes back to AVAILABLE
"""

import logging
from typing import Any

from fastapi import HTTPException

from common.models import (
    RequestStatus, ResourceStatus, can_move_request, can_move_resource, new_request_id, new_resource_id, utc_now_iso,
)
from common.sharding import RESOURCE_PREFIX, request_key, resource_key
from common.storage_client import StorageClient, StorageError, VersionConflict

from .publisher import Publisher, PublishError
from .schemas import RequestCreate, ResourceCreate

log = logging.getLogger("service")

# The matching service owns these: a client setting them would leave a request
# MATCHED with no resource, or a resource ASSIGNED to nobody.
MATCHER_ONLY_REQUEST = {RequestStatus.MATCHED, RequestStatus.UNMATCHED}
MATCHER_ONLY_RESOURCE = {ResourceStatus.ASSIGNED}

SAVE_ATTEMPTS = 3  # re-reads allowed when someone else saved the same record first


class Service:
    def __init__(self, store: StorageClient, publisher: Publisher):
        self.store = store
        self.publisher = publisher

    # -- requests ----------------------------------------------------------------------------

    def create_request(self, body: RequestCreate) -> dict[str, Any]:
        now = utc_now_iso()
        record = {
            "id": new_request_id(),
            **body.model_dump(mode="json"),
            "status": RequestStatus.PENDING,
            "assigned_resource_id": None,
            "created_at": now,
            "updated_at": now,
        }
        saved = self.store.put(request_key(record["id"]), record)
        self._enqueue(saved["id"])
        return saved

    def get_request(self, request_id: str) -> dict[str, Any]:
        return self.store.get(request_key(request_id))

    def update_request_status(self, request_id: str, new: RequestStatus) -> dict[str, Any]:
        if new in MATCHER_ONLY_REQUEST:
            raise HTTPException(409, detail=f"{new} is set by the matching service, not by clients")

        def check(request: dict[str, Any]) -> None:
            if not can_move_request(request["status"], new):
                raise HTTPException(409, detail=f"cannot move a request from {request['status']} to {new}")

        saved = self._change_status(request_key(request_id), new, check)
        if new in (RequestStatus.FULFILLED, RequestStatus.CANCELLED) and saved.get("assigned_resource_id"):
            self._release_resource(saved["assigned_resource_id"], request_id)
        if new == RequestStatus.PENDING:  # UNMATCHED -> PENDING is a retry: queue it again
            self._enqueue(request_id)
        return saved

    # -- resources -----------------------------------------------------------------------------

    def create_resource(self, body: ResourceCreate) -> dict[str, Any]:
        now = utc_now_iso()
        record = {
            "id": new_resource_id(),
            **body.model_dump(mode="json"),
            "status": ResourceStatus.AVAILABLE,
            "assigned_request_id": None,
            "created_at": now,
            "updated_at": now,
        }
        return self.store.put(resource_key(record["id"]), record)

    def list_resources(self, type: str | None, status: str | None) -> list[dict[str, Any]]:
        filters = {k: v for k, v in (("type", type), ("status", status)) if v is not None}
        return sorted(self.store.query(RESOURCE_PREFIX, **filters), key=lambda r: r["created_at"])

    def update_resource_status(self, resource_id: str, new: ResourceStatus) -> dict[str, Any]:
        if new in MATCHER_ONLY_RESOURCE:
            raise HTTPException(409, detail=f"{new} is set by the matching service, not by clients")

        def check(resource: dict[str, Any]) -> None:
            if resource["status"] == ResourceStatus.ASSIGNED:
                raise HTTPException(409, detail="resource is assigned: it is freed when its request ends")
            if not can_move_resource(resource["status"], new):
                raise HTTPException(409, detail=f"cannot move a resource from {resource['status']} to {new}")

        return self._change_status(resource_key(resource_id), new, check)

    # -- internals ---------------------------------------------------------------------------------

    def _change_status(self, key: str, new_status: str, check) -> dict[str, Any]:
        """Read, validate with `check` (raises 409), save only if nobody changed
        the record meanwhile (DESIGN.md section 5). If they did, read it again and re-check."""
        for _ in range(SAVE_ATTEMPTS):
            record = self.store.get(key)  # NotFound -> 404 (main.py)
            check(record)
            updated = {**record, "status": new_status, "updated_at": utc_now_iso()}
            try:
                return self.store.put_if_version(key, updated, record["version"])
            except VersionConflict:
                continue
        raise HTTPException(409, detail="the record keeps changing, try again")

    def _release_resource(self, resource_id: str, request_id: str) -> None:
        """ASSIGNED -> AVAILABLE, but only if it is still promised to THIS request.

        Best effort, one try: the request has already been saved, so a failure
        here is logged rather than reported (the client's call did succeed).
        """
        try:
            resource = self.store.get_or_none(resource_key(resource_id))
            if resource is None or resource["assigned_request_id"] != request_id:
                return
            freed = {
                **resource, "status": ResourceStatus.AVAILABLE, "assigned_request_id": None,
                "updated_at": utc_now_iso(),
            }
            self.store.put_if_version(resource_key(resource_id), freed, resource["version"])
        except StorageError as exc:
            log.error("could not free %s after %s ended (it stays ASSIGNED): %s", resource_id, request_id, exc)

    def _enqueue(self, request_id: str) -> None:
        """Tell the matching service about a request. The request is already saved,
        so a RabbitMQ outage must not fail the client's call: the request stays
        PENDING and can still be matched with POST /match/{id}."""
        try:
            self.publisher.publish(request_id)
        except PublishError as exc:
            log.error("%s is saved but NOT queued: %s", request_id, exc)
