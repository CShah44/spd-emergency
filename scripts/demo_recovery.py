"""Spec demo: a failed node comes back and catches up before serving again.

    python scripts/demo_recovery.py --node s0-primary          # after demo_storage_failure.py
    python scripts/demo_recovery.py                            # does the failure itself first
    python scripts/demo_recovery.py --node s1-replica --method pause

Story:
  1. The node is down (if no node is down, we fail shard 0's leader first).
  2. While it's down we change its shard: update one record, delete one,
     create new ones. The node misses all of it.
  3. We bring it back and watch it go DOWN -> RECOVERING -> UP.
  4. We look INSIDE the recovered node (bypassing the coordinator) to prove
     it has every change, and that it is identical to its partner.
"""

import argparse
import time

from _demo import (
    METHODS, METHODS_HELP, banner, check_stack, coordinator_logs_cmd, fail, fail_node, leader_of, new_request, node_direct, node_state, ok,
    pause_for_audience, print_status, revive_node, step, store, wait_for,
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--node", help="the node to recover (default: any DOWN node, else fail shard 0's leader)")
    p.add_argument("--method", choices=METHODS, default="stop",
                   help="how to fail the node: " + METHODS_HELP)
    p.add_argument("--step", action="store_true", help="wait for Enter between stages")
    args = p.parse_args()
    check_stack()

    st = store.status()
    down = [n["node_id"] for n in st["nodes"] if n["state"] == "DOWN"]
    victim = args.node or (down[0] if down else None)
    if victim is None:
        victim = leader_of(0)
        banner(f"DEMO: node recovery (failing {victim} first)")
        step(f"No node is down, so failing {victim} with method '{args.method}'")
        fail_node(victim, args.method)
    else:
        banner(f"DEMO: node recovery for {victim}")
    shard = int(victim[1])
    partner = f"s{shard}-{'replica' if victim.endswith('primary') else 'primary'}"

    # Make sure the coordinator knows (a request to the shard marks it DOWN immediately).
    probe_key, probe = new_request(shard)
    store.put(probe_key, probe)
    if wait_for(lambda: node_state(victim) == "DOWN", timeout=15) is None:
        raise SystemExit(f"{victim} is not DOWN; fail it first (demo_storage_failure.py)")

    step(f"While {victim} is down, changing shard {shard} (it misses all of this)")
    k_upd, rec = new_request(shard)
    store.put(k_upd, rec)
    rec = store.get(k_upd)
    upd = store.put_if_version(k_upd, {**rec, "status": "MATCHED"}, rec["version"])
    ok(f"updated {k_upd} -> MATCHED (version {upd['version']})")
    k_del, rec = new_request(shard)
    store.put(k_del, rec)
    store.delete(k_del)
    ok(f"deleted {k_del}")
    created = []
    for _ in range(5):
        k, rec = new_request(shard)
        store.put(k, rec)
        created.append(k)
    ok(f"created {len(created)} new records")
    print_status(f"BEFORE recovery ({victim} is DOWN, its partner {partner} has the latest data):", shard)
    pause_for_audience(args.step)

    step(f"Bringing {victim} back")
    revive_node(victim, args.method)
    seen, last, start = [], None, time.monotonic()
    while time.monotonic() - start < 40:
        try:
            state = node_state(victim)
        except Exception:  # noqa: BLE001
            state = None
        if state and state != last:
            seen.append(f"{state} (+{time.monotonic() - start:.1f}s)")
            last = state
        if state == "UP" and len(seen) > 1:
            break
        time.sleep(0.1)
    print("   state changes seen: " + "  ->  ".join(seen))
    print("   (RECOVERING usually lasts only milliseconds, so the poll may not catch it;")
    print(f"    the coordinator log shows it: {coordinator_logs_cmd()})")

    report = next(n for n in store.status()["nodes"] if n["node_id"] == victim)["last_recovery"]
    if report:
        ok(f"recovery report: copied {report['pulled']} entries from {report['partner']}, "
           f"sent back {report['pushed']}, conflicts {report['conflicts']}, took {report['seconds'] * 1000:.0f} ms")
    print_status("AFTER recovery:", shard)
    print(f"   Leader of shard {shard}: {leader_of(shard)}. {victim} is back but does not take")
    print("   leadership back (no failback); it moves only when the leader itself fails.")
    pause_for_audience(args.step)

    step(f"Looking INSIDE {victim} directly (not through the coordinator)")
    r = node_direct(victim, f"/internal/data/{k_upd}")
    (ok if r.status_code == 200 and r.json()["status"] == "MATCHED" else fail)(
        f"update arrived: {k_upd} is {r.json().get('status') if r.status_code == 200 else r.status_code}")
    r = node_direct(victim, f"/internal/data/{k_del}")
    (ok if r.status_code == 404 else fail)(f"delete arrived: {k_del} -> {r.status_code} (tombstone kept)")
    have = sum(node_direct(victim, f"/internal/data/{k}").status_code == 200 for k in created)
    (ok if have == len(created) else fail)(f"new records arrived: {have}/{len(created)}")

    mine = node_direct(victim, "/internal/sync").json()["entries"]
    theirs = node_direct(partner, "/internal/sync").json()["entries"]
    (ok if mine == theirs else fail)(
        f"{victim} and {partner} are identical: {len(mine)} entries each (tombstones included)"
        if mine == theirs else f"{victim} and {partner} differ ({len(mine)} vs {len(theirs)} entries)")


if __name__ == "__main__":
    main()
