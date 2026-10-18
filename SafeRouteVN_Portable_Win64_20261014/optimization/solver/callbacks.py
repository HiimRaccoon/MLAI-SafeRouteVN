"""OR-Tools distance, time, and raw-exposure callback layer."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Mapping, Sequence

from optimization.models import ContractValidationError, MatrixBundle
from optimization.models.common import validate_finite_number


INT64_MAX = 9_223_372_036_854_775_807


@dataclass(frozen=True, slots=True)
class IntegerScalingPolicy:
    """Documented conversion from internal decimal values to solver int64."""

    distance: int
    time: int
    risk: int
    demand: int
    rounding_policy: str = "ROUND_HALF_UP"

    def __post_init__(self) -> None:
        for field_name in ("distance", "time", "risk", "demand"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ContractValidationError(
                    f"integer_scaling.{field_name} must be a positive integer"
                )
        if self.rounding_policy != "ROUND_HALF_UP":
            raise ContractValidationError(
                "integer_scaling.rounding_policy must be ROUND_HALF_UP"
            )

    def to_solver(self, value: float, metric: str) -> int:
        """Scale a finite non-negative value using explicit decimal rounding."""

        raw = validate_finite_number(value, metric, minimum=0.0)
        if metric not in ("distance", "time", "risk", "demand"):
            raise ContractValidationError(f"unknown scaling metric: {metric}")
        scale = getattr(self, metric)
        result = int(
            (Decimal(str(raw)) * Decimal(scale)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        if result > INT64_MAX:
            raise ContractValidationError(f"scaled {metric} exceeds int64")
        return result

    def from_solver(self, value: int, metric: str) -> float:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractValidationError("solver value must be an integer")
        if metric not in ("distance", "time", "risk", "demand"):
            raise ContractValidationError(f"unknown scaling metric: {metric}")
        return value / getattr(self, metric)


@dataclass(frozen=True, slots=True)
class RegisteredCallbacks:
    """Callback indices retained by the routing-model builder."""

    distance_index: int
    time_index: int
    risk_index: int


def _matrix_index(
    manager: Any, routing_index: int, routing_node_to_matrix_index: Sequence[int]
) -> int:
    routing_node = manager.IndexToNode(routing_index)
    try:
        return routing_node_to_matrix_index[routing_node]
    except (IndexError, TypeError) as error:
        raise ContractValidationError(
            "OR-Tools routing node has no MatrixBundle mapping"
        ) from error


def create_distance_callback(
    manager: Any,
    bundle: MatrixBundle,
    routing_node_to_matrix_index: Sequence[int],
    scaling: IntegerScalingPolicy,
) -> Callable[[int, int], int]:
    """Create the meter-based primary routing-cost callback."""

    def distance_callback(from_index: int, to_index: int) -> int:
        row = _matrix_index(manager, from_index, routing_node_to_matrix_index)
        column = _matrix_index(manager, to_index, routing_node_to_matrix_index)
        return scaling.to_solver(bundle.distance_matrix[row][column], "distance")

    return distance_callback


def create_time_callback(
    manager: Any,
    bundle: MatrixBundle,
    routing_node_to_matrix_index: Sequence[int],
    service_time_by_routing_node: Mapping[int, float],
    scaling: IntegerScalingPolicy,
) -> Callable[[int, int], int]:
    """Create travel plus from-node service transit for the Time Dimension."""

    def time_callback(from_index: int, to_index: int) -> int:
        from_node = manager.IndexToNode(from_index)
        row = _matrix_index(manager, from_index, routing_node_to_matrix_index)
        column = _matrix_index(manager, to_index, routing_node_to_matrix_index)
        transit = bundle.time_matrix[row][column] + service_time_by_routing_node.get(
            from_node, 0.0
        )
        return scaling.to_solver(transit, "time")

    return time_callback


def create_risk_callback(
    manager: Any,
    bundle: MatrixBundle,
    routing_node_to_matrix_index: Sequence[int],
    scaling: IntegerScalingPolicy,
) -> Callable[[int, int], int]:
    """Create a raw exposure callback; Phase E.1/E.2 never uses it as cost."""

    def risk_callback(from_index: int, to_index: int) -> int:
        row = _matrix_index(manager, from_index, routing_node_to_matrix_index)
        column = _matrix_index(manager, to_index, routing_node_to_matrix_index)
        return scaling.to_solver(bundle.risk_matrix[row][column], "risk")

    return risk_callback


def register_callbacks(
    routing: Any,
    manager: Any,
    bundle: MatrixBundle,
    routing_node_to_matrix_index: Sequence[int],
    service_time_by_routing_node: Mapping[int, float],
    scaling: IntegerScalingPolicy,
) -> RegisteredCallbacks:
    """Register all Phase E.1 callback interfaces with one RoutingModel."""

    if len(routing_node_to_matrix_index) == 0:
        raise ContractValidationError("routing node mapping must not be empty")
    size = len(bundle.node_order)
    if any(index < 0 or index >= size for index in routing_node_to_matrix_index):
        raise ContractValidationError("routing node mapping contains an invalid matrix index")
    for row in range(size):
        for column in range(size):
            scaling.to_solver(bundle.distance_matrix[row][column], "distance")
            scaling.to_solver(bundle.time_matrix[row][column], "time")
            scaling.to_solver(bundle.risk_matrix[row][column], "risk")
    for value in service_time_by_routing_node.values():
        scaling.to_solver(value, "time")

    distance_index = routing.RegisterTransitCallback(
        create_distance_callback(
            manager, bundle, routing_node_to_matrix_index, scaling
        )
    )
    time_index = routing.RegisterTransitCallback(
        create_time_callback(
            manager,
            bundle,
            routing_node_to_matrix_index,
            service_time_by_routing_node,
            scaling,
        )
    )
    risk_index = routing.RegisterTransitCallback(
        create_risk_callback(manager, bundle, routing_node_to_matrix_index, scaling)
    )
    return RegisteredCallbacks(distance_index, time_index, risk_index)
