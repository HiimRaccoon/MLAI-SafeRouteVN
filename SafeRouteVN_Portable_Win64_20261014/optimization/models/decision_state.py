"""Immutable, versioned M1 initial DecisionState; no route/solver result here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
import re
from typing import Any, Mapping

from .common import ContractValidationError, freeze_json, to_json_value


STATE_SCHEMA_VERSION = "task02-m1-decision-state/2"
EVENT_SCHEMA_VERSION = "task02-m1-event-envelope/1"
UNIT_METADATA = {"mass": "kg", "distance": "meter", "duration": "second",
                 "cost_rate": "VND/km", "position_accuracy": "meter",
                 "geometry_order": "longitude_latitude", "time_zone": "+07:00"}


class StateContractError(ValueError):
    """A stable code and field path for adapter/consumer diagnostics."""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code, self.path = code, path


def _field(data: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in data:
        raise StateContractError("INVALID_DATA", f"{path}.{key}", "required field is missing")
    return data[key]


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise StateContractError("INVALID_DATA", path, "nonempty string required")
    return value


def _number(value: Any, path: str) -> float:
    if type(value) not in (int, float):
        raise StateContractError("INVALID_DATA", path, "finite nonnegative number required")
    try:
        numeric = float(value)
    except OverflowError as error:
        raise StateContractError("INVALID_DATA", path, "finite nonnegative number required") from error
    if not math.isfinite(numeric) or numeric < 0:
        raise StateContractError("INVALID_DATA", path, "finite nonnegative number required")
    return numeric


def _node(value: Any, path: str) -> int:
    if type(value) is not int or value <= 0:
        raise StateContractError("INVALID_DATA", path, "positive OSM integer node required")
    return value


def _instant(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?\+07:00", value):
        raise StateContractError("INVALID_DATA", path, "ISO timestamp with +07:00 required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise StateContractError("INVALID_DATA", path, "invalid ISO timestamp") from error
    if parsed.utcoffset() != timedelta(hours=7):
        raise StateContractError("INVALID_DATA", path, "UTC offset must be +07:00")
    return parsed


def _coordinates(value: Any, path: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise StateContractError("INVALID_DATA", path, "[longitude, latitude] required")
    lon = _number_signed(value[0], f"{path}[0]")
    lat = _number_signed(value[1], f"{path}[1]")
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
        raise StateContractError("INVALID_DATA", path, "WGS84 coordinate out of range")
    return lon, lat


def _number_signed(value: Any, path: str) -> float:
    if type(value) not in (int, float):
        raise StateContractError("INVALID_DATA", path, "finite number required")
    try:
        numeric = float(value)
    except OverflowError as error:
        raise StateContractError("INVALID_DATA", path, "finite number required") from error
    if not math.isfinite(numeric):
        raise StateContractError("INVALID_DATA", path, "finite number required")
    return numeric


def _sha256(value: Any, path: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise StateContractError("INVALID_DATA", path, "lowercase SHA-256 required")
    return value


def _items(value: Any, path: str) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise StateContractError("INVALID_DATA", path, "array required")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class DepotState:
    location_id: str
    graph_node_id: int
    coordinates: tuple[float, float]
    opening_time: str
    closing_time: str

    def __post_init__(self) -> None:
        _text(self.location_id, "depot.location_id")
        _node(self.graph_node_id, "depot.graph_node_id")
        object.__setattr__(self, "coordinates", _coordinates(self.coordinates, "depot.coordinates"))
        if _instant(self.opening_time, "depot.opening_time") > _instant(self.closing_time, "depot.closing_time"):
            raise StateContractError("INVALID_DATA", "depot.closing_time", "depot closes before opening")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DepotState":
        if not isinstance(value, Mapping):
            raise StateContractError("INVALID_DATA", "depot", "object required")
        return cls(*(_field(value, key, "depot") for key in
                     ("location_id", "graph_node_id", "coordinates", "opening_time", "closing_time")))

    def to_dict(self) -> dict[str, Any]:
        return {"location_id": self.location_id, "graph_node_id": self.graph_node_id,
                "coordinates": list(self.coordinates), "opening_time": self.opening_time,
                "closing_time": self.closing_time}


@dataclass(frozen=True, slots=True)
class OrderState:
    order_id: str
    delivery_region_id: str
    status: str
    graph_node_id: int
    coordinates: tuple[float, float]
    pickup_location_id: str
    priority: int
    demand_kg: float
    service_time_seconds: float
    earliest: str
    preferred_due: str
    hard_deadline: str
    assigned_vehicle_id: str | None
    picked_up_at: str | None
    delivered_at: str | None

    def __post_init__(self) -> None:
        _text(self.order_id, "orders.id")
        _text(self.delivery_region_id, "orders.deliveryRegionId")
        if not isinstance(self.status, str) or self.status not in {"WAITING", "ONBOARD", "DELIVERED"}:
            raise StateContractError("INVALID_DATA", "orders.status", "unknown order state")
        _node(self.graph_node_id, "orders.graphNodeId")
        object.__setattr__(self, "coordinates", _coordinates(self.coordinates, "orders.coordinates"))
        _text(self.pickup_location_id, "orders.pickupLocationId")
        if type(self.priority) is not int or self.priority < 1:
            raise StateContractError("INVALID_DATA", "orders.priority", "positive integer required")
        object.__setattr__(self, "demand_kg", _number(self.demand_kg, "orders.demandKg"))
        object.__setattr__(self, "service_time_seconds",
                           _number(self.service_time_seconds, "orders.serviceTimeSeconds"))
        if not (_instant(self.earliest, "orders.earliest")
                <= _instant(self.preferred_due, "orders.preferredDue")
                <= _instant(self.hard_deadline, "orders.hardDeadline")):
            raise StateContractError("INVALID_DATA", "orders.hardDeadline", "earliest/soft/hard window out of order")
        if self.status == "WAITING":
            if any(value is not None for value in
                   (self.assigned_vehicle_id, self.picked_up_at, self.delivered_at)):
                raise StateContractError("INVALID_DATA", "orders.assignedVehicleId",
                                         "WAITING order has custody history")
        else:
            _text(self.assigned_vehicle_id, "orders.assignedVehicleId")
            _instant(self.picked_up_at, "orders.pickedUpAt")
            if self.status == "ONBOARD" and self.delivered_at is not None:
                raise StateContractError("INVALID_DATA", "orders.deliveredAt", "ONBOARD cannot be delivered")
            if self.status == "DELIVERED":
                _instant(self.delivered_at, "orders.deliveredAt")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OrderState":
        if not isinstance(value, Mapping):
            raise StateContractError("INVALID_DATA", "orders", "object required")
        keys = ("order_id", "delivery_region_id", "status", "graph_node_id", "coordinates", "pickup_location_id",
                "priority", "demand_kg", "service_time_seconds", "earliest", "preferred_due",
                "hard_deadline", "assigned_vehicle_id", "picked_up_at", "delivered_at")
        return cls(*(_field(value, key, "orders") for key in keys))

    def to_dict(self) -> dict[str, Any]:
        return {"order_id": self.order_id, "delivery_region_id": self.delivery_region_id,
                "status": self.status,
                "graph_node_id": self.graph_node_id, "coordinates": list(self.coordinates),
                "pickup_location_id": self.pickup_location_id, "priority": self.priority,
                "demand_kg": self.demand_kg, "service_time_seconds": self.service_time_seconds,
                "earliest": self.earliest, "preferred_due": self.preferred_due,
                "hard_deadline": self.hard_deadline,
                "assigned_vehicle_id": self.assigned_vehicle_id,
                "picked_up_at": self.picked_up_at, "delivered_at": self.delivered_at}


@dataclass(frozen=True, slots=True)
class VehicleState:
    vehicle_id: str
    vehicle_type: str
    cost_per_km_vnd: float
    position_accuracy_m: float | None
    availability: str
    current_position_node_id: int
    current_position_coordinates: tuple[float, float]
    position_timestamp: str
    current_load_kg: float
    onboard_order_ids: tuple[str, ...]
    capacity_kg: float
    range_m: float
    remaining_range_m: float
    working_start: str
    working_end: str
    committed_stop_id: str | None

    def __post_init__(self) -> None:
        _text(self.vehicle_id, "vehicles.id")
        _text(self.vehicle_type, "vehicles.type")
        object.__setattr__(self, "cost_per_km_vnd",
                           _number(self.cost_per_km_vnd, "vehicles.costPerKmVnd"))
        if self.position_accuracy_m is not None:
            object.__setattr__(self, "position_accuracy_m",
                               _number(self.position_accuracy_m, "vehicles.positionAccuracyM"))
        if not isinstance(self.availability, str) or self.availability not in {"AVAILABLE", "UNAVAILABLE"}:
            raise StateContractError("INVALID_DATA", "vehicles.availability", "unknown availability")
        _node(self.current_position_node_id, "vehicles.currentPosition.graphNodeId")
        object.__setattr__(self, "current_position_coordinates",
                           _coordinates(self.current_position_coordinates, "vehicles.currentPosition.coordinates"))
        _instant(self.position_timestamp, "vehicles.positionTimestamp")
        for key in ("current_load_kg", "capacity_kg", "range_m", "remaining_range_m"):
            object.__setattr__(self, key, _number(getattr(self, key), f"vehicles.{key}"))
        if self.current_load_kg > self.capacity_kg or self.remaining_range_m > self.range_m:
            raise StateContractError("INVALID_DATA", "vehicles.currentLoadKg",
                                     "load/capacity or remaining/nominal range invalid")
        onboard = _items(self.onboard_order_ids, "vehicles.onboardOrderIds")
        for index, item in enumerate(onboard):
            _text(item, f"vehicles.onboardOrderIds[{index}]")
        if len(onboard) != len(set(onboard)):
            raise StateContractError("INVALID_DATA", "vehicles.onboardOrderIds", "duplicate onboard order")
        object.__setattr__(self, "onboard_order_ids", onboard)
        if _instant(self.working_start, "vehicles.workingStart") > _instant(self.working_end, "vehicles.workingEnd"):
            raise StateContractError("INVALID_DATA", "vehicles.workingEnd", "working window invalid")
        if self.committed_stop_id is not None:
            _text(self.committed_stop_id, "vehicles.committedStopId")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VehicleState":
        if not isinstance(value, Mapping):
            raise StateContractError("INVALID_DATA", "vehicles", "object required")
        keys = ("vehicle_id", "vehicle_type", "cost_per_km_vnd", "position_accuracy_m",
                "availability", "current_position_node_id",
                "current_position_coordinates", "position_timestamp", "current_load_kg",
                "onboard_order_ids", "capacity_kg", "range_m", "remaining_range_m",
                "working_start", "working_end", "committed_stop_id")
        return cls(*(_field(value, key, "vehicles") for key in keys))

    def to_dict(self) -> dict[str, Any]:
        return {"vehicle_id": self.vehicle_id, "vehicle_type": self.vehicle_type,
                "cost_per_km_vnd": self.cost_per_km_vnd,
                "position_accuracy_m": self.position_accuracy_m,
                "availability": self.availability,
                "current_position_node_id": self.current_position_node_id,
                "current_position_coordinates": list(self.current_position_coordinates),
                "position_timestamp": self.position_timestamp,
                "current_load_kg": self.current_load_kg,
                "onboard_order_ids": list(self.onboard_order_ids), "capacity_kg": self.capacity_kg,
                "range_m": self.range_m, "remaining_range_m": self.remaining_range_m,
                "working_start": self.working_start, "working_end": self.working_end,
                "committed_stop_id": self.committed_stop_id}


def _canonical_payload_sha(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(to_json_value(payload), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    schema_version: str
    event_id: str
    event_type: str
    timestamp: str
    scenario_id: str
    source_run: str
    routing_version: str
    features_version: str
    context_version: str
    payload: Mapping[str, Any]
    payload_canonical_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != EVENT_SCHEMA_VERSION:
            raise StateContractError("VERSION_MISMATCH", "pending_events.schema_version", "unknown event envelope version")
        _text(self.event_id, "pending_events.event_id")
        if not isinstance(self.event_type, str) or self.event_type not in {
                "URGENT_ORDER", "VEHICLE_UNAVAILABLE", "LOCAL_RAIN_WHAT_IF"}:
            raise StateContractError("INVALID_DATA", "pending_events.event_type", "unknown event type")
        _instant(self.timestamp, "pending_events.timestamp")
        for key in ("scenario_id", "source_run", "routing_version", "features_version", "context_version"):
            _text(getattr(self, key), f"pending_events.{key}")
        if not isinstance(self.payload, Mapping):
            raise StateContractError("INVALID_DATA", "pending_events.payload", "event object required")
        try:
            frozen = freeze_json(self.payload, "pending_events.payload")
        except ContractValidationError as error:
            raise StateContractError("INVALID_DATA", "pending_events.payload", str(error)) from error
        object.__setattr__(self, "payload", frozen)
        try:
            canonical_sha = _canonical_payload_sha(frozen)
        except (TypeError, ValueError, OverflowError) as error:
            raise StateContractError("INVALID_DATA", "pending_events.payload",
                                     "event payload cannot be canonically encoded") from error
        if self.payload_canonical_sha256 != canonical_sha:
            raise StateContractError("INVALID_DATA", "pending_events.payload_canonical_sha256",
                                     "event payload digest mismatch")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EventEnvelope":
        if not isinstance(value, Mapping):
            raise StateContractError("INVALID_DATA", "pending_events", "object required")
        keys = ("schema_version", "event_id", "event_type", "timestamp", "scenario_id",
                "source_run", "routing_version", "features_version", "context_version",
                "payload", "payload_canonical_sha256")
        return cls(*(_field(value, key, "pending_events") for key in keys))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "event_id": self.event_id,
                "event_type": self.event_type, "timestamp": self.timestamp,
                "scenario_id": self.scenario_id, "source_run": self.source_run,
                "routing_version": self.routing_version,
                "features_version": self.features_version, "context_version": self.context_version,
                "payload": to_json_value(self.payload),
                "payload_canonical_sha256": self.payload_canonical_sha256}


@dataclass(frozen=True, slots=True)
class DecisionState:
    schema_version: str
    suite_id: str
    scenario_id: str
    source_run: str
    source_authentication: str
    fixture_raw_sha256: str | None
    catalog_raw_sha256: str | None
    receipt_sha256: str | None
    receipt_version: str | None
    routing_version: str
    features_version: str
    context_version: str
    delivery_area_version: str
    state_version: int
    current_time: str
    decision_epoch: str
    depot: DepotState
    orders: tuple[OrderState, ...]
    vehicles: tuple[VehicleState, ...]
    current_plans: tuple[Any, ...]
    execution_updates: tuple[Any, ...]
    pending_events: tuple[EventEnvelope, ...]
    event_application_status: str
    depots: tuple[DepotState, ...] = ()
    source_kind: str | None = None

    def __post_init__(self) -> None:
        if self.source_kind not in (None, 'IMPORTED_SCENARIO', 'WEATHER_RUN'):
            raise StateContractError('INVALID_DATA', 'source_kind', 'unknown run source kind')
        if self.schema_version != STATE_SCHEMA_VERSION:
            raise StateContractError("VERSION_MISMATCH", "schema_version", "unknown DecisionState version")
        for key in ("suite_id", "scenario_id", "source_run", "routing_version", "features_version",
                    "context_version", "delivery_area_version"):
            _text(getattr(self, key), key)
        if not isinstance(self.scenario_id, str) or self.scenario_id not in {f"S{i}" for i in range(9)}:
            raise StateContractError("INVALID_DATA", "scenario_id", "S0–S8 required")
        if type(self.state_version) is not int or self.state_version != 1:
            raise StateContractError("VERSION_MISMATCH", "state_version", "pinned initial stateVersion must be 1")
        if _instant(self.current_time, "current_time") != _instant(self.decision_epoch, "decision_epoch"):
            raise StateContractError("INVALID_DATA", "decision_epoch", "initial epoch must equal current time")
        for key in ("orders", "vehicles", "pending_events", "current_plans", "execution_updates"):
            object.__setattr__(self, key, _items(getattr(self, key), key))
        if self.current_plans:
            raise StateContractError("UNSUPPORTED_POLICY", "initialState.currentPlans",
                                     "pinned suite has no commitment policy")
        if self.execution_updates:
            raise StateContractError("UNSUPPORTED_POLICY", "executionUpdates",
                                     "pinned suite has no execution-update policy")
        if not isinstance(self.depot, DepotState):
            raise StateContractError("INVALID_DATA", "depot", "DepotState required")
        if any(not isinstance(item, OrderState) for item in self.orders):
            raise StateContractError("INVALID_DATA", "orders", "OrderState items required")
        if any(not isinstance(item, VehicleState) for item in self.vehicles):
            raise StateContractError("INVALID_DATA", "vehicles", "VehicleState items required")
        if any(not isinstance(item, EventEnvelope) for item in self.pending_events):
            raise StateContractError("INVALID_DATA", "pending_events", "EventEnvelope items required")
        order_ids = [item.order_id for item in self.orders]
        vehicle_ids = [item.vehicle_id for item in self.vehicles]
        if len(order_ids) != len(set(order_ids)):
            raise StateContractError("INVALID_DATA", "orders", "duplicate order ID")
        if len(vehicle_ids) != len(set(vehicle_ids)):
            raise StateContractError("INVALID_DATA", "vehicles", "duplicate vehicle ID")
        by_id = {item.order_id: item for item in self.orders}
        for vehicle in self.vehicles:
            expected = tuple(item.order_id for item in self.orders
                             if item.status == "ONBOARD" and item.assigned_vehicle_id == vehicle.vehicle_id)
            if vehicle.onboard_order_ids != expected:
                raise StateContractError("INVALID_DATA", "vehicles.onboardOrderIds",
                                         "owner and onboard IDs disagree")
            expected_load = sum(by_id[oid].demand_kg for oid in expected)
            if not math.isclose(vehicle.current_load_kg, expected_load, abs_tol=1e-9):
                raise StateContractError("INVALID_DATA", "vehicles.currentLoadKg",
                                         "load differs from owned ONBOARD demand")
        if any(item.status == "ONBOARD" and item.assigned_vehicle_id not in vehicle_ids
               for item in self.orders):
            raise StateContractError("INVALID_DATA", "orders.assignedVehicleId",
                                     "ONBOARD owner is not a vehicle")
        if any(item.scenario_id != self.scenario_id or item.source_run != self.source_run
               or item.routing_version != self.routing_version
               or item.features_version != self.features_version
               or item.context_version != self.context_version for item in self.pending_events):
            raise StateContractError("VERSION_MISMATCH", "pending_events",
                                     "event/source identity differs from initial state")
        if len({item.event_id for item in self.pending_events}) != len(self.pending_events):
            raise StateContractError("INVALID_DATA", "pending_events", "duplicate event ID")
        expected_status = "NOT_MATERIALIZED" if self.pending_events else "NOT_APPLICABLE"
        if self.event_application_status != expected_status:
            raise StateContractError("INVALID_DATA", "event_application_status",
                                     "initial state cannot claim applied events")
        if self.source_authentication in ("PINNED_RECEIPT_VERIFIED", "RUN_MANIFEST_VERIFIED"):
            for key in ("fixture_raw_sha256", "catalog_raw_sha256", "receipt_sha256"):
                _sha256(getattr(self, key), key)
            _text(self.receipt_version, "receipt_version")
        elif self.source_authentication == "UNVERIFIED_TEST_INPUT":
            if any(getattr(self, key) is not None for key in
                   ("fixture_raw_sha256", "catalog_raw_sha256", "receipt_sha256", "receipt_version")):
                raise StateContractError("INVALID_DATA", "source_authentication",
                                         "unverified input cannot claim receipt hashes")
        else:
            raise StateContractError("INVALID_DATA", "source_authentication", "unknown source authentication")

    @property
    def unit_metadata(self) -> dict[str, str]:
        return dict(UNIT_METADATA)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DecisionState":
        if not isinstance(value, Mapping):
            raise StateContractError("INVALID_DATA", "decision_state", "object required")
        if value.get("schema_version") != STATE_SCHEMA_VERSION:
            raise StateContractError("VERSION_MISMATCH", "schema_version",
                                     "DecisionState v2 required; v1 cannot be inferred or silently migrated")
        keys = ("schema_version", "suite_id", "scenario_id", "source_run",
                "source_authentication", "fixture_raw_sha256", "catalog_raw_sha256",
                "receipt_sha256", "receipt_version", "routing_version", "features_version",
                "context_version", "delivery_area_version", "state_version", "current_time",
                "decision_epoch", "depot", "orders", "vehicles", "current_plans",
                "execution_updates", "pending_events", "event_application_status")
        data = {key: _field(value, key, "decision_state") for key in keys}
        if value.get("unit_metadata") != UNIT_METADATA:
            raise StateContractError("VERSION_MISMATCH", "unit_metadata", "canonical unit metadata mismatch")
        data["depot"] = DepotState.from_dict(data["depot"])
        data["orders"] = tuple(OrderState.from_dict(item) for item in _items(data["orders"], "orders"))
        data["vehicles"] = tuple(VehicleState.from_dict(item) for item in _items(data["vehicles"], "vehicles"))
        data["pending_events"] = tuple(EventEnvelope.from_dict(item)
                                       for item in _items(data["pending_events"], "pending_events"))
        data['depots'] = tuple(DepotState.from_dict(d) for d in value.get('depots', []))
        data['source_kind'] = value.get('source_kind')
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "unit_metadata": self.unit_metadata,
                "suite_id": self.suite_id, "scenario_id": self.scenario_id,
                "source_run": self.source_run,
                "source_authentication": self.source_authentication,
                "fixture_raw_sha256": self.fixture_raw_sha256,
                "catalog_raw_sha256": self.catalog_raw_sha256,
                "receipt_sha256": self.receipt_sha256,
                "receipt_version": self.receipt_version,
                "routing_version": self.routing_version,
                "features_version": self.features_version,
                "context_version": self.context_version,
                "delivery_area_version": self.delivery_area_version,
                "state_version": self.state_version, "current_time": self.current_time,
                "decision_epoch": self.decision_epoch, "depot": self.depot.to_dict(),
                "orders": [item.to_dict() for item in self.orders],
                "vehicles": [item.to_dict() for item in self.vehicles],
                "current_plans": list(self.current_plans),
                "execution_updates": list(self.execution_updates),
                "pending_events": [item.to_dict() for item in self.pending_events],
                "event_application_status": self.event_application_status,
                **({'source_kind': self.source_kind} if self.source_kind is not None else {}),
                **({'depots': [d.to_dict() for d in self.depots]} if self.depots else {})}
