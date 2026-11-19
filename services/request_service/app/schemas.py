"""What a valid request body looks like. DESIGN.md sections 2 and 3.

Only the fields a client may send are here. `id`, `status`, `version` and the
timestamps are set by the system, so anything a client sends for them is ignored.
A body that breaks a rule is answered with 400 (see main.py).
"""

from pydantic import BaseModel, Field

from common.models import RequestStatus, ResourceStatus, Type


class Location(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class RequestCreate(BaseModel):
    type: Type
    location: Location
    quantity: int | None = Field(default=None, gt=0)
    details: str | None = Field(default=None, max_length=500)  # shown to people, never used for matching
    contact: str | None = None


class ResourceCreate(BaseModel):
    type: Type
    name: str = Field(min_length=1)
    location: Location
    capacity: int | None = Field(default=None, gt=0)
    availability: str | None = None
    contact: str | None = None


class RequestStatusUpdate(BaseModel):
    status: RequestStatus


class ResourceStatusUpdate(BaseModel):
    status: ResourceStatus
