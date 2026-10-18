"""Explicit conversion at the input boundary. No implicit unit guessing."""

import math
from datetime import datetime
from zoneinfo import ZoneInfo

from geo_data.common import VN_TIMEZONE

UNITS = {
    "distance": "km", "travelSpeed": "km/h", "duration": "h",
    "mass": "kg", "currency": "VND", "costPerDistance": "VND/km",
    "roadWidth": "m", "visibility": "m", "precipitation": "mm",
    "timezone": VN_TIMEZONE, "coordinates": "WGS84 lon,lat",
}


def nonnegative(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return float(value)


def distance_km(value, unit):
    factors = {"km": 1, "m": 0.001}
    if unit not in factors:
        raise ValueError(f"Unsupported distance unit: {unit}")
    return nonnegative(value, "distance") * factors[unit]


def duration_hours(value, unit):
    factors = {"h": 1, "min": 1 / 60, "s": 1 / 3600, "ms": 1 / 3600000}
    if unit not in factors:
        raise ValueError(f"Unsupported duration unit: {unit}")
    return nonnegative(value, "duration") * factors[unit]


def speed_kph(value, unit):
    factors = {"km/h": 1, "m/s": 3.6, "mph": 1.609344}
    if unit not in factors:
        raise ValueError(f"Unsupported speed unit: {unit}")
    return nonnegative(value, "speed") * factors[unit]


def mass_kg(value, unit):
    factors = {"kg": 1, "g": 0.001}
    if unit not in factors:
        raise ValueError(f"Unsupported mass unit: {unit}")
    return nonnegative(value, "mass") * factors[unit]


def travel_hours(length_km, speed):
    length = nonnegative(length_km, "lengthKm")
    speed = nonnegative(speed, "baseSpeedKph")
    if speed == 0:
        raise ValueError("baseSpeedKph must be positive")
    return length / speed


def timestamp_vn(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return value.astimezone(ZoneInfo(VN_TIMEZONE)).isoformat()
