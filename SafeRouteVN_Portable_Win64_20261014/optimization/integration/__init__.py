"""TASK-02 boundary conversions and Phase E compatibility validation."""

from .units import (
    UnitConversion,
    describe_task01_conversion,
    hours_to_seconds,
    kilometers_per_hour_to_meters_per_second,
    kilometers_to_meters,
)
from .validator import PhaseEInputValidator, PhaseEValidationResult

__all__ = [
    "PhaseEInputValidator",
    "PhaseEValidationResult",
    "UnitConversion",
    "describe_task01_conversion",
    "hours_to_seconds",
    "kilometers_per_hour_to_meters_per_second",
    "kilometers_to_meters",
]
