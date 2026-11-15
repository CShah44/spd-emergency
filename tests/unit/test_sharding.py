import subprocess
import sys
from collections import Counter

from common.models import new_request_id, new_resource_id
from common.sharding import resource_key, request_key, shard_for


def test_same_key_same_shard():
    key = "request:req-3f9c2a1b7d4e4f0a"
    assert len({shard_for(key) for _ in range(100)}) == 1


def test_known_values_never_change():
    # Pinned answers. If these ever change, every stored record is in the
    # "wrong" cabinet, so this must stay green forever.
    assert shard_for("request:req-3f9c2a1b7d4e4f0a") == 1
    assert shard_for("resource:res-8b1e0c55a2f94d17") == 1


def test_same_answer_in_a_different_process():
    # The whole point of not using hash(): a fresh interpreter (with a
    # different PYTHONHASHSEED) must agree with this one.
    key = "resource:res-8b1e0c55a2f94d17"
    code = f"from common.sharding import shard_for; print(shard_for({key!r}))"
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONPATH": "services", "PYTHONHASHSEED": "12345", "SYSTEMROOT": _systemroot()},
    ).stdout.strip()
    assert int(out) == shard_for(key)


def test_result_is_in_range():
    for i in range(200):
        assert 0 <= shard_for(f"k{i}") < 3
        assert 0 <= shard_for(f"k{i}", num_shards=5) < 5


def test_keys_spread_across_all_shards():
    keys = [request_key(new_request_id()) for _ in range(1500)] + [
        resource_key(new_resource_id()) for _ in range(1500)
    ]
    counts = Counter(shard_for(k) for k in keys)
    assert set(counts) == {0, 1, 2}
    # 3000 keys / 3 = 1000 each; allow generous slack for randomness.
    for shard, n in counts.items():
        assert 800 < n < 1200, f"shard {shard} got {n} keys"


def test_empty_key_rejected():
    import pytest

    with pytest.raises(ValueError):
        shard_for("")


def _systemroot() -> str:
    import os

    return os.environ.get("SYSTEMROOT", "")
