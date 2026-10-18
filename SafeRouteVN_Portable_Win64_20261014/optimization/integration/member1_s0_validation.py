"""Independent raw-SQLite validation of a versioned Member 1 S0 witness."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from typing import Any, Mapping

from .member1_s0 import SCHEMA_VERSION, SOLVER_VERSION
from .member1_s0_graph import Member1RoadGraph


VALIDATOR_VERSION = "member1-s0-independent-validator-v3"


def validate_s0_solution(
    result: Mapping[str, Any], fixture: Mapping[str, Any], graph: Member1RoadGraph
) -> dict[str, Any]:
    """Recompute every edge, time/load transition and metric from M1 SQLite.

    This does not call the solver's road search or trust its CandidatePath
    totals. Incoming edge is carried through *all* stops and the depot return.
    """

    issues: list[dict[str, Any]] = []

    def bad(code: str, message: str, **context: Any) -> None:
        issues.append({"severity": "ERROR", "code": code, "message": message, "context": context})

    def same(actual: Any, expected: float, code: str, *, context: str) -> None:
        if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(actual) or not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-5):
            bad(code, f"{context} differs from source recomputation", actual=actual, expected=expected)

    def seconds(raw: str, epoch: datetime) -> float:
        if not isinstance(raw, str) or not raw.endswith("+07:00"):
            raise ValueError("timestamp lacks explicit +07:00")
        return (datetime.fromisoformat(raw) - epoch).total_seconds()

    def nonnegative(raw: Any, field: str) -> bool:
        valid = (not isinstance(raw, bool) and isinstance(raw, (int, float))
                 and math.isfinite(raw) and raw >= 0)
        if not valid:
            bad("INPUT_VALUE_INVALID", "expected finite nonnegative number", field=field, value=raw)
        return valid

    def location(item: Any, field: str) -> int | None:
        if not isinstance(item, Mapping):
            bad("LOCATION_INVALID", "location must be an object", field=field)
            return None
        node = item.get("graphNodeId")
        if isinstance(node, bool) or not isinstance(node, int):
            bad("LOCATION_INVALID", "graphNodeId must be an integer", field=field)
            return None
        point = graph.node(node)
        lon, lat = item.get("longitude"), item.get("latitude")
        if (point is None or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                  or not math.isfinite(v) for v in (lon, lat))
            or not -180 <= lon <= 180 or not -90 <= lat <= 90
            or abs(lon - point[0]) > 1e-6 or abs(lat - point[1]) > 1e-6):
            bad("LOCATION_INVALID", "WGS84 [longitude,latitude] must match graph node", field=field, node=node)
        return node

    try:
        accepted_versions = {
            ("member1-s0-integration/1", "s0-turn-state-search-v1"),
            (SCHEMA_VERSION, SOLVER_VERSION),
        }
        if (result.get("schema_version"), result.get("solver_version")) not in accepted_versions:
            bad("SCHEMA_VERSION_MISMATCH", "integration envelope version is wrong")
        expected_versions = {
            "routing_version": fixture["routingVersion"],
            "features_version": fixture["featuresVersion"],
            "context_version": fixture["contextVersion"],
            "delivery_area_version": fixture["deliveryAreaVersion"],
            "state_version": fixture["initialState"]["stateVersion"],
        }
        if result.get("source_versions") != expected_versions or (
            expected_versions["routing_version"] != graph.routing_version
            or expected_versions["features_version"] != graph.features_version
            or expected_versions["context_version"] != graph.context_version
        ):
            bad("VERSION_MISMATCH", "result, fixture and graph versions differ")
        if result.get("source_hashes") != graph.source_hashes:
            bad("SOURCE_HASH_MISMATCH", "source hashes differ from opened graph")
        if result.get("scenario_id") != fixture["scenarioId"]:
            bad("SCENARIO_MISMATCH", "scenario ID differs")
        if fixture.get("scenarioId") != "S0" or fixture.get("schemaVersion") != "member1-scenario-draft/1":
            bad("SCENARIO_CONTRACT_MISMATCH", "only Member 1 S0 scenario draft is supported")
        if fixture.get("events") or fixture.get("executionUpdates"):
            bad("STATE_UPDATES_UNSUPPORTED", "S0 does not reconcile event or execution updates")
        state = fixture["initialState"]
        if not isinstance(state.get("currentPlans"), list):
            bad("CURRENT_PLANS_INVALID", "currentPlans must be an explicit list")
        elif state["currentPlans"]:
            bad("CURRENT_PLANS_UNSUPPORTED", "S0 witness cannot be certified against existing plans")
        if isinstance(state.get("stateVersion"), bool) or not isinstance(state.get("stateVersion"), int) or state["stateVersion"] < 0:
            bad("STATE_VERSION_INVALID", "stateVersion must be a nonnegative integer")
        if not isinstance(state.get("currentTime"), str) or not state["currentTime"].endswith("+07:00"):
            bad("TIME_INVALID", "currentTime requires explicit +07:00", field="initialState.currentTime")
            return {"valid": False, "diagnostics": issues, "validator_version": VALIDATOR_VERSION}
        epoch = datetime.fromisoformat(state["currentTime"])
        if not isinstance(state.get("locations"), list) or len(state["locations"]) != 1 or not isinstance(state["locations"][0], Mapping) or state["locations"][0].get("id") != "DEPOT":
            bad("DEPOT_INVALID", "S0 requires exactly one DEPOT")
            return {"valid": False, "diagnostics": issues, "validator_version": VALIDATOR_VERSION}
        depot = state["locations"][0]
        depot_node = location(depot, "DEPOT")
        depot_open = seconds(depot["openingTime"], epoch)
        depot_close = seconds(depot["closingTime"], epoch)
        if depot_open > depot_close:
            bad("DEPOT_WINDOW_INVALID", "depot opening exceeds closing")
        if not isinstance(state.get("orders"), list) or not 1 <= len(state["orders"]) <= 3 or not isinstance(state.get("vehicles"), list) or not 1 <= len(state["vehicles"]) <= 2:
            bad("S0_SIZE_UNSUPPORTED", "S0 supports one to three orders and one or two vehicles")
            return {"valid": False, "diagnostics": issues, "validator_version": VALIDATOR_VERSION}
        orders = {item["id"]: item for item in state["orders"]}
        vehicles = {item["id"]: item for item in state["vehicles"]}
        if len(orders) != len(state["orders"]) or len(vehicles) != len(state["vehicles"]):
            bad("DUPLICATE_INPUT_ID", "order/vehicle identifiers must be unique")
        for oid, order in orders.items():
            if order.get("status") == "ONBOARD" and not order.get("assignedVehicleId"):
                bad("ORDER_CUSTODY_INVALID", "ONBOARD order has no owner", order_id=oid)
            elif order.get("status") != "WAITING":
                bad("ORDER_STATE_UNSUPPORTED", "S0 only supports WAITING orders", order_id=oid, status=order.get("status"))
            if (order.get("assignedVehicleId") is not None or order.get("pickedUpAt") is not None
                or order.get("deliveredAt") is not None) and order.get("status") == "WAITING":
                bad("ORDER_CUSTODY_UNSUPPORTED", "WAITING order has custody history", order_id=oid)
            if order.get("pickupLocationId") != "DEPOT":
                bad("PICKUP_UNSUPPORTED", "S0 pickup must be at DEPOT", order_id=oid)
            location(order, f"order {oid}")
            nonnegative(order.get("demandKg"), f"order {oid}.demandKg")
            nonnegative(order.get("serviceTimeHours"), f"order {oid}.serviceTimeHours")
            earliest = seconds(order["earliest"], epoch)
            preferred = seconds(order["preferredDue"], epoch)
            deadline = seconds(order["hardDeadline"], epoch)
            if not earliest <= preferred <= deadline:
                bad("ORDER_WINDOW_INVALID", "earliest/preferredDue/hardDeadline are inconsistent", order_id=oid)
        for vid, vehicle in vehicles.items():
            if (vehicle.get("availability") != "AVAILABLE" or vehicle.get("onboardOrderIds") != []
                or vehicle.get("currentLoadKg") != 0 or vehicle.get("committedStopId") is not None):
                bad("VEHICLE_STATE_UNSUPPORTED", "S0 requires AVAILABLE, empty, uncommitted vehicle", vehicle_id=vid)
            position = vehicle.get("currentPosition")
            vehicle_node = location(position, f"vehicle {vid}.currentPosition")
            if vehicle_node is not None and isinstance(position, Mapping) and graph.node(vehicle_node) is not None:
                point = graph.node(vehicle_node)
                if any(not isinstance(position.get(k), (int, float)) or isinstance(position.get(k), bool)
                       or not math.isfinite(position[k]) or abs(position[k] - point[i]) > 1e-6
                       for i, k in enumerate(("longitude", "latitude"))):
                    bad("VEHICLE_LOCATION_INVALID", "vehicle position coordinates disagree with graph", vehicle_id=vid)
            if vehicle_node is not None and vehicle_node != depot_node:
                bad("VEHICLE_POSITION_UNSUPPORTED", "S0 vehicle must start at DEPOT", vehicle_id=vid)
            for key in ("capacityKg", "currentLoadKg", "rangeKm", "remainingRangeKm"):
                nonnegative(vehicle.get(key), f"vehicle {vid}.{key}")
            if (isinstance(vehicle.get("rangeKm"), (int, float)) and isinstance(vehicle.get("remainingRangeKm"), (int, float))
                and vehicle["remainingRangeKm"] > vehicle["rangeKm"]):
                bad("RANGE_INVALID", "remainingRangeKm exceeds rangeKm", vehicle_id=vid)
            if seconds(vehicle["workingStart"], epoch) > seconds(vehicle["workingEnd"], epoch):
                bad("WORK_WINDOW_INVALID", "vehicle working window is reversed", vehicle_id=vid)
        if issues:
            return {"valid": False, "diagnostics": issues, "validator_version": VALIDATOR_VERSION}
        served = result.get("served_orders", [])
        unserved = result.get("unserved_orders", [])
        if len(served) != len(set(served)) or len(unserved) != len({item["order_id"] for item in unserved}):
            bad("DUPLICATE_ORDER", "served/unserved contains a duplicate")
        if set(served) & {item["order_id"] for item in unserved} or set(served) | {item["order_id"] for item in unserved} != set(orders):
            bad("ORDER_COMPLETENESS", "served and unserved must partition all orders")
        if any(not isinstance(item.get("reason"), str) or not item["reason"] for item in unserved):
            bad("UNSERVED_REASON_MISSING", "every unserved order needs a reason")
        status = result.get("status")
        if status == "FEASIBLE" and (len(served) != len(orders) or unserved):
            bad("FALSE_FEASIBLE", "FEASIBLE requires all orders served and none unserved")
        elif status == "PARTIAL" and (not served or not unserved):
            bad("PARTIAL_COMPLETENESS", "PARTIAL requires both served and unserved orders")
        elif status not in {"FEASIBLE", "PARTIAL", "NO_SOLUTION_FOUND", "SEARCH_LIMIT",
                            "TIME_LIMIT", "UNSUPPORTED", "INVALID_DATA", "VERSION_MISMATCH"}:
            bad("STATUS_INVALID", "unknown S0 solver status", status=status)
        if status not in {"FEASIBLE", "PARTIAL"} and (served or result.get("vehicle_routes")):
            bad("STATUS_ROUTE_CONFLICT", "non-plan status cannot claim served orders or route payload")
        route_served: list[str] = []
        totals = {"total_distance_m": 0.0, "total_travel_time_s": 0.0,
                  "total_exposure": 0.0, "total_elapsed_time_s": 0.0,
                  "total_soft_lateness_s": 0.0}
        seen_vehicles: set[str] = set()
        for route in result.get("vehicle_routes", []):
            vid = route.get("vehicle_id")
            if vid not in vehicles or vid in seen_vehicles:
                bad("VEHICLE_ASSIGNMENT", "route has unknown or duplicate vehicle", vehicle_id=vid)
                continue
            seen_vehicles.add(vid)
            vehicle = vehicles[vid]
            if vehicle.get("availability") != "AVAILABLE" or vehicle.get("onboardOrderIds") != [] or vehicle.get("currentLoadKg") != 0:
                bad("UNSUPPORTED_VEHICLE_STATE", "vehicle cannot be used in S0", vehicle_id=vid)
            sequence = route.get("order_sequence", [])
            if len(sequence) != len(set(sequence)) or any(oid not in orders for oid in sequence):
                bad("ORDER_SEQUENCE", "route order sequence is invalid", vehicle_id=vid)
                continue
            route_served.extend(sequence)
            if [stop.get("order_id") for stop in route.get("stops", [])] != sequence:
                bad("STOP_SEQUENCE", "stops disagree with order sequence", vehicle_id=vid)
            if route.get("start_node") != depot_node or route.get("end_node") != depot_node:
                bad("DEPOT_ENDPOINT", "route must start and end at depot", vehicle_id=vid)
            expected_nodes = [depot_node] + [orders[oid]["graphNodeId"] for oid in sequence] + [depot_node]
            if route.get("node_sequence") != expected_nodes or len(route.get("legs", [])) != len(expected_nodes) - 1:
                bad("ROUTE_SEQUENCE", "decision node/leg sequence is invalid", vehicle_id=vid)
                continue
            load = sum(orders[oid]["demandKg"] for oid in sequence)
            same(route.get("load_before_pickup_kg"), 0.0, "LOAD_MISMATCH", context="load before depot pickup")
            same(route.get("load_after_depot_pickup_kg"), load, "LOAD_MISMATCH", context="depot pickup load")
            same(route.get("pickup_service_s"), 0.0, "PICKUP_TIME_MISMATCH", context="zero documented depot loading duration")
            if load > vehicle["capacityKg"] + 1e-9:
                bad("CAPACITY_EXCEEDED", "loaded cargo exceeds vehicle capacity", vehicle_id=vid)
            departure = max(0.0, depot_open, seconds(vehicle["workingStart"], epoch))
            same(route.get("departure_s"), departure, "TIME_MISMATCH", context="departure")
            clock = departure
            incoming = None
            total_distance = total_travel = total_exposure = total_lateness = 0.0
            for index, leg in enumerate(route["legs"]):
                current = expected_nodes[index]
                target = expected_nodes[index + 1]
                if leg.get("from_node") != current or leg.get("to_node") != target or leg.get("incoming_edge") != incoming:
                    bad("LEG_ENDPOINT", "leg endpoint/incoming edge does not match route", vehicle_id=vid, leg_index=index)
                node = current
                edge_ids = leg.get("edge_ids", [])
                if not isinstance(edge_ids, list) or not edge_ids:
                    bad("MISSING_EDGE", "road leg has no directed edges", vehicle_id=vid, leg_index=index)
                    continue
                nodes = [current]
                edge_refs = []
                geometry = []
                flags = []
                distance = travel = exposure = 0.0
                for edge_id in edge_ids:
                    row = graph.db.execute(
                        "SELECT e.edgeId,e.fromNodeId,e.toNodeId,e.lengthKm,e.geometryJson,"
                        "f.travelTimeHours,f.relativeExposure,f.payloadJson "
                        "FROM edges e JOIN costs.features f USING(edgeId) WHERE e.edgeId=?", (edge_id,)
                    ).fetchone()
                    if row is None:
                        bad("MISSING_EDGE", "edge absent from routing/features snapshot", edge_id=edge_id)
                        break
                    edge = dict(row)
                    if edge["fromNodeId"] != node:
                        bad("EDGE_DIRECTION", "edge direction/continuity is invalid", edge_id=edge_id)
                        break
                    if (incoming, edge_id) in graph.forbidden:
                        bad("FORBIDDEN_TURN", "full route uses forbidden incoming/outgoing pair", before=incoming, after=edge_id)
                    payload = json.loads(edge["payloadJson"])
                    points = json.loads(edge["geometryJson"])["coordinates"]
                    if not isinstance(points, list) or len(points) < 2:
                        bad("GEOMETRY_MISMATCH", "edge geometry is malformed", edge_id=edge_id)
                        break
                    from_coord = graph.node(node)
                    to_coord = graph.node(edge["toNodeId"])
                    if from_coord is None or to_coord is None or any(abs(a-b) > 1e-6 for a,b in zip(points[0],from_coord)) or any(abs(a-b) > 1e-6 for a,b in zip(points[-1],to_coord)):
                        bad("GEOMETRY_MISMATCH", "edge geometry endpoints disagree with graph nodes", edge_id=edge_id)
                    geometry.extend(points if not geometry else points[1:])
                    edge_refs.append({"u": node, "v": edge["toNodeId"], "key": edge_id})
                    nodes.append(edge["toNodeId"])
                    distance += edge["lengthKm"] * 1000
                    travel += edge["travelTimeHours"] * 3600
                    exposure += edge["relativeExposure"]
                    flags.append({key: payload.get(key) for key in (
                        "edgeId", "contextVersion", "missingFlags", "fallbackUsed",
                        "weatherFallbackUsed", "weatherRegionId", "weatherValidAt",
                        "sourceType", "travelSourceType", "riskModelVersion",
                    )})
                    node, incoming = edge["toNodeId"], edge_id
                if node != target:
                    bad("LEG_ENDPOINT", "road leg does not reach decision node", vehicle_id=vid, leg_index=index)
                if leg.get("final_edge") != incoming:
                    bad("FINAL_EDGE_MISMATCH", "leg final edge is incorrect", vehicle_id=vid, leg_index=index)
                same(leg.get("distance_m"), distance, "PATH_METRIC_MISMATCH", context="leg distance")
                same(leg.get("travel_time_s"), travel, "PATH_METRIC_MISMATCH", context="leg travel time")
                same(leg.get("exposure"), exposure, "PATH_METRIC_MISMATCH", context="leg exposure")
                if leg.get("geometry") != geometry or leg.get("feature_provenance") != flags:
                    bad("PATH_PROVENANCE_MISMATCH", "leg geometry/feature flags differ from source", leg_index=index)
                candidate = leg.get("candidate_path", {})
                if (candidate.get("path_id") != leg.get("path_id") or candidate.get("nodes") != nodes
                    or candidate.get("edges") != edge_refs or candidate.get("geometry") != geometry):
                    bad("CANDIDATE_PATH_MISMATCH", "CandidatePath does not match selected raw edges", leg_index=index)
                same(candidate.get("distance"), distance, "PATH_METRIC_MISMATCH", context="CandidatePath distance")
                same(candidate.get("travel_time"), travel, "PATH_METRIC_MISMATCH", context="CandidatePath time")
                same(candidate.get("risk_score"), exposure, "PATH_METRIC_MISMATCH", context="CandidatePath exposure")
                total_distance += distance
                total_travel += travel
                total_exposure += exposure
                clock += travel
                if index < len(sequence):
                    oid = sequence[index]
                    order = orders[oid]
                    stop = route["stops"][index]
                    same(stop.get("arrival_s"), clock, "TIME_MISMATCH", context="order arrival")
                    service_start = max(clock, seconds(order["earliest"], epoch))
                    completion = service_start + order["serviceTimeHours"] * 3600
                    same(stop.get("service_start_s"), service_start, "TIME_MISMATCH", context="service start")
                    same(stop.get("completion_s"), completion, "TIME_MISMATCH", context="service completion")
                    same(stop.get("waiting_s"), service_start - clock, "TIME_MISMATCH", context="waiting")
                    if completion > seconds(order["hardDeadline"], epoch) + 1e-6:
                        bad("DEADLINE_VIOLATION", "service finishes after hardDeadline", order_id=oid)
                    lateness = max(0.0, completion - seconds(order["preferredDue"], epoch))
                    same(stop.get("soft_lateness_s"), lateness, "SOFT_DUE_MISMATCH", context="preferredDue lateness")
                    total_lateness += lateness
                    load -= order["demandKg"]
                    same(stop.get("load_after_delivery_kg"), load, "LOAD_MISMATCH", context="load after delivery")
                    if load < -1e-9 or load > vehicle["capacityKg"] + 1e-9:
                        bad("CAPACITY_EXCEEDED", "load outside vehicle capacity", vehicle_id=vid)
                    clock = completion
            same(route.get("return_load_kg"), load, "LOAD_MISMATCH", context="return load")
            same(route.get("return_s"), clock, "TIME_MISMATCH", context="return")
            if clock > min(depot_close, seconds(vehicle["workingEnd"], epoch)) + 1e-6:
                bad("RETURN_WINDOW", "vehicle returns after depot/working close", vehicle_id=vid)
            if total_distance > vehicle["remainingRangeKm"] * 1000 + 1e-6:
                bad("RANGE_EXCEEDED", "route exceeds remainingRangeKm", vehicle_id=vid)
            same(route.get("total_distance_m"), total_distance, "ROUTE_METRIC_MISMATCH", context="route distance")
            same(route.get("total_travel_time_s"), total_travel, "ROUTE_METRIC_MISMATCH", context="route travel time")
            same(route.get("total_exposure"), total_exposure, "ROUTE_METRIC_MISMATCH", context="route exposure")
            same(route.get("total_elapsed_time_s"), clock - departure, "ROUTE_METRIC_MISMATCH", context="route elapsed time")
            same(route.get("total_soft_lateness_s"), total_lateness, "ROUTE_METRIC_MISMATCH", context="route lateness")
            totals["total_distance_m"] += total_distance
            totals["total_travel_time_s"] += total_travel
            totals["total_exposure"] += total_exposure
            totals["total_elapsed_time_s"] += clock - departure
            totals["total_soft_lateness_s"] += total_lateness
        if sorted(route_served) != sorted(served):
            bad("SERVED_ROUTE_MISMATCH", "served_orders differs from vehicle routes")
        for key, expected in totals.items():
            same(result.get("metrics", {}).get(key), expected, "FLEET_METRIC_MISMATCH", context=key)
    except (KeyError, IndexError, TypeError, ValueError, sqlite3.Error) as error:
        bad("VALIDATION_INPUT_ERROR", "validator cannot verify malformed solution", reason=str(error))
    return {"valid": not issues, "diagnostics": issues,
            "validator_version": VALIDATOR_VERSION}
