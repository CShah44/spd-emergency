"""Run one load scenario end to end and print its result row.

    python loadtest/run.py --rate 20 --duration 60                 # steady load, nothing broken
    python loadtest/run.py --rate 10 --duration 60 --scenario kill-matcher --record
                                                                   # break something 10 s into the load (see scripts/chaos.py)

What it does:
  1. checks the stack is up and clean (no old PENDING requests: the sweeper would match
     them during the run and their age would ruin the latency numbers)
  2. registers enough responders for every request to find one
  3. sends POST /requests at --rate per second for --duration seconds (Locust)
  4. waits for every accepted request to be answered, and checks none was lost or
     double-assigned (verify.py)
  5. reads p50 / p95 / p99 of spd_match_e2e_seconds (request created -> MATCHED or
     UNMATCHED, measured inside the matcher) from Prometheus

Works on Docker Compose and Kubernetes alike: both expose the API on :8080 and Prometheus
on :9090. For a clean start use `python scripts/k8s_up.py --fresh --seed` (or
`docker compose down -v` then `docker compose up -d`).
"""

import argparse
import csv
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import chaos  # noqa: E402
from loadtest import verify  # noqa: E402
from loadtest.common import CITIES, TYPES, random_point  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "loadtest" / "results"
RESULTS_MD = ROOT / "loadtest" / "RESULTS.md"
PER_USER_RATE = 5  # requests per second each simulated user sends (see locustfile.py)
SCRAPE_WAIT_S = 8  # Prometheus scrapes every 5 s: wait for the last numbers to arrive
TABLE_HEADER = (
    "| scenario | rate (req/s) | duration | accepted | refused | matched | unmatched | p50 | p95 | p99 | lost | double-assigned | recovery | date |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
)


class Abort(Exception):
    pass


# -- before ---------------------------------------------------------------------------------------

def preflight(api: str, prometheus: str, allow_dirty: bool) -> None:
    try:
        summary = httpx.get(f"{api}/requests/summary", timeout=5).json()
    except httpx.HTTPError as exc:
        raise Abort(f"the API at {api} is not answering ({exc!r}). Is the stack running?") from exc
    if summary["PENDING"] and not allow_dirty:
        raise Abort(
            f"{summary['PENDING']} requests from an earlier run are still PENDING. The sweeper would match them "
            "during this run and their age would distort the latency. Start from a clean stack "
            "(scripts/k8s_up.py --fresh, or docker compose down -v), or pass --allow-dirty."
        )
    try:
        httpx.get(f"{prometheus}/-/ready", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        raise Abort(f"Prometheus at {prometheus} is not answering ({exc!r}): the latency numbers come from there") from exc


def seed_responders(api: str, count: int, rng: random.Random) -> None:
    def register(n: int) -> None:
        body = {"type": TYPES[n % len(TYPES)], "name": f"loadtest unit {n}", "location": random_point(rng, 0.1),
                "capacity": 100, "availability": "24x7"}
        httpx.post(f"{api}/resources", json=body, timeout=30).raise_for_status()

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(register, range(count)))


# -- the load ---------------------------------------------------------------------------------------------

def run_locust(api: str, rate: float, duration: int, ids_file: Path, stats_prefix: Path) -> dict:
    users = max(1, math.ceil(rate / PER_USER_RATE))
    env = {**os.environ, "IDS_FILE": str(ids_file), "PER_USER_RATE": str(rate / users)}
    command = [
        sys.executable, "-m", "locust", "-f", str(ROOT / "loadtest" / "locustfile.py"), "--headless",
        "-u", str(users), "-r", str(users), "-t", f"{duration}s", "--host", api, "--csv", str(stats_prefix),
        "--only-summary",
    ]
    log = open(f"{stats_prefix}.log", "w")
    subprocess.run(command, env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False)
    return read_post_stats(f"{stats_prefix}_stats.csv")


def read_post_stats(path: str) -> dict:
    """What the clients saw for POST /requests: how many, how many refused, how long the call took."""
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if row["Name"] == "POST /requests":
                    return {"sent": int(row["Request Count"]), "refused": int(row["Failure Count"]),
                            "post_p95_ms": float(row["95%"])}
    except FileNotFoundError:
        pass
    return {"sent": 0, "refused": 0, "post_p95_ms": None}


# -- after -----------------------------------------------------------------------------------------------------

def prometheus_value(prometheus: str, query: str) -> float | None:
    try:
        result = httpx.get(f"{prometheus}/api/v1/query", params={"query": query}, timeout=10).json()["data"]["result"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if math.isnan(value) else value


def match_latency(prometheus: str, window_s: int) -> dict:
    """p50 / p95 / p99 of spd_match_e2e_seconds over the last `window_s` seconds, plus how many were counted."""
    buckets = f"sum by (le) (increase(spd_match_e2e_seconds_bucket[{window_s}s]))"
    out = {name: prometheus_value(prometheus, f"histogram_quantile({q}, {buckets})")
           for name, q in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))}
    out["observed"] = prometheus_value(prometheus, f"sum(increase(spd_match_e2e_seconds_count[{window_s}s]))")
    return out


LATENCY_TOP_BUCKET_S = 120  # the biggest bucket of spd_match_e2e_seconds: anything slower is only known to be slower


def seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= LATENCY_TOP_BUCKET_S:
        return f">{LATENCY_TOP_BUCKET_S} s"
    return f"{value * 1000:.0f} ms" if value < 1 else f"{value:.2f} s"


def result_row(label: str, rate: float, duration: int, accepted: int, refused: int, report: verify.Report,
               latency: dict, recovery: str = "-") -> str:
    cells = [label, f"{rate:g}", f"{duration} s", str(accepted), str(refused), str(report.matched),
             str(report.unmatched), seconds(latency["p50"]), seconds(latency["p95"]), seconds(latency["p99"]),
             str(report.lost), str(len(report.double_assigned) + len(report.inconsistent) + len(report.leaked)),
             recovery, datetime.now(timezone.utc).strftime("%Y-%m-%d")]
    return "| " + " | ".join(cells) + " |"


def record(row: str) -> None:
    if not RESULTS_MD.exists():
        with open(RESULTS_MD, "w", newline="\n") as f:
            f.write("# Load test results\n\nOne row per run of `python loadtest/run.py --record <name>`.\n\n" + TABLE_HEADER)
    with open(RESULTS_MD, "a", newline="\n") as f:  # plain newlines: the repo is LF everywhere
        f.write(row + "\n")


# -- main --------------------------------------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rate", type=float, default=20, help="requests per second (default 20)")
    p.add_argument("--duration", type=int, default=30, help="seconds of load (default 30)")
    p.add_argument("--responders", type=int, help="responders to register (default: 1.5 per request, so all can match)")
    p.add_argument("--skip-seed", action="store_true", help="don't register responders (use what is there)")
    p.add_argument("--scenario", choices=list(chaos.SCENARIOS), help="break something during the load (scripts/chaos.py)")
    p.add_argument("--chaos-at", type=float, default=10, help="seconds into the load when the scenario starts (default 10)")
    p.add_argument("--drain", type=float, default=900, help="most seconds to wait for answers after the load stops (default 900)")
    p.add_argument("--stall", type=float, default=150, help="give up on the unanswered ones when no answer has arrived for this "
                   "long (default 150: longer than the sweeper needs to rescue requests that were saved but never queued)")
    p.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8080"))
    p.add_argument("--prometheus", default=os.environ.get("PROMETHEUS_URL", "http://localhost:9090"))
    p.add_argument("--label", help="name of the scenario in the result row (default: the scenario, or 'steady')")
    p.add_argument("--record", action="store_true", help="append the row to loadtest/RESULTS.md")
    p.add_argument("--allow-dirty", action="store_true", help="run even if old PENDING requests exist")
    args = p.parse_args()
    args.label = args.label or args.scenario or "steady"

    try:
        preflight(args.api, args.prometheus, args.allow_dirty)
        rng = random.Random()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        RESULTS_DIR.mkdir(exist_ok=True)
        ids_file, prefix = RESULTS_DIR / f"{args.label}-{stamp}-ids.txt", RESULTS_DIR / f"{args.label}-{stamp}"

        if not args.skip_seed:
            responders = args.responders or math.ceil(args.rate * args.duration * 1.5)
            print(f">> registering {responders} responders in {len(CITIES)} cities", flush=True)
            seed_responders(args.api, responders, rng)

        print(f">> sending {args.rate:g} requests/s for {args.duration} s", flush=True)
        started = time.time()
        chaos_result: dict = {}
        chaos_thread = None
        if args.scenario:
            def break_something() -> None:
                chaos_result["outcome"] = chaos.run(args.scenario, chaos.detect_platform())
            chaos_thread = threading.Timer(args.chaos_at, break_something)
            chaos_thread.start()
        post = run_locust(args.api, args.rate, args.duration, ids_file, prefix)
        if chaos_thread:
            chaos_thread.join(timeout=300)
        ids = [line.strip() for line in open(ids_file) if line.strip()] if ids_file.exists() else []
        print(f">> load finished: {post['sent']} sent, {post['refused']} refused; checking every accepted request", flush=True)

        report = verify.verify(args.api, ids, args.drain, args.stall)
        time.sleep(SCRAPE_WAIT_S)
        latency = match_latency(args.prometheus, int(time.time() - started) + 5)
    except Abort as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 2

    print("\n" + verify.summary(report))
    print(f"\nmatcher answers counted by Prometheus: about {latency['observed']:.0f} (an estimate: Prometheus extrapolates at the window edges)"
          if latency["observed"] is not None else "\nmatcher count: n/a")
    print(f"POST /requests as the clients saw it: p95 {post['post_p95_ms']} ms, {post['refused']} refused")
    recovery = "-"
    if "outcome" in chaos_result:
        after = chaos_result["outcome"].recovered_after_s
        recovery = f"{after:g} s" if after is not None else "did not recover"
        print(f"chaos: {chaos_result['outcome'].scenario} broke {chaos_result['outcome'].what}; recovery {recovery}")
    row = result_row(args.label, args.rate, args.duration, len(ids), post["refused"], report, latency, recovery)
    print("\n" + TABLE_HEADER + row)
    json.dump({"row": row, "post": post, "latency": latency, "lost": report.lost, "ok": report.ok},
              open(f"{prefix}.json", "w"), indent=2)
    if args.record:
        record(row)
        print(f"\nrecorded in {RESULTS_MD.relative_to(ROOT)}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
