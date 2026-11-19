"""Spec demos: request creation, resource registration, resource matching.

    python scripts/demo_matching.py
    python scripts/demo_matching.py --step      # wait for Enter between stages

The story, in an isolated spot (Port Blair) so seeded data never interferes:
  1. register responders: two water ones (near and far) and a food one (closest, wrong type)
  2. create a water request and read it back
  3. the matching service claims the NEAREST water responder, by itself, through RabbitMQ
  4. a second request gets the remaining one; a third has nobody left (UNMATCHED)
  5. finishing the first request frees its responder, and POST /match lets the third one in
"""

import argparse
import sys

from _demo_app import (
    api, check, check_api, create_request, get_request, morph_banner, ok, pause_for_audience, register,
    retire_leftovers, settle, spot, step,
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--step", action="store_true", help="wait for Enter between stages")
    args = p.parse_args()
    check_api()
    good = True

    morph_banner("Delta Command: emergency request and matching")
    retire_leftovers()

    step("1. Resource registration: three responders sign up")
    near = register("water", "Water tanker NEAR (about 3 km away)", spot(0.03), capacity=500)
    far = register("water", "Water tanker FAR (about 24 km away)", spot(0.22), capacity=500)
    food = register("food", "Community kitchen (right next door, but it is food)", spot(0.001))
    for r in (near, far, food):
        ok(f"{r['id']}  {r['type']:<6} {r['status']:<9} {r['name']}")
    pause_for_audience(args.step)

    step("2. Request creation: someone reports that they need water, then we read it back")
    first = create_request("water", spot(), quantity=20, details="2 kids, ground floor flooded")
    ok(f"POST /requests -> 201  {first['id']}  status {first['status']}  version {first['version']}")
    ok(f"GET /requests/{first['id']} -> status {get_request(first['id'])['status']} (it may already have moved on)")
    pause_for_audience(args.step)

    step("3. Matching: the request travels through RabbitMQ to the matching service")
    first = settle(first["id"])
    ok(f"status {first['status']}, assigned to {first['assigned_resource_id']}")
    good &= check(first["assigned_resource_id"] == near["id"], "the NEAREST water responder was chosen (not the far one, not the kitchen)")
    again = api.post(f"/match/{first['id']}").json()
    good &= check(again["outcome"] == "skipped", "POST /match on an already matched request changes nothing (outcome: skipped)")
    pause_for_audience(args.step)

    step("4. Only free responders are used: the second request gets the far tanker, the third finds nobody")
    second = settle(create_request("water", spot())["id"])
    good &= check(second["assigned_resource_id"] == far["id"], f"second request -> {second['assigned_resource_id']} (the far tanker)")
    third = settle(create_request("water", spot())["id"])
    good &= check(third["status"] == "UNMATCHED", f"third request -> {third['status']}: every water responder is busy")
    pause_for_audience(args.step)

    step("5. Finishing a request frees its responder, and the waiting request can be matched")
    for status in ("IN_PROGRESS", "FULFILLED"):
        code = api.put(f"/requests/{first['id']}/status", json={"status": status}).status_code
        ok(f"PUT /requests/{first['id']}/status {status} -> {code}")
    freed = api.get("/resources", params={"status": "AVAILABLE", "type": "water"}).json()
    good &= check(near["id"] in [r["id"] for r in freed], f"{near['id']} is AVAILABLE again")
    retry = api.post(f"/match/{third['id']}").json()  # direct call, no queue involved
    good &= check(retry["outcome"] == "matched" and retry["request"]["assigned_resource_id"] == near["id"],
                  f"POST /match/{third['id']} -> {retry['outcome']}, assigned to {retry['request']['assigned_resource_id']}")

    print()
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
