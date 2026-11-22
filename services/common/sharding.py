"""Which shard (cabinet) holds a key. DESIGN.md section 6, docs/GEO_SHARDING.md.

Two rules, chosen by the key:

  * A key whose id carries a geohash cell ('resource:res-tdr-8b1e...', see
    common/geohash.py) is placed by its REGION: the cell's first 2 characters
    (a parent about 1,250 x 625 km), looked up on a consistent-hash ring. Nearby
    records share a region, so a "what is free near X" search usually needs one
    shard instead of all of them (roadmap task 5).
  * Any other key (old-style ids, anything without a cell) keeps the original
    rule: sha1(key) mod num_shards. Nothing already stored has to move.

Why a ring and not "hash mod 3" for regions: with a ring, adding a 4th shard
moves only the regions the new shard takes over (about a quarter of them);
with mod, most regions would change shard.

Every service must compute the SAME shard for the same key, in every process,
on every machine, forever. That is why we use SHA-1 and never Python's built-in
hash(): hash() of a str is salted per process (PYTHONHASHSEED), so two services
would disagree about where a record lives. SHA-1 is used only because it spreads
keys evenly and is available everywhere, not for security.
"""

import bisect
import functools
import hashlib

from common import geohash

NUM_SHARDS = 3

REQUEST_PREFIX = "request:"
RESOURCE_PREFIX = "resource:"

# The query parameter that carries geohash cells: ?cells=tdr,tf2 (a reserved name:
# a record field called "cells" can't be used as a query filter).
CELLS_PARAM = "cells"

REGION_CHARS = 2  # a region is the cell's parent: its first 2 geohash characters
POINTS_PER_SHARD = 64  # spots each shard takes on the ring: more = a more even share


def _sha1(text: str) -> int:
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest(), 16)


@functools.lru_cache(maxsize=None)
def _ring(num_shards: int) -> tuple[list[int], list[int]]:
    """The consistent-hash ring: each shard owns POINTS_PER_SHARD spots on a circle
    of SHA-1 values. A region belongs to the first spot at or after its own hash."""
    spots = sorted((_sha1(f"shard-{s}-{i}"), s) for s in range(num_shards) for i in range(POINTS_PER_SHARD))
    return [p for p, _ in spots], [s for _, s in spots]


def region_of(cell: str) -> str:
    return cell[:REGION_CHARS]


def shard_for_cell(cell: str, num_shards: int = NUM_SHARDS) -> int:
    """The shard that owns every record in this geohash cell."""
    positions, owners = _ring(num_shards)
    return owners[bisect.bisect_left(positions, _sha1(region_of(cell))) % len(positions)]


def shards_for_cells(cells: list[str], num_shards: int = NUM_SHARDS) -> set[int]:
    return {shard_for_cell(c, num_shards) for c in cells}


def cell_of_key(key: str) -> str | None:
    """'resource:res-tdr-8b1e...' -> 'tdr'. None for a key without a cell."""
    return geohash.cell_of_id(key.split(":", 1)[-1])


def parse_cells(raw: str | None) -> set[str] | None:
    """'tdr,tf2' -> {'tdr', 'tf2'}; None/empty -> None (no cell filter). Raises
    ValueError for something that isn't a geohash cell."""
    if not raw:
        return None
    cells = {c.strip() for c in raw.split(",") if c.strip()}
    bad = [c for c in cells if not geohash.is_cell(c)]
    if bad:
        raise ValueError(f"not geohash cells: {sorted(bad)}")
    return cells or None


def key_in_cells(key: str, cells: set[str] | None) -> bool:
    """Does a cells= query include this key? Keys in one of the cells do; keys with
    no cell at all (old-style ids) do too, so they are never hidden on a shard
    that is asked; keys in other cells don't."""
    if cells is None:
        return True
    cell = cell_of_key(key)
    return cell is None or cell in cells


def shard_for(key: str, num_shards: int = NUM_SHARDS) -> int:
    """Return the shard number (0 .. num_shards-1) for a storage key."""
    if not key:
        raise ValueError("key must be a non-empty string")
    cell = cell_of_key(key)
    if cell is not None:
        return shard_for_cell(cell, num_shards)
    # 160-bit hash -> big integer -> remainder picks the shard. Stable everywhere.
    return _sha1(key) % num_shards


def request_key(request_id: str) -> str:
    """'req-3f9c...' -> 'request:req-3f9c...'"""
    return REQUEST_PREFIX + request_id


def resource_key(resource_id: str) -> str:
    """'res-8b1e...' -> 'resource:res-8b1e...'"""
    return RESOURCE_PREFIX + resource_id
