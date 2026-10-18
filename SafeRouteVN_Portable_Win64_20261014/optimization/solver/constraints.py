"""VRPTW input contracts, Time Dimension, Capacity Dimension, and feasibility rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from optimization.models import ContractValidationError, GeoPoint, MatrixBundle
from optimization.models.common import (
    NodeId,
    require_non_empty_string,
    validate_finite_number,
    validate_node_id,
)

from .callbacks import IntegerScalingPolicy


def _window(value: Any, field_name: str) -> tuple[float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise ContractValidationError(f"{field_name} must be [earliest, latest]")
    earliest = validate_finite_number(value[0], f"{field_name}[0]", minimum=0.0)
    latest = validate_finite_number(value[1], f"{field_name}[1]", minimum=0.0)
    if earliest > latest:
        raise ContractValidationError(f"{field_name} earliest must not exceed latest")
    return earliest, latest


@dataclass(frozen=True, slots=True)
class Depot:
    node_id: NodeId
    location: GeoPoint

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", validate_node_id(self.node_id, "depot.node_id"))
        point = self.location if isinstance(self.location, GeoPoint) else GeoPoint.from_value(self.location)
        object.__setattr__(self, "location", point)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "Depot":
        if not isinstance(payload, Mapping) or "node_id" not in payload or "location" not in payload:
            raise ContractValidationError("depot requires node_id and location")
        return cls(payload["node_id"], payload["location"])


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    depot: Depot
    scenario_version: str = "UNVERSIONED"
    allow_unassigned_orders: bool = True
    allow_non_depot_start_end: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", require_non_empty_string(self.scenario_id, "scenario_id"))
        object.__setattr__(
            self,
            "scenario_version",
            require_non_empty_string(self.scenario_version, "scenario_version"),
        )
        if not isinstance(self.depot, Depot):
            raise ContractValidationError("depot must be Depot")
        if not isinstance(self.allow_unassigned_orders, bool):
            raise ContractValidationError("allow_unassigned_orders must be boolean")
        if not isinstance(self.allow_non_depot_start_end, bool):
            raise ContractValidationError("allow_non_depot_start_end must be boolean")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "Scenario":
        if not isinstance(payload, Mapping):
            raise ContractValidationError("scenario must be a mapping")
        return cls(
            scenario_id=payload.get("scenario_id"),
            depot=Depot.from_mapping(payload.get("depot")),
            scenario_version=payload.get("scenario_version", "UNVERSIONED"),
            allow_unassigned_orders=payload.get("allow_unassigned_orders", True),
            allow_non_depot_start_end=payload.get("allow_non_depot_start_end", False),
        )


@dataclass(frozen=True, slots=True)
class Vehicle:
    vehicle_id: str
    start_node: NodeId
    end_node: NodeId
    capacity: float
    capacity_unit: str
    availability_window: tuple[float, float]
    current_load: float = 0.0
    available: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "vehicle_id", require_non_empty_string(self.vehicle_id, "vehicle_id"))
        object.__setattr__(self, "start_node", validate_node_id(self.start_node, "start_node"))
        object.__setattr__(self, "end_node", validate_node_id(self.end_node, "end_node"))
        capacity = validate_finite_number(self.capacity, "capacity", minimum=0.0)
        current = validate_finite_number(self.current_load, "current_load", minimum=0.0)
        if current > capacity:
            raise ContractValidationError("current_load must not exceed vehicle capacity")
        object.__setattr__(self, "capacity", capacity)
        object.__setattr__(self, "current_load", current)
        object.__setattr__(self, "capacity_unit", require_non_empty_string(self.capacity_unit, "capacity_unit"))
        object.__setattr__(self, "availability_window", _window(self.availability_window, "availability_window"))
        if not isinstance(self.available, bool):
            raise ContractValidationError("available must be boolean")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], depot_node: NodeId) -> "Vehicle":
        if not isinstance(payload, Mapping):
            raise ContractValidationError("vehicle must be a mapping")
        return cls(
            vehicle_id=payload.get("vehicle_id", payload.get("id")),
            start_node=payload.get("start_node", depot_node),
            end_node=payload.get("end_node", depot_node),
            capacity=payload.get("capacity"),
            capacity_unit=payload.get("unit", payload.get("capacity_unit")),
            availability_window=payload.get(
                "availability_window", payload.get("working_time_window")
            ),
            current_load=payload.get("current_load", 0.0),
            available=payload.get("available", payload.get("availability", True)),
        )


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    location: NodeId
    demand: float
    demand_unit: str
    service_time: float
    time_window: tuple[float, float]
    deadline: float
    earliest_arrival: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", require_non_empty_string(self.order_id, "order_id"))
        object.__setattr__(self, "location", validate_node_id(self.location, "location"))
        object.__setattr__(self, "demand", validate_finite_number(self.demand, "demand", minimum=0.0))
        object.__setattr__(self, "demand_unit", require_non_empty_string(self.demand_unit, "demand_unit"))
        object.__setattr__(self, "service_time", validate_finite_number(self.service_time, "service_time", minimum=0.0))
        object.__setattr__(self, "time_window", _window(self.time_window, "time_window"))
        object.__setattr__(self, "deadline", validate_finite_number(self.deadline, "deadline", minimum=0.0))
        object.__setattr__(self, "earliest_arrival", validate_finite_number(self.earliest_arrival, "earliest_arrival", minimum=0.0))

    @property
    def effective_window(self) -> tuple[float, float]:
        """Arrival window with the completion-deadline upper bound applied."""

        return (
            max(self.time_window[0], self.earliest_arrival),
            min(self.time_window[1], self.deadline - self.service_time),
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "Order":
        if not isinstance(payload, Mapping):
            raise ContractValidationError("order must be a mapping")
        return cls(
            order_id=payload.get("order_id", payload.get("id")),
            location=payload.get("location"),
            demand=payload.get("demand"),
            demand_unit=payload.get("unit", payload.get("demand_unit")),
            service_time=payload.get("service_time"),
            time_window=payload.get("time_window"),
            deadline=payload.get("deadline", payload.get("hard_deadline")),
            earliest_arrival=payload.get("earliest_arrival", 0.0),
        )


@dataclass(frozen=True, slots=True)
class ConstraintDimensions:
    time: Any
    capacity: Any


def validate_feasibility_contracts(
    bundle: MatrixBundle,
    scenario: Scenario,
    vehicles: Sequence[Vehicle],
    orders: Sequence[Order],
) -> None:
    """Validate nodes, depot policy, identifiers, and demand-unit consistency."""

    if not vehicles:
        raise ContractValidationError("at least one available vehicle is required")
    if len({vehicle.vehicle_id for vehicle in vehicles}) != len(vehicles):
        raise ContractValidationError("vehicle_id values must be unique")
    if len({order.order_id for order in orders}) != len(orders):
        raise ContractValidationError("order_id values must be unique")
    nodes = set(bundle.node_order)
    if scenario.depot.node_id not in nodes:
        raise ContractValidationError("depot node is absent from MatrixBundle.node_order")
    for vehicle in vehicles:
        if vehicle.start_node not in nodes or vehicle.end_node not in nodes:
            raise ContractValidationError(
                f"vehicle {vehicle.vehicle_id} start/end node is absent from MatrixBundle.node_order"
            )
        if not scenario.allow_non_depot_start_end and (
            vehicle.start_node != scenario.depot.node_id
            or vehicle.end_node != scenario.depot.node_id
        ):
            raise ContractValidationError(
                f"vehicle {vehicle.vehicle_id} must start and end at the scenario depot"
            )
    for order in orders:
        if order.location not in nodes:
            raise ContractValidationError(
                f"order {order.order_id} location is absent from MatrixBundle.node_order"
            )
    units = {vehicle.capacity_unit for vehicle in vehicles}
    units.update(order.demand_unit for order in orders)
    if len(units) > 1:
        raise ContractValidationError(
            "order demand and vehicle capacity must use one common unit"
        )


def attach_constraints(
    *,
    routing: Any,
    manager: Any,
    time_callback_index: int,
    order_routing_nodes: Mapping[str, int],
    order_by_id: Mapping[str, Order],
    vehicles: Sequence[Vehicle],
    scenario: Scenario,
    scaling: IntegerScalingPolicy,
    max_waiting_time_seconds: float,
) -> ConstraintDimensions:
    """Attach Time and Capacity dimensions and hard feasibility rules."""

    max_latest = max(
        [vehicle.availability_window[1] for vehicle in vehicles]
        + [order.deadline for order in order_by_id.values()]
        + [0.0]
    )
    time_horizon = scaling.to_solver(
        max_latest + max_waiting_time_seconds, "time"
    )
    routing.AddDimension(
        time_callback_index,
        scaling.to_solver(max_waiting_time_seconds, "time"),
        time_horizon,
        False,
        "Time",
    )
    time_dimension = routing.GetDimensionOrDie("Time")

    for order_id, routing_node in order_routing_nodes.items():
        order = order_by_id[order_id]
        earliest, latest = order.effective_window
        index = manager.NodeToIndex(routing_node)
        if earliest <= latest:
            time_dimension.CumulVar(index).SetRange(
                scaling.to_solver(earliest, "time"),
                scaling.to_solver(latest, "time"),
            )
        elif scenario.allow_unassigned_orders:
            routing.ActiveVar(index).SetValue(0)
        else:
            raise ContractValidationError(
                f"order {order.order_id} has an impossible deadline/time window"
            )

    for vehicle_index, vehicle in enumerate(vehicles):
        earliest = scaling.to_solver(vehicle.availability_window[0], "time")
        latest = scaling.to_solver(vehicle.availability_window[1], "time")
        time_dimension.CumulVar(routing.Start(vehicle_index)).SetRange(earliest, latest)
        time_dimension.CumulVar(routing.End(vehicle_index)).SetRange(earliest, latest)
        routing.AddVariableMinimizedByFinalizer(
            time_dimension.CumulVar(routing.Start(vehicle_index))
        )
        routing.AddVariableMinimizedByFinalizer(
            time_dimension.CumulVar(routing.End(vehicle_index))
        )

    demand_by_routing_node = {
        routing_node: scaling.to_solver(order_by_id[order_id].demand, "demand")
        for order_id, routing_node in order_routing_nodes.items()
    }

    def demand_callback(routing_index: int) -> int:
        return demand_by_routing_node.get(manager.IndexToNode(routing_index), 0)

    demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_callback_index,
        0,
        [scaling.to_solver(vehicle.capacity, "demand") for vehicle in vehicles],
        False,
        "Capacity",
    )
    capacity_dimension = routing.GetDimensionOrDie("Capacity")
    for vehicle_index, vehicle in enumerate(vehicles):
        start_load = scaling.to_solver(vehicle.current_load, "demand")
        capacity_dimension.CumulVar(routing.Start(vehicle_index)).SetRange(
            start_load, start_load
        )

    return ConstraintDimensions(time_dimension, capacity_dimension)
