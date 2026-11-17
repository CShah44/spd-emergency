"""Spec demo: a storage node fails and the system keeps working on the replica.

    python scripts/demo_storage_failure.py                  # stop shard 0's leader container
    python scripts/demo_storage_failure.py --shard 2 --method pause
    python scripts/demo_storage_failure.py --restore        # bring the node back at the end

--method stop   docker compose stop (the container really dies: connection refused)
--method pause  POST /admin/pause on the node (it answers 503 and stops heartbeating;
                handy on Kubernetes, where a stopped pod is restarted at once)

By default the failed node is LEFT DOWN, so you can run demo_recovery.py next.
"""

import argparse
import time

from _demo import (
    banner, check_stack, fail, fail_node, leader_of, new_request, new_resource, node_state, ok,
    pause_for_audience, print_status, revive_node, step, store, wait_for,
)
from common.storage_client import StorageError


def timed(fn):
    t = time.monotonic()
    result = fn()
    return result, (time.monotonic() - t) * 1000


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--shard", type=int, default=0, choices=[0, 1, 2])
    p.add_argument("--node", help="node to fail (default: the shard's current leader)")
    p.add_argument("--method", choices=["stop", "pause"], default="stop")
    p.add_argument("--restore", action="store_true", help="bring the node back at the end")
    p.add_argument("--step", action="store_true", help="wait for Enter between stages")
    args = p.parse_args()
    check_stack()

    shard = args.shard
    victim = args.node or leader_of(shard)
    if victim is None:
        raise SystemExit(f"shard {shard} has no live node; restore it first")

    banner(f"DEMO: storage failure on shard {shard} ({victim})")

    step(f"Preparing: 5 requests and 5 resources stored on shard {shard}")
    keys = []
    for _ in range(5):
        for maker in (new_request, new_resource):
            k, rec = maker(shard)
            store.put(k, rec)
            keys.append(k)
    sample = keys[0]
    ok(f"sample record {sample} is version {store.get(sample)['version']}")
    print_status("BEFORE the failure:", focus_shard=shard)
    pause_for_audience(args.step)

    step(f"FAILING {victim} with method '{args.method}'")
    fail_node(victim, args.method)
    ok(f"{victim} is {'stopped' if args.method == 'stop' else 'paused'}")
    pause_for_audience(args.step)

    step("DURING the failure: keep using shard " + str(shard))
    rec, ms = timed(lambda: store.get(sample))
    ok(f"read {sample}: status {rec['status']}, version {rec['version']} ({ms:.0f} ms)")
    new_status = "MATCHED" if sample.startswith("request:") else "ASSIGNED"
    saved, ms = timed(lambda: store.put_if_version(sample, {**rec, "status": new_status}, rec["version"]))
    ok(f"conditional update (If-Match {rec['version']}) -> {saved['status']}, version {saved['version']} ({ms:.0f} ms)")
    k, new = new_request(shard)
    _, ms = timed(lambda: store.put(k, new))
    ok(f"created new record {k} ({ms:.0f} ms)")
    try:
        found = store.query("resource:", status="AVAILABLE")
        ok(f"search across all shards still works: {len(found)} AVAILABLE resources")
    except StorageError as exc:
        fail(f"search failed: {exc}")

    detected = wait_for(lambda: node_state(victim) == "DOWN", timeout=15)
    if detected is not None:
        ok(f"coordinator has marked {victim} DOWN; shard {shard} is now led by {leader_of(shard)}")
    else:
        fail(f"coordinator still thinks {victim} is up")
    print("   (The first failed call marks a node DOWN at once. With no traffic, the")
    print("    missing heartbeats would do it within 10 s.)")
    print_status("DURING the failure:", focus_shard=shard)
    pause_for_audience(args.step)

    step("Other shards are unaffected")
    for other in {0, 1, 2} - {shard}:
        k, rec = new_resource(other)
        store.put(k, rec)
        ok(f"shard {other}: wrote {k}, led by {leader_of(other)}")

    if args.restore:
        step(f"Restoring {victim}")
        revive_node(victim, args.method)
        took = wait_for(lambda: node_state(victim) == "UP", timeout=30)
        ok(f"{victim} is back UP after {took:.1f}s (it caught up first)" if took else f"{victim} not UP yet")
        print_status("AFTER restoring:", focus_shard=shard)
    else:
        print(f"\n   {victim} is still down. Next: python scripts/demo_recovery.py --node {victim}"
              + (" --method pause" if args.method == "pause" else ""))


if __name__ == "__main__":
    main()
