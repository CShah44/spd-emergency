"""One storage node's data: a single SQLite file. DESIGN.md sections 5 and 6.

Table kv(key, value_json, version, updated_at, deleted)

Three ideas live here:

1. Compare-and-set in ONE SQL statement.
   `UPDATE ... WHERE key=? AND version=?` checks and writes atomically, so two
   writers can never both succeed with the same old version.

2. Two kinds of write.
   * Client write (`put`), done on whichever node is acting as primary: the
     node decides the next version.
   * Replica write (`apply_replica`): "store exactly version N". Applied only if
     N is newer than what we hold. So applying it twice changes nothing
     (idempotent) and a late, older write can never overwrite a newer one.
     Recovery reuses the same rule.

3. Tombstones.
   A delete keeps the row with deleted=1 and a new version. If we removed the
   row, a partner that was down would never learn about the delete, and during
   recovery it would copy the "deleted" record back (a zombie record).

4. The operation log (catch-up without comparing everything).
   Every client write made HERE gets the next sequence number (seq) of this
   node's log, in the same transaction as the write. The log keeps one row per
   key: (key, seq of its latest local write). Older seqs of the same key don't
   need keeping, because a partner that missed them only needs the newest
   value, and the newest value is in kv.

   The log has an id (log_id), made when the database file is created. A node
   whose disk was wiped gets a new log_id, so nobody mistakes its new seq 1 for
   the old seq 1.

   For each partner log, a node remembers a WATERMARK: "I have every write of
   that log up to seq N". Recovery then asks the partner only for its log after
   N. The watermark only moves forward over seqs we really received (live
   replication can arrive out of order, so later seqs wait in `_pending` until
   the gap is filled). A missed write therefore leaves the watermark behind it,
   and the next catch-up fetches it again: it can be too low (extra work), never
   too high (lost data).
"""

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from common.models import utc_now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value_json TEXT    NOT NULL,
    version    INTEGER NOT NULL,
    updated_at TEXT    NOT NULL,
    deleted    INTEGER NOT NULL DEFAULT 0
);
-- This node's own writes: the seq of each key's latest local write.
CREATE TABLE IF NOT EXISTS oplog (
    key TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE
);
-- "I have every write of partner log <log_id> up to <seq>."
CREATE TABLE IF NOT EXISTS watermarks (
    log_id TEXT PRIMARY KEY,
    seq    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Out-of-order seqs we hold while waiting for a gap to fill. If a gap never fills
# (a write that was never replicated), we stop collecting: the watermark then
# just stays behind, which is safe.
MAX_PENDING = 100_000


class NotFound(Exception):
    pass


class Conflict(Exception):
    def __init__(self, key: str, current_version: int):
        super().__init__(f"{key}: stored version is {current_version}")
        self.current_version = current_version


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit, every statement is its own transaction.
        # check_same_thread=False: FastAPI calls us from a thread pool; the lock
        # below makes sure only one thread uses the connection at a time.
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        # WAL: readers don't block the writer. synchronous=FULL: a commit is on
        # disk before we answer 200, so an acknowledged write survives a crash.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)
        self._conn.execute("INSERT OR IGNORE INTO meta (name, value) VALUES ('log_id', ?)", (uuid.uuid4().hex,))
        self.log_id: str = self._conn.execute("SELECT value FROM meta WHERE name = 'log_id'").fetchone()[0]
        self._lock = threading.Lock()
        self._pending: dict[str, set[int]] = {}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- client writes -----------------------------------------------------------

    def put(self, key: str, record: dict[str, Any], expected_version: int | None = None) -> dict[str, Any]:
        """Create/overwrite (expected_version=None) or compare-and-set.

        Returns the saved record with its new version.
        Raises NotFound (CAS on a missing or deleted key) or Conflict (version moved on).
        """
        return self.put_logged(key, record, expected_version)[0]

    def put_logged(
        self, key: str, record: dict[str, Any], expected_version: int | None = None
    ) -> tuple[dict[str, Any], int]:
        """put(), also returning the write's seq in this node's log."""
        body = _encode(record)
        now = utc_now_iso()
        with self._lock, self._transaction():
            if expected_version is None:
                # Upsert. Versions continue past a tombstone (v3 deleted -> v4 recreated),
                # so "newer version wins" stays true on the replica.
                rows = self._conn.execute(
                    """
                    INSERT INTO kv (key, value_json, version, updated_at, deleted)
                    VALUES (?, ?, 1, ?, 0)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        version    = kv.version + 1,
                        updated_at = excluded.updated_at,
                        deleted    = 0
                    RETURNING version
                    """,
                    (key, body, now),
                ).fetchall()  # fetchall, not fetchone: finishes the statement so it commits
            else:
                # The compare-and-set: only rows still at the expected version match.
                rows = self._conn.execute(
                    """
                    UPDATE kv SET value_json = ?, version = version + 1, updated_at = ?
                    WHERE key = ? AND version = ? AND deleted = 0
                    RETURNING version
                    """,
                    (body, now, key, expected_version),
                ).fetchall()
                if not rows:
                    # Nothing matched: work out why, for the right error code.
                    current = self._conn.execute(
                        "SELECT version, deleted FROM kv WHERE key = ?", (key,)
                    ).fetchone()
                    if current is None or current[1]:
                        raise NotFound(key)
                    raise Conflict(key, current[0])
            seq = self._log_write(key)
        return _decode(body, rows[0][0]), seq

    def delete(self, key: str) -> int:
        """Turn the record into a tombstone. Returns the tombstone's version."""
        return self.delete_logged(key)[0]

    def delete_logged(self, key: str) -> tuple[int, int]:
        """delete(), returning (tombstone version, seq in this node's log)."""
        with self._lock, self._transaction():
            rows = self._conn.execute(
                """
                UPDATE kv SET deleted = 1, value_json = '{}', version = version + 1, updated_at = ?
                WHERE key = ? AND deleted = 0
                RETURNING version
                """,
                (utc_now_iso(), key),
            ).fetchall()
            if not rows:
                raise NotFound(key)
            seq = self._log_write(key)
        return rows[0][0], seq

    # -- replica writes ------------------------------------------------------------

    def apply_replica(
        self,
        key: str,
        version: int,
        record: dict[str, Any] | None,
        deleted: bool = False,
        origin: tuple[str, int] | None = None,
    ) -> bool:
        """Store exactly this version, but only if it is newer than ours.

        `origin` = (log_id, seq) of the write on the node that made it, so we can
        move our watermark for that log. Returns True if applied, False if we
        already had this version or a newer one.
        """
        entry = {"key": key, "version": version, "deleted": deleted, "record": record}
        with self._lock, self._transaction():
            applied, _ = self._apply(entry, detect_conflict=False)
            if origin is not None:
                self._saw_seq(*origin)
        return applied

    def apply_many(self, entries: list[dict[str, Any]]) -> int:
        """apply_replica for a whole batch in ONE transaction. Returns how many were applied."""
        return self.apply_batch(entries)["applied"]

    def apply_batch(
        self,
        entries: list[dict[str, Any]],
        origin_log_id: str | None = None,
        through_seq: int | None = None,
    ) -> dict[str, int]:
        """Recovery: apply a batch in ONE transaction (all or nothing).

        Each entry: {key, version, deleted, record}. If the batch is "everything
        log <origin_log_id> wrote up to <through_seq>", the watermark for that
        log moves up to through_seq in the same transaction.

        Returns {"applied": n, "conflicts": n}. A conflict is the same key at the
        same version with different data: possible only after a whole-shard
        outage, when both nodes took writes alone. "Newer wins" can't choose, so
        we keep ours and count it.
        """
        applied = conflicts = 0
        with self._lock, self._transaction():
            for e in entries:
                ok, conflict = self._apply(e, detect_conflict=True)
                applied += ok
                conflicts += conflict
            if origin_log_id is not None and through_seq is not None:
                self._raise_watermark(origin_log_id, through_seq)
        return {"applied": applied, "conflicts": conflicts}

    # -- reads -----------------------------------------------------------------------

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json, version FROM kv WHERE key = ? AND deleted = 0", (key,)
            ).fetchone()
        return None if row is None else _decode(row[0], row[1])

    def version_of(self, key: str) -> int:
        """Stored version, including tombstones; 0 if the key was never seen."""
        with self._lock:
            row = self._conn.execute("SELECT version FROM kv WHERE key = ?", (key,)).fetchone()
        return 0 if row is None else row[0]

    def scan(self, prefix: str = "", filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Live records whose key starts with prefix, filtered by exact field matches."""
        filters = filters or {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT value_json, version FROM kv WHERE deleted = 0 AND substr(key, 1, ?) = ? ORDER BY key",
                (len(prefix), prefix),
            ).fetchall()
        records = [_decode(body, version) for body, version in rows]
        return [r for r in records if all(str(r.get(f)) == v for f, v in filters.items())]

    def sync_entries(self, prefix: str = "") -> list[dict[str, Any]]:
        """Everything, tombstones included: what a recovering partner needs to catch up."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value_json, version, deleted FROM kv WHERE substr(key, 1, ?) = ? ORDER BY key",
                (len(prefix), prefix),
            ).fetchall()
        return [
            {
                "key": key,
                "version": version,
                "deleted": bool(deleted),
                "record": None if deleted else _decode(body, version),
            }
            for key, body, version, deleted in rows
        ]

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM kv WHERE deleted = 0").fetchone()[0]

    # -- operation log -----------------------------------------------------------------

    def position(self) -> dict[str, Any]:
        """Where this node is: its own log (id + latest seq) and its watermark for
        every partner log it has caught up with."""
        with self._lock:
            return {
                "log_id": self.log_id,
                "head": self._head(),
                "watermarks": dict(self._conn.execute("SELECT log_id, seq FROM watermarks").fetchall()),
            }

    def oplog_since(self, after: int, limit: int = 1000) -> dict[str, Any]:
        """This node's own writes with seq > after, oldest first, each with the
        key's CURRENT value (tombstones included). Current, not historical: a
        partner only needs the newest value, and "newer wins" makes it safe even
        if the key changed again since."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT o.seq, o.key, k.value_json, k.version, k.deleted
                FROM oplog o JOIN kv k ON k.key = o.key
                WHERE o.seq > ? ORDER BY o.seq LIMIT ?
                """,
                (after, limit),
            ).fetchall()
            head = self._head()
        entries = [
            {
                "seq": seq,
                "key": key,
                "version": version,
                "deleted": bool(deleted),
                "record": None if deleted else _decode(body, version),
            }
            for seq, key, body, version, deleted in rows
        ]
        return {"log_id": self.log_id, "head": head, "entries": entries}

    # -- internals (call with self._lock held) ------------------------------------------

    def _transaction(self):
        return _Transaction(self._conn)

    def _head(self) -> int:
        return self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM oplog").fetchone()[0]

    def _log_write(self, key: str) -> int:
        """Give a local write the next seq. Same transaction as the write itself."""
        seq = self._head() + 1
        self._conn.execute(
            "INSERT INTO oplog (key, seq) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET seq = excluded.seq",
            (key, seq),
        )
        return seq

    def _apply(self, e: dict[str, Any], detect_conflict: bool) -> tuple[bool, bool]:
        deleted = bool(e.get("deleted"))
        record = None if deleted else e.get("record")
        body = "{}" if record is None else _encode(record)
        conflict = False
        if detect_conflict:
            row = self._conn.execute(
                "SELECT value_json, version, deleted FROM kv WHERE key = ?", (e["key"],)
            ).fetchone()
            if row is not None and row[1] == e["version"]:
                conflict = (bool(row[2]), json.loads(row[0])) != (deleted, json.loads(body))
        cur = self._conn.execute(
            """
            INSERT INTO kv (key, value_json, version, updated_at, deleted)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                version    = excluded.version,
                updated_at = excluded.updated_at,
                deleted    = excluded.deleted
            WHERE excluded.version > kv.version
            """,
            (e["key"], body, e["version"], utc_now_iso(), int(deleted)),
        )
        return cur.rowcount == 1, conflict

    def _watermark(self, log_id: str) -> int | None:
        row = self._conn.execute("SELECT seq FROM watermarks WHERE log_id = ?", (log_id,)).fetchone()
        return None if row is None else row[0]

    def _saw_seq(self, log_id: str, seq: int) -> None:
        """Live replication delivered write <seq> of <log_id>: move the watermark
        if it closes the gap, otherwise remember it until the gap is filled."""
        pending = self._pending.setdefault(log_id, set())
        mark = self._watermark(log_id)
        if mark is not None and seq <= mark:
            return
        if len(pending) < MAX_PENDING:
            pending.add(seq)
        if mark is not None:
            self._raise_watermark(log_id, mark)

    def _raise_watermark(self, log_id: str, seq: int) -> None:
        """Watermark = max(current, seq), then absorb any pending seqs that now follow on."""
        mark = max(self._watermark(log_id) or 0, seq)
        pending = self._pending.setdefault(log_id, set())
        pending.difference_update([s for s in pending if s <= mark])
        while mark + 1 in pending:
            mark += 1
            pending.discard(mark)
        self._conn.execute(
            "INSERT INTO watermarks (log_id, seq) VALUES (?, ?) ON CONFLICT(log_id) DO UPDATE SET seq = excluded.seq",
            (log_id, mark),
        )


class _Transaction:
    """BEGIN ... COMMIT, or ROLLBACK if anything inside raises (all or nothing)."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self):
        self._conn.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, *_):
        self._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def _encode(record: dict[str, Any]) -> str:
    # The version column is the source of truth, so we don't store a copy inside the JSON.
    return json.dumps({k: v for k, v in record.items() if k != "version"}, separators=(",", ":"))


def _decode(body: str, version: int) -> dict[str, Any]:
    return {**json.loads(body), "version": version}
