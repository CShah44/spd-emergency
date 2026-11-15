import re

import pytest

from common.models import (
    TYPES,
    RequestStatus,
    ResourceStatus,
    can_move_request,
    can_move_resource,
    new_request_id,
    new_resource_id,
    utc_now_iso,
)


def test_fixed_types():
    assert TYPES == {"water", "food", "medicine", "transport", "shelter"}


def test_id_formats():
    assert re.fullmatch(r"req-[0-9a-f]{16}", new_request_id())
    assert re.fullmatch(r"res-[0-9a-f]{16}", new_resource_id())
    assert new_request_id() != new_request_id()


def test_timestamp_format():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", utc_now_iso())


@pytest.mark.parametrize(
    "current,new",
    [
        ("PENDING", "MATCHED"),
        ("PENDING", "UNMATCHED"),
        ("PENDING", "CANCELLED"),
        ("MATCHED", "IN_PROGRESS"),
        ("MATCHED", "CANCELLED"),
        ("IN_PROGRESS", "FULFILLED"),
        ("IN_PROGRESS", "CANCELLED"),
        ("UNMATCHED", "PENDING"),
        ("UNMATCHED", "CANCELLED"),
    ],
)
def test_allowed_request_moves(current, new):
    assert can_move_request(current, new)


@pytest.mark.parametrize(
    "current,new",
    [
        ("PENDING", "FULFILLED"),
        ("PENDING", "IN_PROGRESS"),
        ("MATCHED", "PENDING"),
        ("FULFILLED", "CANCELLED"),
        ("CANCELLED", "PENDING"),
        ("IN_PROGRESS", "MATCHED"),
        ("UNMATCHED", "MATCHED"),  # must go back to PENDING and be matched again
    ],
)
def test_forbidden_request_moves(current, new):
    assert not can_move_request(current, new)


def test_final_statuses_have_no_exits():
    for s in RequestStatus:
        assert not can_move_request("FULFILLED", s)
        assert not can_move_request("CANCELLED", s)


def test_resource_moves():
    assert can_move_resource("AVAILABLE", "ASSIGNED")
    assert can_move_resource("ASSIGNED", "AVAILABLE")
    assert can_move_resource("AVAILABLE", "OFFLINE")
    assert can_move_resource("OFFLINE", "AVAILABLE")
    assert not can_move_resource("OFFLINE", "ASSIGNED")
    assert not can_move_resource("ASSIGNED", "ASSIGNED")
    assert not can_move_resource("ASSIGNED", "OFFLINE")  # must be released first


def test_unknown_status_raises():
    with pytest.raises(ValueError):
        can_move_request("PENDING", "DONE")


def test_enums_compare_to_strings():
    assert RequestStatus.PENDING == "PENDING"
    assert ResourceStatus.AVAILABLE == "AVAILABLE"
