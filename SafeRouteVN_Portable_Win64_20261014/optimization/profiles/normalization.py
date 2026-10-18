"""Normalization configuration and metric transformation foundation.

This module transforms raw metrics only. It contains no profile names, profile
weights, ranking, or selection behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from optimization.config_loader import ConfigurationError, load_config_bundle
from optimization.models import Diagnostic, DiagnosticSeverity, MatrixBundle
from optimization.models.common import (
    ContractValidationError,
    NodeId,
    require_non_empty_string,
    validate_finite_number,
)


REFERENCE_FIELDS = ("distance", "time", "length", "risk")
REFERENCE_SYMBOLS = {
    "distance": "Cref",
    "time": "Tref",
    "length": "Lref",
    "risk": "Rref",
}


@dataclass(frozen=True, slots=True)
class NormalizationConfig:
    """Validated positive reference values for raw metric normalization."""

    schema_version: str
    normalization_version: str
    distance_reference: float
    time_reference: float
    length_reference: float
    risk_reference: float
    normalization_source: str = "direct_constructor_unspecified"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_non_empty_string(self.schema_version, "schema_version"),
        )
        object.__setattr__(
            self,
            "normalization_version",
            require_non_empty_string(
                self.normalization_version, "normalization_version"
            ),
        )
        object.__setattr__(
            self,
            "normalization_source",
            require_non_empty_string(
                self.normalization_source, "normalization_source"
            ),
        )
        for field_name in (
            "distance_reference",
            "time_reference",
            "length_reference",
            "risk_reference",
        ):
            value = validate_finite_number(
                getattr(self, field_name), field_name, minimum=0.0
            )
            if value == 0.0:
                raise ContractValidationError(f"{field_name} must be greater than zero")
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class NormalizationConfigLoadResult:
    """Validation status for normalization configuration loading."""

    config: NormalizationConfig | None
    diagnostics: tuple[Diagnostic, ...]

    @property
    def is_valid(self) -> bool:
        return self.config is not None and not any(
            item.severity == DiagnosticSeverity.ERROR for item in self.diagnostics
        )


@dataclass(frozen=True, slots=True)
class RawMetrics:
    """Raw values supplied to normalization."""

    distance: float
    time: float
    length: float
    risk: float

    def __post_init__(self) -> None:
        for field_name in REFERENCE_FIELDS:
            object.__setattr__(
                self,
                field_name,
                validate_finite_number(
                    getattr(self, field_name), field_name, minimum=0.0
                ),
            )


@dataclass(frozen=True, slots=True)
class NormalizedMetrics:
    """Profile-neutral normalized values."""

    distance_normalized: float
    time_normalized: float
    length_normalized: float
    risk_normalized: float

    def __post_init__(self) -> None:
        for field_name in (
            "distance_normalized",
            "time_normalized",
            "length_normalized",
            "risk_normalized",
        ):
            object.__setattr__(
                self,
                field_name,
                validate_finite_number(
                    getattr(self, field_name), field_name, minimum=0.0
                ),
            )

    def to_dict(self) -> dict[str, float]:
        """Return the required normalized metric contract."""

        return {
            "distance_normalized": self.distance_normalized,
            "time_normalized": self.time_normalized,
            "length_normalized": self.length_normalized,
            "risk_normalized": self.risk_normalized,
        }


def _diagnostic(
    code: str,
    severity: DiagnosticSeverity,
    message: str,
    details: Mapping[str, Any],
) -> Diagnostic:
    return Diagnostic(code=code, severity=severity, message=message, details=details)


def validate_normalization_document(
    document: Mapping[str, Any],
) -> NormalizationConfigLoadResult:
    """Validate a parsed normalization document without inventing defaults."""

    diagnostics: list[Diagnostic] = []
    if not isinstance(document, Mapping):
        return NormalizationConfigLoadResult(
            config=None,
            diagnostics=(
                _diagnostic(
                    "INVALID_NORMALIZATION_CONFIG",
                    DiagnosticSeverity.ERROR,
                    "Normalization configuration root must be a mapping.",
                    {},
                ),
            ),
        )

    schema_version = document.get("schema_version")
    normalization = document.get("normalization")
    if not isinstance(schema_version, str) or not schema_version.strip():
        diagnostics.append(
            _diagnostic(
                "MISSING_NORMALIZATION_SCHEMA_VERSION",
                DiagnosticSeverity.ERROR,
                "Normalization schema_version is required.",
                {},
            )
        )
    if not isinstance(normalization, Mapping):
        diagnostics.append(
            _diagnostic(
                "INVALID_NORMALIZATION_CONFIG",
                DiagnosticSeverity.ERROR,
                "The normalization section must be a mapping.",
                {},
            )
        )
        return NormalizationConfigLoadResult(None, tuple(diagnostics))

    normalization_version = normalization.get("normalization_version")
    if not isinstance(normalization_version, str) or not normalization_version.strip():
        diagnostics.append(
            _diagnostic(
                "MISSING_NORMALIZATION_VERSION",
                DiagnosticSeverity.ERROR,
                "normalization_version is required.",
                {},
            )
        )

    normalization_source = normalization.get("normalization_source")
    if not isinstance(normalization_source, str) or not normalization_source.strip():
        diagnostics.append(
            _diagnostic(
                "MISSING_NORMALIZATION_SOURCE",
                DiagnosticSeverity.ERROR,
                "normalization_source is required for reproducible normalization.",
                {},
            )
        )

    references: dict[str, float] = {}
    for metric in REFERENCE_FIELDS:
        section = normalization.get(metric)
        if not isinstance(section, Mapping) or section.get("reference_value") is None:
            diagnostics.append(
                _diagnostic(
                    "MISSING_REFERENCE_VALUE",
                    DiagnosticSeverity.ERROR,
                    f"{REFERENCE_SYMBOLS[metric]} reference value is missing.",
                    {"metric": metric, "symbol": REFERENCE_SYMBOLS[metric]},
                )
            )
            continue
        try:
            value = validate_finite_number(
                section["reference_value"],
                f"normalization.{metric}.reference_value",
                minimum=0.0,
            )
            if value == 0.0:
                raise ContractValidationError("reference value must be greater than zero")
        except ContractValidationError as error:
            diagnostics.append(
                _diagnostic(
                    "INVALID_REFERENCE_VALUE",
                    DiagnosticSeverity.ERROR,
                    f"{REFERENCE_SYMBOLS[metric]} reference value is invalid.",
                    {
                        "metric": metric,
                        "symbol": REFERENCE_SYMBOLS[metric],
                        "reason": str(error),
                    },
                )
            )
            continue
        references[metric] = value

    if any(item.severity == DiagnosticSeverity.ERROR for item in diagnostics):
        return NormalizationConfigLoadResult(None, tuple(diagnostics))

    config = NormalizationConfig(
        schema_version=schema_version,
        normalization_version=normalization_version,
        distance_reference=references["distance"],
        time_reference=references["time"],
        length_reference=references["length"],
        risk_reference=references["risk"],
        normalization_source=normalization_source,
    )
    diagnostics.append(
        _diagnostic(
            "NORMALIZATION_CONFIG_VALID",
            DiagnosticSeverity.INFO,
            "Normalization references passed validation.",
            {
                "normalization_version": config.normalization_version,
                "normalization_source": config.normalization_source,
            },
        )
    )
    return NormalizationConfigLoadResult(config, tuple(diagnostics))


def load_normalization_config(
    config_directory: str | Path,
) -> NormalizationConfigLoadResult:
    """Load and validate `configs/normalization.yaml`."""

    try:
        bundle = load_config_bundle(config_directory)
    except ConfigurationError as error:
        return NormalizationConfigLoadResult(
            None,
            (
                _diagnostic(
                    "NORMALIZATION_CONFIG_LOAD_ERROR",
                    DiagnosticSeverity.ERROR,
                    "Normalization configuration could not be loaded.",
                    {"reason": str(error)},
                ),
            ),
        )
    return validate_normalization_document(bundle.get("normalization.yaml"))


def normalize_metrics(
    raw_metrics: RawMetrics,
    config: NormalizationConfig,
) -> NormalizedMetrics:
    """Divide raw metrics by their validated reference values."""

    if not isinstance(raw_metrics, RawMetrics):
        raise ContractValidationError("raw_metrics must be a RawMetrics instance")
    if not isinstance(config, NormalizationConfig):
        raise ContractValidationError("config must be a NormalizationConfig instance")
    return NormalizedMetrics(
        distance_normalized=raw_metrics.distance / config.distance_reference,
        time_normalized=raw_metrics.time / config.time_reference,
        length_normalized=raw_metrics.length / config.length_reference,
        risk_normalized=raw_metrics.risk / config.risk_reference,
    )


def normalize_matrix_entry(
    bundle: MatrixBundle,
    origin: NodeId,
    destination: NodeId,
    config: NormalizationConfig,
) -> NormalizedMetrics:
    """Normalize one directed MatrixBundle entry without copying matrix state.

    MatrixBundle D is the current physical route length/distance source, so it
    supplies both the distance and length readiness metrics until a separate
    approved cost metric exists.
    """

    if not isinstance(bundle, MatrixBundle):
        raise ContractValidationError("bundle must be a MatrixBundle")
    try:
        row = bundle.node_order.index(origin)
        column = bundle.node_order.index(destination)
    except ValueError as error:
        raise ContractValidationError(
            "origin and destination must exist in MatrixBundle.node_order"
        ) from error
    distance = bundle.distance_matrix[row][column]
    return normalize_metrics(
        RawMetrics(
            distance=distance,
            time=bundle.time_matrix[row][column],
            length=distance,
            risk=bundle.risk_matrix[row][column],
        ),
        config,
    )
