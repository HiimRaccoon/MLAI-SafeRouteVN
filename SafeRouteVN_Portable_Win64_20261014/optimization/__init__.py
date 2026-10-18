"""SafeRoute VN optimization decision engine.

Phase A exposes contracts, validation, and configuration loading only.
Optimization algorithms are intentionally not implemented yet.
"""

from .config_loader import ConfigBundle, load_config_bundle
from .models import CandidatePath, DecisionResult, MatrixBundle, RouteAlternative

__all__ = [
    "CandidatePath",
    "ConfigBundle",
    "DecisionResult",
    "MatrixBundle",
    "RouteAlternative",
    "load_config_bundle",
]
