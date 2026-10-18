"""Independent raw-source validator for Step-4 motion snapshots.

The implementation intentionally does not import the replay engine or its
timeline builder.  It reconstructs a small execution oracle from raw SQLite
edges and the authenticated initial state/plan.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from optimization.integration.member1_s0_graph import Member1RoadGraph, RoadDataError
from optimization.models.decision_state import DecisionState
from optimization.models.motion_state import (
    MOTION_UNIT_METADATA, MotionContractError, MotionState, canonical_json,
)


MOTION_VALIDATOR_VERSION = "task02-m1-motion-independent-validator/2"

_I64_MAX = (1 << 63) - 1


def _offset(epoch: datetime, value: str) -> int:
    delta = _when(value) - epoch
    result = ((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds)
    if abs(result) > _I64_MAX:
        raise ValueError("timestamp offset exceeds int64")
    return result


def _us(value: Any) -> int:
    if type(value) not in (int, float, Decimal):
        raise MotionContractError("INVALID_DATA", "plan", "finite numeric time required")
    try:
        numeric = float(value)
    except OverflowError as error:
        raise MotionContractError("INVALID_DATA", "plan", "finite numeric time required") from error
    if not math.isfinite(numeric) or numeric < 0:
        raise MotionContractError("INVALID_DATA", "plan", "finite nonnegative time required")
    result = int((Decimal(str(value)) * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))
    if result > _I64_MAX:
        raise MotionContractError("INVALID_DATA", "plan", "microsecond time exceeds int64")
    return result


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _iso(epoch: datetime, offset: int) -> str:
    return (epoch + timedelta(microseconds=offset)).isoformat()


def _haversine(a: Sequence[float], b: Sequence[float]) -> float:
    x1, y1 = map(math.radians, a); x2, y2 = map(math.radians, b)
    dx, dy = x2 - x1, y2 - y1
    v = math.sin(dy / 2) ** 2 + math.cos(y1) * math.cos(y2) * math.sin(dx / 2) ** 2
    return 6_371_008.8 * 2 * math.asin(min(1.0, math.sqrt(v)))


def _point(points: Sequence[Sequence[float]], fraction: float) -> list[float]:
    lengths = [_haversine(points[i], points[i + 1]) for i in range(len(points) - 1)]
    target, cursor = sum(lengths) * fraction, 0.0
    for i, length in enumerate(lengths):
        if target <= cursor + length or i == len(lengths) - 1:
            local = 0.0 if length == 0 else (target - cursor) / length
            return [float(points[i][axis]) + local * (float(points[i + 1][axis]) - float(points[i][axis]))
                    for axis in (0, 1)]
        cursor += length
    return [float(x) for x in points[-1]]


def _aid(vehicle: str, kind: str, ordinal: int, identity: str) -> str:
    return "act-" + hashlib.sha256(f"{vehicle}|{kind}|{ordinal}|{identity}".encode()).hexdigest()[:20]


def _actions(state: DecisionState, plan: Mapping[str, Any], graph: Member1RoadGraph) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(plan, Mapping):
        raise MotionContractError("INVALID_DATA", "plan", "plan object required")
    if not isinstance(plan.get("status"), str) or plan["status"] not in {"FEASIBLE", "PARTIAL"}:
        raise MotionContractError("INVALID_DATA", "plan.status", "witness status required")
    routes = plan.get("vehicle_routes")
    if not isinstance(routes, list):
        raise MotionContractError("INVALID_DATA", "plan.vehicle_routes", "route array required")
    by_order = {item.order_id: item for item in state.orders}
    by_vehicle = {item.vehicle_id: item for item in state.vehicles}
    epoch = _when(state.decision_epoch)
    result: dict[str, list[dict[str, Any]]] = {}
    seen_orders: set[str] = set()
    for ri, route in enumerate(routes):
        path = f"plan.vehicle_routes[{ri}]"
        if not isinstance(route, Mapping):
            raise MotionContractError("INVALID_DATA", path, "route object required")
        vehicle_id = route.get("vehicle_id")
        if not isinstance(vehicle_id, str) or vehicle_id not in by_vehicle or vehicle_id in result:
            raise MotionContractError("INVALID_DATA", f"{path}.vehicle_id", "unique known vehicle required")
        for field in ("order_sequence", "legs", "stops", "node_sequence"):
            if not isinstance(route.get(field), list):
                raise MotionContractError("INVALID_DATA", f"{path}.{field}", "array required")
        sequence = route["order_sequence"]
        if (len(route["legs"]) != len(sequence) + 1 or len(route["stops"]) != len(sequence)
                or len(route["node_sequence"]) != len(route["legs"]) + 1):
            raise MotionContractError("INVALID_DATA", path, "route cardinality mismatch")
        for j, oid in enumerate(sequence):
            if not isinstance(oid, str) or not oid or oid not in by_order or oid in seen_orders:
                raise MotionContractError("INVALID_DATA", f"{path}.order_sequence[{j}]",
                                          "unique known order ID required")
            seen_orders.add(oid)
        vehicle = by_vehicle[vehicle_id]
        cursor = _us(route["departure_s"])
        lower = max(_offset(epoch, state.depot.opening_time), _offset(epoch, vehicle.working_start))
        upper = min(_offset(epoch, state.depot.closing_time), _offset(epoch, vehicle.working_end))
        if (vehicle.availability != "AVAILABLE" or vehicle.current_position_node_id != state.depot.graph_node_id
                or route["node_sequence"][0] != state.depot.graph_node_id
                or route["node_sequence"][-1] != state.depot.graph_node_id):
            raise MotionContractError("INVALID_DATA", f"{path}.node_sequence", "route depot/vehicle invalid")
        if cursor < lower or cursor > upper:
            raise MotionContractError("WORKING_WINDOW", f"{path}.departure_s", "departure outside window")
        pickup_load = vehicle.current_load_kg + sum(by_order[oid].demand_kg for oid in sequence
                                                     if by_order[oid].status == "WAITING")
        if pickup_load > vehicle.capacity_kg + 1e-9:
            raise MotionContractError("CAPACITY", f"{path}.order_sequence", "pickup exceeds capacity")
        actions: list[dict[str, Any]] = []
        ordinal = 0
        for oid in sequence:
            if by_order[oid].status == "WAITING":
                actions.append({"id": _aid(vehicle_id, "PICKUP", ordinal, oid), "kind": "PICKUP",
                                "start": cursor, "end": cursor, "order": oid,
                                "node": state.depot.graph_node_id})
                ordinal += 1
        incoming = None
        local = route["node_sequence"][0]
        for li, leg in enumerate(route["legs"]):
            if not isinstance(leg, Mapping) or not isinstance(leg.get("edge_ids"), list):
                raise MotionContractError("INVALID_DATA", f"{path}.legs[{li}]", "leg object required")
            if leg.get("incoming_edge") != incoming:
                raise ValueError(f"turn state differs at route {ri} leg {li}")
            for ei, edge_id in enumerate(leg["edge_ids"]):
                raw = graph.edge(edge_id)
                if raw is None:
                    raise ValueError(f"missing edge at route {ri} leg {li} edge {ei}")
                try:
                    edge = graph._checked_edge(raw)
                except RoadDataError as error:
                    raise ValueError(str(error)) from error
                if edge["fromNodeId"] != local or (incoming, edge_id) in graph.forbidden:
                    raise ValueError(f"invalid directed chain at route {ri} leg {li} edge {ei}")
                duration = _us(Decimal(str(edge["travelTimeHours"])) * Decimal(3600))
                geometry = [[p.longitude, p.latitude] for p in edge["points"]]
                actions.append({"id": _aid(vehicle_id, "EDGE", ordinal, edge_id), "kind": "EDGE",
                                "start": cursor, "end": cursor + duration, "edge": edge_id,
                                "from": edge["fromNodeId"], "to": edge["toNodeId"],
                                "incoming": incoming, "distance": float(edge["lengthKm"]) * 1000.0,
                                "exposure": float(edge["relativeExposure"]), "geometry": geometry})
                ordinal += 1; cursor += duration; local = edge["toNodeId"]; incoming = edge_id
            if local != leg["to_node"] or incoming != leg.get("final_edge"):
                raise ValueError(f"leg endpoint differs at route {ri} leg {li}")
            if li < len(route["stops"]):
                oid = route["order_sequence"][li]; order = by_order[oid]
                delta = _when(order.earliest) - epoch
                earliest = ((delta.days * 86_400 + delta.seconds) * 1_000_000
                            + delta.microseconds)
                start = max(cursor, earliest)
                if start > cursor:
                    actions.append({"id": _aid(vehicle_id, "WAIT", ordinal, oid), "kind": "WAIT",
                                    "start": cursor, "end": start, "order": oid, "node": local,
                                    "incoming": incoming})
                    ordinal += 1
                end = start + _us(order.service_time_seconds)
                if end > _offset(epoch, order.hard_deadline):
                    raise MotionContractError("HARD_DEADLINE", f"orders.{oid}.hard_deadline",
                                              "quantized completion exceeds hard deadline")
                actions.append({"id": _aid(vehicle_id, "SERVICE", ordinal, oid), "kind": "SERVICE",
                                "start": start, "end": end, "order": oid, "node": local,
                                "incoming": incoming})
                ordinal += 1; cursor = end
        distance = sum(a["distance"] for a in actions if a["kind"] == "EDGE")
        if distance > vehicle.remaining_range_m + 1e-7:
            raise MotionContractError("RANGE", f"{path}.legs", "full return exceeds remaining range")
        if cursor > upper:
            raise MotionContractError("WORKING_WINDOW", f"{path}.return_s", "return outside window")
        result[vehicle_id] = actions
    return result


def _done(action: Mapping[str, Any], query: int) -> bool:
    return query > action["start"] if action["start"] == action["end"] else query >= action["end"]


def _active(action: Mapping[str, Any], query: int) -> bool:
    return action["start"] < query < action["end"]


def _close(expected: Any, actual: Any, path: str, issues: list[dict[str, Any]]) -> None:
    if type(expected) is bool and type(actual) is not bool:
        issues.append({"severity": "ERROR", "code": "INVALID_DATA",
                       "message": "boolean contract flag required", "context": {"path": path}})
        return
    if type(actual) is bool and type(expected) in (int, float):
        issues.append({"severity": "ERROR", "code": "INVALID_DATA",
                       "message": "boolean is not a numeric metric", "context": {"path": path}})
        return
    if type(expected) in (int, float) and type(actual) in (int, float):
        if not math.isclose(float(expected), float(actual), rel_tol=1e-11, abs_tol=1e-7):
            issues.append({"severity": "ERROR", "code": "MOTION_MISMATCH",
                           "message": "numeric replay value differs", "context": {"path": path}})
        return
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        if set(expected) != set(actual):
            issues.append({"severity": "ERROR", "code": "MOTION_MISMATCH",
                           "message": "object keys differ", "context": {"path": path}})
            return
        for key in expected:
            _close(expected[key], actual[key], f"{path}.{key}", issues)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            issues.append({"severity": "ERROR", "code": "MOTION_MISMATCH",
                           "message": "array length differs", "context": {"path": path}})
            return
        for i, item in enumerate(expected):
            _close(item, actual[i], f"{path}[{i}]", issues)
        return
    if expected != actual:
        issues.append({"severity": "ERROR", "code": "MOTION_MISMATCH",
                       "message": "replay value differs", "context": {"path": path}})


def _expected_physical(state: DecisionState, plan: Mapping[str, Any], graph: Member1RoadGraph,
                       query_us: int) -> dict[str, Any]:
    timeline = _actions(state, plan, graph)
    epoch = _when(state.decision_epoch)
    orders = {o.order_id: {"order_id": o.order_id, "status": o.status,
              "owner_vehicle_id": o.assigned_vehicle_id, "picked_up_at": o.picked_up_at,
              "delivered_at": o.delivered_at, "demand_kg": o.demand_kg,
              "provenance": "PINNED_INITIAL" if o.status != "WAITING" else "REPLAYED_PLAN"}
              for o in state.orders}
    vehicles, sequenced_history = [], []
    for vehicle in state.vehicles:
        acts = timeline.get(vehicle.vehicle_id, [])
        complete = [a for a in acts if _done(a, query_us)]
        active = next((a for a in acts if _active(a, query_us)), None)
        metrics = {"distance_m": 0.0, "travel_time_us": 0.0, "waiting_time_us": 0.0,
                   "service_time_us": 0.0, "relative_exposure_proxy": 0.0, "cost_vnd": 0.0}
        node, incoming = vehicle.current_position_node_id, None
        for action in acts:
            fraction = 1.0 if _done(action, query_us) else ((query_us - action["start"]) /
                       (action["end"] - action["start"]) if _active(action, query_us) else 0.0)
            if action["kind"] == "EDGE":
                distance = action["distance"] * fraction
                metrics["distance_m"] += distance
                metrics["travel_time_us"] += (action["end"] - action["start"]) * fraction
                metrics["relative_exposure_proxy"] += action["exposure"] * fraction
                metrics["cost_vnd"] += distance / 1000 * vehicle.cost_per_km_vnd
            elif action["kind"] == "WAIT":
                metrics["waiting_time_us"] += (action["end"] - action["start"]) * fraction
            elif action["kind"] == "SERVICE":
                metrics["service_time_us"] += (action["end"] - action["start"]) * fraction
        for action in complete:
            if action["kind"] == "PICKUP" and orders[action["order"]]["status"] == "WAITING":
                orders[action["order"]].update(status="ONBOARD", owner_vehicle_id=vehicle.vehicle_id,
                                                picked_up_at=_iso(epoch, action["start"]))
            elif action["kind"] == "SERVICE":
                orders[action["order"]].update(status="DELIVERED", owner_vehicle_id=vehicle.vehicle_id,
                                                delivered_at=_iso(epoch, action["end"]))
            if action["kind"] == "EDGE":
                node, incoming = action["to"], action["edge"]
        local_history: list[dict[str, Any]] = []
        for action in acts:
            if action["kind"] == "PICKUP" and _done(action, query_us):
                local_history.append({"action_id": action["id"], "kind": "PICKUP", "at_us": action["start"],
                                "vehicle_id": vehicle.vehicle_id, "order_id": action["order"]})
            elif action["kind"] == "EDGE":
                if query_us > action["start"]:
                    local_history.append({"action_id": action["id"] + "-enter", "kind": "EDGE_ENTER",
                                    "at_us": action["start"], "vehicle_id": vehicle.vehicle_id,
                                    "edge_id": action["edge"]})
                if _done(action, query_us):
                    local_history.append({"action_id": action["id"] + "-exit", "kind": "EDGE_COMPLETE",
                                    "at_us": action["end"], "vehicle_id": vehicle.vehicle_id,
                                    "edge_id": action["edge"]})
            elif action["kind"] == "SERVICE":
                if query_us > action["start"]:
                    local_history.append({"action_id": action["id"] + "-start", "kind": "SERVICE_START",
                                    "at_us": action["start"], "vehicle_id": vehicle.vehicle_id,
                                    "order_id": action["order"]})
                if _done(action, query_us):
                    local_history.append({"action_id": action["id"] + "-complete", "kind": "DELIVERY_COMPLETE",
                                    "at_us": action["end"], "vehicle_id": vehicle.vehicle_id,
                                    "order_id": action["order"]})
        if active and active["kind"] == "EDGE":
            fraction = (query_us - active["start"]) / (active["end"] - active["start"])
            position = {"kind": "ON_EDGE", "edge_id": active["edge"], "from_node": active["from"],
                        "to_node": active["to"], "progress": fraction,
                        "coordinates": _point(active["geometry"], fraction),
                        "position_source": "SIMULATED", "accuracy_m": vehicle.position_accuracy_m,
                        "incoming_edge": active["incoming"], "edge_entry_us": active["start"],
                        "expected_exit_us": active["end"], "remaining_time_us": active["end"]-query_us,
                        "remaining_distance_m": active["distance"]*(1-fraction),
                        "remaining_exposure": active["exposure"]*(1-fraction)}
            activity, next_node, reroute = "MOVING", active["to"], active["end"]
            commitment = {"kind": "EDGE", "edge_id": active["edge"], "to_node": active["to"],
                          "until_us": active["end"]}
        else:
            coords = graph.node(node)
            position = {"kind": "AT_NODE", "node_id": node, "coordinates": list(coords),
                        "position_source": "SIMULATED", "accuracy_m": vehicle.position_accuracy_m,
                        "incoming_edge": incoming}
            if active and active["kind"] == "SERVICE":
                activity, next_node, reroute = "SERVICING", node, active["end"]
                commitment = {"kind": "SERVICE", "order_id": active["order"], "node_id": node,
                              "until_us": active["end"]}
            elif active and active["kind"] == "WAIT":
                activity, next_node, reroute, commitment = "WAITING_AT_STOP", node, query_us, None
            else:
                returned = bool(acts) and query_us >= max(a["end"] for a in acts)
                activity, next_node, reroute, commitment = (
                    "IDLE_AT_DEPOT" if (returned or not acts) and node == state.depot.graph_node_id
                    else "AT_NODE", node, query_us, None)
        onboard = sorted(oid for oid, order in orders.items()
                         if order["status"] == "ONBOARD" and order["owner_vehicle_id"] == vehicle.vehicle_id)
        service_ids = [a["order"] for a in acts if a["kind"] == "SERVICE"]
        next_action = next((a for a in acts if not _done(a, query_us) and not _active(a, query_us)
                            and a["start"] >= query_us), None)
        remaining_range = vehicle.remaining_range_m - metrics["distance_m"]
        if remaining_range < -1e-7:
            raise MotionContractError("RANGE", f"vehicles.{vehicle.vehicle_id}.remaining_range_m",
                                      "executed prefix exceeds remaining range")
        vehicles.append({"vehicle_id": vehicle.vehicle_id, "availability": vehicle.availability,
                         "capacity_kg": vehicle.capacity_kg,
                         "current_load_kg": sum(orders[o]["demand_kg"] for o in onboard),
                         "onboard_order_ids": onboard,
                         "remaining_range_m": max(0.0, remaining_range),
                         "position_timestamp": _iso(epoch, query_us), "position": position,
                         "activity": activity, "execution_cursor": sum(_done(a, query_us) for a in acts),
                         "next_action": None if next_action is None else {
                             "kind": next_action["kind"], "start_us": next_action["start"],
                             "order_id": next_action.get("order"), "edge_id": next_action.get("edge")},
                         "next_controllable_node": int(next_node), "reroute_available_at_us": int(reroute),
                         "completed_stops": [o for o in service_ids if orders[o]["status"] == "DELIVERED"],
                         "active_commitment": commitment,
                         "explicit_committed_stop_id": vehicle.committed_stop_id,
                         "planned_suffix": [o for o in service_ids if orders[o]["status"] != "DELIVERED"],
                         "executed_metrics": metrics})
        zero_by_id = {action["id"]: action["start"] == action["end"] for action in acts}
        for ordinal, entry in enumerate(local_history):
            base_id = entry["action_id"].removesuffix("-exit").removesuffix("-complete")
            completed_prior = (entry["kind"] in {"EDGE_COMPLETE", "DELIVERY_COMPLETE"}
                               and not zero_by_id.get(base_id, False))
            sequenced_history.append((entry["at_us"], 0 if completed_prior else 1,
                                      vehicle.vehicle_id, ordinal, entry))
    history = [entry for _, _, _, _, entry in sorted(sequenced_history, key=lambda x: x[:4])]
    aggregate = {key: sum(v["executed_metrics"][key] for v in vehicles)
                 for key in ("distance_m", "travel_time_us", "waiting_time_us", "service_time_us",
                             "relative_exposure_proxy", "cost_vnd")}
    return {"orders": list(orders.values()), "vehicles": vehicles, "execution_history": history,
            "executed_prefix_sha256": hashlib.sha256(canonical_json(history)).hexdigest(),
            "execution_metrics": aggregate}


def validate_motion_state(state: DecisionState, plan: Mapping[str, Any], acceptance: Mapping[str, Any],
                          graph: Member1RoadGraph, candidate: Mapping[str, Any], *,
                          trusted_parent: MotionState | None = None,
                          trusted_parent_chain: Sequence[MotionState] = ()) -> dict[str, Any]:
    """Certify physical state *and* source/frame/lineage against trusted inputs."""
    issues: list[dict[str, Any]] = []

    def issue(code: str, path: str, message: str) -> None:
        issues.append({"severity": "ERROR", "code": code, "message": message,
                       "context": {"path": path}})

    try:
        MotionState.from_dict(candidate)
        if not isinstance(plan, Mapping):
            raise MotionContractError("INVALID_DATA", "plan", "plan object required")
        if not isinstance(acceptance, Mapping):
            raise MotionContractError("INVALID_DATA", "acceptance", "trusted acceptance required")
        acceptance_material = dict(acceptance)
        acceptance_material.pop("record_created_at", None)
        acceptance_material["acceptance_sha256"] = None
        expected_acceptance_sha = hashlib.sha256(canonical_json(acceptance_material)).hexdigest()
        acceptance_checks = {
            "acceptance_sha256": expected_acceptance_sha,
            "initial_state_sha256": hashlib.sha256(canonical_json(state.to_dict())).hexdigest(),
            "initial_state_schema_version": state.schema_version,
            "plan_canonical_sha256": hashlib.sha256(canonical_json(plan)).hexdigest(),
            "solution_id": plan.get("solution_id"), "scenario_id": state.scenario_id,
            "fixture_raw_sha256": state.fixture_raw_sha256, "receipt_sha256": state.receipt_sha256,
            "routing_version": state.routing_version, "features_version": state.features_version,
            "context_version": state.context_version,
            "leader_policy_id": "task02-m1-event-policy/1",
            "leader_policy_sha256": "70ee2b3977578cc7f69a561e92b2f92a836977f1d30afaa496ad9eefdd14aed6",
            "execution_start": state.decision_epoch,
        }
        for key, expected_value in acceptance_checks.items():
            if acceptance.get(key) != expected_value:
                issue("ACCEPTANCE_BINDING", f"acceptance.{key}",
                      "trusted acceptance differs from source/plan/policy")

        epoch, target = _when(state.decision_epoch), _when(candidate["current_time"])
        query_us = _offset(epoch, candidate["current_time"])
        if query_us < 0:
            issue("TIME_REWIND", "current_time", "candidate precedes execution epoch")
        if candidate.get("decision_epoch") != candidate["current_time"]:
            issue("FRAME_BINDING", "decision_epoch", "decision epoch differs from query")
        for key in ("plan_origin_epoch", "cost_epoch"):
            if candidate.get(key) != state.decision_epoch:
                issue("FRAME_BINDING", key, "epoch differs from trusted plan origin")
        frame = {
            "replay_policy_version": "task02-m1-motion-replay-policy/2",
            "leader_policy_id": "task02-m1-event-policy/1",
            "timeline_version": "task02-m1-edge-timeline-us-ceiling/1",
            "geometry_interpolation_version": "task02-m1-directed-polyline-haversine/1",
            "mode": "ROLLING_REPLAY_SYNTHETIC", "snapshot_boundary": "BEFORE_NEW_ACTIONS",
            "unit_metadata": MOTION_UNIT_METADATA,
            "limits": {"event_application": False, "dynamic_solver": False,
                       "travel_features_frozen_at_plan_origin": True,
                       "pickup_service_seconds": 0.0,
                       "duration_quantization": "CEILING_TO_INTEGER_MICROSECOND",
                       "duration_quantization_error_bound_us_per_duration": 1},
        }
        for key, expected_value in frame.items():
            _close(expected_value, candidate.get(key), key, issues)
        root = {
            "scenario_id": state.scenario_id, "state_schema_version": state.schema_version,
            "state_version": state.state_version,
            "initial_state_sha256": acceptance.get("initial_state_sha256"),
            "fixture_raw_sha256": state.fixture_raw_sha256,
            "receipt_sha256": state.receipt_sha256,
        }
        _close(root, candidate.get("root_initial_state"), "root_initial_state", issues)
        _close({"routing": state.routing_version, "features": state.features_version,
                "context": state.context_version, "delivery_area": state.delivery_area_version},
               candidate.get("source_versions"), "source_versions", issues)
        accepted_projection = {
            "acceptance_id": acceptance.get("acceptance_id"),
            "acceptance_sha256": acceptance.get("acceptance_sha256"),
            "acceptance_type": acceptance.get("acceptance_type"),
            "accepted_at": acceptance.get("accepted_at"),
            "execution_start": acceptance.get("execution_start"),
            "run_id": acceptance.get("run_id"), "solution_id": plan.get("solution_id"),
            "selected_profile": acceptance.get("selected_profile"),
            "plan_sha256": acceptance.get("plan_sha256"),
            "manifest_sha256": acceptance.get("manifest_sha256"),
            "validation_sha256": acceptance.get("validation_sha256"),
        }
        _close(accepted_projection, candidate.get("accepted_plan"), "accepted_plan", issues)

        for index, event in enumerate(state.pending_events):
            if target > _when(event.timestamp):
                issue("EVENT_TRANSITION_REQUIRED", f"pending_events[{index}]",
                      "unapplied event barrier was crossed")
        _close([event.to_dict() for event in state.pending_events],
               candidate.get("pending_events"), "pending_events", issues)
        if candidate.get("applied_event_ids") != []:
            issue("EVENT_APPLIED", "applied_event_ids", "Step 4 applies no event")

        no_op = (isinstance(trusted_parent, MotionState)
                 and target == _when(trusted_parent.current_time)
                 and candidate == trusted_parent.to_dict())
        if no_op:
            ancestor = trusted_parent_chain[-1] if trusted_parent_chain else None
            parent_check = validate_motion_state(state, plan, acceptance, graph, trusted_parent.to_dict(),
                                                 trusted_parent=ancestor,
                                                 trusted_parent_chain=trusted_parent_chain[:-1])
            if not parent_check["valid"]:
                issue("PARENT_INVALID", "parent_state", "trusted no-op parent failed bound validation")
        elif candidate.get("parent_state") is None:
            if trusted_parent is not None:
                issue("LINEAGE_BINDING", "parent_state", "trusted parent supplied for root replay")
            if candidate.get("state_version") != state.state_version + 1:
                issue("LINEAGE_BINDING", "state_version", "root replay version differs")
            _close({"root_replay": True, "transition_count": 1},
                   candidate.get("lineage"), "lineage", issues)
        elif trusted_parent is None:
            issue("TRUSTED_PARENT_REQUIRED", "parent_state", "full child validation needs a trusted parent")
        elif not isinstance(trusted_parent, MotionState):
            issue("TRUSTED_PARENT_REQUIRED", "parent_state", "validated MotionState required")
        else:
            ancestor = trusted_parent_chain[-1] if trusted_parent_chain else None
            parent_check = validate_motion_state(state, plan, acceptance, graph, trusted_parent.to_dict(),
                                                 trusted_parent=ancestor,
                                                 trusted_parent_chain=trusted_parent_chain[:-1])
            if not parent_check["valid"]:
                issue("PARENT_INVALID", "parent_state", "trusted parent failed bound validation")
            parent_raw = trusted_parent.to_dict()
            _close({"state_id": trusted_parent.state_id,
                    "content_sha256": trusted_parent.content_sha256,
                    "state_version": trusted_parent.state_version,
                    "current_time": trusted_parent.current_time},
                   candidate.get("parent_state"), "parent_state", issues)
            if candidate.get("state_version") != trusted_parent.state_version + 1:
                issue("LINEAGE_BINDING", "state_version", "child version must increment")
            if target <= _when(trusted_parent.current_time):
                issue("LINEAGE_BINDING", "current_time", "child must advance trusted parent")
            parent_history = parent_raw["execution_history"]
            if candidate.get("execution_history", [])[:len(parent_history)] != parent_history:
                issue("LINEAGE_BINDING", "execution_history",
                      "child history must extend the trusted executed prefix")
            _close({"root_replay": False,
                    "transition_count": parent_raw["lineage"]["transition_count"] + 1},
                   candidate.get("lineage"), "lineage", issues)

        if query_us >= 0:
            expected = _expected_physical(state, plan, graph, query_us)
            for key, value in expected.items():
                _close(value, candidate.get(key), key, issues)
    except (MotionContractError, ValueError, KeyError, OverflowError) as error:
        path = error.path if isinstance(error, MotionContractError) else "$"
        code = error.code if isinstance(error, MotionContractError) else "MOTION_RECONSTRUCTION"
        issue(code, path, str(error))
    return {"status": "PASSED" if not issues else "FAILED", "valid": not issues,
            "validator_version": MOTION_VALIDATOR_VERSION,
            "scope": "bound source/frame/lineage and independent raw-edge physical feasibility",
            "diagnostics": issues}


__all__ = ["MOTION_VALIDATOR_VERSION", "validate_motion_state"]
