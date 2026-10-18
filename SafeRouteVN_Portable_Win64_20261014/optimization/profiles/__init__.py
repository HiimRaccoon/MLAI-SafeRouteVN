"""Normalization, objective, explanation, and profile-decision contracts."""

from .config import ProfileConfig, ProfileConfigLoadResult, load_profile_config
from .explanation import TradeoffDeltas, TradeoffExplanation
from .normalization import (
    NormalizationConfig,
    NormalizationConfigLoadResult,
    NormalizedMetrics,
    RawMetrics,
    load_normalization_config,
    normalize_matrix_entry,
    normalize_metrics,
    validate_normalization_document,
)
from .objective import (
    LinearObjectiveFunction,
    ObjectiveComponents,
    ObjectiveFunction,
    ObjectiveWeights,
)
from .engine import (
    AlternativeEvaluation,
    GeometryReference,
    ProfileDecisionResult,
    ProfileEngine,
    ProfileEngineLoadResult,
)

__all__ = [
    "AlternativeEvaluation",
    "GeometryReference",
    "LinearObjectiveFunction",
    "NormalizationConfig",
    "NormalizationConfigLoadResult",
    "NormalizedMetrics",
    "ObjectiveFunction",
    "ObjectiveComponents",
    "ObjectiveWeights",
    "ProfileConfig",
    "ProfileConfigLoadResult",
    "ProfileDecisionResult",
    "ProfileEngine",
    "ProfileEngineLoadResult",
    "RawMetrics",
    "TradeoffDeltas",
    "TradeoffExplanation",
    "load_normalization_config",
    "load_profile_config",
    "normalize_matrix_entry",
    "normalize_metrics",
    "validate_normalization_document",
]
