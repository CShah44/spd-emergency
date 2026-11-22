"""Geohash cells for geo-sharding."""

import random

import pytest

from common import geohash
from matching_service.app.geo import haversine_km


@pytest.mark.parametrize("lat, lon, precision, expected", [
    (57.64911, 10.40744, 11, "u4pruydqqvj"),  # the example every geohash description uses
    (42.605, -5.603, 5, "ezs42"),
    (12.9716, 77.5946, 3, "tdr"),  # Bengaluru
    (0.0, 0.0, 3, "s00"),
])
def test_known_cells(lat, lon, precision, expected):
    assert geohash.encode(lat, lon, precision) == expected


def test_a_cell_is_about_156_km_tall_at_precision_3():
    lat_min, lat_max, lon_min, lon_max = geohash.decode("tdr")

    assert lat_max - lat_min == pytest.approx(1.40625) and lon_max - lon_min == pytest.approx(1.40625)
    assert haversine_km({"lat": lat_min, "lon": 77}, {"lat": lat_max, "lon": 77}) == pytest.approx(156, abs=1)


def test_a_point_is_inside_its_own_cell():
    rng = random.Random(1)
    for _ in range(200):
        lat, lon = rng.uniform(-90, 90), rng.uniform(-180, 180)
        lat_min, lat_max, lon_min, lon_max = geohash.decode(geohash.encode(lat, lon))
        assert lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


@pytest.mark.parametrize("lat, lon", [(91, 0), (-91, 0), (0, 181), (0, -181)])
def test_encode_rejects_impossible_locations(lat, lon):
    with pytest.raises(ValueError):
        geohash.encode(lat, lon)


def test_decode_rejects_text_that_is_not_a_cell():
    with pytest.raises(ValueError):
        geohash.decode("tda!")
    with pytest.raises(ValueError):
        geohash.decode("")


# -- neighbours -----------------------------------------------------------------------------------

def test_a_normal_cell_has_eight_neighbours():
    around = geohash.neighbours("tdr")

    assert len(around) == 8 and "tdr" not in around and len(set(around)) == 8


def test_neighbouring_is_mutual():
    for neighbour in geohash.neighbours("tdr"):
        assert "tdr" in geohash.neighbours(neighbour)


def test_neighbours_wrap_around_the_date_line():
    east_edge = geohash.encode(10, 179.9)
    west_edge = geohash.encode(10, -179.9)

    assert west_edge in geohash.neighbours(east_edge)


def test_a_cell_at_the_pole_has_fewer_neighbours():
    assert len(geohash.neighbours(geohash.encode(89.9, 10))) == 5


@pytest.mark.parametrize("lat, lon", [(12.97, 77.59), (0.0, 0.0), (45.0, 7.0), (60.0, 24.0), (-33.9, 18.4), (10.0, 179.8)])
def test_the_nine_cells_cover_every_point_within_50_km(lat, lon):
    """Why the matcher can look in the request's cell plus its 8 neighbours and nowhere else."""
    covered = set(geohash.cells_around(lat, lon))
    rng = random.Random(7)
    origin = {"lat": lat, "lon": lon}
    for _ in range(500):
        bearing_km, angle = 50 * rng.random() ** 0.5, rng.uniform(0, 360)
        from math import cos, radians, sin

        dlat = bearing_km * cos(radians(angle)) / 111.195
        dlon = bearing_km * sin(radians(angle)) / (111.195 * cos(radians(lat)))
        point = {"lat": lat + dlat, "lon": (lon + dlon + 180) % 360 - 180}
        if haversine_km(origin, point) <= 50:
            assert geohash.encode(point["lat"], point["lon"]) in covered


def test_cells_around_starts_with_the_points_own_cell():
    around = geohash.cells_around(12.9716, 77.5946)

    assert around[0] == "tdr" and len(around) == 9


# -- ids that carry a cell -----------------------------------------------------------------------------

@pytest.mark.parametrize("record_id, cell", [
    ("res-tdr-8b1e3f9c0a7d4e21", "tdr"),
    ("req-u4p-0011223344556677", "u4p"),
    ("res-8b1e3f9c0a7d4e21", None),  # an id from before geo-sharding
    ("req-3f9c1d7a5b2e8f40", None),
    ("res-tdnn-8b1e", None),  # wrong cell length
    ("res-tda-8b1e", None),  # 'a' is not in the geohash alphabet
    ("something", None),
    ("", None),
])
def test_cell_of_id(record_id, cell):
    assert geohash.cell_of_id(record_id) == cell
