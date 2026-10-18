"""Real Google OR-Tools VRPTW model builder and solver orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Iterable, Mapping, Sequence

from optimization.config_loader import ConfigurationError, load_config_bundle
from optimization.matrix import validate_matrix_bundle
from optimization.models import (
    ContractValidationError,
    Diagnostic,
    DiagnosticSeverity,
    MatrixBundle,
)
from optimization.models.common import require_non_empty_string, validate_finite_number

from .callbacks import IntegerScalingPolicy, register_callbacks
from .constraints import (
    Order,
    Scenario,
    Vehicle,
    attach_constraints,
    validate_feasibility_contracts,
)
from .solution_parser import (
    SolverExperimentMetadata,
    VRPTWSolution,
    failure_solution,
    parse_solution,
)
from .solution_validator import validate_solution


@dataclass(frozen=True, slots=True)
class SolverOutcome:
    feasibility_status: str
    timeout_outcome: str | None
    best_solution_found: bool
    solution_quality: str


def classify_solver_outcome(
    solver_status: str,
    assignment_present: bool,
) -> SolverOutcome:
    """Map OR-Tools status to the stable public timeout/quality contract."""

    timed_out = solver_status in {
        "ROUTING_FAIL_TIMEOUT",
        "ROUTING_PARTIAL_SUCCESS_LOCAL_OPTIMUM_NOT_REACHED",
    }
    if timed_out:
        return SolverOutcome(
            "TIME_LIMIT",
            "BEST_FEASIBLE_SOLUTION" if assignment_present else "TIME_LIMIT_NO_SOLUTION",
            assignment_present,
            "FEASIBLE" if assignment_present else "NO_SOLUTION",
        )
    if solver_status in {"ROUTING_FAIL", "ROUTING_INFEASIBLE"}:
        return SolverOutcome("INFEASIBLE", None, False, "NO_SOLUTION")
    if solver_status in {"ROUTING_INVALID", "ROUTING_NOT_SOLVED"}:
        return SolverOutcome("ERROR", None, False, "NO_SOLUTION")
    if assignment_present and solver_status in {"ROUTING_SUCCESS", "ROUTING_OPTIMAL"}:
        return SolverOutcome("FEASIBLE", None, True, "FEASIBLE")
    return SolverOutcome("ERROR", None, assignment_present, "INVALID_ASSIGNMENT_STATE")


def _routing_status_name(routing_enums_pb2: Any, status: Any) -> str:
    try:
        return routing_enums_pb2.RoutingSearchStatus.Name(int(status))
    except (AttributeError, KeyError, TypeError, ValueError):
        try:
            return routing_enums_pb2.RoutingSearchStatus.Value.Name(int(status))
        except (AttributeError, KeyError, TypeError, ValueError):
            return f"ROUTING_STATUS_{int(status)}"


@dataclass(frozen=True, slots=True)
class SolverConfig:
    schema_version: str
    solver_version: str
    ortools_version: str
    first_solution_strategy: str
    local_search_metaheuristic: str
    time_limit_seconds: int
    random_seed: int
    log_search: bool
    max_waiting_time_seconds: float
    unassigned_order_penalty_meters: float
    scaling: IntegerScalingPolicy

    def __post_init__(self) -> None:
        for field_name in (
            "schema_version",
            "solver_version",
            "ortools_version",
            "first_solution_strategy",
            "local_search_metaheuristic",
        ):
            object.__setattr__(
                self,
                field_name,
                require_non_empty_string(getattr(self, field_name), field_name),
            )
        if (
            isinstance(self.time_limit_seconds, bool)
            or not isinstance(self.time_limit_seconds, int)
            or self.time_limit_seconds <= 0
        ):
            raise ContractValidationError("time_limit_seconds must be a positive integer")
        if (
            isinstance(self.random_seed, bool)
            or not isinstance(self.random_seed, int)
            or self.random_seed < 0
        ):
            raise ContractValidationError("random_seed must be a non-negative integer")
        if not isinstance(self.log_search, bool):
            raise ContractValidationError("log_search must be boolean")
        object.__setattr__(
            self,
            "max_waiting_time_seconds",
            validate_finite_number(
                self.max_waiting_time_seconds,
                "max_waiting_time_seconds",
                minimum=0.0,
            ),
        )
        penalty = validate_finite_number(
            self.unassigned_order_penalty_meters,
            "unassigned_order_penalty_meters",
            minimum=0.0,
        )
        if penalty == 0.0:
            raise ContractValidationError(
                "unassigned_order_penalty_meters must be greater than zero"
            )
        object.__setattr__(self, "unassigned_order_penalty_meters", penalty)
        if not isinstance(self.scaling, IntegerScalingPolicy):
            raise ContractValidationError("scaling must be IntegerScalingPolicy")

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "SolverConfig":
        if not isinstance(document, Mapping):
            raise ContractValidationError("solver configuration must be a mapping")
        solver = document.get("solver")
        if not isinstance(solver, Mapping):
            raise ContractValidationError("solver section must be a mapping")
        scaling = solver.get("integer_scaling")
        if not isinstance(scaling, Mapping):
            raise ContractValidationError("solver.integer_scaling must be a mapping")
        return cls(
            schema_version=document.get("schema_version"),
            solver_version=solver.get("solver_version"),
            ortools_version=solver.get("ortools_version"),
            first_solution_strategy=solver.get("first_solution_strategy"),
            local_search_metaheuristic=solver.get("local_search_metaheuristic"),
            time_limit_seconds=solver.get("time_limit_seconds"),
            random_seed=solver.get("random_seed"),
            log_search=solver.get("log_search"),
            max_waiting_time_seconds=solver.get("max_waiting_time_seconds"),
            unassigned_order_penalty_meters=solver.get(
                "unassigned_order_penalty_meters"
            ),
            scaling=IntegerScalingPolicy(
                distance=scaling.get("distance"),
                time=scaling.get("time"),
                risk=scaling.get("risk"),
                demand=scaling.get("demand"),
                rounding_policy=scaling.get("rounding_policy"),
            ),
        )


@dataclass(frozen=True, slots=True)
class SolverConfigLoadResult:
    config: SolverConfig | None
    diagnostics: tuple[Diagnostic, ...]

    @property
    def is_valid(self) -> bool:
        return self.config is not None and not any(
            item.severity == DiagnosticSeverity.ERROR for item in self.diagnostics
        )


@dataclass(frozen=True, slots=True)
class NodeMapping:
    """Explicit virtual routing-node to MatrixBundle index mapping."""

    routing_node_to_matrix_index: tuple[int, ...]
    order_routing_nodes: Mapping[str, int]
    order_id_by_routing_node: Mapping[int, str]
    vehicle_start_routing_nodes: tuple[int, ...]
    vehicle_end_routing_nodes: tuple[int, ...]

    @classmethod
    def build(
        cls,
        bundle: MatrixBundle,
        vehicles: Sequence[Vehicle],
        orders: Sequence[Order],
    ) -> "NodeMapping":
        matrix_indices = {node: index for index, node in enumerate(bundle.node_order)}
        mapping: list[int] = []
        order_nodes: dict[str, int] = {}
        order_ids: dict[int, str] = {}
        for order in orders:
            routing_node = len(mapping)
            mapping.append(matrix_indices[order.location])
            order_nodes[order.order_id] = routing_node
            order_ids[routing_node] = order.order_id

        anchor_nodes: dict[Any, int] = {}

        def anchor(application_node: Any) -> int:
            if application_node not in anchor_nodes:
                anchor_nodes[application_node] = len(mapping)
                mapping.append(matrix_indices[application_node])
            return anchor_nodes[application_node]

        starts = tuple(anchor(vehicle.start_node) for vehicle in vehicles)
        ends = tuple(anchor(vehicle.end_node) for vehicle in vehicles)
        return cls(tuple(mapping), order_nodes, order_ids, starts, ends)


def load_solver_config(config_directory: str | Path) -> SolverConfigLoadResult:
    try:
        bundle = load_config_bundle(config_directory)
        config = SolverConfig.from_document(bundle.get("solver.yaml"))
    except (ConfigurationError, ContractValidationError, TypeError, ValueError) as error:
        return SolverConfigLoadResult(
            None,
            (
                Diagnostic(
                    code="INVALID_SOLVER_CONFIGURATION",
                    severity=DiagnosticSeverity.ERROR,
                    message="Solver configuration could not be loaded.",
                    details={"reason": str(error)},
                ),
            ),
        )
    return SolverConfigLoadResult(
        config,
        (
            Diagnostic(
                code="SOLVER_CONFIGURATION_VALID",
                severity=DiagnosticSeverity.INFO,
                message="Solver configuration passed validation.",
                details={"solver_version": config.solver_version},
            ),
        ),
    )


class VRPTWSolver:
    """Build and solve a distance-cost VRPTW with real Google OR-Tools."""

    def __init__(self, config: SolverConfig) -> None:
        if not isinstance(config, SolverConfig):
            raise ContractValidationError("config must be SolverConfig")
        self.config = config

    @classmethod
    def from_config_directory(cls, config_directory: str | Path) -> "VRPTWSolver":
        loaded = load_solver_config(config_directory)
        if loaded.config is None:
            raise ContractValidationError(loaded.diagnostics[0].message)
        return cls(loaded.config)

    def _experiment_metadata(
        self,
        *,
        timestamp: str,
        elapsed: float,
        vehicle_count: int,
        order_count: int,
        solver_status: str,
        outcome: SolverOutcome,
        ortools_version: str | None = None,
        routing_cost_mode: str = "DISTANCE",
        routing_objective_value: int | None = None,
        cost_domain: Any | None = None,
    ) -> SolverExperimentMetadata:
        return SolverExperimentMetadata(
            ortools_version=ortools_version or self.config.ortools_version,
            solver_config_version=self.config.solver_version,
            random_seed=self.config.random_seed,
            time_limit_seconds=self.config.time_limit_seconds,
            first_solution_strategy=self.config.first_solution_strategy,
            local_search_metaheuristic=self.config.local_search_metaheuristic,
            execution_timestamp=timestamp,
            elapsed_time_seconds=max(0.0, elapsed),
            number_of_vehicles=vehicle_count,
            number_of_orders=order_count,
            problem_size=vehicle_count + order_count,
            solver_status=solver_status,
            best_solution_found=outcome.best_solution_found,
            solution_quality=outcome.solution_quality,
            timeout_outcome=outcome.timeout_outcome,
            routing_cost_mode=routing_cost_mode,
            routing_objective_value=routing_objective_value,
            cost_domain_version=(
                cost_domain.cost_domain_version
                if cost_domain is not None else "legacy-distance-cost-domain-e26-v1"
            ),
            scaling_version=(
                cost_domain.scaling_version
                if cost_domain is not None else "distance-scaling-e26-v1"
            ),
            penalty_policy_version=(
                cost_domain.penalty_policy_version
                if cost_domain is not None else "distance-scaled-disjunction-e26-v1"
            ),
            fleet_validation_version=(
                getattr(cost_domain, "fleet_validation_version", "legacy-unvalidated-fleet-domain-v1")
                if cost_domain is not None else "legacy-unvalidated-fleet-domain-v1"
            ),
        )

    def solve(
        self,
        matrix_bundle: MatrixBundle,
        scenario: Scenario | Mapping[str, Any],
        vehicles: Iterable[Vehicle | Mapping[str, Any]],
        orders: Iterable[Order | Mapping[str, Any]],
        *,
        objective_adapter: Any | None = None,
    ) -> VRPTWSolution:
        started = monotonic()
        timestamp = datetime.now(timezone.utc).isoformat()
        diagnostics: list[Diagnostic] = []
        scenario_id = (
            scenario.scenario_id
            if isinstance(scenario, Scenario)
            else str(scenario.get("scenario_id", "UNKNOWN"))
            if isinstance(scenario, Mapping)
            else "UNKNOWN"
        )
        matrix_version = (
            matrix_bundle.matrix_version
            if isinstance(matrix_bundle, MatrixBundle)
            else "UNKNOWN"
        )
        context_version = (
            str(matrix_bundle.source.get("context_version", "UNVERSIONED"))
            if isinstance(matrix_bundle, MatrixBundle)
            else "UNVERSIONED"
        )
        scenario_version = (
            scenario.scenario_version
            if isinstance(scenario, Scenario)
            else str(scenario.get("scenario_version", "UNVERSIONED"))
            if isinstance(scenario, Mapping)
            else "UNVERSIONED"
        )
        parsed_vehicles: tuple[Vehicle, ...] = ()
        parsed_orders: tuple[Order, ...] = ()
        routing_cost_mode = (
            str(getattr(objective_adapter, "cost_mode", "PROFILE_OBJECTIVE"))
            if objective_adapter is not None
            else "DISTANCE"
        )

        def fail(
            status: str,
            reason: str,
            *,
            solver_status: str,
            outcome: SolverOutcome | None = None,
            ortools_version: str | None = None,
        ) -> VRPTWSolution:
            resolved_outcome = outcome or SolverOutcome(
                status, None, False, "NO_SOLUTION"
            )
            return failure_solution(
                scenario_id=scenario_id,
                scenario_version=scenario_version,
                context_version=context_version,
                status=status,
                orders=parsed_orders,
                reason=reason,
                diagnostics=diagnostics,
                solver_version=ortools_version or self.config.ortools_version,
                solver_config_version=self.config.solver_version,
                matrix_version=matrix_version,
                experiment_metadata=self._experiment_metadata(
                    timestamp=timestamp,
                    elapsed=monotonic() - started,
                    vehicle_count=len(parsed_vehicles),
                    order_count=len(parsed_orders),
                    solver_status=solver_status,
                    outcome=resolved_outcome,
                    ortools_version=ortools_version,
                    routing_cost_mode=routing_cost_mode,
                    cost_domain=objective_adapter,
                ),
                metrics_available=isinstance(matrix_bundle, MatrixBundle),
            )
        try:
            resolved_scenario = (
                scenario if isinstance(scenario, Scenario) else Scenario.from_mapping(scenario)
            )
            raw_vehicles = tuple(vehicles)
            raw_orders = tuple(orders)
            parsed_vehicles = tuple(
                item
                if isinstance(item, Vehicle)
                else Vehicle.from_mapping(item, resolved_scenario.depot.node_id)
                for item in raw_vehicles
            )
            parsed_vehicles = tuple(vehicle for vehicle in parsed_vehicles if vehicle.available)
            parsed_orders = tuple(
                item if isinstance(item, Order) else Order.from_mapping(item)
                for item in raw_orders
            )
            if not isinstance(matrix_bundle, MatrixBundle):
                raise ContractValidationError("matrix_bundle must be MatrixBundle")
            matrix_diagnostics: list[Diagnostic] = []
            if not validate_matrix_bundle(
                matrix_bundle, diagnostics=matrix_diagnostics
            ):
                diagnostics.extend(matrix_diagnostics)
                raise ContractValidationError("MatrixBundle compatibility validation failed")
            validate_feasibility_contracts(
                matrix_bundle,
                resolved_scenario,
                parsed_vehicles,
                parsed_orders,
            )
        except Exception as error:  # Public boundary returns diagnostics for malformed input.
            diagnostics.append(
                Diagnostic(
                    code="INVALID_SOLVER_INPUT",
                    severity=DiagnosticSeverity.ERROR,
                    message="VRPTW input validation failed.",
                    details={"reason": str(error)},
                )
            )
            return fail("INVALID_INPUT", "invalid_input", solver_status="INPUT_VALIDATION_FAILED")

        total_demand = sum(order.demand for order in parsed_orders)
        residual_capacity = sum(
            vehicle.capacity - vehicle.current_load for vehicle in parsed_vehicles
        )
        impossible_deadlines = [
            order.order_id
            for order in parsed_orders
            if order.effective_window[0] > order.effective_window[1]
        ]
        if not resolved_scenario.allow_unassigned_orders and (
            total_demand > residual_capacity or impossible_deadlines
        ):
            code = (
                "INSUFFICIENT_CAPACITY"
                if total_demand > residual_capacity
                else "IMPOSSIBLE_DEADLINE"
            )
            diagnostics.append(
                Diagnostic(
                    code=code,
                    severity=DiagnosticSeverity.ERROR,
                    message="Mandatory-order feasibility precheck failed.",
                    details={
                        "total_demand": total_demand,
                        "residual_capacity": residual_capacity,
                        "orders": impossible_deadlines,
                    },
                )
            )
            return fail("INFEASIBLE", code.lower(), solver_status="PRECHECK_INFEASIBLE")

        try:
            import ortools
            from ortools.constraint_solver import pywrapcp, routing_enums_pb2
        except (ImportError, OSError) as error:
            diagnostics.append(
                Diagnostic(
                    code="ORTOOLS_UNAVAILABLE",
                    severity=DiagnosticSeverity.FATAL,
                    message="Google OR-Tools is required for Phase E.1/E.2.",
                    details={"reason": str(error), "required": self.config.ortools_version},
                    context={"component": "vrptw_solver", "stage": "dependency_load"},
                )
            )
            return fail("ERROR", "ortools_unavailable", solver_status="ORTOOLS_UNAVAILABLE")

        if ortools.__version__ != self.config.ortools_version:
            diagnostics.append(
                Diagnostic(
                    code="ORTOOLS_VERSION_MISMATCH",
                    severity=DiagnosticSeverity.FATAL,
                    message="Runtime OR-Tools version differs from the pinned solver contract.",
                    details={
                        "required": self.config.ortools_version,
                        "actual": ortools.__version__,
                    },
                    context={"component": "vrptw_solver", "stage": "dependency_validation"},
                )
            )
            return fail("ERROR", "ortools_version_mismatch", solver_status="ORTOOLS_VERSION_MISMATCH", ortools_version=ortools.__version__)

        try:
            node_mapping = NodeMapping.build(
                matrix_bundle, parsed_vehicles, parsed_orders
            )
            manager = pywrapcp.RoutingIndexManager(
                len(node_mapping.routing_node_to_matrix_index),
                len(parsed_vehicles),
                list(node_mapping.vehicle_start_routing_nodes),
                list(node_mapping.vehicle_end_routing_nodes),
            )
            routing = pywrapcp.RoutingModel(manager)
            service_times = {
                routing_node: next(
                    order.service_time
                    for order in parsed_orders
                    if order.order_id == order_id
                )
                for order_id, routing_node in node_mapping.order_routing_nodes.items()
            }
            callbacks = register_callbacks(
                routing,
                manager,
                matrix_bundle,
                node_mapping.routing_node_to_matrix_index,
                service_times,
                self.config.scaling,
            )
            if objective_adapter is None:
                routing.SetArcCostEvaluatorOfAllVehicles(callbacks.distance_index)
            else:
                objective_callback_index = objective_adapter.register(
                    routing=routing,
                    manager=manager,
                    matrix_bundle=matrix_bundle,
                    routing_node_to_matrix_index=node_mapping.routing_node_to_matrix_index,
                    service_time_by_routing_node=service_times,
                    max_route_arcs=len(parsed_orders) + len(parsed_vehicles),
                    maximum_drops=len(parsed_orders) if resolved_scenario.allow_unassigned_orders else 0,
                    number_of_vehicles=len(parsed_vehicles),
                    number_of_orders=len(parsed_orders),
                )
                routing.SetArcCostEvaluatorOfAllVehicles(objective_callback_index)

            if resolved_scenario.allow_unassigned_orders:
                penalty = (
                    objective_adapter.unassigned_order_penalty()
                    if objective_adapter is not None
                    else self.config.scaling.to_solver(
                        self.config.unassigned_order_penalty_meters, "distance"
                    )
                )
                for routing_node in node_mapping.order_routing_nodes.values():
                    routing.AddDisjunction([manager.NodeToIndex(routing_node)], penalty)

            dimensions = attach_constraints(
                routing=routing,
                manager=manager,
                time_callback_index=callbacks.time_index,
                order_routing_nodes=node_mapping.order_routing_nodes,
                order_by_id={order.order_id: order for order in parsed_orders},
                vehicles=parsed_vehicles,
                scenario=resolved_scenario,
                scaling=self.config.scaling,
                max_waiting_time_seconds=self.config.max_waiting_time_seconds,
            )
            search_parameters = pywrapcp.DefaultRoutingSearchParameters()
            try:
                search_parameters.first_solution_strategy = getattr(
                    routing_enums_pb2.FirstSolutionStrategy,
                    self.config.first_solution_strategy,
                )
                search_parameters.local_search_metaheuristic = getattr(
                    routing_enums_pb2.LocalSearchMetaheuristic,
                    self.config.local_search_metaheuristic,
                )
            except AttributeError as error:
                raise ContractValidationError(
                    "solver configuration contains an unknown OR-Tools strategy"
                ) from error
            search_parameters.time_limit.seconds = self.config.time_limit_seconds
            search_parameters.log_search = self.config.log_search
            search_parameters.sat_parameters.random_seed = self.config.random_seed
            search_parameters.sat_parameters.num_workers = 1
            diagnostics.append(
                Diagnostic(
                    code="SOLVER_STARTED",
                    severity=DiagnosticSeverity.INFO,
                    message="Google OR-Tools RoutingModel solve started.",
                    details={
                        "solver_version": self.config.solver_version,
                        "ortools_version": ortools.__version__,
                        "matrix_version": matrix_bundle.matrix_version,
                        "first_solution_strategy": self.config.first_solution_strategy,
                        "local_search_metaheuristic": self.config.local_search_metaheuristic,
                        "random_seed": self.config.random_seed,
                        "distance_cost_only": objective_adapter is None,
                        "routing_cost_mode": routing_cost_mode,
                        "cost_domain_version": (
                            objective_adapter.cost_domain.cost_domain_version
                            if objective_adapter is not None else "legacy-distance-cost-domain-e26-v1"
                        ),
                        "penalty_policy_version": (
                            objective_adapter.cost_domain.penalty_policy_version
                            if objective_adapter is not None else "distance-scaled-disjunction-e26-v1"
                        ),
                        "fleet_validation_version": (
                            objective_adapter.fleet_bound.fleet_validation_version
                            if objective_adapter is not None else "legacy-unvalidated-fleet-domain-v1"
                        ),
                        "maximum_fleet_service_objective": (
                            objective_adapter.fleet_bound.maximum_fleet_service_objective
                            if objective_adapter is not None else None
                        ),
                        "unassigned_order_penalty": (
                            objective_adapter.unassigned_order_penalty()
                            if objective_adapter is not None and resolved_scenario.allow_unassigned_orders
                            else penalty if resolved_scenario.allow_unassigned_orders else None
                        ),
                    },
                )
            )
            assignment = routing.SolveWithParameters(search_parameters)
            raw_status = _routing_status_name(routing_enums_pb2, routing.status())
            outcome = classify_solver_outcome(raw_status, assignment is not None)
        except Exception as error:  # OR-Tools/SWIG failures must not escape the API.
            diagnostic_code = getattr(error, "diagnostic_code", "SOLVER_MODEL_ERROR")
            diagnostics.append(
                Diagnostic(
                    code=diagnostic_code,
                    severity=DiagnosticSeverity.FATAL,
                    message="OR-Tools model construction or solve failed.",
                    details={"reason": str(error)},
                    context={"component": "vrptw_solver", "stage": "model_or_solve"},
                )
            )
            return fail("ERROR", diagnostic_code.lower(), solver_status=diagnostic_code, ortools_version=ortools.__version__)

        if assignment is None:
            diagnostics.append(
                Diagnostic(
                    code="NO_FEASIBLE_SOLUTION",
                    severity=DiagnosticSeverity.ERROR,
                    message="OR-Tools found no feasible routing assignment.",
                    details={
                        "solver_status": raw_status,
                        "elapsed_time": monotonic() - started,
                        "best_solution_found": False,
                        "solution_quality": outcome.solution_quality,
                        "timeout_outcome": outcome.timeout_outcome,
                    },
                )
            )
            return fail(
                outcome.feasibility_status,
                "time_limit_no_solution" if outcome.timeout_outcome else "solver_infeasible",
                solver_status=raw_status,
                outcome=outcome,
                ortools_version=ortools.__version__,
            )

        diagnostics.append(
            Diagnostic(
                code="SOLUTION_FOUND",
                severity=DiagnosticSeverity.INFO,
                message="Google OR-Tools returned a feasible assignment.",
                details={
                    "solver_status": raw_status,
                    "elapsed_time": monotonic() - started,
                    "best_solution_found": True,
                    "solution_quality": outcome.solution_quality,
                    "timeout_outcome": outcome.timeout_outcome,
                },
            )
        )
        try:
            metadata = self._experiment_metadata(
                timestamp=timestamp,
                elapsed=monotonic() - started,
                vehicle_count=len(parsed_vehicles),
                order_count=len(parsed_orders),
                solver_status=raw_status,
                outcome=outcome,
                ortools_version=ortools.__version__,
                routing_cost_mode=routing_cost_mode,
                routing_objective_value=int(assignment.ObjectiveValue()),
                cost_domain=objective_adapter,
            )
            parsed = parse_solution(
                routing=routing,
                manager=manager,
                assignment=assignment,
                dimensions=dimensions,
                node_mapping=node_mapping,
                matrix_bundle=matrix_bundle,
                scenario=resolved_scenario,
                vehicles=parsed_vehicles,
                orders=parsed_orders,
                scaling=self.config.scaling,
                diagnostics=diagnostics,
                solver_version=ortools.__version__,
                solver_config_version=self.config.solver_version,
                feasibility_status=outcome.feasibility_status,
                experiment_metadata=metadata,
            )
            return validate_solution(
                parsed,
                matrix_bundle,
                resolved_scenario,
                parsed_vehicles,
                parsed_orders,
            ).solution
        except Exception as error:  # Preserve the no-crash solver result contract.
            diagnostics.append(
                Diagnostic(
                    code="SOLUTION_PARSER_ERROR",
                    severity=DiagnosticSeverity.FATAL,
                    message="The OR-Tools assignment could not be parsed.",
                    details={"reason": str(error)},
                    context={"component": "solution_parser", "stage": "assignment_extraction"},
                )
            )
            return fail("ERROR", "solution_parser_error", solver_status="SOLUTION_PARSER_ERROR", ortools_version=ortools.__version__)
