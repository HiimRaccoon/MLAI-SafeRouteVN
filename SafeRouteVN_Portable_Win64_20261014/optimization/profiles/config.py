"""Profile weight configuration contract without selection behavior."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from optimization.config_loader import ConfigurationError, load_config_bundle
from optimization.models import Diagnostic, DiagnosticSeverity, OptimizationProfile
from optimization.models.common import ContractValidationError, require_non_empty_string

from .objective import ObjectiveWeights


PROFILE_ORDER = (
    OptimizationProfile.FASTEST,
    OptimizationProfile.BALANCED,
    OptimizationProfile.SAFER,
)
WEIGHT_FIELDS = frozenset({"time", "risk", "distance"})


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    """Validated profile names and their configured objective weights."""

    schema_version: str
    profile_version: str
    weights: Mapping[OptimizationProfile, ObjectiveWeights]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_non_empty_string(self.schema_version, "schema_version"),
        )
        object.__setattr__(
            self,
            "profile_version",
            require_non_empty_string(self.profile_version, "profile_version"),
        )
        if tuple(self.weights) != PROFILE_ORDER:
            raise ContractValidationError(
                "profile weights must be ordered FASTEST, BALANCED, SAFER"
            )
        object.__setattr__(self, "weights", MappingProxyType(dict(self.weights)))


@dataclass(frozen=True, slots=True)
class ProfileConfigLoadResult:
    """Profile configuration status without performing profile selection."""

    config: ProfileConfig | None
    diagnostics: tuple[Diagnostic, ...]

    @property
    def is_valid(self) -> bool:
        return self.config is not None and not any(
            item.severity == DiagnosticSeverity.ERROR for item in self.diagnostics
        )


def _diagnostic(
    code: str,
    severity: DiagnosticSeverity,
    message: str,
    details: Mapping[str, Any],
) -> Diagnostic:
    return Diagnostic(code=code, severity=severity, message=message, details=details)


def validate_profile_document(document: Mapping[str, Any]) -> ProfileConfigLoadResult:
    """Validate profile configuration structure and weights only."""

    diagnostics: list[Diagnostic] = []
    schema_version = document.get("schema_version") if isinstance(document, Mapping) else None
    profile_version = document.get("profile_version") if isinstance(document, Mapping) else None
    profiles = document.get("profiles") if isinstance(document, Mapping) else None
    if not isinstance(schema_version, str) or not schema_version.strip():
        diagnostics.append(
            _diagnostic(
                "INVALID_PROFILE_SCHEMA_VERSION",
                DiagnosticSeverity.ERROR,
                "Profile schema_version is required.",
                {},
            )
        )
    if not isinstance(profile_version, str) or not profile_version.strip():
        diagnostics.append(
            _diagnostic(
                "INVALID_PROFILE_VERSION",
                DiagnosticSeverity.ERROR,
                "profile_version is required.",
                {},
            )
        )
    expected_names = tuple(profile.value for profile in PROFILE_ORDER)
    if not isinstance(profiles, Mapping) or tuple(profiles) != expected_names:
        diagnostics.append(
            _diagnostic(
                "INVALID_PROFILE_SET",
                DiagnosticSeverity.ERROR,
                "Profiles must be ordered FASTEST, BALANCED, SAFER.",
                {"expected": list(expected_names)},
            )
        )
        return ProfileConfigLoadResult(None, tuple(diagnostics))

    parsed_weights: dict[OptimizationProfile, ObjectiveWeights] = {}
    for profile in PROFILE_ORDER:
        profile_section = profiles[profile.value]
        weights = (
            profile_section.get("weights")
            if isinstance(profile_section, Mapping)
            else None
        )
        if not isinstance(weights, Mapping) or set(weights) != WEIGHT_FIELDS:
            diagnostics.append(
                _diagnostic(
                    "INVALID_PROFILE_WEIGHTS",
                    DiagnosticSeverity.ERROR,
                    "Profile weights must contain exactly time, risk, and distance.",
                    {"profile": profile.value},
                )
            )
            continue
        try:
            parsed_weights[profile] = ObjectiveWeights(
                time=weights["time"],
                risk=weights["risk"],
                distance=weights["distance"],
            )
        except ContractValidationError as error:
            diagnostics.append(
                _diagnostic(
                    "INVALID_PROFILE_WEIGHTS",
                    DiagnosticSeverity.ERROR,
                    "Profile contains invalid objective weights.",
                    {"profile": profile.value, "reason": str(error)},
                )
            )

    if any(item.severity == DiagnosticSeverity.ERROR for item in diagnostics):
        return ProfileConfigLoadResult(None, tuple(diagnostics))
    config = ProfileConfig(
        schema_version=schema_version,
        profile_version=profile_version,
        weights=parsed_weights,
    )
    diagnostics.append(
        _diagnostic(
            "PROFILE_CONFIG_VALID",
            DiagnosticSeverity.INFO,
            "Profile configuration structure passed validation.",
            {"profile_version": profile_version},
        )
    )
    return ProfileConfigLoadResult(config, tuple(diagnostics))


def load_profile_config(config_directory: str | Path) -> ProfileConfigLoadResult:
    """Load profile weights without evaluating or selecting a profile."""

    try:
        bundle = load_config_bundle(config_directory)
    except ConfigurationError as error:
        return ProfileConfigLoadResult(
            None,
            (
                _diagnostic(
                    "PROFILE_CONFIG_LOAD_ERROR",
                    DiagnosticSeverity.ERROR,
                    "Profile configuration could not be loaded.",
                    {"reason": str(error)},
                ),
            ),
        )
    return validate_profile_document(bundle.get("profiles.yaml"))
