"""Versioned, bounded turn-state S0 solver; legacy VRPTW contracts stay intact.

This solver searches the tiny S0 stop-assignment space and invokes lazy road
search with `(node, incoming_edge)` state at every leg. It returns a feasible
witness, not an optimality proof. No profile weights or drop penalties apply.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, Mapping

from .member1_s0_graph import Member1RoadGraph, RoadDataError, RoadPath


SCHEMA_VERSION = "member1-s0-integration/2"
SOLVER_VERSION = "s0-turn-state-pareto-v2"


class InputIssue(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SearchLimits:
    max_road_states: int = 250_000
    max_arrival_options: int = 4
    max_route_states: int = 50_000
    time_limit_seconds: float = 120.0
    max_road_labels: int = 500_000

    def __post_init__(self) -> None:
        for name in ("max_road_states", "max_arrival_options", "max_route_states", "max_road_labels"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.time_limit_seconds, (int, float)) or not math.isfinite(self.time_limit_seconds) or self.time_limit_seconds <= 0:
            raise ValueError("time_limit_seconds must be finite and positive")

    def to_dict(self) -> dict[str, int | float]:
        return {name: getattr(self, name) for name in (
            "max_road_states", "max_arrival_options", "max_route_states",
            "time_limit_seconds", "max_road_labels"
        )}


def _diagnostic(code: str, message: str, **context: Any) -> dict[str, Any]:
    return {"severity": "ERROR" if code not in {"FEASIBLE_WITNESS"} else "INFO",
            "code": code, "message": message, "context": context}


def _number(value: Any, field: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise InputIssue("INVALID_DATA", f"{field} must be finite and >= {minimum}")
    return float(value)


def _instant(value: Any, field: str, epoch: datetime | None = None) -> float | datetime:
    if not isinstance(value, str) or not value.endswith("+07:00"):
        raise InputIssue("INVALID_DATA", f"{field} must be ISO with explicit +07:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise InputIssue("INVALID_DATA", f"{field} is not ISO datetime") from error
    if parsed.utcoffset() != timedelta(hours=7):
        raise InputIssue("INVALID_DATA", f"{field} has wrong UTC offset")
    return parsed if epoch is None else (parsed - epoch).total_seconds()


def _location(graph: Member1RoadGraph, item: Mapping[str, Any], field: str) -> int:
    node = item.get("graphNodeId")
    if isinstance(node, bool) or not isinstance(node, int):
        raise InputIssue("INVALID_DATA", f"{field}.graphNodeId must be integer")
    actual = graph.node(node)
    if actual is None:
        raise InputIssue("INVALID_DATA", f"{field}.graphNodeId is absent from routing graph")
    lon = _number(item.get("longitude"), f"{field}.longitude", minimum=-180)
    lat = _number(item.get("latitude"), f"{field}.latitude", minimum=-90)
    if lon > 180 or lat > 90 or abs(lon - actual[0]) > 1e-6 or abs(lat - actual[1]) > 1e-6:
        raise InputIssue("INVALID_DATA", f"{field} coordinates do not match graph node in [lon,lat] order")
    return node


def _parse_s0(fixture: Mapping[str, Any], graph: Member1RoadGraph) -> dict[str, Any]:
    if not isinstance(fixture, Mapping) or fixture.get("scenarioId") != "S0":
        raise InputIssue("INVALID_DATA", "only a Member 1 S0 fixture is supported")
    if fixture.get("schemaVersion") != "member1-scenario-draft/1":
        raise InputIssue("VERSION_MISMATCH", "Member 1 scenario schema is not supported")
    for key, expected in (("routingVersion", graph.routing_version),
                          ("featuresVersion", graph.features_version),
                          ("contextVersion", graph.context_version)):
        if fixture.get(key) != expected:
            raise InputIssue("VERSION_MISMATCH", f"{key} disagrees with opened Member 1 snapshot")
    if not isinstance(fixture.get("deliveryAreaVersion"), str) or not fixture["deliveryAreaVersion"]:
        raise InputIssue("INVALID_DATA", "deliveryAreaVersion is required")
    if fixture.get("events") or fixture.get("executionUpdates"):
        raise InputIssue("UNSUPPORTED", "S0 event/execution updates are not supported")
    state = fixture.get("initialState")
    if not isinstance(state, Mapping):
        raise InputIssue("INVALID_DATA", "initialState is required")
    if "currentPlans" not in state or not isinstance(state["currentPlans"], list):
        raise InputIssue("INVALID_DATA", "initialState.currentPlans must be a list")
    if state["currentPlans"]:
        raise InputIssue("UNSUPPORTED", "S0 integration does not interpret currentPlans or commitments")
    epoch = _instant(state.get("currentTime"), "initialState.currentTime")
    assert isinstance(epoch, datetime)
    state_version = state.get("stateVersion")
    if isinstance(state_version, bool) or not isinstance(state_version, int) or state_version < 0:
        raise InputIssue("INVALID_DATA", "stateVersion must be a nonnegative integer")
    locations = state.get("locations")
    if not isinstance(locations, list) or len(locations) != 1 or locations[0].get("id") != "DEPOT":
        raise InputIssue("UNSUPPORTED", "S0 requires one explicit DEPOT location")
    depot = locations[0]
    depot_node = _location(graph, depot, "DEPOT")
    depot_open = _instant(depot.get("openingTime"), "DEPOT.openingTime", epoch)
    depot_close = _instant(depot.get("closingTime"), "DEPOT.closingTime", epoch)
    if depot_open > depot_close:
        raise InputIssue("INVALID_DATA", "depot opening exceeds closing")

    raw_orders = state.get("orders")
    raw_vehicles = state.get("vehicles")
    if not isinstance(raw_orders, list) or not 1 <= len(raw_orders) <= 3:
        raise InputIssue("UNSUPPORTED", "S0 turn-state solver supports one to three orders")
    if not isinstance(raw_vehicles, list) or not 1 <= len(raw_vehicles) <= 2:
        raise InputIssue("UNSUPPORTED", "S0 turn-state solver supports one or two vehicles")
    orders: dict[str, dict[str, Any]] = {}
    for raw in raw_orders:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str) or not raw["id"]:
            raise InputIssue("INVALID_DATA", "order id is required")
        oid = raw["id"]
        if oid in orders:
            raise InputIssue("INVALID_DATA", "duplicate order id")
        if raw.get("status") == "ONBOARD" and not raw.get("assignedVehicleId"):
            raise InputIssue("INVALID_DATA", f"ONBOARD order {oid} has no owner")
        if raw.get("status") != "WAITING":
            raise InputIssue("UNSUPPORTED", f"order status {raw.get('status')} is not supported in S0")
        if raw.get("assignedVehicleId") is not None or raw.get("pickedUpAt") is not None or raw.get("deliveredAt") is not None:
            raise InputIssue("UNSUPPORTED", f"order {oid} has non-WAITING custody history")
        if raw.get("pickupLocationId") != "DEPOT":
            raise InputIssue("UNSUPPORTED", f"order {oid} pickup is not DEPOT")
        node = _location(graph, raw, f"order {oid}")
        earliest = _instant(raw.get("earliest"), f"order {oid}.earliest", epoch)
        preferred = _instant(raw.get("preferredDue"), f"order {oid}.preferredDue", epoch)
        deadline = _instant(raw.get("hardDeadline"), f"order {oid}.hardDeadline", epoch)
        if not earliest <= preferred <= deadline:
            raise InputIssue("INVALID_DATA", f"order {oid} deadlines are inconsistent")
        orders[oid] = {"id": oid, "node": node,
                       "demand_kg": _number(raw.get("demandKg"), f"order {oid}.demandKg"),
                       "service_s": _number(raw.get("serviceTimeHours"), f"order {oid}.serviceTimeHours") * 3600,
                       "earliest_s": earliest, "preferred_due_s": preferred,
                       "hard_deadline_s": deadline}
    vehicles: dict[str, dict[str, Any]] = {}
    for raw in raw_vehicles:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str) or not raw["id"]:
            raise InputIssue("INVALID_DATA", "vehicle id is required")
        vid = raw["id"]
        if vid in vehicles:
            raise InputIssue("INVALID_DATA", "duplicate vehicle id")
        if raw.get("availability") != "AVAILABLE":
            raise InputIssue("UNSUPPORTED", f"vehicle {vid} availability is not AVAILABLE")
        if raw.get("onboardOrderIds") != [] or raw.get("currentLoadKg") != 0:
            raise InputIssue("UNSUPPORTED", f"vehicle {vid} has cargo or ONBOARD state")
        if raw.get("committedStopId") is not None:
            raise InputIssue("UNSUPPORTED", f"vehicle {vid} has a committed stop")
        start = _location(graph, raw.get("currentPosition", {}), f"vehicle {vid}.currentPosition")
        if start != depot_node:
            raise InputIssue("UNSUPPORTED", f"vehicle {vid} is not at DEPOT for S0 pickup")
        work_start = _instant(raw.get("workingStart"), f"vehicle {vid}.workingStart", epoch)
        work_end = _instant(raw.get("workingEnd"), f"vehicle {vid}.workingEnd", epoch)
        if work_start > work_end:
            raise InputIssue("INVALID_DATA", f"vehicle {vid} working window is reversed")
        capacity = _number(raw.get("capacityKg"), f"vehicle {vid}.capacityKg")
        nominal_range = _number(raw.get("rangeKm"), f"vehicle {vid}.rangeKm")
        remaining_range = _number(raw.get("remainingRangeKm"), f"vehicle {vid}.remainingRangeKm")
        if remaining_range > nominal_range:
            raise InputIssue("INVALID_DATA", f"vehicle {vid} remainingRangeKm exceeds rangeKm")
        vehicles[vid] = {"id": vid, "start_node": start, "capacity_kg": capacity,
                         "remaining_range_m": remaining_range * 1000,
                         "nominal_range_m": nominal_range * 1000,
                         "work_start_s": work_start, "work_end_s": work_end}
    return {"scenario_id": "S0", "epoch": epoch, "state_version": state_version,
            "depot_node": depot_node, "depot_open_s": depot_open,
            "depot_close_s": depot_close, "orders": orders, "vehicles": vehicles,
            "delivery_area_version": fixture["deliveryAreaVersion"]}


def solve_s0(
    fixture: Mapping[str, Any], graph: Member1RoadGraph, *,
    limits: SearchLimits | None = None,
) -> dict[str, Any]:
    """Find a full-service S0 witness first, then an explicit partial plan.

    Road/path-option and route search caps mean failure is never labelled a
    proof of infeasibility. The output is a separate, versioned envelope.
    """

    limits = limits or SearchLimits()
    started = monotonic()
    diagnostics: list[dict[str, Any]] = []
    source_versions = {
        "routing_version": graph.routing_version,
        "features_version": graph.features_version,
        "context_version": graph.context_version,
        "delivery_area_version": fixture.get("deliveryAreaVersion") if isinstance(fixture, Mapping) else None,
        "state_version": fixture.get("initialState", {}).get("stateVersion") if isinstance(fixture, Mapping) and isinstance(fixture.get("initialState"), Mapping) else None,
    }
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "solver_version": SOLVER_VERSION,
        "scenario_id": fixture.get("scenarioId", "UNKNOWN") if isinstance(fixture, Mapping) else "UNKNOWN",
        "source_versions": source_versions, "source_hashes": dict(graph.source_hashes),
        "served_orders": [], "unserved_orders": [], "vehicle_routes": [],
        "metrics": {"total_distance_m": 0.0, "total_travel_time_s": 0.0,
                    "total_exposure": 0.0, "total_elapsed_time_s": 0.0,
                    "total_soft_lateness_s": 0.0},
        "diagnostics": diagnostics,
        "search": {"limits": limits.to_dict(), "route_states": 0,
                   "road_queries": 0, "road_settled_states": 0,
                   "road_generated_labels": 0, "road_dominated_prunes": 0,
                   "road_resource_prunes": 0, "road_peak_active_labels": 0,
                   "truncated": False, "truncation_reasons": [],
                   "search_complete": False, "optimality_proven": False,
                   "elapsed_seconds": 0.0},
    }

    def finish(status: str, *, routes: list[dict[str, Any]] | None = None,
               served: list[str] | None = None, reason: str = "NOT_SERVED") -> dict[str, Any]:
        routes = routes or []
        served = sorted(served or [])
        all_ids = sorted(item.get("id") for item in fixture.get("initialState", {}).get("orders", [])
                         if isinstance(item, Mapping) and isinstance(item.get("id"), str)) if isinstance(fixture, Mapping) else []
        base["status"] = status
        if status in {"FEASIBLE", "PARTIAL"} and monotonic() - started >= limits.time_limit_seconds:
            if not any(item["code"] == "TIME_LIMIT" for item in diagnostics):
                diagnostics.append(_diagnostic("TIME_LIMIT", "S0 integration exceeded its cooperative wall-clock budget"))
        truncations = sorted({item["context"].get("cause", item["code"])
                              for item in diagnostics if item["code"] in {"SEARCH_LIMIT", "TIME_LIMIT"}})
        base["search"]["truncated"] = bool(truncations)
        base["search"]["truncation_reasons"] = truncations
        # A bounded search does not invalidate a checkable witness, but its
        # unserved orders are not proven impossible.
        if status in {"FEASIBLE", "PARTIAL"}:
            for diagnostic in diagnostics:
                if diagnostic["code"] in {"SEARCH_LIMIT", "TIME_LIMIT"}:
                    diagnostic["severity"] = "WARNING"
        if status == "PARTIAL" and truncations:
            reason = "SEARCH_INCOMPLETE"
        base["served_orders"] = served
        base["unserved_orders"] = [{"order_id": oid, "reason": reason} for oid in all_ids if oid not in served]
        base["vehicle_routes"] = routes
        for key in ("total_distance_m", "total_travel_time_s", "total_exposure",
                    "total_elapsed_time_s", "total_soft_lateness_s"):
            base["metrics"][key] = sum(route[key] for route in routes)
        base["search"]["elapsed_seconds"] = monotonic() - started
        base["solution_id"] = "s0_" + hashlib.sha256(json.dumps({
            "versions": source_versions, "status": status,
            "routes": [[leg["edge_ids"] for leg in route["legs"]] for route in routes],
            "served": served,
        }, sort_keys=True).encode()).hexdigest()[:20]
        return base

    try:
        parsed = _parse_s0(fixture, graph)
    except InputIssue as issue:
        diagnostics.append(_diagnostic(issue.code, str(issue)))
        return finish(issue.code, reason=issue.code)
    except (RoadDataError, KeyError, TypeError, ValueError) as error:
        diagnostics.append(_diagnostic("INVALID_DATA", str(error)))
        return finish("INVALID_DATA", reason="INVALID_DATA")

    orders = parsed["orders"]
    vehicles = parsed["vehicles"]
    order_ids = tuple(sorted(orders))
    vehicle_ids = tuple(sorted(vehicles))
    memo: dict[tuple[int, int, str | None, float, float, int], Any] = {}
    noted: set[str] = set()
    timed_out = False
    search_limited = False

    def note(code: str, message: str, **context: Any) -> None:
        if code not in noted:
            diagnostics.append(_diagnostic(code, message, **context))
            noted.add(code)

    def guard() -> bool:
        nonlocal timed_out, search_limited
        if monotonic() - started >= limits.time_limit_seconds:
            timed_out = True
            note("TIME_LIMIT", "S0 integration search reached its time limit")
            return False
        base["search"]["route_states"] += 1
        if base["search"]["route_states"] > limits.max_route_states:
            search_limited = True
            note("SEARCH_LIMIT", "S0 integration search reached its route-state limit")
            return False
        return True

    def options(start: int, end: int, incoming: str | None,
                remaining_distance_m: float, remaining_time_s: float,
                requested_options: int) -> Any:
        nonlocal search_limited, timed_out
        # Resource budgets are part of identity: paths pruned for one vehicle
        # or arrival time must not be reused for another.
        key = (start, end, incoming, remaining_distance_m, remaining_time_s,
               requested_options)
        if key not in memo:
            result = graph.find_paths(start, end, incoming_edge=incoming,
                                      max_road_states=limits.max_road_states,
                                      max_arrival_options=requested_options,
                                      max_road_labels=limits.max_road_labels,
                                      max_distance_m=remaining_distance_m,
                                      max_travel_time_s=remaining_time_s,
                                      deadline=started + limits.time_limit_seconds)
            memo[key] = result
            base["search"]["road_queries"] += 1
            base["search"]["road_settled_states"] += result.settled_states
            base["search"]["road_generated_labels"] += result.generated_labels
            base["search"]["road_dominated_prunes"] += result.dominated_prunes
            base["search"]["road_resource_prunes"] += result.resource_prunes
            base["search"]["road_peak_active_labels"] = max(
                base["search"]["road_peak_active_labels"], result.peak_active_labels)
            if not result.paths and result.distance_prunes:
                note("RANGE_EXCEEDED", "Road options exceeded remainingRangeKm",
                     from_node=start, to_node=end)
            if not result.paths and result.time_prunes:
                note("DEADLINE_VIOLATION", "Road options exceeded completion/return time",
                     from_node=start, to_node=end)
            if result.limit and not (result.limit == "OPTION_LIMIT" and
                                     requested_options < limits.max_arrival_options):
                if result.limit == "TIME_LIMIT":
                    timed_out = True
                    note("TIME_LIMIT", "Road search exceeded cooperative deadline",
                         cause=result.limit, from_node=start, to_node=end)
                else:
                    search_limited = True
                    note("SEARCH_LIMIT", "Road path options were truncated", cause=result.limit,
                         from_node=start, to_node=end)
            if not result.paths and not result.limit:
                note("NO_PATH", "No turn-valid road path for a required leg",
                     from_node=start, to_node=end, incoming_edge=incoming)
        return memo[key]

    def route_for(vid: str, sequence: tuple[str, ...]) -> dict[str, Any] | None:
        vehicle = vehicles[vid]
        pickup_load = sum(orders[oid]["demand_kg"] for oid in sequence)
        if pickup_load > vehicle["capacity_kg"] + 1e-9:
            note("CAPACITY_EXCEEDED", "Depot pickup exceeds vehicle capacity", vehicle_id=vid)
            return None
        departure = max(0.0, parsed["depot_open_s"], vehicle["work_start_s"])
        if departure > min(parsed["depot_close_s"], vehicle["work_end_s"]):
            note("TIME_WINDOW_VIOLATION", "Vehicle cannot depart within depot/working window", vehicle_id=vid)
            return None

        def visit(index: int, node: int, incoming: str | None, clock: float,
                  remaining_load: float, distance: float, travel: float,
                  exposure: float, lateness: float,
                  legs: list[dict[str, Any]], stops: list[dict[str, Any]]) -> dict[str, Any] | None:
            if not guard():
                return None
            target = orders[sequence[index]]["node"] if index < len(sequence) else parsed["depot_node"]
            latest = (min(parsed["depot_close_s"], vehicle["work_end_s"])
                      if index == len(sequence) else
                      min(orders[sequence[index]]["hard_deadline_s"], vehicle["work_end_s"])
                      - orders[sequence[index]]["service_s"])
            if latest < clock - 1e-9:
                note("DEADLINE_VIOLATION", "No remaining time for required road leg")
                return None
            previous_limit = "OPTION_LIMIT"
            for requested in range(1, limits.max_arrival_options + 1):
                if previous_limit != "OPTION_LIMIT":
                    break
                result = options(node, target, incoming,
                                 max(0.0, vehicle["remaining_range_m"] - distance),
                                 max(0.0, latest - clock), requested)
                previous_limit = result.limit
                if len(result.paths) < requested:
                    break
                path = result.paths[requested - 1]
                new_distance = distance + path.candidate.distance
                if new_distance > vehicle["remaining_range_m"] + 1e-6:
                    note("RANGE_EXCEEDED", "Route exceeds remainingRangeKm", vehicle_id=vid)
                    continue
                arrival = clock + path.candidate.travel_time
                leg = path.to_leg(incoming)
                if index == len(sequence):
                    if arrival > min(parsed["depot_close_s"], vehicle["work_end_s"]) + 1e-6:
                        note("TIME_WINDOW_VIOLATION", "Return exceeds depot or vehicle closing", vehicle_id=vid)
                        continue
                    return {
                        "vehicle_id": vid, "start_node": parsed["depot_node"],
                        "end_node": parsed["depot_node"], "order_sequence": list(sequence),
                        "node_sequence": [parsed["depot_node"]] + [orders[oid]["node"] for oid in sequence] + [parsed["depot_node"]],
                        "departure_s": departure, "return_s": arrival,
                        "load_before_pickup_kg": 0.0,
                        "load_after_depot_pickup_kg": pickup_load,
                        "return_load_kg": remaining_load,
                        "pickup_service_s": 0.0,
                        "legs": legs + [leg], "stops": stops,
                        "total_distance_m": new_distance,
                        "total_travel_time_s": travel + path.candidate.travel_time,
                        "total_exposure": exposure + path.candidate.risk_score,
                        "total_elapsed_time_s": arrival - departure,
                        "total_soft_lateness_s": lateness,
                    }
                order = orders[sequence[index]]
                served_at = max(arrival, order["earliest_s"])
                completion = served_at + order["service_s"]
                if completion > min(order["hard_deadline_s"], vehicle["work_end_s"]) + 1e-6:
                    note("DEADLINE_VIOLATION", "Customer completion exceeds hard deadline or working end",
                         order_id=order["id"])
                    continue
                after_load = remaining_load - order["demand_kg"]
                if after_load < -1e-9:
                    note("LOAD_INCONSISTENCY", "Delivery exceeds cargo loaded at depot", order_id=order["id"])
                    continue
                stop = {"order_id": order["id"], "node_id": order["node"],
                        "arrival_s": arrival, "service_start_s": served_at,
                        "completion_s": completion, "waiting_s": served_at - arrival,
                        "load_after_delivery_kg": after_load,
                        "soft_lateness_s": max(0.0, completion - order["preferred_due_s"])}
                found = visit(index + 1, target, path.final_edge, completion,
                              after_load, new_distance,
                              travel + path.candidate.travel_time,
                              exposure + path.candidate.risk_score,
                              lateness + stop["soft_lateness_s"], legs + [leg], stops + [stop])
                if found is not None:
                    return found
            return None

        return visit(0, parsed["depot_node"], None, departure, pickup_load,
                     0.0, 0.0, 0.0, 0.0, [], [])

    try:
        for served_count in range(len(order_ids), 0, -1):
            for subset in itertools.combinations(order_ids, served_count):
                for assignment in itertools.product(range(len(vehicle_ids)), repeat=served_count):
                    grouped = {vid: [] for vid in vehicle_ids}
                    for oid, vehicle_index in zip(subset, assignment):
                        grouped[vehicle_ids[vehicle_index]].append(oid)
                    choices = [tuple(itertools.permutations(grouped[vid])) if grouped[vid] else ((),)
                               for vid in vehicle_ids]
                    for sequences in itertools.product(*choices):
                        routes: list[dict[str, Any]] = []
                        for vid, sequence in zip(vehicle_ids, sequences):
                            if sequence:
                                route = route_for(vid, sequence)
                                if route is None:
                                    break
                                routes.append(route)
                        else:
                            status = "FEASIBLE" if served_count == len(order_ids) else "PARTIAL"
                            if status == "FEASIBLE":
                                diagnostics.append(_diagnostic("FEASIBLE_WITNESS", "All S0 orders served; optimality not proven"))
                            return finish(status, routes=routes, served=list(subset),
                                          reason="NOT_SERVABLE_UNDER_CURRENT_CONSTRAINTS")
                        if timed_out or base["search"]["route_states"] > limits.max_route_states:
                            break
                    if timed_out or base["search"]["route_states"] > limits.max_route_states:
                        break
                if timed_out or base["search"]["route_states"] > limits.max_route_states:
                    break
            if timed_out or base["search"]["route_states"] > limits.max_route_states:
                break
    except (RoadDataError, KeyError, TypeError, ValueError) as error:
        diagnostics.append(_diagnostic("INVALID_DATA", str(error)))
        return finish("INVALID_DATA", reason="INVALID_DATA")
    if timed_out:
        return finish("TIME_LIMIT", reason="SEARCH_INCOMPLETE")
    if search_limited:
        return finish("SEARCH_LIMIT", reason="SEARCH_INCOMPLETE")
    if "CAPACITY_EXCEEDED" in noted or "RANGE_EXCEEDED" in noted:
        return finish("PARTIAL", reason="CAPACITY_OR_RANGE")
    return finish("NO_SOLUTION_FOUND", reason="NO_TURN_VALID_FEASIBLE_ROUTE_FOUND")


__all__ = ["Member1RoadGraph", "SearchLimits", "solve_s0", "SCHEMA_VERSION", "SOLVER_VERSION"]
