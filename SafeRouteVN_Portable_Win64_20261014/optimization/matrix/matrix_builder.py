"""Production-oriented construction of aligned directed D/T/R/G matrices."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Iterable, Mapping, MutableSequence, Sequence

from optimization.models import (
    CandidatePath,
    ContractValidationError,
    Diagnostic,
    DiagnosticSeverity,
    GeoPoint,
    MatrixBundle,
)
from optimization.models.common import NodeId, validate_node_id


LOGGER = logging.getLogger(__name__)
DEFAULT_SELECTION_RULE = (
    "travel_time,distance,risk_score,path_id,node_sequence,edge_sequence"
)


class MatrixBuilder:
    """Build one internally consistent MatrixBundle from CandidatePath objects.

    A complete directed candidate set is required for every off-diagonal pair
    in the canonical decision-node order. The builder fails closed and returns
    ``None`` when finite matrix values cannot be produced without inventing
    data. Structured diagnostics remain available through :attr:`diagnostics`.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or LOGGER
        self._diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        """Diagnostics from the most recent build call."""

        return self._diagnostics

    def build(
        self,
        candidate_paths: Iterable[CandidatePath],
        *,
        matrix_version: str,
        source: Mapping[str, Any] | None = None,
        node_order: Sequence[NodeId] | None = None,
        selection_key: Callable[[CandidatePath], Any] | None = None,
        selection_rule: str = DEFAULT_SELECTION_RULE,
        schema_version: str = "1.0.0",
    ) -> MatrixBundle | None:
        """Build a complete directed matrix bundle or return ``None``.

        The default deterministic rule selects by travel time, distance, risk
        exposure, path ID, node sequence, and edge sequence. A later phase may
        inject another deterministic key without placing profile semantics in
        this module.
        """

        diagnostics: list[Diagnostic] = []
        self._logger.info("Matrix build started: matrix_version=%r", matrix_version)
        diagnostics.append(
            self._diagnostic(
                "MATRIX_BUILD_STARTED",
                DiagnosticSeverity.INFO,
                "D/T/R/G matrix construction started.",
                {"matrix_version": repr(matrix_version)},
            )
        )

        try:
            paths = list(candidate_paths)
        except (TypeError, ValueError) as error:
            diagnostics.append(
                self._diagnostic(
                    "INVALID_CANDIDATE_COLLECTION",
                    DiagnosticSeverity.ERROR,
                    "Candidate paths must be an iterable of CandidatePath objects.",
                    {"reason": str(error)},
                )
            )
            self._finish_failure(diagnostics, "Candidate collection is invalid")
            return None

        if not paths:
            diagnostics.append(
                self._diagnostic(
                    "EMPTY_CANDIDATE_SET",
                    DiagnosticSeverity.ERROR,
                    "At least one directed CandidatePath is required.",
                    {},
                )
            )
            self._finish_failure(diagnostics, "Candidate collection is empty")
            return None

        pair_candidates: dict[tuple[NodeId, NodeId], list[CandidatePath]] = defaultdict(list)
        decision_nodes: set[NodeId] = set()
        path_ids: set[str] = set()
        fatal_input = False

        for index, path in enumerate(paths):
            if not isinstance(path, CandidatePath):
                fatal_input = True
                diagnostics.append(
                    self._diagnostic(
                        "INVALID_CANDIDATE",
                        DiagnosticSeverity.ERROR,
                        "Candidate collection contains a non-CandidatePath value.",
                        {"index": index, "type": type(path).__name__},
                    )
                )
                continue
            try:
                if not path.geometry:
                    raise ContractValidationError("geometry must not be empty")
                for point in path.geometry:
                    if not isinstance(point, GeoPoint):
                        raise ContractValidationError(
                            "geometry must contain GeoPoint values"
                        )
                    GeoPoint(point.longitude, point.latitude)
            except (ContractValidationError, TypeError, AttributeError) as error:
                fatal_input = True
                diagnostics.append(
                    self._diagnostic(
                        "INVALID_GEOMETRY",
                        DiagnosticSeverity.ERROR,
                        "CandidatePath geometry failed validation.",
                        {"index": index, "path_id": path.path_id, "reason": str(error)},
                    )
                )
                continue
            try:
                CandidatePath.from_dict(path.to_dict())
            except Exception as error:  # Fail closed for externally mutated objects.
                fatal_input = True
                diagnostics.append(
                    self._diagnostic(
                        "INVALID_CANDIDATE",
                        DiagnosticSeverity.ERROR,
                        "CandidatePath failed contract validation.",
                        {"index": index, "path_id": path.path_id, "reason": str(error)},
                    )
                )
                continue
            if path.path_id in path_ids:
                fatal_input = True
                diagnostics.append(
                    self._diagnostic(
                        "DUPLICATE_PATH_ID",
                        DiagnosticSeverity.ERROR,
                        "Candidate path IDs must be unique for replayable provenance.",
                        {"path_id": path.path_id},
                    )
                )
                continue
            path_ids.add(path.path_id)
            origin, destination = path.nodes[0], path.nodes[-1]
            decision_nodes.update((origin, destination))
            if origin == destination:
                diagnostics.append(
                    self._diagnostic(
                        "SELF_PATH_IGNORED",
                        DiagnosticSeverity.INFO,
                        "A self path was ignored because matrix diagonals are fixed at zero.",
                        {"path_id": path.path_id, "node": origin},
                    )
                )
                continue
            pair_candidates[(origin, destination)].append(path)

        if fatal_input:
            self._finish_failure(diagnostics, "Candidate validation failed")
            return None
        if not decision_nodes:
            diagnostics.append(
                self._diagnostic(
                    "NO_DECISION_NODES",
                    DiagnosticSeverity.ERROR,
                    "No origin/destination decision nodes were found.",
                    {},
                )
            )
            self._finish_failure(diagnostics, "No decision nodes found")
            return None

        canonical_order = self._canonical_node_order(
            decision_nodes, node_order, diagnostics
        )
        if canonical_order is None:
            self._finish_failure(diagnostics, "Node ordering is inconsistent")
            return None

        missing_pairs = [
            {"origin": origin, "destination": destination}
            for origin in canonical_order
            for destination in canonical_order
            if origin != destination and not pair_candidates.get((origin, destination))
        ]
        if missing_pairs:
            diagnostics.append(
                self._diagnostic(
                    "MISSING_CANDIDATE_PATHS",
                    DiagnosticSeverity.ERROR,
                    "The directed candidate set is incomplete.",
                    {"count": len(missing_pairs), "pairs": missing_pairs},
                )
            )
            self._finish_failure(
                diagnostics,
                f"Missing directed candidate paths: count={len(missing_pairs)}",
            )
            return None

        duplicate_pairs = [
            {
                "origin": pair[0],
                "destination": pair[1],
                "candidate_path_ids": sorted(path.path_id for path in pair_paths),
            }
            for pair, pair_paths in pair_candidates.items()
            if len(pair_paths) > 1
        ]
        if duplicate_pairs:
            diagnostics.append(
                self._diagnostic(
                    "DUPLICATE_OD_CANDIDATES",
                    DiagnosticSeverity.WARNING,
                    "Multiple candidates exist for one or more directed node pairs; "
                    "the deterministic selection rule was applied.",
                    {"pairs": duplicate_pairs, "selection_rule": selection_rule},
                )
            )
            self._logger.info(
                "Matrix build resolved duplicate OD candidates: pairs=%d",
                len(duplicate_pairs),
            )

        selected: dict[tuple[NodeId, NodeId], CandidatePath] = {}
        try:
            for pair, pair_paths in pair_candidates.items():
                selected[pair] = min(
                    pair_paths,
                    key=lambda path: self._selection_key(path, selection_key),
                )
        except (TypeError, ValueError, ContractValidationError) as error:
            diagnostics.append(
                self._diagnostic(
                    "INVALID_SELECTION_RULE",
                    DiagnosticSeverity.ERROR,
                    "Candidate selection did not produce a deterministic comparable key.",
                    {"selection_rule": selection_rule, "reason": str(error)},
                )
            )
            self._finish_failure(diagnostics, "Candidate selection failed")
            return None

        size = len(canonical_order)
        distance = [[0.0 for _ in range(size)] for _ in range(size)]
        travel_time = [[0.0 for _ in range(size)] for _ in range(size)]
        risk = [[0.0 for _ in range(size)] for _ in range(size)]
        geometry: list[list[tuple[Any, ...]]] = [
            [tuple() for _ in range(size)] for _ in range(size)
        ]
        provenance: list[list[Mapping[str, Any]]] = [
            [{} for _ in range(size)] for _ in range(size)
        ]

        for row, origin in enumerate(canonical_order):
            for column, destination in enumerate(canonical_order):
                if origin == destination:
                    provenance[row][column] = {
                        "origin": origin,
                        "destination": destination,
                        "selected_path_id": None,
                        "candidate_path_ids": [],
                        "selection_rule": "diagonal_zero",
                    }
                    continue
                pair = (origin, destination)
                path = selected[pair]
                candidates = sorted(
                    pair_candidates[pair], key=lambda item: self._default_selection_key(item)
                )
                distance[row][column] = path.distance
                travel_time[row][column] = path.travel_time
                risk[row][column] = path.risk_score
                geometry[row][column] = path.geometry
                provenance[row][column] = {
                    "origin": origin,
                    "destination": destination,
                    "selected_path_id": path.path_id,
                    "candidate_path_ids": [item.path_id for item in candidates],
                    "selection_rule": selection_rule,
                    "selected_edges": [edge.to_dict() for edge in path.edges],
                }

        source_metadata = dict(source or {})
        source_metadata.update(
            {
                "candidate_path_schema": "CandidatePath",
                "candidate_count": len(paths),
                "decision_node_count": size,
                "selection_rule": selection_rule,
                "unit_schema_version": "v1",
                "distance_unit": "meter",
                "time_unit": "second",
                "risk_semantic": "exposure_proxy",
                "geometry_order": "longitude_latitude",
                "coordinate_system": "WGS84",
            }
        )

        try:
            bundle = MatrixBundle(
                version=matrix_version,
                node_order=canonical_order,
                distance_matrix=tuple(tuple(row) for row in distance),
                time_matrix=tuple(tuple(row) for row in travel_time),
                risk_matrix=tuple(tuple(row) for row in risk),
                geometry_matrix=tuple(tuple(row) for row in geometry),
                schema_version=schema_version,
                source=source_metadata,
                entry_provenance=tuple(tuple(row) for row in provenance),
            )
        except (ContractValidationError, TypeError, ValueError) as error:
            diagnostics.append(
                self._diagnostic(
                    "INVALID_MATRIX_VALUES",
                    DiagnosticSeverity.ERROR,
                    "MatrixBundle construction failed validation.",
                    {"reason": str(error)},
                )
            )
            self._finish_failure(diagnostics, "MatrixBundle validation failed")
            return None

        validation_diagnostics: list[Diagnostic] = []
        if not validate_matrix_bundle(bundle, diagnostics=validation_diagnostics):
            diagnostics.extend(validation_diagnostics)
            self._finish_failure(diagnostics, "Matrix consistency validation failed")
            return None

        diagnostics.append(
            self._diagnostic(
                "MATRIX_BUILT",
                DiagnosticSeverity.INFO,
                "D/T/R/G MatrixBundle constructed successfully.",
                {
                    "matrix_version": bundle.matrix_version,
                    "schema_version": bundle.schema_version,
                    "size": size,
                },
            )
        )
        self._diagnostics = tuple(diagnostics)
        self._logger.info(
            "Matrix build completed: matrix_version=%s size=%d candidates=%d",
            bundle.matrix_version,
            size,
            len(paths),
        )
        return bundle

    @staticmethod
    def _canonical_node_order(
        decision_nodes: set[NodeId],
        requested_order: Sequence[NodeId] | None,
        diagnostics: list[Diagnostic],
    ) -> tuple[NodeId, ...] | None:
        if requested_order is None:
            return tuple(sorted(decision_nodes, key=MatrixBuilder._node_sort_key))
        if isinstance(requested_order, (str, bytes)):
            diagnostics.append(
                MatrixBuilder._diagnostic(
                    "INCONSISTENT_NODE_ORDER",
                    DiagnosticSeverity.ERROR,
                    "node_order must be a sequence of node identifiers.",
                    {},
                )
            )
            return None
        try:
            order = tuple(
                validate_node_id(node, f"node_order[{index}]")
                for index, node in enumerate(requested_order)
            )
        except ContractValidationError as error:
            diagnostics.append(
                MatrixBuilder._diagnostic(
                    "INCONSISTENT_NODE_ORDER",
                    DiagnosticSeverity.ERROR,
                    "node_order contains an invalid node identifier.",
                    {"reason": str(error)},
                )
            )
            return None
        if len(order) != len(set(order)) or set(order) != decision_nodes:
            diagnostics.append(
                MatrixBuilder._diagnostic(
                    "INCONSISTENT_NODE_ORDER",
                    DiagnosticSeverity.ERROR,
                    "node_order must contain every decision node exactly once.",
                    {
                        "provided": list(order),
                        "expected": sorted(
                            decision_nodes, key=MatrixBuilder._node_sort_key
                        ),
                    },
                )
            )
            return None
        return order

    @staticmethod
    def _node_sort_key(node: NodeId) -> tuple[int, Any]:
        return (0, node) if isinstance(node, int) else (1, node)

    @staticmethod
    def _default_selection_key(path: CandidatePath) -> tuple[Any, ...]:
        return (
            path.travel_time,
            path.distance,
            path.risk_score,
            path.path_id,
            tuple((0, node) if isinstance(node, int) else (1, node) for node in path.nodes),
            tuple(
                (repr(edge.u), repr(edge.v), repr(edge.key)) for edge in path.edges
            ),
        )

    @staticmethod
    def _selection_key(
        path: CandidatePath, custom_key: Callable[[CandidatePath], Any] | None
    ) -> tuple[Any, ...]:
        default_key = MatrixBuilder._default_selection_key(path)
        return default_key if custom_key is None else (custom_key(path), default_key)

    @staticmethod
    def _diagnostic(
        code: str,
        severity: DiagnosticSeverity,
        message: str,
        details: Mapping[str, Any],
    ) -> Diagnostic:
        return Diagnostic(code=code, severity=severity, message=message, details=details)

    def _finish_failure(self, diagnostics: list[Diagnostic], log_message: str) -> None:
        self._diagnostics = tuple(diagnostics)
        self._logger.warning("%s", log_message)


def validate_matrix_bundle(
    bundle: MatrixBundle,
    *,
    diagnostics: MutableSequence[Diagnostic] | None = None,
) -> bool:
    """Validate matrix alignment, diagonals, geometry, and entry provenance."""

    issues: list[Diagnostic] = []
    if not isinstance(bundle, MatrixBundle):
        issues.append(
            MatrixBuilder._diagnostic(
                "INVALID_MATRIX_BUNDLE",
                DiagnosticSeverity.ERROR,
                "Value is not a MatrixBundle.",
                {"type": type(bundle).__name__},
            )
        )
    else:
        try:
            MatrixBundle.from_dict(bundle.to_dict())
        except Exception as error:  # Validate externally mutated instances safely.
            issues.append(
                MatrixBuilder._diagnostic(
                    "INVALID_MATRIX_STRUCTURE",
                    DiagnosticSeverity.ERROR,
                    "MatrixBundle structure or values are invalid.",
                    {"reason": str(error)},
                )
            )
        else:
            size = len(bundle.node_order)
            if not bundle.source:
                issues.append(
                    MatrixBuilder._diagnostic(
                        "MISSING_MATRIX_SOURCE",
                        DiagnosticSeverity.ERROR,
                        "MatrixBundle source metadata is required.",
                        {},
                    )
                )
            for index, node in enumerate(bundle.node_order):
                diagonal_values = (
                    bundle.distance_matrix[index][index],
                    bundle.time_matrix[index][index],
                    bundle.risk_matrix[index][index],
                )
                if diagonal_values != (0.0, 0.0, 0.0):
                    issues.append(
                        MatrixBuilder._diagnostic(
                            "INVALID_MATRIX_DIAGONAL",
                            DiagnosticSeverity.ERROR,
                            "D/T/R diagonal values must all be zero.",
                            {"index": index, "node": node, "values": diagonal_values},
                        )
                    )
            for row, origin in enumerate(bundle.node_order):
                for column, destination in enumerate(bundle.node_order):
                    entry = bundle.entry_provenance[row][column]
                    if (
                        entry.get("origin") != origin
                        or entry.get("destination") != destination
                    ):
                        issues.append(
                            MatrixBuilder._diagnostic(
                                "INCONSISTENT_ENTRY_PROVENANCE",
                                DiagnosticSeverity.ERROR,
                                "Entry provenance does not match node_order.",
                                {"row": row, "column": column},
                            )
                        )
                    if row != column:
                        if not bundle.geometry_matrix[row][column]:
                            issues.append(
                                MatrixBuilder._diagnostic(
                                    "INVALID_GEOMETRY",
                                    DiagnosticSeverity.ERROR,
                                    "Off-diagonal matrix geometry must not be empty.",
                                    {"origin": origin, "destination": destination},
                                )
                            )
                        if not entry.get("selected_path_id"):
                            issues.append(
                                MatrixBuilder._diagnostic(
                                    "MISSING_ENTRY_PATH_PROVENANCE",
                                    DiagnosticSeverity.ERROR,
                                    "Off-diagonal entry is missing its source path ID.",
                                    {"origin": origin, "destination": destination},
                                )
                            )
            if len(bundle.entry_provenance) != size:
                issues.append(
                    MatrixBuilder._diagnostic(
                        "INVALID_PROVENANCE_DIMENSIONS",
                        DiagnosticSeverity.ERROR,
                        "Entry provenance dimensions must match node_order.",
                        {},
                    )
                )
    if diagnostics is not None:
        diagnostics.extend(issues)
    return not issues


def build_matrix_bundle(
    candidate_paths: Iterable[CandidatePath],
    *,
    matrix_version: str,
    source: Mapping[str, Any] | None = None,
    node_order: Sequence[NodeId] | None = None,
    selection_key: Callable[[CandidatePath], Any] | None = None,
    selection_rule: str = DEFAULT_SELECTION_RULE,
    schema_version: str = "1.0.0",
    diagnostics: MutableSequence[Diagnostic] | None = None,
    logger: logging.Logger | None = None,
) -> MatrixBundle | None:
    """Functional MatrixBundle builder with an optional diagnostic sink."""

    builder = MatrixBuilder(logger=logger)
    bundle = builder.build(
        candidate_paths,
        matrix_version=matrix_version,
        source=source,
        node_order=node_order,
        selection_key=selection_key,
        selection_rule=selection_rule,
        schema_version=schema_version,
    )
    if diagnostics is not None:
        diagnostics.extend(builder.diagnostics)
    return bundle
