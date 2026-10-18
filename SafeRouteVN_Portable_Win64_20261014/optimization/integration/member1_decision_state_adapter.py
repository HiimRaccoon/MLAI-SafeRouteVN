"""Read-only, receipt-gated M1 initial-state adapter; never runs a solver."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from optimization.models.common import ContractValidationError, freeze_json
from optimization.models.decision_state import (
    STATE_SCHEMA_VERSION, EVENT_SCHEMA_VERSION, UNIT_METADATA, DecisionState,
    DepotState, EventEnvelope, OrderState, StateContractError, VehicleState,
    _canonical_payload_sha, _instant,
)
from .member1_mapping_audit import (
    EPOCH, PASS_STATUS, RECEIPT_PATH, SOURCE_RUN, SUITE_ID, audit_snapshot,
)


ADAPTER_SCHEMA_VERSION = "task02-m1-initial-adapter/2"
RUN_MANIFEST_VERSION = "task02-m1-initial-adapter-run/2"
_VERIFIED_PARSE_TOKEN = object()


def _diagnostic(code: str, path: str, message: str, scenario_id: str | None = None,
                state_version: int | None = None, fixture_version: str | None = None) -> dict[str, Any]:
    return {"severity": "ERROR", "code": code, "path": path,
            "message": message, "scenario_id": scenario_id,
            "state_version": state_version, "fixture_version": fixture_version}


def _required(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StateContractError("INVALID_DATA", path, "object required")
    return value


def _get(value: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in value:
        raise StateContractError("INVALID_DATA", f"{path}.{key}", "required field missing")
    return value[key]


def _source_number(value: Any, path: str) -> float:
    if type(value) not in (int, float):
        raise StateContractError("INVALID_DATA", path, "finite nonnegative number required")
    try:
        numeric = float(value)
    except OverflowError as error:
        raise StateContractError("INVALID_DATA", path, "finite nonnegative number required") from error
    if not math.isfinite(numeric) or numeric < 0:
        raise StateContractError("INVALID_DATA", path, "finite nonnegative number required")
    return numeric


def _source_node(value: Any, path: str) -> int:
    if type(value) is not int or value <= 0:
        raise StateContractError("INVALID_DATA", path, "positive OSM integer required")
    return value


def _source_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateContractError("INVALID_DATA", path, "nonempty string required")
    return value


def _source_coords(value: Mapping[str, Any], path: str) -> tuple[float, float]:
    lon = _get(value, "longitude", path)
    lat = _get(value, "latitude", path)
    if type(lon) not in (int, float) or type(lat) not in (int, float):
        raise StateContractError("INVALID_DATA", path, "WGS84 [longitude,latitude] invalid")
    try:
        lon, lat = float(lon), float(lat)
    except OverflowError as error:
        raise StateContractError("INVALID_DATA", path, "WGS84 coordinate out of range") from error
    if (not math.isfinite(lon) or not math.isfinite(lat)
        or not -180 <= lon <= 180 or not -90 <= lat <= 90):
        raise StateContractError("INVALID_DATA", path, "WGS84 [longitude,latitude] invalid")
    return lon, lat


def _source_time(value: Any, path: str) -> str:
    _instant(value, path)
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise StateContractError("INVALID_DATA", path, "array required")
    return value


def _depot(source: Any) -> DepotState:
    locations = _array(source, "initialState.locations")
    if len(locations) != 1:
        raise StateContractError("INVALID_DATA", "initialState.locations", "one DEPOT required")
    raw = _required(locations[0], "initialState.locations[0]")
    path = "initialState.locations[0]"
    return DepotState(
        location_id=_get(raw, "id", path),
        graph_node_id=_source_node(_get(raw, "graphNodeId", path), f"{path}.graphNodeId"),
        coordinates=_source_coords(raw, path),
        opening_time=_source_time(_get(raw, "openingTime", path), f"{path}.openingTime"),
        closing_time=_source_time(_get(raw, "closingTime", path), f"{path}.closingTime"),
    )


def _order(source: Any, index: int, prefix: str = "initialState.orders") -> OrderState:
    path = f"{prefix}[{index}]"
    raw = _required(source, path)
    service_hours = _source_number(_get(raw, "serviceTimeHours", path), f"{path}.serviceTimeHours")
    return OrderState(
        order_id=_get(raw, "id", path),
        delivery_region_id=_source_text(_get(raw, "deliveryRegionId", path),
                                        f"{path}.deliveryRegionId"),
        status=_get(raw, "status", path),
        graph_node_id=_source_node(_get(raw, "graphNodeId", path), f"{path}.graphNodeId"),
        coordinates=_source_coords(raw, path),
        pickup_location_id=_get(raw, "pickupLocationId", path),
        priority=_get(raw, "priority", path),
        demand_kg=_source_number(_get(raw, "demandKg", path), f"{path}.demandKg"),
        service_time_seconds=service_hours * 3600.0,
        earliest=_source_time(_get(raw, "earliest", path), f"{path}.earliest"),
        preferred_due=_source_time(_get(raw, "preferredDue", path), f"{path}.preferredDue"),
        hard_deadline=_source_time(_get(raw, "hardDeadline", path), f"{path}.hardDeadline"),
        assigned_vehicle_id=_get(raw, "assignedVehicleId", path),
        picked_up_at=_get(raw, "pickedUpAt", path),
        delivered_at=_get(raw, "deliveredAt", path),
    )


def _vehicle(source: Any, index: int) -> VehicleState:
    path = f"initialState.vehicles[{index}]"
    raw = _required(source, path)
    position = _required(_get(raw, "currentPosition", path), f"{path}.currentPosition")
    range_km = _source_number(_get(raw, "rangeKm", path), f"{path}.rangeKm")
    remaining_km = _source_number(_get(raw, "remainingRangeKm", path), f"{path}.remainingRangeKm")
    return VehicleState(
        vehicle_id=_get(raw, "id", path),
        vehicle_type=_source_text(_get(raw, "type", path), f"{path}.type"),
        cost_per_km_vnd=_source_number(_get(raw, "costPerKmVnd", path),
                                       f"{path}.costPerKmVnd"),
        position_accuracy_m=(None if _get(raw, "positionAccuracyM", path) is None else
                             _source_number(raw["positionAccuracyM"], f"{path}.positionAccuracyM")),
        availability=_get(raw, "availability", path),
        current_position_node_id=_source_node(
            _get(position, "graphNodeId", f"{path}.currentPosition"),
            f"{path}.currentPosition.graphNodeId"),
        current_position_coordinates=_source_coords(position, f"{path}.currentPosition"),
        position_timestamp=_source_time(_get(raw, "positionTimestamp", path),
                                        f"{path}.positionTimestamp"),
        current_load_kg=_source_number(_get(raw, "currentLoadKg", path), f"{path}.currentLoadKg"),
        onboard_order_ids=tuple(_array(_get(raw, "onboardOrderIds", path), f"{path}.onboardOrderIds")),
        capacity_kg=_source_number(_get(raw, "capacityKg", path), f"{path}.capacityKg"),
        range_m=range_km * 1000.0, remaining_range_m=remaining_km * 1000.0,
        working_start=_source_time(_get(raw, "workingStart", path), f"{path}.workingStart"),
        working_end=_source_time(_get(raw, "workingEnd", path), f"{path}.workingEnd"),
        committed_stop_id=_get(raw, "committedStopId", path),
    )


def _event(source: Any, index: int, *, scenario_id: str, fixture: Mapping[str, Any]) -> EventEnvelope:
    path = f"events[{index}]"
    raw = _required(source, path)
    kind = _get(raw, "type", path)
    if kind == "URGENT_ORDER":
        payload = _required(_get(raw, "orderPayload", path), f"{path}.orderPayload")
        parsed = _order(payload, index, prefix="events.orderPayload")
        if parsed.status != "WAITING" or parsed.order_id in {
                item.get("id") for item in fixture["initialState"]["orders"]}:
            raise StateContractError("INVALID_DATA", f"{path}.orderPayload",
                                     "urgent order must be new and WAITING")
    elif kind == "VEHICLE_UNAVAILABLE":
        if (raw.get("availability") != "UNAVAILABLE"
            or raw.get("vehicleId") not in {
                item.get("id") for item in fixture["initialState"]["vehicles"]}):
            raise StateContractError("INVALID_DATA", path, "vehicle unavailability payload invalid")
    elif kind == "LOCAL_RAIN_WHAT_IF":
        delta = _required(_get(raw, "contextDelta", path), f"{path}.contextDelta")
        edges = _array(_get(raw, "affectedEdgeIds", path), f"{path}.affectedEdgeIds")
        polygon = _required(_get(raw, "polygon", path), f"{path}.polygon")
        if (raw.get("requiresFeatureRecompute") is not True
            or not edges or any(not isinstance(item, str) or not item for item in edges)
            or len(set(edges)) != len(edges)
            or polygon.get("type") != "Polygon" or not isinstance(polygon.get("coordinates"), list)
            or not _source_number(delta.get("precipitationMm"), f"{path}.contextDelta.precipitationMm")
            or not _source_number(delta.get("precipitationIntervalHours"),
                                  f"{path}.contextDelta.precipitationIntervalHours")):
            raise StateContractError("INVALID_DATA", path, "rain feature evidence invalid")
        _source_time(_get(raw, "startTime", path), f"{path}.startTime")
        _source_time(_get(raw, "endTime", path), f"{path}.endTime")
    else:
        raise StateContractError("INVALID_DATA", f"{path}.type", "unknown event type")
    timestamp = _source_time(_get(raw, "timestamp", path), f"{path}.timestamp")
    try:
        payload_digest = _canonical_payload_sha(freeze_json(raw, f"{path}.payload"))
    except (ContractValidationError, TypeError, ValueError) as error:
        raise StateContractError("INVALID_DATA", f"{path}.payload",
                                 "event payload contains non-JSON or nonfinite data") from error
    return EventEnvelope(
        schema_version=EVENT_SCHEMA_VERSION,
        event_id=_get(raw, "eventId", path), event_type=kind, timestamp=timestamp,
        scenario_id=scenario_id, source_run=SOURCE_RUN,
        routing_version=fixture["routingVersion"],
        features_version=fixture["featuresVersion"],
        context_version=fixture["contextVersion"],
        payload=raw, payload_canonical_sha256=payload_digest,
    )


def _parse_fixture(fixture: Any, *, provenance: Mapping[str, Any] | None = None,
                   _verification_token: object | None = None) -> DecisionState:
    if provenance is not None and _verification_token is not _VERIFIED_PARSE_TOKEN:
        raise StateContractError("SOURCE_MISMATCH", "provenance",
                                 "caller-supplied dict cannot certify raw source bytes")
    raw = _required(fixture, "fixture")
    scenario_id = _get(raw, "scenarioId", "fixture")
    if not isinstance(scenario_id, str) or scenario_id not in {f"S{i}" for i in range(9)}:
        raise StateContractError("INVALID_DATA", "scenarioId", "S0–S8 required")
    if raw.get("schemaVersion") != "member1-scenario-draft/1":
        raise StateContractError("VERSION_MISMATCH", "schemaVersion", "M1 scenario draft/1 required")
    versions = {key: _get(raw, key, "fixture") for key in
                ("routingVersion", "featuresVersion", "contextVersion", "deliveryAreaVersion")}
    for key, value in versions.items():
        if not isinstance(value, str) or not value:
            raise StateContractError("VERSION_MISMATCH", key, "source version missing")
    initial = _required(_get(raw, "initialState", "fixture"), "initialState")
    state_version = _get(initial, "stateVersion", "initialState")
    if type(state_version) is not int or state_version != 1:
        raise StateContractError("VERSION_MISMATCH", "initialState.stateVersion", "pinned initial version 1 required")
    current_time = _source_time(_get(initial, "currentTime", "initialState"), "initialState.currentTime")
    if current_time != EPOCH:
        raise StateContractError("VERSION_MISMATCH", "initialState.currentTime", "pinned decision epoch differs")
    plans = _array(_get(initial, "currentPlans", "initialState"), "initialState.currentPlans")
    updates = _array(_get(raw, "executionUpdates", "fixture"), "executionUpdates")
    if plans:
        raise StateContractError("UNSUPPORTED_POLICY", "initialState.currentPlans",
                                 "commitments cannot be interpreted in gate 1")
    if updates:
        raise StateContractError("UNSUPPORTED_POLICY", "executionUpdates",
                                 "execution updates cannot be materialized in gate 1")
    orders = _array(_get(initial, "orders", "initialState"), "initialState.orders")
    vehicles = _array(_get(initial, "vehicles", "initialState"), "initialState.vehicles")
    events = _array(_get(raw, "events", "fixture"), "events")
    meta = provenance or {}
    return DecisionState(
        schema_version=STATE_SCHEMA_VERSION, suite_id=SUITE_ID,
        scenario_id=scenario_id, source_run=SOURCE_RUN,
        source_authentication=meta.get("source_authentication", "UNVERIFIED_TEST_INPUT"),
        fixture_raw_sha256=meta.get("fixture_raw_sha256"),
        catalog_raw_sha256=meta.get("catalog_raw_sha256"),
        receipt_sha256=meta.get("receipt_sha256"),
        receipt_version=meta.get("receipt_version"),
        routing_version=versions["routingVersion"],
        features_version=versions["featuresVersion"],
        context_version=versions["contextVersion"],
        delivery_area_version=versions["deliveryAreaVersion"],
        state_version=state_version, current_time=current_time,
        decision_epoch=current_time,
        depot=_depot(_get(initial, "locations", "initialState")),
        orders=tuple(_order(item, index) for index, item in enumerate(orders)),
        vehicles=tuple(_vehicle(item, index) for index, item in enumerate(vehicles)),
        current_plans=tuple(plans), execution_updates=tuple(updates),
        pending_events=tuple(_event(item, index, scenario_id=scenario_id, fixture=raw)
                             for index, item in enumerate(events)),
        event_application_status="NOT_MATERIALIZED" if events else "NOT_APPLICABLE",
    )


def parse_unverified_fixture(fixture: Any) -> dict[str, Any]:
    """Pure parser for tests; never certifies raw bytes or the M1 source gate."""
    scenario_id = fixture.get("scenarioId") if isinstance(fixture, Mapping) else None
    fixture_version = fixture.get("schemaVersion") if isinstance(fixture, Mapping) else None
    state_version = (fixture.get("initialState", {}).get("stateVersion")
                     if isinstance(fixture, Mapping) and isinstance(fixture.get("initialState"), Mapping)
                     else None)
    try:
        state = _parse_fixture(fixture)
    except StateContractError as error:
        return {"schema_version": ADAPTER_SCHEMA_VERSION, "status": error.code,
                "source_gate": "UNVERIFIED_TEST_INPUT", "state": None,
                "diagnostics": [_diagnostic(error.code, error.path, str(error),
                                            scenario_id, state_version, fixture_version)]}
    return {"schema_version": ADAPTER_SCHEMA_VERSION, "status": "INITIAL_STATE_READY",
            "source_gate": "UNVERIFIED_TEST_INPUT", "state": state.to_dict(),
            "diagnostics": []}


def load_pinned_initial_states(snapshot_root: str | Path) -> dict[str, Any]:
    """Receipt-gate all nine fixtures, then hash and parse each raw file once."""
    report = audit_snapshot(Path(snapshot_root))
    if report.get("status") != PASS_STATUS:
        codes = {item.get("code") for item in report.get("diagnostics", [])}
        status = ("SOURCE_MISMATCH" if any(code and
                  (code.startswith("RECEIPT_") or code == "FIXTURE_HASH") for code in codes)
                  else "VERSION_MISMATCH" if any(code and "VERSION" in code for code in codes)
                  else "INVALID_DATA")
        return {"schema_version": ADAPTER_SCHEMA_VERSION, "status": status,
                "source_gate": report.get("status"), "states": [],
                "diagnostics": [_diagnostic(item.get("code", "SOURCE_GATE_FAIL"),
                                            item.get("context", {}).get("path", "snapshot"),
                                            item.get("message", "source gate failed"),
                                            item.get("context", {}).get("scenario_id"))
                                for item in report.get("diagnostics", [])]}
    root = Path(snapshot_root).resolve()
    receipt = json.loads(RECEIPT_PATH.read_bytes())
    states: list[dict[str, Any]] = []
    try:
        for number in range(9):
            scenario_id = f"S{number}"
            path = root / "scenarios" / "fixtures" / SUITE_ID / f"{scenario_id}.json"
            data = path.read_bytes()
            actual_sha = hashlib.sha256(data).hexdigest()
            if (actual_sha != receipt["fixture_raw_sha256"][scenario_id]
                or actual_sha != report["fixture_raw_sha256"][scenario_id]):
                raise StateContractError("SOURCE_MISMATCH", str(path),
                                         "fixture changed after source gate")
            fixture = json.loads(data)
            state = _parse_fixture(fixture, provenance={
                "source_authentication": "PINNED_RECEIPT_VERIFIED",
                "fixture_raw_sha256": actual_sha,
                "catalog_raw_sha256": report["catalog_sha256"],
                "receipt_sha256": report["receipt_sha256"],
                "receipt_version": report["receipt_version"],
            }, _verification_token=_VERIFIED_PARSE_TOKEN)
            states.append(state.to_dict())
    except (OSError, KeyError, TypeError, ValueError, StateContractError) as error:
        code = error.code if isinstance(error, StateContractError) else "INVALID_DATA"
        field = error.path if isinstance(error, StateContractError) else "snapshot.fixture"
        return {"schema_version": ADAPTER_SCHEMA_VERSION, "status": code,
                "source_gate": "SOURCE_CHANGED_OR_INVALID", "states": [],
                "diagnostics": [_diagnostic(code, field, str(error),
                                            locals().get("scenario_id"))]}
    return {"schema_version": ADAPTER_SCHEMA_VERSION, "status": "INITIAL_STATE_READY",
            "source_gate": PASS_STATUS, "states": states, "diagnostics": [],
            "receipt_sha256": report["receipt_sha256"],
            "receipt_version": report["receipt_version"],
            "catalog_raw_sha256": report["catalog_sha256"],
            "fixture_raw_sha256": report["fixture_raw_sha256"],
            "database_hashes_checked_by_this_adapter": False,
            "integrated_solver_validated": False}


def apply_pending_event(state: DecisionState, event_id: str) -> dict[str, Any]:
    """Explicitly reject materialization until event-specific contracts exist."""
    if not isinstance(state, DecisionState):
        raise StateContractError("INVALID_DATA", "state", "DecisionState required")
    event = next((item for item in state.pending_events if item.event_id == event_id), None)
    if event is None:
        return {"schema_version": ADAPTER_SCHEMA_VERSION, "scenario_id": state.scenario_id,
                "status": "INVALID_DATA", "post_event_state": None,
                "diagnostics": [_diagnostic("EVENT_NOT_FOUND", "pending_events", "event ID not pending",
                                            state.scenario_id, state.state_version)]}
    if event.event_type == "URGENT_ORDER":
        status, code, reason = ("UNSUPPORTED_POLICY", "DEPOT_RELOAD_POLICY_MISSING",
                                "dispatch time, vehicle positions and depot reload policy are not materialized")
    elif event.event_type == "VEHICLE_UNAVAILABLE":
        status, code, reason = ("UNSUPPORTED_POLICY", "CUSTODY_POLICY_MISSING",
                                "unavailable vehicle carrying cargo has no approved custody/transfer policy")
    else:
        status, code, reason = ("UNSUPPORTED_FEATURES", "POST_RAIN_FEATURES_MISSING",
                                "post-rain features/context version has not been supplied")
    return {"schema_version": ADAPTER_SCHEMA_VERSION, "scenario_id": state.scenario_id,
            "event_id": event_id, "status": status, "post_event_state": None,
            "diagnostics": [_diagnostic(code, f"pending_events.{event_id}", reason,
                                        state.scenario_id, state.state_version)]}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Receipt-gated M1 initial DecisionState adapter")
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    batch = load_pinned_initial_states(args.snapshot_root)
    if batch["status"] != "INITIAL_STATE_READY":
        print(json.dumps({"status": batch["status"], "source_gate": batch["source_gate"],
                          "diagnostics": batch["diagnostics"]}, ensure_ascii=False))
        return 2
    states = [DecisionState.from_dict(item) for item in batch["states"]]
    rejections = [apply_pending_event(state, state.pending_events[0].event_id)
                  for state in states if state.pending_events]
    if len(rejections) != 3 or any(item["post_event_state"] is not None for item in rejections):
        print(json.dumps({"status": "INVALID_DATA", "diagnostics": [
            _diagnostic("EVENT_BOUNDARY", "pending_events", "S2/S3/S4 rejection boundary failed")]}))
        return 2
    run_id = "M1_STATE_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = args.output_root.resolve() / run_id
    destination.mkdir(parents=True, exist_ok=False)
    documents = {"initial_states.json": batch, "event_rejections.json": {
        "schema_version": ADAPTER_SCHEMA_VERSION, "run_id": run_id,
        "post_event_states_materialized": 0, "rejections": rejections}}
    for name, document in documents.items():
        (destination / name).write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8")
    model_path = Path(__file__).resolve().parents[1] / "models" / "decision_state.py"
    manifest = {
        "schema_version": RUN_MANIFEST_VERSION, "run_id": run_id,
        "status": "DECISION_STATE_ADAPTER_READY", "source_gate": PASS_STATUS,
        "receipt_sha256": batch["receipt_sha256"], "receipt_version": batch["receipt_version"],
        "catalog_raw_sha256": batch["catalog_raw_sha256"],
        "fixture_raw_sha256": batch["fixture_raw_sha256"],
        "state_schema_version": STATE_SCHEMA_VERSION,
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "adapter_schema_version": ADAPTER_SCHEMA_VERSION,
        "initial_states_count": len(states), "pending_events_count": len(rejections),
        "database_hashes_checked_by_this_adapter": False,
        "integrated_solver_validated": False,
        "code_sha256": {"member1_decision_state_adapter.py": _sha(Path(__file__)),
                        "decision_state.py": _sha(model_path),
                        "member1_mapping_audit.py": _sha(Path(__file__).with_name("member1_mapping_audit.py"))},
        "files": {name: {"bytes": (destination / name).stat().st_size,
                         "sha256": _sha(destination / name)} for name in documents},
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")
    print(json.dumps({"status": manifest["status"], "run_id": run_id,
                      "states": len(states), "pending_events": len(rejections),
                      "source_gate": PASS_STATUS, "output_dir": str(destination)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
