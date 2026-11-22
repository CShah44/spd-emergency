"""Fixed lists, statuses and allowed status moves. DESIGN.md sections 1-3.

Stdlib only, so every service (including storage) can import it without extra
dependencies. Request/body validation (pydantic) lives in each service.
"""

import secrets
from datetime import datetime, timezone
from enum import Enum


class StrEnum(str, Enum):
    """An Enum whose members compare equal to their string values."""

    def __str__(self) -> str:
        return self.value


class Type(StrEnum):
    WATER = "water"
    FOOD = "food"
    MEDICINE = "medicine"
    TRANSPORT = "transport"
    SHELTER = "shelter"


class RequestStatus(StrEnum):
    PENDING = "PENDING"
    MATCHED = "MATCHED"
    IN_PROGRESS = "IN_PROGRESS"
    FULFILLED = "FULFILLED"
    UNMATCHED = "UNMATCHED"
    CANCELLED = "CANCELLED"


class ResourceStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    ASSIGNED = "ASSIGNED"
    OFFLINE = "OFFLINE"


# A state machine: for each status, the statuses it may move to next.
# Anything not listed is rejected with 409.
REQUEST_TRANSITIONS: dict[RequestStatus, set[RequestStatus]] = {
    RequestStatus.PENDING: {RequestStatus.MATCHED, RequestStatus.UNMATCHED, RequestStatus.CANCELLED},
    RequestStatus.MATCHED: {RequestStatus.IN_PROGRESS, RequestStatus.CANCELLED},
    RequestStatus.IN_PROGRESS: {RequestStatus.FULFILLED, RequestStatus.CANCELLED},
    RequestStatus.UNMATCHED: {RequestStatus.PENDING, RequestStatus.CANCELLED},  # retry or give up
    RequestStatus.FULFILLED: set(),  # final
    RequestStatus.CANCELLED: set(),  # final
}

RESOURCE_TRANSITIONS: dict[ResourceStatus, set[ResourceStatus]] = {
    ResourceStatus.AVAILABLE: {ResourceStatus.ASSIGNED, ResourceStatus.OFFLINE},
    # Freed when its request is FULFILLED or CANCELLED. No direct ASSIGNED -> OFFLINE:
    # release it first, so the request it was promised to isn't silently stranded.
    ResourceStatus.ASSIGNED: {ResourceStatus.AVAILABLE},
    ResourceStatus.OFFLINE: {ResourceStatus.AVAILABLE},
}

TYPES: frozenset[str] = frozenset(t.value for t in Type)


def can_move_request(current: str, new: str) -> bool:
    return RequestStatus(new) in REQUEST_TRANSITIONS[RequestStatus(current)]


def can_move_resource(current: str, new: str) -> bool:
    return ResourceStatus(new) in RESOURCE_TRANSITIONS[ResourceStatus(current)]


def new_request_id(cell: str | None = None) -> str:
    """'req-' + 16 random hex characters, or 'req-<cell>-' + 16 hex when the record belongs to a
    geohash cell (docs/GEO_SHARDING.md): the cell is how its shard is found."""
    return "req-" + (f"{cell}-" if cell else "") + secrets.token_hex(8)


def new_resource_id(cell: str | None = None) -> str:
    """'res-' + 16 random hex characters, or 'res-<cell>-' + 16 hex (see new_request_id)."""
    return "res-" + (f"{cell}-" if cell else "") + secrets.token_hex(8)


def utc_now_iso() -> str:
    """Current UTC time as ISO 8601 with a trailing Z, e.g. 2026-11-09T10:15:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
