"""Versioned fleet-level proof for the unified profile objective domain.

Change Impact Analysis
----------------------
This module validates the frozen E3.2-B1 bound; it does not calculate a new
penalty or alter an objective.  It makes the fleet decomposition explicit and
fails closed when a domain that dominates only one route is supplied.
"""

from __future__ import annotations

from dataclasses import dataclass

from optimization.models.common import JsonValue
from optimization.solver.callbacks import INT64_MAX

from .cost_domain import ObjectiveCostDomain, ObjectiveCostDomainError


FLEET_VALIDATION_VERSION = "fleet-cost-domain-validation-e32b11-v1"


@dataclass(frozen=True, slots=True)
class FleetObjectiveBound:
    """Conservative theoretical bound across every route in one fleet solution."""

    number_of_vehicles: int
    number_of_orders: int
    maximum_drops: int
    maximum_edge_cost: int
    maximum_single_route_arcs: int
    maximum_fleet_route_arcs: int
    maximum_single_route_objective: int
    maximum_fleet_service_objective: int
    drop_penalty: int
    maximum_penalty_accumulation: int
    maximum_total_objective: int
    int64_headroom: int
    fleet_validation_version: str = FLEET_VALIDATION_VERSION
    bound_type: str = "CONSERVATIVE_THEORETICAL_UPPER_BOUND"

    def __post_init__(self) -> None:
        values = (
            self.number_of_vehicles, self.number_of_orders, self.maximum_drops,
            self.maximum_edge_cost, self.maximum_single_route_arcs,
            self.maximum_fleet_route_arcs, self.maximum_single_route_objective,
            self.maximum_fleet_service_objective, self.drop_penalty,
            self.maximum_penalty_accumulation, self.maximum_total_objective,
            self.int64_headroom,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ObjectiveCostDomainError("fleet objective bounds must be non-negative integers")
        if self.number_of_vehicles <= 0:
            raise ObjectiveCostDomainError("fleet validation requires at least one vehicle")
        if self.maximum_drops > self.number_of_orders:
            raise ObjectiveCostDomainError("maximum drops cannot exceed order count")
        if self.maximum_single_route_arcs != self.number_of_orders + 1:
            raise ObjectiveCostDomainError("single-route arc bound must equal orders plus one")
        if self.maximum_fleet_route_arcs != self.number_of_orders + self.number_of_vehicles:
            raise ObjectiveCostDomainError("fleet arc bound must equal orders plus vehicles")
        if self.maximum_single_route_objective != self.maximum_edge_cost * self.maximum_single_route_arcs:
            raise ObjectiveCostDomainError("single-route objective bound is inconsistent")
        if self.maximum_fleet_service_objective != self.maximum_edge_cost * self.maximum_fleet_route_arcs:
            raise ObjectiveCostDomainError("fleet service objective bound is inconsistent")
        if self.drop_penalty <= self.maximum_fleet_service_objective:
            raise ObjectiveCostDomainError("drop penalty does not dominate the fleet service objective")
        if self.maximum_penalty_accumulation != self.drop_penalty * self.maximum_drops:
            raise ObjectiveCostDomainError("penalty accumulation bound is inconsistent")
        if self.maximum_total_objective != self.maximum_fleet_service_objective + self.maximum_penalty_accumulation:
            raise ObjectiveCostDomainError("fleet total objective bound is inconsistent")
        if self.maximum_total_objective > INT64_MAX:
            raise ObjectiveCostDomainError("fleet total objective exceeds int64")
        if self.int64_headroom != INT64_MAX - self.maximum_total_objective:
            raise ObjectiveCostDomainError("fleet int64 headroom is inconsistent")

    @classmethod
    def validate(
        cls,
        domain: ObjectiveCostDomain,
        *,
        number_of_vehicles: int,
        number_of_orders: int,
        maximum_drops: int,
    ) -> "FleetObjectiveBound":
        if not isinstance(domain, ObjectiveCostDomain):
            raise ObjectiveCostDomainError("fleet validation requires ObjectiveCostDomain")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (number_of_vehicles, number_of_orders, maximum_drops)
        ):
            raise ObjectiveCostDomainError("fleet counts must be non-negative integers")
        single_arcs = number_of_orders + 1
        fleet_arcs = number_of_orders + number_of_vehicles
        maximum_single = domain.maximum_edge_cost * single_arcs
        maximum_fleet = domain.maximum_edge_cost * fleet_arcs
        penalty_accumulation = domain.drop_penalty * maximum_drops
        total = maximum_fleet + penalty_accumulation
        bound = cls(
            number_of_vehicles,
            number_of_orders,
            maximum_drops,
            domain.maximum_edge_cost,
            single_arcs,
            fleet_arcs,
            maximum_single,
            maximum_fleet,
            domain.drop_penalty,
            penalty_accumulation,
            total,
            INT64_MAX - total if total <= INT64_MAX else 0,
        )
        if domain.maximum_route_arcs != bound.maximum_fleet_route_arcs:
            raise ObjectiveCostDomainError("B1 route arc bound is not the complete fleet arc bound")
        if domain.maximum_service_objective != bound.maximum_fleet_service_objective:
            raise ObjectiveCostDomainError("B1 service objective is not the complete fleet objective")
        if domain.maximum_drops != maximum_drops:
            raise ObjectiveCostDomainError("B1 and fleet maximum drop counts differ")
        if domain.maximum_total_objective != bound.maximum_total_objective:
            raise ObjectiveCostDomainError("B1 and fleet total objective bounds differ")
        return bound

    @property
    def fleet_dominance_margin(self) -> int:
        return self.drop_penalty - self.maximum_fleet_service_objective

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "fleet_validation_version": self.fleet_validation_version,
            "bound_type": self.bound_type,
            "number_of_vehicles": self.number_of_vehicles,
            "number_of_orders": self.number_of_orders,
            "maximum_drops": self.maximum_drops,
            "maximum_edge_cost": self.maximum_edge_cost,
            "maximum_single_route_arcs": self.maximum_single_route_arcs,
            "maximum_fleet_route_arcs": self.maximum_fleet_route_arcs,
            "maximum_single_route_objective": self.maximum_single_route_objective,
            "maximum_fleet_service_objective": self.maximum_fleet_service_objective,
            "drop_penalty": self.drop_penalty,
            "fleet_dominance_margin": self.fleet_dominance_margin,
            "maximum_penalty_accumulation": self.maximum_penalty_accumulation,
            "maximum_total_objective": self.maximum_total_objective,
            "int64_headroom": self.int64_headroom,
            "fleet_comparable": True,
        }

