"""Command-line client for the SPD Emergency API (the spec's "1 or more clients").

    python client/cli.py request water 12.9716 77.5946 --quantity 20 --details "2 kids, flooded"
    python client/cli.py get-request req-3f9c2a1b7d4e4f0a
    python client/cli.py register water "City Water Tanker 4" 12.99 77.60 --capacity 500
    python client/cli.py resources --type water --status AVAILABLE
    python client/cli.py match req-3f9c2a1b7d4e4f0a
    python client/cli.py set-request-status req-3f9c2a1b7d4e4f0a IN_PROGRESS
    python client/cli.py set-resource-status res-8b1e0c55a2f94d17 OFFLINE

The answer is printed as JSON on stdout (pipe it to jq); the banner and errors go to stderr.
Talks to the request service at --url, or $API_URL, default http://localhost:8080.
"""

import argparse
import json
import os
import sys

import httpx

TYPES = ["water", "food", "medicine", "transport", "shelter"]
REQUEST_STATUSES = ["PENDING", "MATCHED", "IN_PROGRESS", "FULFILLED", "UNMATCHED", "CANCELLED"]
RESOURCE_STATUSES = ["AVAILABLE", "ASSIGNED", "OFFLINE"]

BANNER = "  S.P.D. EMERGENCY!  ::  Delta Command client"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("API_URL", "http://localhost:8080"), help="request service URL")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("request", help="report an emergency: what is needed, and where")
    s.add_argument("type", choices=TYPES)
    s.add_argument("lat", type=float)
    s.add_argument("lon", type=float)
    s.add_argument("--quantity", type=int)
    s.add_argument("--details")
    s.add_argument("--contact")

    s = sub.add_parser("get-request", help="look up a request")
    s.add_argument("id")

    s = sub.add_parser("set-request-status", help="IN_PROGRESS, FULFILLED, CANCELLED (or PENDING to retry)")
    s.add_argument("id")
    s.add_argument("status", choices=REQUEST_STATUSES)

    s = sub.add_parser("match", help="match a request right now instead of waiting for the queue")
    s.add_argument("id")

    s = sub.add_parser("register", help="register a responder: something or someone that can help")
    s.add_argument("type", choices=TYPES)
    s.add_argument("name")
    s.add_argument("lat", type=float)
    s.add_argument("lon", type=float)
    s.add_argument("--capacity", type=int)
    s.add_argument("--availability")
    s.add_argument("--contact")

    s = sub.add_parser("resources", help="list responders")
    s.add_argument("--type", choices=TYPES)
    s.add_argument("--status", choices=RESOURCE_STATUSES)

    s = sub.add_parser("set-resource-status", help="AVAILABLE or OFFLINE")
    s.add_argument("id")
    s.add_argument("status", choices=RESOURCE_STATUSES)
    return p


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def build_call(args: argparse.Namespace) -> tuple[str, str, dict]:
    """Turn the parsed command line into (HTTP method, path, options for httpx)."""
    c = args.command
    if c == "request":
        body = _drop_none({"type": args.type, "location": {"lat": args.lat, "lon": args.lon},
                           "quantity": args.quantity, "details": args.details, "contact": args.contact})
        return "POST", "/requests", {"json": body}
    if c == "get-request":
        return "GET", f"/requests/{args.id}", {}
    if c == "set-request-status":
        return "PUT", f"/requests/{args.id}/status", {"json": {"status": args.status}}
    if c == "match":
        return "POST", f"/match/{args.id}", {}
    if c == "register":
        body = _drop_none({"type": args.type, "name": args.name, "location": {"lat": args.lat, "lon": args.lon},
                           "capacity": args.capacity, "availability": args.availability, "contact": args.contact})
        return "POST", "/resources", {"json": body}
    if c == "resources":
        return "GET", "/resources", {"params": _drop_none({"type": args.type, "status": args.status})}
    return "PUT", f"/resources/{args.id}/status", {"json": {"status": args.status}}  # set-resource-status


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.stderr.isatty():
        print(BANNER, file=sys.stderr)
    method, path, options = build_call(args)
    try:
        response = httpx.request(method, args.url + path, timeout=20.0, **options)
    except httpx.TransportError as exc:
        print(f"[!!] cannot reach {args.url}: {exc!r}", file=sys.stderr)
        return 2
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if response.is_success:
        print(json.dumps(body, indent=2))
        return 0
    print(f"[!!] {response.status_code}: {json.dumps(body)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
