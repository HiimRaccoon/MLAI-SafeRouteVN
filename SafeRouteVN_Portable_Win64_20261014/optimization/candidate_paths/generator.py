"""Dependency-light K-shortest candidate path generation.

The generator consumes a NetworkX-compatible directed graph through duck
typing. It does not import NetworkX, which keeps Phase B usable before the
integrated dependency manifest exists. Directed multigraph edge keys are
preserved in every returned :class:`CandidatePath`.
"""

from __future__ import annotations

import heapq
import itertools
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, MutableSequence, Sequence

from optimization.models import (
    CandidatePath,
    Diagnostic,
    DiagnosticSeverity,
    EdgeReference,
    GeoPoint,
)
from optimization.models.common import (
    ContractValidationError,
    NodeId,
    validate_finite_number,
)


LOGGER = logging.getLogger(__name__)

DISTANCE_ATTRIBUTES = ("distance", "length")
TRAVEL_TIME_ATTRIBUTES = ("travel_time", "base_travel_time", "time")
RISK_ATTRIBUTES = ("risk_score", "relative_exposure", "edge_proxy", "risk")


@dataclass(frozen=True, slots=True)
class _TraversalEdge:
    reference: EdgeReference
    distance: float
    travel_time: float
    risk_score: float
    search_weight: float
    geometry: tuple[GeoPoint, ...]


class _RoadGraphAdapter:
    """Small adapter for a NetworkX-like TASK-01 RoadGraph."""

    def __init__(self, graph: Any) -> None:
        self.graph = graph

    def is_directed(self) -> bool:
        method = getattr(self.graph, "is_directed", None)
        if callable(method):
            return bool(method())
        directed = getattr(self.graph, "directed", True)
        return bool(directed)

    def is_multigraph(self) -> bool:
        method = getattr(self.graph, "is_multigraph", None)
        if callable(method):
            return bool(method())
        return bool(getattr(self.graph, "multigraph", False))

    def has_node(self, node: NodeId) -> bool:
        try:
            return bool(node in self.graph)
        except (TypeError, AttributeError):
            nodes = getattr(self.graph, "nodes", ())
            try:
                return bool(node in nodes)
            except TypeError:
                return False

    def node_data(self, node: NodeId) -> Mapping[str, Any]:
        nodes = getattr(self.graph, "nodes", None)
        if nodes is not None:
            try:
                data = nodes[node]
                if isinstance(data, Mapping):
                    return data
            except (KeyError, TypeError):
                pass
        method = getattr(self.graph, "get_node_data", None)
        if callable(method):
            data = method(node)
            if isinstance(data, Mapping):
                return data
        return {}

    def successors(self, node: NodeId) -> Iterable[NodeId]:
        method = getattr(self.graph, "successors", None)
        if callable(method):
            return method(node)
        adjacency = getattr(self.graph, "adj", None)
        if adjacency is not None:
            return adjacency[node].keys()
        try:
            return self.graph[node].keys()
        except (KeyError, TypeError, AttributeError) as error:
            raise ContractValidationError(
                "RoadGraph must expose successors(node) or adjacency data"
            ) from error

    def edge_data(self, u: NodeId, v: NodeId) -> Mapping[Any, Any]:
        method = getattr(self.graph, "get_edge_data", None)
        if callable(method):
            data = method(u, v)
        else:
            adjacency = getattr(self.graph, "adj", self.graph)
            data = adjacency[u][v]
        if not isinstance(data, Mapping):
            raise ContractValidationError(f"edge data for {u!r}->{v!r} must be a mapping")
        return data

    def outgoing(self, node: NodeId) -> tuple[tuple[NodeId, NodeId, Mapping[str, Any]], ...]:
        outgoing: list[tuple[NodeId, NodeId, Mapping[str, Any]]] = []
        for neighbor in self.successors(node):
            edge_data = self.edge_data(node, neighbor)
            if self.is_multigraph():
                for key, attributes in edge_data.items():
                    if not isinstance(attributes, Mapping):
                        raise ContractValidationError(
                            f"attributes for edge {node!r}->{neighbor!r}/{key!r} "
                            "must be a mapping"
                        )
                    outgoing.append((neighbor, key, attributes))
            else:
                key = edge_data.get("key", 0)
                outgoing.append((neighbor, key, edge_data))
        outgoing.sort(key=lambda item: (repr(item[0]), repr(item[1])))
        return tuple(outgoing)


class CandidatePathGenerator:
    """Generate top-K loopless directed paths with structured diagnostics."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or LOGGER
        self._diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        """Diagnostics from the most recent generation call."""

        return self._diagnostics

    def generate(
        self,
        graph: Any,
        origin: NodeId,
        destination: NodeId,
        *,
        k: int = 3,
        weight_attribute: str = "travel_time",
        max_expansions: int = 100_000,
    ) -> list[CandidatePath]:
        """Return up to ``k`` unique directed CandidatePath objects.

        Paths are enumerated in non-decreasing search-weight order. The search
        is loopless, preserving the exact multigraph edge key selected for each
        hop. Paths with an identical node sequence or edge sequence are treated
        as duplicates; because enumeration is weight ordered, the retained path
        is the best representative for the requested weight.

        Invalid and disconnected inputs return an empty list. Structured
        diagnostics are available through :attr:`diagnostics`.
        """

        diagnostics: list[Diagnostic] = []
        self._logger.info(
            "Candidate path generation started: origin=%r destination=%r k=%r weight=%s",
            origin,
            destination,
            k,
            weight_attribute,
        )
        diagnostics.append(
            self._diagnostic(
                "GENERATION_STARTED",
                DiagnosticSeverity.INFO,
                "Candidate path generation started.",
                {
                    "origin": origin,
                    "destination": destination,
                    "requested_k": k,
                    "weight_attribute": weight_attribute,
                },
            )
        )

        adapter = _RoadGraphAdapter(graph)
        invalid = False
        if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
            invalid = True
            diagnostics.append(
                self._diagnostic(
                    "INVALID_K",
                    DiagnosticSeverity.ERROR,
                    "k must be a positive integer.",
                    {"value": repr(k)},
                )
            )
        if (
            isinstance(max_expansions, bool)
            or not isinstance(max_expansions, int)
            or max_expansions <= 0
        ):
            invalid = True
            diagnostics.append(
                self._diagnostic(
                    "INVALID_MAX_EXPANSIONS",
                    DiagnosticSeverity.ERROR,
                    "max_expansions must be a positive integer.",
                    {"value": repr(max_expansions)},
                )
            )
        if not isinstance(weight_attribute, str) or not weight_attribute.strip():
            invalid = True
            diagnostics.append(
                self._diagnostic(
                    "INVALID_WEIGHT_ATTRIBUTE",
                    DiagnosticSeverity.ERROR,
                    "weight_attribute must be a non-empty string.",
                    {"value": repr(weight_attribute)},
                )
            )
        if not adapter.is_directed():
            invalid = True
            diagnostics.append(
                self._diagnostic(
                    "INVALID_GRAPH",
                    DiagnosticSeverity.ERROR,
                    "RoadGraph must be directed.",
                    {},
                )
            )
        if not adapter.has_node(origin):
            invalid = True
            diagnostics.append(
                self._diagnostic(
                    "MISSING_ORIGIN",
                    DiagnosticSeverity.ERROR,
                    "Origin node does not exist in RoadGraph.",
                    {"origin": origin},
                )
            )
        if not adapter.has_node(destination):
            invalid = True
            diagnostics.append(
                self._diagnostic(
                    "MISSING_DESTINATION",
                    DiagnosticSeverity.ERROR,
                    "Destination node does not exist in RoadGraph.",
                    {"destination": destination},
                )
            )
        if invalid:
            self._logger.warning(
                "Candidate path generation rejected invalid input: origin=%r destination=%r",
                origin,
                destination,
            )
            self._diagnostics = tuple(diagnostics)
            return []

        if origin == destination:
            point = self._node_point(adapter, origin)
            if point is None:
                diagnostics.append(
                    self._diagnostic(
                        "MISSING_GEOMETRY",
                        DiagnosticSeverity.ERROR,
                        "Origin/destination node has no usable coordinates.",
                        {"node": origin},
                    )
                )
                self._diagnostics = tuple(diagnostics)
                return []
            candidate = CandidatePath(
                path_id="path_001",
                nodes=(origin,),
                edges=(),
                geometry=(point,),
                distance=0.0,
                travel_time=0.0,
                risk_score=0.0,
            )
            diagnostics.append(self._generated_diagnostic(1, k, 0, 0))
            self._diagnostics = tuple(diagnostics)
            self._logger.info("Candidate path generation completed: generated=1 duplicates=0")
            return [candidate]

        counter = itertools.count()
        frontier: list[
            tuple[
                float,
                int,
                int,
                NodeId,
                tuple[NodeId, ...],
                tuple[_TraversalEdge, ...],
            ]
        ] = [(0.0, 0, next(counter), origin, (origin,), ())]
        candidates: list[CandidatePath] = []
        seen_node_sequences: set[tuple[NodeId, ...]] = set()
        seen_edge_sequences: set[tuple[tuple[NodeId, NodeId, NodeId], ...]] = set()
        invalid_edges_reported: set[tuple[NodeId, NodeId, NodeId, str]] = set()
        duplicates_removed = 0
        expansions = 0
        limit_reached = False

        while frontier and len(candidates) < k:
            total_weight, _, _, current, nodes, traversal_edges = heapq.heappop(frontier)
            if current == destination:
                edge_signature = tuple(
                    (edge.reference.u, edge.reference.v, edge.reference.key)
                    for edge in traversal_edges
                )
                if nodes in seen_node_sequences or edge_signature in seen_edge_sequences:
                    duplicates_removed += 1
                    continue
                candidate = self._to_candidate(
                    index=len(candidates) + 1,
                    nodes=nodes,
                    edges=traversal_edges,
                )
                candidates.append(candidate)
                seen_node_sequences.add(nodes)
                seen_edge_sequences.add(edge_signature)
                continue

            if expansions >= max_expansions:
                limit_reached = True
                break
            expansions += 1

            try:
                outgoing = adapter.outgoing(current)
            except (ContractValidationError, KeyError, TypeError, AttributeError) as error:
                diagnostics.append(
                    self._diagnostic(
                        "INVALID_GRAPH",
                        DiagnosticSeverity.ERROR,
                        "RoadGraph adjacency could not be read.",
                        {"node": current, "reason": str(error)},
                    )
                )
                self._logger.warning("Invalid RoadGraph adjacency at %r: %s", current, error)
                self._diagnostics = tuple(diagnostics)
                return []

            for neighbor, edge_key, attributes in outgoing:
                if neighbor in nodes:
                    continue
                try:
                    edge = self._build_traversal_edge(
                        adapter,
                        current,
                        neighbor,
                        edge_key,
                        attributes,
                        weight_attribute,
                    )
                except ContractValidationError as error:
                    diagnostic_key = (current, neighbor, edge_key, str(error))
                    if diagnostic_key not in invalid_edges_reported:
                        invalid_edges_reported.add(diagnostic_key)
                        diagnostics.append(
                            self._diagnostic(
                                "INVALID_EDGE",
                                DiagnosticSeverity.WARNING,
                                "An edge was excluded because required attributes were invalid.",
                                {
                                    "u": current,
                                    "v": neighbor,
                                    "key": edge_key,
                                    "reason": str(error),
                                },
                            )
                        )
                        self._logger.warning(
                            "Excluded invalid edge %r->%r/%r: %s",
                            current,
                            neighbor,
                            edge_key,
                            error,
                        )
                    continue
                next_edges = traversal_edges + (edge,)
                heapq.heappush(
                    frontier,
                    (
                        total_weight + edge.search_weight,
                        len(next_edges),
                        next(counter),
                        neighbor,
                        nodes + (neighbor,),
                        next_edges,
                    ),
                )

        if limit_reached:
            diagnostics.append(
                self._diagnostic(
                    "SEARCH_LIMIT_REACHED",
                    DiagnosticSeverity.WARNING,
                    "Candidate search stopped at max_expansions.",
                    {"max_expansions": max_expansions, "generated": len(candidates)},
                )
            )
            self._logger.warning(
                "Candidate path search limit reached: max_expansions=%d generated=%d",
                max_expansions,
                len(candidates),
            )
        if duplicates_removed:
            diagnostics.append(
                self._diagnostic(
                    "DUPLICATES_REMOVED",
                    DiagnosticSeverity.INFO,
                    "Duplicate path candidates were removed.",
                    {"count": duplicates_removed},
                )
            )
            self._logger.info("Candidate path duplicates removed: count=%d", duplicates_removed)
        if not candidates:
            diagnostics.append(
                self._diagnostic(
                    "NO_PATH",
                    DiagnosticSeverity.WARNING,
                    "No feasible directed path exists between origin and destination.",
                    {"origin": origin, "destination": destination},
                )
            )
            self._logger.warning(
                "No directed candidate path found: origin=%r destination=%r",
                origin,
                destination,
            )

        diagnostics.append(
            self._generated_diagnostic(len(candidates), k, duplicates_removed, expansions)
        )
        self._diagnostics = tuple(diagnostics)
        self._logger.info(
            "Candidate path generation completed: generated=%d duplicates=%d expansions=%d",
            len(candidates),
            duplicates_removed,
            expansions,
        )
        return candidates

    def _build_traversal_edge(
        self,
        adapter: _RoadGraphAdapter,
        u: NodeId,
        v: NodeId,
        key: NodeId,
        attributes: Mapping[str, Any],
        weight_attribute: str,
    ) -> _TraversalEdge:
        distance = self._numeric_attribute(attributes, DISTANCE_ATTRIBUTES, "distance")
        travel_time = self._numeric_attribute(
            attributes, TRAVEL_TIME_ATTRIBUTES, "travel_time"
        )
        risk_score = self._numeric_attribute(attributes, RISK_ATTRIBUTES, "risk_score")
        if weight_attribute in TRAVEL_TIME_ATTRIBUTES:
            search_weight = travel_time
        elif weight_attribute in DISTANCE_ATTRIBUTES:
            search_weight = distance
        elif weight_attribute in RISK_ATTRIBUTES:
            search_weight = risk_score
        else:
            search_weight = validate_finite_number(
                attributes.get(weight_attribute),
                f"edge attribute '{weight_attribute}'",
                minimum=0.0,
            )
        geometry = self._edge_geometry(adapter, u, v, attributes)
        return _TraversalEdge(
            reference=EdgeReference(u=u, v=v, key=key),
            distance=distance,
            travel_time=travel_time,
            risk_score=risk_score,
            search_weight=search_weight,
            geometry=geometry,
        )

    @staticmethod
    def _numeric_attribute(
        attributes: Mapping[str, Any], names: tuple[str, ...], label: str
    ) -> float:
        for name in names:
            if name in attributes and attributes[name] is not None:
                return validate_finite_number(
                    attributes[name], f"edge {label} ({name})", minimum=0.0
                )
        raise ContractValidationError(
            f"missing {label}; expected one of {', '.join(names)}"
        )

    def _edge_geometry(
        self,
        adapter: _RoadGraphAdapter,
        u: NodeId,
        v: NodeId,
        attributes: Mapping[str, Any],
    ) -> tuple[GeoPoint, ...]:
        raw_geometry = attributes.get("geometry")
        points = self._coerce_geometry(raw_geometry) if raw_geometry is not None else ()
        u_point = self._node_point(adapter, u)
        v_point = self._node_point(adapter, v)
        if not points:
            if u_point is None or v_point is None:
                raise ContractValidationError(
                    "edge geometry is missing and endpoint coordinates are unavailable"
                )
            return (u_point, v_point)
        if len(points) > 1 and u_point is not None:
            if self._squared_distance(points[-1], u_point) < self._squared_distance(
                points[0], u_point
            ):
                points = tuple(reversed(points))
        return points

    @staticmethod
    def _coerce_geometry(raw_geometry: Any) -> tuple[GeoPoint, ...]:
        if hasattr(raw_geometry, "coords"):
            raw_geometry = raw_geometry.coords
        elif isinstance(raw_geometry, Mapping):
            raw_geometry = raw_geometry.get("coordinates")
        if not isinstance(raw_geometry, Iterable) or isinstance(raw_geometry, (str, bytes)):
            raise ContractValidationError("edge geometry must expose a coordinate sequence")
        try:
            points = tuple(GeoPoint.from_value(point) for point in raw_geometry)
        except (TypeError, ContractValidationError) as error:
            raise ContractValidationError(f"invalid edge geometry: {error}") from error
        if not points:
            raise ContractValidationError("edge geometry must not be empty")
        return points

    @staticmethod
    def _node_point(adapter: _RoadGraphAdapter, node: NodeId) -> GeoPoint | None:
        attributes = adapter.node_data(node)
        longitude = attributes.get("x", attributes.get("longitude", attributes.get("lon")))
        latitude = attributes.get("y", attributes.get("latitude", attributes.get("lat")))
        if longitude is None or latitude is None:
            return None
        try:
            return GeoPoint(longitude=longitude, latitude=latitude)
        except ContractValidationError:
            return None

    @staticmethod
    def _squared_distance(first: GeoPoint, second: GeoPoint) -> float:
        return (first.longitude - second.longitude) ** 2 + (
            first.latitude - second.latitude
        ) ** 2

    @staticmethod
    def _merge_geometry(edges: tuple[_TraversalEdge, ...]) -> tuple[GeoPoint, ...]:
        merged: list[GeoPoint] = []
        for edge in edges:
            if not merged:
                merged.extend(edge.geometry)
            elif edge.geometry and merged[-1] == edge.geometry[0]:
                merged.extend(edge.geometry[1:])
            else:
                merged.extend(edge.geometry)
        return tuple(merged)

    def _to_candidate(
        self,
        *,
        index: int,
        nodes: tuple[NodeId, ...],
        edges: tuple[_TraversalEdge, ...],
    ) -> CandidatePath:
        return CandidatePath(
            path_id=f"path_{index:03d}",
            nodes=nodes,
            edges=tuple(edge.reference for edge in edges),
            geometry=self._merge_geometry(edges),
            distance=sum(edge.distance for edge in edges),
            travel_time=sum(edge.travel_time for edge in edges),
            risk_score=sum(edge.risk_score for edge in edges),
        )

    @staticmethod
    def _diagnostic(
        code: str,
        severity: DiagnosticSeverity,
        message: str,
        details: Mapping[str, Any],
    ) -> Diagnostic:
        return Diagnostic(code=code, severity=severity, message=message, details=details)

    def _generated_diagnostic(
        self, generated: int, requested: int, duplicates: int, expansions: int
    ) -> Diagnostic:
        return self._diagnostic(
            "CANDIDATES_GENERATED",
            DiagnosticSeverity.INFO,
            "Candidate path generation completed.",
            {
                "generated": generated,
                "requested_k": requested,
                "duplicates_removed": duplicates,
                "expansions": expansions,
            },
        )


def generate_candidate_paths(
    graph: Any,
    origin: NodeId,
    destination: NodeId,
    *,
    k: int = 3,
    weight_attribute: str = "travel_time",
    max_expansions: int = 100_000,
    diagnostics: MutableSequence[Diagnostic] | None = None,
    logger: logging.Logger | None = None,
) -> list[CandidatePath]:
    """Functional API returning only ``list[CandidatePath]``.

    Pass a mutable ``diagnostics`` sequence when structured diagnostics are
    required alongside the list output.
    """

    generator = CandidatePathGenerator(logger=logger)
    candidates = generator.generate(
        graph,
        origin,
        destination,
        k=k,
        weight_attribute=weight_attribute,
        max_expansions=max_expansions,
    )
    if diagnostics is not None:
        diagnostics.extend(generator.diagnostics)
    return candidates
