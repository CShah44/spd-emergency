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
"""

import json
import sqlite3
import threading
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
)
"""


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
        self._conn.execute(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- client writes -----------------------------------------------------------

    def put(self, key: str, record: dict[str, Any], expected_version: int | None = None) -> dict[str, Any]:
        """Create/overwrite (expected_version=None) or compare-and-set.

        Returns the saved record with its new version.
        Raises NotFound (CAS on a missing or deleted key) or Conflict (version moved on).
        """
        body = _encode(record)
        now = utc_now_iso()
        with self._lock:
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
        return _decode(body, rows[0][0])

    def delete(self, key: str) -> int:
        """Turn the record into a tombstone. Returns the tombstone's version."""
        with self._lock:
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
        return rows[0][0]

    # -- replica writes ------------------------------------------------------------

    def apply_replica(
        self, key: str, version: int, record: dict[str, Any] | None, deleted: bool = False
    ) -> bool:
        """Store exactly this version, but only if it is newer than ours.

        Returns True if applied, False if we already had this version or a newer one.
        """
        body = "{}" if deleted or record is None else _encode(record)
        with self._lock:
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
                (key, body, version, utc_now_iso(), int(deleted)),
            )
            return cur.rowcount == 1

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


def _encode(record: dict[str, Any]) -> str:
    # The version column is the source of truth, so we don't store a copy inside the JSON.
    return json.dumps({k: v for k, v in record.items() if k != "version"}, separators=(",", ":"))


def _decode(body: str, version: int) -> dict[str, Any]:
    return {**json.loads(body), "version": version}
