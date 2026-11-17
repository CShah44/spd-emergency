import threading

import pytest

from storage_node.app.db import Conflict, NotFound, Store


@pytest.fixture
def db(tmp_path):
    s = Store(tmp_path / "node.db")
    yield s
    s.close()


def test_put_get_and_version_counting(db):
    assert db.put("k", {"a": 1, "version": 99})["version"] == 1  # client version ignored
    assert db.put("k", {"a": 2})["version"] == 2
    assert db.get("k") == {"a": 2, "version": 2}
    assert db.get("missing") is None


def test_compare_and_set(db):
    db.put("k", {"s": "AVAILABLE"})
    assert db.put("k", {"s": "ASSIGNED"}, expected_version=1)["version"] == 2
    with pytest.raises(Conflict) as exc:
        db.put("k", {"s": "AVAILABLE"}, expected_version=1)
    assert exc.value.current_version == 2
    with pytest.raises(NotFound):
        db.put("ghost", {}, expected_version=1)


def test_concurrent_cas_only_one_wins(db):
    db.put("k", {"s": "AVAILABLE"})
    wins = []

    def claim(i):
        try:
            db.put("k", {"by": i}, expected_version=1)
            wins.append(i)
        except Conflict:
            pass

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1
    assert db.get("k")["by"] == wins[0]


def test_delete_leaves_tombstone_and_versions_continue(db):
    db.put("k", {"a": 1})  # v1
    assert db.delete("k") == 2  # tombstone v2
    assert db.get("k") is None
    assert db.version_of("k") == 2
    with pytest.raises(NotFound):
        db.delete("k")
    with pytest.raises(NotFound):
        db.put("k", {}, expected_version=2)  # CAS can't revive a deleted record
    assert db.put("k", {"a": 3})["version"] == 3  # recreate continues counting


def test_replica_applies_only_newer_versions(db):
    assert db.apply_replica("k", 3, {"a": 3}) is True
    assert db.apply_replica("k", 3, {"a": 3}) is False  # same again: no-op (idempotent)
    assert db.apply_replica("k", 2, {"a": "old"}) is False  # late, older write: ignored
    assert db.get("k") == {"a": 3, "version": 3}
    assert db.apply_replica("k", 5, {"a": 5}) is True  # gaps are fine
    assert db.get("k")["version"] == 5


def test_replica_tombstone(db):
    db.apply_replica("k", 1, {"a": 1})
    assert db.apply_replica("k", 2, None, deleted=True) is True
    assert db.get("k") is None
    assert db.apply_replica("k", 1, {"a": 1}) is False  # zombie blocked by tombstone
    assert db.apply_replica("never-seen", 4, None, deleted=True) is True


def test_scan_filters_and_skips_tombstones(db):
    db.put("resource:a", {"type": "water", "status": "AVAILABLE"})
    db.put("resource:b", {"type": "water", "status": "ASSIGNED"})
    db.put("resource:c", {"type": "food", "status": "AVAILABLE"})
    db.put("request:x", {"type": "water", "status": "AVAILABLE"})
    db.put("resource:d", {"type": "water", "status": "AVAILABLE"})
    db.delete("resource:d")

    got = db.scan("resource:", {"type": "water", "status": "AVAILABLE"})
    assert got == [{"type": "water", "status": "AVAILABLE", "version": 1}]
    assert len(db.scan("resource:")) == 3
    assert len(db.scan()) == 4
    assert db.count() == 4


def test_prefix_with_like_wildcards_is_literal(db):
    db.put("a_b", {})
    db.put("axb", {})
    assert len(db.scan("a_")) == 1  # '_' is not a wildcard here


def test_sync_entries_include_tombstones(db):
    db.put("k1", {"a": 1})
    db.put("k2", {"a": 2})
    db.delete("k2")
    entries = {e["key"]: e for e in db.sync_entries()}
    assert entries["k1"] == {"key": "k1", "version": 1, "deleted": False, "record": {"a": 1, "version": 1}}
    assert entries["k2"] == {"key": "k2", "version": 2, "deleted": True, "record": None}


def test_data_survives_reopen(tmp_path):
    path = tmp_path / "node.db"
    s = Store(path)
    s.put("k", {"a": 1})
    s.put("gone", {})
    s.delete("gone")
    s.close()

    s = Store(path)
    assert s.get("k") == {"a": 1, "version": 1}
    assert s.version_of("gone") == 2
    s.close()


def test_replay_of_a_partner_brings_node_up_to_date(tmp_path):
    """Recovery in miniature: copy everything newer from a partner."""
    primary, replica = Store(tmp_path / "p.db"), Store(tmp_path / "r.db")
    primary.put("k1", {"a": 1})
    primary.put("k2", {"b": 1})
    for e in primary.sync_entries():
        replica.apply_replica(e["key"], e["version"], e["record"], e["deleted"])

    # replica "goes down"; primary keeps changing
    primary.put("k1", {"a": 2})
    primary.delete("k2")
    primary.put("k3", {"c": 1})

    for e in primary.sync_entries():
        replica.apply_replica(e["key"], e["version"], e["record"], e["deleted"])
    assert replica.sync_entries() == primary.sync_entries()
    primary.close()
    replica.close()
