"""What the load test and the verifier share: where responders and requests are put."""

import random

# The same five cities scripts/seed.py uses.
CITIES = {
    "Bengaluru": (12.9716, 77.5946),
    "Mumbai": (19.0760, 72.8777),
    "Chennai": (13.0827, 80.2707),
    "Kolkata": (22.5726, 88.3639),
    "Ahmedabad": (23.0225, 72.5714),
}
TYPES = ["water", "food", "medicine", "transport", "shelter"]


def random_point(rng: random.Random, spread_deg: float = 0.2) -> dict[str, float]:
    """A point near one of the cities (0.2 degrees is about 22 km, well inside the 50 km match limit)."""
    lat, lon = rng.choice(list(CITIES.values()))
    return {"lat": round(lat + rng.uniform(-spread_deg, spread_deg), 4),
            "lon": round(lon + rng.uniform(-spread_deg, spread_deg), 4)}
