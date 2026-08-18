"""Great-circle helpers for locating strokes relative to a home point."""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088
KM_PER_MILE = 1.609344

_COMPASS_16 = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two decimal-degree points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial true bearing in degrees (0-360) from point 1 toward point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def compass_point(bearing_deg: float) -> str:
    """16-point compass abbreviation for a true bearing."""
    return _COMPASS_16[int((bearing_deg % 360.0) / 22.5 + 0.5) % 16]


def km_to_miles(km: float) -> float:
    return km / KM_PER_MILE


def bounding_box(lat: float, lon: float, radius_km: float):
    """Cheap lat/lon box enclosing the radius, for pre-filtering before haversine.

    Returns (lat_min, lat_max, lon_min, lon_max). Longitude bounds are clamped to
    the full range near the poles, where a fixed ground distance spans every
    meridian.
    """
    dlat = math.degrees(radius_km / EARTH_RADIUS_KM)
    coslat = math.cos(math.radians(lat))
    if abs(coslat) < 1e-6:
        return (lat - dlat, lat + dlat, -180.0, 180.0)
    dlon = math.degrees(radius_km / (EARTH_RADIUS_KM * coslat))
    # A box that would wrap the antimeridian is widened to the whole world; the
    # haversine pass downstream still does the real filtering.
    if dlon >= 180.0 or lon - dlon < -180.0 or lon + dlon > 180.0:
        return (lat - dlat, lat + dlat, -180.0, 180.0)
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)
