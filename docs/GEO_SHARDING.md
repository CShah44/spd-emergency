# Geo-sharding interface (task 5)

Settled in Step 0 so both sides build against the same thing. The code for task 5 itself
comes later; what exists today is `services/common/geohash.py`.

## Ids carry their cell

New ids are `res-<cell>-<hex>` and `req-<cell>-<hex>`, where `<cell>` is the geohash
(precision 3, a 1.40625 degree square, about 156 km tall) of the record's location. The
cell can be read from the id alone: `geohash.cell_of_id(id)`.

Ids from before geo-sharding (`res-<16 hex>`) have no cell. `cell_of_id` returns `None` for
them, and they keep being placed by `sha1(key) mod 3`. Nothing already stored needs to move.

## The calls

| Call | Owner | What it does |
|---|---|---|
| `geohash.encode(lat, lon)` | B (done) | the cell of a point |
| `geohash.cells_around(lat, lon)` | B (done) | the point's cell plus its 8 neighbours: where to look within 50 km |
| `geohash.cell_of_id(id)` | B (done) | the cell inside an id, or `None` |
| `sharding.shard_for(key)` | A | cell from the id when there is one, otherwise today's hash |
| `StorageClient.query(prefix, cells=[...], **filters)` | A | the coordinator asks only the shards that own those cells; without `cells` it asks every shard, as today |

## The one decision for A: how a cell becomes a shard

The plan says "consistent hashing over cells". That has a catch: a match looks in 9 cells,
and if each cell is hashed on its own, those 9 land on 3 shards almost at random. On average
a match would still touch about 2.9 of the 3 shards (`3 x (1 - (2/3)^9)`), so the headline
number "3 shards per match -> 1" would not appear.

So the shard has to be chosen so that **neighbouring cells usually share a shard**. The
simplest way is to shard on a coarser parent, the first 2 characters of the cell (about
1250 x 625 km): a request and all 8 of its neighbours then share a parent except right at
a parent's edge. Whatever A picks, the test is the measured `spd_shards_per_query`.

The price is the hot spot already described in the plan: a big region lands on one shard.
Measure keys per shard after seeding and report it next to the speed-up.

## A's decision: regions on a consistent-hash ring (built)

Simulated on the five cities the seed data and the load test use (2,000 requests jittered
around Bengaluru, Mumbai, Chennai, Kolkata and Ahmedabad), average **shards asked per match**:

| Cell -> shard rule | All 9 cells | Only cells within 50 km |
|---|---:|---:|
| `sha1(cell) mod 3` | 3.00 | 1.75 |
| `sha1(region) mod 3` (region = first 2 characters) | 1.60 | **1.20** |
| region on a consistent-hash ring (built) | 1.60 | **1.20** |

How the five cities fall on the 3 shards: per-cell hashing leaves one shard **empty**
(cities 2 / 0 / 3); by region, one shard holds 3 of the 5 (1 / 3 / 1 with the ring). With five
cities the best possible is 2 / 2 / 1, so a hot spot is unavoidable at this scale: report the
measured keys per shard next to the speed-up, as said above.

Why a ring rather than `mod 3` for regions, when both score the same: with the ring, a 4th
shard takes over about a quarter of the regions and nothing else moves; with `mod`, most
regions would change shard (`test_adding_a_fourth_shard_moves_only_what_the_new_shard_takes`).
Moving the data itself when a shard is added (a live rebalance) is not built.

What exists now (A):
- `sharding.shard_for(key)`: region ring for keys whose id has a cell, `sha1(key) mod 3`
  otherwise. Also `shard_for_cell`, `shards_for_cells`, `key_in_cells`, `CELLS_PARAM`.
- `StorageClient.query(prefix, cells=[...], **filters)` sends `?cells=a,b`. The coordinator
  asks only the owning shards (`spd_shards_per_query` records how many); each storage node
  returns only records in those cells, plus old-style records it holds. A bad cell is a 400.
  The fake coordinator filters the same way, so B's unit tests see the same behaviour.
- A side effect worth demonstrating: a search near Mumbai keeps working while the shard that
  owns Chennai is entirely down. Without `cells=` that search is a 503.

## What's left for B, and one recommendation

1. The request service creates ids with the cell (`res-<cell>-<hex>`, `req-<cell>-<hex>`).
2. The matcher passes `cells=` for both its lookups (free resources, and "already assigned
   to this request").
3. **Recommended: ask only for the cells that come within 50 km of the point**, not all 9.
   Most of the remaining cost is the search being wider than the radius: a point in the middle
   of a 156 km cell needs no neighbour at all. On the five cities this takes shards per match
   from 1.60 to 1.20 and cells per match from 9 to about 2.9. A small helper in
   `common/geohash.py`, e.g. `cells_near(lat, lon, km)`: keep each cell of `cells_around()`
   whose rectangle is within `km` of the point (distance to the rectangle's nearest point).

## Rules both sides rely on

- The matcher also passes `cells=` for its "is a resource already assigned to this request"
  lookup. Without that, every match still asks all 3 shards once.
- A responder does not move. If a location changes, the record is re-registered under a new id.
- Mixed data (old ids and new ids) works but is not optimal: a `cells=` query cannot see an
  old-style resource that hashed to a shard that does not own the cell. The matcher's
  fallback ("ask every shard if the cells found nothing") covers the empty case. For a clean
  benchmark, start from `--fresh`.
