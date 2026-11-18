"""Spec demo: asynchronous processing. Many requests are submitted at once; the API
answers immediately, and the matching service works through them via RabbitMQ.

    python scripts/demo_async.py                          # 40 requests, 30 responders
    python scripts/demo_async.py --requests 100 --resources 80

Because there are fewer responders than requests, some requests must end UNMATCHED,
and no responder may be given to two requests. Both are checked at the end.
Needs the stack running (Compose or Kubernetes + scripts/k8s_forward.py).
"""

import argparse
import sys
import time

from _demo_app import (
    api, check, check_api, describe_queue, get_request, morph_banner, ok, retire_leftovers, step, submit_batch,
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--requests", type=int, default=40)
    p.add_argument("--resources", type=int, default=30)
    p.add_argument("--timeout", type=float, default=120.0, help="seconds to wait for the queue to drain")
    args = p.parse_args()
    check_api()

    morph_banner("Delta Command: many emergencies at once")
    retire_leftovers()

    step(f"Registering {args.resources} water responders, then sending {args.requests} requests at once")
    resources, request_ids, took = submit_batch(args.resources, args.requests)
    ok(f"{args.requests} requests accepted (201) in {took:.2f}s: the API does not wait for matching")
    ok(describe_queue() + "  <- the backlog the matching service is now working through")

    step("Watching the queue drain")
    start = time.monotonic()
    statuses: dict[str, str] = {}
    while time.monotonic() - start < args.timeout:
        statuses = {i: get_request(i)["status"] for i in request_ids}
        pending = sum(s == "PENDING" for s in statuses.values())
        print(f"   {time.monotonic() - start:5.1f}s  pending {pending:>4}  matched {sum(s == 'MATCHED' for s in statuses.values()):>4}"
              f"  unmatched {sum(s == 'UNMATCHED' for s in statuses.values()):>4}   {describe_queue()}")
        if pending == 0:
            break
        time.sleep(1)
    drained = time.monotonic() - start

    step("Checking the outcome")
    requests = [get_request(i) for i in request_ids]
    matched = [r for r in requests if r["status"] == "MATCHED"]
    expected = min(args.requests, args.resources)
    good = check(all(r["status"] != "PENDING" for r in requests),
                 f"every request was processed ({args.requests / drained:.0f} requests/s overall)")
    good &= check(len(matched) == expected, f"{len(matched)} matched, {len(requests) - len(matched)} unmatched (expected {expected} matched)")
    assigned = [r["assigned_resource_id"] for r in matched]
    good &= check(len(set(assigned)) == len(assigned), "no responder was assigned to two requests")
    mine = {r["id"] for r in resources}
    in_use = [r for r in api.get("/resources", params={"status": "ASSIGNED"}).json() if r["id"] in mine]
    good &= check(len(in_use) == len(matched), f"{len(in_use)} responders are ASSIGNED, one per matched request")

    print()
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
