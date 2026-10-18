"""Seeded S0-S8 fixtures on reachable graph nodes; no assignment solver claims."""

import copy
from datetime import timedelta
import json
import math
from pathlib import Path
import random

from pyproj import Geod
from shapely.geometry import box, mapping, shape

from geo_data.bundle import cached, instant, positive, publish, temporary
from geo_data.common import digest, exclusive_lock, read_json, write_json
from geo_data.depot_selection import select_depot
from geo_data.delivery_area import DeliveryArea, validate_area_membership
from geo_data.osm.routing import RoutingGraph, SearchLimitError

DEFAULT_SCENARIO = Path(__file__).with_name("scenario_profile.json")
SCENARIO_SCHEMA = "member1-scenarios/1"
GENERATOR_VERSION = "connected-depot/2"
GEOD = Geod(ellps="WGS84")


def validate_config(config):
    for key in ("maxDepotSnapKm", "radiusKm", "minDistanceKm", "capacityKg", "demandMinKg", "demandMaxKg",
                "serviceTimeHours", "shiftHours", "eventAfterHours", "costPerKmVnd", "rangeKm", "rainRadiusKm", "rainDurationHours", "rainMm"):
        positive(config[key], key)
    for key in ("candidateLimit", "searchMaxStates", "orders", "vehicles"):
        if isinstance(config[key], bool) or not isinstance(config[key], int) or config[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("depotCandidateLimit", "depotProbeMaxStates"):
        if key in config and (isinstance(config[key], bool) or not isinstance(config[key], int) or config[key] <= 0):
            raise ValueError(f"{key} must be a positive integer")
    if not -180 <= config["anchorLongitude"] <= 180 or not -90 <= config["anchorLatitude"] <= 90:
        raise ValueError("Invalid scenario anchor")
    if (config["orders"] < 3 or config["vehicles"] < 2 or config["candidateLimit"] < config["orders"] or
            config["demandMinKg"] > config["demandMaxKg"] or config["demandMaxKg"] > config["capacityKg"] or
            config["minDistanceKm"] >= config["radiusKm"] or config["eventAfterHours"] >= config["shiftHours"]):
        raise ValueError("Inconsistent scenario profile")
    for key in ("tradeoffMinFraction", "lowTradeoffMaxFraction"):
        if not 0 < positive(config[key], key) < 1:
            raise ValueError(f"{key} must be in (0,1)")
    return config


def timestamp(epoch, hours):
    return (epoch + timedelta(hours=hours)).isoformat()


def validate_scenario(scenario):
    state = scenario["initialState"]
    orders = {o["id"]: o for o in state["orders"]}
    vehicles = {v["id"]: v for v in state["vehicles"]}
    if len(orders) != len(state["orders"]) or len(vehicles) != len(state["vehicles"]):
        raise ValueError("Duplicate scenario entity IDs")
    owned = set()
    for vehicle in vehicles.values():
        ids = vehicle["onboardOrderIds"]
        if len(ids) != len(set(ids)) or owned.intersection(ids):
            raise ValueError("Onboard order ownership is not unique")
        owned.update(ids)
        if any(i not in orders for i in ids):
            raise ValueError("Unknown onboard order")
        expected = sum(orders[i]["demandKg"] for i in ids)
        if not math.isclose(vehicle["currentLoadKg"], expected) or expected > vehicle["capacityKg"]:
            raise ValueError("Vehicle load and onboard orders are inconsistent")
        for order_id in ids:
            if orders[order_id]["status"] != "ONBOARD" or orders[order_id]["assignedVehicleId"] != vehicle["id"]:
                raise ValueError("Onboard order assignment mismatch")
    epoch = instant(state["currentTime"])
    for order in orders.values():
        positive(order["demandKg"], "demandKg")
        positive(order["serviceTimeHours"], "serviceTimeHours", zero=True)
        if not instant(order["earliest"]) <= instant(order["preferredDue"]) <= instant(order["hardDeadline"]):
            raise ValueError("Order time window is inconsistent")
        if order["status"] == "ONBOARD" and order["id"] not in owned:
            raise ValueError("ONBOARD order has no owning vehicle")
    previous = epoch
    for event in scenario["events"]:
        current = instant(event["timestamp"])
        if current < previous:
            raise ValueError("Events must be ordered after decision epoch")
        previous = current
    return True


def generate_scenarios(routing, features, output, *, seed=42, config_path=DEFAULT_SCENARIO, depot_node=None, delivery_area=None, progress=print):
    output = Path(output)
    config = validate_config(read_json(config_path))
    area = DeliveryArea(read_json(delivery_area)) if delivery_area else None
    if config.get("requiredDeliveryAreaId") and (area is None or area.summary["areaId"] != config["requiredDeliveryAreaId"]):
        raise ValueError("Scenario profile requires its matching --delivery-area")
    if area and config["orders"] < len(area.regions):
        raise ValueError("Scenario orders must cover every delivery region")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("Scenario seed must be an integer")
    with RoutingGraph(routing, features=features) as graph:
        epoch = instant(graph.feature_manifest["at"])
        identity = {"stage": SCENARIO_SCHEMA, "routingVersion": graph.manifest["version"],
                    "featuresVersion": graph.feature_manifest["version"], "seed": seed,
                    "profileHash": digest(config), "depotNode": depot_node, "generatorVersion": GENERATOR_VERSION}
        if area:
            identity.update(deliveryAreaVersion=area.version, generatorVersion="delivery-area/1")
        with exclusive_lock(output):
            state_path = output / "build-state.json"
            # Upgrade only an unfinished legacy attempt with otherwise identical inputs.
            # Completed bundles stay immutable and require another output directory.
            if state_path.exists() and not (output / "manifest.json").exists():
                legacy_identity = {k: v for k, v in identity.items() if k != "generatorVersion"}
                if read_json(state_path) == legacy_identity:
                    write_json(state_path, identity)
            if saved := cached(output, identity):
                progress(f"Scenario cache verified: {output}")
                return saved
            rng = random.Random(seed)
            lon, lat = config["anchorLongitude"], config["anchorLatitude"]
            depot_audit = []
            if depot_node is None:
                try:
                    depot = select_depot(graph, config, progress=progress, audit=depot_audit, delivery_area=area)
                except ValueError:
                    write_json(output / "diagnostics.json", {"stage": "depot-selection", "depotSelection": depot_audit,
                               "generatorVersion": identity["generatorVersion"], "complete": False})
                    raise
            else:
                depot = graph.db.execute("SELECT * FROM nodes WHERE nodeId=?", (depot_node,)).fetchone()
                if depot is None:
                    raise ValueError("Depot node not found")
            if area and not area.covers(depot["longitude"], depot["latitude"]):
                raise ValueError("Explicit depot is outside the delivery area; select an in-area depot")
            lon, lat, depot_id = depot["longitude"], depot["latitude"], depot["nodeId"]
            candidates = []
            groups = {rid: [] for rid in area.regions} if area else {}
            for node in graph.db.execute("SELECT nodeId,longitude,latitude FROM nodes ORDER BY nodeId"):
                distance = GEOD.inv(lon, lat, node["longitude"], node["latitude"])[2] / 1000
                if distance < config["minDistanceKm"] or node["nodeId"] == depot_id:
                    continue
                if area:
                    rid = area.region_id(node["longitude"], node["latitude"])
                    if rid:
                        groups[rid].append({**dict(node), "deliveryRegionId": rid})
                elif distance <= config["radiusKm"]:
                    candidates.append(dict(node))
            if area:
                for rid, nodes in groups.items():
                    if not nodes:
                        raise ValueError(f"No graph delivery candidates in region {rid}")
                    rng.shuffle(nodes)
                # Round-robin prevents the larger polygon dominating the search budget.
                for index in range(max(map(len, groups.values()))):
                    candidates.extend(nodes[index] for nodes in groups.values() if index < len(nodes))
                progress(f"Delivery polygon candidates: { {rid: len(nodes) for rid, nodes in groups.items()} }; routing graph unchanged")
            else:
                rng.shuffle(candidates)
            reachable, diagnostics, tradeoff, low_tradeoff = [], [], None, None

            def save_diagnostics(complete=False):
                write_json(output / "diagnostics.json", {"stage": "candidate-search", "depotNodeId": depot_id,
                           "deliveryArea": area.summary if area else None,
                           "depotSelection": depot_audit, "reachableCount": len(reachable), "requiredCount": config["orders"],
                           "generatorVersion": identity["generatorVersion"], "diagnostics": diagnostics, "complete": complete})

            def checkpoint(node_id, status, **detail):
                diagnostics.append({"nodeId": node_id, "status": status, **detail})
                save_diagnostics()
                progress(f"  {status} | reachable {len(reachable)}/{config['orders']}")

            save_diagnostics()
            for index, node in enumerate(candidates[:config["candidateLimit"]], 1):
                progress(f"Scenarios candidate {index}/{min(len(candidates), config['candidateLimit'])}: node {node['nodeId']}")
                search_phase = "outbound"
                try:
                    fast = graph.path(depot_id, node["nodeId"], max_states=config["searchMaxStates"])
                    if fast is None:
                        checkpoint(node["nodeId"], "outbound-unreachable-under-policy")
                        continue
                    search_phase = "return"
                    back = graph.path(node["nodeId"], depot_id, incoming_edge=fast["edgeIds"][-1], max_states=config["searchMaxStates"])
                    if back is None:
                        checkpoint(node["nodeId"], "return-unreachable-under-policy")
                        continue
                    reachable.append({**node, "fastest": fast, "return": back})
                    search_phase = "exposure"
                    safe = graph.path(depot_id, node["nodeId"], weight="exposure", max_states=config["searchMaxStates"])
                    if safe is not None:
                        delay = (safe["travelTimeHours"] - fast["travelTimeHours"]) / fast["travelTimeHours"]
                        reduction = (fast["relativeExposure"] - safe["relativeExposure"]) / max(fast["relativeExposure"], 1e-12)
                        evidence = {**node, "fastest": fast, "safer": safe, "delayFraction": delay, "exposureReductionFraction": reduction}
                        if tradeoff is None and min(delay, reduction) >= config["tradeoffMinFraction"]:
                            tradeoff = evidence
                        if low_tradeoff is None and max(abs(delay), abs(reduction)) <= config["lowTradeoffMaxFraction"]:
                            low_tradeoff = evidence
                    checkpoint(node["nodeId"], "round-trip-verified")
                except SearchLimitError as exc:
                    checkpoint(node["nodeId"], "search-budget-exceeded", phase=search_phase, detail=str(exc))
                regions_covered = not area or {r["deliveryRegionId"] for r in reachable} == set(area.regions)
                if len(reachable) >= config["orders"] + 1 and tradeoff and low_tradeoff and regions_covered:
                    break
            if len(reachable) < config["orders"]:
                raise ValueError(f"Only {len(reachable)} round-trip candidates; need {config['orders']}; depot={depot_id}. See {output / 'diagnostics.json'} for unreachable vs search-budget failures.")
            if area:
                first_per_region = [next((n for n in reachable if n["deliveryRegionId"] == rid), None) for rid in area.regions]
                if any(n is None for n in first_per_region):
                    raise ValueError("Not every delivery region has a verified round trip; inspect diagnostics/search limits")
                selected_ids = {n["nodeId"] for n in first_per_region}
                reachable = first_per_region + [n for n in reachable if n["nodeId"] not in selected_ids]

            def order(i, node, created_hours=0):
                return {"id": f"O{i:03d}", "pickupLocationId": "DEPOT", "graphNodeId": node["nodeId"],
                        **({"deliveryRegionId": node["deliveryRegionId"]} if area else {}),
                        "longitude": node["longitude"], "latitude": node["latitude"],
                        "demandKg": round(rng.uniform(config["demandMinKg"], config["demandMaxKg"]), 3),
                        "serviceTimeHours": config["serviceTimeHours"], "earliest": timestamp(epoch, created_hours),
                        "preferredDue": timestamp(epoch, max(created_hours, config["shiftHours"] * 0.75)),
                        "hardDeadline": timestamp(epoch, config["shiftHours"]), "priority": 1,
                        "status": "WAITING", "assignedVehicleId": None, "pickedUpAt": None, "deliveredAt": None}

            orders = [order(i+1, n) for i, n in enumerate(reachable[:config["orders"]])]
            vehicles = [{"id": f"V{i+1}", "type": "motorcycle", "capacityKg": config["capacityKg"],
                         "costPerKmVnd": config["costPerKmVnd"], "rangeKm": config["rangeKm"],
                         "remainingRangeKm": config["rangeKm"], "currentPosition": {"graphNodeId": depot_id, "longitude": lon, "latitude": lat},
                         "positionTimestamp": epoch.isoformat(), "positionAccuracyM": None,
                         "currentLoadKg": 0, "onboardOrderIds": [], "availability": "AVAILABLE", "committedStopId": None,
                         "workingStart": epoch.isoformat(), "workingEnd": timestamp(epoch, config["shiftHours"])} for i in range(config["vehicles"])]
            base = {"schemaVersion": "member1-scenario-draft/1", "seed": seed, "sourceType": "SYNTHETIC",
                    "routingVersion": graph.manifest["version"], "featuresVersion": graph.feature_manifest["version"],
                    "contextVersion": graph.feature_manifest["contextVersion"],
                    "initialState": {"currentTime": epoch.isoformat(), "stateVersion": 1, "orders": orders, "vehicles": vehicles,
                                     "locations": [{"id": "DEPOT", "graphNodeId": depot_id, "longitude": lon, "latitude": lat,
                                                    "openingTime": epoch.isoformat(), "closingTime": timestamp(epoch, config["shiftHours"])}],
                                     "currentPlans": []}, "events": [], "executionUpdates": [],
                    "expectedInvariants": ["No implicit ONBOARD transfer", "DELIVERED immutable", "All profiles share one frozen context", "Report unserved orders explicitly"],
                    "validation": {"directedRoundTripsChecked": True, "jointVRPFeasibilityProven": False, "integrated": False}}
            if area:
                base.update(deliveryAreaVersion=area.version, deliveryArea=area.summary)
            scenarios = {f"S{i}": copy.deepcopy(base) for i in range(9)}
            for sid, scenario in scenarios.items():
                scenario["scenarioId"] = sid
            scenarios["S0"]["initialState"]["orders"] = copy.deepcopy(orders[:3])
            scenarios["S0"]["initialState"]["vehicles"] = copy.deepcopy(vehicles[:2])
            scenarios["S1"]["description"] = "Normal synthetic delivery day"
            event_at = timestamp(epoch, config["eventAfterHours"])
            urgent = order(len(orders)+1, reachable[-1], config["eventAfterHours"])
            urgent["priority"] = 3
            scenarios["S2"]["events"] = [{"eventId": "S2-E1", "type": "URGENT_ORDER", "timestamp": event_at, "sourceType": "SYNTHETIC", "orderPayload": urgent}]
            onboard = scenarios["S3"]["initialState"]["orders"][0]
            onboard.update(status="ONBOARD", assignedVehicleId="V1", pickedUpAt=epoch.isoformat())
            carrier = scenarios["S3"]["initialState"]["vehicles"][0]
            carrier.update(currentLoadKg=onboard["demandKg"], onboardOrderIds=[onboard["id"]])
            scenarios["S3"]["events"] = [{"eventId": "S3-E1", "type": "VEHICLE_UNAVAILABLE", "timestamp": event_at, "vehicleId": "V1", "availability": "UNAVAILABLE", "sourceType": "SYNTHETIC"}]
            scenarios["S3"]["expectedInvariants"].append("Unavailable vehicle with ONBOARD goods requires diagnostics; no reassignment")
            center = reachable[0]
            radius = config["rainRadiusKm"] * 1000
            west = GEOD.fwd(center["longitude"], center["latitude"], 270, radius)[0]
            east = GEOD.fwd(center["longitude"], center["latitude"], 90, radius)[0]
            south = GEOD.fwd(center["longitude"], center["latitude"], 180, radius)[1]
            north = GEOD.fwd(center["longitude"], center["latitude"], 0, radius)[1]
            polygon = box(west, south, east, north)
            affected = []
            progress("Scenarios: intersecting S4 synthetic rain polygon with routing edges...")
            for edge in graph.db.execute("SELECT edgeId,geometryJson FROM edges ORDER BY edgeId"):
                if polygon.intersects(shape(json.loads(edge["geometryJson"]))):
                    affected.append(edge["edgeId"])
            scenarios["S4"]["events"] = [{"eventId": "S4-E1", "type": "LOCAL_RAIN_WHAT_IF", "timestamp": event_at,
                "sourceType": "SYNTHETIC WHAT-IF", "polygon": mapping(polygon), "affectedEdgeIds": affected,
                "startTime": event_at, "endTime": timestamp(epoch, config["eventAfterHours"] + config["rainDurationHours"]),
                "contextDelta": {"precipitationMm": config["rainMm"], "precipitationIntervalHours": 1},
                "requiresFeatureRecompute": True}]
            tight = scenarios["S5"]["initialState"]["orders"][0]
            shortest_plus_service = reachable[0]["fastest"]["travelTimeHours"] + config["serviceTimeHours"]
            tight["hardDeadline"] = tight["preferredDue"] = timestamp(epoch, shortest_plus_service * 0.5)
            scenarios["S5"]["expectedInvariants"].append(f"{tight['id']} misses its hard deadline even on the shortest-time direct trip plus service")
            heavy = scenarios["S6"]["initialState"]["orders"][0]
            heavy["demandKg"] = config["capacityKg"] + 1
            scenarios["S6"]["expectedInvariants"].append(f"{heavy['id']} exceeds every vehicle capacity; must not be served without splitting (unsupported)")
            for sid, evidence in (("S7", tradeoff), ("S8", low_tradeoff)):
                scenarios[sid]["tradeoffEvidence"] = evidence
                scenarios[sid]["validation"]["tradeoffVerified"] = evidence is not None
                if evidence:
                    scenarios[sid]["initialState"]["orders"] = [order(1, evidence)]
                else:
                    scenarios[sid]["validation"]["pendingReason"] = "No qualifying route pair found within candidate/search limits; not an accepted benchmark yet"
            names = []
            for sid, scenario in scenarios.items():
                validate_scenario(scenario)
                validate_area_membership(scenario, area)
                name = f"{sid}.json"
                write_json(temporary(output, name), scenario)
                names.append(name)
            if area:
                write_json(temporary(output, "delivery_area.geojson"), area.document)
                names.append("delivery_area.geojson")
            write_json(temporary(output, "scenario_profile.json"), config)
            write_json(temporary(output, "qa_report.json"), {"reachableCandidates": reachable, "diagnostics": diagnostics,
                       "depotNodeId": depot_id, "depotSelection": depot_audit, "generatorVersion": identity["generatorVersion"],
                       "s7Verified": tradeoff is not None, "s8Verified": low_tradeoff is not None,
                       "scope": "Polygon-selected deliveries on the unchanged routing graph" if area else "Seeded local delivery scenarios on the full-city graph; not exhaustive city coverage",
                       "deliveryArea": area.summary if area else None,
                       "solverValidation": "Pending Member 2; no solver executed"})
            save_diagnostics(complete=True)
            return publish(output, identity, SCENARIO_SCHEMA, names + ["scenario_profile.json", "qa_report.json"], retained=["diagnostics.json"],
                           routingVersion=graph.manifest["version"], featuresVersion=graph.feature_manifest["version"],
                           contextVersion=graph.feature_manifest["contextVersion"], at=epoch.isoformat(), seed=seed,
                           scenarioCount=9, suiteReady=bool(tradeoff and low_tradeoff), integrated=False,
                           **({"deliveryAreaVersion": area.version, "deliveryArea": area.summary} if area else {}),
                           s7Verified=tradeoff is not None, s8Verified=low_tradeoff is not None, sourceType="SYNTHETIC")
