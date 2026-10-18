"""Read-only, lazy Member 1 routing/features adapter for the S0 solver.

This is a separate turn-state path-option domain. It does not redefine the
single-selected-path MatrixBundle or the legacy VRPTWSolution contract.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from time import monotonic
from typing import Any

from optimization.models import CandidatePath, EdgeReference, GeoPoint


class RoadDataError(ValueError):
    """Malformed node/edge/feature data, not a search failure."""


@dataclass(frozen=True)
class RoadPath:
    candidate: CandidatePath
    edge_ids: tuple[str, ...]
    feature_provenance: tuple[dict[str, Any], ...]

    @property
    def final_edge(self) -> str | None:
        return self.edge_ids[-1] if self.edge_ids else None

    def to_leg(self, incoming_edge: str | None) -> dict[str, Any]:
        return {
            "path_id": self.candidate.path_id,
            "from_node": self.candidate.nodes[0],
            "to_node": self.candidate.nodes[-1],
            "incoming_edge": incoming_edge,
            "final_edge": self.final_edge,
            "edge_ids": list(self.edge_ids),
            "candidate_path": self.candidate.to_dict(),
            "distance_m": self.candidate.distance,
            "travel_time_s": self.candidate.travel_time,
            "exposure": self.candidate.risk_score,
            "geometry": [point.to_list() for point in self.candidate.geometry],
            "feature_provenance": list(self.feature_provenance),
        }


@dataclass(frozen=True)
class RoadSearchResult:
    paths: tuple[RoadPath, ...]
    settled_states: int
    limit: str | None = None
    generated_labels: int = 0
    dominated_prunes: int = 0
    resource_prunes: int = 0
    peak_active_labels: int = 0
    distance_prunes: int = 0
    time_prunes: int = 0


class Member1RoadGraph:
    """SQLite graph reader; only outgoing edges being searched are fetched."""

    def __init__(
        self,
        network_sqlite: str | Path,
        features_sqlite: str | Path,
        *,
        routing_version: str,
        features_version: str,
        context_version: str,
        source_hashes: dict[str, str] | None = None,
    ) -> None:
        network = Path(network_sqlite).resolve()
        features = Path(features_sqlite).resolve()
        if not network.is_file() or not features.is_file():
            raise RoadDataError("routing and features SQLite files must exist")
        self.network_path = network
        self.features_path = features
        self.routing_version = routing_version
        self.features_version = features_version
        self.context_version = context_version
        self.source_hashes = source_hashes or {}
        self.db = sqlite3.connect(network.as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute(
                "ATTACH DATABASE ? AS costs", (features.as_uri() + "?mode=ro",)
            )
            self.forbidden = {
                (row[0], row[1])
                for row in self.db.execute(
                    "SELECT inEdgeId,outEdgeId FROM forbidden_turns"
                )
            }
            self._outgoing = lru_cache(maxsize=8192)(self._read_outgoing)
        except Exception:
            self.db.close()
            raise

    def __enter__(self) -> "Member1RoadGraph":
        return self

    def __exit__(self, *_args: Any) -> None:
        self._outgoing.cache_clear()
        self.db.close()

    def node(self, node_id: int) -> tuple[float, float] | None:
        row = self.db.execute(
            "SELECT longitude,latitude FROM nodes WHERE nodeId=?", (node_id,)
        ).fetchone()
        return (float(row[0]), float(row[1])) if row else None

    def edge(self, edge_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT e.edgeId,e.fromNodeId,e.toNodeId,e.lengthKm,e.geometryJson,"
            "f.travelTimeHours,f.relativeExposure,f.payloadJson "
            "FROM edges e JOIN costs.features f USING(edgeId) WHERE e.edgeId=?",
            (edge_id,),
        ).fetchone()
        return dict(row) if row else None

    def path_from_edge_ids(
        self, start: int, edge_ids: tuple[str, ...] | list[str], *,
        incoming_edge: str | None = None,
    ) -> RoadPath:
        """Reconstruct and source-check an explicit directed path.

        This additive audit seam does not search.  It is used to authenticate a
        cached finite path-pool against the currently opened raw SQLite view.
        """

        if not edge_ids or any(not isinstance(item, str) or not item for item in edge_ids):
            raise RoadDataError("explicit path needs nonempty string edge IDs")
        rows: list[dict[str, Any]] = []
        cursor = start
        previous = incoming_edge
        for edge_id in edge_ids:
            raw = self.edge(edge_id)
            if raw is None:
                raise RoadDataError(f"explicit path edge is absent: {edge_id}")
            if raw["fromNodeId"] != cursor:
                raise RoadDataError(f"explicit path edge is discontinuous: {edge_id}")
            if (previous, edge_id) in self.forbidden:
                raise RoadDataError(f"explicit path contains forbidden turn: {previous}->{edge_id}")
            # _assemble performs full geometry/feature/unit validation.
            self._checked_edge(raw)
            rows.append(raw)
            cursor = raw["toNodeId"]
            previous = edge_id
        return self._assemble(rows, start)

    def _read_outgoing(self, node_id: int) -> tuple[dict[str, Any], ...]:
        return tuple(
            dict(row)
            for row in self.db.execute(
                "SELECT e.edgeId,e.fromNodeId,e.toNodeId,e.lengthKm,e.geometryJson,"
                "f.travelTimeHours,f.relativeExposure,f.payloadJson "
                "FROM edges e JOIN costs.features f USING(edgeId) "
                "WHERE e.fromNodeId=? ORDER BY e.edgeId",
                (node_id,),
            )
        )

    def _checked_edge(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = json.loads(raw["payloadJson"])
            geometry = json.loads(raw["geometryJson"])
            if geometry.get("type") != "LineString":
                raise ValueError("geometry is not a GeoJSON LineString")
            points = tuple(GeoPoint.from_value(item) for item in geometry["coordinates"])
            if len(points) < 2:
                raise ValueError("edge geometry has fewer than two coordinates")
            origin, target = self.node(raw["fromNodeId"]), self.node(raw["toNodeId"])
            if origin is None or target is None:
                raise ValueError("edge endpoint node is absent")
            if max(abs(points[0].longitude - origin[0]), abs(points[0].latitude - origin[1]),
                   abs(points[-1].longitude - target[0]), abs(points[-1].latitude - target[1])) > 1e-6:
                raise ValueError("edge geometry endpoints disagree with directed nodes")
            for field in ("lengthKm", "travelTimeHours", "relativeExposure"):
                value = raw[field]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"invalid {field}")
            if payload.get("edgeId") != raw["edgeId"] or payload.get("contextVersion") != self.context_version:
                raise ValueError("feature identity/context mismatch")
            if payload.get("fromNodeId") != raw["fromNodeId"] or payload.get("toNodeId") != raw["toNodeId"]:
                raise ValueError("feature directed endpoints mismatch")
            return {**raw, "payload": payload, "points": points}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RoadDataError(f"invalid edge {raw.get('edgeId')}: {error}") from error

    def _assemble(self, edges: list[dict[str, Any]], start: int) -> RoadPath:
        nodes = [start]
        points: list[GeoPoint] = []
        refs: list[EdgeReference] = []
        flags: list[dict[str, Any]] = []
        for raw in edges:
            edge = self._checked_edge(raw)
            if edge["fromNodeId"] != nodes[-1]:
                raise RoadDataError("path edge sequence is discontinuous")
            nodes.append(edge["toNodeId"])
            refs.append(EdgeReference(edge["fromNodeId"], edge["toNodeId"], edge["edgeId"]))
            points.extend(edge["points"] if not points else edge["points"][1:])
            payload = edge["payload"]
            flags.append({key: payload.get(key) for key in (
                "edgeId", "contextVersion", "missingFlags", "fallbackUsed",
                "weatherFallbackUsed", "weatherRegionId", "weatherValidAt",
                "sourceType", "travelSourceType", "riskModelVersion",
            )})
        edge_ids = tuple(edge["edgeId"] for edge in edges)
        digest = hashlib.sha256(json.dumps(edge_ids, separators=(",", ":")).encode()).hexdigest()[:20]
        candidate = CandidatePath(
            path_id=f"m1-{digest}", nodes=tuple(nodes), edges=tuple(refs),
            geometry=tuple(points),
            distance=sum(edge["lengthKm"] * 1000 for edge in edges),
            travel_time=sum(edge["travelTimeHours"] * 3600 for edge in edges),
            risk_score=sum(edge["relativeExposure"] for edge in edges),
        )
        return RoadPath(candidate, edge_ids, tuple(flags))

    def _fast_witness(
        self, start: int, end: int, incoming_edge: str | None, *,
        max_road_states: int, max_road_labels: int,
        max_distance_m: float, max_travel_time_s: float, deadline: float,
    ) -> RoadSearchResult:
        """Find a first time-priority witness, never an absence proof.

        A slower/shorter label may be hidden at the same turn state, so an
        exhausted no-path result must be followed by the Pareto search.
        """

        initial = (start, incoming_edge)
        best = {initial: (0.0, 0.0)}
        parent: dict[tuple[int, str | None], tuple[tuple[int, str | None], dict[str, Any]]] = {}
        heap = [(0.0, 0.0, 0, initial)]
        serial = settled = dominated = resource_pruned = distance_pruned = time_pruned = 0
        generated = peak_active = 1

        def outcome(paths: tuple[RoadPath, ...] = (), limit: str | None = None) -> RoadSearchResult:
            return RoadSearchResult(paths, settled, limit, generated, dominated,
                                    resource_pruned, peak_active, distance_pruned, time_pruned)

        while heap:
            if monotonic() >= deadline:
                return outcome(limit="TIME_LIMIT")
            cost, distance, _, state = heapq.heappop(heap)
            if (cost, distance) != best[state]:
                continue
            settled += 1
            if settled > max_road_states:
                return outcome(limit="SEARCH_LIMIT")
            node, previous_edge = state
            if node == end and state != initial:
                chain = []
                cursor = state
                while cursor != initial:
                    cursor, edge = parent[cursor]
                    chain.append(edge)
                chain.reverse()
                return outcome((self._assemble(chain, start),), "OPTION_LIMIT")
            for raw in self._outgoing(node):
                if (previous_edge, raw["edgeId"]) in self.forbidden:
                    continue
                step, length = raw["travelTimeHours"], raw["lengthKm"]
                if any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or value < 0 for value in (step, length)):
                    raise RoadDataError(f"invalid time/distance on edge {raw['edgeId']}")
                new_cost, new_distance = cost + step * 3600, distance + length * 1000
                if new_cost > max_travel_time_s + 1e-9 or new_distance > max_distance_m + 1e-6:
                    resource_pruned += 1
                    if new_cost > max_travel_time_s + 1e-9:
                        time_pruned += 1
                    if new_distance > max_distance_m + 1e-6:
                        distance_pruned += 1
                    continue
                following = (raw["toNodeId"], raw["edgeId"])
                if (new_cost, new_distance) >= best.get(following, (math.inf, math.inf)):
                    dominated += 1
                    continue
                if generated >= max_road_labels:
                    return outcome(limit="LABEL_LIMIT")
                best[following] = (new_cost, new_distance)
                parent[following] = (state, raw)
                serial += 1
                generated += 1
                heapq.heappush(heap, (new_cost, new_distance, serial, following))
                peak_active = max(peak_active, len(heap))
            if monotonic() >= deadline:
                return outcome(limit="TIME_LIMIT")
        return outcome()

    def find_objective_paths(
        self,
        start: int,
        end: int,
        *,
        incoming_edge: str | None,
        objective: str,
        max_road_states: int,
        max_arrival_options: int,
        max_road_labels: int = 500_000,
        max_distance_m: float = math.inf,
        max_travel_time_s: float = math.inf,
        deadline: float = math.inf,
    ) -> RoadSearchResult:
        """Bounded three-resource labels ordered by time, distance, or exposure.

        Unlike ``find_paths`` this Step-3 seam keeps labels nondominated in all
        three physical coordinates.  Consequently a slower/longer but lower
        exposure option is not discarded by a time/distance-only frontier.
        The finite limits are telemetry, never an absence proof.
        """

        if objective not in {"time", "distance", "exposure"}:
            raise RoadDataError("objective must be time, distance, or exposure")
        if self.node(start) is None or self.node(end) is None:
            raise RoadDataError("origin or destination graph node is absent")
        if incoming_edge is not None:
            before = self.edge(incoming_edge)
            if before is None or before["toNodeId"] != start:
                raise RoadDataError("incoming edge is absent or does not end at origin")
        if (max_road_states <= 0 or max_arrival_options <= 0 or max_road_labels <= 0
                or math.isnan(max_distance_m) or max_distance_m < 0
                or math.isnan(max_travel_time_s) or max_travel_time_s < 0
                or math.isnan(deadline)):
            raise RoadDataError("invalid road search limits or resource budget")

        initial = (start, incoming_edge)
        # state,time,distance,exposure,parent,edge,active
        labels: list[list[Any]] = [[initial, 0.0, 0.0, 0.0, None, None, True]]
        frontiers: dict[tuple[int, str | None], list[int]] = {initial: [0]}
        coordinate = {"time": 1, "distance": 2, "exposure": 3}[objective]
        heap: list[tuple[float, float, float, float, int]] = [(0.0, 0.0, 0.0, 0.0, 0)]
        settled = dominated = resource_pruned = distance_pruned = time_pruned = 0
        active_count = peak_active = 1
        found: list[RoadPath] = []

        def outcome(limit: str | None = None) -> RoadSearchResult:
            return RoadSearchResult(tuple(found), settled, limit, len(labels), dominated,
                                    resource_pruned, peak_active, distance_pruned,
                                    time_pruned)

        while heap:
            if monotonic() >= deadline:
                return outcome("TIME_LIMIT")
            _, _, _, _, label_id = heapq.heappop(heap)
            label = labels[label_id]
            if not label[6]:
                continue
            settled += 1
            if settled > max_road_states:
                return outcome("SEARCH_LIMIT")
            node, previous_edge = label[0]
            if node == end and label_id:
                chain: list[dict[str, Any]] = []
                cursor = label_id
                while cursor:
                    parent_id, edge = labels[cursor][4], labels[cursor][5]
                    chain.append(edge)
                    cursor = parent_id
                chain.reverse()
                found.append(self._assemble(chain, start))
                if len(found) >= max_arrival_options:
                    return outcome("OPTION_LIMIT" if heap else None)
                continue
            for raw_unchecked in self._outgoing(node):
                edge_id = raw_unchecked["edgeId"]
                if (previous_edge, edge_id) in self.forbidden:
                    continue
                raw = self._checked_edge(raw_unchecked)
                next_time = label[1] + raw["travelTimeHours"] * 3600.0
                next_distance = label[2] + raw["lengthKm"] * 1000.0
                next_exposure = label[3] + raw["relativeExposure"]
                if (next_time > max_travel_time_s + 1e-9
                        or next_distance > max_distance_m + 1e-6):
                    resource_pruned += 1
                    time_pruned += int(next_time > max_travel_time_s + 1e-9)
                    distance_pruned += int(next_distance > max_distance_m + 1e-6)
                    continue
                following = (raw["toNodeId"], edge_id)
                values = (next_time, next_distance, next_exposure)
                existing = frontiers.get(following, [])
                if any(all(labels[old][axis] <= values[axis - 1] + 1e-12
                           for axis in (1, 2, 3)) for old in existing):
                    dominated += 1
                    continue
                if len(labels) >= max_road_labels:
                    return outcome("LABEL_LIMIT")
                survivors = []
                for old in existing:
                    if all(values[axis - 1] <= labels[old][axis] + 1e-12
                           for axis in (1, 2, 3)):
                        labels[old][6] = False
                        active_count -= 1
                        dominated += 1
                    else:
                        survivors.append(old)
                new_id = len(labels)
                labels.append([following, *values, label_id, raw_unchecked, True])
                survivors.append(new_id)
                frontiers[following] = survivors
                active_count += 1
                peak_active = max(peak_active, active_count)
                ordered = [values[coordinate - 1], values[0], values[1], values[2], new_id]
                heapq.heappush(heap, tuple(ordered))
            if monotonic() >= deadline:
                return outcome("TIME_LIMIT")
        return outcome()

    def find_paths(
        self,
        start: int,
        end: int,
        *,
        incoming_edge: str | None,
        max_road_states: int,
        max_arrival_options: int,
        max_road_labels: int = 500_000,
        max_distance_m: float = math.inf,
        max_travel_time_s: float = math.inf,
        deadline: float = math.inf,
    ) -> RoadSearchResult:
        """Bounded time/distance labels over (node,last edge).

        Earlier arrival can wait, so a label is safely discarded only when
        another label at the same turn state uses no more time *and* distance.
        Limits are unknown, never a proof of infeasibility. Exposure is
        accumulated for output but is neither a constraint nor a dominance
        coordinate in this S0 feasibility search.
        """

        if self.node(start) is None or self.node(end) is None:
            raise RoadDataError("origin or destination graph node is absent")
        if incoming_edge is not None:
            before = self.edge(incoming_edge)
            if before is None or before["toNodeId"] != start:
                raise RoadDataError("incoming edge is absent or does not end at origin")
        if (max_road_states <= 0 or max_arrival_options <= 0 or max_road_labels <= 0
            or math.isnan(max_distance_m) or max_distance_m < 0
            or math.isnan(max_travel_time_s) or max_travel_time_s < 0
            or math.isnan(deadline)):
            raise RoadDataError("invalid road search limits or resource budget")
        fast_result = None
        if max_arrival_options == 1:
            fast_result = self._fast_witness(
                start, end, incoming_edge, max_road_states=max_road_states,
                max_road_labels=max_road_labels, max_distance_m=max_distance_m,
                max_travel_time_s=max_travel_time_s, deadline=deadline)
            if fast_result.paths or fast_result.limit:
                return fast_result
        initial = (start, incoming_edge)
        # [state, time_seconds, distance_m, parent_label, edge, active]
        labels: list[list[Any]] = [[initial, 0.0, 0.0, None, None, True]]
        frontiers: dict[tuple[int, str | None], list[int]] = {initial: [0]}
        heap: list[tuple[float, float, int]] = [(0.0, 0.0, 0)]
        settled = 0
        dominated = resource_pruned = distance_pruned = time_pruned = 0
        active_count = peak_active = 1
        found: list[RoadPath] = []

        def outcome(limit: str | None = None) -> RoadSearchResult:
            prior = fast_result
            return RoadSearchResult(
                tuple(found), settled + (prior.settled_states if prior else 0),
                limit, len(labels) + (prior.generated_labels if prior else 0),
                dominated + (prior.dominated_prunes if prior else 0),
                resource_pruned + (prior.resource_prunes if prior else 0),
                max(peak_active, prior.peak_active_labels if prior else 0),
                distance_pruned + (prior.distance_prunes if prior else 0),
                time_pruned + (prior.time_prunes if prior else 0),
            )

        while heap:
            # Cooperative deadline: SQLite's individual query is not preempted.
            if monotonic() >= deadline:
                return outcome("TIME_LIMIT")
            cost, distance, label_id = heapq.heappop(heap)
            label = labels[label_id]
            if not label[5]:
                continue
            settled += 1
            if settled > max_road_states:
                return outcome("SEARCH_LIMIT")
            state = label[0]
            node, previous_edge = state
            if node == end and state != initial:
                chain: list[dict[str, Any]] = []
                cursor = label_id
                while cursor != 0:
                    _, _, _, parent_id, edge, _ = labels[cursor]
                    chain.append(edge)
                    cursor = parent_id
                chain.reverse()
                found.append(self._assemble(chain, start))
                if len(found) >= max_arrival_options:
                    return outcome("OPTION_LIMIT" if heap else None)
                continue
            for raw in self._outgoing(node):
                if (previous_edge, raw["edgeId"]) in self.forbidden:
                    continue
                step, length = raw["travelTimeHours"], raw["lengthKm"]
                if any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or value < 0 for value in (step, length)):
                    raise RoadDataError(f"invalid time/distance on edge {raw['edgeId']}")
                following = (raw["toNodeId"], raw["edgeId"])
                next_cost, next_distance = cost + step * 3600, distance + length * 1000
                if next_cost > max_travel_time_s + 1e-9 or next_distance > max_distance_m + 1e-6:
                    resource_pruned += 1
                    if next_cost > max_travel_time_s + 1e-9:
                        time_pruned += 1
                    if next_distance > max_distance_m + 1e-6:
                        distance_pruned += 1
                    continue
                existing = frontiers.get(following, [])
                if any(labels[old][1] <= next_cost and labels[old][2] <= next_distance
                       for old in existing):
                    dominated += 1
                    continue
                if len(labels) >= max_road_labels:
                    return outcome("LABEL_LIMIT")
                survivors = []
                for old in existing:
                    if next_cost <= labels[old][1] and next_distance <= labels[old][2]:
                        labels[old][5] = False
                        active_count -= 1
                        dominated += 1
                    else:
                        survivors.append(old)
                new_id = len(labels)
                labels.append([following, next_cost, next_distance, label_id, raw, True])
                survivors.append(new_id)
                frontiers[following] = survivors
                active_count += 1
                peak_active = max(peak_active, active_count)
                heapq.heappush(heap, (next_cost, next_distance, new_id))
            if monotonic() >= deadline:
                return outcome("TIME_LIMIT")
        return outcome()
