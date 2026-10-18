"""Configuration-driven Profile Decision Engine for TASK-02 Phase D."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from optimization.models import (
    CandidatePath,
    Diagnostic,
    DiagnosticSeverity,
    MatrixBundle,
    OptimizationProfile,
)
from optimization.models.common import (
    ContractValidationError,
    JsonValue,
    NodeId,
    UnitMetadata,
    freeze_json,
    require_non_empty_string,
    to_json_value,
    validate_finite_number,
)

from .config import ProfileConfig, load_profile_config
from .explanation import TradeoffDeltas, TradeoffExplanation
from .normalization import (
    NormalizationConfig,
    NormalizedMetrics,
    RawMetrics,
    load_normalization_config,
    normalize_metrics,
)
from .objective import (
    LinearObjectiveFunction,
    ObjectiveComponents,
    ObjectiveFunction,
)


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GeometryReference:
    """Reference to the selected CandidatePath geometry and matrix provenance."""

    candidate_path_id: str
    matrix_version: str
    origin: NodeId
    destination: NodeId
    matrix_row: int
    matrix_column: int
    matrix_selected_geometry: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_path_id",
            require_non_empty_string(self.candidate_path_id, "candidate_path_id"),
        )
        object.__setattr__(
            self,
            "matrix_version",
            require_non_empty_string(self.matrix_version, "matrix_version"),
        )
        if self.matrix_row < 0 or self.matrix_column < 0:
            raise ContractValidationError("matrix indices must be non-negative")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "candidate_path_id": self.candidate_path_id,
            "matrix_version": self.matrix_version,
            "origin": self.origin,
            "destination": self.destination,
            "matrix_row": self.matrix_row,
            "matrix_column": self.matrix_column,
            "matrix_selected_geometry": self.matrix_selected_geometry,
        }


@dataclass(frozen=True, slots=True)
class AlternativeEvaluation:
    """One normalized and scored CandidatePath evaluation."""

    candidate_id: str
    normalized_metrics: NormalizedMetrics
    objective_score: float
    objective_breakdown: ObjectiveComponents
    geometry_reference: GeometryReference
    provenance_reference: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_id",
            require_non_empty_string(self.candidate_id, "candidate_id"),
        )
        if not isinstance(self.normalized_metrics, NormalizedMetrics):
            raise ContractValidationError("normalized_metrics must be NormalizedMetrics")
        object.__setattr__(
            self,
            "objective_score",
            validate_finite_number(
                self.objective_score, "objective_score", minimum=0.0
            ),
        )
        if not isinstance(self.objective_breakdown, ObjectiveComponents):
            raise ContractValidationError(
                "objective_breakdown must be ObjectiveComponents"
            )
        if not isinstance(self.geometry_reference, GeometryReference):
            raise ContractValidationError(
                "geometry_reference must be GeometryReference"
            )
        object.__setattr__(
            self,
            "provenance_reference",
            freeze_json(self.provenance_reference, "provenance_reference"),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "candidate_id": self.candidate_id,
            "normalized_metrics": self.normalized_metrics.to_dict(),
            "objective_score": self.objective_score,
            "objective_breakdown": self.objective_breakdown.to_dict(),
            "geometry_reference": self.geometry_reference.to_dict(),
            "provenance_reference": to_json_value(self.provenance_reference),
        }


@dataclass(frozen=True, slots=True)
class ProfileDecisionResult:
    """Serializable Phase D handoff contract for one configured profile."""

    selected_profile: OptimizationProfile
    selected_candidate_id: str
    objective_score: float
    normalized_metrics: NormalizedMetrics
    alternatives_evaluated: tuple[AlternativeEvaluation, ...]
    ranking_reason: str
    explanation: TradeoffExplanation
    provenance_reference: Mapping[str, Any]
    objective_breakdown: ObjectiveComponents
    route_geometry_reference: GeometryReference
    schema_version: str = "1.0.0"
    unit_metadata: UnitMetadata = UnitMetadata()

    def __post_init__(self) -> None:
        profile = (
            self.selected_profile
            if isinstance(self.selected_profile, OptimizationProfile)
            else OptimizationProfile(self.selected_profile)
        )
        object.__setattr__(self, "selected_profile", profile)
        object.__setattr__(
            self,
            "selected_candidate_id",
            require_non_empty_string(
                self.selected_candidate_id, "selected_candidate_id"
            ),
        )
        object.__setattr__(
            self,
            "objective_score",
            validate_finite_number(
                self.objective_score, "objective_score", minimum=0.0
            ),
        )
        if not self.alternatives_evaluated:
            raise ContractValidationError("alternatives_evaluated must not be empty")
        selected = [
            item
            for item in self.alternatives_evaluated
            if item.candidate_id == self.selected_candidate_id
        ]
        if len(selected) != 1:
            raise ContractValidationError(
                "selected_candidate_id must identify exactly one evaluated alternative"
            )
        object.__setattr__(
            self,
            "ranking_reason",
            require_non_empty_string(self.ranking_reason, "ranking_reason"),
        )
        if not isinstance(self.explanation, TradeoffExplanation):
            raise ContractValidationError("explanation must be TradeoffExplanation")
        object.__setattr__(
            self,
            "provenance_reference",
            freeze_json(self.provenance_reference, "provenance_reference"),
        )
        object.__setattr__(
            self,
            "schema_version",
            require_non_empty_string(self.schema_version, "schema_version"),
        )
        if not isinstance(self.unit_metadata, UnitMetadata):
            raise ContractValidationError("unit_metadata must be UnitMetadata")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            **self.unit_metadata.to_dict(),
            "schema_version": self.schema_version,
            "profile": self.selected_profile.value,
            "selected_candidate_id": self.selected_candidate_id,
            "objective_score": self.objective_score,
            "normalized_metrics": self.normalized_metrics.to_dict(),
            "alternatives_evaluated": [
                item.to_dict() for item in self.alternatives_evaluated
            ],
            "ranking_reason": self.ranking_reason,
            "explanation": self.explanation.to_dict(),
            "provenance_reference": to_json_value(self.provenance_reference),
            "objective_breakdown": self.objective_breakdown.to_dict(),
            "route_geometry_reference": self.route_geometry_reference.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ProfileEngineLoadResult:
    """Non-throwing configuration load result for the engine."""

    engine: "ProfileEngine | None"
    diagnostics: tuple[Diagnostic, ...]

    @property
    def is_valid(self) -> bool:
        return self.engine is not None and not any(
            item.severity == DiagnosticSeverity.ERROR for item in self.diagnostics
        )


class ProfileEngine:
    """Evaluate CandidatePath alternatives with configured normalized objectives."""

    def __init__(
        self,
        profile_config: ProfileConfig,
        normalization_config: NormalizationConfig,
        *,
        objective: ObjectiveFunction | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not isinstance(profile_config, ProfileConfig):
            raise ContractValidationError("profile_config must be ProfileConfig")
        if not isinstance(normalization_config, NormalizationConfig):
            raise ContractValidationError(
                "normalization_config must be NormalizationConfig"
            )
        self._profile_config = profile_config
        self._normalization_config = normalization_config
        self._objective = objective or LinearObjectiveFunction()
        if not isinstance(self._objective, ObjectiveFunction):
            raise ContractValidationError("objective must implement ObjectiveFunction")
        self._logger = logger or LOGGER
        self._diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return self._diagnostics

    @classmethod
    def from_config_directory(
        cls,
        config_directory: str | Path,
        *,
        normalization_config: NormalizationConfig | None = None,
        objective: ObjectiveFunction | None = None,
        logger: logging.Logger | None = None,
    ) -> ProfileEngineLoadResult:
        """Load configuration without hiding missing or invalid references."""

        diagnostics: list[Diagnostic] = []
        profile_result = load_profile_config(config_directory)
        diagnostics.extend(profile_result.diagnostics)
        resolved_normalization = normalization_config
        if resolved_normalization is None:
            normalization_result = load_normalization_config(config_directory)
            diagnostics.extend(normalization_result.diagnostics)
            resolved_normalization = normalization_result.config
        if profile_result.config is None or resolved_normalization is None:
            return ProfileEngineLoadResult(None, tuple(diagnostics))
        return ProfileEngineLoadResult(
            cls(
                profile_result.config,
                resolved_normalization,
                objective=objective,
                logger=logger,
            ),
            tuple(diagnostics),
        )

    def evaluate(
        self,
        profile: OptimizationProfile | str,
        matrix_bundle: MatrixBundle,
        candidate_paths: Iterable[CandidatePath],
    ) -> ProfileDecisionResult | None:
        """Evaluate and deterministically select one registered alternative."""

        diagnostics: list[Diagnostic] = []
        try:
            selected_profile = (
                profile if isinstance(profile, OptimizationProfile) else OptimizationProfile(profile)
            )
        except (ValueError, TypeError):
            diagnostics.append(
                self._diagnostic(
                    "UNKNOWN_PROFILE",
                    DiagnosticSeverity.ERROR,
                    "Requested profile is not configured.",
                    {"profile": repr(profile)},
                )
            )
            self._diagnostics = tuple(diagnostics)
            return None
        weights = self._profile_config.weights.get(selected_profile)
        if weights is None:
            diagnostics.append(
                self._diagnostic(
                    "MISSING_PROFILE_CONFIGURATION",
                    DiagnosticSeverity.ERROR,
                    "Requested profile has no configured weights.",
                    {"profile": selected_profile.value},
                )
            )
            self._diagnostics = tuple(diagnostics)
            return None
        weights = weights.normalized()

        if not isinstance(matrix_bundle, MatrixBundle):
            diagnostics.append(
                self._diagnostic(
                    "INVALID_MATRIX_BUNDLE",
                    DiagnosticSeverity.ERROR,
                    "Profile evaluation requires a MatrixBundle.",
                    {},
                )
            )
            self._diagnostics = tuple(diagnostics)
            return None
        try:
            paths = list(candidate_paths)
        except TypeError as error:
            diagnostics.append(
                self._diagnostic(
                    "INVALID_ALTERNATIVES",
                    DiagnosticSeverity.ERROR,
                    "Candidate alternatives must be iterable.",
                    {"reason": str(error)},
                )
            )
            self._diagnostics = tuple(diagnostics)
            return None
        if not paths or any(not isinstance(path, CandidatePath) for path in paths):
            diagnostics.append(
                self._diagnostic(
                    "INVALID_ALTERNATIVES",
                    DiagnosticSeverity.ERROR,
                    "At least one CandidatePath alternative is required.",
                    {},
                )
            )
            self._diagnostics = tuple(diagnostics)
            return None
        if len({path.path_id for path in paths}) != len(paths):
            diagnostics.append(
                self._diagnostic(
                    "DUPLICATE_CANDIDATE_ID",
                    DiagnosticSeverity.ERROR,
                    "Candidate path IDs must be unique.",
                    {},
                )
            )
            self._diagnostics = tuple(diagnostics)
            return None
        endpoints = {(path.nodes[0], path.nodes[-1]) for path in paths}
        if len(endpoints) != 1:
            diagnostics.append(
                self._diagnostic(
                    "INCONSISTENT_ALTERNATIVE_ENDPOINTS",
                    DiagnosticSeverity.ERROR,
                    "All alternatives must share one origin/destination pair.",
                    {"pairs": [list(pair) for pair in sorted(endpoints, key=repr)]},
                )
            )
            self._diagnostics = tuple(diagnostics)
            return None

        evaluations: list[tuple[AlternativeEvaluation, CandidatePath]] = []
        for path in paths:
            evaluation = self._evaluate_candidate(
                path, matrix_bundle, weights, diagnostics
            )
            if evaluation is None:
                self._diagnostics = tuple(diagnostics)
                return None
            evaluations.append((evaluation, path))

        evaluations.sort(key=lambda item: self._ranking_key(item[0]))
        selected_evaluation, selected_path = evaluations[0]
        compared_evaluation, compared_path = (
            evaluations[1] if len(evaluations) > 1 else evaluations[0]
        )
        explanation = self._explanation(
            selected_evaluation,
            selected_path,
            compared_evaluation,
            compared_path,
            only_candidate=len(evaluations) == 1,
        )
        ranking_reason = (
            "Minimum configured normalized objective; ties resolve by normalized "
            "time, risk, distance, then candidate ID."
        )
        result = ProfileDecisionResult(
            selected_profile=selected_profile,
            selected_candidate_id=selected_path.path_id,
            objective_score=selected_evaluation.objective_score,
            normalized_metrics=selected_evaluation.normalized_metrics,
            alternatives_evaluated=tuple(item[0] for item in evaluations),
            ranking_reason=ranking_reason,
            explanation=explanation,
            provenance_reference=selected_evaluation.provenance_reference,
            objective_breakdown=selected_evaluation.objective_breakdown,
            route_geometry_reference=selected_evaluation.geometry_reference,
        )
        diagnostics.append(
            self._diagnostic(
                "PROFILE_DECISION_CREATED",
                DiagnosticSeverity.INFO,
                "Profile decision completed.",
                {
                    "profile": selected_profile.value,
                    "selected_candidate_id": selected_path.path_id,
                    "alternatives_evaluated": len(evaluations),
                },
            )
        )
        self._diagnostics = tuple(diagnostics)
        self._logger.info(
            "Profile decision completed: profile=%s candidate=%s score=%s",
            selected_profile.value,
            selected_path.path_id,
            selected_evaluation.objective_score,
        )
        return result

    def _evaluate_candidate(
        self,
        path: CandidatePath,
        matrix_bundle: MatrixBundle,
        weights: Any,
        diagnostics: list[Diagnostic],
    ) -> AlternativeEvaluation | None:
        origin, destination = path.nodes[0], path.nodes[-1]
        try:
            row = matrix_bundle.node_order.index(origin)
            column = matrix_bundle.node_order.index(destination)
        except ValueError:
            diagnostics.append(
                self._diagnostic(
                    "CANDIDATE_NOT_IN_MATRIX",
                    DiagnosticSeverity.ERROR,
                    "Candidate endpoints are absent from MatrixBundle.node_order.",
                    {"candidate_path_id": path.path_id},
                )
            )
            return None
        entry = matrix_bundle.entry_provenance[row][column]
        registered_ids = set(entry.get("candidate_path_ids", ()))
        selected_id = entry.get("selected_path_id")
        if selected_id:
            registered_ids.add(selected_id)
        if path.path_id not in registered_ids:
            diagnostics.append(
                self._diagnostic(
                    "CANDIDATE_NOT_IN_MATRIX_PROVENANCE",
                    DiagnosticSeverity.ERROR,
                    "Candidate path is not registered in MatrixBundle provenance.",
                    {"candidate_path_id": path.path_id, "origin": origin, "destination": destination},
                )
            )
            return None
        try:
            normalized = normalize_metrics(
                RawMetrics(
                    distance=path.distance,
                    time=path.travel_time,
                    length=path.distance,
                    risk=path.risk_score,
                ),
                self._normalization_config,
            )
            components = self._objective.components(normalized, weights)
            score = self._objective.score(normalized, weights)
        except (ContractValidationError, TypeError, ValueError) as error:
            diagnostics.append(
                self._diagnostic(
                    "INVALID_NORMALIZED_METRICS",
                    DiagnosticSeverity.ERROR,
                    "Candidate metrics could not be normalized and scored.",
                    {"candidate_path_id": path.path_id, "reason": str(error)},
                )
            )
            return None
        matrix_selected = selected_id == path.path_id
        if matrix_selected:
            matrix_values = (
                matrix_bundle.distance_matrix[row][column],
                matrix_bundle.time_matrix[row][column],
                matrix_bundle.risk_matrix[row][column],
                matrix_bundle.geometry_matrix[row][column],
            )
            candidate_values = (
                path.distance,
                path.travel_time,
                path.risk_score,
                path.geometry,
            )
            if matrix_values != candidate_values:
                diagnostics.append(
                    self._diagnostic(
                        "MATRIX_CANDIDATE_MISMATCH",
                        DiagnosticSeverity.ERROR,
                        "Selected matrix entry does not match its CandidatePath.",
                        {"candidate_path_id": path.path_id},
                    )
                )
                return None
        provenance = {
            "matrix_schema_version": matrix_bundle.schema_version,
            "matrix_version": matrix_bundle.matrix_version,
            "matrix_source": to_json_value(matrix_bundle.source),
            "matrix_entry": to_json_value(entry),
            "candidate_path_id": path.path_id,
            "profile_version": self._profile_config.profile_version,
            "normalization_version": self._normalization_config.normalization_version,
            "normalization_source": self._normalization_config.normalization_source,
        }
        return AlternativeEvaluation(
            candidate_id=path.path_id,
            normalized_metrics=normalized,
            objective_score=score,
            objective_breakdown=components,
            geometry_reference=GeometryReference(
                candidate_path_id=path.path_id,
                matrix_version=matrix_bundle.matrix_version,
                origin=origin,
                destination=destination,
                matrix_row=row,
                matrix_column=column,
                matrix_selected_geometry=matrix_selected,
            ),
            provenance_reference=provenance,
        )

    @staticmethod
    def _ranking_key(evaluation: AlternativeEvaluation) -> tuple[Any, ...]:
        metrics = evaluation.normalized_metrics
        return (
            evaluation.objective_score,
            metrics.time_normalized,
            metrics.risk_normalized,
            metrics.distance_normalized,
            evaluation.candidate_id,
        )

    @staticmethod
    def _explanation(
        selected: AlternativeEvaluation,
        selected_path: CandidatePath,
        compared: AlternativeEvaluation,
        compared_path: CandidatePath,
        *,
        only_candidate: bool,
    ) -> TradeoffExplanation:
        deltas = TradeoffDeltas(
            time_delta=selected_path.travel_time - compared_path.travel_time,
            distance_delta=selected_path.distance - compared_path.distance,
            risk_delta=selected_path.risk_score - compared_path.risk_score,
            objective_delta=selected.objective_score - compared.objective_score,
        )
        if only_candidate:
            why_changed = (
                f"Selected {selected.candidate_id} because it was the only evaluated "
                "feasible candidate."
            )
        else:
            why_changed = (
                f"Selected {selected.candidate_id} over {compared.candidate_id} because "
                f"its configured normalized objective was {selected.objective_score:.6f} "
                f"versus {compared.objective_score:.6f}."
            )
        return TradeoffExplanation(why_changed=why_changed, tradeoff=deltas)

    @staticmethod
    def _diagnostic(
        code: str,
        severity: DiagnosticSeverity,
        message: str,
        details: Mapping[str, Any],
    ) -> Diagnostic:
        return Diagnostic(code=code, severity=severity, message=message, details=details)
