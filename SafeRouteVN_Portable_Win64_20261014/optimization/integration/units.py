"""Explicit TASK-01 to TASK-02 unit-boundary conversions."""

from __future__ import annotations

from dataclasses import dataclass

from optimization.models.common import validate_finite_number


@dataclass(frozen=True, slots=True)
class UnitConversion:
    """Auditable record of one explicit scalar unit conversion."""

    source_value: float
    source_unit: str
    target_value: float
    target_unit: str
    formula: str


def kilometers_to_meters(value: float) -> float:
    """Convert TASK-01 distance in kilometres to internal metres."""

    kilometers = validate_finite_number(value, "distance_km", minimum=0.0)
    return kilometers * 1000.0


def hours_to_seconds(value: float) -> float:
    """Convert TASK-01 travel time in hours to internal seconds."""

    hours = validate_finite_number(value, "time_hour", minimum=0.0)
    return hours * 3600.0


def kilometers_per_hour_to_meters_per_second(value: float) -> float:
    """Convert TASK-01 km/h speed to the internal SI speed representation."""

    speed = validate_finite_number(value, "speed_km_per_hour", minimum=0.0)
    return speed / 3.6


def describe_task01_conversion(
    *, distance_km: float, time_hour: float, speed_km_per_hour: float
) -> tuple[UnitConversion, UnitConversion, UnitConversion]:
    """Return converted values together with explicit conversion provenance."""

    distance = validate_finite_number(distance_km, "distance_km", minimum=0.0)
    time = validate_finite_number(time_hour, "time_hour", minimum=0.0)
    speed = validate_finite_number(
        speed_km_per_hour, "speed_km_per_hour", minimum=0.0
    )
    return (
        UnitConversion(
            distance,
            "kilometer",
            kilometers_to_meters(distance),
            "meter",
            "kilometer * 1000",
        ),
        UnitConversion(
            time,
            "hour",
            hours_to_seconds(time),
            "second",
            "hour * 3600",
        ),
        UnitConversion(
            speed,
            "kilometer_per_hour",
            kilometers_per_hour_to_meters_per_second(speed),
            "meter_per_second",
            "kilometer_per_hour / 3.6",
        ),
    )
