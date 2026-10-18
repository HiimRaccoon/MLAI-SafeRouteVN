"""Strict internal motion-state contract for receipt-bound M1 replay.

This contract is intentionally separate from DecisionState/2 and the public
M2->M3/M4 API v1.  It represents simulated execution state, including a
mid-edge position, without pretending that a simulation is GPS telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
import re
from typing import Any, Mapping

from .common import ContractValidationError, freeze_json, to_json_value


MOTION_SCHEMA_VERSION = "task02-m1-motion-state/1"
MOTION_UNIT_METADATA = {
    "mass": "kg",
    "distance": "meter",
    "duration": "microsecond",
    "cost": "VND",
    "cost_rate": "VND/km",
    "geometry_order": "longitude_latitude",
    "exposure": "relative_exposure_proxy",
    "time_zone": "+07:00",
}
_SHA = re.compile(r"[0-9a-f]{64}")
_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+07:00")
_I64_MAX = (1 << 63) - 1


class MotionContractError(ValueError):
    """Stable diagnostic code and field path for motion contract failures."""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MotionContractError("INVALID_DATA", path, "object required")
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise MotionContractError("INVALID_DATA", path, "array required")
    return list(value)


def _required(value: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in value:
        raise MotionContractError("INVALID_DATA", f"{path}.{key}", "required field missing")
    return value[key]


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise MotionContractError("INVALID_DATA", path, "nonempty string required")
    return value


def _sha(value: Any, path: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise MotionContractError("INVALID_DATA", path, "lowercase SHA-256 required")
    return value


def _instant(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or _TIME.fullmatch(value) is None:
        raise MotionContractError("INVALID_DATA", path, "ISO timestamp with +07:00 required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise MotionContractError("INVALID_DATA", path, "invalid timestamp") from error
    if parsed.utcoffset() != timedelta(hours=7):
        raise MotionContractError("INVALID_DATA", path, "+07:00 required")
    return parsed


def _integer(value: Any, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > _I64_MAX:
        raise MotionContractError("INVALID_DATA", path, "bounded integer required")
    return value


def _number(value: Any, path: str, *, minimum: float = 0.0,
            maximum: float | None = None) -> float:
    if type(value) not in (int, float):
        raise MotionContractError("INVALID_DATA", path, "finite number required")
    try:
        numeric = float(value)
    except OverflowError as error:
        raise MotionContractError("INVALID_DATA", path, "finite number required") from error
    if (not math.isfinite(numeric) or numeric < minimum
            or (maximum is not None and numeric > maximum)):
        raise MotionContractError("INVALID_DATA", path, "number outside contract bounds")
    return numeric


def _coordinates(value: Any, path: str) -> list[float]:
    items = _array(value, path)
    if len(items) != 2:
        raise MotionContractError("INVALID_DATA", path, "[longitude, latitude] required")
    lon = _number(items[0], f"{path}[0]", minimum=-180.0, maximum=180.0)
    lat = _number(items[1], f"{path}[1]", minimum=-90.0, maximum=90.0)
    return [lon, lat]


def _walk_json(value: Any, path: str = "$", *, depth: int = 0) -> None:
    if depth > 64:
        raise MotionContractError("INVALID_DATA", path, "JSON nesting is too deep")
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return
    if type(value) is int:
        if abs(value) > _I64_MAX:
            raise MotionContractError("INVALID_DATA", path, "integer exceeds int64")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise MotionContractError("INVALID_DATA", path, "nonfinite number")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise MotionContractError("INVALID_DATA", path, "JSON object keys must be strings")
            _walk_json(item, f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _walk_json(item, f"{path}[{index}]", depth=depth + 1)
        return
    raise MotionContractError("INVALID_DATA", path, "JSON-compatible value required")


def canonical_json(value: Any) -> bytes:
    _walk_json(value)
    try:
        return json.dumps(to_json_value(value), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ContractValidationError, TypeError, ValueError, OverflowError) as error:
        raise MotionContractError("INVALID_DATA", "$", "payload is not canonical JSON") from error


def motion_content_sha256(value: Mapping[str, Any]) -> str:
    material = dict(value)
    material["state_id"] = None
    material["content_sha256"] = None
    return hashlib.sha256(canonical_json(material)).hexdigest()


def _validate_position(value: Any, path: str) -> None:
    position = _mapping(value, path)
    kind = _text(_required(position, "kind", path), f"{path}.kind")
    if position.get("position_source") != "SIMULATED":
        raise MotionContractError("INVALID_DATA", f"{path}.position_source", "SIMULATED required")
    if position.get("accuracy_m") is not None:
        _number(position["accuracy_m"], f"{path}.accuracy_m")
    _coordinates(_required(position, "coordinates", path), f"{path}.coordinates")
    incoming = position.get("incoming_edge")
    if incoming is not None:
        _text(incoming, f"{path}.incoming_edge")
    if kind == "AT_NODE":
        _integer(_required(position, "node_id", path), f"{path}.node_id", minimum=1)
        if any(key in position for key in ("edge_id", "progress", "from_node", "to_node")):
            raise MotionContractError("INVALID_DATA", path, "AT_NODE cannot claim edge progress")
    elif kind == "ON_EDGE":
        _text(_required(position, "edge_id", path), f"{path}.edge_id")
        _integer(_required(position, "from_node", path), f"{path}.from_node", minimum=1)
        _integer(_required(position, "to_node", path), f"{path}.to_node", minimum=1)
        _number(_required(position, "progress", path), f"{path}.progress", maximum=1.0)
        _integer(_required(position, "edge_entry_us", path), f"{path}.edge_entry_us")
        _integer(_required(position, "expected_exit_us", path), f"{path}.expected_exit_us")
        for key in ("remaining_time_us", "remaining_distance_m", "remaining_exposure"):
            _number(_required(position, key, path), f"{path}.{key}")
    else:
        raise MotionContractError("INVALID_DATA", f"{path}.kind", "unknown position kind")


def validate_motion_payload(value: Any, *, verify_digest: bool = True) -> None:
    root = _mapping(value, "motion_state")
    _walk_json(root, "motion_state")
    if root.get("schema_version") != MOTION_SCHEMA_VERSION:
        raise MotionContractError("VERSION_MISMATCH", "schema_version", "motion state /1 required")
    if root.get("unit_metadata") != MOTION_UNIT_METADATA:
        raise MotionContractError("VERSION_MISMATCH", "unit_metadata", "canonical units differ")
    if root.get("mode") != "ROLLING_REPLAY_SYNTHETIC":
        raise MotionContractError("INVALID_DATA", "mode", "ROLLING_REPLAY_SYNTHETIC required")
    if root.get("snapshot_boundary") != "BEFORE_NEW_ACTIONS":
        raise MotionContractError("INVALID_DATA", "snapshot_boundary", "BEFORE_NEW_ACTIONS required")
    for key in ("replay_policy_version", "leader_policy_id", "state_id"):
        _text(_required(root, key, "motion_state"), key)
    _sha(_required(root, "content_sha256", "motion_state"), "content_sha256")
    _integer(_required(root, "state_version", "motion_state"), "state_version", minimum=2)
    current = _instant(_required(root, "current_time", "motion_state"), "current_time")
    decision = _instant(_required(root, "decision_epoch", "motion_state"), "decision_epoch")
    origin = _instant(_required(root, "plan_origin_epoch", "motion_state"), "plan_origin_epoch")
    _instant(_required(root, "cost_epoch", "motion_state"), "cost_epoch")
    if current != decision or current < origin:
        raise MotionContractError("INVALID_DATA", "decision_epoch", "query epoch is inconsistent")

    initial = _mapping(_required(root, "root_initial_state", "motion_state"), "root_initial_state")
    for key in ("scenario_id", "state_schema_version", "fixture_raw_sha256",
                "receipt_sha256", "initial_state_sha256"):
        value_at = _required(initial, key, "root_initial_state")
        _sha(value_at, f"root_initial_state.{key}") if key.endswith("sha256") else _text(
            value_at, f"root_initial_state.{key}")
    _integer(_required(initial, "state_version", "root_initial_state"),
             "root_initial_state.state_version", minimum=1)

    accepted = _mapping(_required(root, "accepted_plan", "motion_state"), "accepted_plan")
    for key in ("acceptance_id", "run_id", "solution_id", "acceptance_type"):
        _text(_required(accepted, key, "accepted_plan"), f"accepted_plan.{key}")
    for key in ("acceptance_sha256", "plan_sha256", "manifest_sha256", "validation_sha256"):
        _sha(_required(accepted, key, "accepted_plan"), f"accepted_plan.{key}")
    _instant(_required(accepted, "accepted_at", "accepted_plan"), "accepted_plan.accepted_at")
    _instant(_required(accepted, "execution_start", "accepted_plan"), "accepted_plan.execution_start")
    profile = accepted.get("selected_profile")
    if profile is not None:
        _text(profile, "accepted_plan.selected_profile")

    versions = _mapping(_required(root, "source_versions", "motion_state"), "source_versions")
    for key in ("routing", "features", "context", "delivery_area"):
        _text(_required(versions, key, "source_versions"), f"source_versions.{key}")
    for key in ("timeline_version", "geometry_interpolation_version"):
        _text(_required(root, key, "motion_state"), key)
    lineage = _mapping(_required(root, "lineage", "motion_state"), "lineage")
    if type(_required(lineage, "root_replay", "lineage")) is not bool:
        raise MotionContractError("INVALID_DATA", "lineage.root_replay", "boolean required")
    _integer(_required(lineage, "transition_count", "lineage"),
             "lineage.transition_count", minimum=1)
    limits = _mapping(_required(root, "limits", "motion_state"), "limits")
    for key in ("event_application", "dynamic_solver", "travel_features_frozen_at_plan_origin"):
        if type(_required(limits, key, "limits")) is not bool:
            raise MotionContractError("INVALID_DATA", f"limits.{key}", "boolean required")
    _number(_required(limits, "pickup_service_seconds", "limits"), "limits.pickup_service_seconds")
    _text(_required(limits, "duration_quantization", "limits"), "limits.duration_quantization")
    _integer(_required(limits, "duration_quantization_error_bound_us_per_duration", "limits"),
             "limits.duration_quantization_error_bound_us_per_duration", minimum=1)

    orders = _array(_required(root, "orders", "motion_state"), "orders")
    vehicles = _array(_required(root, "vehicles", "motion_state"), "vehicles")
    order_ids: list[str] = []
    vehicle_ids: list[str] = []
    onboard_owner: dict[str, str] = {}
    for index, item in enumerate(orders):
        path = f"orders[{index}]"
        order = _mapping(item, path)
        oid = _text(_required(order, "order_id", path), f"{path}.order_id")
        order_ids.append(oid)
        status = _required(order, "status", path)
        if not isinstance(status, str) or status not in {"WAITING", "ONBOARD", "DELIVERED"}:
            raise MotionContractError("INVALID_DATA", f"{path}.status", "unknown order status")
        _number(_required(order, "demand_kg", path), f"{path}.demand_kg")
        owner = order.get("owner_vehicle_id")
        picked = order.get("picked_up_at")
        delivered = order.get("delivered_at")
        if status == "WAITING":
            if owner is not None or picked is not None or delivered is not None:
                raise MotionContractError("INVALID_DATA", path, "WAITING order has custody history")
        else:
            owner = _text(owner, f"{path}.owner_vehicle_id")
            onboard_owner[oid] = owner if status == "ONBOARD" else ""
            _instant(picked, f"{path}.picked_up_at")
            if status == "ONBOARD" and delivered is not None:
                raise MotionContractError("INVALID_DATA", f"{path}.delivered_at", "ONBOARD cannot be delivered")
            if status == "DELIVERED":
                _instant(delivered, f"{path}.delivered_at")
    if len(order_ids) != len(set(order_ids)):
        raise MotionContractError("INVALID_DATA", "orders", "duplicate order ID")

    for index, item in enumerate(vehicles):
        path = f"vehicles[{index}]"
        vehicle = _mapping(item, path)
        vid = _text(_required(vehicle, "vehicle_id", path), f"{path}.vehicle_id")
        vehicle_ids.append(vid)
        availability = _required(vehicle, "availability", path)
        if not isinstance(availability, str) or availability not in {"AVAILABLE", "UNAVAILABLE"}:
            raise MotionContractError("INVALID_DATA", f"{path}.availability", "unknown availability")
        _validate_position(_required(vehicle, "position", path), f"{path}.position")
        _instant(_required(vehicle, "position_timestamp", path), f"{path}.position_timestamp")
        activity = _required(vehicle, "activity", path)
        if not isinstance(activity, str) or activity not in {
                "IDLE_AT_DEPOT", "WAITING_AT_STOP", "SERVICING", "MOVING", "AT_NODE"}:
            raise MotionContractError("INVALID_DATA", f"{path}.activity", "unknown activity")
        load = _number(_required(vehicle, "current_load_kg", path), f"{path}.current_load_kg")
        capacity = _number(_required(vehicle, "capacity_kg", path), f"{path}.capacity_kg")
        if load > capacity + 1e-9:
            raise MotionContractError("INVALID_DATA", f"{path}.current_load_kg", "capacity exceeded")
        _number(_required(vehicle, "remaining_range_m", path), f"{path}.remaining_range_m")
        onboard = _array(_required(vehicle, "onboard_order_ids", path), f"{path}.onboard_order_ids")
        for oid_index, oid in enumerate(onboard):
            _text(oid, f"{path}.onboard_order_ids[{oid_index}]")
            if onboard_owner.get(oid) != vid:
                raise MotionContractError("INVALID_DATA", f"{path}.onboard_order_ids[{oid_index}]",
                                          "onboard owner mismatch")
        if len(onboard) != len(set(onboard)):
            raise MotionContractError("INVALID_DATA", f"{path}.onboard_order_ids", "duplicate onboard ID")
        _integer(_required(vehicle, "execution_cursor", path), f"{path}.execution_cursor")
        _integer(_required(vehicle, "next_controllable_node", path),
                 f"{path}.next_controllable_node", minimum=1)
        _integer(_required(vehicle, "reroute_available_at_us", path),
                 f"{path}.reroute_available_at_us")
        next_action = vehicle.get("next_action")
        if next_action is not None:
            next_action = _mapping(next_action, f"{path}.next_action")
            _text(_required(next_action, "kind", f"{path}.next_action"), f"{path}.next_action.kind")
            _integer(_required(next_action, "start_us", f"{path}.next_action"),
                     f"{path}.next_action.start_us")
            for key in ("order_id", "edge_id"):
                if next_action.get(key) is not None:
                    _text(next_action[key], f"{path}.next_action.{key}")
        commitment = vehicle.get("active_commitment")
        if commitment is not None:
            commitment = _mapping(commitment, f"{path}.active_commitment")
            _text(_required(commitment, "kind", f"{path}.active_commitment"),
                  f"{path}.active_commitment.kind")
            _integer(_required(commitment, "until_us", f"{path}.active_commitment"),
                     f"{path}.active_commitment.until_us")
        for key in ("completed_stops", "planned_suffix"):
            values = _array(_required(vehicle, key, path), f"{path}.{key}")
            if any(not isinstance(entry, str) or not entry for entry in values):
                raise MotionContractError("INVALID_DATA", f"{path}.{key}", "string IDs required")
        metrics = _mapping(_required(vehicle, "executed_metrics", path), f"{path}.executed_metrics")
        for key in ("distance_m", "travel_time_us", "waiting_time_us", "service_time_us",
                    "relative_exposure_proxy", "cost_vnd"):
            _number(_required(metrics, key, f"{path}.executed_metrics"),
                    f"{path}.executed_metrics.{key}")
    if len(vehicle_ids) != len(set(vehicle_ids)):
        raise MotionContractError("INVALID_DATA", "vehicles", "duplicate vehicle ID")
    if any(owner and owner not in vehicle_ids for owner in onboard_owner.values()):
        raise MotionContractError("INVALID_DATA", "orders.owner_vehicle_id", "unknown custody owner")

    pending = _array(_required(root, "pending_events", "motion_state"), "pending_events")
    event_ids: list[str] = []
    for index, event in enumerate(pending):
        item = _mapping(event, f"pending_events[{index}]")
        event_ids.append(_text(_required(item, "event_id", f"pending_events[{index}]"),
                               f"pending_events[{index}].event_id"))
    if len(event_ids) != len(set(event_ids)):
        raise MotionContractError("INVALID_DATA", "pending_events", "duplicate event ID")
    if _array(_required(root, "applied_event_ids", "motion_state"), "applied_event_ids"):
        raise MotionContractError("INVALID_DATA", "applied_event_ids", "Step 4 cannot apply events")
    history = _array(_required(root, "execution_history", "motion_state"), "execution_history")
    action_ids = []
    prior_us = -1
    for index, action in enumerate(history):
        path = f"execution_history[{index}]"
        item = _mapping(action, path)
        action_ids.append(_text(_required(item, "action_id", path), f"{path}.action_id"))
        kind = _text(_required(item, "kind", path), f"{path}.kind")
        if kind not in {"PICKUP", "EDGE_ENTER", "EDGE_COMPLETE", "SERVICE_START",
                        "DELIVERY_COMPLETE"}:
            raise MotionContractError("INVALID_DATA", f"{path}.kind", "unknown action kind")
        _text(_required(item, "vehicle_id", path), f"{path}.vehicle_id")
        at_us = _integer(_required(item, "at_us", path), f"{path}.at_us")
        if at_us < prior_us:
            raise MotionContractError("INVALID_DATA", f"{path}.at_us", "history is not ordered")
        prior_us = at_us
    if len(action_ids) != len(set(action_ids)):
        raise MotionContractError("INVALID_DATA", "execution_history", "duplicate action ID")
    _sha(_required(root, "executed_prefix_sha256", "motion_state"), "executed_prefix_sha256")
    aggregate = _mapping(_required(root, "execution_metrics", "motion_state"), "execution_metrics")
    for key in ("distance_m", "travel_time_us", "waiting_time_us", "service_time_us",
                "relative_exposure_proxy", "cost_vnd"):
        _number(_required(aggregate, key, "execution_metrics"), f"execution_metrics.{key}")
    parent = root.get("parent_state")
    if parent is not None:
        parent = _mapping(parent, "parent_state")
        _text(_required(parent, "state_id", "parent_state"), "parent_state.state_id")
        _sha(_required(parent, "content_sha256", "parent_state"), "parent_state.content_sha256")
        _integer(_required(parent, "state_version", "parent_state"), "parent_state.state_version", minimum=2)
        _instant(_required(parent, "current_time", "parent_state"), "parent_state.current_time")

    if verify_digest:
        expected = motion_content_sha256(root)
        if root.get("content_sha256") != expected or root.get("state_id") != f"motion-{expected[:24]}":
            raise MotionContractError("MOTION_DIGEST", "content_sha256", "motion identity mismatch")


def finalize_motion_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.setdefault("schema_version", MOTION_SCHEMA_VERSION)
    payload.setdefault("unit_metadata", dict(MOTION_UNIT_METADATA))
    payload["state_id"] = None
    payload["content_sha256"] = None
    digest = motion_content_sha256(payload)
    payload["content_sha256"] = digest
    payload["state_id"] = f"motion-{digest[:24]}"
    validate_motion_payload(payload)
    return json.loads(canonical_json(payload))


@dataclass(frozen=True, slots=True)
class MotionState:
    """Immutable validated wrapper around the versioned motion payload."""

    _payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        validate_motion_payload(self._payload)
        try:
            object.__setattr__(self, "_payload", freeze_json(self._payload, "motion_state"))
        except ContractValidationError as error:
            raise MotionContractError("INVALID_DATA", "motion_state", str(error)) from error

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MotionState":
        return cls(value)

    def to_dict(self) -> dict[str, Any]:
        return to_json_value(self._payload)

    @property
    def state_id(self) -> str:
        return str(self._payload["state_id"])

    @property
    def content_sha256(self) -> str:
        return str(self._payload["content_sha256"])

    @property
    def current_time(self) -> str:
        return str(self._payload["current_time"])

    @property
    def state_version(self) -> int:
        return int(self._payload["state_version"])


__all__ = [
    "MOTION_SCHEMA_VERSION", "MOTION_UNIT_METADATA", "MotionContractError",
    "MotionState", "canonical_json", "finalize_motion_payload",
    "motion_content_sha256", "validate_motion_payload",
]
