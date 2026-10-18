"""Read-only source-contract audit for the frozen Member 1 S0–S8 suite.

This checks catalog/fixture/stage identity, not joint routing feasibility.
The independent Member 1 verify-scenarios command checks full stage files,
including the large SQLite databases; this audit deliberately does not rehash
those databases or copy them into TASK-02.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SUITE_ID = "thu-duc-binh-thanh-v1"
SOURCE_RUN = "cached_context/hcmc/member1-tdbt-v1"
EPOCH = "2026-09-27T21:00:00+07:00"
STAGES = ("routing", "travel", "weather-plan", "weather", "features", "scenarios", "qa")
RECEIPT_PATH = Path(__file__).with_name("member1_trusted_receipt.json")
AUDIT_SCHEMA_VERSION = "task02-m1-source-contract-audit/2"
MANIFEST_SCHEMA_VERSION = "task02-m1-audit-manifest/2"
PASS_STATUS = "M1_SOURCE_CONTRACT_GATE_PASS"
FAIL_STATUS = "M1_SOURCE_CONTRACT_GATE_FAIL"
EVENT_TIME = "2026-09-27T21:15:00+07:00"
DEPOT_NODE = 366428309
DEMANDS = (3.41, 4.14, 3.527, 3.44, 4.228, 1.364, 3.694, 1.631)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha(document: dict[str, Any]) -> str:
    encoded = (json.dumps(document, ensure_ascii=False, sort_keys=True,
                          indent=2, allow_nan=False) + "\n").encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _number(value: Any, *, nonnegative: bool = True) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        numeric = float(value)
    except OverflowError:
        return False
    return math.isfinite(numeric) and (not nonnegative or numeric >= 0)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?\+07:00", value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() == timedelta(hours=7) else None


def _coordinates(value: Any) -> bool:
    return (isinstance(value, dict) and _number(value.get("longitude"), nonnegative=False)
            and _number(value.get("latitude"), nonnegative=False)
            and -180 <= value["longitude"] <= 180 and -90 <= value["latitude"] <= 90)


def _expected_order_ids(number: int) -> list[str]:
    count = 3 if number == 0 else 1 if number in (7, 8) else 8
    return [f"O{i:03d}" for i in range(1, count + 1)]


def _json_safe(value: Any) -> Any:
    """Keep diagnostics serializable even when a rejected source contains NaN."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _validate_fixture_semantics(fixture: Any, number: int) -> list[dict[str, Any]]:
    """Validate only observed facts of this pinned source suite, not VRP policy."""
    scenario_id = f"S{number}"
    findings: list[dict[str, Any]] = []

    def bad(code: str, path: str, message: str) -> None:
        findings.append({"code": code, "message": message,
                         "context": {"scenario_id": scenario_id, "path": path}})

    if not isinstance(fixture, dict):
        bad("FIXTURE_SCHEMA", "$", "scenario must be an object")
        return findings
    state = fixture.get("initialState")
    events = fixture.get("events")
    updates = fixture.get("executionUpdates")
    if not isinstance(state, dict):
        bad("FIXTURE_SCHEMA", "initialState", "state must be an object")
        return findings
    for path, value in (("initialState.orders", state.get("orders")),
                        ("initialState.vehicles", state.get("vehicles")),
                        ("initialState.locations", state.get("locations")),
                        ("initialState.currentPlans", state.get("currentPlans")),
                        ("events", events), ("executionUpdates", updates)):
        if not isinstance(value, list):
            bad("FIXTURE_SCHEMA", path, "required array missing or invalid")
    if findings:
        return findings
    orders, vehicles, locations = state["orders"], state["vehicles"], state["locations"]
    if fixture.get("schemaVersion") != "member1-scenario-draft/1":
        bad("FIXTURE_SCHEMA", "schemaVersion", "unexpected scenario schema")
    if type(state.get("stateVersion")) is not int or state["stateVersion"] != 1:
        bad("STATE_VERSION", "initialState.stateVersion", "pinned initial state is version 1")
    if state.get("currentTime") != EPOCH or _timestamp(state.get("currentTime")) is None:
        bad("STATE_TIME", "initialState.currentTime", "pinned state epoch must be ISO +07:00")
    if state["currentPlans"]:
        bad("STATE_PLANS", "initialState.currentPlans", "pinned source has no current plans")
    if updates:
        bad("STATE_UPDATES", "executionUpdates", "pinned source has no execution updates")
    claims = fixture.get("validation")
    if not isinstance(claims, dict) or (claims.get("integrated") is not False
                                       or claims.get("jointVRPFeasibilityProven") is not False):
        bad("SOURCE_SCOPE", "validation", "M1 export is not integrated or joint-VRP-proven")
    if len(locations) != 1 or not isinstance(locations[0], dict):
        bad("DEPOT_CONTRACT", "initialState.locations", "exactly one depot location required")
        depot = None
    else:
        depot = locations[0]
        if (depot.get("id") != "DEPOT" or depot.get("graphNodeId") != DEPOT_NODE
            or not _coordinates(depot) or _timestamp(depot.get("openingTime")) is None
            or _timestamp(depot.get("closingTime")) is None
            or depot.get("openingTime") != EPOCH
            or depot.get("closingTime") != "2026-09-28T05:00:00+07:00"):
            bad("DEPOT_CONTRACT", "initialState.locations[0]", "depot identity, coordinates or window invalid")
    if any(not isinstance(item, dict) for item in orders):
        bad("ORDER_SCHEMA", "initialState.orders", "orders must be objects")
        return findings
    if any(not isinstance(item, dict) for item in vehicles):
        bad("VEHICLE_SCHEMA", "initialState.vehicles", "vehicles must be objects")
        return findings
    order_ids = [item.get("id") for item in orders]
    vehicle_ids = [item.get("id") for item in vehicles]
    if order_ids != _expected_order_ids(number) or len(set(order_ids)) != len(order_ids):
        bad("ORDER_IDENTITY", "initialState.orders", "order IDs/order count differ from pinned suite")
    if vehicle_ids != ["V1", "V2"]:
        bad("VEHICLE_IDENTITY", "initialState.vehicles", "vehicle IDs/order differ from pinned suite")
    onboard_by_vehicle = {"V1": [], "V2": []}
    for index, order in enumerate(orders):
        path = f"initialState.orders[{index}]"
        ident = order.get("id")
        if (type(order.get("graphNodeId")) is not int or not _coordinates(order)
            or order.get("pickupLocationId") != "DEPOT"):
            bad("ORDER_LOCATION", path, "order node/coordinates/depot pickup invalid")
        if (not _number(order.get("demandKg")) or not _number(order.get("serviceTimeHours"))
            or type(order.get("priority")) is not int or order["priority"] < 1):
            bad("ORDER_UNITS", path, "demand kg/service hours/priority invalid")
        times = [_timestamp(order.get(key)) for key in ("earliest", "preferredDue", "hardDeadline")]
        if any(value is None for value in times) or not times[0] <= times[1] <= times[2]:
            bad("ORDER_WINDOW", path, "earliest/soft/hard must be ISO +07:00 and ordered")
        status = order.get("status")
        if status == "WAITING":
            if any(order.get(key) is not None for key in
                   ("assignedVehicleId", "pickedUpAt", "deliveredAt")):
                bad("CUSTODY_MISMATCH", path, "WAITING order has owner/custody history")
        elif status == "ONBOARD":
            owner = order.get("assignedVehicleId")
            if (number != 3 or ident != "O001" or owner not in onboard_by_vehicle
                or _timestamp(order.get("pickedUpAt")) is None
                or order.get("deliveredAt") is not None):
                bad("CUSTODY_MISMATCH", path, "ONBOARD owner/time contradicts pinned source")
            elif owner in onboard_by_vehicle:
                onboard_by_vehicle[owner].append(ident)
        else:
            bad("ORDER_STATUS", path + ".status", "unsupported order status in pinned initial state")
        expected_demand = (16 if number == 6 and ident == "O001" else
                           1.111 if number == 7 and ident == "O001" else
                           2.542 if number == 8 and ident == "O001" else
                           DEMANDS[int(ident[1:]) - 1] if isinstance(ident, str)
                           and re.fullmatch(r"O00[1-8]", ident) else None)
        if expected_demand is not None and (not _number(order.get("demandKg"))
                                             or not math.isclose(order["demandKg"], expected_demand, abs_tol=1e-9)):
            bad("S6_HARD_FACT" if number == 6 and ident == "O001" else "ORDER_DEMAND",
                path + ".demandKg", "demand differs from pinned source")
        if number == 5 and ident == "O001" and (
                order.get("preferredDue") != "2026-09-27T21:05:38.224243+07:00"
                or order.get("hardDeadline") != "2026-09-27T21:05:38.224243+07:00"
                or not _number(order.get("serviceTimeHours"))
                or not math.isclose(order["serviceTimeHours"], 1 / 12, abs_tol=1e-12)):
            bad("S5_HARD_FACT", path, "O001 soft/hard due or five-minute service differs")
    for index, vehicle in enumerate(vehicles):
        path = f"initialState.vehicles[{index}]"
        ident = vehicle.get("id")
        position = vehicle.get("currentPosition")
        if (vehicle.get("availability") != "AVAILABLE" or vehicle.get("committedStopId") is not None
            or not isinstance(position, dict) or position.get("graphNodeId") != DEPOT_NODE
            or not _coordinates(position) or _timestamp(vehicle.get("positionTimestamp")) is None):
            bad("VEHICLE_STATE", path, "initial vehicle availability/position/commitment invalid")
        if (not _number(vehicle.get("capacityKg")) or vehicle.get("capacityKg") != 15
            or not _number(vehicle.get("currentLoadKg"))
            or not _number(vehicle.get("rangeKm")) or not _number(vehicle.get("remainingRangeKm"))
            or vehicle.get("rangeKm") != 120 or vehicle.get("remainingRangeKm") > vehicle.get("rangeKm")):
            bad("VEHICLE_RESOURCES", path, "capacity/load/range units or values invalid")
        if (vehicle.get("workingStart") != EPOCH
            or vehicle.get("workingEnd") != "2026-09-28T05:00:00+07:00"
            or _timestamp(vehicle.get("workingStart")) is None
            or _timestamp(vehicle.get("workingEnd")) is None):
            bad("VEHICLE_WINDOW", path, "working time window invalid")
        onboard = vehicle.get("onboardOrderIds")
        expected = onboard_by_vehicle.get(ident, [])
        expected_load = sum(item["demandKg"] for item in orders
                            if item.get("id") in expected and _number(item.get("demandKg")))
        if (not isinstance(onboard, list) or onboard != expected
            or not _number(vehicle.get("currentLoadKg"))
            or not math.isclose(vehicle["currentLoadKg"], expected_load, abs_tol=1e-9)):
            bad("CUSTODY_MISMATCH", path, "onboard IDs or load differ from owned orders")
    if number == 3 and (orders[0].get("assignedVehicleId") != "V1"
                        or orders[0].get("pickedUpAt") != EPOCH):
        bad("CUSTODY_MISMATCH", "initialState.orders[0]", "S3 O001 owner/pickup differs")
    if number == 1 and (not all(order.get("status") == "WAITING" for order in orders)
                        or not math.isclose(sum(order.get("demandKg", 0) for order in orders
                                                if _number(order.get("demandKg"))), 25.434, abs_tol=1e-9)):
        bad("S1_HARD_FACT", "initialState.orders", "eight WAITING orders total 25.434 kg")
    if number == 6 and not math.isclose(sum(order.get("demandKg", 0) for order in orders
                                            if _number(order.get("demandKg"))), 38.024, abs_tol=1e-9):
        bad("S6_HARD_FACT", "initialState.orders", "total demand must be 38.024 kg")
    expected_events = 1 if number in (2, 3, 4) else 0
    if len(events) != expected_events or any(not isinstance(item, dict) for item in events):
        bad("EVENT_SCHEMA", "events", "event count/type differs from pinned source")
        return findings
    if not events:
        if number in (7, 8):
            evidence = fixture.get("tradeoffEvidence")
            if not isinstance(evidence, dict) or evidence.get("nodeId") != orders[0].get("graphNodeId"):
                bad("QA_TRADEOFF", "tradeoffEvidence", "single-leg QA evidence missing or target differs")
            else:
                fastest, safer = evidence.get("fastest"), evidence.get("safer")
                if not isinstance(fastest, dict) or not isinstance(safer, dict):
                    bad("QA_TRADEOFF", "tradeoffEvidence", "fastest/safer one-leg data absent")
                else:
                    for name, route in (("fastest", fastest), ("safer", safer)):
                        if (route.get("fromNodeId") != DEPOT_NODE
                            or route.get("toNodeId") != orders[0].get("graphNodeId")
                            or not isinstance(route.get("edgeIds"), list) or not route["edgeIds"]
                            or any(not isinstance(edge, str) for edge in route["edgeIds"])
                            or any(not _number(route.get(key)) for key in
                                   ("distanceKm", "travelTimeHours", "relativeExposure"))):
                            bad("QA_TRADEOFF", f"tradeoffEvidence.{name}", "one-leg QA route malformed")
                    if (_number(fastest.get("travelTimeHours")) and _number(safer.get("travelTimeHours"))
                        and _number(fastest.get("relativeExposure")) and _number(safer.get("relativeExposure"))
                        and (fastest["travelTimeHours"] > safer["travelTimeHours"]
                             or fastest["relativeExposure"] < safer["relativeExposure"])):
                        bad("QA_TRADEOFF", "tradeoffEvidence", "one-leg time/exposure direction invalid")
        return findings
    event = events[0]
    if _timestamp(event.get("timestamp")) is None or event.get("timestamp") != EVENT_TIME:
        bad("EVENT_TIMESTAMP", "events[0].timestamp", "event must occur at pinned 21:15 +07:00")
    if number == 2:
        payload = event.get("orderPayload")
        if (event.get("eventId") != "S2-E1" or event.get("type") != "URGENT_ORDER"
            or not isinstance(payload, dict) or payload.get("id") != "O009"
            or payload.get("status") != "WAITING" or payload.get("priority") != 3
            or not _number(payload.get("demandKg"))
            or not math.isclose(payload.get("demandKg", 0), 4.722, abs_tol=1e-9)
            or payload.get("earliest") != EVENT_TIME
            or payload.get("pickupLocationId") != "DEPOT"
            or payload.get("assignedVehicleId") is not None
            or payload.get("pickedUpAt") is not None or payload.get("deliveredAt") is not None
            or "O009" in order_ids):
            bad("S2_EVENT_PAYLOAD", "events[0].orderPayload", "O009 urgent payload/initial-state separation invalid")
        if payload is not None and isinstance(payload, dict) and (
                _timestamp(payload.get("earliest")) is None or
                not _number(payload.get("serviceTimeHours")) or
                not _coordinates(payload) or type(payload.get("graphNodeId")) is not int):
            bad("S2_EVENT_PAYLOAD", "events[0].orderPayload", "O009 time/units/location invalid")
    elif number == 3:
        if (event.get("eventId") != "S3-E1" or event.get("type") != "VEHICLE_UNAVAILABLE"
            or event.get("vehicleId") != "V1" or event.get("availability") != "UNAVAILABLE"):
            bad("S3_EVENT", "events[0]", "V1 unavailability event invalid")
    else:
        delta, edges, polygon = event.get("contextDelta"), event.get("affectedEdgeIds"), event.get("polygon")
        if (event.get("eventId") != "S4-E1" or event.get("type") != "LOCAL_RAIN_WHAT_IF"
            or event.get("startTime") != EVENT_TIME
            or event.get("endTime") != "2026-09-27T22:15:00+07:00"
            or event.get("requiresFeatureRecompute") is not True
            or not isinstance(delta, dict) or delta.get("precipitationMm") != 12
            or delta.get("precipitationIntervalHours") != 1
            or not isinstance(edges, list) or len(edges) != 2253
            or any(not isinstance(edge, str) or not edge for edge in edges)
            or (all(isinstance(edge, str) for edge in edges)
                and len(set(edges)) != len(edges))):
            bad("S4_EVENT_EVIDENCE", "events[0]", "rain delta/recompute/edge evidence invalid")
        rings = polygon.get("coordinates") if isinstance(polygon, dict) else None
        if (not isinstance(polygon, dict) or polygon.get("type") != "Polygon"
            or not isinstance(rings, list) or len(rings) != 1
            or not isinstance(rings[0], list) or len(rings[0]) < 4
            or rings[0][0] != rings[0][-1]
            or any(not isinstance(point, list) or len(point) != 2
                   or not _number(point[0], nonnegative=False)
                   or not _number(point[1], nonnegative=False)
                   or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90
                   for point in rings[0])):
            bad("S4_EVENT_EVIDENCE", "events[0].polygon", "WGS84 GeoJSON polygon invalid")
    return findings


def _fixture_row(scenario: dict[str, Any], relative_path: str, sha256: str) -> dict[str, Any]:
    state = scenario["initialState"]
    events = scenario.get("events", [])
    tradeoff = scenario.get("tradeoffEvidence", {})
    return {
        "scenario_id": scenario["scenarioId"], "fixture_path": relative_path,
        "sha256": sha256, "state_version": state.get("stateVersion"),
        "current_time": state.get("currentTime"),
        "current_plans": state.get("currentPlans"),
        "execution_updates": scenario.get("executionUpdates", []),
        "events": [{"type": event.get("type"), "timestamp": event.get("timestamp"),
                    "event_id": event.get("eventId"),
                    "affected_edge_count": len(event["affectedEdgeIds"])
                    if isinstance(event.get("affectedEdgeIds"), list) else None,
                    "affected_edge_ids_sha256": hashlib.sha256(json.dumps(
                        event.get("affectedEdgeIds", []), ensure_ascii=False,
                        separators=(",", ":")).encode("utf-8")).hexdigest()
                    if isinstance(event.get("affectedEdgeIds"), list) else None,
                    "context_delta": event.get("contextDelta"),
                    "start_time": event.get("startTime"),
                    "end_time": event.get("endTime"),
                    "requires_feature_recompute": event.get("requiresFeatureRecompute"),
                    "polygon": event.get("polygon"),
                    "order_payload": event.get("orderPayload"),
                    "vehicle_id": event.get("vehicleId"),
                    "availability": event.get("availability")}
                   for event in events],
        "orders": [{key: order.get(key) for key in (
            "id", "status", "assignedVehicleId", "pickedUpAt", "deliveredAt",
            "pickupLocationId", "graphNodeId", "demandKg", "serviceTimeHours",
            "earliest", "preferredDue", "hardDeadline")}
                   for order in state.get("orders", [])],
        "vehicles": [{key: vehicle.get(key) for key in (
            "id", "availability", "currentLoadKg", "onboardOrderIds",
            "committedStopId", "currentPosition", "capacityKg", "rangeKm",
            "remainingRangeKm", "workingStart", "workingEnd")}
                     for vehicle in state.get("vehicles", [])],
        "locations": state.get("locations"),
        "versions": {key: scenario.get(key) for key in (
            "schemaVersion", "routingVersion", "featuresVersion",
            "contextVersion", "deliveryAreaVersion")},
        "validation_claims": scenario.get("validation"),
        "qa_tradeoff": {key: tradeoff.get(key) for key in (
            "delayFraction", "exposureReductionFraction")},
    }


def audit_snapshot(snapshot_root: Path, *,
                   _test_receipt_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a structured source audit; never execute a TASK-02 solver.

    The private override is solely for isolated tests. The CLI has no receipt
    option and always reads the pinned TASK-02 receipt beside this module.
    """
    root = snapshot_root.resolve()
    report: dict[str, Any] = {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "suite_id": SUITE_ID, "source_run": SOURCE_RUN, "epoch": EPOCH,
        "integrated_solver_validated": False,
        "database_hashes_checked_by_this_audit": False,
        "rows": [], "diagnostics": [], "semantic_cases_checked": 0,
        "fixture_raw_sha256": {}, "receipt_version": None,
        "receipt_sha256": None, "receipt_export_gate_pass": False,
    }

    def issue(code: str, message: str, **context: Any) -> None:
        report["diagnostics"].append({"code": code, "message": message, "context": context})

    if not root.is_dir():
        issue("SOURCE_MISSING", "snapshot root does not exist", snapshot_root=str(root))
        report["status"] = FAIL_STATUS
        return report
    scenarios = root / "scenarios"
    try:
        receipt = (_test_receipt_override if _test_receipt_override is not None
                   else _json(RECEIPT_PATH))
        if not isinstance(receipt, dict):
            issue("RECEIPT_INVALID", "receipt JSON root must be an object",
                  path=str(RECEIPT_PATH))
            report["status"] = FAIL_STATUS
            return report
        report["receipt_version"] = receipt.get("receipt_version")
        report["receipt_sha256"] = (_canonical_sha(receipt)
                                    if _test_receipt_override is not None
                                    else _sha(RECEIPT_PATH))
        if _test_receipt_override is not None:
            report["receipt_origin"] = "INJECTED_TEST_ONLY"
        if (not isinstance(receipt.get("fixture_raw_sha256"), dict)
            or not isinstance(receipt.get("stage_versions"), dict)):
            issue("RECEIPT_INVALID", "receipt hash/version containers must be objects",
                  path=str(RECEIPT_PATH))
            report["status"] = FAIL_STATUS
            return report
        if (receipt.get("receipt_version") != "task02-m1-source-receipt/1"
            or receipt.get("suite_id") != SUITE_ID
            or receipt.get("source_run") != SOURCE_RUN
            or receipt.get("epoch") != EPOCH
            or set(receipt.get("fixture_raw_sha256", {})) != {f"S{i}" for i in range(9)}):
            issue("RECEIPT_INVALID", "local trusted receipt is incomplete or incompatible",
                  path=str(RECEIPT_PATH))
        catalog_path = scenarios / "manifests" / f"{SUITE_ID}.json"
        catalog = _json(catalog_path)
        if not isinstance(catalog, dict):
            issue("CATALOG_SCHEMA", "catalog JSON root must be an object",
                  path=str(catalog_path))
            report["status"] = FAIL_STATUS
            return report
        for key in ("sourceManifestHashes", "versions", "scenarios"):
            if not isinstance(catalog.get(key), dict):
                issue("CATALOG_SCHEMA", "catalog container must be an object",
                      path=f"{catalog_path}:{key}")
        if any(item["code"] == "CATALOG_SCHEMA" for item in report["diagnostics"]):
            report["status"] = FAIL_STATUS
            return report
        if (catalog.get("suiteId") != SUITE_ID or catalog.get("sourceRun") != SOURCE_RUN
            or catalog.get("at") != EPOCH or catalog.get("complete") is not True
            or catalog.get("integrated") is not False):
            issue("CATALOG_IDENTITY", "catalog suite/run/epoch/completeness is incompatible")
        report["catalog_sha256"] = _sha(scenarios / "manifests" / f"{SUITE_ID}.json")
        if (report["catalog_sha256"] != receipt.get("catalog_raw_sha256")
            or catalog.get("version") != receipt.get("catalog_version")):
            issue("RECEIPT_CATALOG_HASH", "raw catalog bytes/version differ from independent receipt",
                  path=f"scenarios/manifests/{SUITE_ID}.json")
        if (catalog.get("schemaVersion") != "member1-scenario-catalog/1"
            or catalog.get("suiteReady") is not True
            or catalog.get("units") != {"distance": "km", "duration": "h", "mass": "kg",
                                               "money": "VND", "speed": "km/h",
                                               "timezone": "Asia/Ho_Chi_Minh", "width": "m"}
            or not isinstance(catalog.get("scenarios"), dict)
            or set(catalog["scenarios"]) != {f"S{i}" for i in range(9)}):
            issue("CATALOG_SCHEMA", "catalog schema, units or nine-case index invalid")
        for scenario_id, entry in catalog["scenarios"].items():
            if not isinstance(entry, dict):
                issue("CATALOG_SCHEMA", "scenario catalog entry must be an object",
                      path=f"scenarios.{scenario_id}", scenario_id=scenario_id)
        if any(item["code"] == "CATALOG_SCHEMA" for item in report["diagnostics"]):
            report["status"] = FAIL_STATUS
            return report
        source = scenarios / SOURCE_RUN
        manifests = {stage: _json(source / stage / "manifest.json") for stage in STAGES}
        for stage, manifest in manifests.items():
            if not isinstance(manifest, dict):
                issue("STAGE_SCHEMA", "stage manifest root must be an object",
                      stage=stage, path=str(source / stage / "manifest.json"))
        if any(item["code"] == "STAGE_SCHEMA" for item in report["diagnostics"]):
            report["status"] = FAIL_STATUS
            return report
        manifest_files = manifests["scenarios"].get("files")
        if not isinstance(manifest_files, dict):
            issue("STAGE_SCHEMA", "scenario manifest files must be an object",
                  path="scenarios/manifest.json:files")
        else:
            for name, entry in manifest_files.items():
                if not isinstance(entry, dict):
                    issue("STAGE_SCHEMA", "scenario manifest file entry must be an object",
                          path=f"scenarios/manifest.json:files.{name}")
        if any(item["code"] == "STAGE_SCHEMA" for item in report["diagnostics"]):
            report["status"] = FAIL_STATUS
            return report
        for stage, manifest in manifests.items():
            if (manifest.get("complete") is not True
                or _canonical_sha(manifest) != catalog.get("sourceManifestHashes", {}).get(stage)
                or manifest.get("version") != catalog.get("versions", {}).get(stage)):
                issue("STAGE_IDENTITY", "stage manifest differs from catalog", stage=stage)
            if manifest.get("version") != receipt.get("stage_versions", {}).get(stage):
                issue("RECEIPT_STAGE_VERSION", "stage version differs from independent receipt", stage=stage)
        routing = manifests["routing"]
        travel = manifests["travel"]
        weather = manifests["weather"]
        features = manifests["features"]
        scenario_manifest = manifests["scenarios"]
        if (travel.get("routingVersion") != routing.get("version")
            or features.get("routingVersion") != routing.get("version")
            or features.get("travelVersion") != travel.get("version")
            or features.get("contextVersion") != weather.get("version")
            or scenario_manifest.get("routingVersion") != routing.get("version")
            or scenario_manifest.get("featuresVersion") != features.get("version")
            or scenario_manifest.get("contextVersion") != weather.get("version")
            or scenario_manifest.get("at") != EPOCH or features.get("at") != EPOCH):
            issue("STAGE_VERSION_CHAIN", "routing/travel/weather/features/scenario versions or epoch differ")
        qa = _json(source / "qa" / "qa_report.json")
        if not isinstance(qa, dict):
            issue("QA_SCHEMA", "QA report root must be an object",
                  path=str(source / "qa" / "qa_report.json"))
            report["status"] = FAIL_STATUS
            return report
        if (qa.get("checksPassed") is not True or qa.get("suiteReady") is not True
            or qa.get("integrated") is not False or qa.get("scenariosChecked") != 9):
            issue("QA_SCOPE", "Member 1 QA/export state is unexpected")
        weather_context = _json(source / "weather" / "weather_context.json")
        if not isinstance(weather_context, dict) or not isinstance(weather_context.get("regions"), list):
            issue("WEATHER_SCHEMA", "weather context/regions must be objects/array",
                  path=str(source / "weather" / "weather_context.json"))
            report["status"] = FAIL_STATUS
            return report
        regions = weather_context.get("regions", [])
        for index, region in enumerate(regions):
            path = f"weather_context.json:regions[{index}]"
            if not isinstance(region, dict):
                issue("WEATHER_SCHEMA", "weather region must be an object", path=path)
                continue
            flags = region.get("missingFlags", [])
            if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
                issue("WEATHER_SCHEMA", "missingFlags must be an array of strings",
                      path=f"{path}.missingFlags")
            for key in ("validAt", "fetchedAt"):
                if _timestamp(region.get(key)) is None:
                    issue("WEATHER_SCHEMA", "weather timestamp must be ISO +07:00",
                          path=f"{path}.{key}")
        if any(item["code"] == "WEATHER_SCHEMA" for item in report["diagnostics"]):
            report["status"] = FAIL_STATUS
            return report
        report["feature_provenance"] = {
            "edge_count": features.get("edgeCount"),
            "feature_fallback_edges": features.get("fallbackEdges"),
            "feature_source_type": features.get("sourceType"),
            "risk_model_version": features.get("riskModelVersion"),
            "travel_fallback_edges": travel.get("fallbackEdges"),
            "travel_source_type": travel.get("sourceType"),
            "weather_region_count": len(regions),
            "weather_fallback_regions": sum(item.get("fallbackUsed") is True for item in regions),
            "weather_missing_flags": dict(sorted(Counter(
                flag for item in regions for flag in item.get("missingFlags", [])).items())),
            "weather_valid_at": sorted({item.get("validAt") for item in regions}),
            "weather_fetched_at_min": min((item.get("fetchedAt") for item in regions), default=None),
            "weather_fetched_at_max": max((item.get("fetchedAt") for item in regions), default=None),
        }
        for number in range(9):
            scenario_id = f"S{number}"
            relative = f"fixtures/{SUITE_ID}/{scenario_id}.json"
            catalog_entries = catalog.get("scenarios")
            entry = catalog_entries.get(scenario_id, {}) if isinstance(catalog_entries, dict) else {}
            fixture_path = scenarios / relative
            source_path = source / "scenarios" / f"{scenario_id}.json"
            try:
                actual_sha = _sha(fixture_path)
                source_sha = _sha(source_path)
                fixture = _json(fixture_path)
            except (OSError, ValueError, UnicodeError) as error:
                issue("FIXTURE_READ_ERROR", "unable to read fixture/source-run copy",
                      scenario_id=scenario_id, path=relative, reason=str(error))
                continue
            report["fixture_raw_sha256"][scenario_id] = actual_sha
            if actual_sha != receipt.get("fixture_raw_sha256", {}).get(scenario_id):
                issue("RECEIPT_FIXTURE_HASH", "raw fixture bytes differ from independent receipt",
                      scenario_id=scenario_id, path=relative)
            manifest_files = scenario_manifest.get("files")
            source_entry = manifest_files.get(f"{scenario_id}.json", {}) if isinstance(manifest_files, dict) else {}
            if (entry.get("file") != relative or entry.get("sha256") != actual_sha
                or source_sha != actual_sha or source_entry.get("sha256") != actual_sha
                or source_entry.get("bytes") != fixture_path.stat().st_size):
                issue("FIXTURE_HASH", "fixture/catalog/source-run bytes disagree", scenario_id=scenario_id)
            report["semantic_cases_checked"] += 1
            try:
                report["diagnostics"].extend(_validate_fixture_semantics(fixture, number))
            except (KeyError, TypeError, ValueError) as error:
                issue("FIXTURE_SCHEMA", "semantic check could not read malformed fields",
                      scenario_id=scenario_id, path=relative, reason=str(error))
            if not isinstance(fixture, dict):
                issue("FIXTURE_SCHEMA", "fixture root must be an object",
                      scenario_id=scenario_id, path=relative)
                continue
            fixture_state = fixture.get("initialState")
            fixture_state = fixture_state if isinstance(fixture_state, dict) else {}
            fixture_claims = fixture.get("validation")
            fixture_claims = fixture_claims if isinstance(fixture_claims, dict) else {}
            if (fixture.get("scenarioId") != scenario_id
                or fixture.get("routingVersion") != routing.get("version")
                or fixture.get("featuresVersion") != features.get("version")
                or fixture.get("contextVersion") != weather.get("version")
                or fixture.get("deliveryAreaVersion") != scenario_manifest.get("deliveryAreaVersion")
                or fixture_state.get("currentTime") != EPOCH
                or fixture_claims.get("integrated") is not False
                or fixture_claims.get("jointVRPFeasibilityProven") is not False
                or not isinstance(fixture_state.get("orders"), list)
                or len(fixture_state["orders"]) != entry.get("orders")
                or not isinstance(fixture_state.get("vehicles"), list)
                or len(fixture_state["vehicles"]) != entry.get("vehicles")
                or not isinstance(fixture.get("events"), list)
                or len(fixture["events"]) != entry.get("events")):
                issue("FIXTURE_CONTRACT", "fixture versions/state/export claims differ",
                      scenario_id=scenario_id, path=relative)
            if (isinstance(fixture_state.get("orders"), list)
                and all(isinstance(order, dict) for order in fixture_state["orders"])
                and isinstance(fixture_state.get("vehicles"), list)
                and all(isinstance(vehicle, dict) for vehicle in fixture_state["vehicles"])
                and isinstance(fixture.get("events"), list)
                and all(isinstance(event, dict) for event in fixture["events"])):
                try:
                    report["rows"].append(_json_safe(_fixture_row(fixture, relative, actual_sha)))
                except (KeyError, TypeError, ValueError) as error:
                    issue("FIXTURE_SCHEMA", "unable to render malformed fixture row",
                          scenario_id=scenario_id, path=relative, reason=str(error))
            else:
                issue("FIXTURE_SCHEMA", "unable to render malformed fixture row",
                      scenario_id=scenario_id, path=relative)
        report["stage_versions"] = {stage: manifests[stage].get("version") for stage in STAGES}
        report["receipt_export_gate_pass"] = not report["diagnostics"] and report["semantic_cases_checked"] == 9
        report["status"] = (("OFFLINE_TEST_GATE_PASS" if _test_receipt_override is not None
                             else PASS_STATUS) if report["receipt_export_gate_pass"] else FAIL_STATUS)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        issue("SOURCE_READ_ERROR", "unable to complete source contract audit", reason=str(error))
        report["status"] = FAIL_STATUS
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only M1 S0–S8 source contract audit")
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    report = audit_snapshot(args.snapshot_root)
    if args.output_root:
        run_id = "M1_AUDIT_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = args.output_root.resolve() / run_id
        destination.mkdir(parents=True, exist_ok=False)
        payload_path = destination / "audit.json"
        payload_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION, "run_id": run_id,
            "status": report["status"], "audit_code_sha256": _sha(Path(__file__)),
            "receipt_version": report["receipt_version"],
            "receipt_sha256": report["receipt_sha256"],
            "receipt_export_gate_pass": report["receipt_export_gate_pass"],
            "semantic_cases_checked": report["semantic_cases_checked"],
            "database_hashes_checked_by_this_audit": False,
            "audit_json": {"sha256": _sha(payload_path), "bytes": payload_path.stat().st_size},
        }
        (destination / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        report_path = str(destination)
    else:
        report_path = None
    print(json.dumps({"status": report["status"], "suite_id": report["suite_id"],
                      "scenarios": len(report["rows"]),
                      "semantic_cases_checked": report["semantic_cases_checked"],
                      "receipt_export_gate_pass": report["receipt_export_gate_pass"],
                      "integrated_solver_validated": False,
                      "diagnostics": report["diagnostics"], "output_dir": report_path},
                     ensure_ascii=False))
    return 0 if report["status"] == PASS_STATUS else 2


if __name__ == "__main__":
    raise SystemExit(main())
