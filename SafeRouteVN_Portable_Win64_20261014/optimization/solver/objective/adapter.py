"""Profile-aware normalized cost adapter for the existing OR-Tools solver."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from optimization.models import (
    Diagnostic,
    DiagnosticSeverity,
    MatrixBundle,
    OptimizationProfile,
)
from optimization.models.common import ContractValidationError, JsonValue, require_non_empty_string
from optimization.profiles import RawMetrics
from optimization.solver.callbacks import INT64_MAX
from optimization.solver.solution_parser import VRPTWSolution
from optimization.solver.vrptw_solver import VRPTWSolver

from .explanation import OptimizationExplanation, explain_result
from .cost_domain import (
    COST_DOMAIN_VERSION,
    PENALTY_POLICY_VERSION,
    SCALING_VERSION,
    ObjectiveCostDomain,
    ObjectiveCostDomainError,
)
from .fleet_validation import FLEET_VALIDATION_VERSION, FleetObjectiveBound
from .profiles import CalibrationMetadata, PhaseE3Configuration, ProfileObjectiveSpec
from .scoring import (
    ObjectiveBreakdown,
    ObjectiveIntegerScaler,
    ObjectiveRecord,
    RouteObjectiveRecord,
    ScoredMetrics,
    SolverObjectiveRecord,
    ranking_value,
    score_raw_metrics,
)


class ProfileObjectiveCostAdapter:
    """Register one lexicographically packed, normalized OR-Tools arc cost."""

    cost_domain_version = COST_DOMAIN_VERSION
    scaling_version = SCALING_VERSION
    penalty_policy_version = PENALTY_POLICY_VERSION
    fleet_validation_version = FLEET_VALIDATION_VERSION

    def __init__(self, configuration: PhaseE3Configuration, profile: ProfileObjectiveSpec) -> None:
        if not isinstance(configuration, PhaseE3Configuration):
            raise ContractValidationError("configuration must be PhaseE3Configuration")
        if not isinstance(profile, ProfileObjectiveSpec):
            raise ContractValidationError("profile must be ProfileObjectiveSpec")
        self.configuration = configuration
        self.profile = profile
        self.scaler = ObjectiveIntegerScaler(
            configuration.objective.integer_scaling_factor,
            configuration.objective.rounding_policy,
        )
        self.cost_mode = f"PROFILE_OBJECTIVE:{profile.profile.value}:{configuration.objective.objective_version}"
        self._multipliers: tuple[int, ...] = ()
        self._cost_domain: ObjectiveCostDomain | None = None
        self._fleet_bound: FleetObjectiveBound | None = None

    @property
    def cost_domain(self) -> ObjectiveCostDomain:
        if self._cost_domain is None:
            raise ObjectiveCostDomainError("objective cost domain is unavailable before callback registration")
        return self._cost_domain

    def unassigned_order_penalty(self) -> int:
        """Return a disjunction penalty in the same packed integer domain as arcs."""

        return self.cost_domain.drop_penalty

    @property
    def fleet_bound(self) -> FleetObjectiveBound:
        if self._fleet_bound is None:
            raise ObjectiveCostDomainError("fleet objective validation is unavailable before callback registration")
        return self._fleet_bound

    def _score(self, matrix: MatrixBundle, row: int, column: int, service_time: float) -> ScoredMetrics:
        distance = matrix.distance_matrix[row][column]
        return score_raw_metrics(
            RawMetrics(
                distance=distance,
                time=matrix.time_matrix[row][column] + service_time,
                length=distance,
                risk=matrix.risk_matrix[row][column],
            ),
            self.configuration.normalization,
            self.profile,
        )

    def _scaled_vector(self, scored: ScoredMetrics) -> tuple[int, ...]:
        return tuple(
            self.scaler.scale(ranking_value(scored, field))
            for field in self.profile.ranking_order
        )

    def edge_scaled_cost(self, matrix: MatrixBundle, row: int, column: int, service_time: float = 0.0) -> int:
        if not self._multipliers:
            raise ContractValidationError("objective adapter must be registered before cost extraction")
        vector = self._scaled_vector(self._score(matrix, row, column, service_time))
        cost = sum(value * multiplier for value, multiplier in zip(vector, self._multipliers))
        if cost > INT64_MAX:
            raise ObjectiveCostDomainError("packed edge objective exceeds int64")
        return cost

    def register(
        self,
        *,
        routing: Any,
        manager: Any,
        matrix_bundle: MatrixBundle,
        routing_node_to_matrix_index: Sequence[int],
        service_time_by_routing_node: Mapping[int, float],
        max_route_arcs: int,
        maximum_drops: int = 0,
        number_of_vehicles: int | None = None,
        number_of_orders: int | None = None,
    ) -> int:
        if max_route_arcs <= 0:
            raise ContractValidationError("max_route_arcs must be positive")
        max_service = max(service_time_by_routing_node.values(), default=0.0)
        maxima = [0] * len(self.profile.ranking_order)
        for row in range(len(matrix_bundle.node_order)):
            for column in range(len(matrix_bundle.node_order)):
                vector = self._scaled_vector(self._score(matrix_bundle, row, column, max_service))
                maxima = [max(current, value) for current, value in zip(maxima, vector)]
        total_bounds = [value * max_route_arcs for value in maxima]
        multipliers = [1] * len(total_bounds)
        for index in range(len(total_bounds) - 2, -1, -1):
            multipliers[index] = multipliers[index + 1] * (total_bounds[index + 1] + 1)
            if multipliers[index] > INT64_MAX:
                raise ObjectiveCostDomainError("objective tie-break multiplier exceeds int64")
        self._multipliers = tuple(multipliers)
        worst_edge = sum(value * multiplier for value, multiplier in zip(maxima, multipliers))
        if worst_edge * max_route_arcs > INT64_MAX:
            raise ObjectiveCostDomainError("fleet objective can exceed OR-Tools int64 capacity")
        self._cost_domain = ObjectiveCostDomain.derive(
            integer_scaling_factor=self.scaler.factor,
            rounding_policy=self.scaler.rounding_policy,
            maximum_edge_cost=worst_edge,
            maximum_route_arcs=max_route_arcs,
            maximum_drops=maximum_drops,
        )
        if (number_of_vehicles is None) != (number_of_orders is None):
            raise ObjectiveCostDomainError("vehicle and order counts must be supplied together")
        if number_of_vehicles is not None and number_of_orders is not None:
            self._fleet_bound = FleetObjectiveBound.validate(
                self._cost_domain,
                number_of_vehicles=number_of_vehicles,
                number_of_orders=number_of_orders,
                maximum_drops=maximum_drops,
            )

        def objective_callback(from_index: int, to_index: int) -> int:
            from_routing_node = manager.IndexToNode(from_index)
            to_routing_node = manager.IndexToNode(to_index)
            row = routing_node_to_matrix_index[from_routing_node]
            column = routing_node_to_matrix_index[to_routing_node]
            return self.edge_scaled_cost(
                matrix_bundle,
                row,
                column,
                service_time_by_routing_node.get(from_routing_node, 0.0),
            )

        return routing.RegisterTransitCallback(objective_callback)


@dataclass(frozen=True, slots=True)
class NormalizationMetadata:
    normalization_version: str
    normalization_source: str
    calibration: CalibrationMetadata
    distance_reference: float
    time_reference: float
    length_reference: float
    risk_reference: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "normalization_version": self.normalization_version,
            "normalization_source": self.normalization_source,
            **self.calibration.to_dict(),
            "references": {
                "distance": self.distance_reference,
                "time": self.time_reference,
                "length": self.length_reference,
                "risk": self.risk_reference,
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NormalizationMetadata":
        refs = payload["references"]
        return cls(
            payload["normalization_version"],
            payload["normalization_source"],
            CalibrationMetadata(payload["calibration_source"], payload["calibration_version"], payload["calibration_date"]),
            refs["distance"], refs["time"], refs["length"], refs["risk"],
        )


@dataclass(frozen=True, slots=True)
class OptimizationExperimentResult:
    scenario_id: str
    profile: OptimizationProfile
    route_result: VRPTWSolution
    objective: ObjectiveRecord
    metrics: ScoredMetrics
    explanation: OptimizationExplanation
    normalization_metadata: NormalizationMetadata
    objective_version: str
    profile_config_version: str
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", require_non_empty_string(self.scenario_id, "scenario_id"))
        if not isinstance(self.profile, OptimizationProfile):
            object.__setattr__(self, "profile", OptimizationProfile(self.profile))
        for name in ("objective_version", "profile_config_version", "schema_version"):
            object.__setattr__(self, name, require_non_empty_string(getattr(self, name), name))
        if self.route_result.scenario_id != self.scenario_id:
            raise ContractValidationError("experiment and route scenario IDs must match")
        if self.objective.profile != self.profile.value:
            raise ContractValidationError("experiment and objective profiles must match")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "profile": self.profile.value,
            "route_result": self.route_result.to_dict(),
            "objective": self.objective.to_dict(),
            "metrics": self.metrics.to_dict(),
            "explanation": self.explanation.to_dict(),
            "solver_metadata": self.route_result.experiment_metadata.to_dict(),
            "normalization_metadata": self.normalization_metadata.to_dict(),
            "objective_version": self.objective_version,
            "profile_config_version": self.profile_config_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OptimizationExperimentResult":
        return cls(
            payload["scenario_id"],
            OptimizationProfile(payload["profile"]),
            VRPTWSolution.from_dict(payload["route_result"]),
            ObjectiveRecord.from_dict(payload["objective"]),
            ScoredMetrics.from_dict(payload["metrics"]),
            OptimizationExplanation.from_dict(payload["explanation"]),
            NormalizationMetadata.from_dict(payload["normalization_metadata"]),
            payload["objective_version"],
            payload["profile_config_version"],
            payload.get("schema_version", "1.0.0"),
        )


@dataclass(frozen=True, slots=True)
class ProfileOptimizationResponse:
    status: str
    result: OptimizationExperimentResult | None
    diagnostics: tuple[Diagnostic, ...]

    @property
    def is_valid(self) -> bool:
        return self.result is not None and not any(
            item.severity in {DiagnosticSeverity.ERROR, DiagnosticSeverity.FATAL}
            for item in self.diagnostics
        )


@dataclass(frozen=True, slots=True)
class ProfileComparisonResult:
    results: tuple[OptimizationExperimentResult, ...]
    diagnostics: tuple[Diagnostic, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "results": [item.to_dict() for item in self.results],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


class ProfileAwareOptimizer:
    """Orchestrate configuration-driven profile cost without changing feasibility."""

    def __init__(self, solver: VRPTWSolver, configuration: PhaseE3Configuration) -> None:
        if not isinstance(solver, VRPTWSolver):
            raise ContractValidationError("solver must be VRPTWSolver")
        if not isinstance(configuration, PhaseE3Configuration):
            raise ContractValidationError("configuration must be PhaseE3Configuration")
        self.solver = solver
        self.configuration = configuration

    @staticmethod
    def _error(code: str, message: str, details: Mapping[str, Any]) -> Diagnostic:
        return Diagnostic(code, DiagnosticSeverity.ERROR, message, details, {"component": "profile_objective"})

    def optimize(
        self,
        profile: OptimizationProfile | str,
        matrix_bundle: MatrixBundle,
        scenario: Any,
        vehicles: Iterable[Any],
        orders: Iterable[Any],
    ) -> ProfileOptimizationResponse:
        try:
            selected_profile = profile if isinstance(profile, OptimizationProfile) else OptimizationProfile(profile)
        except (ValueError, TypeError):
            diagnostic = self._error("INVALID_OBJECTIVE_CONFIGURATION", "Unknown optimization profile.", {"profile": repr(profile)})
            return ProfileOptimizationResponse("INVALID_OBJECTIVE_CONFIGURATION", None, (diagnostic,))
        specification = self.configuration.objective.profiles[selected_profile]
        risk_used = specification.weights.risk > 0 or "risk" in specification.ranking_order
        if risk_used and matrix_bundle.source.get("risk_available") is False:
            diagnostic = self._error("INVALID_RISK_CONFIGURATION", "Configured profile requires raw exposure data, but MatrixBundle marks risk unavailable.", {"profile": selected_profile.value})
            return ProfileOptimizationResponse("INVALID_RISK_CONFIGURATION", None, (diagnostic,))
        vehicle_items, order_items = tuple(vehicles), tuple(orders)
        adapter = ProfileObjectiveCostAdapter(self.configuration, specification)
        route_result = self.solver.solve(
            matrix_bundle,
            scenario,
            vehicle_items,
            order_items,
            objective_adapter=adapter,
        )
        if not route_result.is_feasible:
            return ProfileOptimizationResponse(route_result.business_status, None, route_result.diagnostics)
        try:
            experiment = self._build_result(route_result, matrix_bundle, specification, adapter)
        except (ContractValidationError, KeyError, TypeError, ValueError) as error:
            diagnostic = self._error("INVALID_OBJECTIVE_CONFIGURATION", "Objective result could not be constructed.", {"reason": str(error)})
            return ProfileOptimizationResponse("INVALID_OBJECTIVE_CONFIGURATION", None, route_result.diagnostics + (diagnostic,))
        return ProfileOptimizationResponse(route_result.business_status, experiment, route_result.diagnostics)

    def _build_result(
        self,
        solution: VRPTWSolution,
        matrix: MatrixBundle,
        specification: ProfileObjectiveSpec,
        adapter: ProfileObjectiveCostAdapter,
    ) -> OptimizationExperimentResult:
        route_records: list[RouteObjectiveRecord] = []
        for route in solution.vehicle_routes:
            solver_scored = score_raw_metrics(
                RawMetrics(route.distance, route.travel_time + route.service_time, route.distance, route.risk_exposure),
                self.configuration.normalization,
                specification,
            )
            evaluation_scored = score_raw_metrics(
                RawMetrics(route.distance, route.time, route.distance, route.risk_exposure),
                self.configuration.normalization,
                specification,
            )
            route_scaled = 0
            for edge_index, edge in enumerate(route.edge_metrics):
                service = 0.0
                if edge_index > 0 and edge_index - 1 < len(route.stops):
                    stop = route.stops[edge_index - 1]
                    service = stop.departure_time - stop.arrival_time
                route_scaled += adapter.edge_scaled_cost(matrix, edge.matrix_row, edge.matrix_column, service)
            route_records.append(RouteObjectiveRecord(route.vehicle_id, solver_scored, evaluation_scored, route_scaled))

        fleet_distance = sum(item.evaluation_metrics.raw_metrics.distance for item in route_records)
        fleet_time = sum(item.evaluation_metrics.raw_metrics.time for item in route_records)
        fleet_risk = sum(item.evaluation_metrics.raw_metrics.risk for item in route_records)
        fleet_scored = score_raw_metrics(
            RawMetrics(fleet_distance, fleet_time, fleet_distance, fleet_risk),
            self.configuration.normalization,
            specification,
        )
        evaluation_breakdown = ObjectiveBreakdown(
            sum(item.evaluation_metrics.breakdown.time_component for item in route_records),
            sum(item.evaluation_metrics.breakdown.distance_component for item in route_records),
            sum(item.evaluation_metrics.breakdown.risk_component for item in route_records),
            sum(item.evaluation_metrics.breakdown.total_score for item in route_records),
        )
        solver_raw = sum(item.solver_metrics.breakdown.total_score for item in route_records)
        route_scaled = sum(item.solver_scaled_cost for item in route_records)
        exact_scaled = solution.experiment_metadata.routing_objective_value
        if exact_scaled is None or exact_scaled < route_scaled:
            raise ContractValidationError("exact OR-Tools objective is unavailable or below route aggregation")
        objective = ObjectiveRecord(
            self.configuration.objective.objective_version,
            self.configuration.objective.scoring_logic_version,
            specification.profile.value,
            SolverObjectiveRecord(
                solver_raw,
                exact_scaled,
                route_scaled,
                exact_scaled - route_scaled,
                adapter.cost_domain.cost_domain_version,
                adapter.cost_domain.scaling_version,
                adapter.cost_domain.penalty_policy_version,
                adapter.fleet_bound.fleet_validation_version,
            ),
            evaluation_breakdown,
            tuple(route_records),
        )
        normalization = self.configuration.normalization
        normalization_metadata = NormalizationMetadata(
            normalization.normalization_version,
            normalization.normalization_source,
            self.configuration.calibration,
            normalization.distance_reference,
            normalization.time_reference,
            normalization.length_reference,
            normalization.risk_reference,
        )
        explanation = explain_result(
            profile=specification.profile.value,
            distance=fleet_distance,
            time=fleet_time,
            risk=fleet_risk,
            objective=evaluation_breakdown.total_score,
        )
        return OptimizationExperimentResult(
            solution.scenario_id,
            specification.profile,
            solution,
            objective,
            fleet_scored,
            explanation,
            normalization_metadata,
            self.configuration.objective.objective_version,
            self.configuration.objective.profile_config_version,
        )

    def compare_profiles(
        self,
        matrix_bundle: MatrixBundle,
        scenario: Any,
        vehicles: Iterable[Any],
        orders: Iterable[Any],
    ) -> ProfileComparisonResult:
        vehicle_items, order_items = tuple(vehicles), tuple(orders)
        results: list[OptimizationExperimentResult] = []
        diagnostics: list[Diagnostic] = []
        for profile in (OptimizationProfile.FASTEST, OptimizationProfile.BALANCED, OptimizationProfile.SAFER):
            response = self.optimize(profile, matrix_bundle, scenario, vehicle_items, order_items)
            diagnostics.extend(response.diagnostics)
            if response.result is None:
                return ProfileComparisonResult((), tuple(diagnostics))
            results.append(response.result)
        baseline = results[0]
        explained: list[OptimizationExperimentResult] = []
        for result in results:
            if result is baseline:
                explained.append(result)
                continue
            explanation = explain_result(
                profile=result.profile.value,
                distance=result.metrics.raw_metrics.distance,
                time=result.metrics.raw_metrics.time,
                risk=result.metrics.raw_metrics.risk,
                objective=result.objective.evaluation_objective.total_score,
                compared_profile=baseline.profile.value,
                compared_distance=baseline.metrics.raw_metrics.distance,
                compared_time=baseline.metrics.raw_metrics.time,
                compared_risk=baseline.metrics.raw_metrics.risk,
                compared_objective=baseline.objective.evaluation_objective.total_score,
            )
            explained.append(replace(result, explanation=explanation))
        diagnostics.append(
            Diagnostic(
                "PROFILE_COMPARISON_COMPLETE",
                DiagnosticSeverity.INFO,
                "FASTEST, BALANCED, and SAFER were solved with identical non-profile inputs.",
                {"matrix_version": matrix_bundle.matrix_version, "objective_version": self.configuration.objective.objective_version},
                {"component": "profile_objective"},
            )
        )
        return ProfileComparisonResult(tuple(explained), tuple(diagnostics))
