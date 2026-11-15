"""The fake coordinator, driven through StorageClient: this checks both halves
of the section 8 contract at once, without any network."""

import threading

import pytest
from fastapi.testclient import TestClient

from common.sharding import shard_for
from common.storage_client import NotFound, StorageClient, VersionConflict
from coordinator.app import fake


@pytest.fixture
def store():
    fake.reset()
    with TestClient(fake.app) as http:
        yield StorageClient(client=http)


def _resource(rid, type_="water", status="AVAILABLE"):
    return {"id": rid, "type": type_, "status": status, "name": rid, "version": 999}


def test_put_then_get(store):
    saved = store.put("resource:res-1", _resource("res-1"))
    assert saved["version"] == 1  # client-sent version is ignored
    assert store.get("resource:res-1") == saved


def test_overwrite_bumps_version(store):
    store.put("resource:res-1", _resource("res-1"))
    assert store.put("resource:res-1", _resource("res-1"))["version"] == 2


def test_get_missing_is_404(store):
    with pytest.raises(NotFound):
        store.get("request:nope")
    assert store.get_or_none("request:nope") is None


def test_delete(store):
    store.put("resource:res-1", _resource("res-1"))
    store.delete("resource:res-1")
    with pytest.raises(NotFound):
        store.get("resource:res-1")
    with pytest.raises(NotFound):
        store.delete("resource:res-1")


def test_compare_and_set(store):
    v1 = store.put("resource:res-1", _resource("res-1"))
    v2 = store.put_if_version("resource:res-1", _resource("res-1", status="ASSIGNED"), version=1)
    assert v2["version"] == 2 and v2["status"] == "ASSIGNED"
    # A second writer still holding version 1 must lose.
    with pytest.raises(VersionConflict):
        store.put_if_version("resource:res-1", _resource("res-1"), version=v1["version"])
    assert store.get("resource:res-1")["status"] == "ASSIGNED"


def test_compare_and_set_on_missing_key_is_404(store):
    with pytest.raises(NotFound):
        store.put_if_version("resource:ghost", _resource("ghost"), version=1)


def test_only_one_of_many_concurrent_claims_wins(store):
    store.put("resource:res-1", _resource("res-1"))
    wins, losses = [], []

    def claim(i):
        try:
            store.put_if_version("resource:res-1", {**_resource("res-1", status="ASSIGNED"), "by": i}, 1)
            wins.append(i)
        except VersionConflict:
            losses.append(i)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1 and len(losses) == 19
    assert store.get("resource:res-1")["by"] == wins[0]


def test_query_by_prefix_and_fields(store):
    store.put("resource:a", _resource("a", "water", "AVAILABLE"))
    store.put("resource:b", _resource("b", "water", "ASSIGNED"))
    store.put("resource:c", _resource("c", "food", "AVAILABLE"))
    store.put("request:x", {"id": "x", "type": "water", "status": "AVAILABLE"})

    got = store.query("resource:", type="water", status="AVAILABLE")
    assert [r["id"] for r in got] == ["a"]
    assert len(store.query("resource:")) == 3
    assert store.query("resource:", type="shelter") == []


def test_status_counts_keys_per_shard(store):
    keys = [f"request:req-{i:016x}" for i in range(30)]
    for k in keys:
        store.put(k, {"id": k})
    status = store.status()
    assert status["mode"] == "fake"
    assert len(status["nodes"]) == 6
    assert status["total_keys"] == 30
    for shard in range(3):
        expected = sum(1 for k in keys if shard_for(k) == shard)
        assert status["keys_per_shard"][str(shard)] == expected


def test_bad_if_match_is_400(store):
    store.put("resource:res-1", _resource("res-1"))
    http = store._http
    assert http.put("/store/resource:res-1", json={}, headers={"If-Match": "abc"}).status_code == 400
    assert http.put("/store/resource:res-1", json={}, headers={"If-Match": '"1"'}).status_code == 200
