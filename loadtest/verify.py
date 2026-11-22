"""After a load run: did every accepted request get an answer, and was any responder given twice?

    python loadtest/verify.py loadtest/results/steady-ids.txt          # waits up to 60 s for answers
    python loadtest/verify.py ids.txt --wait 120 --api http://localhost:8080

"Lost requests" are ids that end up in none of the acceptable states:
  * still PENDING after the wait (accepted, but nothing ever matched it)
  * not found (the API said 201 but the record is gone)
  * unreadable (the API kept failing for it)
MATCHED and UNMATCHED are both fine: UNMATCHED means nobody was in range, which is an
answer. The target is 0 lost, 0 double-assigned.

The other checks look at the responders:
  * double-assigned: one responder is the assigned_resource_id of two or more requests
  * inconsistent: a MATCHED request whose responder is not ASSIGNED back to that request
  * leaked: a responder ASSIGNED to one of our requests that does not point back at it
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Callable

import httpx

ANSWERED = {"MATCHED", "UNMATCHED"}


@dataclass
class Report:
    total: int = 0
    matched: int = 0
    unmatched: int = 0
    pending: int = 0  # still waiting after the wait
    missing: int = 0  # 404
    unreadable: int = 0  # kept failing
    other: int = 0  # any other status (not something a load run produces)
    double_assigned: list[str] = field(default_factory=list)
    inconsistent: list[str] = field(default_factory=list)
    leaked: list[str] = field(default_factory=list)

    @property
    def lost(self) -> int:
        return self.pending + self.missing + self.unreadable

    @property
    def ok(self) -> bool:
        return not (self.lost or self.double_assigned or self.inconsistent or self.leaked)


def classify(ids: list[str], requests: dict[str, dict | str], resources: list[dict]) -> Report:
    """`requests` maps each id to its record, or to "missing" / "unreadable"."""
    report = Report(total=len(ids))
    by_resource: dict[str, list[str]] = defaultdict(list)
    resource_by_id = {r["id"]: r for r in resources}

    for request_id in ids:
        record = requests.get(request_id, "unreadable")
        if record == "missing":
            report.missing += 1
        elif record == "unreadable":
            report.unreadable += 1
        elif record["status"] == "MATCHED":
            report.matched += 1
            by_resource[record["assigned_resource_id"]].append(request_id)
        elif record["status"] == "UNMATCHED":
            report.unmatched += 1
        elif record["status"] == "PENDING":
            report.pending += 1
        else:
            report.other += 1

    for resource_id, claimants in by_resource.items():
        if len(claimants) > 1:
            report.double_assigned.append(f"{resource_id} <- {', '.join(sorted(claimants))}")
        resource = resource_by_id.get(resource_id)
        for request_id in claimants:
            if resource is None or resource["status"] != "ASSIGNED" or resource.get("assigned_request_id") != request_id:
                report.inconsistent.append(f"{request_id} -> {resource_id}")

    ours = set(ids)
    for resource in resources:
        holder = resource.get("assigned_request_id")
        if resource["status"] == "ASSIGNED" and holder in ours:
            record = requests.get(holder)
            if not isinstance(record, dict) or record.get("assigned_resource_id") != resource["id"]:
                report.leaked.append(f"{resource['id']} held for {holder}")
    return report


def fetch_answers(
    ids: list[str], get: Callable[[str], dict | str], wait_s: float, interval_s: float = 1.0, workers: int = 16,
) -> dict[str, dict | str]:
    """Ask for every id, and keep asking for the ones not answered yet until `wait_s` runs out."""
    results: dict[str, dict | str] = {}
    todo = list(ids)
    deadline = time.monotonic() + wait_s
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while todo:
            for request_id, record in zip(todo, pool.map(get, todo)):
                results[request_id] = record
            todo = [i for i in todo if not (isinstance(results[i], dict) and results[i]["status"] in ANSWERED)
                    and results[i] != "missing"]
            if not todo or time.monotonic() >= deadline:
                break
            time.sleep(interval_s)
    return results


def http_getter(client: httpx.Client) -> Callable[[str], dict | str]:
    def get(request_id: str) -> dict | str:
        try:
            response = client.get(f"/requests/{request_id}")
        except httpx.HTTPError:
            return "unreadable"
        if response.status_code == 404:
            return "missing"
        return response.json() if response.status_code == 200 else "unreadable"
    return get


def verify(api: str, ids: list[str], wait_s: float) -> Report:
    with httpx.Client(base_url=api, timeout=15.0, limits=httpx.Limits(max_connections=32)) as client:
        answers = fetch_answers(ids, http_getter(client), wait_s)
        resources = client.get("/resources").json()
    return classify(ids, answers, resources)


def summary(report: Report) -> str:
    lines = [
        f"requests checked : {report.total}",
        f"  MATCHED        : {report.matched}",
        f"  UNMATCHED      : {report.unmatched}",
        f"  lost           : {report.lost}  (still PENDING {report.pending}, missing {report.missing}, unreadable {report.unreadable})",
        f"double-assigned  : {len(report.double_assigned)}",
        f"inconsistent     : {len(report.inconsistent)}",
        f"leaked claims    : {len(report.leaked)}",
    ]
    for label, found in (("double-assigned", report.double_assigned), ("inconsistent", report.inconsistent),
                         ("leaked", report.leaked)):
        lines += [f"  {label}: {item}" for item in found[:5]]
    lines.append("RESULT: " + ("OK, nothing lost" if report.ok else "FAILED"))
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ids_file", help="the ids the load run saved, one per line")
    p.add_argument("--api", default="http://localhost:8080")
    p.add_argument("--wait", type=float, default=60.0, help="seconds to wait for answers (default 60)")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    args = p.parse_args()
    ids = [line.strip() for line in open(args.ids_file) if line.strip()]
    report = verify(args.api, ids, args.wait)
    print(json.dumps({**asdict(report), "lost": report.lost, "ok": report.ok}, indent=2) if args.json else summary(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
