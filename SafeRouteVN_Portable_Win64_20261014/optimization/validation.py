"""Validation entry points for TASK-02 contracts."""

from __future__ import annotations

from typing import Any, Mapping, TypeVar

from .models import CandidatePath, DecisionResult, MatrixBundle, RouteAlternative
from .models.common import ContractValidationError


ModelT = TypeVar("ModelT", CandidatePath, MatrixBundle, RouteAlternative, DecisionResult)


def require_fields(
    payload: Mapping[str, Any],
    required_fields: set[str] | frozenset[str],
    contract_name: str,
) -> None:
    """Raise a consistent error when contract fields are missing."""

    if not isinstance(payload, Mapping):
        raise ContractValidationError(f"{contract_name} must be a mapping")
    missing = required_fields - set(payload)
    if missing:
        raise ContractValidationError(
            f"{contract_name} missing required fields: {', '.join(sorted(missing))}"
        )


def validate_schema(model_type: type[ModelT], payload: Mapping[str, Any]) -> ModelT:
    """Validate a mapping by constructing the requested internal model."""

    supported = {CandidatePath, MatrixBundle, RouteAlternative, DecisionResult}
    if model_type not in supported:
        raise TypeError(f"unsupported contract model: {model_type!r}")
    return model_type.from_dict(payload)


def validate_contract_consistency(result: DecisionResult) -> None:
    """Re-run envelope-level consistency checks without altering the result."""

    if not isinstance(result, DecisionResult):
        raise ContractValidationError("result must be a DecisionResult")
    DecisionResult.from_dict(result.to_dict())
