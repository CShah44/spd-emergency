"""Spec demo: data is spread across the 3 shards, and each shard is copied
to its replica.

    docker compose up -d --build
    python scripts/demo_distribution.py            # 300 records
    python scripts/demo_distribution.py -n 1000 --step   # Enter between stages

What it shows:
  1. How ONE key is placed: sha1(key) -> big number -> mod 3 -> shard.
  2. N records written through the coordinator land roughly evenly on 3 shards.
  3. Each primary and its replica hold the same number of keys.
  4. Looking inside the nodes directly: a key lives on exactly its shard's two
     nodes and on none of the other four.
"""

import argparse
import hashlib
from collections import Counter

from _demo import (
    NODE_IDS, banner, check_stack, new_request, new_resource, node_direct, ok, fail,
    pause_for_audience, print_status, shard_for, step, store,
)


def explain_one_key(key: str) -> None:
    digest = hashlib.sha1(key.encode()).hexdigest()
    number = int(digest, 16)
    print(f"   key          = {key}")
    print(f"   sha1(key)    = {digest}")
    print(f"   as a number  = {str(number)[:24]}... ({len(str(number))} digits)")
    print(f"   number mod 3 = {number % 3}   -> shard {shard_for(key)}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-n", type=int, default=300, help="records to write (default 300)")
    p.add_argument("--step", action="store_true", help="wait for Enter between stages")
    args = p.parse_args()
    check_stack()

    banner("DEMO: data distribution across 3 shards (+ a replica each)")
    print_status("Cluster before writing:")
    pause_for_audience(args.step)

    step("Where does one record go? The shard rule, worked by hand")
    key, _ = new_request()
    explain_one_key(key)
    print("   Every service computes this the same way, so they all agree where a record lives.")
    pause_for_audience(args.step)

    step(f"Writing {args.n} records (half requests, half resources) through the coordinator")
    written: list[str] = []
    for i in range(args.n):
        k, rec = new_request() if i % 2 == 0 else new_resource()
        store.put(k, rec)
        written.append(k)
    per_shard = Counter(shard_for(k) for k in written)
    for s in range(3):
        bar = "#" * round(40 * per_shard[s] / args.n)
        print(f"   shard {s}: {per_shard[s]:>5} ({100 * per_shard[s] / args.n:4.1f}%)  {bar}")
    print(f"   (an even split would be {args.n / 3:.0f} each; small runs are lumpier)")
    pause_for_audience(args.step)

    st = print_status("Cluster after writing (keys = total stored on each node):")
    counts = {n["node_id"]: n["keys"] for n in st["nodes"]}
    for s in range(3):
        p_, r_ = counts.get(f"s{s}-primary"), counts.get(f"s{s}-replica")
        (ok if p_ == r_ else fail)(f"shard {s}: primary {p_} keys, replica {r_} keys"
                                   + ("  -> identical copies" if p_ == r_ else "  -> DIFFERENT"))
    pause_for_audience(args.step)

    step("Looking inside the nodes directly (bypassing the coordinator) for 3 sample keys")
    for k in written[:3]:
        holders = [n for n in NODE_IDS if node_direct(n, f"/internal/data/{k}").status_code == 200]
        expected = [f"s{shard_for(k)}-primary", f"s{shard_for(k)}-replica"]
        (ok if holders == expected else fail)(f"{k} -> shard {shard_for(k)} -> found on {', '.join(holders)}")
    print("\n   Each record lives on exactly 2 of the 6 nodes: its shard's primary and replica.")


if __name__ == "__main__":
    main()
