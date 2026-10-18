"""Receipt-backed M1 candidate-path and D/T/R/G foundation.

This module deliberately does not coerce the lazy M1 SQLite graph into the
legacy NetworkX candidate generator.  It searches only requested decision-node
pairs and keeps a versioned option set beside the frozen, one-selected-path
``MatrixBundle`` projection.  The matrix is a scalar lookup foundation; route
feasibility still requires the turn/resource-aware solver to carry the actual
incoming edge between legs.
"""

from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
from time import monotonic
from typing import Any, Iterable

from optimization.matrix import MatrixBuilder
from optimization.models import CandidatePath
from optimization.models.decision_state import DecisionState

from .member1_s0_graph import Member1RoadGraph, RoadDataError, RoadPath


PATH_OPTION_SCHEMA_VERSION = "member1-source-path-option/1"
FOUNDATION_SCHEMA_VERSION = "member1-source-dtrg-foundation/1"
FOUNDATION_VERSION = "member1-s1-s5-s6-path-foundation/2"
OBJECTIVES = ("time", "distance", "exposure")


@dataclass(frozen=True)
class CandidateFoundationLimits:
    """Explicit bounded-search limits; a cap is never an absence proof."""

    time_limit_seconds: float = 240.0
    max_states_per_search: int = 250_000
    max_labels_per_search: int = 500_000
    max_labels_per_turn_state: int = 4
    k_per_objective: int = 2
    max_secondary_states_per_search: int = 500
    max_secondary_labels_per_search: int = 1_000

    def __post_init__(self) -> None:
        for name in (
            "max_states_per_search",
            "max_labels_per_search",
            "max_labels_per_turn_state",
            "k_per_objective",
            "max_secondary_states_per_search",
            "max_secondary_labels_per_search",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            type(self.time_limit_seconds) not in (int, float)
            or not math.isfinite(self.time_limit_seconds)
            or self.time_limit_seconds <= 0
        ):
            raise ValueError("time_limit_seconds must be finite and positive")

    def to_dict(self) -> dict[str, int | float]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class ObjectiveSearchResult:
    paths: tuple[RoadPath, ...]
    objective: str
    settled_states: int
    generated_labels: int
    dominated_prunes: int
    cycle_prunes: int
    limit: str | None


@dataclass(frozen=True)
class MultiTargetObjectiveSearchResult:
    paths_by_destination: dict[int, tuple[RoadPath, ...]]
    objective: str
    settled_states: int
    generated_labels: int
    dominated_prunes: int
    cycle_prunes: int
    limit: str | None


def _diagnostic(code: str, message: str, *, severity: str = "ERROR", **context: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, "context": context}


def _objective_key(objective: str, time_s: float, distance_m: float,
                   exposure: float, edge_ids: tuple[str, ...]) -> tuple[Any, ...]:
    metrics = {
        "time": (time_s, distance_m, exposure),
        "distance": (distance_m, time_s, exposure),
        "exposure": (exposure, time_s, distance_m),
    }
    return (*metrics[objective], edge_ids)


def _great_circle_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_000.0 * 2 * math.asin(min(1.0, math.sqrt(value)))


def _search_time_astar(
    graph: Member1RoadGraph,
    origin: int,
    destination: int,
    *,
    incoming_edge: str | None,
    limits: CandidateFoundationLimits,
    deadline: float,
) -> ObjectiveSearchResult:
    """Find a bounded directed time witness with a geographic A* ordering.

    The result is a valid source path, not an optimality proof.  The speed
    lower-bound heuristic only orders exploration and every selected edge is
    still reconstructed and validated from raw SQLite.
    """

    start_point, end_point = graph.node(origin), graph.node(destination)
    if start_point is None or end_point is None:
        raise RoadDataError("origin or destination graph node is absent")
    if incoming_edge is not None:
        prior = graph.edge(incoming_edge)
        if prior is None or prior["toNodeId"] != origin:
            raise RoadDataError("incoming edge is absent or does not end at origin")
    seconds_per_meter = getattr(graph, "_foundation_seconds_per_meter", None)
    if seconds_per_meter is None:
        row = graph.db.execute(
            "SELECT MIN((f.travelTimeHours*3600.0)/(e.lengthKm*1000.0)) "
            "FROM edges e JOIN costs.features f USING(edgeId) "
            "WHERE e.lengthKm>0 AND f.travelTimeHours>=0"
        ).fetchone()
        seconds_per_meter = max(0.0, float(row[0] or 0.0))
        setattr(graph, "_foundation_seconds_per_meter", seconds_per_meter)
    node_cache: dict[int, tuple[float, float]] = {origin: start_point, destination: end_point}

    def heuristic(node: int) -> float:
        point = node_cache.get(node)
        if point is None:
            point = graph.node(node)
            if point is None:
                raise RoadDataError(f"path references absent node {node}")
            node_cache[node] = point
        return _great_circle_m(point, end_point) * seconds_per_meter

    initial = (origin, incoming_edge)
    best: dict[tuple[int, str | None], tuple[float, float]] = {initial: (0.0, 0.0)}
    parent: dict[tuple[int, str | None], tuple[tuple[int, str | None], dict[str, Any]]] = {}
    heap: list[tuple[float, float, float, int, tuple[int, str | None]]] = [
        (heuristic(origin), 0.0, 0.0, 0, initial)
    ]
    serial = settled = generated = dominated = 0
    while heap:
        if monotonic() >= deadline:
            return ObjectiveSearchResult((), "time", settled, generated + 1, dominated, 0, "TIME_LIMIT")
        _, travel, distance, _, state = heapq.heappop(heap)
        if (travel, distance) != best.get(state):
            continue
        settled += 1
        if settled > limits.max_states_per_search:
            return ObjectiveSearchResult((), "time", settled, generated + 1, dominated, 0, "SEARCH_LIMIT")
        node, previous_edge = state
        if node == destination and state != initial:
            chain: list[dict[str, Any]] = []
            cursor = state
            while cursor != initial:
                cursor, edge = parent[cursor]
                chain.append(edge)
            chain.reverse()
            return ObjectiveSearchResult(
                (graph._assemble(chain, origin),), "time", settled,
                generated + 1, dominated, 0, "OPTION_LIMIT",
            )
        for raw in graph._outgoing(node):
            if (previous_edge, raw.get("edgeId")) in graph.forbidden:
                continue
            step, length = raw.get("travelTimeHours"), raw.get("lengthKm")
            if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                   for value in (step, length)):
                raise RoadDataError(f"invalid time/distance on edge {raw.get('edgeId')}")
            next_travel = travel + step * 3600.0
            next_distance = distance + length * 1000.0
            following = (raw["toNodeId"], raw["edgeId"])
            if (next_travel, next_distance) >= best.get(following, (math.inf, math.inf)):
                dominated += 1
                continue
            if generated >= limits.max_labels_per_search:
                return ObjectiveSearchResult((), "time", settled, generated + 1, dominated, 0, "LABEL_LIMIT")
            best[following] = (next_travel, next_distance)
            parent[following] = (state, raw)
            serial += 1
            generated += 1
            heapq.heappush(
                heap,
                (next_travel + heuristic(raw["toNodeId"]), next_travel,
                 next_distance, serial, following),
            )
    return ObjectiveSearchResult((), "time", settled, generated + 1, dominated, 0, None)


def _search_objective(
    graph: Member1RoadGraph,
    origin: int,
    destination: int,
    *,
    objective: str,
    incoming_edge: str | None,
    limits: CandidateFoundationLimits,
    deadline: float,
) -> ObjectiveSearchResult:
    """Bounded loopless scalar search over explicit directed edge identities.

    A small number of labels is retained for each ``(node, incoming_edge)``.
    Separate searches for time, distance, and raw exposure prevent an option
    from disappearing merely because it is worse in the other two domains.
    """

    if objective not in OBJECTIVES:
        raise ValueError(f"unsupported path objective: {objective}")
    if graph.node(origin) is None or graph.node(destination) is None:
        raise RoadDataError("origin or destination graph node is absent")
    if incoming_edge is not None:
        before = graph.edge(incoming_edge)
        if before is None or before["toNodeId"] != origin:
            raise RoadDataError("incoming edge is absent or does not end at origin")

    # node, last edge, time, distance, exposure, parent, raw edge, nodes, edges, active
    labels: list[list[Any]] = [[
        origin, incoming_edge, 0.0, 0.0, 0.0, None, None,
        (origin,), tuple(), True,
    ]]
    frontier: dict[tuple[int, str | None], list[int]] = {(origin, incoming_edge): [0]}
    heap: list[tuple[tuple[Any, ...], int, int]] = [
        (_objective_key(objective, 0.0, 0.0, 0.0, ()), 0, 0)
    ]
    serial = 0
    settled = dominated = cycles = 0
    found: list[RoadPath] = []

    def finish(limit: str | None = None) -> ObjectiveSearchResult:
        return ObjectiveSearchResult(
            tuple(found), objective, settled, len(labels), dominated, cycles, limit
        )

    while heap:
        if monotonic() >= deadline:
            return finish("TIME_LIMIT")
        _, _, label_id = heapq.heappop(heap)
        label = labels[label_id]
        if not label[9]:
            continue
        settled += 1
        if settled > limits.max_states_per_search:
            return finish("SEARCH_LIMIT")
        node, last_edge = label[0], label[1]
        if node == destination and label_id != 0:
            chain: list[dict[str, Any]] = []
            cursor = label_id
            while cursor:
                current = labels[cursor]
                chain.append(current[6])
                cursor = current[5]
            chain.reverse()
            found.append(graph._assemble(chain, origin))  # same checked source-edge assembly
            if len(found) >= limits.k_per_objective:
                return finish("OPTION_LIMIT" if heap else None)
            continue

        for raw in graph._outgoing(node):
            edge_id = raw.get("edgeId")
            if (last_edge, edge_id) in graph.forbidden:
                continue
            if raw.get("toNodeId") in label[7]:
                cycles += 1
                continue
            checked = graph._checked_edge(raw)
            time_s = label[2] + checked["travelTimeHours"] * 3600.0
            distance_m = label[3] + checked["lengthKm"] * 1000.0
            exposure = label[4] + checked["relativeExposure"]
            edge_ids = (*label[8], checked["edgeId"])
            following = (checked["toNodeId"], checked["edgeId"])
            key = _objective_key(objective, time_s, distance_m, exposure, edge_ids)
            existing = [old for old in frontier.get(following, ()) if labels[old][9]]
            ranked = sorted(
                ((*_objective_key(objective, labels[old][2], labels[old][3],
                                  labels[old][4], labels[old][8]), old)
                 for old in existing),
            )
            if len(ranked) >= limits.max_labels_per_turn_state and key >= ranked[-1][:-1]:
                dominated += 1
                continue
            if len(labels) >= limits.max_labels_per_search:
                return finish("LABEL_LIMIT")
            new_id = len(labels)
            labels.append([
                checked["toNodeId"], checked["edgeId"], time_s, distance_m,
                exposure, label_id, raw, (*label[7], checked["toNodeId"]),
                edge_ids, True,
            ])
            existing.append(new_id)
            existing.sort(key=lambda old: _objective_key(
                objective, labels[old][2], labels[old][3], labels[old][4], labels[old][8]
            ))
            if len(existing) > limits.max_labels_per_turn_state:
                removed = existing.pop()
                labels[removed][9] = False
                dominated += 1
                if removed == new_id:
                    continue
            frontier[following] = existing
            serial += 1
            heapq.heappush(heap, (key, serial, new_id))
    return finish()


def _search_objective_multi(
    graph: Member1RoadGraph,
    origin: int,
    destinations: tuple[int, ...],
    *,
    objective: str,
    limits: CandidateFoundationLimits,
    deadline: float,
) -> MultiTargetObjectiveSearchResult:
    """One bounded scalar search from an origin to every decision target."""

    if objective not in OBJECTIVES:
        raise ValueError(f"unsupported path objective: {objective}")
    if graph.node(origin) is None or any(graph.node(node) is None for node in destinations):
        raise RoadDataError("origin or destination graph node is absent")
    targets = frozenset(destinations)
    found: dict[int, list[RoadPath]] = {node: [] for node in destinations}
    # Same label layout as _search_objective, with no initial incoming edge.
    labels: list[list[Any]] = [[origin, None, 0.0, 0.0, 0.0, None, None,
                               (origin,), tuple(), True]]
    frontier: dict[tuple[int, str | None], list[int]] = {(origin, None): [0]}
    heap: list[tuple[tuple[Any, ...], int, int]] = [
        (_objective_key(objective, 0.0, 0.0, 0.0, ()), 0, 0)
    ]
    serial = settled = dominated = cycles = 0

    def finish(limit: str | None = None) -> MultiTargetObjectiveSearchResult:
        return MultiTargetObjectiveSearchResult(
            {node: tuple(paths) for node, paths in found.items()}, objective,
            settled, len(labels), dominated, cycles, limit,
        )

    while heap:
        if monotonic() >= deadline:
            return finish("TIME_LIMIT")
        _, _, label_id = heapq.heappop(heap)
        label = labels[label_id]
        if not label[9]:
            continue
        settled += 1
        if settled > limits.max_states_per_search:
            return finish("SEARCH_LIMIT")
        node, last_edge = label[0], label[1]
        if node in targets and len(found[node]) < limits.k_per_objective:
            chain: list[dict[str, Any]] = []
            cursor = label_id
            while cursor:
                current = labels[cursor]
                chain.append(current[6])
                cursor = current[5]
            chain.reverse()
            found[node].append(graph._assemble(chain, origin))
            if all(len(paths) >= limits.k_per_objective for paths in found.values()):
                return finish("OPTION_LIMIT" if heap else None)
        # A decision node is not an artificial barrier; another target route
        # can validly pass through it, so search continues from this label.
        for raw in graph._outgoing(node):
            edge_id = raw.get("edgeId")
            if (last_edge, edge_id) in graph.forbidden:
                continue
            if raw.get("toNodeId") in label[7]:
                cycles += 1
                continue
            checked = graph._checked_edge(raw)
            time_s = label[2] + checked["travelTimeHours"] * 3600.0
            distance_m = label[3] + checked["lengthKm"] * 1000.0
            exposure = label[4] + checked["relativeExposure"]
            edge_ids = (*label[8], checked["edgeId"])
            following = (checked["toNodeId"], checked["edgeId"])
            key = _objective_key(objective, time_s, distance_m, exposure, edge_ids)
            existing = [old for old in frontier.get(following, ()) if labels[old][9]]
            ranked = sorted(
                ((*_objective_key(objective, labels[old][2], labels[old][3],
                                  labels[old][4], labels[old][8]), old)
                 for old in existing),
            )
            if len(ranked) >= limits.max_labels_per_turn_state and key >= ranked[-1][:-1]:
                dominated += 1
                continue
            if len(labels) >= limits.max_labels_per_search:
                return finish("LABEL_LIMIT")
            new_id = len(labels)
            labels.append([
                checked["toNodeId"], checked["edgeId"], time_s, distance_m,
                exposure, label_id, raw, (*label[7], checked["toNodeId"]),
                edge_ids, True,
            ])
            existing.append(new_id)
            existing.sort(key=lambda old: _objective_key(
                objective, labels[old][2], labels[old][3], labels[old][4], labels[old][8]
            ))
            if len(existing) > limits.max_labels_per_turn_state:
                removed = existing.pop()
                labels[removed][9] = False
                dominated += 1
                if removed == new_id:
                    continue
            frontier[following] = existing
            serial += 1
            heapq.heappush(heap, (key, serial, new_id))
    return finish()


def find_source_candidate_options(
    graph: Member1RoadGraph,
    origin: int,
    destination: int,
    *,
    incoming_edge: str | None = None,
    limits: CandidateFoundationLimits | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Return a versioned union of time/distance/exposure candidate paths."""

    limits = limits or CandidateFoundationLimits()
    absolute_deadline = deadline if deadline is not None else monotonic() + limits.time_limit_seconds
    by_edges: dict[tuple[str, ...], dict[str, Any]] = {}
    searches: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for objective in OBJECTIVES:
        try:
            if objective == "time":
                if limits.k_per_objective == 1:
                    result = _search_time_astar(
                        graph, origin, destination, incoming_edge=incoming_edge,
                        limits=limits, deadline=absolute_deadline,
                    )
                else:
                    source = graph.find_paths(
                        origin, destination, incoming_edge=incoming_edge,
                        max_road_states=limits.max_states_per_search,
                        max_road_labels=limits.max_labels_per_search,
                        max_arrival_options=limits.k_per_objective,
                        deadline=absolute_deadline,
                    )
                    result = ObjectiveSearchResult(
                        source.paths, objective, source.settled_states,
                        source.generated_labels, source.dominated_prunes, 0,
                        source.limit,
                    )
            else:
                secondary_limits = CandidateFoundationLimits(
                    time_limit_seconds=limits.time_limit_seconds,
                    max_states_per_search=limits.max_secondary_states_per_search,
                    max_labels_per_search=limits.max_secondary_labels_per_search,
                    max_labels_per_turn_state=limits.max_labels_per_turn_state,
                    k_per_objective=limits.k_per_objective,
                    max_secondary_states_per_search=limits.max_secondary_states_per_search,
                    max_secondary_labels_per_search=limits.max_secondary_labels_per_search,
                )
                result = _search_objective(
                    graph, origin, destination, objective=objective,
                    incoming_edge=incoming_edge, limits=secondary_limits,
                    deadline=absolute_deadline,
                )
        except (RoadDataError, KeyError, TypeError, ValueError) as error:
            diagnostics.append(_diagnostic(
                "PATH_SOURCE_INVALID", str(error), objective=objective,
                origin=origin, destination=destination,
            ))
            continue
        searches.append({
            "objective": objective,
            "settled_states": result.settled_states,
            "generated_labels": result.generated_labels,
            "dominated_prunes": result.dominated_prunes,
            "cycle_prunes": result.cycle_prunes,
            "limit": result.limit,
            "path_count": len(result.paths),
        })
        if result.limit:
            diagnostics.append(_diagnostic(
                "PATH_SEARCH_TRUNCATED",
                "Candidate coverage is bounded and is not an exhaustive path listing.",
                severity="WARNING", objective=objective, limit=result.limit,
                origin=origin, destination=destination,
            ))
        for path in result.paths:
            item = by_edges.setdefault(path.edge_ids, {
                "schema_version": PATH_OPTION_SCHEMA_VERSION,
                "origin_node": origin,
                "destination_node": destination,
                "incoming_edge": incoming_edge,
                "final_edge": path.final_edge,
                "edge_ids": list(path.edge_ids),
                "selected_by": [],
                "candidate_path": path.candidate.to_dict(),
                "feature_provenance": list(path.feature_provenance),
            })
            item["selected_by"].append(objective)
    options = sorted(
        by_edges.values(),
        key=lambda item: (item["candidate_path"]["travel_time"],
                          item["candidate_path"]["distance"],
                          item["candidate_path"]["risk_score"], tuple(item["edge_ids"])),
    )
    for item in options:
        item["selected_by"].sort(key=OBJECTIVES.index)
    truncated = any(row.get("limit") for row in searches)
    source_invalid = any(item.get("severity") == "ERROR" for item in diagnostics)
    if options:
        if source_invalid:
            status = "SOURCE_INVALID_WITH_CANDIDATE"
        else:
            status = "TRUNCATED_WITH_CANDIDATE" if truncated else "CANDIDATES_FOUND"
    else:
        status = "TRUNCATED_NO_CANDIDATE" if truncated else "NO_CANDIDATE_FOUND"
        diagnostics.append(_diagnostic(
            "MISSING_REQUIRED_PAIR_CANDIDATE",
            "No source-backed candidate was found; no matrix value may be invented.",
            origin=origin, destination=destination, truncated=truncated,
        ))
    return {
        "schema_version": PATH_OPTION_SCHEMA_VERSION,
        "origin_node": origin,
        "destination_node": destination,
        "incoming_edge": incoming_edge,
        "coverage_status": status,
        "coverage_complete": not truncated and not source_invalid,
        "options": options,
        "searches": searches,
        "diagnostics": diagnostics,
    }


def find_time_source_witness(
    graph: Member1RoadGraph,
    origin: int,
    destination: int,
    *,
    incoming_edge: str | None = None,
    limits: CandidateFoundationLimits | None = None,
    deadline: float | None = None,
) -> ObjectiveSearchResult:
    """Public additive seam for a bounded raw-SQLite time witness.

    The OR-Tools bridge needs one authenticated proposal arc per physical
    pair, not the distance/exposure candidate union.  This retains the same A*
    implementation and source validation as the frozen foundation without
    relabelling a time candidate as another objective.
    """

    limits = limits or CandidateFoundationLimits()
    return _search_time_astar(
        graph, origin, destination, incoming_edge=incoming_edge,
        limits=limits,
        deadline=deadline if deadline is not None
        else monotonic() + limits.time_limit_seconds,
    )


def build_source_dtrg_foundation(
    state: DecisionState,
    graph: Member1RoadGraph,
    *,
    limits: CandidateFoundationLimits | None = None,
    decision_nodes: Iterable[int] | None = None,
) -> dict[str, Any]:
    """Build pair options and a compatible MatrixBundle projection.

    Matrix selection remains deterministic time-first.  The complete option
    records remain adjacent and are the only artifact that exposes alternate
    directed edges/objective provenance.
    """

    limits = limits or CandidateFoundationLimits()
    nodes = tuple(sorted(set(decision_nodes or (
        state.depot.graph_node_id,
        *(order.graph_node_id for order in state.orders),
    ))))
    deadline = monotonic() + limits.time_limit_seconds
    pairs: list[dict[str, Any]] = []
    candidates: dict[str, CandidatePath] = {}
    diagnostics: list[dict[str, Any]] = []
    required_missing = False
    for origin in nodes:
        for destination in nodes:
            if origin == destination:
                continue
            pair = find_source_candidate_options(
                graph, origin, destination, limits=limits, deadline=deadline,
            )
            pairs.append(pair)
            diagnostics.extend(pair["diagnostics"])
            if not pair["options"]:
                required_missing = True
            for item in pair["options"]:
                candidate = CandidatePath.from_dict(item["candidate_path"])
                candidates[candidate.path_id] = candidate

    matrix = None
    matrix_diagnostics: list[dict[str, Any]] = []
    if not required_missing:
        builder = MatrixBuilder()
        matrix = builder.build(
            candidates.values(),
            matrix_version=f"m1-{state.scenario_id.lower()}-dtrg/1",
            node_order=nodes,
            source={
                "foundation_schema_version": FOUNDATION_SCHEMA_VERSION,
                "foundation_version": FOUNDATION_VERSION,
                "scenario_id": state.scenario_id,
                "fixture_raw_sha256": state.fixture_raw_sha256,
                "routing_version": state.routing_version,
                "features_version": state.features_version,
                "context_version": state.context_version,
                "source_hashes": dict(graph.source_hashes),
                "matrix_role": "single_selected_path_projection_not_route_feasibility_proof",
            },
        )
        matrix_diagnostics = [item.to_dict() for item in builder.diagnostics]
        if matrix is None:
            required_missing = True
    diagnostics.extend(matrix_diagnostics)
    source_errors = [
        item for item in diagnostics if item.get("severity") == "ERROR"
    ]
    truncated_pairs = [
        {"origin": pair["origin_node"], "destination": pair["destination_node"],
         "status": pair["coverage_status"]}
        for pair in pairs if not pair["coverage_complete"]
    ]
    missing_pairs = [
        {"origin": pair["origin_node"], "destination": pair["destination_node"]}
        for pair in pairs if not pair["options"]
    ]
    if matrix is None or missing_pairs or source_errors:
        gate = "BLOCKED"
    elif truncated_pairs:
        gate = "PASS_WITH_DECLARED_TRUNCATION"
    else:
        gate = "PASS"
    return {
        "schema_version": FOUNDATION_SCHEMA_VERSION,
        "foundation_version": FOUNDATION_VERSION,
        "scenario_id": state.scenario_id,
        "decision_epoch": state.decision_epoch,
        "node_order": list(nodes),
        "unit_metadata": {
            "distance": "meter", "travel_time": "second",
            "risk_semantic": "relative_exposure_proxy_raw_sum",
            "geometry": "GeoJSON longitude_latitude WGS84",
        },
        "source": {
            "authentication": state.source_authentication,
            "fixture_raw_sha256": state.fixture_raw_sha256,
            "receipt_sha256": state.receipt_sha256,
            "routing_version": state.routing_version,
            "features_version": state.features_version,
            "context_version": state.context_version,
            "delivery_area_version": state.delivery_area_version,
            "source_hashes": dict(graph.source_hashes),
        },
        "limits": limits.to_dict(),
        "pair_coverage": pairs,
        "truncated_pairs": truncated_pairs,
        "missing_pairs": missing_pairs,
        "matrix_bundle": matrix.to_dict() if matrix is not None else None,
        "gate": gate,
        "diagnostics": diagnostics,
        "scope_note": (
            "Finite objective-K options are not every feasible path; incoming-edge/resource "
            "route semantics remain the solver's responsibility."
        ),
    }


__all__ = [
    "CandidateFoundationLimits",
    "FOUNDATION_SCHEMA_VERSION",
    "FOUNDATION_VERSION",
    "OBJECTIVES",
    "PATH_OPTION_SCHEMA_VERSION",
    "build_source_dtrg_foundation",
    "find_source_candidate_options",
    "find_time_source_witness",
]
