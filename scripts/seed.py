"""Register sample responders around five cities, so there is something to match.

    python scripts/seed.py              # 2 responders of every type in every city (50 in all)
    python scripts/seed.py --per-city 1

Running it twice registers a second batch. Needs the stack running.
"""

import argparse
import random

from _demo_app import CITIES, api, check_api, jitter, morph_banner, ok, register, step

TYPES = ["water", "food", "medicine", "transport", "shelter"]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--per-city", type=int, default=2, help="responders of each type in each city (default 2)")
    args = p.parse_args()
    check_api()

    morph_banner("Delta Command: seeding responders")
    step(f"Registering {args.per_city} responder(s) of each of {len(TYPES)} types in {len(CITIES)} cities")
    for city, center in CITIES.items():
        for type_ in TYPES:
            for n in range(1, args.per_city + 1):
                register(type_, f"{city} {type_} unit {n}", jitter(center), capacity=random.randint(10, 500),
                         availability="24x7")
        ok(f"{city}: {len(TYPES) * args.per_city} responders")

    available = api.get("/resources", params={"status": "AVAILABLE"}).json()
    ok(f"{len(available)} responders AVAILABLE in total")


if __name__ == "__main__":
    main()
