"""Independent S1/S5/S6 witness validator using raw M1 SQLite rows.

No solver path-search, assignment, route-building or score function is used.
The caller/runner remains responsible for authenticating source file bytes.
"""

from __future__ import annotations

from datetime import datetime
import heapq
import json
import math
import sqlite3
from typing import Any, Mapping

from optimization.models.decision_state import DecisionState
from .member1_s0_graph import Member1RoadGraph
from .member1_static import SCHEMA_VERSION, SOLVER_VERSION, SUPPORTED_SCENARIOS


VALIDATOR_VERSION = "member1-static-s1-s5-s6-independent-validator/2"
UNSERVED_REASONS = frozenset({
    "CAPACITY_EXCEEDS_ALL_VEHICLES_NO_SPLIT",
    "HARD_COMPLETION_DEADLINE_UNREACHABLE_LOWER_BOUND",
    "SEARCH_INCOMPLETE",
    "NOT_SERVED_BY_FOUND_WITNESS",
})
_FLAGS = ("edgeId", "contextVersion", "missingFlags", "fallbackUsed",
          "weatherFallbackUsed", "weatherRegionId", "weatherValidAt",
          "sourceType", "travelSourceType", "riskModelVersion")


def validate_static_solution(
    result: Mapping[str, Any], state: DecisionState, graph: Member1RoadGraph, *,
    expected_schema_version: str = SCHEMA_VERSION,
    expected_solver_version: str = SOLVER_VERSION,
    reported_validator_version: str = VALIDATOR_VERSION,
    supported_scenarios: frozenset[str] = SUPPORTED_SCENARIOS,
    allow_unrecorded_counters: bool = False,
) -> dict[str, Any]:
    """Check witness feasibility and metadata consistency, not optimality."""
    issues: list[dict[str, Any]] = []

    def bad(code: str, message: str, **context: Any) -> None:
        issues.append({"severity": "ERROR", "code": code, "message": message, "context": context})

    def equal_number(actual: Any, expected: float, code: str, field: str) -> None:
        if (type(actual) not in (float, int) or not math.isfinite(actual)
            or not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-5)):
            bad(code, "reported value differs from independent source recomputation",
                field=field, actual=actual, expected=expected)

    def seconds(iso: str, epoch: datetime) -> float:
        return (datetime.fromisoformat(iso) - epoch).total_seconds()

    def raw_node(node_id: int) -> tuple[float, float] | None:
        row = graph.db.execute("SELECT longitude,latitude FROM nodes WHERE nodeId=?",
                               (node_id,)).fetchone()
        return (float(row[0]), float(row[1])) if row else None

    if not isinstance(result, Mapping):
        bad("SOLUTION_SCHEMA", "static solution must be an object")
        return {"status": "FAILED", "valid": False, "diagnostics": issues,
                "validator_version": reported_validator_version}

    search = result.get("search")
    flags: dict[str, bool] = {}
    reasons: Any = None
    reasons_valid = False
    if "search" not in result:
        bad("SEARCH_METADATA_MISSING", "bounded-search metadata is required", path="search")
    elif not isinstance(search, Mapping):
        bad("SEARCH_METADATA_INVALID", "bounded-search metadata must be an object", path="search")
    else:
        for key in ("optimality_proven", "search_complete", "truncated"):
            if key not in search:
                bad("SEARCH_METADATA_MISSING", "required search flag is missing", path=f"search.{key}")
            elif type(search[key]) is not bool:
                bad("SEARCH_METADATA_INVALID", "search flag must be boolean", path=f"search.{key}")
            else:
                flags[key] = search[key]
        if flags.get("optimality_proven") is True:
            bad("OPTIMALITY_CLAIM_UNSUPPORTED", "bounded static solver /1 does not prove optimality",
                path="search.optimality_proven")
        if flags.get("search_complete") is True:
            bad("SEARCH_COMPLETENESS_CLAIM_UNSUPPORTED",
                "bounded static solver /1 does not establish complete search",
                path="search.search_complete")
        reasons = search.get("truncation_reasons")
        reasons_valid = (isinstance(reasons, list)
                         and all(isinstance(item, str) and item for item in reasons)
                         and reasons == sorted(set(reasons)))
        if "truncation_reasons" not in search:
            bad("SEARCH_METADATA_MISSING", "truncation reasons are required",
                path="search.truncation_reasons")
        elif not reasons_valid:
            bad("SEARCH_METADATA_INVALID", "truncation reasons must be sorted unique strings",
                path="search.truncation_reasons")
        elif "truncated" in flags and flags["truncated"] != bool(reasons):
            bad("SEARCH_TRUNCATION_INCONSISTENT",
                "truncated must equal whether truncation reasons are present",
                path="search.truncated")
        if "limits" not in search:
            bad("SEARCH_METADATA_MISSING", "search limits are required", path="search.limits")
        elif not isinstance(search["limits"], Mapping):
            bad("SEARCH_METADATA_INVALID", "search limits must be an object", path="search.limits")
        for key in ("route_states", "road_queries", "road_settled_states",
                    "road_generated_labels", "road_dominated_prunes", "road_resource_prunes",
                    "road_peak_active_labels", "assignments_considered"):
            if key not in search:
                bad("SEARCH_METADATA_MISSING", "search counter is required", path=f"search.{key}")
            elif allow_unrecorded_counters and search[key] is None:
                pass
            elif type(search[key]) is not int or search[key] < 0:
                bad("SEARCH_METADATA_INVALID", "search counter must be a nonnegative integer",
                    path=f"search.{key}")
        if "elapsed_seconds" not in search:
            bad("SEARCH_METADATA_MISSING", "elapsed time is required", path="search.elapsed_seconds")
        else:
            elapsed = search["elapsed_seconds"]
            try:
                elapsed_valid = (type(elapsed) in (int, float)
                                 and math.isfinite(float(elapsed)) and elapsed >= 0)
            except OverflowError:
                elapsed_valid = False
            if not elapsed_valid:
                bad("SEARCH_METADATA_INVALID", "elapsed time must be finite and nonnegative",
                    path="search.elapsed_seconds")
    try:
        forbidden = {(row[0], row[1]) for row in graph.db.execute(
            "SELECT inEdgeId,outEdgeId FROM forbidden_turns")}
        if (result.get("schema_version") != expected_schema_version
            or result.get("solver_version") != expected_solver_version):
            bad("SOLUTION_VERSION", "unknown static solution/solver version")
        if (state.scenario_id not in supported_scenarios or state.pending_events
            or state.current_plans or state.execution_updates):
            bad("STATE_UNSUPPORTED", "only S1/S5/S6 static initial states can be certified")
        if state.source_authentication != result.get("source_authentication"):
            bad("SOURCE_AUTHENTICATION", "state authentication and witness claim disagree")
        expected_versions = {"routing_version": state.routing_version,
                             "features_version": state.features_version,
                             "context_version": state.context_version,
                             "delivery_area_version": state.delivery_area_version,
                             "state_version": state.state_version,
                             "state_schema_version": state.schema_version}
        if result.get("source_versions") != expected_versions or (
            graph.routing_version != state.routing_version
            or graph.features_version != state.features_version
            or graph.context_version != state.context_version):
            bad("VERSION_MISMATCH", "solution/state/opened SQLite version identity differs")
        if result.get("source_hashes") != graph.source_hashes or result.get("receipt_sha256") != state.receipt_sha256:
            bad("SOURCE_HASH_MISMATCH", "source/receipt hashes differ from opened graph and state")
        if state.source_authentication == "PINNED_RECEIPT_VERIFIED" and any(
            graph.source_hashes.get(key) != expected for key, expected in
            ((f"fixture_{state.scenario_id.lower()}_sha256", state.fixture_raw_sha256),
             ("catalog_sha256", state.catalog_raw_sha256),
             ("receipt_sha256", state.receipt_sha256))):
            bad("SOURCE_HASH_MISMATCH", "receipt-gated state hashes do not match opened snapshot")
        if result.get("scenario_id") != state.scenario_id or result.get("decision_epoch") != state.decision_epoch:
            bad("SCENARIO_IDENTITY", "scenario/epoch differs from initial state")
        if not 1 <= len(state.orders) <= 8 or not 1 <= len(state.vehicles) <= 2:
            bad("STATE_SIZE", "witness is outside the bounded static contract")
        if any(order.status != "WAITING" or order.pickup_location_id != "DEPOT" for order in state.orders):
            bad("ORDER_STATE_UNSUPPORTED", "all static orders must be WAITING with depot pickup")
        if any(vehicle.availability != "AVAILABLE" or vehicle.current_load_kg != 0
               or vehicle.onboard_order_ids or vehicle.committed_stop_id is not None
               or vehicle.current_position_node_id != state.depot.graph_node_id
               for vehicle in state.vehicles):
            bad("VEHICLE_STATE_UNSUPPORTED", "vehicle is not empty/available at depot")
        for item, node, coordinates in (
            [("depot", state.depot.graph_node_id, state.depot.coordinates)]
            + [(f"order {o.order_id}", o.graph_node_id, o.coordinates) for o in state.orders]
            + [(f"vehicle {v.vehicle_id}", v.current_position_node_id,
                v.current_position_coordinates) for v in state.vehicles]
        ):
            actual = raw_node(node)
            if actual is None or max(abs(actual[i] - coordinates[i]) for i in range(2)) > 1e-6:
                bad("LOCATION_MISMATCH", "state WGS84 position differs from SQLite node", item=item)
        if issues:
            return {"status": "FAILED", "valid": False, "diagnostics": issues,
                    "validator_version": reported_validator_version}

        orders = {item.order_id: item for item in state.orders}
        vehicles = {item.vehicle_id: item for item in state.vehicles}
        epoch = datetime.fromisoformat(state.decision_epoch)
        depot_open = seconds(state.depot.opening_time, epoch)
        depot_close = seconds(state.depot.closing_time, epoch)
        served = result.get("served_orders")
        unserved = result.get("unserved_orders")
        routes = result.get("vehicle_routes")
        if not isinstance(served, list) or not isinstance(unserved, list) or not isinstance(routes, list):
            bad("SOLUTION_SCHEMA", "served/unserved/routes must be arrays")
            return {"status": "FAILED", "valid": False, "diagnostics": issues,
                    "validator_version": reported_validator_version}
        if any(not isinstance(item, Mapping) or not isinstance(item.get("order_id"), str)
               for item in unserved):
            bad("UNSERVED_SCHEMA", "unserved orders need a string order ID")
            return {"status": "FAILED", "valid": False, "diagnostics": issues,
                    "validator_version": reported_validator_version}
        for index, item in enumerate(unserved):
            reason = item.get("reason")
            if not isinstance(reason, str) or not reason or reason not in UNSERVED_REASONS:
                bad(
                    "UNSERVED_REASON_INVALID",
                    "unserved reason is not part of the static solver /1 contract",
                    path=f"unserved_orders[{index}].reason",
                    actual=reason,
                    allowed=sorted(UNSERVED_REASONS),
                )
        unserved_ids = [item["order_id"] for item in unserved]
        if (len(served) != len(set(served)) or len(unserved_ids) != len(set(unserved_ids))
            or set(served) & set(unserved_ids)
            or set(served) | set(unserved_ids) != set(orders)):
            bad("ORDER_PARTITION", "served/unserved must partition each input order exactly once")
        status = result.get("status")
        if status == "FEASIBLE" and (len(served) != len(orders) or unserved):
            bad("FALSE_FEASIBLE", "FEASIBLE requires full service and no unserved order")
        elif status == "PARTIAL" and (not served or not unserved):
            bad("FALSE_PARTIAL", "PARTIAL requires both served and unserved orders")
        elif status not in {"FEASIBLE", "PARTIAL", "TIME_LIMIT", "SEARCH_LIMIT",
                            "NO_SOLUTION_FOUND", "UNSUPPORTED", "INVALID_DATA",
                            "VERSION_MISMATCH", "SOURCE_MISMATCH"}:
            bad("STATUS_INVALID", "unknown static status", status=status)
        if status not in {"FEASIBLE", "PARTIAL"} and (served or routes):
            bad("STATUS_ROUTE_CONFLICT", "non-plan status cannot carry served/route payload")
        max_capacity = max((vehicle.capacity_kg for vehicle in state.vehicles), default=0.0)

        def deadline_unreachable(order: Any) -> bool:
            """Independent exhaustive lower bound inside the hard time cutoff.

            This does not call the solver or its path search.  It explores raw
            SQLite directed turn states by travel time and ignores return/load,
            which makes it a conservative outbound lower bound.
            """

            for vehicle in state.vehicles:
                departure = max(0.0, depot_open, seconds(vehicle.working_start, epoch))
                latest_arrival = min(
                    seconds(order.hard_deadline, epoch),
                    seconds(vehicle.working_end, epoch),
                ) - order.service_time_seconds
                if latest_arrival < departure - 1e-9:
                    continue
                initial = (state.depot.graph_node_id, None)
                best: dict[tuple[int, str | None], float] = {initial: 0.0}
                serial = 0
                queue: list[tuple[float, int, int, str | None]] = [
                    (0.0, serial, state.depot.graph_node_id, None)
                ]
                budget = latest_arrival - departure
                while queue:
                    travel, _, node, incoming = heapq.heappop(queue)
                    if travel != best.get((node, incoming)):
                        continue
                    if node == order.graph_node_id and (node, incoming) != initial:
                        return False
                    for row in graph.db.execute(
                        "SELECT e.edgeId,e.toNodeId,f.travelTimeHours "
                        "FROM edges e JOIN costs.features f USING(edgeId) "
                        "WHERE e.fromNodeId=? ORDER BY e.edgeId", (node,),
                    ):
                        edge_id, target, hours = row
                        if (incoming, edge_id) in forbidden:
                            continue
                        if type(hours) not in (int, float) or not math.isfinite(hours) or hours < 0:
                            raise ValueError(f"invalid travelTimeHours on edge {edge_id}")
                        following = travel + hours * 3600.0
                        if following > budget + 1e-9:
                            continue
                        turn_state = (int(target), str(edge_id))
                        if following + 1e-12 < best.get(turn_state, math.inf):
                            best[turn_state] = following
                            serial += 1
                            heapq.heappush(
                                queue, (following, serial, int(target), str(edge_id))
                            )
            return True

        for index, item in enumerate(unserved):
            if not isinstance(item, Mapping) or item.get("order_id") not in orders:
                continue
            order = orders[item["order_id"]]
            reason = item.get("reason")
            if reason == "CAPACITY_EXCEEDS_ALL_VEHICLES_NO_SPLIT" and not (
                order.demand_kg > max_capacity + 1e-9
            ):
                bad("UNSERVED_REASON_UNPROVEN",
                    "capacity proof reason is inconsistent with DecisionState",
                    order_id=item.get("order_id"),
                    path=f"unserved_orders[{index}].reason")
            if (reason == "HARD_COMPLETION_DEADLINE_UNREACHABLE_LOWER_BOUND"
                and not deadline_unreachable(order)):
                bad("UNSERVED_REASON_UNPROVEN",
                    "raw SQLite contains a turn-valid outbound path inside the completion cutoff",
                    order_id=item.get("order_id"),
                    path=f"unserved_orders[{index}].reason")
            if reason == "SEARCH_INCOMPLETE" and not (
                flags.get("truncated") is True and reasons_valid and bool(reasons)
            ):
                bad("UNSERVED_REASON_UNPROVEN",
                    "SEARCH_INCOMPLETE requires declared bounded-search truncation",
                    order_id=item.get("order_id"),
                    path=f"unserved_orders[{index}].reason")
        if status not in {"FEASIBLE", "PARTIAL"}:
            return {
                "status": "FAILED" if issues else "NOT_RUN",
                "valid": False if issues else None,
                "diagnostics": issues,
                "validator_version": reported_validator_version,
                "scope": "no witness route was available for feasibility validation",
            }
        totals = {key: 0.0 for key in ("total_distance_m", "total_travel_time_s",
                                      "total_exposure", "total_elapsed_time_s",
                                      "total_soft_lateness_s", "total_cost_vnd")}
        seen_vehicles: set[str] = set()
        route_served: list[str] = []
        for route_index, route in enumerate(routes):
            if not isinstance(route, Mapping):
                bad("ROUTE_SCHEMA", "vehicle route is not an object", route_index=route_index)
                continue
            vid = route.get("vehicle_id")
            if vid not in vehicles or vid in seen_vehicles:
                bad("VEHICLE_ASSIGNMENT", "unknown or duplicate vehicle route", vehicle_id=vid)
                continue
            seen_vehicles.add(vid)
            vehicle = vehicles[vid]
            sequence = route.get("order_sequence")
            if not isinstance(sequence, list) or any(oid not in orders for oid in sequence) or len(sequence) != len(set(sequence)) or not sequence:
                bad("ORDER_SEQUENCE", "route order sequence is invalid", vehicle_id=vid)
                continue
            route_served.extend(sequence)
            nodes = [state.depot.graph_node_id] + [orders[oid].graph_node_id for oid in sequence] + [state.depot.graph_node_id]
            legs = route.get("legs")
            stops = route.get("stops")
            if (route.get("start_node") != nodes[0] or route.get("end_node") != nodes[-1]
                or route.get("node_sequence") != nodes or not isinstance(legs, list)
                or len(legs) != len(nodes)-1 or not isinstance(stops, list)
                or len(stops) != len(sequence)):
                bad("ROUTE_SEQUENCE", "route node/leg/stop sequence is invalid", vehicle_id=vid)
                continue
            loaded = sum(orders[oid].demand_kg for oid in sequence)
            equal_number(route.get("load_before_pickup_kg"), 0.0, "LOAD_MISMATCH", "before depot pickup")
            equal_number(route.get("load_after_depot_pickup_kg"), loaded, "LOAD_MISMATCH", "depot pickup")
            equal_number(route.get("pickup_service_s"), 0.0, "PICKUP_TIME_MISMATCH", "depot loading service")
            if loaded > vehicle.capacity_kg + 1e-9:
                bad("CAPACITY_EXCEEDED", "depot load exceeds vehicle capacity", vehicle_id=vid)
            departure = max(0.0, depot_open, seconds(vehicle.working_start, epoch))
            equal_number(route.get("departure_s"), departure, "TIME_MISMATCH", "departure")
            clock = departure
            load = loaded
            incoming: str | None = None
            route_distance = route_travel = route_exposure = route_lateness = 0.0
            for leg_index, leg in enumerate(legs):
                if not isinstance(leg, Mapping):
                    bad("LEG_SCHEMA", "leg is not an object", vehicle_id=vid, leg_index=leg_index)
                    break
                start_node, end_node = nodes[leg_index:leg_index+2]
                if (leg.get("from_node") != start_node or leg.get("to_node") != end_node
                    or leg.get("incoming_edge") != incoming):
                    bad("LEG_ENDPOINT", "leg endpoint/incoming edge is wrong", vehicle_id=vid, leg_index=leg_index)
                edge_ids = leg.get("edge_ids")
                if not isinstance(edge_ids, list) or not edge_ids or any(not isinstance(e, str) for e in edge_ids):
                    bad("MISSING_EDGE", "leg has no valid directed edge sequence", vehicle_id=vid, leg_index=leg_index)
                    break
                cursor = start_node
                coordinates: list[list[float]] = []
                refs: list[dict[str, Any]] = []
                path_nodes = [start_node]
                flags: list[dict[str, Any]] = []
                distance = travel = exposure = 0.0
                for edge_id in edge_ids:
                    row = graph.db.execute(
                        "SELECT e.edgeId,e.fromNodeId,e.toNodeId,e.lengthKm,e.geometryJson,"
                        "f.travelTimeHours,f.relativeExposure,f.payloadJson "
                        "FROM edges e JOIN costs.features f USING(edgeId) WHERE e.edgeId=?",
                        (edge_id,)).fetchone()
                    if row is None:
                        bad("MISSING_EDGE", "edge is absent from opened SQLite", edge_id=edge_id)
                        break
                    edge = dict(row)
                    if edge["fromNodeId"] != cursor:
                        bad("EDGE_DIRECTION", "directed edge does not continue route", edge_id=edge_id)
                        break
                    if (incoming, edge_id) in forbidden:
                        bad("FORBIDDEN_TURN", "forbidden incoming/outgoing edge pair", before=incoming, after=edge_id)
                    geometry = json.loads(edge["geometryJson"])
                    points = geometry.get("coordinates") if isinstance(geometry, Mapping) and geometry.get("type") == "LineString" else None
                    a, b = raw_node(cursor), raw_node(edge["toNodeId"])
                    if (not isinstance(points, list) or len(points) < 2 or a is None or b is None
                        or any(not isinstance(point, list) or len(point) != 2
                               or any(type(value) not in (int, float) or not math.isfinite(value)
                                      for value in point)
                               or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90
                               for point in points)
                        or any(abs(points[0][i]-a[i]) > 1e-6 or abs(points[-1][i]-b[i]) > 1e-6 for i in range(2))):
                        bad("GEOMETRY_MISMATCH", "GeoJSON [longitude,latitude] does not match edge nodes", edge_id=edge_id)
                        break
                    payload = json.loads(edge["payloadJson"])
                    if (payload.get("edgeId") != edge_id
                        or payload.get("contextVersion") != state.context_version
                        or payload.get("fromNodeId") != cursor
                        or payload.get("toNodeId") != edge["toNodeId"]):
                        bad("FEATURE_PROVENANCE", "feature identity/context differs from raw edge", edge_id=edge_id)
                    coordinates.extend(points if not coordinates else points[1:])
                    refs.append({"u": cursor, "v": edge["toNodeId"], "key": edge_id})
                    path_nodes.append(edge["toNodeId"])
                    flags.append({key: payload.get(key) for key in _FLAGS})
                    distance += edge["lengthKm"] * 1000
                    travel += edge["travelTimeHours"] * 3600
                    exposure += edge["relativeExposure"]
                    cursor, incoming = edge["toNodeId"], edge_id
                if cursor != end_node:
                    bad("LEG_ENDPOINT", "selected edges do not reach next stop", vehicle_id=vid, leg_index=leg_index)
                if leg.get("final_edge") != incoming:
                    bad("FINAL_EDGE_MISMATCH", "last edge identity is wrong", leg_index=leg_index)
                for key, expected in (("distance_m", distance), ("travel_time_s", travel), ("exposure", exposure)):
                    equal_number(leg.get(key), expected, "PATH_METRIC_MISMATCH", f"leg {leg_index}.{key}")
                if leg.get("geometry") != coordinates or leg.get("feature_provenance") != flags:
                    bad("PATH_PROVENANCE_MISMATCH", "geometry or feature flags differ from SQLite", leg_index=leg_index)
                candidate = leg.get("candidate_path")
                if (not isinstance(candidate, Mapping) or candidate.get("path_id") != leg.get("path_id")
                    or candidate.get("nodes") != path_nodes or candidate.get("edges") != refs
                    or candidate.get("geometry") != coordinates):
                    bad("CANDIDATE_PATH_MISMATCH", "CandidatePath does not describe selected raw edges", leg_index=leg_index)
                else:
                    for key, expected in (("distance", distance), ("travel_time", travel), ("risk_score", exposure)):
                        equal_number(candidate.get(key), expected, "CANDIDATE_PATH_MISMATCH", key)
                route_distance += distance
                route_travel += travel
                route_exposure += exposure
                clock += travel
                if leg_index < len(sequence):
                    oid = sequence[leg_index]
                    order = orders[oid]
                    stop = stops[leg_index]
                    if not isinstance(stop, Mapping) or stop.get("order_id") != oid or stop.get("node_id") != end_node:
                        bad("STOP_SEQUENCE", "stop identity/node disagrees with route", leg_index=leg_index)
                        continue
                    equal_number(stop.get("arrival_s"), clock, "TIME_MISMATCH", f"{oid} arrival")
                    service_start = max(clock, seconds(order.earliest, epoch))
                    completion = service_start + order.service_time_seconds
                    equal_number(stop.get("service_start_s"), service_start, "TIME_MISMATCH", f"{oid} service start")
                    equal_number(stop.get("completion_s"), completion, "TIME_MISMATCH", f"{oid} completion")
                    equal_number(stop.get("waiting_s"), service_start-clock, "TIME_MISMATCH", f"{oid} waiting")
                    if completion > min(seconds(order.hard_deadline, epoch), seconds(vehicle.working_end, epoch)) + 1e-6:
                        bad("DEADLINE_VIOLATION", "service completion exceeds hard deadline", order_id=oid)
                    late = max(0.0, completion-seconds(order.preferred_due, epoch))
                    equal_number(stop.get("soft_lateness_s"), late, "SOFT_DUE_MISMATCH", oid)
                    route_lateness += late
                    load -= order.demand_kg
                    equal_number(stop.get("load_after_delivery_kg"), load, "LOAD_MISMATCH", oid)
                    if load < -1e-9 or load > vehicle.capacity_kg + 1e-9:
                        bad("CAPACITY_EXCEEDED", "load transition is outside capacity", order_id=oid)
                    clock = completion
            equal_number(route.get("return_load_kg"), load, "LOAD_MISMATCH", "return load")
            equal_number(route.get("return_s"), clock, "TIME_MISMATCH", "return time")
            if clock > min(depot_close, seconds(vehicle.working_end, epoch)) + 1e-6:
                bad("RETURN_WINDOW", "route returns after depot/vehicle closes", vehicle_id=vid)
            if route_distance > vehicle.remaining_range_m + 1e-6:
                bad("RANGE_EXCEEDED", "full return distance exceeds remaining range", vehicle_id=vid)
            route_cost = route_distance / 1000 * vehicle.cost_per_km_vnd
            for key, expected in (("total_distance_m", route_distance),
                                  ("total_travel_time_s", route_travel),
                                  ("total_exposure", route_exposure),
                                  ("total_elapsed_time_s", clock-departure),
                                  ("total_soft_lateness_s", route_lateness),
                                  ("total_cost_vnd", route_cost)):
                equal_number(route.get(key), expected, "ROUTE_METRIC_MISMATCH", f"{vid}.{key}")
                totals[key] += expected
        if sorted(route_served) != sorted(served):
            bad("SERVED_ROUTE_MISMATCH", "served orders differ from route stops")
        metrics = result.get("metrics")
        if not isinstance(metrics, Mapping):
            bad("FLEET_METRIC_MISMATCH", "metrics object is missing")
        else:
            for key, expected in totals.items():
                equal_number(metrics.get(key), expected, "FLEET_METRIC_MISMATCH", key)
    except (KeyError, IndexError, TypeError, ValueError, sqlite3.Error, OverflowError) as error:
        bad("VALIDATION_INPUT_ERROR", "malformed solution cannot be certified", reason=str(error))
    return {"status": "PASSED" if not issues else "FAILED",
            "valid": not issues, "diagnostics": issues,
            "validator_version": reported_validator_version,
            "scope": "witness feasibility and metadata consistency; not optimality"}


__all__ = ["VALIDATOR_VERSION", "validate_static_solution"]
