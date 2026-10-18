"""Shared value objects for TASK-02 internal contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence, TypeAlias


NodeId: TypeAlias = str | int
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ContractValidationError(ValueError):
    """Raised when an internal TASK-02 contract is invalid."""


class OptimizationProfile(str, Enum):
    """The only public alternatives supported by the MVP."""

    FASTEST = "FASTEST"
    BALANCED = "BALANCED"
    SAFER = "SAFER"


class DiagnosticSeverity(str, Enum):
    """Machine-readable diagnostic severity."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    FATAL = "FATAL"


@dataclass(frozen=True, slots=True)
class UnitMetadata:
    """Frozen TASK-02 internal unit and semantic declaration."""

    unit_schema_version: str = "v1"
    distance_unit: str = "meter"
    time_unit: str = "second"
    risk_semantic: str = "exposure_proxy"
    geometry_order: str = "longitude_latitude"
    coordinate_system: str = "WGS84"

    def __post_init__(self) -> None:
        expected = {
            "unit_schema_version": "v1",
            "distance_unit": "meter",
            "time_unit": "second",
            "risk_semantic": "exposure_proxy",
            "geometry_order": "longitude_latitude",
            "coordinate_system": "WGS84",
        }
        for field_name, expected_value in expected.items():
            actual = require_non_empty_string(getattr(self, field_name), field_name)
            if actual != expected_value:
                raise ContractValidationError(
                    f"{field_name} must be {expected_value!r} for unit schema v1"
                )
            object.__setattr__(self, field_name, actual)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "UnitMetadata":
        """Read flattened unit metadata, retaining additive wire compatibility."""

        return cls(
            unit_schema_version=payload.get("unit_schema_version", "v1"),
            distance_unit=payload.get("distance_unit", "meter"),
            time_unit=payload.get("time_unit", "second"),
            risk_semantic=payload.get("risk_semantic", "exposure_proxy"),
            geometry_order=payload.get("geometry_order", "longitude_latitude"),
            coordinate_system=payload.get("coordinate_system", "WGS84"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "unit_schema_version": self.unit_schema_version,
            "distance_unit": self.distance_unit,
            "time_unit": self.time_unit,
            "risk_semantic": self.risk_semantic,
            "geometry_order": self.geometry_order,
            "coordinate_system": self.coordinate_system,
        }


def require_non_empty_string(value: Any, field_name: str) -> str:
    """Return a stripped non-empty string or raise a contract error."""

    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def validate_node_id(value: Any, field_name: str) -> NodeId:
    """Validate a JSON-safe OSM/application node identifier."""

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ContractValidationError(f"{field_name} must be a string or integer")
    if isinstance(value, str) and not value.strip():
        raise ContractValidationError(f"{field_name} must not be empty")
    return value


def validate_finite_number(
    value: Any,
    field_name: str,
    *,
    minimum: float | None = None,
) -> float:
    """Validate a finite numeric value, optionally with a lower bound."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError(f"{field_name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ContractValidationError(f"{field_name} must be finite")
    if minimum is not None and result < minimum:
        raise ContractValidationError(f"{field_name} must be >= {minimum}")
    return result


def freeze_json(value: Any, field_name: str = "value") -> Any:
    """Validate and recursively freeze a JSON-compatible value."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractValidationError(f"{field_name} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractValidationError(f"{field_name} mapping keys must be strings")
            frozen[key] = freeze_json(item, f"{field_name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(
            freeze_json(item, f"{field_name}[{index}]")
            for index, item in enumerate(value)
        )
    raise ContractValidationError(f"{field_name} must be JSON-compatible")


def to_json_value(value: Any) -> JsonValue:
    """Convert frozen internal values into JSON-serializable containers."""

    if isinstance(value, Mapping):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [to_json_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


@dataclass(frozen=True, slots=True)
class GeoPoint:
    """A WGS84 coordinate in GeoJSON order: longitude, latitude."""

    longitude: float
    latitude: float

    def __post_init__(self) -> None:
        longitude = validate_finite_number(self.longitude, "longitude")
        latitude = validate_finite_number(self.latitude, "latitude")
        if not -180.0 <= longitude <= 180.0:
            raise ContractValidationError("longitude must be within [-180, 180]")
        if not -90.0 <= latitude <= 90.0:
            raise ContractValidationError("latitude must be within [-90, 90]")
        object.__setattr__(self, "longitude", longitude)
        object.__setattr__(self, "latitude", latitude)

    @classmethod
    def from_value(cls, value: Any) -> "GeoPoint":
        """Build a point from a mapping or a two-item [lon, lat] sequence."""

        if isinstance(value, Mapping):
            longitude = value.get("longitude", value.get("lon"))
            latitude = value.get("latitude", value.get("lat"))
            return cls(longitude=longitude, latitude=latitude)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) != 2:
                raise ContractValidationError("geometry coordinate must contain [lon, lat]")
            return cls(longitude=value[0], latitude=value[1])
        raise ContractValidationError("geometry coordinate must be a mapping or [lon, lat]")

    def to_list(self) -> list[float]:
        """Return a GeoJSON-compatible coordinate pair."""

        return [self.longitude, self.latitude]


@dataclass(frozen=True, slots=True)
class EdgeReference:
    """A directed multigraph edge reference, including its edge key."""

    u: NodeId
    v: NodeId
    key: NodeId

    def __post_init__(self) -> None:
        object.__setattr__(self, "u", validate_node_id(self.u, "edge.u"))
        object.__setattr__(self, "v", validate_node_id(self.v, "edge.v"))
        object.__setattr__(self, "key", validate_node_id(self.key, "edge.key"))

    @classmethod
    def from_value(cls, value: Any) -> "EdgeReference":
        """Build an edge reference from a mapping or [u, v, key]."""

        if isinstance(value, Mapping):
            missing = {"u", "v", "key"} - set(value)
            if missing:
                raise ContractValidationError(
                    f"edge is missing required fields: {', '.join(sorted(missing))}"
                )
            return cls(u=value["u"], v=value["v"], key=value["key"])
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) != 3:
                raise ContractValidationError("edge sequence must contain [u, v, key]")
            return cls(u=value[0], v=value[1], key=value[2])
        raise ContractValidationError("edge must be a mapping or [u, v, key]")

    def to_dict(self) -> dict[str, NodeId]:
        """Return the edge as a JSON-compatible mapping."""

        return {"u": self.u, "v": self.v, "key": self.key}


@dataclass(frozen=True, slots=True)
class AlternativeMetrics:
    """Comparable metrics reported for every route alternative."""

    distance: float
    travel_time: float
    cost: float
    lateness: float
    relative_exposure: float
    objective_score: float

    def __post_init__(self) -> None:
        for field_name in (
            "distance",
            "travel_time",
            "cost",
            "lateness",
            "relative_exposure",
            "objective_score",
        ):
            object.__setattr__(
                self,
                field_name,
                validate_finite_number(getattr(self, field_name), field_name, minimum=0.0),
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AlternativeMetrics":
        """Build metrics from their wire representation."""

        required = {
            "distance",
            "travel_time",
            "cost",
            "lateness",
            "relative_exposure",
            "objective_score",
        }
        missing = required - set(payload)
        if missing:
            raise ContractValidationError(
                f"metrics missing required fields: {', '.join(sorted(missing))}"
            )
        return cls(**{field: payload[field] for field in required})

    def to_dict(self) -> dict[str, float]:
        """Return metrics as a JSON-compatible mapping."""

        return {
            "distance": self.distance,
            "travel_time": self.travel_time,
            "cost": self.cost,
            "lateness": self.lateness,
            "relative_exposure": self.relative_exposure,
            "objective_score": self.objective_score,
        }


@dataclass(frozen=True, slots=True)
class ExplanationFact:
    """A structured, engine-derived explanation fact."""

    code: str
    summary: str
    details: Mapping[str, Any]
    provenance_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", require_non_empty_string(self.code, "fact.code"))
        object.__setattr__(
            self, "summary", require_non_empty_string(self.summary, "fact.summary")
        )
        object.__setattr__(self, "details", freeze_json(self.details, "fact.details"))
        refs = tuple(
            require_non_empty_string(ref, "fact.provenance_refs")
            for ref in self.provenance_refs
        )
        object.__setattr__(self, "provenance_refs", refs)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExplanationFact":
        """Build an explanation fact from a mapping."""

        return cls(
            code=payload.get("code"),
            summary=payload.get("summary"),
            details=payload.get("details", {}),
            provenance_refs=tuple(payload.get("provenance_refs", ())),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        """Return the fact as JSON-compatible data."""

        return {
            "code": self.code,
            "summary": self.summary,
            "details": to_json_value(self.details),
            "provenance_refs": list(self.provenance_refs),
        }


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A structured validation/solver diagnostic."""

    code: str
    severity: DiagnosticSeverity
    message: str
    details: Mapping[str, Any]
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", require_non_empty_string(self.code, "diagnostic.code"))
        severity = (
            self.severity
            if isinstance(self.severity, DiagnosticSeverity)
            else DiagnosticSeverity(self.severity)
        )
        object.__setattr__(self, "severity", severity)
        object.__setattr__(
            self, "message", require_non_empty_string(self.message, "diagnostic.message")
        )
        object.__setattr__(self, "details", freeze_json(self.details, "diagnostic.details"))
        object.__setattr__(self, "context", freeze_json(self.context, "diagnostic.context"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Diagnostic":
        """Build a diagnostic from a mapping."""

        return cls(
            code=payload.get("code"),
            severity=payload.get("severity"),
            message=payload.get("message"),
            details=payload.get("details", {}),
            context=payload.get("context", {}),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        """Return the diagnostic as JSON-compatible data."""

        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "details": to_json_value(self.details),
            "context": to_json_value(self.context),
        }
