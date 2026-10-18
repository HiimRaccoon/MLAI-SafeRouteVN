"""Validated trade-off explanation data structures.

The structures contain supplied facts only. They do not infer or generate an
explanation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from optimization.models.common import (
    ContractValidationError,
    require_non_empty_string,
    validate_finite_number,
)


@dataclass(frozen=True, slots=True)
class TradeoffDeltas:
    """Numeric change values; positive and negative deltas are both valid."""

    time_delta: float
    distance_delta: float
    risk_delta: float
    objective_delta: float

    def __post_init__(self) -> None:
        for field_name in (
            "time_delta",
            "distance_delta",
            "risk_delta",
            "objective_delta",
        ):
            object.__setattr__(
                self,
                field_name,
                validate_finite_number(getattr(self, field_name), field_name),
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TradeoffDeltas":
        required = {
            "time_delta",
            "distance_delta",
            "risk_delta",
            "objective_delta",
        }
        missing = required - set(payload)
        if missing:
            raise ContractValidationError(
                f"tradeoff missing required fields: {', '.join(sorted(missing))}"
            )
        return cls(**{field: payload[field] for field in required})

    def to_dict(self) -> dict[str, float]:
        return {
            "time_delta": self.time_delta,
            "distance_delta": self.distance_delta,
            "risk_delta": self.risk_delta,
            "objective_delta": self.objective_delta,
        }


@dataclass(frozen=True, slots=True)
class TradeoffExplanation:
    """Future DecisionResult explanation contract with no generation logic."""

    why_changed: str
    tradeoff: TradeoffDeltas

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "why_changed",
            require_non_empty_string(self.why_changed, "why_changed"),
        )
        if not isinstance(self.tradeoff, TradeoffDeltas):
            object.__setattr__(
                self, "tradeoff", TradeoffDeltas.from_dict(self.tradeoff)
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TradeoffExplanation":
        if "why_changed" not in payload or "tradeoff" not in payload:
            raise ContractValidationError(
                "explanation requires why_changed and tradeoff"
            )
        if not isinstance(payload["tradeoff"], Mapping):
            raise ContractValidationError("tradeoff must be a mapping")
        return cls(
            why_changed=payload["why_changed"],
            tradeoff=TradeoffDeltas.from_dict(payload["tradeoff"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"why_changed": self.why_changed, "tradeoff": self.tradeoff.to_dict()}
