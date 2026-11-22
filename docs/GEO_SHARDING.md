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

## Rules both sides rely on

- The matcher also passes `cells=` for its "is a resource already assigned to this request"
  lookup. Without that, every match still asks all 3 shards once.
- A responder does not move. If a location changes, the record is re-registered under a new id.
- Mixed data (old ids and new ids) works but is not optimal: a `cells=` query cannot see an
  old-style resource that hashed to a shard that does not own the cell. The matcher's
  fallback ("ask every shard if the cells found nothing") covers the empty case. For a clean
  benchmark, start from `--fresh`.
