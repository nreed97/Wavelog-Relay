"""Maidenhead locator conversion and great-circle math."""

from __future__ import annotations

import math
import re

EARTH_RADIUS_KM = 6371.0

_GRID_RE = re.compile(r"^[A-R]{2}(?:[0-9]{2}(?:[A-X]{2}(?:[0-9]{2})?)?)?$")


def is_valid_grid(grid: str | None) -> bool:
    return bool(grid) and bool(_GRID_RE.match(grid.strip().upper()))


def grid_to_latlon(grid: str) -> tuple[float, float]:
    """Return the (lat, lon) centre of a 2, 4, 6 or 8 character Maidenhead locator."""
    g = grid.strip().upper()
    if not is_valid_grid(g):
        raise ValueError(f"invalid Maidenhead locator: {grid!r}")

    lon = (ord(g[0]) - ord("A")) * 20.0 - 180.0
    lat = (ord(g[1]) - ord("A")) * 10.0 - 90.0
    lon_size, lat_size = 20.0, 10.0

    if len(g) >= 4:
        lon += int(g[2]) * 2.0
        lat += int(g[3]) * 1.0
        lon_size, lat_size = 2.0, 1.0
    if len(g) >= 6:
        lon += (ord(g[4]) - ord("A")) * (5.0 / 60.0)
        lat += (ord(g[5]) - ord("A")) * (2.5 / 60.0)
        lon_size, lat_size = 5.0 / 60.0, 2.5 / 60.0
    if len(g) >= 8:
        lon += int(g[6]) * (0.5 / 60.0)
        lat += int(g[7]) * (0.25 / 60.0)
        lon_size, lat_size = 0.5 / 60.0, 0.25 / 60.0

    return lat + lat_size / 2.0, lon + lon_size / 2.0


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle (short path) bearing in degrees 0..360 from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))
