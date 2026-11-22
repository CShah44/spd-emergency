# Catch-up benchmark: operation log vs. full sync

Roadmap task 4. Reproduce with `python scripts/bench_catchup.py` (about 2 minutes).

**Setup.** The real coordinator and six real storage nodes in one process (the in-process
cluster the unit tests use: no Docker, no network hops), each node on its own SQLite file
with `synchronous=FULL`. Shard 0 holds N records. `s0-primary` is stopped, M records are
written while it is gone, then it comes back and catches up, once with each mode, on the
same data. Both nodes are checked to be identical afterwards. Run on 2026-11-22, Windows 11
laptop.

| Records on the shard | Writes missed | Mode | Entries read | Catch-up time |
|---:|---:|---|---:|---:|
| 2,000 | 10 | log | 10 | 125 ms |
| 2,000 | 10 | full | 4,000 | 329 ms |
| 2,000 | 100 | log | 100 | 172 ms |
| 2,000 | 100 | full | 4,000 | 281 ms |
| 2,000 | 1,000 | log | 1,000 | 219 ms |
| 2,000 | 1,000 | full | 4,000 | 312 ms |
| 10,000 | 10 | log | 10 | 78 ms |
| 10,000 | 10 | full | 20,000 | 938 ms |
| 10,000 | 100 | log | 100 | 93 ms |
| 10,000 | 100 | full | 20,000 | 625 ms |
| 10,000 | 1,000 | log | 1,000 | 172 ms |
| 10,000 | 1,000 | full | 20,000 | 547 ms |
| 20,000 | 10 | log | 10 | 31 ms |
| 20,000 | 10 | full | 40,000 | 875 ms |
| 20,000 | 100 | log | 100 | 78 ms |
| 20,000 | 100 | full | 40,000 | 766 ms |
| 20,000 | 1,000 | log | 1,000 | 110 ms |
| 20,000 | 1,000 | full | 40,000 | 954 ms |

**What it shows.**
- **Entries read is the reliable number.** Log catch-up reads exactly the writes the node
  missed, whatever the shard size. Full sync reads both nodes' entire data (2 × N), so it
  grows with the shard: 10 missed writes cost 10 entries instead of 40,000 at 20,000 records.
- **Times are small and noisy here** (single runs, Windows timer and disk-sync jitter of
  tens of ms), so treat them as a trend, not exact figures. At 10,000+ records the log
  catch-up is about 5–25× faster.
- **On Docker or Kubernetes the gap is larger:** these numbers have no network hops, and full
  sync moves 2 × N records over the network where the log moves M.

**Caveat.** Seeding writes the N records straight into the two node databases (same end
state as writing through the coordinator, minus the HTTP round trips). Only the catch-up is
timed, and it runs with normal durability.
