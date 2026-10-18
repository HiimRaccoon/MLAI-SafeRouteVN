"""Strict Phase E.3 profile, objective, and calibration configuration."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from optimization.config_loader import ConfigurationError, load_config_bundle
from optimization.models import Diagnostic, DiagnosticSeverity, OptimizationProfile
from optimization.models.common import (
    ContractValidationError,
    require_non_empty_string,
)
from optimization.profiles import NormalizationConfig, ObjectiveWeights
from optimization.profiles.normalization import validate_normalization_document


PROFILE_ORDER = (
    OptimizationProfile.FASTEST,
    OptimizationProfile.BALANCED,
    OptimizationProfile.SAFER,
)
ALLOWED_RANKING_FIELDS = frozenset({"weighted_score", "time", "distance", "risk"})


@dataclass(frozen=True, slots=True)
class CalibrationMetadata:
    source: str
    version: str
    date: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", require_non_empty_string(self.source, "calibration_source"))
        object.__setattr__(self, "version", require_non_empty_string(self.version, "calibration_version"))
        value = require_non_empty_string(self.date, "calibration_date")
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ContractValidationError("calibration_date must use YYYY-MM-DD") from error
        object.__setattr__(self, "date", value)

    def to_dict(self) -> dict[str, str]:
        return {"calibration_source": self.source, "calibration_version": self.version, "calibration_date": self.date}


@dataclass(frozen=True, slots=True)
class ProfileObjectiveSpec:
    profile: OptimizationProfile
    weights: ObjectiveWeights
    ranking_order: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.profile, OptimizationProfile):
            object.__setattr__(self, "profile", OptimizationProfile(self.profile))
        if not isinstance(self.weights, ObjectiveWeights):
            raise ContractValidationError("weights must be ObjectiveWeights")
        ranking = tuple(self.ranking_order)
        if not ranking or ranking[0] != "weighted_score":
            raise ContractValidationError("ranking_order must start with weighted_score")
        if len(ranking) != len(set(ranking)) or any(item not in ALLOWED_RANKING_FIELDS for item in ranking):
            raise ContractValidationError("ranking_order contains duplicate or unsupported fields")
        object.__setattr__(self, "ranking_order", ranking)


@dataclass(frozen=True, slots=True)
class ObjectiveConfiguration:
    schema_version: str
    objective_version: str
    scoring_logic_version: str
    profile_config_version: str
    integer_scaling_factor: int
    rounding_policy: str
    profiles: Mapping[OptimizationProfile, ProfileObjectiveSpec]

    def __post_init__(self) -> None:
        for name in ("schema_version", "objective_version", "scoring_logic_version", "profile_config_version"):
            object.__setattr__(self, name, require_non_empty_string(getattr(self, name), name))
        if isinstance(self.integer_scaling_factor, bool) or not isinstance(self.integer_scaling_factor, int) or self.integer_scaling_factor <= 0:
            raise ContractValidationError("integer_scaling_factor must be a positive integer")
        if self.rounding_policy != "ROUND_HALF_UP":
            raise ContractValidationError("objective rounding_policy must be ROUND_HALF_UP")
        if tuple(self.profiles) != PROFILE_ORDER:
            raise ContractValidationError("profiles must be ordered FASTEST, BALANCED, SAFER")
        object.__setattr__(self, "profiles", MappingProxyType(dict(self.profiles)))


@dataclass(frozen=True, slots=True)
class PhaseE3Configuration:
    objective: ObjectiveConfiguration
    normalization: NormalizationConfig
    calibration: CalibrationMetadata


@dataclass(frozen=True, slots=True)
class ObjectiveConfigurationLoadResult:
    config: PhaseE3Configuration | None
    diagnostics: tuple[Diagnostic, ...]

    @property
    def is_valid(self) -> bool:
        return self.config is not None and not any(
            item.severity in {DiagnosticSeverity.ERROR, DiagnosticSeverity.FATAL}
            for item in self.diagnostics
        )


def _diagnostic(code: str, message: str, details: Mapping[str, Any]) -> Diagnostic:
    return Diagnostic(
        code,
        DiagnosticSeverity.ERROR,
        message,
        details,
        {"component": "objective_configuration"},
    )


def validate_objective_configuration(
    profiles_document: Mapping[str, Any],
    normalization_document: Mapping[str, Any],
) -> ObjectiveConfigurationLoadResult:
    """Validate E.3 configuration without defaults, repairs, or invented values."""

    diagnostics: list[Diagnostic] = []
    if not isinstance(profiles_document, Mapping):
        return ObjectiveConfigurationLoadResult(None, (_diagnostic("INVALID_OBJECTIVE_CONFIGURATION", "profiles configuration must be a mapping.", {}),))
    objective_section = profiles_document.get("objective")
    profiles_section = profiles_document.get("profiles")
    if not isinstance(objective_section, Mapping) or not isinstance(profiles_section, Mapping):
        return ObjectiveConfigurationLoadResult(None, (_diagnostic("INVALID_OBJECTIVE_CONFIGURATION", "objective and profiles sections are required.", {}),))

    parsed_profiles: dict[OptimizationProfile, ProfileObjectiveSpec] = {}
    expected_names = tuple(profile.value for profile in PROFILE_ORDER)
    if tuple(profiles_section) != expected_names:
        diagnostics.append(_diagnostic("INVALID_OBJECTIVE_CONFIGURATION", "Profiles must be ordered FASTEST, BALANCED, SAFER.", {"expected": list(expected_names)}))
    else:
        for profile in PROFILE_ORDER:
            section = profiles_section.get(profile.value)
            weights = section.get("weights") if isinstance(section, Mapping) else None
            ranking = section.get("ranking_order") if isinstance(section, Mapping) else None
            if not isinstance(weights, Mapping) or set(weights) != {"time", "distance", "risk"}:
                diagnostics.append(_diagnostic("INVALID_PROFILE_WEIGHT_CONFIGURATION", "Profile weights must contain exactly time, distance, and risk.", {"profile": profile.value}))
                continue
            numeric: dict[str, float] = {}
            invalid_range = False
            for name in ("time", "distance", "risk"):
                value = weights.get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    diagnostics.append(_diagnostic("INVALID_PROFILE_WEIGHT_CONFIGURATION", "Profile weights must be finite numeric values.", {"profile": profile.value, "weight": name}))
                    invalid_range = True
                    break
                numeric[name] = float(value)
                if not 0.0 <= numeric[name] <= 1.0:
                    diagnostics.append(_diagnostic("INVALID_WEIGHT_RANGE", "Every profile weight must be within [0, 1].", {"profile": profile.value, "weight": name, "value": numeric[name]}))
                    invalid_range = True
            if invalid_range:
                continue
            if not math.isclose(sum(numeric.values()), 1.0, rel_tol=0.0, abs_tol=1e-12):
                diagnostics.append(_diagnostic("INVALID_PROFILE_WEIGHT_CONFIGURATION", "Profile weights must sum to exactly one.", {"profile": profile.value, "sum": sum(numeric.values())}))
                continue
            try:
                parsed_profiles[profile] = ProfileObjectiveSpec(
                    profile,
                    ObjectiveWeights(time=numeric["time"], distance=numeric["distance"], risk=numeric["risk"]),
                    tuple(ranking) if isinstance(ranking, (list, tuple)) else (),
                )
            except (ContractValidationError, ValueError, TypeError) as error:
                diagnostics.append(_diagnostic("INVALID_OBJECTIVE_CONFIGURATION", "Profile ranking configuration is invalid.", {"profile": profile.value, "reason": str(error)}))

    normalization_result = validate_normalization_document(normalization_document)
    if normalization_result.config is None:
        diagnostics.append(_diagnostic("INVALID_NORMALIZATION_CONFIGURATION", "Normalization references must be explicitly calibrated before E.3 optimization.", {"diagnostics": [item.to_dict() for item in normalization_result.diagnostics]}))
    normalization_section = normalization_document.get("normalization") if isinstance(normalization_document, Mapping) else None
    calibration: CalibrationMetadata | None = None
    if isinstance(normalization_section, Mapping):
        try:
            calibration = CalibrationMetadata(
                normalization_section.get("calibration_source"),
                normalization_section.get("calibration_version"),
                normalization_section.get("calibration_date"),
            )
        except (ContractValidationError, TypeError) as error:
            diagnostics.append(_diagnostic("INVALID_NORMALIZATION_CONFIGURATION", "Calibration source, version, and date are required.", {"reason": str(error)}))
    else:
        diagnostics.append(_diagnostic("INVALID_NORMALIZATION_CONFIGURATION", "normalization section is required.", {}))

    objective: ObjectiveConfiguration | None = None
    try:
        objective = ObjectiveConfiguration(
            schema_version=profiles_document.get("schema_version"),
            objective_version=objective_section.get("objective_version"),
            scoring_logic_version=objective_section.get("scoring_logic_version"),
            profile_config_version=profiles_document.get("profile_version"),
            integer_scaling_factor=objective_section.get("integer_scaling_factor"),
            rounding_policy=objective_section.get("rounding_policy"),
            profiles=parsed_profiles,
        )
    except (ContractValidationError, TypeError, ValueError) as error:
        diagnostics.append(_diagnostic("INVALID_OBJECTIVE_CONFIGURATION", "Objective configuration is invalid.", {"reason": str(error)}))

    if diagnostics or objective is None or normalization_result.config is None or calibration is None:
        return ObjectiveConfigurationLoadResult(None, tuple(diagnostics))
    return ObjectiveConfigurationLoadResult(
        PhaseE3Configuration(objective, normalization_result.config, calibration),
        (
            Diagnostic(
                "OBJECTIVE_CONFIGURATION_VALID",
                DiagnosticSeverity.INFO,
                "Phase E.3 objective configuration passed strict validation.",
                {"objective_version": objective.objective_version, "profile_config_version": objective.profile_config_version},
                {"component": "objective_configuration"},
            ),
        ),
    )


def load_objective_configuration(config_directory: str | Path) -> ObjectiveConfigurationLoadResult:
    try:
        bundle = load_config_bundle(config_directory)
    except ConfigurationError as error:
        return ObjectiveConfigurationLoadResult(None, (_diagnostic("INVALID_OBJECTIVE_CONFIGURATION", "Configuration bundle could not be loaded.", {"reason": str(error)}),))
    return validate_objective_configuration(bundle.get("profiles.yaml"), bundle.get("normalization.yaml"))
