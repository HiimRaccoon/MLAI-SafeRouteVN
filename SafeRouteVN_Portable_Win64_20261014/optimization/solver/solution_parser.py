"""Convert an OR-Tools assignment into the frozen VRPTW solution contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from optimization.models import Diagnostic, DiagnosticSeverity, MatrixBundle
from optimization.models.common import (
    ContractValidationError,
    JsonValue,
    NodeId,
    require_non_empty_string,
    validate_finite_number,
    validate_node_id,
)

from .callbacks import IntegerScalingPolicy
from .constraints import Order, Scenario, Vehicle


ALLOWED_FEASIBILITY_STATUSES = frozenset(
    {"FEASIBLE", "INFEASIBLE", "TIME_LIMIT", "INVALID_INPUT", "ERROR"}
)


@dataclass(frozen=True, slots=True)
class RouteStop:
    order_id: str
    node_id: NodeId
    arrival_time: float
    departure_time: float
    cumulative_load: float

    def __post_init__(self) -> None:
        require_non_empty_string(self.order_id, "order_id")
        validate_node_id(self.node_id, "node_id")
        arrival = validate_finite_number(self.arrival_time, "arrival_time", minimum=0.0)
        departure = validate_finite_number(self.departure_time, "departure_time", minimum=0.0)
        validate_finite_number(self.cumulative_load, "cumulative_load", minimum=0.0)
        if departure < arrival:
            raise ContractValidationError("departure_time must not precede arrival_time")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "order_id": self.order_id,
            "node_id": self.node_id,
            "arrival_time": self.arrival_time,
            "departure_time": self.departure_time,
            "cumulative_load": self.cumulative_load,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RouteStop":
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class RouteEdgeMetric:
    from_node: NodeId
    to_node: NodeId
    matrix_row: int
    matrix_column: int
    distance: float
    time: float
    risk_exposure: float
    geometry_available: bool
    source_path_id: str | None

    def __post_init__(self) -> None:
        validate_node_id(self.from_node, "from_node")
        validate_node_id(self.to_node, "to_node")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (self.matrix_row, self.matrix_column)):
            raise ContractValidationError("matrix row/column must be non-negative integers")
        for name in ("distance", "time", "risk_exposure"):
            validate_finite_number(getattr(self, name), name, minimum=0.0)
        if not isinstance(self.geometry_available, bool):
            raise ContractValidationError("geometry_available must be boolean")
        if self.source_path_id is not None:
            require_non_empty_string(self.source_path_id, "source_path_id")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "from_node": self.from_node,
            "to_node": self.to_node,
            "matrix_row": self.matrix_row,
            "matrix_column": self.matrix_column,
            "distance": self.distance,
            "time": self.time,
            "risk_exposure": self.risk_exposure,
            "geometry_available": self.geometry_available,
            "source_path_id": self.source_path_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RouteEdgeMetric":
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class VehicleRoute:
    vehicle_id: str
    used: bool
    start_node: NodeId
    end_node: NodeId
    node_sequence: tuple[NodeId, ...]
    order_sequence: tuple[str, ...]
    stops: tuple[RouteStop, ...]
    edge_metrics: tuple[RouteEdgeMetric, ...]
    distance: float
    time: float
    risk_exposure: float
    start_time: float = 0.0
    end_time: float = 0.0
    travel_time: float = 0.0
    service_time: float = 0.0
    waiting_time: float = 0.0

    def __post_init__(self) -> None:
        require_non_empty_string(self.vehicle_id, "vehicle_id")
        validate_node_id(self.start_node, "start_node")
        validate_node_id(self.end_node, "end_node")
        if not isinstance(self.used, bool):
            raise ContractValidationError("used must be boolean")
        if not self.node_sequence:
            raise ContractValidationError("node_sequence must not be empty")
        for name in (
            "distance", "time", "risk_exposure", "start_time", "end_time",
            "travel_time", "service_time", "waiting_time",
        ):
            validate_finite_number(getattr(self, name), name, minimum=0.0)
        if self.end_time < self.start_time:
            raise ContractValidationError("end_time must not precede start_time")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "vehicle_id": self.vehicle_id,
            "used": self.used,
            "start_node": self.start_node,
            "end_node": self.end_node,
            "node_sequence": list(self.node_sequence),
            "order_sequence": list(self.order_sequence),
            "stops": [stop.to_dict() for stop in self.stops],
            "edge_metrics": [edge.to_dict() for edge in self.edge_metrics],
            "distance": self.distance,
            "time": self.time,
            "risk_exposure": self.risk_exposure,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "travel_time": self.travel_time,
            "service_time": self.service_time,
            "waiting_time": self.waiting_time,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VehicleRoute":
        return cls(
            vehicle_id=payload["vehicle_id"],
            used=payload["used"],
            start_node=payload["start_node"],
            end_node=payload["end_node"],
            node_sequence=tuple(payload["node_sequence"]),
            order_sequence=tuple(payload["order_sequence"]),
            stops=tuple(RouteStop.from_dict(item) for item in payload["stops"]),
            edge_metrics=tuple(
                RouteEdgeMetric.from_dict(item) for item in payload["edge_metrics"]
            ),
            distance=payload["distance"],
            time=payload["time"],
            risk_exposure=payload["risk_exposure"],
            start_time=payload.get("start_time", 0.0),
            end_time=payload.get("end_time", payload["time"]),
            travel_time=payload.get(
                "travel_time", sum(item["time"] for item in payload["edge_metrics"])
            ),
            service_time=payload.get(
                "service_time",
                sum(item["departure_time"] - item["arrival_time"] for item in payload["stops"]),
            ),
            waiting_time=payload.get(
                "waiting_time",
                max(
                    0.0,
                    payload["time"]
                    - sum(item["time"] for item in payload["edge_metrics"])
                    - sum(item["departure_time"] - item["arrival_time"] for item in payload["stops"]),
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class UnservedOrder:
    order_id: str
    reason: str

    def __post_init__(self) -> None:
        require_non_empty_string(self.order_id, "order_id")
        require_non_empty_string(self.reason, "reason")

    def to_dict(self) -> dict[str, str]:
        return {"order_id": self.order_id, "reason": self.reason}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "UnservedOrder":
        return cls(payload["order_id"], payload["reason"])


@dataclass(frozen=True, slots=True)
class VehicleUtilization:
    total_vehicles: int
    used_vehicles: int
    utilization_ratio: float

    def __post_init__(self) -> None:
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (self.total_vehicles, self.used_vehicles)):
            raise ContractValidationError("vehicle counts must be non-negative integers")
        if self.used_vehicles > self.total_vehicles:
            raise ContractValidationError("used_vehicles must not exceed total_vehicles")
        ratio = validate_finite_number(self.utilization_ratio, "utilization_ratio", minimum=0.0)
        if ratio > 1.0:
            raise ContractValidationError("utilization_ratio must not exceed 1")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "total_vehicles": self.total_vehicles,
            "used_vehicles": self.used_vehicles,
            "utilization_ratio": self.utilization_ratio,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VehicleUtilization":
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class NormalizedReadyMetrics:
    distance_available: bool
    time_available: bool
    risk_available: bool

    def __post_init__(self) -> None:
        if not all(isinstance(value, bool) for value in (self.distance_available, self.time_available, self.risk_available)):
            raise ContractValidationError("normalization readiness flags must be boolean")

    def to_dict(self) -> dict[str, bool]:
        return {
            "distance_available": self.distance_available,
            "time_available": self.time_available,
            "risk_available": self.risk_available,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NormalizedReadyMetrics":
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class ConstraintStatus:
    capacity_valid: bool
    time_window_valid: bool
    deadline_valid: bool

    def __post_init__(self) -> None:
        if not all(isinstance(value, bool) for value in (self.capacity_valid, self.time_window_valid, self.deadline_valid)):
            raise ContractValidationError("constraint status fields must be boolean")

    @property
    def is_valid(self) -> bool:
        return self.capacity_valid and self.time_window_valid and self.deadline_valid

    def to_dict(self) -> dict[str, bool]:
        return {
            "capacity_valid": self.capacity_valid,
            "time_window_valid": self.time_window_valid,
            "deadline_valid": self.deadline_valid,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ConstraintStatus":
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class SolverExperimentMetadata:
    ortools_version: str
    solver_config_version: str
    random_seed: int
    time_limit_seconds: int
    first_solution_strategy: str
    local_search_metaheuristic: str
    execution_timestamp: str
    elapsed_time_seconds: float
    number_of_vehicles: int
    number_of_orders: int
    problem_size: int
    solver_status: str
    best_solution_found: bool
    solution_quality: str
    timeout_outcome: str | None = None
    routing_cost_mode: str = "DISTANCE"
    routing_objective_value: int | None = None
    cost_domain_version: str = "legacy-distance-cost-domain-e26-v1"
    scaling_version: str = "distance-scaling-e26-v1"
    penalty_policy_version: str = "distance-scaled-disjunction-e26-v1"
    fleet_validation_version: str = "legacy-unvalidated-fleet-domain-v1"

    def __post_init__(self) -> None:
        for name in (
            "ortools_version", "solver_config_version", "first_solution_strategy",
            "local_search_metaheuristic", "execution_timestamp", "solver_status",
            "solution_quality",
            "routing_cost_mode",
            "cost_domain_version", "scaling_version", "penalty_policy_version",
            "fleet_validation_version",
        ):
            require_non_empty_string(getattr(self, name), name)
        for name in ("random_seed", "time_limit_seconds", "number_of_vehicles", "number_of_orders", "problem_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"{name} must be a non-negative integer")
        if self.time_limit_seconds == 0:
            raise ContractValidationError("time_limit_seconds must be positive")
        if self.problem_size != self.number_of_vehicles + self.number_of_orders:
            raise ContractValidationError("problem_size must equal vehicle count plus order count")
        validate_finite_number(self.elapsed_time_seconds, "elapsed_time_seconds", minimum=0.0)
        if not isinstance(self.best_solution_found, bool):
            raise ContractValidationError("best_solution_found must be boolean")
        if self.timeout_outcome not in {None, "BEST_FEASIBLE_SOLUTION", "TIME_LIMIT_NO_SOLUTION"}:
            raise ContractValidationError("unsupported timeout_outcome")
        if self.routing_objective_value is not None and (
            isinstance(self.routing_objective_value, bool)
            or not isinstance(self.routing_objective_value, int)
            or self.routing_objective_value < 0
        ):
            raise ContractValidationError("routing_objective_value must be a non-negative integer or null")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "ortools_version": self.ortools_version,
            "solver_config_version": self.solver_config_version,
            "random_seed": self.random_seed,
            "time_limit_seconds": self.time_limit_seconds,
            "first_solution_strategy": self.first_solution_strategy,
            "local_search_metaheuristic": self.local_search_metaheuristic,
            "execution_timestamp": self.execution_timestamp,
            "elapsed_time_seconds": self.elapsed_time_seconds,
            "number_of_vehicles": self.number_of_vehicles,
            "number_of_orders": self.number_of_orders,
            "problem_size": self.problem_size,
            "solver_status": self.solver_status,
            "best_solution_found": self.best_solution_found,
            "solution_quality": self.solution_quality,
            "timeout_outcome": self.timeout_outcome,
            "routing_cost_mode": self.routing_cost_mode,
            "routing_objective_value": self.routing_objective_value,
            "cost_domain_version": self.cost_domain_version,
            "scaling_version": self.scaling_version,
            "penalty_policy_version": self.penalty_policy_version,
            "fleet_validation_version": self.fleet_validation_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SolverExperimentMetadata":
        values = dict(payload)
        values.setdefault("routing_cost_mode", "DISTANCE")
        values.setdefault("routing_objective_value", None)
        values.setdefault("cost_domain_version", "legacy-distance-cost-domain-e26-v1")
        values.setdefault("scaling_version", "distance-scaling-e26-v1")
        values.setdefault("penalty_policy_version", "distance-scaled-disjunction-e26-v1")
        values.setdefault("fleet_validation_version", "legacy-unvalidated-fleet-domain-v1")
        return cls(**values)


@dataclass(frozen=True, slots=True)
class VRPTWSolution:
    solution_id: str
    scenario_id: str
    solver_version: str
    solver_config_version: str
    feasibility_status: str
    vehicle_routes: tuple[VehicleRoute, ...]
    served_orders: tuple[str, ...]
    unserved_orders: tuple[UnservedOrder, ...]
    total_distance: float
    total_time: float
    total_risk_exposure: float
    vehicle_utilization: VehicleUtilization
    normalized_ready_metrics: NormalizedReadyMetrics
    constraint_status: ConstraintStatus
    diagnostics: tuple[Diagnostic, ...]
    matrix_version: str
    context_version: str
    scenario_version: str
    experiment_metadata: SolverExperimentMetadata
    schema_version: str = "2.0.0"

    def __post_init__(self) -> None:
        if self.feasibility_status not in ALLOWED_FEASIBILITY_STATUSES:
            raise ContractValidationError(
                f"unsupported feasibility_status: {self.feasibility_status}"
            )
        for field_name in (
            "solution_id",
            "scenario_id",
            "solver_version",
            "solver_config_version",
            "matrix_version",
            "context_version",
            "scenario_version",
            "schema_version",
        ):
            require_non_empty_string(getattr(self, field_name), field_name)
        for field_name in ("total_distance", "total_time", "total_risk_exposure"):
            validate_finite_number(getattr(self, field_name), field_name, minimum=0.0)

    @property
    def solver_configuration_version(self) -> str:
        return self.solver_config_version

    @property
    def is_feasible(self) -> bool:
        return self.feasibility_status == "FEASIBLE" or (
            self.feasibility_status == "TIME_LIMIT"
            and self.experiment_metadata.best_solution_found
        )

    @property
    def business_status(self) -> str:
        """Stable business mapping while preserving the Phase E.2.5 status field."""

        if self.feasibility_status == "TIME_LIMIT":
            return (
                "TIME_LIMIT_WITH_SOLUTION"
                if self.experiment_metadata.best_solution_found
                else "TIME_LIMIT_NO_SOLUTION"
            )
        return self.feasibility_status

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "solution_id": self.solution_id,
            "scenario_id": self.scenario_id,
            "solver_version": self.solver_version,
            "solver_config_version": self.solver_config_version,
            "feasibility_status": self.feasibility_status,
            "business_status": self.business_status,
            "vehicle_routes": [route.to_dict() for route in self.vehicle_routes],
            "served_orders": list(self.served_orders),
            "unserved_orders": [item.to_dict() for item in self.unserved_orders],
            "total_distance": self.total_distance,
            "total_time": self.total_time,
            "total_risk_exposure": self.total_risk_exposure,
            "vehicle_utilization": self.vehicle_utilization.to_dict(),
            "normalized_ready_metrics": self.normalized_ready_metrics.to_dict(),
            "constraint_status": self.constraint_status.to_dict(),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "matrix_version": self.matrix_version,
            "context_version": self.context_version,
            "scenario_version": self.scenario_version,
            "experiment_metadata": self.experiment_metadata.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VRPTWSolution":
        return cls(
            solution_id=payload["solution_id"],
            scenario_id=payload["scenario_id"],
            solver_version=payload["solver_version"],
            solver_config_version=payload["solver_config_version"],
            feasibility_status=payload["feasibility_status"],
            vehicle_routes=tuple(
                VehicleRoute.from_dict(item) for item in payload["vehicle_routes"]
            ),
            served_orders=tuple(payload["served_orders"]),
            unserved_orders=tuple(
                UnservedOrder.from_dict(item) for item in payload["unserved_orders"]
            ),
            total_distance=payload["total_distance"],
            total_time=payload["total_time"],
            total_risk_exposure=payload["total_risk_exposure"],
            vehicle_utilization=VehicleUtilization.from_dict(
                payload["vehicle_utilization"]
            ),
            normalized_ready_metrics=NormalizedReadyMetrics.from_dict(
                payload["normalized_ready_metrics"]
            ),
            constraint_status=ConstraintStatus.from_dict(payload["constraint_status"]),
            diagnostics=tuple(Diagnostic.from_dict(item) for item in payload["diagnostics"]),
            matrix_version=payload["matrix_version"],
            context_version=payload["context_version"],
            scenario_version=payload["scenario_version"],
            experiment_metadata=SolverExperimentMetadata.from_dict(
                payload["experiment_metadata"]
            ),
            schema_version=payload.get("schema_version", "2.0.0"),
        )

    def to_decision_handoff(self) -> dict[str, JsonValue]:
        """Return a profile-neutral payload for future DecisionResult mapping."""

        return {
            "solution_id": self.solution_id,
            "routes": [route.to_dict() for route in self.vehicle_routes],
            "metrics": {
                "distance": self.total_distance,
                "travel_time": self.total_time,
                "relative_exposure": self.total_risk_exposure,
            },
            "served_orders": list(self.served_orders),
            "unserved_orders": [item.to_dict() for item in self.unserved_orders],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "provenance": {
                "matrix_version": self.matrix_version,
                "context_version": self.context_version,
                "scenario_version": self.scenario_version,
                "solver_config_version": self.solver_config_version,
            },
        }


def failure_solution(
    *,
    scenario_id: str,
    scenario_version: str,
    context_version: str,
    status: str,
    orders: Sequence[Order],
    reason: str,
    diagnostics: Sequence[Diagnostic],
    solver_version: str,
    solver_config_version: str,
    matrix_version: str,
    experiment_metadata: SolverExperimentMetadata,
    metrics_available: bool = False,
) -> VRPTWSolution:
    payload = f"{scenario_id}|{status}|{matrix_version}|{reason}|{solver_config_version}"
    return VRPTWSolution(
        solution_id="solution_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
        scenario_id=scenario_id,
        solver_version=solver_version,
        solver_config_version=solver_config_version,
        feasibility_status=status,
        vehicle_routes=(),
        served_orders=(),
        unserved_orders=tuple(UnservedOrder(order.order_id, reason) for order in orders),
        total_distance=0.0,
        total_time=0.0,
        total_risk_exposure=0.0,
        vehicle_utilization=VehicleUtilization(
            experiment_metadata.number_of_vehicles, 0, 0.0
        ),
        normalized_ready_metrics=NormalizedReadyMetrics(
            metrics_available, metrics_available, metrics_available
        ),
        constraint_status=ConstraintStatus(False, False, False),
        diagnostics=tuple(diagnostics),
        matrix_version=matrix_version,
        context_version=context_version,
        scenario_version=scenario_version,
        experiment_metadata=experiment_metadata,
    )


def _unserved_reason(
    order: Order,
    vehicles: Sequence[Vehicle],
    matrix: MatrixBundle,
    *,
    aggregate_capacity_shortfall: bool,
) -> str:
    earliest, latest = order.effective_window
    if earliest > latest:
        return "impossible_deadline"
    residual = [vehicle.capacity - vehicle.current_load for vehicle in vehicles]
    if not residual or order.demand > max(residual) or aggregate_capacity_shortfall:
        return "insufficient_capacity"
    order_column = matrix.node_order.index(order.location)
    for vehicle in vehicles:
        start_row = matrix.node_order.index(vehicle.start_node)
        earliest_arrival = max(
            vehicle.availability_window[0] + matrix.time_matrix[start_row][order_column],
            earliest,
        )
        if earliest_arrival <= min(latest, vehicle.availability_window[1]):
            return "solver_unassigned"
    return "impossible_time_window"


def parse_solution(
    *,
    routing: Any,
    manager: Any,
    assignment: Any,
    dimensions: Any,
    node_mapping: Any,
    matrix_bundle: MatrixBundle,
    scenario: Scenario,
    vehicles: Sequence[Vehicle],
    orders: Sequence[Order],
    scaling: IntegerScalingPolicy,
    diagnostics: Sequence[Diagnostic],
    solver_version: str,
    solver_config_version: str,
    feasibility_status: str,
    experiment_metadata: SolverExperimentMetadata,
) -> VRPTWSolution:
    """Extract ordered routes, original-unit metrics, and edge risk provenance."""

    order_by_id = {order.order_id: order for order in orders}
    served: set[str] = set()
    routes: list[VehicleRoute] = []
    total_distance = total_time = total_risk = 0.0

    for vehicle_index, vehicle in enumerate(vehicles):
        index = routing.Start(vehicle_index)
        start_time = scaling.from_solver(
            assignment.Value(dimensions.time.CumulVar(index)), "time"
        )
        node_sequence: list[NodeId] = []
        order_sequence: list[str] = []
        stops: list[RouteStop] = []
        edge_metrics: list[RouteEdgeMetric] = []
        route_distance = route_risk = 0.0
        route_travel = 0.0

        while not routing.IsEnd(index):
            routing_node = manager.IndexToNode(index)
            matrix_index = node_mapping.routing_node_to_matrix_index[routing_node]
            node_sequence.append(matrix_bundle.node_order[matrix_index])
            order_id = node_mapping.order_id_by_routing_node.get(routing_node)
            if order_id is not None:
                order = order_by_id[order_id]
                arrival = scaling.from_solver(
                    assignment.Value(dimensions.time.CumulVar(index)), "time"
                )
                load_before = scaling.from_solver(
                    assignment.Value(dimensions.capacity.CumulVar(index)), "demand"
                )
                stops.append(
                    RouteStop(
                        order_id,
                        order.location,
                        arrival,
                        arrival + order.service_time,
                        load_before + order.demand,
                    )
                )
                order_sequence.append(order_id)
                served.add(order_id)
            next_index = assignment.Value(routing.NextVar(index))
            next_node = manager.IndexToNode(next_index)
            next_matrix_index = node_mapping.routing_node_to_matrix_index[next_node]
            edge_distance = matrix_bundle.distance_matrix[matrix_index][next_matrix_index]
            edge_time = matrix_bundle.time_matrix[matrix_index][next_matrix_index]
            edge_risk = matrix_bundle.risk_matrix[matrix_index][next_matrix_index]
            provenance = matrix_bundle.entry_provenance[matrix_index][next_matrix_index]
            edge_metrics.append(
                RouteEdgeMetric(
                    matrix_bundle.node_order[matrix_index],
                    matrix_bundle.node_order[next_matrix_index],
                    matrix_index,
                    next_matrix_index,
                    edge_distance,
                    edge_time,
                    edge_risk,
                    bool(matrix_bundle.geometry_matrix[matrix_index][next_matrix_index]),
                    provenance.get("selected_path_id"),
                )
            )
            route_distance += edge_distance
            route_travel += edge_time
            route_risk += edge_risk
            index = next_index

        end_routing_node = manager.IndexToNode(index)
        end_matrix_index = node_mapping.routing_node_to_matrix_index[end_routing_node]
        node_sequence.append(matrix_bundle.node_order[end_matrix_index])
        end_time = scaling.from_solver(
            assignment.Value(dimensions.time.CumulVar(index)), "time"
        )
        route_time = end_time - start_time
        route_service = sum(order_by_id[order_id].service_time for order_id in order_sequence)
        route_waiting = route_time - route_travel - route_service
        if route_waiting < -1e-6:
            raise ContractValidationError(
                f"vehicle {vehicle.vehicle_id} has inconsistent Time Dimension totals"
            )
        route_waiting = max(0.0, route_waiting)
        total_distance += route_distance
        total_time += route_time
        total_risk += route_risk
        routes.append(
            VehicleRoute(
                vehicle.vehicle_id,
                bool(routing.IsVehicleUsed(assignment, vehicle_index)),
                vehicle.start_node,
                vehicle.end_node,
                tuple(node_sequence),
                tuple(order_sequence),
                tuple(stops),
                tuple(edge_metrics),
                route_distance,
                route_time,
                route_risk,
                start_time,
                end_time,
                route_travel,
                route_service,
                route_waiting,
            )
        )

    total_demand = sum(order.demand for order in orders)
    residual = sum(vehicle.capacity - vehicle.current_load for vehicle in vehicles)
    unserved = tuple(
        UnservedOrder(
            order.order_id,
            _unserved_reason(
                order,
                vehicles,
                matrix_bundle,
                aggregate_capacity_shortfall=total_demand > residual,
            ),
        )
        for order in orders
        if order.order_id not in served
    )
    result_diagnostics = list(diagnostics)
    if unserved:
        result_diagnostics.append(
            Diagnostic(
                "UNSERVED_ORDERS",
                DiagnosticSeverity.WARNING,
                "The solver returned explicit unserved orders.",
                {"orders": [item.to_dict() for item in unserved]},
            )
        )
    used_count = sum(route.used for route in routes)
    identity = json.dumps(
        {
            "scenario_id": scenario.scenario_id,
            "scenario_version": scenario.scenario_version,
            "matrix_version": matrix_bundle.matrix_version,
            "solver_config_version": solver_config_version,
            "routes": [route.to_dict() for route in routes],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return VRPTWSolution(
        solution_id="solution_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
        scenario_id=scenario.scenario_id,
        solver_version=solver_version,
        solver_config_version=solver_config_version,
        feasibility_status=feasibility_status,
        vehicle_routes=tuple(routes),
        served_orders=tuple(sorted(served)),
        unserved_orders=unserved,
        total_distance=total_distance,
        total_time=total_time,
        total_risk_exposure=total_risk,
        vehicle_utilization=VehicleUtilization(
            len(routes), used_count, used_count / len(routes) if routes else 0.0
        ),
        normalized_ready_metrics=NormalizedReadyMetrics(True, True, True),
        constraint_status=ConstraintStatus(True, True, True),
        diagnostics=tuple(result_diagnostics),
        matrix_version=matrix_bundle.matrix_version,
        context_version=str(matrix_bundle.source.get("context_version", "UNVERSIONED")),
        scenario_version=scenario.scenario_version,
        experiment_metadata=experiment_metadata,
    )
