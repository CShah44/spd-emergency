"""Distance between two points on the Earth. DESIGN.md section 4, step 2."""

from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_KM = 6371.0088  # mean radius


def haversine_km(a: dict[str, float], b: dict[str, float]) -> float:
    """Straight-line ("as the crow flies") distance in km between two
    {"lat": ..., "lon": ...} points, following the curve of the Earth."""
    lat1, lon1, lat2, lon2 = map(radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(h))
