"""Which shard (cabinet) holds a key. DESIGN.md section 6.

Every service must compute the SAME shard for the same key, in every process,
on every machine, forever. That is why we use SHA-1 and never Python's
built-in hash(): hash() of a str is salted per process (PYTHONHASHSEED), so two
services would disagree about where a record lives.

SHA-1 is used only because it spreads keys evenly and is available everywhere,
not for security.
"""

import hashlib

NUM_SHARDS = 3

REQUEST_PREFIX = "request:"
RESOURCE_PREFIX = "resource:"


def shard_for(key: str, num_shards: int = NUM_SHARDS) -> int:
    """Return the shard number (0 .. num_shards-1) for a storage key."""
    if not key:
        raise ValueError("key must be a non-empty string")
    # 160-bit hash -> big integer -> remainder picks the shard. Stable everywhere.
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return int(digest, 16) % num_shards


def request_key(request_id: str) -> str:
    """'req-3f9c...' -> 'request:req-3f9c...'"""
    return REQUEST_PREFIX + request_id


def resource_key(resource_id: str) -> str:
    """'res-8b1e...' -> 'resource:res-8b1e...'"""
    return RESOURCE_PREFIX + resource_id
