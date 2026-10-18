"""Single-source normalized scoring and auditable objective records."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

from optimization.models.common import (
    ContractValidationError,
    JsonValue,
    require_non_empty_string,
    validate_finite_number,
)
from optimization.profiles import (
    LinearObjectiveFunction,
    NormalizationConfig,
    NormalizedMetrics,
    ObjectiveComponents,
    RawMetrics,
    normalize_metrics,
)
from optimization.solver.callbacks import INT64_MAX

from .profiles import ProfileObjectiveSpec


@dataclass(frozen=True, slots=True)
class ObjectiveBreakdown:
    time_component: float
    distance_component: float
    risk_component: float
    total_score: float

    def __post_init__(self) -> None:
        for name in ("time_component", "distance_component", "risk_component", "total_score"):
            object.__setattr__(self, name, validate_finite_number(getattr(self, name), name, minimum=0.0))
        if abs(self.total_score - (self.time_component + self.distance_component + self.risk_component)) > 1e-9:
            raise ContractValidationError("objective total must equal its component sum")

    @classmethod
    def from_components(cls, components: ObjectiveComponents) -> "ObjectiveBreakdown":
        return cls(
            components.time_component,
            components.distance_component,
            components.risk_component,
            components.total,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ObjectiveBreakdown":
        return cls(
            payload["time_component"],
            payload["distance_component"],
            payload["risk_component"],
            payload["total_score"],
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "time_component": self.time_component,
            "distance_component": self.distance_component,
            "risk_component": self.risk_component,
            "total_score": self.total_score,
        }


@dataclass(frozen=True, slots=True)
class ScoredMetrics:
    raw_metrics: RawMetrics
    normalized_metrics: NormalizedMetrics
    breakdown: ObjectiveBreakdown

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ScoredMetrics":
        raw = payload["raw_metrics"]
        normalized = payload["normalized_metrics"]
        return cls(
            RawMetrics(raw["distance"], raw["time"], raw["length"], raw["risk"]),
            NormalizedMetrics(
                normalized["distance_normalized"],
                normalized["time_normalized"],
                normalized["length_normalized"],
                normalized["risk_normalized"],
            ),
            ObjectiveBreakdown.from_dict(payload["objective_breakdown"]),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "raw_metrics": {
                "distance": self.raw_metrics.distance,
                "time": self.raw_metrics.time,
                "length": self.raw_metrics.length,
                "risk": self.raw_metrics.risk,
            },
            "normalized_metrics": self.normalized_metrics.to_dict(),
            "objective_breakdown": self.breakdown.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RouteObjectiveRecord:
    vehicle_id: str
    solver_metrics: ScoredMetrics
    evaluation_metrics: ScoredMetrics
    solver_scaled_cost: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "vehicle_id", require_non_empty_string(self.vehicle_id, "vehicle_id"))
        if isinstance(self.solver_scaled_cost, bool) or not isinstance(self.solver_scaled_cost, int) or self.solver_scaled_cost < 0:
            raise ContractValidationError("solver_scaled_cost must be a non-negative integer")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "vehicle_id": self.vehicle_id,
            "solver_metrics": self.solver_metrics.to_dict(),
            "evaluation_metrics": self.evaluation_metrics.to_dict(),
            "solver_scaled_cost": self.solver_scaled_cost,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RouteObjectiveRecord":
        return cls(
            payload["vehicle_id"],
            ScoredMetrics.from_dict(payload["solver_metrics"]),
            ScoredMetrics.from_dict(payload["evaluation_metrics"]),
            payload["solver_scaled_cost"],
        )


@dataclass(frozen=True, slots=True)
class SolverObjectiveRecord:
    raw_cost: float
    scaled_cost: int
    route_scaled_cost: int
    unassigned_penalty_scaled: int
    cost_domain_version: str = "legacy-profile-cost-domain-e3-v1"
    scaling_version: str = "objective-round-half-up-e3-v1"
    penalty_policy_version: str = "distance-scaled-disjunction-e26-v1"
    fleet_validation_version: str = "legacy-unvalidated-fleet-domain-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_cost", validate_finite_number(self.raw_cost, "raw_cost", minimum=0.0))
        for name in ("scaled_cost", "route_scaled_cost", "unassigned_penalty_scaled"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"{name} must be a non-negative integer")
        if self.route_scaled_cost + self.unassigned_penalty_scaled != self.scaled_cost:
            raise ContractValidationError("scaled solver cost must equal route cost plus unassigned penalty")
        for name in ("cost_domain_version", "scaling_version", "penalty_policy_version", "fleet_validation_version"):
            object.__setattr__(self, name, require_non_empty_string(getattr(self, name), name))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SolverObjectiveRecord":
        return cls(
            **{name: payload[name] for name in ("raw_cost", "scaled_cost", "route_scaled_cost", "unassigned_penalty_scaled")},
            cost_domain_version=payload.get("cost_domain_version", "legacy-profile-cost-domain-e3-v1"),
            scaling_version=payload.get("scaling_version", "objective-round-half-up-e3-v1"),
            penalty_policy_version=payload.get("penalty_policy_version", "distance-scaled-disjunction-e26-v1"),
            fleet_validation_version=payload.get("fleet_validation_version", "legacy-unvalidated-fleet-domain-v1"),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "raw_cost": self.raw_cost,
            "scaled_cost": self.scaled_cost,
            "route_scaled_cost": self.route_scaled_cost,
            "unassigned_penalty_scaled": self.unassigned_penalty_scaled,
            "cost_domain_version": self.cost_domain_version,
            "scaling_version": self.scaling_version,
            "penalty_policy_version": self.penalty_policy_version,
            "fleet_validation_version": self.fleet_validation_version,
        }


@dataclass(frozen=True, slots=True)
class ObjectiveRecord:
    objective_version: str
    scoring_logic_version: str
    profile: str
    solver_objective: SolverObjectiveRecord
    evaluation_objective: ObjectiveBreakdown
    route_objectives: tuple[RouteObjectiveRecord, ...]

    def __post_init__(self) -> None:
        for name in ("objective_version", "scoring_logic_version", "profile"):
            object.__setattr__(self, name, require_non_empty_string(getattr(self, name), name))
        route_total = sum(item.evaluation_metrics.breakdown.total_score for item in self.route_objectives)
        if abs(route_total - self.evaluation_objective.total_score) > 1e-9:
            raise ContractValidationError("fleet evaluation objective must equal route aggregation")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "objective_version": self.objective_version,
            "scoring_logic_version": self.scoring_logic_version,
            "profile": self.profile,
            "solver_objective": self.solver_objective.to_dict(),
            "evaluation_objective": self.evaluation_objective.to_dict(),
            "route_objectives": [item.to_dict() for item in self.route_objectives],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ObjectiveRecord":
        return cls(
            payload["objective_version"],
            payload["scoring_logic_version"],
            payload["profile"],
            SolverObjectiveRecord.from_dict(payload["solver_objective"]),
            ObjectiveBreakdown.from_dict(payload["evaluation_objective"]),
            tuple(RouteObjectiveRecord.from_dict(item) for item in payload["route_objectives"]),
        )


class ObjectiveIntegerScaler:
    """Deterministic non-negative objective scaling with int64 protection."""

    def __init__(self, factor: int, rounding_policy: str) -> None:
        if isinstance(factor, bool) or not isinstance(factor, int) or factor <= 0:
            raise ContractValidationError("objective scaling factor must be positive")
        if rounding_policy != "ROUND_HALF_UP":
            raise ContractValidationError("objective rounding must be ROUND_HALF_UP")
        self.factor = factor
        self.rounding_policy = rounding_policy

    def scale(self, value: float) -> int:
        raw = validate_finite_number(value, "objective_value", minimum=0.0)
        scaled = int((Decimal(str(raw)) * Decimal(self.factor)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if scaled > INT64_MAX:
            raise ContractValidationError("scaled objective exceeds int64")
        return scaled


def score_raw_metrics(
    raw_metrics: RawMetrics,
    normalization: NormalizationConfig,
    profile: ProfileObjectiveSpec,
) -> ScoredMetrics:
    """Normalize once and delegate the only formula to Phase C.5 ObjectiveFunction."""

    normalized = normalize_metrics(raw_metrics, normalization)
    components = LinearObjectiveFunction().components(normalized, profile.weights)
    return ScoredMetrics(raw_metrics, normalized, ObjectiveBreakdown.from_components(components))


def ranking_value(scored: ScoredMetrics, field: str) -> float:
    if field == "weighted_score":
        return scored.breakdown.total_score
    return {
        "time": scored.normalized_metrics.time_normalized,
        "distance": scored.normalized_metrics.distance_normalized,
        "risk": scored.normalized_metrics.risk_normalized,
    }[field]

