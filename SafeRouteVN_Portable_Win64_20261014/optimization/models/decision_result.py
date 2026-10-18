"""Route alternative and versioned DecisionResult envelope contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from .common import (
    AlternativeMetrics,
    ContractValidationError,
    Diagnostic,
    ExplanationFact,
    GeoPoint,
    JsonValue,
    OptimizationProfile,
    UnitMetadata,
    freeze_json,
    require_non_empty_string,
    to_json_value,
)


def _string_sequence_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for vehicle_id, items in value.items():
        vehicle = require_non_empty_string(vehicle_id, f"{field_name}.vehicle_id")
        if isinstance(items, (str, bytes)):
            raise ContractValidationError(f"{field_name}.{vehicle} must be a sequence")
        result[vehicle] = tuple(
            require_non_empty_string(item, f"{field_name}.{vehicle}[{index}]")
            for index, item in enumerate(items)
        )
    return MappingProxyType(result)


def _route_geometry_mapping(value: Mapping[str, Any]) -> Mapping[str, tuple[GeoPoint, ...]]:
    result: dict[str, tuple[GeoPoint, ...]] = {}
    for vehicle_id, geometry in value.items():
        vehicle = require_non_empty_string(vehicle_id, "route_geometry.vehicle_id")
        if isinstance(geometry, (str, bytes)):
            raise ContractValidationError(f"route_geometry.{vehicle} must be a sequence")
        result[vehicle] = tuple(
            point if isinstance(point, GeoPoint) else GeoPoint.from_value(point)
            for point in geometry
        )
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class RouteAlternative:
    """One profile-specific fleet route alternative."""

    profile: OptimizationProfile
    vehicle_assignments: Mapping[str, tuple[str, ...]]
    stop_sequence: Mapping[str, tuple[str, ...]]
    route_geometry: Mapping[str, tuple[GeoPoint, ...]]
    metrics: AlternativeMetrics
    explanation_facts: tuple[ExplanationFact, ...]

    def __post_init__(self) -> None:
        profile = (
            self.profile
            if isinstance(self.profile, OptimizationProfile)
            else OptimizationProfile(self.profile)
        )
        object.__setattr__(self, "profile", profile)
        assignments = _string_sequence_mapping(
            self.vehicle_assignments, "vehicle_assignments"
        )
        sequences = _string_sequence_mapping(self.stop_sequence, "stop_sequence")
        geometry = _route_geometry_mapping(self.route_geometry)

        missing_sequences = set(assignments) - set(sequences)
        if missing_sequences:
            raise ContractValidationError(
                "assigned vehicles missing stop sequences: "
                + ", ".join(sorted(missing_sequences))
            )
        missing_geometry = set(sequences) - set(geometry)
        if missing_geometry:
            raise ContractValidationError(
                "sequenced vehicles missing route geometry: "
                + ", ".join(sorted(missing_geometry))
            )
        assigned_orders = [order for orders in assignments.values() for order in orders]
        if len(assigned_orders) != len(set(assigned_orders)):
            raise ContractValidationError("an order cannot be assigned to multiple vehicles")

        object.__setattr__(self, "vehicle_assignments", assignments)
        object.__setattr__(self, "stop_sequence", sequences)
        object.__setattr__(self, "route_geometry", geometry)
        metrics = (
            self.metrics
            if isinstance(self.metrics, AlternativeMetrics)
            else AlternativeMetrics.from_dict(self.metrics)
        )
        object.__setattr__(self, "metrics", metrics)
        facts = tuple(
            fact if isinstance(fact, ExplanationFact) else ExplanationFact.from_dict(fact)
            for fact in self.explanation_facts
        )
        object.__setattr__(self, "explanation_facts", facts)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RouteAlternative":
        """Build an alternative from contract data."""

        required = {
            "profile",
            "vehicle_assignments",
            "stop_sequence",
            "route_geometry",
            "metrics",
            "explanation_facts",
        }
        missing = required - set(payload)
        if missing:
            raise ContractValidationError(
                f"RouteAlternative missing required fields: {', '.join(sorted(missing))}"
            )
        return cls(
            profile=payload["profile"],
            vehicle_assignments=payload["vehicle_assignments"],
            stop_sequence=payload["stop_sequence"],
            route_geometry=payload["route_geometry"],
            metrics=AlternativeMetrics.from_dict(payload["metrics"]),
            explanation_facts=tuple(
                ExplanationFact.from_dict(fact) for fact in payload["explanation_facts"]
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the alternative as JSON-compatible data."""

        return {
            "profile": self.profile.value,
            "vehicle_assignments": {
                vehicle: list(orders) for vehicle, orders in self.vehicle_assignments.items()
            },
            "stop_sequence": {
                vehicle: list(stops) for vehicle, stops in self.stop_sequence.items()
            },
            "route_geometry": {
                vehicle: [point.to_list() for point in geometry]
                for vehicle, geometry in self.route_geometry.items()
            },
            "metrics": self.metrics.to_dict(),
            "explanation_facts": [fact.to_dict() for fact in self.explanation_facts],
        }


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """One versioned scenario envelope with exactly three ordered alternatives."""

    scenario_id: str
    context_version: str
    state_version: str
    alternatives: tuple[RouteAlternative, ...]
    diagnostics: tuple[Diagnostic, ...]
    provenance: Mapping[str, Any]
    schema_version: str = "1.0.0"
    unit_metadata: UnitMetadata = field(default_factory=UnitMetadata)

    def __post_init__(self) -> None:
        for field_name in ("scenario_id", "context_version", "state_version", "schema_version"):
            object.__setattr__(
                self,
                field_name,
                require_non_empty_string(getattr(self, field_name), field_name),
            )
        alternatives = tuple(
            alternative
            if isinstance(alternative, RouteAlternative)
            else RouteAlternative.from_dict(alternative)
            for alternative in self.alternatives
        )
        expected_order = (
            OptimizationProfile.FASTEST,
            OptimizationProfile.BALANCED,
            OptimizationProfile.SAFER,
        )
        actual_order = tuple(alternative.profile for alternative in alternatives)
        if actual_order != expected_order:
            raise ContractValidationError(
                "alternatives must be ordered exactly as FASTEST, BALANCED, SAFER"
            )
        object.__setattr__(self, "alternatives", alternatives)
        diagnostics = tuple(
            diagnostic
            if isinstance(diagnostic, Diagnostic)
            else Diagnostic.from_dict(diagnostic)
            for diagnostic in self.diagnostics
        )
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "provenance", freeze_json(self.provenance, "provenance"))
        metadata = (
            self.unit_metadata
            if isinstance(self.unit_metadata, UnitMetadata)
            else UnitMetadata.from_mapping(self.unit_metadata)
        )
        object.__setattr__(self, "unit_metadata", metadata)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DecisionResult":
        """Validate and build a DecisionResult envelope from a mapping."""

        required = {
            "scenario_id",
            "context_version",
            "state_version",
            "alternatives",
            "diagnostics",
            "provenance",
        }
        missing = required - set(payload)
        if missing:
            raise ContractValidationError(
                f"DecisionResult missing required fields: {', '.join(sorted(missing))}"
            )
        return cls(
            scenario_id=payload["scenario_id"],
            context_version=payload["context_version"],
            state_version=payload["state_version"],
            alternatives=tuple(
                RouteAlternative.from_dict(item) for item in payload["alternatives"]
            ),
            diagnostics=tuple(
                Diagnostic.from_dict(item) for item in payload["diagnostics"]
            ),
            provenance=payload["provenance"],
            schema_version=payload.get("schema_version", "1.0.0"),
            unit_metadata=UnitMetadata.from_mapping(payload),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        """Return the complete envelope as JSON-compatible data."""

        return {
            **self.unit_metadata.to_dict(),
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "context_version": self.context_version,
            "state_version": self.state_version,
            "alternatives": [item.to_dict() for item in self.alternatives],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "provenance": to_json_value(self.provenance),
        }
