"""Geohash cells, for geo-sharding (docs/GEO_SHARDING.md). Standard geohash, no dependencies.

A geohash names a rectangle of the Earth with a short string; the longer the string, the
smaller the rectangle. We use precision 3: a cell is 1.40625 degrees on each side, about
156 km tall (and up to 156 km wide, narrower towards the poles). Cells that touch have
names that differ, so neighbours() lists the 8 around a cell.

A record's id carries its cell: 'res-<cell>-<hex>' and 'req-<cell>-<hex>', so which cell
(and therefore which shard) a record belongs to can be read from its id alone. Ids from
before geo-sharding ('res-<16 hex>') have no cell: cell_of_id() answers None for them.
"""

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"  # geohash's alphabet: no a, i, l or o
_DECODE = {c: i for i, c in enumerate(_BASE32)}

PRECISION = 3


def encode(lat: float, lon: float, precision: int = PRECISION) -> str:
    """The cell (a geohash of `precision` characters) containing this point."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"not a valid location: lat={lat}, lon={lon}")
    lat_range, lon_range = [-90.0, 90.0], [-180.0, 180.0]
    chars, bits, value, use_lon = [], 0, 0, True  # the bits alternate: longitude first
    while len(chars) < precision:
        low_high, point = (lon_range, lon) if use_lon else (lat_range, lat)
        middle = (low_high[0] + low_high[1]) / 2
        if point >= middle:
            value = (value << 1) | 1
            low_high[0] = middle
        else:
            value <<= 1
            low_high[1] = middle
        use_lon = not use_lon
        bits += 1
        if bits == 5:
            chars.append(_BASE32[value])
            bits, value = 0, 0
    return "".join(chars)


def decode(cell: str) -> tuple[float, float, float, float]:
    """(lat_min, lat_max, lon_min, lon_max) of a cell."""
    if not is_cell(cell):
        raise ValueError(f"not a geohash cell: {cell!r}")
    lat_range, lon_range = [-90.0, 90.0], [-180.0, 180.0]
    use_lon = True
    for char in cell:
        for shift in (4, 3, 2, 1, 0):
            low_high = lon_range if use_lon else lat_range
            middle = (low_high[0] + low_high[1]) / 2
            low_high[0 if (_DECODE[char] >> shift) & 1 else 1] = middle
            use_lon = not use_lon
    return lat_range[0], lat_range[1], lon_range[0], lon_range[1]


def is_cell(text: str) -> bool:
    return bool(text) and all(c in _DECODE for c in text)


def neighbours(cell: str) -> list[str]:
    """The cells touching this one (8, or 5 at the very top and bottom of the map).
    Longitude wraps around the date line."""
    lat_min, lat_max, lon_min, lon_max = decode(cell)
    lat_step, lon_step = lat_max - lat_min, lon_max - lon_min
    lat_mid, lon_mid = (lat_min + lat_max) / 2, (lon_min + lon_max) / 2
    found = []
    for d_lat in (-1, 0, 1):
        for d_lon in (-1, 0, 1):
            if d_lat == 0 and d_lon == 0:
                continue
            lat = lat_mid + d_lat * lat_step
            if not -90 <= lat <= 90:
                continue  # off the top or bottom of the map
            lon = (lon_mid + d_lon * lon_step + 180) % 360 - 180  # wrap past +-180
            found.append(encode(lat, lon, len(cell)))
    return sorted(set(found))


def cells_around(lat: float, lon: float, precision: int = PRECISION) -> list[str]:
    """The point's own cell plus its neighbours: where to look for anything near it.
    A cell is at least 50 km wide up to about 70 degrees of latitude, so this covers a
    50 km search radius there."""
    cell = encode(lat, lon, precision)
    return [cell] + neighbours(cell)


def cell_of_id(record_id: str) -> str | None:
    """'res-u4p-8b1e...' -> 'u4p'. None for an id without a cell (an old-style id)."""
    parts = record_id.split("-")
    if len(parts) == 3 and len(parts[1]) == PRECISION and is_cell(parts[1]):
        return parts[1]
    return None
