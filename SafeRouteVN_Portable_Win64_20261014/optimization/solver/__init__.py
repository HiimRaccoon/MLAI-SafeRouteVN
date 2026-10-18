"""Real Google OR-Tools VRPTW core and constraint engine."""

from .callbacks import IntegerScalingPolicy, RegisteredCallbacks
from .constraints import Depot, Order, Scenario, Vehicle
from .solution_parser import (
    ConstraintStatus,
    NormalizedReadyMetrics,
    RouteEdgeMetric,
    RouteStop,
    SolverExperimentMetadata,
    UnservedOrder,
    VehicleUtilization,
    VehicleRoute,
    VRPTWSolution,
)
from .solution_validator import SolutionValidationResult, validate_solution
from .vrptw_solver import (
    NodeMapping,
    SolverConfig,
    SolverConfigLoadResult,
    SolverOutcome,
    VRPTWSolver,
    classify_solver_outcome,
    load_solver_config,
)
from .objective import (
    ObjectiveConfigurationLoadResult,
    OptimizationExperimentResult,
    ProfileAwareOptimizer,
    ProfileComparisonResult,
    ProfileOptimizationResponse,
    load_objective_configuration,
)

__all__ = [
    "Depot",
    "ConstraintStatus",
    "IntegerScalingPolicy",
    "NodeMapping",
    "NormalizedReadyMetrics",
    "ObjectiveConfigurationLoadResult",
    "OptimizationExperimentResult",
    "Order",
    "ProfileAwareOptimizer",
    "ProfileComparisonResult",
    "ProfileOptimizationResponse",
    "RegisteredCallbacks",
    "RouteEdgeMetric",
    "RouteStop",
    "Scenario",
    "SolverConfig",
    "SolverConfigLoadResult",
    "SolverExperimentMetadata",
    "SolverOutcome",
    "SolutionValidationResult",
    "UnservedOrder",
    "VRPTWSolution",
    "VRPTWSolver",
    "Vehicle",
    "VehicleUtilization",
    "VehicleRoute",
    "classify_solver_outcome",
    "load_solver_config",
    "load_objective_configuration",
    "validate_solution",
]
