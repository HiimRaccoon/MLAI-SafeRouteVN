"""Independent post-solution validation for the frozen VRPTW contract."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isclose
from typing import Sequence

from optimization.models import Diagnostic, DiagnosticSeverity, MatrixBundle

from .constraints import Order, Scenario, Vehicle
from .solution_parser import ConstraintStatus, VRPTWSolution


@dataclass(frozen=True, slots=True)
class SolutionValidationResult:
    solution: VRPTWSolution
    is_valid: bool
    diagnostics: tuple[Diagnostic, ...]


def validate_solution(
    solution: VRPTWSolution,
    matrix_bundle: MatrixBundle,
    scenario: Scenario,
    vehicles: Sequence[Vehicle],
    orders: Sequence[Order],
) -> SolutionValidationResult:
    """Reject malformed solver output before it crosses the TASK-02 boundary."""

    errors: list[Diagnostic] = []
    capacity_valid = time_window_valid = deadline_valid = True
    vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in vehicles}
    order_by_id = {order.order_id: order for order in orders}
    node_indices = {node: index for index, node in enumerate(matrix_bundle.node_order)}
    seen_vehicles: set[str] = set()
    seen_orders: set[str] = set()

    def report(code: str, message: str, **details: object) -> None:
        errors.append(
            Diagnostic(
                code,
                DiagnosticSeverity.ERROR,
                message,
                details,
                {"component": "solution_validator", **details},
            )
        )

    for route in solution.vehicle_routes:
        vehicle = vehicle_by_id.get(route.vehicle_id)
        if vehicle is None or route.vehicle_id in seen_vehicles:
            report("INVALID_VEHICLE_ASSIGNMENT", "Route vehicle is missing or duplicated.", vehicle_id=route.vehicle_id)
            continue
        seen_vehicles.add(route.vehicle_id)
        if route.start_node != vehicle.start_node or route.end_node != vehicle.end_node:
            report("ROUTE_ENDPOINT_METADATA_MISMATCH", "Route endpoint metadata differs from the assigned vehicle.", vehicle_id=route.vehicle_id)
        if not route.node_sequence or route.node_sequence[0] != vehicle.start_node:
            report("INVALID_ROUTE_START", "Route does not start at its configured vehicle start.", vehicle_id=route.vehicle_id)
        if not route.node_sequence or route.node_sequence[-1] != vehicle.end_node:
            report("INVALID_ROUTE_END", "Route does not end at its configured vehicle end.", vehicle_id=route.vehicle_id)
        if not scenario.allow_non_depot_start_end and (
            route.start_node != scenario.depot.node_id or route.end_node != scenario.depot.node_id
        ):
            report("DEPOT_CONTRACT_VIOLATION", "Route start/end violates the scenario depot contract.", vehicle_id=route.vehicle_id)
        if len(route.edge_metrics) != max(0, len(route.node_sequence) - 1):
            report("INVALID_ROUTE_SEQUENCE", "Route edge count does not match its node sequence.", vehicle_id=route.vehicle_id)

        computed_distance = computed_travel = computed_risk = 0.0
        for edge_index, (from_node, to_node) in enumerate(zip(route.node_sequence, route.node_sequence[1:])):
            if from_node not in node_indices or to_node not in node_indices:
                report("UNKNOWN_ROUTE_NODE", "Route contains a node absent from MatrixBundle.", from_node=from_node, to_node=to_node)
                continue
            row, column = node_indices[from_node], node_indices[to_node]
            if edge_index >= len(route.edge_metrics):
                continue
            edge = route.edge_metrics[edge_index]
            geometry_required = row != column
            expected_source = matrix_bundle.entry_provenance[row][column].get("selected_path_id")
            if (
                edge.from_node != from_node
                or edge.to_node != to_node
                or edge.matrix_row != row
                or edge.matrix_column != column
                or not isclose(edge.distance, matrix_bundle.distance_matrix[row][column])
                or not isclose(edge.time, matrix_bundle.time_matrix[row][column])
                or not isclose(edge.risk_exposure, matrix_bundle.risk_matrix[row][column])
                or edge.source_path_id != expected_source
                or (geometry_required and not edge.geometry_available)
                or (geometry_required and not matrix_bundle.geometry_matrix[row][column])
            ):
                report("MATRIX_CONTINUITY_VIOLATION", "Route edge does not match D/T/R/G and provenance.", vehicle_id=route.vehicle_id, edge_index=edge_index)
            computed_distance += matrix_bundle.distance_matrix[row][column]
            computed_travel += matrix_bundle.time_matrix[row][column]
            computed_risk += matrix_bundle.risk_matrix[row][column]
        if (
            not isclose(route.distance, computed_distance)
            or not isclose(route.travel_time, computed_travel)
            or not isclose(route.risk_exposure, computed_risk)
        ):
            report("ROUTE_METRIC_MISMATCH", "Route totals do not match MatrixBundle route edges.", vehicle_id=route.vehicle_id)

        load = vehicle.current_load
        if tuple(stop.order_id for stop in route.stops) != route.order_sequence:
            report("ORDER_SEQUENCE_MISMATCH", "Ordered stops do not match route order_sequence.", vehicle_id=route.vehicle_id)
        cursor = 1
        for stop in route.stops:
            order = order_by_id.get(stop.order_id)
            if order is None:
                report("UNKNOWN_SERVED_ORDER", "A served order does not exist in solver input.", order_id=stop.order_id)
                continue
            if stop.order_id in seen_orders:
                report("DUPLICATE_ORDER_SERVICE", "An order is served more than once.", order_id=stop.order_id)
            seen_orders.add(stop.order_id)
            if stop.node_id != order.location:
                report("ORDER_LOCATION_MISMATCH", "Served order location does not match input.", order_id=stop.order_id)
            try:
                cursor = route.node_sequence.index(stop.node_id, cursor, len(route.node_sequence) - 1) + 1
            except ValueError:
                report("STOP_SEQUENCE_VIOLATION", "Ordered stop does not occur in route node order.", order_id=stop.order_id, vehicle_id=route.vehicle_id)
            load += order.demand
            if load > vehicle.capacity + 1e-9 or not isclose(stop.cumulative_load, load):
                capacity_valid = False
                report("CAPACITY_VIOLATION", "Route load exceeds or disagrees with vehicle capacity.", order_id=stop.order_id, vehicle_id=vehicle.vehicle_id)
            earliest = max(order.time_window[0], order.earliest_arrival)
            if stop.arrival_time < earliest - 1e-9 or stop.arrival_time > order.time_window[1] + 1e-9:
                time_window_valid = False
                report("TIME_WINDOW_VIOLATION", "Arrival is outside the customer time window.", order_id=stop.order_id)
            if not isclose(stop.departure_time, stop.arrival_time + order.service_time):
                deadline_valid = False
                report("SERVICE_COMPLETION_MISMATCH", "Departure must equal arrival plus service time.", order_id=stop.order_id)
            if stop.arrival_time + order.service_time > order.deadline + 1e-9:
                deadline_valid = False
                report("COMPLETION_DEADLINE_VIOLATION", "Customer service completion exceeds its deadline.", order_id=stop.order_id)

        if not (
            vehicle.availability_window[0] - 1e-9
            <= route.start_time
            <= route.end_time
            <= vehicle.availability_window[1] + 1e-9
        ):
            time_window_valid = False
            report("VEHICLE_TIME_WINDOW_VIOLATION", "Route timing is outside the vehicle availability window.", vehicle_id=route.vehicle_id)
        computed_service = sum(
            order_by_id[stop.order_id].service_time
            for stop in route.stops
            if stop.order_id in order_by_id
        )
        computed_waiting = 0.0
        current_time = route.start_time
        if len(route.edge_metrics) != len(route.stops) + 1:
            report("TIME_SEQUENCE_SHAPE_INVALID", "A route must contain one more edge than customer stops.", vehicle_id=route.vehicle_id)
        else:
            for edge_index, edge in enumerate(route.edge_metrics):
                earliest_arrival = current_time + edge.time
                if edge_index < len(route.stops):
                    stop = route.stops[edge_index]
                    wait = stop.arrival_time - earliest_arrival
                    current_time = stop.departure_time
                else:
                    wait = route.end_time - earliest_arrival
                if wait < -1e-9:
                    time_window_valid = False
                    report("TIME_CONTINUITY_VIOLATION", "Route timeline is earlier than travel and service permit.", vehicle_id=route.vehicle_id, edge_index=edge_index)
                else:
                    computed_waiting += wait
        if (
            not isclose(route.time, route.end_time - route.start_time)
            or not isclose(route.service_time, computed_service)
            or not isclose(route.waiting_time, computed_waiting)
            or not isclose(route.time, route.travel_time + route.service_time + route.waiting_time)
        ):
            report("TIME_METRIC_MISMATCH", "Route time must equal travel plus waiting plus service without double counting.", vehicle_id=route.vehicle_id)

    reported_served = set(solution.served_orders)
    if len(solution.served_orders) != len(reported_served) or reported_served != seen_orders:
        report("SERVED_ORDER_SET_MISMATCH", "served_orders does not exactly match validated route stops.")
    unserved_ids = [item.order_id for item in solution.unserved_orders]
    if len(unserved_ids) != len(set(unserved_ids)):
        report("DUPLICATE_UNSERVED_ORDER", "unserved_orders contains duplicates.")
    if reported_served.intersection(unserved_ids):
        report("ORDER_BOTH_SERVED_AND_UNSERVED", "An order is reported as both served and unserved.")
    expected_orders = set(order_by_id)
    completed = reported_served.union(unserved_ids)
    if completed != expected_orders or any(not item.reason for item in solution.unserved_orders):
        report("INCOMPLETE_ORDER_ACCOUNTING", "Every input order must be served or explicitly unserved with a reason.", missing=sorted(expected_orders - completed), unknown=sorted(completed - expected_orders))

    route_distance = sum(route.distance for route in solution.vehicle_routes)
    route_time = sum(route.time for route in solution.vehicle_routes)
    route_risk = sum(route.risk_exposure for route in solution.vehicle_routes)
    if not all(
        (
            isclose(solution.total_distance, route_distance),
            isclose(solution.total_time, route_time),
            isclose(solution.total_risk_exposure, route_risk),
        )
    ):
        report("SOLUTION_METRIC_MISMATCH", "Solution totals do not equal the sum of vehicle-route totals.")

    constraint_status = ConstraintStatus(capacity_valid, time_window_valid, deadline_valid)
    valid = not errors and constraint_status.is_valid
    if valid:
        validation_diagnostics = (
            Diagnostic(
                "POST_SOLUTION_VALID",
                DiagnosticSeverity.INFO,
                "VRPTW solution passed independent post-solution validation.",
                {},
                {"component": "solution_validator", "scenario_id": solution.scenario_id},
            ),
        )
        validated = replace(solution, constraint_status=constraint_status, diagnostics=solution.diagnostics + validation_diagnostics)
    else:
        validation_diagnostics = tuple(errors)
        validated = replace(
            solution,
            feasibility_status="ERROR",
            constraint_status=constraint_status,
            diagnostics=solution.diagnostics + validation_diagnostics,
        )
    return SolutionValidationResult(validated, valid, validation_diagnostics)
