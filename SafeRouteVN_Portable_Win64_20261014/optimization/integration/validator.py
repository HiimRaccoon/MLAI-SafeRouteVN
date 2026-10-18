"""Phase E input compatibility validation without solver behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from optimization.matrix import validate_matrix_bundle
from optimization.models import (
    CandidatePath,
    Diagnostic,
    DiagnosticSeverity,
    MatrixBundle,
)
from optimization.profiles import ProfileDecisionResult
from optimization.models.common import to_json_value


@dataclass(frozen=True, slots=True)
class PhaseEValidationResult:
    """Structured compatibility status for a future solver handoff."""

    diagnostics: tuple[Diagnostic, ...]

    @property
    def is_valid(self) -> bool:
        return not any(
            item.severity == DiagnosticSeverity.ERROR for item in self.diagnostics
        )


class PhaseEInputValidator:
    """Fail-closed validator for Phase D output and future solver inputs."""

    REQUIRED_CONSTRAINT_SECTIONS = (
        "scenario_id",
        "context_version",
        "matrix_version",
        "unit_schema_version",
        "vehicles",
        "depot",
        "orders",
    )
    REQUIRED_VEHICLE_FIELDS = (
        "vehicle_id",
        "capacity",
        "current_load",
        "availability",
        "start_location",
        "end_location",
        "working_time_window",
    )
    REQUIRED_DEPOT_FIELDS = ("start_node", "end_node")
    REQUIRED_ORDER_FIELDS = (
        "order_id",
        "demand",
        "service_time",
        "time_window",
        "earliest_arrival",
        "hard_deadline",
    )

    def validate(
        self,
        *,
        candidate_paths: Iterable[CandidatePath],
        matrix_bundle: MatrixBundle,
        decision_result: ProfileDecisionResult,
        scenario_constraints: Mapping[str, Any],
    ) -> PhaseEValidationResult:
        diagnostics: list[Diagnostic] = []
        try:
            candidates = tuple(candidate_paths)
        except TypeError as error:
            return PhaseEValidationResult(
                (
                    self._error(
                        "INVALID_CANDIDATE_COLLECTION",
                        "candidate_paths must be iterable.",
                        {"reason": str(error)},
                    ),
                )
            )

        if not candidates or any(
            not isinstance(candidate, CandidatePath) for candidate in candidates
        ):
            diagnostics.append(
                self._error(
                    "INVALID_CANDIDATE_COLLECTION",
                    "At least one CandidatePath is required.",
                    {},
                )
            )
        if not isinstance(matrix_bundle, MatrixBundle):
            diagnostics.append(
                self._error(
                    "INVALID_MATRIX_BUNDLE", "matrix_bundle must be MatrixBundle.", {}
                )
            )
        if not isinstance(decision_result, ProfileDecisionResult):
            diagnostics.append(
                self._error(
                    "INVALID_DECISION_RESULT",
                    "decision_result must be ProfileDecisionResult.",
                    {},
                )
            )
        if not isinstance(scenario_constraints, Mapping):
            diagnostics.append(
                self._error(
                    "INVALID_SCENARIO_CONSTRAINTS",
                    "scenario_constraints must be a mapping.",
                    {},
                )
            )
        if diagnostics:
            return PhaseEValidationResult(tuple(diagnostics))

        matrix_issues: list[Diagnostic] = []
        if not validate_matrix_bundle(matrix_bundle, diagnostics=matrix_issues):
            diagnostics.extend(matrix_issues)

        self._validate_constraints(scenario_constraints, diagnostics)
        self._validate_units(candidates, matrix_bundle, decision_result, diagnostics)
        self._validate_versions(
            matrix_bundle, decision_result, scenario_constraints, diagnostics
        )
        self._validate_candidate_references(
            candidates, matrix_bundle, decision_result, diagnostics
        )
        self._validate_provenance(matrix_bundle, decision_result, diagnostics)

        if not diagnostics:
            diagnostics.append(
                Diagnostic(
                    code="PHASE_E_INPUT_COMPATIBLE",
                    severity=DiagnosticSeverity.INFO,
                    message="Phase E input contracts are mutually compatible.",
                    details={
                        "matrix_version": matrix_bundle.matrix_version,
                        "selected_candidate_id": decision_result.selected_candidate_id,
                    },
                )
            )
        return PhaseEValidationResult(tuple(diagnostics))

    def _validate_constraints(
        self, constraints: Mapping[str, Any], diagnostics: list[Diagnostic]
    ) -> None:
        missing = [field for field in self.REQUIRED_CONSTRAINT_SECTIONS if field not in constraints]
        if missing:
            diagnostics.append(
                self._error(
                    "MISSING_SCENARIO_CONSTRAINTS",
                    "Scenario constraint sections are missing.",
                    {"fields": missing},
                )
            )
            return
        self._validate_records(
            constraints.get("vehicles"),
            self.REQUIRED_VEHICLE_FIELDS,
            "vehicles",
            diagnostics,
        )
        depot = constraints.get("depot")
        if not isinstance(depot, Mapping):
            diagnostics.append(
                self._error("INVALID_DEPOT_CONSTRAINTS", "depot must be a mapping.", {})
            )
        else:
            missing_depot = [field for field in self.REQUIRED_DEPOT_FIELDS if field not in depot]
            if missing_depot:
                diagnostics.append(
                    self._error(
                        "MISSING_DEPOT_FIELDS",
                        "Depot constraints are incomplete.",
                        {"fields": missing_depot},
                    )
                )
        self._validate_records(
            constraints.get("orders"),
            self.REQUIRED_ORDER_FIELDS,
            "orders",
            diagnostics,
        )

    def _validate_records(
        self,
        records: Any,
        required: Sequence[str],
        label: str,
        diagnostics: list[Diagnostic],
    ) -> None:
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            diagnostics.append(
                self._error(
                    f"INVALID_{label.upper()}", f"{label} must be a sequence.", {}
                )
            )
            return
        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                diagnostics.append(
                    self._error(
                        f"INVALID_{label.upper()}_ENTRY",
                        f"{label}[{index}] must be a mapping.",
                        {},
                    )
                )
                continue
            missing = [field for field in required if field not in record]
            if missing:
                diagnostics.append(
                    self._error(
                        f"MISSING_{label.upper()}_FIELDS",
                        f"{label}[{index}] is incomplete.",
                        {"index": index, "fields": missing},
                    )
                )

    def _validate_units(
        self,
        candidates: tuple[CandidatePath, ...],
        matrix: MatrixBundle,
        decision: ProfileDecisionResult,
        diagnostics: list[Diagnostic],
    ) -> None:
        canonical = matrix.unit_metadata
        if decision.unit_metadata != canonical:
            diagnostics.append(
                self._error(
                    "UNIT_METADATA_MISMATCH",
                    "Decision and matrix unit metadata differ.",
                    {},
                )
            )
        mismatched = [
            candidate.path_id
            for candidate in candidates
            if candidate.unit_metadata != canonical
        ]
        if mismatched:
            diagnostics.append(
                self._error(
                    "UNIT_METADATA_MISMATCH",
                    "Candidate and matrix unit metadata differ.",
                    {"candidate_path_ids": mismatched},
                )
            )

    def _validate_versions(
        self,
        matrix: MatrixBundle,
        decision: ProfileDecisionResult,
        constraints: Mapping[str, Any],
        diagnostics: list[Diagnostic],
    ) -> None:
        expected_matrix_version = constraints.get("matrix_version")
        decision_matrix_version = decision.provenance_reference.get("matrix_version")
        geometry_matrix_version = decision.route_geometry_reference.matrix_version
        if not (
            expected_matrix_version
            == decision_matrix_version
            == geometry_matrix_version
            == matrix.matrix_version
        ):
            diagnostics.append(
                self._error(
                    "MATRIX_VERSION_MISMATCH",
                    "Matrix versions are inconsistent across the handoff.",
                    {
                        "constraints": expected_matrix_version,
                        "decision": decision_matrix_version,
                        "geometry": geometry_matrix_version,
                        "matrix": matrix.matrix_version,
                    },
                )
            )
        source = matrix.source
        for field in ("scenario_id", "context_version"):
            expected = constraints.get(field)
            actual = source.get(field)
            if expected != actual:
                diagnostics.append(
                    self._error(
                        f"{field.upper()}_MISMATCH",
                        f"{field} is inconsistent between constraints and MatrixBundle source.",
                        {"constraints": expected, "matrix_source": actual},
                    )
                )
        if constraints.get("unit_schema_version") != matrix.unit_metadata.unit_schema_version:
            diagnostics.append(
                self._error(
                    "UNIT_SCHEMA_VERSION_MISMATCH",
                    "Scenario and matrix unit schema versions differ.",
                    {
                        "constraints": constraints.get("unit_schema_version"),
                        "matrix": matrix.unit_metadata.unit_schema_version,
                    },
                )
            )

    def _validate_candidate_references(
        self,
        candidates: tuple[CandidatePath, ...],
        matrix: MatrixBundle,
        decision: ProfileDecisionResult,
        diagnostics: list[Diagnostic],
    ) -> None:
        candidate_ids = {candidate.path_id for candidate in candidates}
        if len(candidate_ids) != len(candidates):
            diagnostics.append(
                self._error(
                    "DUPLICATE_CANDIDATE_ID", "Candidate IDs must be unique.", {}
                )
            )
        referenced = {decision.selected_candidate_id}
        referenced.update(item.candidate_id for item in decision.alternatives_evaluated)
        for row, provenance_row in enumerate(matrix.entry_provenance):
            for column, entry in enumerate(provenance_row):
                if row == column:
                    continue
                referenced.update(entry.get("candidate_path_ids", ()))
                selected = entry.get("selected_path_id")
                if selected:
                    referenced.add(selected)
        missing = sorted(referenced - candidate_ids)
        if missing:
            diagnostics.append(
                self._error(
                    "UNKNOWN_CANDIDATE_REFERENCE",
                    "The handoff references CandidatePath IDs not supplied to Phase E.",
                    {"candidate_path_ids": missing},
                )
            )
            return

        by_id = {candidate.path_id: candidate for candidate in candidates}
        for row, origin in enumerate(matrix.node_order):
            for column, destination in enumerate(matrix.node_order):
                if row == column:
                    continue
                entry = matrix.entry_provenance[row][column]
                for candidate_id in entry.get("candidate_path_ids", ()):
                    path = by_id[candidate_id]
                    if (path.nodes[0], path.nodes[-1]) != (origin, destination):
                        diagnostics.append(
                            self._error(
                                "CANDIDATE_ENDPOINT_MISMATCH",
                                "Candidate endpoints do not match matrix provenance.",
                                {"candidate_path_id": candidate_id},
                            )
                        )
                selected_id = entry.get("selected_path_id")
                if selected_id not in by_id:
                    continue
                selected = by_id[selected_id]
                matrix_values = (
                    matrix.distance_matrix[row][column],
                    matrix.time_matrix[row][column],
                    matrix.risk_matrix[row][column],
                    matrix.geometry_matrix[row][column],
                )
                candidate_values = (
                    selected.distance,
                    selected.travel_time,
                    selected.risk_score,
                    selected.geometry,
                )
                if matrix_values != candidate_values:
                    diagnostics.append(
                        self._error(
                            "MATRIX_CANDIDATE_MISMATCH",
                            "Matrix D/T/R/G values differ from the selected CandidatePath.",
                            {"candidate_path_id": selected_id},
                        )
                    )

    def _validate_provenance(
        self,
        matrix: MatrixBundle,
        decision: ProfileDecisionResult,
        diagnostics: list[Diagnostic],
    ) -> None:
        if not matrix.source or not matrix.entry_provenance:
            diagnostics.append(
                self._error(
                    "MISSING_PROVENANCE", "MatrixBundle provenance is required.", {}
                )
            )
        if not decision.provenance_reference:
            diagnostics.append(
                self._error(
                    "MISSING_PROVENANCE", "Decision provenance is required.", {}
                )
            )
        decision_source = decision.provenance_reference.get("matrix_source")
        if decision_source != to_json_value(matrix.source):
            diagnostics.append(
                self._error(
                    "MATRIX_SOURCE_PROVENANCE_MISMATCH",
                    "Decision matrix-source provenance differs from MatrixBundle source.",
                    {},
                )
            )
        if decision.route_geometry_reference.candidate_path_id != decision.selected_candidate_id:
            diagnostics.append(
                self._error(
                    "GEOMETRY_REFERENCE_MISMATCH",
                    "Selected candidate and geometry reference differ.",
                    {},
                )
            )

    @staticmethod
    def _error(code: str, message: str, details: Mapping[str, Any]) -> Diagnostic:
        return Diagnostic(
            code=code,
            severity=DiagnosticSeverity.ERROR,
            message=message,
            details=details,
        )
