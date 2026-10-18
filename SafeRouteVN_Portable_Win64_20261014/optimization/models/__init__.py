"""Public exports for TASK-02 internal data contracts."""

from .candidate_path import CandidatePath
from .common import (
    AlternativeMetrics,
    ContractValidationError,
    Diagnostic,
    DiagnosticSeverity,
    EdgeReference,
    ExplanationFact,
    GeoPoint,
    OptimizationProfile,
    UnitMetadata,
)
from .decision_result import DecisionResult, RouteAlternative
from .matrix_bundle import MatrixBundle

__all__ = [
    "AlternativeMetrics",
    "CandidatePath",
    "ContractValidationError",
    "DecisionResult",
    "Diagnostic",
    "DiagnosticSeverity",
    "EdgeReference",
    "ExplanationFact",
    "GeoPoint",
    "MatrixBundle",
    "OptimizationProfile",
    "RouteAlternative",
    "UnitMetadata",
]
