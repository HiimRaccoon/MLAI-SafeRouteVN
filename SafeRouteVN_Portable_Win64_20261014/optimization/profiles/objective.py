"""Profile-neutral objective function contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from optimization.models.common import ContractValidationError, validate_finite_number

from .normalization import NormalizedMetrics


@dataclass(frozen=True, slots=True)
class ObjectiveWeights:
    """Validated weights supplied by configuration, never module constants."""

    time: float
    risk: float
    distance: float

    def __post_init__(self) -> None:
        for field_name in ("time", "risk", "distance"):
            object.__setattr__(
                self,
                field_name,
                validate_finite_number(
                    getattr(self, field_name), field_name, minimum=0.0
                ),
            )
        if self.time + self.risk + self.distance <= 0.0:
            raise ContractValidationError("at least one objective weight must be positive")

    def to_dict(self) -> dict[str, float]:
        return {"time": self.time, "risk": self.risk, "distance": self.distance}

    def normalized(self) -> "ObjectiveWeights":
        """Return proportionally normalized weights with a sum of one."""

        total = self.time + self.risk + self.distance
        return ObjectiveWeights(
            time=self.time / total,
            risk=self.risk / total,
            distance=self.distance / total,
        )


@dataclass(frozen=True, slots=True)
class ObjectiveComponents:
    """Auditable components produced by the objective abstraction."""

    time_component: float
    risk_component: float
    distance_component: float

    def __post_init__(self) -> None:
        for field_name in (
            "time_component",
            "risk_component",
            "distance_component",
        ):
            object.__setattr__(
                self,
                field_name,
                validate_finite_number(
                    getattr(self, field_name), field_name, minimum=0.0
                ),
            )

    @property
    def total(self) -> float:
        return self.time_component + self.risk_component + self.distance_component

    def to_dict(self) -> dict[str, float]:
        return {
            "time_component": self.time_component,
            "distance_component": self.distance_component,
            "risk_component": self.risk_component,
        }


@runtime_checkable
class ObjectiveFunction(Protocol):
    """Generic callable contract for normalized objective evaluation."""

    def score(
        self, metrics: NormalizedMetrics, weights: ObjectiveWeights
    ) -> float:
        """Return one objective score without selecting or ranking profiles."""

    def components(
        self, metrics: NormalizedMetrics, weights: ObjectiveWeights
    ) -> ObjectiveComponents:
        """Return the components used to calculate the score."""


class LinearObjectiveFunction:
    """Linear objective implementation driven entirely by supplied weights."""

    def score(self, metrics: NormalizedMetrics, weights: ObjectiveWeights) -> float:
        return validate_finite_number(
            self.components(metrics, weights).total,
            "objective_score",
            minimum=0.0,
        )

    def components(
        self, metrics: NormalizedMetrics, weights: ObjectiveWeights
    ) -> ObjectiveComponents:
        if not isinstance(metrics, NormalizedMetrics):
            raise ContractValidationError("metrics must be NormalizedMetrics")
        if not isinstance(weights, ObjectiveWeights):
            raise ContractValidationError("weights must be ObjectiveWeights")
        return ObjectiveComponents(
            time_component=metrics.time_normalized * weights.time,
            risk_component=metrics.risk_normalized * weights.risk,
            distance_component=metrics.distance_normalized * weights.distance,
        )
