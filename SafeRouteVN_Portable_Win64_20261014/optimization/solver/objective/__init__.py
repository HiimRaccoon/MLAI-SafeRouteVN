"""TASK-02 Phase E.3 profile-aware objective integration."""

from .adapter import (
    NormalizationMetadata,
    OptimizationExperimentResult,
    ProfileAwareOptimizer,
    ProfileComparisonResult,
    ProfileObjectiveCostAdapter,
    ProfileOptimizationResponse,
)
from .explanation import OptimizationExplanation, explain_result
from .cost_domain import (
    COST_DOMAIN_VERSION,
    PENALTY_POLICY_VERSION,
    SCALING_VERSION,
    ObjectiveCostDomain,
    ObjectiveCostDomainError,
)
from .fleet_validation import FLEET_VALIDATION_VERSION, FleetObjectiveBound
from .profiles import (
    CalibrationMetadata,
    ObjectiveConfiguration,
    ObjectiveConfigurationLoadResult,
    PhaseE3Configuration,
    ProfileObjectiveSpec,
    load_objective_configuration,
    validate_objective_configuration,
)
from .scoring import (
    ObjectiveBreakdown,
    ObjectiveIntegerScaler,
    ObjectiveRecord,
    RouteObjectiveRecord,
    ScoredMetrics,
    SolverObjectiveRecord,
    score_raw_metrics,
)

__all__ = [
    "CalibrationMetadata",
    "COST_DOMAIN_VERSION",
    "FLEET_VALIDATION_VERSION",
    "FleetObjectiveBound",
    "NormalizationMetadata",
    "ObjectiveBreakdown",
    "ObjectiveConfiguration",
    "ObjectiveConfigurationLoadResult",
    "ObjectiveCostDomain",
    "ObjectiveCostDomainError",
    "ObjectiveIntegerScaler",
    "ObjectiveRecord",
    "OptimizationExperimentResult",
    "OptimizationExplanation",
    "PhaseE3Configuration",
    "PENALTY_POLICY_VERSION",
    "ProfileAwareOptimizer",
    "ProfileComparisonResult",
    "ProfileObjectiveCostAdapter",
    "ProfileObjectiveSpec",
    "ProfileOptimizationResponse",
    "RouteObjectiveRecord",
    "ScoredMetrics",
    "SCALING_VERSION",
    "SolverObjectiveRecord",
    "explain_result",
    "load_objective_configuration",
    "score_raw_metrics",
    "validate_objective_configuration",
]
