"""Deterministic, source-edge execution replay for accepted M1 plans.

No solver or path search is called here.  Assignment, sequence and physical
paths come from a separately authenticated and independently validated plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from optimization.models.decision_state import DecisionState
from optimization.models.motion_state import (
    MOTION_SCHEMA_VERSION, MOTION_UNIT_METADATA, MotionContractError, MotionState, canonical_json,
    finalize_motion_payload,
)
from optimization.integration.member1_s0_graph import Member1RoadGraph, RoadDataError


REPLAY_POLICY_VERSION = "task02-m1-motion-replay-policy/2"
LEADER_POLICY_ID = "task02-m1-event-policy/1"
LEADER_POLICY_SHA256 = "70ee2b3977578cc7f69a561e92b2f92a836977f1d30afaa496ad9eefdd14aed6"
ACCEPTANCE_SCHEMA_VERSION = "task02-m1-synthetic-leader-acceptance/2"
TIMELINE_VERSION = "task02-m1-edge-timeline-us-ceiling/1"
GEOMETRY_INTERPOLATION_VERSION = "task02-m1-directed-polyline-haversine/1"
_I64_MAX = (1 << 63) - 1


class MotionReplayError(ValueError):
    def __init__(self, status: str, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.path = status, code, path

    def diagnostic(self) -> dict[str, Any]:
        return {"severity": "ERROR", "code": self.code, "path": self.path,
                "message": str(self)}


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    return _sha_bytes(canonical_json(value))


def _ceil_us(value: Any, path: str) -> int:
    if isinstance(value, bool) or type(value) not in (int, float, str, Decimal):
        raise MotionReplayError("INVALID_DATA", "TIME_VALUE", path,
                                "finite nonnegative duration required")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise MotionReplayError("INVALID_DATA", "TIME_VALUE", path,
                                "finite nonnegative duration required") from error
    if not number.is_finite() or number < 0:
        raise MotionReplayError("INVALID_DATA", "TIME_VALUE", path,
                                "finite nonnegative duration required")
    result = int((number * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))
    if result > _I64_MAX:
        raise MotionReplayError("INVALID_DATA", "TIME_OVERFLOW", path,
                                "microsecond duration exceeds int64")
    return result


def _plan_number(value: Any, path: str) -> float:
    if type(value) not in (int, float):
        raise MotionReplayError("INVALID_DATA", "PLAN_NUMBER", path, "finite number required")
    try:
        result = float(value)
    except OverflowError as error:
        raise MotionReplayError("INVALID_DATA", "PLAN_NUMBER", path, "finite number required") from error
    if not math.isfinite(result) or result < 0:
        raise MotionReplayError("INVALID_DATA", "PLAN_NUMBER", path, "finite nonnegative number required")
    return result


def _parse_time(value: Any, path: str) -> datetime:
    if (not isinstance(value, str) or re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+07:00", value) is None):
        raise MotionReplayError("INVALID_DATA", "TIME_FORMAT", path,
                                "ISO +07:00 timestamp with at most six fractional digits required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise MotionReplayError("INVALID_DATA", "TIME_FORMAT", path, "invalid timestamp") from error
    if parsed.utcoffset() != timedelta(hours=7):
        raise MotionReplayError("INVALID_DATA", "TIME_ZONE", path, "+07:00 required")
    return parsed


def _iso(epoch: datetime, offset_us: int) -> str:
    return (epoch + timedelta(microseconds=offset_us)).isoformat()


def _offset_us(epoch: datetime, value: str, path: str, *, signed: bool = False) -> int:
    delta = _parse_time(value, path) - epoch
    microseconds = ((delta.days * 86_400 + delta.seconds) * 1_000_000
                    + delta.microseconds)
    if microseconds > _I64_MAX or (microseconds < 0 and not signed) or microseconds < -_I64_MAX:
        raise MotionReplayError("INVALID_DATA", "TIME_RANGE", path,
                                "timestamp precedes execution epoch or exceeds int64")
    return microseconds


def _haversine(left: Sequence[float], right: Sequence[float]) -> float:
    lon1, lat1 = map(math.radians, left)
    lon2, lat2 = map(math.radians, right)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_008.8 * 2 * math.asin(min(1.0, math.sqrt(value)))


def interpolate_directed_geometry(points: Sequence[Sequence[float]], fraction: float) -> list[float]:
    if not isinstance(points, Sequence) or len(points) < 2:
        raise MotionReplayError("INVALID_DATA", "EDGE_GEOMETRY", "geometry",
                                "directed LineString needs at least two points")
    checked: list[list[float]] = []
    for index, item in enumerate(points):
        if (not isinstance(item, Sequence) or len(item) != 2
                or any(type(value) not in (int, float) or not math.isfinite(float(value))
                       for value in item)):
            raise MotionReplayError("INVALID_DATA", "EDGE_GEOMETRY",
                                    f"geometry[{index}]", "finite [lon,lat] required")
        checked.append([float(item[0]), float(item[1])])
    if not 0.0 <= fraction <= 1.0:
        raise MotionReplayError("INVALID_DATA", "EDGE_PROGRESS", "position.progress",
                                "progress must be in [0,1]")
    lengths = [_haversine(checked[index], checked[index + 1])
               for index in range(len(checked) - 1)]
    total = sum(lengths)
    if not math.isfinite(total) or total <= 0:
        raise MotionReplayError("INVALID_DATA", "EDGE_GEOMETRY", "geometry",
                                "degenerate geometry cannot be interpolated")
    target = fraction * total
    cursor = 0.0
    for index, length in enumerate(lengths):
        if target <= cursor + length or index == len(lengths) - 1:
            local = 0.0 if length == 0 else (target - cursor) / length
            local = min(1.0, max(0.0, local))
            return [checked[index][axis] + local * (checked[index + 1][axis] - checked[index][axis])
                    for axis in (0, 1)]
        cursor += length
    return checked[-1]


@dataclass(frozen=True, slots=True)
class TimelineAction:
    action_id: str
    kind: str
    vehicle_id: str
    start_us: int
    end_us: int
    node_id: int | None = None
    order_id: str | None = None
    edge_id: str | None = None
    from_node: int | None = None
    to_node: int | None = None
    incoming_edge: str | None = None
    distance_m: float = 0.0
    exposure: float = 0.0
    geometry: tuple[tuple[float, float], ...] = ()

    @property
    def zero_duration(self) -> bool:
        return self.start_us == self.end_us

    def completed_at(self, query_us: int) -> bool:
        return query_us > self.start_us if self.zero_duration else query_us >= self.end_us

    def active_at(self, query_us: int) -> bool:
        return not self.zero_duration and self.start_us < query_us < self.end_us


def _action_id(vehicle_id: str, kind: str, ordinal: int, identity: str) -> str:
    digest = hashlib.sha256(f"{vehicle_id}|{kind}|{ordinal}|{identity}".encode()).hexdigest()[:20]
    return f"act-{digest}"


def _edge_source(graph: Member1RoadGraph, edge_id: str, path: str) -> dict[str, Any]:
    raw = graph.edge(edge_id)
    if raw is None:
        raise MotionReplayError("INVALID_DATA", "EDGE_MISSING", path, f"edge absent: {edge_id}")
    try:
        checked = graph._checked_edge(raw)  # source validation only; no search
    except RoadDataError as error:
        raise MotionReplayError("INVALID_DATA", "EDGE_SOURCE_INVALID", path, str(error)) from error
    return checked


def build_route_timelines(state: DecisionState, plan: Mapping[str, Any],
                          graph: Member1RoadGraph) -> dict[str, list[TimelineAction]]:
    """Build exact edge/action timelines without invoking a path search."""

    if not isinstance(plan, Mapping):
        raise MotionReplayError("INVALID_DATA", "PLAN_WITNESS_REQUIRED", "plan", "plan object required")
    status = plan.get("status")
    if not isinstance(status, str) or status not in {"FEASIBLE", "PARTIAL"}:
        raise MotionReplayError("INVALID_DATA", "PLAN_WITNESS_REQUIRED", "plan.status",
                                "accepted plan must carry a feasible witness")
    if plan.get("scenario_id") != state.scenario_id:
        raise MotionReplayError("INVALID_DATA", "SCENARIO_MISMATCH", "plan.scenario_id",
                                "plan and initial state differ")
    routes = plan.get("vehicle_routes")
    if not isinstance(routes, list):
        raise MotionReplayError("INVALID_DATA", "PLAN_ROUTES", "plan.vehicle_routes", "array required")
    by_order = {item.order_id: item for item in state.orders}
    vehicle_ids = {item.vehicle_id for item in state.vehicles}
    epoch = _parse_time(state.decision_epoch, "decision_epoch")
    timelines: dict[str, list[TimelineAction]] = {}
    seen_orders: set[str] = set()
    for route_index, route in enumerate(routes):
        path = f"plan.vehicle_routes[{route_index}]"
        if not isinstance(route, Mapping):
            raise MotionReplayError("INVALID_DATA", "PLAN_ROUTE", path, "object required")
        vehicle_id = route.get("vehicle_id")
        if not isinstance(vehicle_id, str) or vehicle_id not in vehicle_ids or vehicle_id in timelines:
            raise MotionReplayError("INVALID_DATA", "PLAN_VEHICLE", f"{path}.vehicle_id",
                                    "unique known vehicle required")
        sequence = route.get("order_sequence")
        legs = route.get("legs")
        stops = route.get("stops")
        nodes = route.get("node_sequence")
        if not all(isinstance(item, list) for item in (sequence, legs, stops, nodes)):
            raise MotionReplayError("INVALID_DATA", "PLAN_ROUTE", path, "route arrays required")
        if len(legs) != len(sequence) + 1 or len(stops) != len(sequence) or len(nodes) != len(legs) + 1:
            raise MotionReplayError("INVALID_DATA", "PLAN_ROUTE", path, "route cardinality mismatch")
        for order_index, order_id in enumerate(sequence):
            if (not isinstance(order_id, str) or not order_id or order_id not in by_order
                    or order_id in seen_orders):
                raise MotionReplayError("INVALID_DATA", "PLAN_ORDER",
                                        f"{path}.order_sequence[{order_index}]",
                                        "unique known order ID required")
            seen_orders.add(order_id)
        cursor = _ceil_us(_plan_number(route.get("departure_s"), f"{path}.departure_s"),
                          f"{path}.departure_s")
        vehicle = next(item for item in state.vehicles if item.vehicle_id == vehicle_id)
        first_allowed = max(_offset_us(epoch, vehicle.working_start,
                                       f"vehicles.{vehicle_id}.working_start", signed=True),
                            _offset_us(epoch, state.depot.opening_time,
                                       "depot.opening_time", signed=True))
        last_allowed = min(_offset_us(epoch, vehicle.working_end,
                                      f"vehicles.{vehicle_id}.working_end", signed=True),
                           _offset_us(epoch, state.depot.closing_time,
                                      "depot.closing_time", signed=True))
        if vehicle.availability != "AVAILABLE" or vehicle.current_position_node_id != state.depot.graph_node_id:
            raise MotionReplayError("INVALID_DATA", "VEHICLE_START", f"{path}.vehicle_id",
                                    "route vehicle must be available at depot")
        if cursor < first_allowed or cursor > last_allowed:
            raise MotionReplayError("INVALID_DATA", "WORKING_WINDOW", f"{path}.departure_s",
                                    "depot departure outside working window")
        if nodes[0] != state.depot.graph_node_id or nodes[-1] != state.depot.graph_node_id:
            raise MotionReplayError("INVALID_DATA", "DEPOT_RETURN", f"{path}.node_sequence",
                                    "route must begin and end at depot")
        pickup_load = vehicle.current_load_kg + sum(
            by_order[oid].demand_kg for oid in sequence if by_order[oid].status == "WAITING")
        if pickup_load > vehicle.capacity_kg + 1e-9:
            raise MotionReplayError("INVALID_DATA", "CAPACITY", f"{path}.order_sequence",
                                    "depot pickup exceeds capacity")
        actions: list[TimelineAction] = []
        ordinal = 0
        for order_id in sequence:
            order = by_order[order_id]
            if order.status == "WAITING":
                actions.append(TimelineAction(
                    _action_id(vehicle_id, "PICKUP", ordinal, order_id), "PICKUP",
                    vehicle_id, cursor, cursor, node_id=state.depot.graph_node_id,
                    order_id=order_id,
                ))
                ordinal += 1
            elif order.status == "ONBOARD" and order.assigned_vehicle_id != vehicle_id:
                raise MotionReplayError("INVALID_DATA", "CUSTODY_OWNER", f"{path}.order_sequence",
                                        "ONBOARD order assigned to another vehicle")
        incoming: str | None = None
        for leg_index, leg in enumerate(legs):
            leg_path = f"{path}.legs[{leg_index}]"
            if not isinstance(leg, Mapping) or not isinstance(leg.get("edge_ids"), list):
                raise MotionReplayError("INVALID_DATA", "PLAN_LEG", leg_path, "leg/edge IDs required")
            if leg.get("from_node") != nodes[leg_index] or leg.get("to_node") != nodes[leg_index + 1]:
                raise MotionReplayError("INVALID_DATA", "PLAN_LEG", leg_path, "node sequence differs")
            if leg.get("incoming_edge") != incoming:
                raise MotionReplayError("INVALID_DATA", "TURN_STATE", f"{leg_path}.incoming_edge",
                                        "incoming edge was not preserved")
            leg_start = cursor
            local_node = nodes[leg_index]
            for edge_index, edge_id in enumerate(leg["edge_ids"]):
                edge_path = f"{leg_path}.edge_ids[{edge_index}]"
                if not isinstance(edge_id, str) or not edge_id:
                    raise MotionReplayError("INVALID_DATA", "EDGE_ID", edge_path, "edge ID required")
                edge = _edge_source(graph, edge_id, edge_path)
                if edge["fromNodeId"] != local_node:
                    raise MotionReplayError("INVALID_DATA", "EDGE_CONTINUITY", edge_path,
                                            "directed edge is discontinuous")
                if (incoming, edge_id) in graph.forbidden:
                    raise MotionReplayError("INVALID_DATA", "FORBIDDEN_TURN", edge_path,
                                            "forbidden turn in accepted plan")
                duration = _ceil_us(Decimal(str(edge["travelTimeHours"])) * Decimal(3600), edge_path)
                if duration <= 0:
                    raise MotionReplayError("INVALID_DATA", "EDGE_TIME", edge_path,
                                            "road edge needs positive travel time")
                geometry = tuple((point.longitude, point.latitude) for point in edge["points"])
                actions.append(TimelineAction(
                    _action_id(vehicle_id, "EDGE", ordinal, edge_id), "EDGE", vehicle_id,
                    cursor, cursor + duration, edge_id=edge_id,
                    from_node=edge["fromNodeId"], to_node=edge["toNodeId"],
                    incoming_edge=incoming, distance_m=float(edge["lengthKm"]) * 1000.0,
                    exposure=float(edge["relativeExposure"]), geometry=geometry,
                ))
                ordinal += 1
                cursor += duration
                local_node = edge["toNodeId"]
                incoming = edge_id
            if local_node != nodes[leg_index + 1] or leg.get("final_edge") != incoming:
                raise MotionReplayError("INVALID_DATA", "PLAN_LEG", leg_path,
                                        "leg endpoint/final edge differs from raw path")
            reported = _plan_number(leg.get("travel_time_s"), f"{leg_path}.travel_time_s")
            tolerance = (len(leg["edge_ids"]) + 2) / 1_000_000.0
            if not math.isfinite(reported) or abs((cursor - leg_start) / 1_000_000.0 - reported) > tolerance:
                raise MotionReplayError("INVALID_DATA", "PLAN_SCHEDULE", f"{leg_path}.travel_time_s",
                                        "leg travel differs from raw edge durations")
            if leg_index < len(stops):
                stop = stops[leg_index]
                order_id = sequence[leg_index]
                order = by_order[order_id]
                if (not isinstance(stop, Mapping) or stop.get("order_id") != order_id
                        or stop.get("node_id") != nodes[leg_index + 1]):
                    raise MotionReplayError("INVALID_DATA", "PLAN_STOP", f"{path}.stops[{leg_index}]",
                                            "stop identity differs")
                arrival = cursor
                earliest = _offset_us(epoch, order.earliest, f"orders.{order_id}.earliest",
                                      signed=True)
                service_start = max(cursor, earliest)
                if service_start > cursor:
                    actions.append(TimelineAction(
                        _action_id(vehicle_id, "WAIT", ordinal, order_id), "WAIT", vehicle_id,
                        cursor, service_start, node_id=order.graph_node_id, order_id=order_id,
                        incoming_edge=incoming,
                    ))
                    ordinal += 1
                service_duration = _ceil_us(order.service_time_seconds,
                                            f"orders.{order_id}.service_time_seconds")
                actions.append(TimelineAction(
                    _action_id(vehicle_id, "SERVICE", ordinal, order_id), "SERVICE", vehicle_id,
                    service_start, service_start + service_duration,
                    node_id=order.graph_node_id, order_id=order_id, incoming_edge=incoming,
                ))
                ordinal += 1
                cursor = service_start + service_duration
                deadline = _offset_us(epoch, order.hard_deadline,
                                      f"orders.{order_id}.hard_deadline", signed=True)
                if cursor > deadline:
                    raise MotionReplayError("INVALID_DATA", "HARD_DEADLINE",
                                            f"orders.{order_id}.hard_deadline",
                                            "quantized service completion exceeds hard deadline")
                stop_tolerance = (sum(len(item.get("edge_ids", [])) for item in legs[:leg_index + 1])
                                  + leg_index + 4) / 1_000_000.0
                comparisons = {
                    "arrival_s": arrival / 1e6,
                    "waiting_s": (service_start - arrival) / 1e6,
                    "service_start_s": service_start / 1e6,
                    "completion_s": cursor / 1e6,
                }
                for field, expected in comparisons.items():
                    actual = stop.get(field)
                    if abs(_plan_number(actual, f"{path}.stops[{leg_index}].{field}") - expected) > stop_tolerance:
                        raise MotionReplayError("INVALID_DATA", "PLAN_SCHEDULE",
                                                f"{path}.stops[{leg_index}].{field}",
                                                "stop schedule differs from raw timeline")
        reported_return = _plan_number(route.get("return_s"), f"{path}.return_s")
        if abs(reported_return - cursor / 1e6) > (
                sum(len(item.get("edge_ids", [])) for item in legs) + len(stops) + 4) / 1e6:
            raise MotionReplayError("INVALID_DATA", "PLAN_SCHEDULE", f"{path}.return_s",
                                    "return time differs from raw timeline")
        distance = sum(item.distance_m for item in actions if item.kind == "EDGE")
        if distance > vehicle.remaining_range_m + 1e-7:
            raise MotionReplayError("INVALID_DATA", "RANGE", f"{path}.legs",
                                    "full return route exceeds remaining range")
        if cursor > last_allowed:
            raise MotionReplayError("INVALID_DATA", "WORKING_WINDOW", f"{path}.return_s",
                                    "return exceeds depot or vehicle closing time")
        timelines[vehicle_id] = actions
    return timelines


def _history(actions: Sequence[TimelineAction], query_us: int) -> list[dict[str, Any]]:
    records: list[tuple[int, int, dict[str, Any]]] = []
    serial = 0
    for action in actions:
        if action.kind == "PICKUP" and action.completed_at(query_us):
            records.append((action.start_us, serial, {"action_id": action.action_id,
                            "kind": "PICKUP", "at_us": action.start_us,
                            "vehicle_id": action.vehicle_id, "order_id": action.order_id}))
            serial += 1
        elif action.kind == "EDGE":
            if query_us > action.start_us:
                records.append((action.start_us, serial, {"action_id": action.action_id + "-enter",
                                "kind": "EDGE_ENTER", "at_us": action.start_us,
                                "vehicle_id": action.vehicle_id, "edge_id": action.edge_id}))
                serial += 1
            if action.completed_at(query_us):
                records.append((action.end_us, serial, {"action_id": action.action_id + "-exit",
                                "kind": "EDGE_COMPLETE", "at_us": action.end_us,
                                "vehicle_id": action.vehicle_id, "edge_id": action.edge_id}))
                serial += 1
        elif action.kind == "SERVICE":
            if query_us > action.start_us:
                records.append((action.start_us, serial, {"action_id": action.action_id + "-start",
                                "kind": "SERVICE_START", "at_us": action.start_us,
                                "vehicle_id": action.vehicle_id, "order_id": action.order_id}))
                serial += 1
            if action.completed_at(query_us):
                records.append((action.end_us, serial, {"action_id": action.action_id + "-complete",
                                "kind": "DELIVERY_COMPLETE", "at_us": action.end_us,
                                "vehicle_id": action.vehicle_id, "order_id": action.order_id}))
                serial += 1
    records.sort(key=lambda item: (item[0], item[1], item[2]["action_id"]))
    return [item[2] for item in records]


def _vehicle_snapshot(vehicle: Any, route_actions: Sequence[TimelineAction],
                      query_us: int, state: DecisionState, orders: dict[str, dict[str, Any]],
                      graph: Member1RoadGraph) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    actions = list(route_actions)
    history = _history(actions, query_us)
    active = next((item for item in actions if item.active_at(query_us)), None)
    completed = [item for item in actions if item.completed_at(query_us)]
    entered = [item for item in actions if query_us > item.start_us]
    metrics = {"distance_m": 0.0, "travel_time_us": 0.0, "waiting_time_us": 0.0,
               "service_time_us": 0.0, "relative_exposure_proxy": 0.0, "cost_vnd": 0.0}
    last_edge: TimelineAction | None = None
    for action in actions:
        fraction = 1.0 if action.completed_at(query_us) else (
            (query_us - action.start_us) / (action.end_us - action.start_us)
            if action.active_at(query_us) else 0.0)
        fraction = min(1.0, max(0.0, fraction))
        if action.kind == "EDGE":
            if fraction:
                last_edge = action
            distance = action.distance_m * fraction
            metrics["distance_m"] += distance
            metrics["travel_time_us"] += (action.end_us - action.start_us) * fraction
            metrics["relative_exposure_proxy"] += action.exposure * fraction
            metrics["cost_vnd"] += distance / 1000.0 * vehicle.cost_per_km_vnd
        elif action.kind == "WAIT":
            metrics["waiting_time_us"] += (action.end_us - action.start_us) * fraction
        elif action.kind == "SERVICE":
            metrics["service_time_us"] += (action.end_us - action.start_us) * fraction

    node = vehicle.current_position_node_id
    incoming = None
    for action in completed:
        if action.kind == "EDGE":
            node, incoming = int(action.to_node), action.edge_id
    if active and active.kind == "EDGE":
        progress = (query_us - active.start_us) / (active.end_us - active.start_us)
        position = {"kind": "ON_EDGE", "edge_id": active.edge_id,
                    "from_node": active.from_node, "to_node": active.to_node,
                    "progress": progress,
                    "coordinates": interpolate_directed_geometry(active.geometry, progress),
                    "position_source": "SIMULATED", "accuracy_m": vehicle.position_accuracy_m,
                    "incoming_edge": active.incoming_edge,
                    "edge_entry_us": active.start_us, "expected_exit_us": active.end_us,
                    "remaining_time_us": active.end_us - query_us,
                    "remaining_distance_m": active.distance_m * (1.0 - progress),
                    "remaining_exposure": active.exposure * (1.0 - progress)}
        activity = "MOVING"
        next_node, reroute = active.to_node, active.end_us
        commitment = {"kind": "EDGE", "edge_id": active.edge_id,
                      "to_node": active.to_node, "until_us": active.end_us}
    else:
        coordinates = graph.node(node)
        if coordinates is None:
            raise MotionReplayError("INVALID_DATA", "NODE_MISSING", "vehicles.position",
                                    f"node absent: {node}")
        position = {"kind": "AT_NODE", "node_id": node,
                    "coordinates": list(coordinates), "position_source": "SIMULATED",
                    "accuracy_m": vehicle.position_accuracy_m, "incoming_edge": incoming}
        if active and active.kind == "SERVICE":
            activity, next_node, reroute = "SERVICING", node, active.end_us
            commitment = {"kind": "SERVICE", "order_id": active.order_id,
                          "node_id": node, "until_us": active.end_us}
        elif active and active.kind == "WAIT":
            activity, next_node, reroute = "WAITING_AT_STOP", node, query_us
            commitment = None
        else:
            last_end = max((item.end_us for item in actions), default=0)
            returned = bool(actions) and query_us >= last_end
            activity = ("IDLE_AT_DEPOT" if (returned or not actions)
                        and node == state.depot.graph_node_id else "AT_NODE")
            next_node, reroute, commitment = node, query_us, None

    pickup_completed = {item.order_id for item in completed if item.kind == "PICKUP"}
    delivered = {item.order_id for item in completed if item.kind == "SERVICE"}
    route_order_ids = [item.order_id for item in actions if item.kind == "PICKUP"]
    service_order_ids = [item.order_id for item in actions if item.kind == "SERVICE"]
    for order_id in pickup_completed:
        order = orders[order_id]
        if order["status"] == "WAITING":
            action = next(item for item in actions if item.kind == "PICKUP" and item.order_id == order_id)
            order.update({"status": "ONBOARD", "owner_vehicle_id": vehicle.vehicle_id,
                          "picked_up_at": _iso(_parse_time(state.decision_epoch, "decision_epoch"),
                                                action.start_us)})
    for order_id in delivered:
        order = orders[order_id]
        action = next(item for item in actions if item.kind == "SERVICE" and item.order_id == order_id)
        order.update({"status": "DELIVERED", "owner_vehicle_id": vehicle.vehicle_id,
                      "delivered_at": _iso(_parse_time(state.decision_epoch, "decision_epoch"),
                                           action.end_us)})
    onboard = sorted(item["order_id"] for item in orders.values()
                     if item["status"] == "ONBOARD" and item["owner_vehicle_id"] == vehicle.vehicle_id)
    load = sum(float(orders[order_id]["demand_kg"]) for order_id in onboard)
    completed_stops = [order_id for order_id in service_order_ids if order_id in delivered]
    suffix = [order_id for order_id in service_order_ids if order_id not in delivered]
    next_action = next((item for item in actions
                        if not item.completed_at(query_us) and not item.active_at(query_us)
                        and item.start_us >= query_us), None)
    cursor = sum(1 for item in actions if item.completed_at(query_us))
    remaining_range = vehicle.remaining_range_m - metrics["distance_m"]
    if remaining_range < -1e-7:
        raise MotionReplayError("INVALID_DATA", "RANGE", f"vehicles.{vehicle.vehicle_id}.remaining_range_m",
                                "executed prefix exceeds remaining range")
    return {
        "vehicle_id": vehicle.vehicle_id, "availability": vehicle.availability,
        "capacity_kg": vehicle.capacity_kg, "current_load_kg": load,
        "onboard_order_ids": onboard,
        "remaining_range_m": max(0.0, remaining_range),
        "position_timestamp": _iso(_parse_time(state.decision_epoch, "decision_epoch"), query_us),
        "position": position, "activity": activity, "execution_cursor": cursor,
        "next_action": (None if next_action is None else {
            "kind": next_action.kind, "start_us": next_action.start_us,
            "order_id": next_action.order_id, "edge_id": next_action.edge_id}),
        "next_controllable_node": int(next_node), "reroute_available_at_us": int(reroute),
        "completed_stops": completed_stops, "active_commitment": commitment,
        "explicit_committed_stop_id": vehicle.committed_stop_id,
        "planned_suffix": suffix, "executed_metrics": metrics,
    }, history


def _acceptance_digest(acceptance: Mapping[str, Any]) -> str:
    material = dict(acceptance)
    # Audit creation time is not part of the execution identity. Otherwise two
    # genuine revalidations of the same frozen plan produce different motion IDs.
    material.pop("record_created_at", None)
    material["acceptance_sha256"] = None
    return canonical_sha256(material)


def validate_acceptance_binding(state: DecisionState, plan: Mapping[str, Any],
                                acceptance: Mapping[str, Any]) -> None:
    if not isinstance(plan, Mapping):
        raise MotionReplayError("INVALID_DATA", "PLAN_WITNESS_REQUIRED", "plan", "plan object required")
    if not isinstance(acceptance, Mapping) or acceptance.get("schema_version") != ACCEPTANCE_SCHEMA_VERSION:
        raise MotionReplayError("INVALID_DATA", "ACCEPTANCE_VERSION", "acceptance.schema_version",
                                "trusted synthetic acceptance /1 required")
    try:
        expected_digest = _acceptance_digest(acceptance)
        plan_digest = canonical_sha256(plan)
    except MotionContractError as error:
        raise MotionReplayError("INVALID_DATA", error.code, error.path, str(error)) from error
    if acceptance.get("acceptance_sha256") != expected_digest:
        raise MotionReplayError("INVALID_DATA", "ACCEPTANCE_DIGEST", "acceptance.acceptance_sha256",
                                "acceptance digest mismatch")
    state_digest = canonical_sha256(state.to_dict())
    checks = {
        "scenario_id": state.scenario_id,
        "initial_state_sha256": state_digest,
        "initial_state_schema_version": state.schema_version,
        "solution_id": plan.get("solution_id"),
        "plan_canonical_sha256": plan_digest,
        "fixture_raw_sha256": state.fixture_raw_sha256,
        "receipt_sha256": state.receipt_sha256,
        "routing_version": state.routing_version,
        "features_version": state.features_version,
        "context_version": state.context_version,
    }
    for key, expected in checks.items():
        actual = acceptance.get(key)
        if actual != expected:
            raise MotionReplayError("INVALID_DATA", "ACCEPTANCE_BINDING", f"acceptance.{key}",
                                    "accepted plan/state/source binding differs")
    if not isinstance(acceptance.get("run_id"), str) or not acceptance["run_id"]:
        raise MotionReplayError("INVALID_DATA", "ACCEPTANCE_BINDING", "acceptance.run_id",
                                "trusted run ID is required")
    if (acceptance.get("leader_policy_id") != LEADER_POLICY_ID
            or acceptance.get("leader_policy_sha256") != LEADER_POLICY_SHA256):
        raise MotionReplayError("INVALID_DATA", "POLICY_BINDING", "acceptance.leader_policy_sha256",
                                "Leader event policy differs")
    if plan.get("scenario_id") != state.scenario_id:
        raise MotionReplayError("INVALID_DATA", "SCENARIO_MISMATCH", "plan.scenario_id",
                                "plan scenario differs")
    profile = plan.get("profile")
    selected = profile.get("name") if isinstance(profile, Mapping) else None
    if acceptance.get("selected_profile") != selected:
        raise MotionReplayError("INVALID_DATA", "PROFILE_BINDING", "acceptance.selected_profile",
                                "accepted profile differs")
    accepted_at = _parse_time(acceptance.get("accepted_at"), "acceptance.accepted_at")
    execution_start = _parse_time(acceptance.get("execution_start"), "acceptance.execution_start")
    if execution_start != _parse_time(state.decision_epoch, "decision_epoch") or accepted_at > execution_start:
        raise MotionReplayError("INVALID_DATA", "ACCEPTANCE_TIME", "acceptance.accepted_at",
                                "synthetic acceptance must precede execution start")


def replay_motion(state: DecisionState, plan: Mapping[str, Any], acceptance: Mapping[str, Any],
                  graph: Member1RoadGraph, target_time: str,
                  *, parent: MotionState | None = None,
                  parent_chain: Sequence[MotionState] = ()) -> dict[str, Any]:
    """Materialize a deterministic BEFORE_NEW_ACTIONS motion snapshot."""

    validate_acceptance_binding(state, plan, acceptance)
    epoch = _parse_time(state.decision_epoch, "decision_epoch")
    target = _parse_time(target_time, "target_time")
    delta = target - epoch
    query_us = ((delta.days * 86_400 + delta.seconds) * 1_000_000
                + delta.microseconds)
    if query_us < 0:
        raise MotionReplayError("INVALID_DATA", "TIME_REWIND", "target_time",
                                "target precedes root execution epoch")
    for index, event in enumerate(state.pending_events):
        if target > _parse_time(event.timestamp, f"pending_events[{index}].timestamp"):
            raise MotionReplayError("UNSUPPORTED", "EVENT_TRANSITION_REQUIRED",
                                    f"pending_events[{index}]",
                                    "replay cannot cross an unapplied event")
    if parent is not None:
        if not isinstance(parent, MotionState):
            raise MotionReplayError("INVALID_DATA", "PARENT_INVALID", "parent_state",
                                    "validated MotionState required")
        raw_parent = parent.to_dict()
        if (raw_parent["root_initial_state"]["initial_state_sha256"]
                != acceptance["initial_state_sha256"]
                or raw_parent["accepted_plan"]["acceptance_sha256"]
                != acceptance["acceptance_sha256"]):
            raise MotionReplayError("INVALID_DATA", "PARENT_BINDING", "parent_state",
                                    "parent belongs to another root or accepted plan")
        parent_time = _parse_time(parent.current_time, "parent_state.current_time")
        if target < parent_time:
            raise MotionReplayError("INVALID_DATA", "TIME_REWIND", "target_time",
                                    "incremental replay cannot move backward")
        # Import inside the function to keep the independent validator free of
        # any dependency on this replay implementation.
        from .member1_motion_validation import validate_motion_state
        ancestor = parent_chain[-1] if parent_chain else None
        parent_check = validate_motion_state(state, plan, acceptance, graph, raw_parent,
                                             trusted_parent=ancestor,
                                             trusted_parent_chain=parent_chain[:-1])
        if parent_check.get("valid") is not True:
            raise MotionReplayError("INVALID_DATA", "PARENT_INVALID", "parent_state",
                                    "parent failed independent bound validation")
        if target == parent_time:
            return parent.to_dict()

    timelines = build_route_timelines(state, plan, graph)
    orders = {item.order_id: {
        "order_id": item.order_id, "status": item.status,
        "owner_vehicle_id": item.assigned_vehicle_id,
        "picked_up_at": item.picked_up_at, "delivered_at": item.delivered_at,
        "demand_kg": item.demand_kg,
        "provenance": "PINNED_INITIAL" if item.status != "WAITING" else "REPLAYED_PLAN",
    } for item in state.orders}
    vehicles = []
    sequenced_history: list[tuple[int, int, str, int, dict[str, Any]]] = []
    for vehicle in state.vehicles:
        snapshot, records = _vehicle_snapshot(
            vehicle, timelines.get(vehicle.vehicle_id, ()), query_us, state, orders, graph)
        vehicles.append(snapshot)
        zero_by_id = {action.action_id: action.zero_duration
                      for action in timelines.get(vehicle.vehicle_id, ())}
        for ordinal, record in enumerate(records):
            base_id = record["action_id"].removesuffix("-exit").removesuffix("-complete")
            completed_prior_action = (record["kind"] in {"EDGE_COMPLETE", "DELIVERY_COMPLETE"}
                                      and not zero_by_id.get(base_id, False))
            # At a boundary, already started completions precede starts at T.
            # This keeps each earlier snapshot's history an actual prefix.
            sequenced_history.append((record["at_us"], 0 if completed_prior_action else 1,
                                      vehicle.vehicle_id, ordinal, record))
    history = [record for _, _, _, _, record in sorted(sequenced_history,
                                                       key=lambda item: item[:4])]
    executed_prefix_sha = canonical_sha256(history)
    aggregate = {key: sum(float(item["executed_metrics"][key]) for item in vehicles)
                 for key in ("distance_m", "travel_time_us", "waiting_time_us",
                             "service_time_us", "relative_exposure_proxy", "cost_vnd")}
    pending = [event.to_dict() for event in state.pending_events]
    parent_ref = None if parent is None else {
        "state_id": parent.state_id, "content_sha256": parent.content_sha256,
        "state_version": parent.state_version, "current_time": parent.current_time,
    }
    accepted_plan = {
        "acceptance_id": acceptance["acceptance_id"],
        "acceptance_sha256": acceptance["acceptance_sha256"],
        "acceptance_type": acceptance["acceptance_type"],
        "accepted_at": acceptance["accepted_at"],
        "execution_start": acceptance["execution_start"],
        "run_id": acceptance["run_id"], "solution_id": plan["solution_id"],
        "selected_profile": acceptance.get("selected_profile"),
        "plan_sha256": acceptance["plan_sha256"],
        "manifest_sha256": acceptance["manifest_sha256"],
        "validation_sha256": acceptance["validation_sha256"],
    }
    payload = {
        "schema_version": MOTION_SCHEMA_VERSION,
        "unit_metadata": dict(MOTION_UNIT_METADATA),
        "replay_policy_version": REPLAY_POLICY_VERSION,
        "leader_policy_id": LEADER_POLICY_ID,
        "mode": "ROLLING_REPLAY_SYNTHETIC", "snapshot_boundary": "BEFORE_NEW_ACTIONS",
        "state_id": None, "content_sha256": None,
        "state_version": parent.state_version + 1 if parent else 2,
        "current_time": target.isoformat(), "decision_epoch": target.isoformat(),
        "plan_origin_epoch": state.decision_epoch, "cost_epoch": state.decision_epoch,
        "root_initial_state": {
            "scenario_id": state.scenario_id, "state_schema_version": state.schema_version,
            "state_version": state.state_version,
            "initial_state_sha256": acceptance["initial_state_sha256"],
            "fixture_raw_sha256": state.fixture_raw_sha256,
            "receipt_sha256": state.receipt_sha256,
        },
        "parent_state": parent_ref, "accepted_plan": accepted_plan,
        "source_versions": {"routing": state.routing_version,
                            "features": state.features_version,
                            "context": state.context_version,
                            "delivery_area": state.delivery_area_version},
        "timeline_version": TIMELINE_VERSION,
        "geometry_interpolation_version": GEOMETRY_INTERPOLATION_VERSION,
        "orders": list(orders.values()), "vehicles": vehicles,
        "pending_events": pending, "applied_event_ids": [],
        "execution_history": history, "executed_prefix_sha256": executed_prefix_sha,
        "execution_metrics": aggregate,
        "lineage": {"root_replay": parent is None,
                    "transition_count": 1 if parent is None else parent.state_version},
        "limits": {"event_application": False, "dynamic_solver": False,
                   "travel_features_frozen_at_plan_origin": True,
                   "pickup_service_seconds": 0.0,
                   "duration_quantization": "CEILING_TO_INTEGER_MICROSECOND",
                   "duration_quantization_error_bound_us_per_duration": 1},
    }
    return finalize_motion_payload(payload)


__all__ = [
    "ACCEPTANCE_SCHEMA_VERSION", "GEOMETRY_INTERPOLATION_VERSION", "LEADER_POLICY_ID",
    "LEADER_POLICY_SHA256", "MotionReplayError", "REPLAY_POLICY_VERSION",
    "TIMELINE_VERSION", "TimelineAction", "build_route_timelines", "canonical_sha256",
    "interpolate_directed_geometry", "replay_motion", "validate_acceptance_binding",
]
