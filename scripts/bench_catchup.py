"""Benchmark: catch-up from the operation log vs. full sync (roadmap task 4).

    python scripts/bench_catchup.py              # the default table
    python scripts/bench_catchup.py --quick      # smaller, for a smoke run

Runs the REAL coordinator and six REAL storage nodes in one process (the same
in-process cluster the unit tests use: no Docker, no network), each node on its
own SQLite file with the normal settings. For each data size it:
  1. stores N records on shard 0 (fast seeding: disk syncing is off only here),
  2. stops s0-primary and writes M more records it misses,
  3. brings it back and times its catch-up, once in "log" mode and once in
     "full" mode, on the same data.

What to expect: log catch-up reads about M entries whatever N is; full sync
reads about 2N (both nodes' whole data), so it slows down as the shard grows.
Times here exclude real network hops, so on Docker or Kubernetes both modes
are slower, and full sync falls further behind (it moves far more data).
"""

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "services"), str(ROOT / "tests" / "unit")]

from fastapi.testclient import TestClient  # noqa: E402

from common.sharding import shard_for  # noqa: E402
from conftest import Cluster  # noqa: E402  (tests/unit/conftest.py: the in-process cluster)

NODE, PARTNER, SHARD = "s0-primary", "s0-replica", 0


def keys_on_shard(n: int, start: int = 0) -> list[str]:
    out, i = [], start
    while len(out) < n:
        key = f"resource:bench{i}"
        if shard_for(key) == SHARD:
            out.append(key)
        i += 1
    return out


def set_fsync(cluster: Cluster, on: bool) -> None:
    for store in cluster.stores.values():
        store._conn.execute(f"PRAGMA synchronous={'FULL' if on else 'OFF'}")


def record(i: int) -> dict:
    return {"type": "water", "name": f"Tanker {i}", "location": {"lat": 12.97, "lon": 77.59},
            "status": "AVAILABLE", "capacity": 500}


def seed(cluster: Cluster, keys: list[str]) -> None:
    """Same end state as writing each record through the coordinator (on the
    primary's log, replicated, replica's watermark up to date), minus the HTTP
    round trips that would make seeding 20,000 records take minutes."""
    primary, replica = cluster.stores[NODE], cluster.stores[PARTNER]
    for i, key in enumerate(keys):
        primary.put(key, record(i))
    entries = primary.oplog_since(0, limit=len(keys) + 1)["entries"]
    replica.apply_batch(entries, primary.log_id, primary.position()["head"])


def catch_up(coord, cluster: Cluster, mode: str, missed_keys: list[str]) -> dict:
    cluster.coordinator.state.recovery_mode = mode
    cluster.net.dead.add(NODE)
    cluster.coordinator.state.registry.mark_down(NODE, "bench: stopped")
    for i, key in enumerate(missed_keys):
        assert coord.put(f"/store/{key}", json=record(i)).status_code == 200

    set_fsync(cluster, True)  # the catch-up itself runs with normal durability
    cluster.net.dead.discard(NODE)
    assert cluster.register(coord, NODE) == "RECOVERING"
    cluster.wait_state(coord, NODE, "UP", timeout=600)
    set_fsync(cluster, False)

    report = next(n for n in coord.get("/internal/status").json()["nodes"] if n["node_id"] == NODE)["last_recovery"]
    assert report["mode"] == mode, report
    assert cluster.stores[NODE].sync_entries() == cluster.stores[PARTNER].sync_entries(), "nodes differ!"
    return report


def run(sizes: list[int], missed: list[int]) -> list[dict]:
    rows = []
    for size in sizes:
        with tempfile.TemporaryDirectory() as tmp:
            cluster = Cluster(Path(tmp))
            try:
                with TestClient(cluster.coordinator) as coord:
                    cluster.register_all(coord)
                    set_fsync(cluster, False)
                    base = keys_on_shard(size)
                    started = time.monotonic()
                    seed(cluster, base)
                    print(f"seeded {size} records on shard 0 in {time.monotonic() - started:.0f}s", flush=True)

                    for m in missed:
                        for mode in ("log", "full"):
                            # Overwrite existing records, so the data size stays N.
                            report = catch_up(coord, cluster, mode, base[:m])
                            rows.append({"records": size, "missed": m, **report})
                            print(f"  N={size:>6} M={m:>5} {mode:>4}: read {report['entries_read']:>6} entries, "
                                  f"{report['seconds'] * 1000:8.1f} ms", flush=True)
            finally:
                cluster.close()
    return rows


def table(rows: list[dict]) -> str:
    out = ["| Records on the shard | Writes missed | Mode | Entries read | Catch-up time |",
           "|---:|---:|---|---:|---:|"]
    for r in rows:
        out.append(f"| {r['records']:,} | {r['missed']:,} | {r['mode']} | {r['entries_read']:,} | "
                   f"{r['seconds'] * 1000:,.0f} ms |")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--quick", action="store_true", help="small sizes, for a smoke run")
    args = p.parse_args()
    sizes, missed = ([500, 2000], [10, 100]) if args.quick else ([2000, 10000, 20000], [10, 100, 1000])
    print(table(run(sizes, missed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
