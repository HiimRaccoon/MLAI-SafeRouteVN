"""Versioned TASK-02 configuration loading skeleton.

Configuration files are JSON-compatible YAML so Phase A can load them with the
Python standard library. Full YAML syntax is also supported when PyYAML is
installed by the integrated project environment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .models.common import ContractValidationError, freeze_json


CONFIG_SECTIONS = MappingProxyType(
    {
        "profiles.yaml": "profiles",
        "risk_model.yaml": "risk_model",
        "travel_profile.yaml": "travel_profile",
        "normalization.yaml": "normalization",
        "solver.yaml": "solver",
    }
)


class ConfigurationError(ContractValidationError):
    """Raised when versioned configuration cannot be loaded or validated."""


@dataclass(frozen=True, slots=True)
class ConfigBundle:
    """All configuration documents required by the future engine."""

    documents: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        expected = set(CONFIG_SECTIONS)
        actual = set(self.documents)
        if actual != expected:
            missing = expected - actual
            extra = actual - expected
            details = []
            if missing:
                details.append("missing=" + ",".join(sorted(missing)))
            if extra:
                details.append("extra=" + ",".join(sorted(extra)))
            raise ConfigurationError("invalid config bundle: " + "; ".join(details))
        frozen = {
            filename: freeze_json(document, f"config.{filename}")
            for filename, document in self.documents.items()
        }
        object.__setattr__(self, "documents", MappingProxyType(frozen))

    def get(self, filename: str) -> Mapping[str, Any]:
        """Return one loaded configuration document by filename."""

        try:
            return self.documents[filename]
        except KeyError as error:
            raise ConfigurationError(f"unknown configuration file: {filename}") from error


def _parse_yaml_compatible(text: str, path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as json_error:
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError as import_error:
            raise ConfigurationError(
                f"{path} uses non-JSON YAML syntax; install PyYAML to load it"
            ) from import_error
        try:
            payload = yaml.safe_load(text)
        except yaml.YAMLError as yaml_error:  # type: ignore[attr-defined]
            raise ConfigurationError(f"invalid YAML in {path}: {yaml_error}") from yaml_error
        if payload is None:
            raise ConfigurationError(f"configuration file is empty: {path}") from json_error
    if not isinstance(payload, Mapping):
        raise ConfigurationError(f"configuration root must be a mapping: {path}")
    return payload


def _validate_document(filename: str, payload: Mapping[str, Any]) -> None:
    if not isinstance(payload.get("schema_version"), str) or not payload["schema_version"].strip():
        raise ConfigurationError(f"{filename} requires a non-empty schema_version")
    section = CONFIG_SECTIONS[filename]
    if section not in payload or not isinstance(payload[section], Mapping):
        raise ConfigurationError(f"{filename} requires a '{section}' mapping")


def load_config_bundle(config_directory: str | Path) -> ConfigBundle:
    """Load and minimally validate all required versioned config documents."""

    directory = Path(config_directory)
    if not directory.is_dir():
        raise ConfigurationError(f"config directory does not exist: {directory}")

    documents: dict[str, Mapping[str, Any]] = {}
    for filename in CONFIG_SECTIONS:
        path = directory / filename
        if not path.is_file():
            raise ConfigurationError(f"required configuration file is missing: {path}")
        payload = _parse_yaml_compatible(path.read_text(encoding="utf-8"), path)
        _validate_document(filename, payload)
        documents[filename] = payload
    return ConfigBundle(documents=documents)
