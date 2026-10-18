"""Bounded, turn-aware witness search for the M1 S1 initial state only.

This is deliberately separate from the frozen S0 and MatrixBundle solvers.
The geometric stop heuristic proposes orders; only selected raw road paths
establish feasibility. Exhausting a search cap never proves infeasibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import itertools
import json
import math
from time import monotonic
from typing import Any

from optimization.models.decision_state import DecisionState
from .member1_s0_graph import Member1RoadGraph, RoadDataError


SCHEMA_VERSION = "member1-s1-static-solution/1"
SOLVER_VERSION = "s1-bounded-turn-state-search/1"


@dataclass(frozen=True)
class StaticSolverContract:
    """Version/name boundary for the shared bounded static-search core."""

    schema_version: str
    solver_version: str
    supported_scenarios: frozenset[str]
    solution_prefix: str


S1_CONTRACT = StaticSolverContract(
    SCHEMA_VERSION, SOLVER_VERSION, frozenset({"S1"}), "s1",
)


@dataclass(frozen=True)
class S1SearchLimits:
    time_limit_seconds: float = 600.0
    max_route_states: int = 60_000
    max_road_states: int = 250_000
    max_road_labels: int = 500_000
    max_arrival_options: int = 4
    max_assignments: int = 128
    max_stop_sequences: int = 12
    beam_width: int = 24

    def __post_init__(self) -> None:
        for name in ("max_route_states", "max_road_states", "max_road_labels",
                     "max_arrival_options", "max_assignments", "max_stop_sequences", "beam_width"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (type(self.time_limit_seconds) not in (float, int)
            or not math.isfinite(self.time_limit_seconds) or self.time_limit_seconds <= 0):
            raise ValueError("time_limit_seconds must be finite and positive")

    def to_dict(self) -> dict[str, int | float]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def _diagnostic(code: str, message: str, *, severity: str = "WARNING", **context: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, "context": context}


def _seconds(value: str, epoch: datetime) -> float:
    return (datetime.fromisoformat(value) - epoch).total_seconds()


def _geo_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    # Stop ordering only. This is never used as a routing cost or feasibility metric.
    latitude = math.radians((a[1] + b[1]) / 2)
    return 111_200 * math.hypot((a[0] - b[0]) * math.cos(latitude), a[1] - b[1])


def _sequences(group: tuple[str, ...], points: dict[str, tuple[float, float]],
               depot: tuple[float, float], limits: S1SearchLimits) -> list[tuple[str, ...]]:
    if not group:
        return [()]
    beam: list[tuple[float, tuple[str, ...], tuple[float, float]]] = [(0.0, (), depot)]
    for _ in group:
        expanded = []
        for cost, sequence, position in beam:
            for oid in group:
                if oid not in sequence:
                    expanded.append((cost + _geo_m(position, points[oid]),
                                     sequence + (oid,), points[oid]))
        expanded.sort(key=lambda row: (row[0], row[1]))
        beam = expanded[:limits.beam_width]
    beam.sort(key=lambda row: (row[0] + _geo_m(row[2], depot), row[1]))
    return [row[1] for row in beam[:limits.max_stop_sequences]]


def _input_errors(state: DecisionState, graph: Member1RoadGraph,
                  contract: StaticSolverContract = S1_CONTRACT) -> list[dict[str, Any]]:
    issues = []

    def fail(code: str, path: str, message: str) -> None:
        issues.append(_diagnostic(code, message, severity="ERROR", path=path))

    if (state.scenario_id not in contract.supported_scenarios
        or state.suite_id != "thu-duc-binh-thanh-v1"):
        fail("INVALID_DATA", "scenario_id", "scenario is outside this static solver contract")
    if state.pending_events or state.current_plans or state.execution_updates:
        fail("UNSUPPORTED", "pending_events", "S1 static search cannot apply events or commitments")
    if (state.routing_version != graph.routing_version
        or state.features_version != graph.features_version
        or state.context_version != graph.context_version):
        fail("VERSION_MISMATCH", "source_versions", "state and opened routing/features/context differ")
    if state.source_authentication == "PINNED_RECEIPT_VERIFIED":
        fixture_field = f"fixture_{state.scenario_id.lower()}_sha256"
        for field, expected in ((fixture_field, state.fixture_raw_sha256),
                                ("catalog_sha256", state.catalog_raw_sha256),
                                ("receipt_sha256", state.receipt_sha256)):
            if graph.source_hashes.get(field) != expected:
                fail("SOURCE_MISMATCH", field, "verified state differs from opened source provenance")
    if not 1 <= len(state.orders) <= 8 or not 1 <= len(state.vehicles) <= 2:
        fail("UNSUPPORTED", "orders/vehicles", "S1 bounded search supports up to eight orders and two vehicles")
    depot = graph.node(state.depot.graph_node_id)
    if depot is None or max(abs(depot[i] - state.depot.coordinates[i]) for i in range(2)) > 1e-6:
        fail("INVALID_DATA", "depot.graph_node_id", "depot WGS84 coordinates disagree with graph")
    for order in state.orders:
        point = graph.node(order.graph_node_id)
        if point is None or max(abs(point[i] - order.coordinates[i]) for i in range(2)) > 1e-6:
            fail("INVALID_DATA", f"orders.{order.order_id}.graph_node_id", "order location differs from graph")
        if order.status != "WAITING" or order.pickup_location_id != "DEPOT":
            fail("UNSUPPORTED", f"orders.{order.order_id}.status", "S1 supports depot pickup of WAITING orders only")
    for vehicle in state.vehicles:
        point = graph.node(vehicle.current_position_node_id)
        if (point is None or vehicle.current_position_node_id != state.depot.graph_node_id
            or max(abs(point[i] - vehicle.current_position_coordinates[i]) for i in range(2)) > 1e-6):
            fail("UNSUPPORTED", f"vehicles.{vehicle.vehicle_id}.current_position", "vehicle must start at depot")
        if (vehicle.availability != "AVAILABLE" or vehicle.current_load_kg != 0
            or vehicle.onboard_order_ids or vehicle.committed_stop_id is not None):
            fail("UNSUPPORTED", f"vehicles.{vehicle.vehicle_id}.availability", "S1 vehicle must be available, empty and uncommitted")
    return issues


def _solve_static(state: DecisionState, graph: Member1RoadGraph, *,
                  limits: S1SearchLimits | None = None,
                  contract: StaticSolverContract = S1_CONTRACT) -> dict[str, Any]:
    """Return a checkable full/partial witness or a truthful bounded-search status."""
    limits = limits or S1SearchLimits()
    started = monotonic()
    diagnostics: list[dict[str, Any]] = []
    versions = {"routing_version": state.routing_version,
                "features_version": state.features_version,
                "context_version": state.context_version,
                "delivery_area_version": state.delivery_area_version,
                "state_version": state.state_version,
                "state_schema_version": state.schema_version}
    search: dict[str, Any] = {"limits": limits.to_dict(), "route_states": 0,
                              "road_queries": 0, "road_settled_states": 0,
                              "road_generated_labels": 0, "road_dominated_prunes": 0,
                              "road_resource_prunes": 0, "road_peak_active_labels": 0,
                              "assignments_considered": 0, "truncated": False,
                              "truncation_reasons": [], "search_complete": False,
                              "optimality_proven": False, "elapsed_seconds": 0.0}
    all_ids = tuple(sorted(order.order_id for order in state.orders))
    orders = {order.order_id: order for order in state.orders}
    vehicles = {vehicle.vehicle_id: vehicle for vehicle in state.vehicles}
    base: dict[str, Any] = {"schema_version": contract.schema_version,
                            "solver_version": contract.solver_version,
                            "scenario_id": state.scenario_id, "decision_epoch": state.decision_epoch,
                            "source_authentication": state.source_authentication,
                            "source_versions": versions,
                            "source_hashes": dict(graph.source_hashes),
                            "receipt_sha256": state.receipt_sha256,
                            "served_orders": [], "unserved_orders": [], "vehicle_routes": [],
                            "metrics": {}, "diagnostics": diagnostics, "search": search}
    truncations: set[str] = set()
    best_partial: list[dict[str, Any]] = []
    proven_unserved: dict[str, str] = {}

    def finish(status: str, routes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        routes = routes or []
        served = sorted(oid for route in routes for oid in route["order_sequence"])
        if status == "PARTIAL" and (not served or len(served) == len(all_ids)):
            routes, served = [], []
            status = "TIME_LIMIT" if "TIME_LIMIT" in truncations else "SEARCH_LIMIT"
        if status not in {"FEASIBLE", "PARTIAL"}:
            routes, served = [], []
        search["truncated"] = bool(truncations)
        search["truncation_reasons"] = sorted(truncations)
        search["elapsed_seconds"] = monotonic() - started
        recorded = {(item["code"], item.get("context", {}).get("cause")) for item in diagnostics}
        for cause in sorted(truncations):
            code = "TIME_LIMIT" if cause == "TIME_LIMIT" else "SEARCH_LIMIT"
            if (code, cause) not in recorded:
                diagnostics.append(_diagnostic(
                    code, "Search scope was truncated; witness feasibility does not prove optimality",
                    cause=cause))
        base["status"] = status
        base["vehicle_routes"] = routes
        base["served_orders"] = served
        max_capacity = max((vehicle.capacity_kg for vehicle in vehicles.values()), default=0.0)
        base["unserved_orders"] = []
        for oid in all_ids:
            if oid in served:
                continue
            if contract == S1_CONTRACT:
                # Preserve the reviewed S1 /1 wire semantics.  Its generic
                # reason is a witness statement, not an infeasibility proof.
                reason = (
                    "SEARCH_INCOMPLETE" if truncations
                    else "NOT_SERVED_BY_FOUND_WITNESS"
                )
            else:
                reason = proven_unserved.get(oid) or (
                    "CAPACITY_EXCEEDS_ALL_VEHICLES_NO_SPLIT"
                    if orders[oid].demand_kg > max_capacity + 1e-9
                    else "SEARCH_INCOMPLETE" if truncations
                    else "NOT_SERVED_BY_FOUND_WITNESS"
                )
            base["unserved_orders"].append({"order_id": oid, "reason": reason})
        base["metrics"] = {key: sum(route[key] for route in routes) for key in
                           ("total_distance_m", "total_travel_time_s", "total_exposure",
                            "total_elapsed_time_s", "total_soft_lateness_s", "total_cost_vnd")}
        base["solution_id"] = contract.solution_prefix + "_" + hashlib.sha256(json.dumps({
            "versions": versions, "status": status, "served": served,
            "routes": [[leg["edge_ids"] for leg in route["legs"]] for route in routes],
        }, sort_keys=True).encode()).hexdigest()[:20]
        return base

    problems = _input_errors(state, graph, contract)
    if problems:
        diagnostics.extend(problems)
        return finish("VERSION_MISMATCH" if any(p["code"] == "VERSION_MISMATCH" for p in problems)
                      else "SOURCE_MISMATCH" if any(p["code"] == "SOURCE_MISMATCH" for p in problems)
                      else "UNSUPPORTED" if any(p["code"] == "UNSUPPORTED" for p in problems)
                      else "INVALID_DATA")
    vehicle_ids = tuple(sorted(vehicles))
    epoch = datetime.fromisoformat(state.decision_epoch)
    depot_node = state.depot.graph_node_id
    depot_open = _seconds(state.depot.opening_time, epoch)
    depot_close = _seconds(state.depot.closing_time, epoch)
    points = {oid: orders[oid].coordinates for oid in all_ids}
    road_memo: dict[tuple[Any, ...], Any] = {}

    def guard() -> bool:
        if monotonic() - started >= limits.time_limit_seconds:
            truncations.add("TIME_LIMIT")
            return False
        search["route_states"] += 1
        if search["route_states"] > limits.max_route_states:
            truncations.add("ROUTE_STATE_LIMIT")
            return False
        return True

    def paths(start_node: int, end_node: int, incoming: str | None,
              distance_budget: float, time_budget: float, count: int) -> Any:
        key = (start_node, end_node, incoming, distance_budget, time_budget, count)
        if key not in road_memo:
            result = graph.find_paths(start_node, end_node, incoming_edge=incoming,
                                      max_road_states=limits.max_road_states,
                                      max_road_labels=limits.max_road_labels,
                                      max_arrival_options=count,
                                      max_distance_m=max(0.0, distance_budget),
                                      max_travel_time_s=max(0.0, time_budget),
                                      deadline=started + limits.time_limit_seconds)
            road_memo[key] = result
            search["road_queries"] += 1
            search["road_settled_states"] += result.settled_states
            search["road_generated_labels"] += result.generated_labels
            search["road_dominated_prunes"] += result.dominated_prunes
            search["road_resource_prunes"] += result.resource_prunes
            search["road_peak_active_labels"] = max(search["road_peak_active_labels"],
                                                       result.peak_active_labels)
            if result.limit == "TIME_LIMIT":
                truncations.add("TIME_LIMIT")
            elif result.limit in {"SEARCH_LIMIT", "LABEL_LIMIT"}:
                truncations.add(result.limit)
            # OPTION_LIMIT is a real truncation only after the configured
            # maximum has been requested; smaller requests are expanded on need.
            elif result.limit == "OPTION_LIMIT" and count == limits.max_arrival_options:
                truncations.add("PATH_OPTION_LIMIT")
        return road_memo[key]

    # These two exclusions are hard facts, not heuristic drop decisions.  They
    # reduce S5/S6 search without claiming anything about other unserved orders.
    if contract != S1_CONTRACT:
        max_capacity = max((vehicle.capacity_kg for vehicle in vehicles.values()), default=0.0)
        for oid in all_ids:
            if orders[oid].demand_kg > max_capacity + 1e-9:
                proven_unserved[oid] = "CAPACITY_EXCEEDS_ALL_VEHICLES_NO_SPLIT"
        for oid in all_ids:
            if oid in proven_unserved:
                continue
            order = orders[oid]
            service_possible = False
            lower_bound_complete = True
            for vehicle in vehicles.values():
                departure = max(0.0, depot_open, _seconds(vehicle.working_start, epoch))
                latest_arrival = min(
                    _seconds(order.hard_deadline, epoch),
                    _seconds(vehicle.working_end, epoch),
                ) - order.service_time_seconds
                if latest_arrival < departure - 1e-9:
                    continue
                direct = paths(
                    depot_node, order.graph_node_id, None,
                    vehicle.remaining_range_m, latest_arrival - departure, 1,
                )
                if direct.paths:
                    service_possible = True
                    break
                if direct.limit is not None:
                    lower_bound_complete = False
            if not service_possible and lower_bound_complete:
                proven_unserved[oid] = "HARD_COMPLETION_DEADLINE_UNREACHABLE_LOWER_BOUND"

    search_ids = tuple(oid for oid in all_ids if oid not in proven_unserved)

    def route_for(vid: str, sequence: tuple[str, ...]) -> dict[str, Any] | None:
        vehicle = vehicles[vid]
        load = sum(orders[oid].demand_kg for oid in sequence)
        if load > vehicle.capacity_kg + 1e-9:
            return None
        departure = max(0.0, depot_open, _seconds(vehicle.working_start, epoch))
        last_return = min(depot_close, _seconds(vehicle.working_end, epoch))
        if departure > last_return:
            return None

        def visit(index: int, node: int, incoming: str | None, clock: float,
                  current_load: float, distance: float, travel: float,
                  exposure: float, lateness: float,
                  legs: list[dict[str, Any]], stops: list[dict[str, Any]]) -> dict[str, Any] | None:
            if not guard():
                return None
            at_return = index == len(sequence)
            target = depot_node if at_return else orders[sequence[index]].graph_node_id
            latest = last_return if at_return else min(
                _seconds(orders[sequence[index]].hard_deadline, epoch),
                _seconds(vehicle.working_end, epoch)) - orders[sequence[index]].service_time_seconds
            if latest < clock - 1e-9:
                return None
            seen_paths: set[tuple[str, ...]] = set()
            previous_limit: str | None = "OPTION_LIMIT"
            for requested in range(1, limits.max_arrival_options + 1):
                if previous_limit != "OPTION_LIMIT" or not guard():
                    break
                result = paths(node, target, incoming,
                               vehicle.remaining_range_m - distance, latest - clock, requested)
                previous_limit = result.limit
                for path in result.paths:
                    if path.edge_ids in seen_paths:
                        continue
                    seen_paths.add(path.edge_ids)
                    new_distance = distance + path.candidate.distance
                    arrival = clock + path.candidate.travel_time
                    if new_distance > vehicle.remaining_range_m + 1e-6 or arrival > latest + 1e-6:
                        continue
                    leg = path.to_leg(incoming)
                    if at_return:
                        return {"vehicle_id": vid, "start_node": depot_node,
                                "end_node": depot_node, "order_sequence": list(sequence),
                                "node_sequence": [depot_node] + [orders[oid].graph_node_id for oid in sequence] + [depot_node],
                                "departure_s": departure, "return_s": arrival,
                                "load_before_pickup_kg": 0.0,
                                "load_after_depot_pickup_kg": load,
                                "return_load_kg": current_load, "pickup_service_s": 0.0,
                                "legs": legs + [leg], "stops": stops,
                                "total_distance_m": new_distance,
                                "total_travel_time_s": travel + path.candidate.travel_time,
                                "total_exposure": exposure + path.candidate.risk_score,
                                "total_elapsed_time_s": arrival - departure,
                                "total_soft_lateness_s": lateness,
                                "total_cost_vnd": new_distance / 1000 * vehicle.cost_per_km_vnd}
                    order = orders[sequence[index]]
                    service_start = max(arrival, _seconds(order.earliest, epoch))
                    completion = service_start + order.service_time_seconds
                    if completion > min(_seconds(order.hard_deadline, epoch),
                                        _seconds(vehicle.working_end, epoch)) + 1e-6:
                        continue
                    next_load = current_load - order.demand_kg
                    if next_load < -1e-9:
                        continue
                    stop = {"order_id": order.order_id, "node_id": target,
                            "arrival_s": arrival, "service_start_s": service_start,
                            "completion_s": completion, "waiting_s": service_start - arrival,
                            "load_after_delivery_kg": next_load,
                            "soft_lateness_s": max(0.0, completion - _seconds(order.preferred_due, epoch))}
                    found = visit(index + 1, target, path.final_edge, completion,
                                  next_load, new_distance,
                                  travel + path.candidate.travel_time,
                                  exposure + path.candidate.risk_score,
                                  lateness + stop["soft_lateness_s"],
                                  legs + [leg], stops + [stop])
                    if found is not None:
                        return found
                if len(result.paths) < requested or "TIME_LIMIT" in truncations:
                    break
            return None

        return visit(0, depot_node, None, departure, load, 0.0, 0.0, 0.0, 0.0, [], [])

    try:
        assignments = []
        # Only 2^8 masks, never unbounded route permutations. Capacity is
        # checked before expensive road work; geometric score is proposal only.
        for mask in range(1 << len(search_ids)):
            groups = (tuple(oid for i, oid in enumerate(search_ids) if mask & (1 << i)),
                      tuple(oid for i, oid in enumerate(search_ids) if not mask & (1 << i)))
            if len(vehicle_ids) == 1 and groups[1]:
                continue
            if any(sum(orders[oid].demand_kg for oid in group) > vehicles[vehicle_ids[i]].capacity_kg + 1e-9
                   for i, group in enumerate(groups[:len(vehicle_ids)])):
                continue
            score = sum(_geo_m(state.depot.coordinates, points[oid]) for oid in all_ids)
            # Compactness and one-depot-roundtrip approximation rank masks.
            for group in groups[:len(vehicle_ids)]:
                if group:
                    seq = _sequences(group, points, state.depot.coordinates, limits)[0]
                    score += sum(_geo_m(points[a], points[b]) for a, b in zip(seq, seq[1:]))
                    score += _geo_m(state.depot.coordinates, points[seq[0]])
                    score += _geo_m(points[seq[-1]], state.depot.coordinates)
            assignments.append((score, groups))
        assignments.sort(key=lambda row: (row[0], row[1]))
        if len(assignments) > limits.max_assignments:
            truncations.add("ASSIGNMENT_LIMIT")
        for _, groups in assignments[:limits.max_assignments]:
            if not guard():
                break
            search["assignments_considered"] += 1
            candidate_routes: list[dict[str, Any]] = []
            for vehicle_index, vid in enumerate(vehicle_ids):
                group = groups[vehicle_index]
                if not group:
                    continue
                candidate = None
                sequences = _sequences(group, points, state.depot.coordinates, limits)
                if math.factorial(len(group)) > len(sequences):
                    truncations.add("STOP_SEQUENCE_LIMIT")
                for sequence in sequences:
                    candidate = route_for(vid, sequence)
                    if candidate is not None or truncations & {"TIME_LIMIT", "ROUTE_STATE_LIMIT"}:
                        break
                if candidate is None:
                    break
                candidate_routes.append(candidate)
                if sum(len(route["order_sequence"]) for route in candidate_routes) > sum(
                        len(route["order_sequence"]) for route in best_partial):
                    best_partial = list(candidate_routes)
            else:
                if sum(len(route["order_sequence"]) for route in candidate_routes) == len(search_ids):
                    status = "FEASIBLE" if not proven_unserved else "PARTIAL"
                    diagnostics.append(_diagnostic(
                        "FEASIBLE_WITNESS" if status == "FEASIBLE" else "PARTIAL_WITNESS",
                        "Static route witness found; optimality not proven",
                        severity="INFO", proven_unserved=dict(proven_unserved),
                    ))
                    return finish(status, candidate_routes)
            if truncations & {"TIME_LIMIT", "ROUTE_STATE_LIMIT"}:
                break
        # A full-service assignment may be impossible under capacity. In that
        # case search explicit smaller depot-loaded route witnesses, rather
        # than returning an unsubstantiated empty plan. The subset cap is
        # recorded and cannot be interpreted as an absence proof.
        if not best_partial and not truncations & {"TIME_LIMIT", "ROUTE_STATE_LIMIT"}:
            subsets_checked = 0
            for count in range(len(search_ids) - 1, 0, -1):
                for subset in itertools.combinations(search_ids, count):
                    for vid in vehicle_ids:
                        if sum(orders[oid].demand_kg for oid in subset) > vehicles[vid].capacity_kg + 1e-9:
                            continue
                        subsets_checked += 1
                        if subsets_checked > limits.max_assignments:
                            truncations.add("PARTIAL_SUBSET_LIMIT")
                            break
                        for sequence in _sequences(subset, points, state.depot.coordinates, limits):
                            route = route_for(vid, sequence)
                            if route is not None:
                                best_partial = [route]
                                break
                            if truncations & {"TIME_LIMIT", "ROUTE_STATE_LIMIT"}:
                                break
                        if best_partial or truncations & {"TIME_LIMIT", "ROUTE_STATE_LIMIT"}:
                            break
                    if best_partial or truncations & {"TIME_LIMIT", "ROUTE_STATE_LIMIT", "PARTIAL_SUBSET_LIMIT"}:
                        break
                if best_partial or truncations & {"TIME_LIMIT", "ROUTE_STATE_LIMIT", "PARTIAL_SUBSET_LIMIT"}:
                    break
    except (RoadDataError, KeyError, TypeError, ValueError) as error:
        diagnostics.append(_diagnostic("INVALID_DATA", str(error), severity="ERROR"))
        return finish("INVALID_DATA")
    if best_partial:
        return finish("PARTIAL", best_partial)
    if "TIME_LIMIT" in truncations:
        return finish("TIME_LIMIT")
    if truncations:
        return finish("SEARCH_LIMIT")
    diagnostics.append(_diagnostic("NO_SOLUTION_FOUND", "Bounded static search found no witness; infeasibility not proven"))
    return finish("NO_SOLUTION_FOUND")


def solve_s1(state: DecisionState, graph: Member1RoadGraph, *,
             limits: S1SearchLimits | None = None) -> dict[str, Any]:
    """Backward-compatible frozen S1 entry point."""

    return _solve_static(state, graph, limits=limits, contract=S1_CONTRACT)


__all__ = [
    "SCHEMA_VERSION", "SOLVER_VERSION", "S1SearchLimits", "solve_s1",
]
