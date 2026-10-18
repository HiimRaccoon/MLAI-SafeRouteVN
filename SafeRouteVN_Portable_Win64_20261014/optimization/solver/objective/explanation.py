"""Deterministic explanations generated only from objective result facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from optimization.models.common import JsonValue, require_non_empty_string, validate_finite_number


@dataclass(frozen=True, slots=True)
class OptimizationExplanation:
    selected_profile: str
    reason: str
    compared_profile: str | None
    time_delta: float
    distance_delta: float
    risk_delta: float
    objective_delta: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_profile", require_non_empty_string(self.selected_profile, "selected_profile"))
        object.__setattr__(self, "reason", require_non_empty_string(self.reason, "reason"))
        if self.compared_profile is not None:
            object.__setattr__(self, "compared_profile", require_non_empty_string(self.compared_profile, "compared_profile"))
        for name in ("time_delta", "distance_delta", "risk_delta", "objective_delta"):
            object.__setattr__(self, name, validate_finite_number(getattr(self, name), name))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OptimizationExplanation":
        tradeoff = payload["tradeoff"]
        return cls(
            payload["selected_profile"],
            payload["reason"],
            payload.get("compared_profile"),
            tradeoff["time_delta"],
            tradeoff["distance_delta"],
            tradeoff["risk_delta"],
            tradeoff["objective_delta"],
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "selected_profile": self.selected_profile,
            "reason": self.reason,
            "compared_profile": self.compared_profile,
            "tradeoff": {
                "time_delta": self.time_delta,
                "distance_delta": self.distance_delta,
                "risk_delta": self.risk_delta,
                "objective_delta": self.objective_delta,
            },
        }


def explain_result(
    *,
    profile: str,
    distance: float,
    time: float,
    risk: float,
    objective: float,
    compared_profile: str | None = None,
    compared_distance: float | None = None,
    compared_time: float | None = None,
    compared_risk: float | None = None,
    compared_objective: float | None = None,
) -> OptimizationExplanation:
    if compared_profile is None:
        reason = (
            f"Profile {profile} selected the feasible fleet route with configured "
            f"normalized evaluation score {objective:.6f}; raw totals are "
            f"time={time:.6f}s, distance={distance:.6f}m, exposure={risk:.6f}."
        )
        return OptimizationExplanation(profile, reason, None, 0.0, 0.0, 0.0, 0.0)
    deltas = (
        time - float(compared_time),
        distance - float(compared_distance),
        risk - float(compared_risk),
        objective - float(compared_objective),
    )
    reason = (
        f"Profile {profile} produced score {objective:.6f} versus {compared_profile} "
        f"score {float(compared_objective):.6f}; metric deltas are "
        f"time={deltas[0]:+.6f}s, distance={deltas[1]:+.6f}m, "
        f"exposure={deltas[2]:+.6f}."
    )
    return OptimizationExplanation(profile, reason, compared_profile, *deltas)
