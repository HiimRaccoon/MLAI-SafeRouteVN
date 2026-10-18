"""Unified integer cost domain for profile arcs and disjunction penalties.

Change Impact Analysis
----------------------
This additive contract changes only profile-aware disjunction penalty derivation.
Distance-only E2.6 solving, feasibility dimensions, profile weights, normalized
scoring, and evaluation objectives are unchanged.  The penalty is derived from
the registered packed route-cost bound and rejected before solving if the full
objective cannot fit in signed int64.
"""

from __future__ import annotations

from dataclasses import dataclass

from optimization.models.common import ContractValidationError, JsonValue
from optimization.solver.callbacks import INT64_MAX


COST_DOMAIN_VERSION = "objective-cost-domain-e32b1-v1"
SCALING_VERSION = "objective-round-half-up-e3-v1"
PENALTY_POLICY_VERSION = "max-service-plus-one-e32b1-v1"


class ObjectiveCostDomainError(ContractValidationError):
    """Fail-closed error surfaced as a structured solver diagnostic."""

    diagnostic_code = "INVALID_COST_DOMAIN_CONFIGURATION"


@dataclass(frozen=True, slots=True)
class ObjectiveCostDomain:
    """Prove that packed route costs and drop penalties share one int64 domain."""

    integer_scaling_factor: int
    rounding_policy: str
    maximum_edge_cost: int
    maximum_route_arcs: int
    maximum_drops: int
    maximum_service_objective: int
    drop_penalty: int
    maximum_total_objective: int
    overflow_margin: int
    minimum_service_objective: int = 0
    cost_domain_version: str = COST_DOMAIN_VERSION
    scaling_version: str = SCALING_VERSION
    penalty_policy_version: str = PENALTY_POLICY_VERSION

    def __post_init__(self) -> None:
        integer_fields = (
            "integer_scaling_factor", "maximum_edge_cost", "maximum_route_arcs",
            "maximum_drops", "maximum_service_objective", "drop_penalty",
            "maximum_total_objective", "overflow_margin", "minimum_service_objective",
        )
        for name in integer_fields:
            value = getattr(self, name)
            minimum = 1 if name in {"integer_scaling_factor", "maximum_route_arcs"} else 0
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ObjectiveCostDomainError(f"{name} must be an integer >= {minimum}")
        if self.rounding_policy != "ROUND_HALF_UP":
            raise ObjectiveCostDomainError("cost-domain rounding must be ROUND_HALF_UP")
        if self.maximum_service_objective != self.maximum_edge_cost * self.maximum_route_arcs:
            raise ObjectiveCostDomainError("maximum service objective does not match its route bound")
        if self.drop_penalty <= self.maximum_service_objective:
            raise ObjectiveCostDomainError("drop penalty must exceed the maximum service objective")
        expected_total = self.maximum_service_objective + self.drop_penalty * self.maximum_drops
        if self.maximum_total_objective != expected_total:
            raise ObjectiveCostDomainError("maximum total objective does not match its bound")
        if self.maximum_total_objective > INT64_MAX:
            raise ObjectiveCostDomainError("unified objective domain exceeds int64")
        if self.overflow_margin != INT64_MAX - self.maximum_total_objective:
            raise ObjectiveCostDomainError("overflow margin is inconsistent")

    @classmethod
    def derive(
        cls,
        *,
        integer_scaling_factor: int,
        rounding_policy: str,
        maximum_edge_cost: int,
        maximum_route_arcs: int,
        maximum_drops: int,
    ) -> "ObjectiveCostDomain":
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (maximum_edge_cost, maximum_drops)
        ) or isinstance(maximum_route_arcs, bool) or not isinstance(maximum_route_arcs, int) or maximum_route_arcs <= 0:
            raise ObjectiveCostDomainError("cost-domain bounds must be non-negative integers and route arcs positive")
        maximum_service = maximum_edge_cost * maximum_route_arcs
        if maximum_service >= INT64_MAX:
            raise ObjectiveCostDomainError("maximum service objective leaves no safe penalty value")
        penalty = maximum_service + 1
        maximum_total = maximum_service + penalty * maximum_drops
        if maximum_total > INT64_MAX:
            raise ObjectiveCostDomainError("route plus drop-penalty bound exceeds int64")
        return cls(
            integer_scaling_factor=integer_scaling_factor,
            rounding_policy=rounding_policy,
            maximum_edge_cost=maximum_edge_cost,
            maximum_route_arcs=maximum_route_arcs,
            maximum_drops=maximum_drops,
            maximum_service_objective=maximum_service,
            drop_penalty=penalty,
            maximum_total_objective=maximum_total,
            overflow_margin=INT64_MAX - maximum_total,
        )

    @property
    def penalty_service_ratio(self) -> float:
        if self.maximum_service_objective == 0:
            return float("inf")
        return self.drop_penalty / self.maximum_service_objective

    @property
    def penalty_dominance_margin(self) -> int:
        return self.drop_penalty - self.maximum_service_objective

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "cost_domain_version": self.cost_domain_version,
            "scaling_version": self.scaling_version,
            "penalty_policy_version": self.penalty_policy_version,
            "integer_scaling_factor": self.integer_scaling_factor,
            "rounding_policy": self.rounding_policy,
            "minimum_feasible_route_cost": self.minimum_service_objective,
            "maximum_edge_cost": self.maximum_edge_cost,
            "maximum_route_arcs": self.maximum_route_arcs,
            "maximum_feasible_route_cost": self.maximum_service_objective,
            "minimum_drop_penalty": self.drop_penalty,
            "maximum_drops": self.maximum_drops,
            "maximum_total_objective": self.maximum_total_objective,
            "overflow_margin": self.overflow_margin,
            "penalty_service_ratio": self.penalty_service_ratio,
            "penalty_dominance_margin": self.penalty_dominance_margin,
            "comparable": True,
        }

