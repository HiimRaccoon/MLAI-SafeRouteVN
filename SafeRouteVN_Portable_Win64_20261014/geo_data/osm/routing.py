"""Reference turn-aware reader for QA and consumer integration, not a VRP solver."""

from functools import lru_cache
import heapq
import itertools
import math
from pathlib import Path
import sqlite3

from geo_data.bundle import verify
from geo_data.features.edge_features import FEATURE_SCHEMA
from geo_data.features.travel_time import TRAVEL_SCHEMA
from geo_data.osm.process_graph import ROUTING_SCHEMA


class SearchLimitError(ValueError):
    pass


class RoutingGraph:
    def __init__(self, routing, *, travel=None, features=None):
        self.manifest = verify(routing, ROUTING_SCHEMA)
        self.feature_manifest = verify(features, FEATURE_SCHEMA) if features else None
        self.travel_manifest = verify(travel, TRAVEL_SCHEMA) if travel else None
        for manifest in (self.feature_manifest, self.travel_manifest):
            if manifest and manifest["routingVersion"] != self.manifest["version"]:
                raise ValueError("Routing reader input version mismatch")
        if self.feature_manifest and self.travel_manifest and self.feature_manifest["travelVersion"] != self.travel_manifest["version"]:
            raise ValueError("Features use a different travel snapshot")
        self.db = sqlite3.connect((Path(routing) / "network.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        try:
            if features:
                self.db.execute("ATTACH DATABASE ? AS costs", ((Path(features) / "features.sqlite").resolve().as_uri() + "?mode=ro",))
                self.query = "SELECT e.edgeId,e.fromNodeId,e.toNodeId,e.lengthKm,c.travelTimeHours,c.relativeExposure FROM edges e JOIN costs.features c USING(edgeId) WHERE e.fromNodeId=? ORDER BY e.edgeId"
            elif travel:
                self.db.execute("ATTACH DATABASE ? AS costs", ((Path(travel) / "travel.sqlite").resolve().as_uri() + "?mode=ro",))
                self.query = "SELECT e.edgeId,e.fromNodeId,e.toNodeId,e.lengthKm,c.travelTimeHours,NULL AS relativeExposure FROM edges e JOIN costs.travel c USING(edgeId) WHERE e.fromNodeId=? ORDER BY e.edgeId"
            else:
                self.query = "SELECT e.edgeId,e.fromNodeId,e.toNodeId,e.lengthKm,NULL AS travelTimeHours,NULL AS relativeExposure FROM edges e WHERE e.fromNodeId=? ORDER BY e.edgeId"
            if features or travel:
                table = "features" if features else "travel"
                count = self.db.execute(f"SELECT COUNT(*) FROM edges e JOIN costs.{table} c USING(edgeId)").fetchone()[0]
                if count != self.manifest["edgeCount"]:
                    raise ValueError("Route cost coverage is incomplete")
            self.forbidden = {(r[0], r[1]) for r in self.db.execute("SELECT inEdgeId,outEdgeId FROM forbidden_turns")}
            self.outgoing = lru_cache(maxsize=8192)(self._outgoing)
        except Exception:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.outgoing.cache_clear()
        self.db.close()

    def _outgoing(self, node):
        return tuple(dict(r) for r in self.db.execute(self.query, (node,)))

    def path(self, start, end, *, weight="time", max_states=250000, incoming_edge=None):
        if weight not in ("time", "distance", "exposure"):
            raise ValueError("Unknown route weight")
        if weight == "time" and not (self.travel_manifest or self.feature_manifest):
            raise ValueError("Time routing requires travel or feature data")
        if weight == "exposure" and not self.feature_manifest:
            raise ValueError("Exposure routing requires features")
        if isinstance(max_states, bool) or not isinstance(max_states, int) or max_states <= 0:
            raise ValueError("max_states must be a positive integer")
        for node in (start, end):
            if self.db.execute("SELECT 1 FROM nodes WHERE nodeId=?", (node,)).fetchone() is None:
                raise ValueError(f"Node {node} is absent from routing graph")
        if incoming_edge:
            previous = self.db.execute("SELECT toNodeId FROM edges WHERE edgeId=?", (incoming_edge,)).fetchone()
            if previous is None or previous[0] != start:
                raise ValueError("incoming_edge must end at start node")
        initial = (start, incoming_edge)
        distances, parents = {initial: 0.0}, {}
        serial = itertools.count()
        queue = [(0.0, next(serial), initial)]
        settled = 0
        field = {"time": "travelTimeHours", "distance": "lengthKm", "exposure": "relativeExposure"}[weight]
        while queue:
            cost, _, state = heapq.heappop(queue)
            if cost != distances[state]:
                continue
            settled += 1
            if settled > max_states:
                raise SearchLimitError("Route search hit max_states; reachability is unknown, not proven impossible")
            node, before = state
            if node == end:
                edges = []
                while state != initial:
                    state, edge = parents[state]
                    edges.append(edge)
                edges.reverse()
                return {"fromNodeId": start, "toNodeId": end, "edgeIds": [e["edgeId"] for e in edges],
                        "distanceKm": sum(e["lengthKm"] for e in edges),
                        "travelTimeHours": sum(e["travelTimeHours"] for e in edges) if self.travel_manifest or self.feature_manifest else None,
                        "relativeExposure": sum(e["relativeExposure"] for e in edges) if self.feature_manifest else None,
                        "weight": weight, "settledStates": settled, "routingVersion": self.manifest["version"]}
            for edge in self.outgoing(node):
                if (before, edge["edgeId"]) in self.forbidden:
                    continue
                step = edge[field]
                if step is None or not math.isfinite(step) or step < 0:
                    raise ValueError("Invalid route cost")
                following = (edge["toNodeId"], edge["edgeId"])
                candidate = cost + step
                if candidate < distances.get(following, math.inf):
                    distances[following] = candidate
                    parents[following] = (state, edge)
                    heapq.heappush(queue, (candidate, next(serial), following))
        return None

    def validate_path(self, start, edge_ids, *, incoming_edge=None):
        if incoming_edge:
            incoming = self.db.execute("SELECT toNodeId FROM edges WHERE edgeId=?", (incoming_edge,)).fetchone()
            if incoming is None or incoming[0] != start:
                raise ValueError("Invalid incoming edge for path validation")
        node, previous = start, incoming_edge
        for edge_id in edge_ids:
            edge = self.db.execute("SELECT fromNodeId,toNodeId FROM edges WHERE edgeId=?", (edge_id,)).fetchone()
            if edge is None or edge[0] != node or (previous, edge_id) in self.forbidden:
                raise ValueError(f"Invalid edge sequence at {edge_id}")
            node, previous = edge[1], edge_id
        return node
