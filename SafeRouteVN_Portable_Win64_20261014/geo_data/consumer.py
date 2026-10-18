"""Read-only consumer adapter and S0 handoff smoke test owned by Member 1."""

import json
import math
from pathlib import Path

from geo_data.bundle import cached, publish, temporary
from geo_data.common import exclusive_lock, read_json, write_json
from geo_data.delivery_area import scenario_area, validate_area_membership
from geo_data.handoff import disjoint, verify_handoff
from geo_data.osm.routing import RoutingGraph
from geo_data.scenario_generator import validate_scenario


class Member1Dataset:
    def __init__(self, package):
        self.package = Path(package)
        self.manifest = verify_handoff(self.package)
        self.data = self.package / "data"
        self.graph = RoutingGraph(self.data / "routing", features=self.data / "features")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.graph.__exit__(*args)

    def scenario(self, scenario_id="S0"):
        if scenario_id not in {f"S{i}" for i in range(9)}:
            raise ValueError("Scenario must be S0 through S8")
        scenario = read_json(self.data / "scenarios" / f"{scenario_id}.json")
        validate_scenario(scenario)
        validate_area_membership(scenario, scenario_area(self.data / "scenarios", read_json(self.data / "scenarios/manifest.json")))
        if (scenario["routingVersion"] != self.graph.manifest["version"] or
                scenario["featuresVersion"] != self.graph.feature_manifest["version"] or
                scenario["contextVersion"] != self.graph.feature_manifest["contextVersion"]):
            raise ValueError("Scenario does not match this dataset snapshot")
        for obj in [*scenario["initialState"]["locations"], *scenario["initialState"]["orders"],
                    *(v["currentPosition"] for v in scenario["initialState"]["vehicles"])]:
            node = self.graph.db.execute("SELECT longitude,latitude FROM nodes WHERE nodeId=?", (obj["graphNodeId"],)).fetchone()
            if node is None or not all(math.isclose(obj[key], node[key], abs_tol=1e-7, rel_tol=0) for key in ("longitude", "latitude")):
                raise ValueError("Scenario coordinate does not match its graph node")
        return scenario

    def edge(self, edge_id):
        row = self.graph.db.execute("""SELECT e.*,w.tagsJson,f.payloadJson FROM edges e
          JOIN ways w USING(osmWayId) JOIN costs.features f USING(edgeId) WHERE edgeId=?""", (edge_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown routing edge: {edge_id}")
        feature = json.loads(row["payloadJson"])
        if feature["edgeId"] != edge_id or feature["fromNodeId"] != row["fromNodeId"] or feature["toNodeId"] != row["toNodeId"]:
            raise ValueError("Feature and graph edge disagree")
        return {**feature, "geometry": json.loads(row["geometryJson"]), "osmWayId": row["osmWayId"],
                "osmTags": json.loads(row["tagsJson"])}

    def describe_path(self, start, edge_ids, *, incoming_edge=None):
        edge_ids = list(edge_ids)
        if self.graph.db.execute("SELECT 1 FROM nodes WHERE nodeId=?", (start,)).fetchone() is None:
            raise ValueError("Unknown path start node")
        end = self.graph.validate_path(start, edge_ids, incoming_edge=incoming_edge)
        features, distance, duration, exposure = [], 0.0, 0.0, 0.0
        for sequence, edge_id in enumerate(edge_ids):
            edge = self.edge(edge_id)
            distance += edge["lengthKm"]
            duration += edge["travelTimeHours"]
            exposure += edge["relativeExposure"]
            features.append({"type": "Feature", "geometry": edge["geometry"], "properties": {
                "edgeId": edge_id, "sequence": sequence, "fromNodeId": edge["fromNodeId"], "toNodeId": edge["toNodeId"],
                "lengthKm": edge["lengthKm"], "travelTimeHours": edge["travelTimeHours"],
                "relativeExposure": edge["relativeExposure"], "edgeProxy": edge["edgeProxy"],
                "widthM": edge["widthM"], "missingFlags": edge["missingFlags"]}})
        return {"fromNodeId": start, "toNodeId": end, "edgeIds": edge_ids, "distanceKm": distance,
                "travelTimeHours": duration, "relativeExposure": exposure,
                "geojson": {"type": "FeatureCollection", "features": features}}


def consumer_smoke(package, output, *, progress=print):
    package, output = Path(package), Path(output)
    disjoint(package, output)
    with Member1Dataset(package) as dataset, exclusive_lock(output):
        identity = {"stage": "member1-consumer-smoke/1", "packageVersion": dataset.manifest["version"]}
        if saved := cached(output, identity):
            progress(f"Consumer smoke cache verified: {output}")
            return saved
        scenario = dataset.scenario("S0")
        state = scenario["initialState"]
        if len(state["orders"]) != 3 or len(state["vehicles"]) != 2:
            raise ValueError("S0 requires 3 orders and 2 vehicles")
        routes = {r["nodeId"]: r for r in read_json(dataset.data / "scenarios/qa_report.json")["reachableCandidates"]}
        locations = {p["id"]: p for p in state["locations"]}
        summaries, geometries = [], []
        for order in state["orders"]:
            progress(f"Consumer S0: checking {order['id']} pickup/delivery/return paths...")
            origin = locations[order["pickupLocationId"]]["graphNodeId"]
            records = routes.get(order["graphNodeId"])
            if records is None:
                raise ValueError("S0 order lacks saved route evidence")
            outbound = records["fastest"]
            if outbound["fromNodeId"] != origin or outbound["toNodeId"] != order["graphNodeId"]:
                raise ValueError("S0 route evidence has wrong endpoints")
            before = None
            for leg, route, expected_end in (("outbound", outbound, order["graphNodeId"]), ("return", records["return"], origin)):
                result = dataset.describe_path(route["fromNodeId"], route["edgeIds"], incoming_edge=before)
                if result["toNodeId"] != expected_end or route["toNodeId"] != expected_end:
                    raise ValueError("S0 route evidence end mismatch")
                for key in ("distanceKm", "travelTimeHours", "relativeExposure"):
                    if not math.isclose(result[key], route[key], rel_tol=1e-9, abs_tol=1e-10):
                        raise ValueError(f"S0 route metric mismatch: {key}")
                before = route["edgeIds"][-1] if route["edgeIds"] else before
                for feature in result.pop("geojson")["features"]:
                    feature["properties"].update(orderId=order["id"], leg=leg)
                    geometries.append(feature)
                summaries.append({"orderId": order["id"], "leg": leg, **result})
        for point in [*state["locations"], *state["orders"]]:
            geometries.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [point["longitude"], point["latitude"]]},
                               "properties": {"id": point["id"], "graphNodeId": point["graphNodeId"]}})
        report = {"consumerSmokePassed": True, "scenarioId": "S0", "ordersChecked": 3, "vehiclesChecked": 2,
                  "legsChecked": len(summaries), "paths": summaries, "packageVersion": dataset.manifest["version"],
                  "scope": "Reference adapter validates saved paths, metrics and geometry; no shortest-path search or VRP solve",
                  "integrated": False, "remaining": ["Member 2 solver/matrix integration", "Member 3 backend state/event integration"]}
        write_json(temporary(output, "consumer_report.json"), report)
        write_json(temporary(output, "s0_routes.geojson"), {"type": "FeatureCollection", "features": geometries})
        return publish(output, identity, "member1-consumer-smoke/1", ["consumer_report.json", "s0_routes.geojson"],
                       consumerSmokePassed=True, integrated=False, legsChecked=len(summaries))
