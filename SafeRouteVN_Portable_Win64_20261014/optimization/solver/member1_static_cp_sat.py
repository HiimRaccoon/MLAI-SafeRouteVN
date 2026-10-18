"""Restricted M1 static assignment/sequence model using Google OR-Tools CP-SAT.

The model consumes a source-backed one-path-per-pair projection.  It decides
logical assignment and sequence only.  The integration layer must realize and
independently validate directed physical paths with incoming-edge state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Mapping, Sequence

from optimization.models.decision_state import DecisionState
from optimization.models.matrix_bundle import MatrixBundle


MODEL_CONFIG_VERSION = "member1-ortools-static-model-config/3"
OBJECTIVE_POLICY_VERSION = "member1-ortools-static-physical-lexicographic/2"
SCALING_VERSION = "member1-ortools-static-conservative-scaling/2"
INT64_MAX = 9_223_372_036_854_775_807


def _finite(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    converted = float(value)
    if not math.isfinite(converted) or converted < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return converted


@dataclass(frozen=True)
class M1OrToolsStaticConfig:
    time_limit_seconds: float = 120.0
    max_model_proposals: int = 64
    max_road_states: int = 250_000
    max_road_labels: int = 500_000
    max_arrival_options: int = 4
    time_scale: int = 1000
    distance_scale: int = 1000
    demand_scale: int = 1000
    cost_scale: int = 1
    random_seed: int = 0
    num_search_workers: int = 1
    master_reserve_fraction: float = 0.05

    def __post_init__(self) -> None:
        _finite(self.time_limit_seconds, "time_limit_seconds", minimum=1e-6)
        for name in (
            "max_model_proposals", "max_road_states", "max_road_labels",
            "max_arrival_options", "time_scale", "distance_scale",
            "demand_scale", "cost_scale", "num_search_workers",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.random_seed) is not int or self.random_seed < 0:
            raise ValueError("random_seed must be a nonnegative integer")
        reserve = _finite(
            self.master_reserve_fraction,
            "master_reserve_fraction",
            minimum=1e-6,
        )
        if reserve >= 1.0:
            raise ValueError("master_reserve_fraction must be less than 1.0")
        # Reject obviously unsafe coefficients before an engine/model exists.
        if max(self.time_scale, self.distance_scale, self.demand_scale,
               self.cost_scale) > INT64_MAX // 1_000_000:
            raise ValueError("scaling factor can overflow signed int64")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": MODEL_CONFIG_VERSION,
            "time_limit_seconds": float(self.time_limit_seconds),
            "max_model_proposals": self.max_model_proposals,
            "max_road_states": self.max_road_states,
            "max_road_labels": self.max_road_labels,
            "max_arrival_options": self.max_arrival_options,
            "random_seed": self.random_seed,
            "num_search_workers": self.num_search_workers,
            "master_reserve_fraction": float(self.master_reserve_fraction),
            "scaling": {
                "version": SCALING_VERSION,
                "time": self.time_scale,
                "distance": self.distance_scale,
                "demand": self.demand_scale,
                "cost": self.cost_scale,
                "increment_rounding": "CEILING",
                "hard_upper_bound_rounding": "FLOOR",
                "hard_lower_bound_rounding": "CEILING",
            },
            "objective_policy_version": OBJECTIVE_POLICY_VERSION,
            "proposal_range_is_relaxation": True,
        }


def _ceil(value: float, scale: int, name: str) -> int:
    converted = _finite(value, name)
    result = math.ceil(converted * scale - 1e-12)
    if result > INT64_MAX:
        raise ValueError(f"{name} integer conversion exceeds signed int64")
    return result


def _floor(value: float, scale: int, name: str) -> int:
    converted = _finite(value, name)
    result = math.floor(converted * scale + 1e-12)
    if result > INT64_MAX:
        raise ValueError(f"{name} integer conversion exceeds signed int64")
    return result


def _floor_signed(value: float, scale: int, name: str) -> int:
    """Scale a relative timestamp without treating pre-epoch values as durations."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    result = math.floor(converted * scale + 1e-12)
    if abs(result) > INT64_MAX:
        raise ValueError(f"{name} integer conversion exceeds signed int64")
    return result


def _seconds(value: str, epoch: datetime) -> float:
    return (datetime.fromisoformat(value) - epoch).total_seconds()


def solve_restricted_sequence_model(
    state: DecisionState,
    matrix: MatrixBundle,
    *,
    config: M1OrToolsStaticConfig,
    forbidden_signatures: Sequence[Mapping[str, Sequence[str]]] = (),
    phase_time_limit_seconds: float | None = None,
) -> dict[str, Any]:
    """Solve assignment/sequence lexicographically inside the matrix domain."""

    try:
        import ortools
        from ortools.sat.python import cp_model
    except (ImportError, OSError) as error:
        return {"status": "ENGINE_UNAVAILABLE", "diagnostic": str(error)}

    if ortools.__version__ != "9.15.6755":
        return {
            "status": "ENGINE_VERSION_MISMATCH",
            "required": "9.15.6755", "actual": ortools.__version__,
        }
    orders = tuple(sorted(state.orders, key=lambda item: item.order_id))
    vehicles = tuple(sorted(state.vehicles, key=lambda item: item.vehicle_id))
    order_ids = tuple(order.order_id for order in orders)
    vehicle_ids = tuple(vehicle.vehicle_id for vehicle in vehicles)
    node_index = {node: index for index, node in enumerate(matrix.node_order)}
    if state.depot.graph_node_id not in node_index or any(
        order.graph_node_id not in node_index for order in orders
    ):
        raise ValueError("matrix node order does not cover depot and every order")
    epoch = datetime.fromisoformat(state.decision_epoch)
    start, end = 0, len(orders) + 1
    order_nodes = tuple(range(1, len(orders) + 1))
    logical_nodes = (start,) + order_nodes + (end,)

    def physical(logical: int) -> int:
        if logical in (start, end):
            return state.depot.graph_node_id
        return orders[logical - 1].graph_node_id

    def metric(table: Sequence[Sequence[float]], a: int, b: int) -> float:
        return float(table[node_index[physical(a)]][node_index[physical(b)]])

    model = cp_model.CpModel()
    arcs: dict[tuple[int, int, int], Any] = {}
    assigned: dict[tuple[int, int], Any] = {}
    used: dict[int, Any] = {}
    position: dict[tuple[int, int], Any] = {}
    service_start: dict[tuple[int, int], Any] = {}
    lateness: dict[tuple[int, int], Any] = {}
    max_time_s = max(
        [_seconds(state.depot.closing_time, epoch)]
        + [_seconds(vehicle.working_end, epoch) for vehicle in vehicles]
        + [_seconds(order.hard_deadline, epoch) for order in orders]
        + [0.0]
    )
    horizon = _floor(max_time_s, config.time_scale, "time horizon")
    earliest_preferred_s = min(
        [_seconds(order.preferred_due, epoch) for order in orders] + [0.0]
    )
    lateness_horizon = _ceil(
        max_time_s - earliest_preferred_s,
        config.time_scale,
        "lateness horizon",
    )
    for vehicle_no, _vehicle in enumerate(vehicles):
        used[vehicle_no] = model.NewBoolVar(f"used_{vehicle_no}")
        for order_no in order_nodes:
            assigned[vehicle_no, order_no] = model.NewBoolVar(
                f"assigned_{vehicle_no}_{order_no}"
            )
            position[vehicle_no, order_no] = model.NewIntVar(
                0, len(orders), f"position_{vehicle_no}_{order_no}"
            )
            service_start[vehicle_no, order_no] = model.NewIntVar(
                0, horizon, f"service_start_{vehicle_no}_{order_no}"
            )
            lateness[vehicle_no, order_no] = model.NewIntVar(
                0, lateness_horizon, f"lateness_{vehicle_no}_{order_no}"
            )
        for origin in logical_nodes[:-1]:
            for destination in logical_nodes[1:]:
                if origin == destination or origin == end or destination == start:
                    continue
                arcs[vehicle_no, origin, destination] = model.NewBoolVar(
                    f"arc_{vehicle_no}_{origin}_{destination}"
                )

    for order_no in order_nodes:
        model.Add(sum(assigned[v, order_no] for v in range(len(vehicles))) <= 1)

    cost_terms = []
    distance_terms: dict[int, list[Any]] = {v: [] for v in range(len(vehicles))}
    for v, vehicle in enumerate(vehicles):
        outgoing_start = [arcs[v, start, j] for j in logical_nodes[1:]]
        incoming_end = [arcs[v, i, end] for i in logical_nodes[:-1]]
        model.Add(sum(outgoing_start) == 1)
        model.Add(sum(incoming_end) == 1)
        model.Add(arcs[v, start, end] + used[v] == 1)
        for order_no in order_nodes:
            incoming = [arcs[v, i, order_no] for i in logical_nodes[:-1]
                        if i != order_no]
            outgoing = [arcs[v, order_no, j] for j in logical_nodes[1:]
                        if j != order_no]
            model.Add(sum(incoming) == assigned[v, order_no])
            model.Add(sum(outgoing) == assigned[v, order_no])
            model.Add(position[v, order_no] == 0).OnlyEnforceIf(
                assigned[v, order_no].Not()
            )
            model.Add(position[v, order_no] >= 1).OnlyEnforceIf(
                assigned[v, order_no]
            )
            model.Add(lateness[v, order_no] == 0).OnlyEnforceIf(
                assigned[v, order_no].Not()
            )
        model.Add(sum(assigned[v, i] for i in order_nodes) >= used[v])
        model.Add(sum(assigned[v, i] for i in order_nodes) <= len(orders) * used[v])
        load = sum(
            _ceil(orders[i - 1].demand_kg, config.demand_scale, "order demand")
            * assigned[v, i]
            for i in order_nodes
        )
        capacity = _floor(
            vehicle.capacity_kg - vehicle.current_load_kg,
            config.demand_scale, "vehicle residual capacity",
        )
        model.Add(load <= capacity)
        departure_s = max(
            0.0, _seconds(state.depot.opening_time, epoch),
            _seconds(vehicle.working_start, epoch),
        )
        close_s = min(
            _seconds(state.depot.closing_time, epoch),
            _seconds(vehicle.working_end, epoch),
        )
        vehicle_window_open = close_s >= departure_s - 1e-12
        if vehicle_window_open:
            departure = _ceil(departure_s, config.time_scale, "vehicle departure")
            close = _floor(close_s, config.time_scale, "vehicle close")
        else:
            # A vehicle whose working window closed before the decision epoch
            # has no dispatch option.  It remains an unused optional vehicle;
            # it must not abort another vehicle's valid assignment domain.
            departure = close = 0
            model.Add(used[v] == 0)
            for order_no in order_nodes:
                model.Add(assigned[v, order_no] == 0)
        for order_no in order_nodes:
            order = orders[order_no - 1]
            if not vehicle_window_open:
                continue
            earliest_s = max(0.0, _seconds(order.earliest, epoch))
            latest_start_s = min(
                _seconds(order.hard_deadline, epoch),
                _seconds(vehicle.working_end, epoch),
                _seconds(state.depot.closing_time, epoch),
            ) - order.service_time_seconds
            # A completion window that is already closed makes this particular
            # order/vehicle option impossible; it does not invalidate other
            # optional orders or another vehicle with a later working window.
            if latest_start_s < max(earliest_s, departure_s) - 1e-12:
                model.Add(assigned[v, order_no] == 0)
                continue
            earliest = _ceil(earliest_s, config.time_scale, "order earliest")
            latest_start = _floor(
                latest_start_s,
                config.time_scale,
                "order latest service start",
            )
            model.Add(service_start[v, order_no] >= earliest).OnlyEnforceIf(
                assigned[v, order_no]
            )
            model.Add(service_start[v, order_no] <= latest_start).OnlyEnforceIf(
                assigned[v, order_no]
            )
            model.Add(
                lateness[v, order_no]
                >= service_start[v, order_no]
                + _ceil(order.service_time_seconds, config.time_scale, "service duration")
                - _floor_signed(
                    _seconds(order.preferred_due, epoch),
                    config.time_scale,
                    "preferred completion",
                )
            ).OnlyEnforceIf(assigned[v, order_no])

        for (vehicle_no, origin, destination), arc in arcs.items():
            if vehicle_no != v:
                continue
            distance = _ceil(metric(matrix.distance_matrix, origin, destination),
                             config.distance_scale, "arc distance")
            distance_terms[v].append(distance * arc)
            cost = _ceil(
                metric(matrix.distance_matrix, origin, destination) / 1000.0
                * vehicle.cost_per_km_vnd,
                config.cost_scale, "arc cost",
            )
            cost_terms.append(cost * arc)
            travel = _ceil(metric(matrix.time_matrix, origin, destination),
                           config.time_scale, "arc travel time")
            if origin == start:
                origin_time = departure
                service = 0
            else:
                origin_time = service_start[v, origin]
                service = _ceil(orders[origin - 1].service_time_seconds,
                                config.time_scale, "service duration")
            if destination == end:
                if vehicle_window_open or origin != start:
                    model.Add(origin_time + service + travel <= close).OnlyEnforceIf(arc)
            else:
                model.Add(
                    service_start[v, destination] >= origin_time + service + travel
                ).OnlyEnforceIf(arc)
            if origin == start and destination != end:
                model.Add(position[v, destination] == 1).OnlyEnforceIf(arc)
            elif origin not in (start, end) and destination != end:
                model.Add(
                    position[v, destination] == position[v, origin] + 1
                ).OnlyEnforceIf(arc)
        # The one-time-path projection is a sequence proposal, not a distance
        # lower bound over every physical option.  Enforcing its distance here
        # can discard a slower/shorter feasible route.  Exact full-route range
        # is enforced when raw path columns are built and again by the
        # independent validator.

    # Exclude previously proposed logical route signatures.  Dropped nodes are
    # already determined by the selected route arcs/assignment variables.
    id_to_node = {order.order_id: index + 1 for index, order in enumerate(orders)}
    for signature_index, signature in enumerate(forbidden_signatures):
        selected = []
        for v, vehicle_id in enumerate(vehicle_ids):
            sequence = tuple(signature.get(vehicle_id, ()))
            logical = (start,) + tuple(id_to_node[item] for item in sequence) + (end,)
            selected.extend(arcs[v, a, b] for a, b in zip(logical, logical[1:]))
        if selected:
            model.Add(sum(selected) <= len(selected) - 1)

    served_expr = sum(assigned.values())
    late_expr = sum(lateness.values())
    cost_expr = sum(cost_terms)
    phase_budget = phase_time_limit_seconds or max(
        0.05, config.time_limit_seconds / 3.0
    )
    phases: list[dict[str, Any]] = []

    def run_phase(name: str, objective: str) -> tuple[Any, Any, str]:
        if objective == "MAX_SERVED":
            model.Maximize(served_expr)
        elif objective == "MIN_LATENESS":
            model.Minimize(late_expr)
        else:
            model.Minimize(cost_expr)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(phase_budget)
        solver.parameters.num_search_workers = config.num_search_workers
        solver.parameters.random_seed = config.random_seed
        status_code = solver.Solve(model)
        status_name = solver.StatusName(status_code)
        phases.append({
            "phase": name, "objective": objective, "engine_status": status_name,
            "wall_time_seconds": solver.WallTime(),
            "branches": solver.NumBranches(), "conflicts": solver.NumConflicts(),
            "objective_value": solver.ObjectiveValue()
            if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
            "best_bound": solver.BestObjectiveBound()
            if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
        })
        return solver, status_code, status_name

    def capture(solver: Any, phase: str) -> dict[str, Any]:
        sequences: dict[str, list[str]] = {}
        served_ids: set[str] = set()
        for v, vehicle_id in enumerate(vehicle_ids):
            sequence: list[str] = []
            current = start
            visited: set[int] = set()
            while current != end:
                following = [
                    destination for destination in logical_nodes[1:]
                    if destination != current
                    and (v, current, destination) in arcs
                    and solver.Value(arcs[v, current, destination])
                ]
                if len(following) != 1:
                    raise ValueError(
                        "CP-SAT route extraction found invalid successor count"
                    )
                current = following[0]
                if current not in (start, end):
                    if current in visited:
                        raise ValueError("CP-SAT route extraction found a subtour")
                    visited.add(current)
                    order_id = orders[current - 1].order_id
                    sequence.append(order_id)
                    served_ids.add(order_id)
            sequences[vehicle_id] = sequence
        return {
            "phase": phase,
            "sequences": sequences,
            "served_orders": sorted(served_ids),
            "restricted_objective": {
                "served_count": int(solver.Value(served_expr)),
                "soft_lateness_scaled": int(solver.Value(late_expr)),
                "cost_scaled": int(solver.Value(cost_expr)),
            },
        }

    def better(candidate: Mapping[str, Any], current: Mapping[str, Any] | None) -> bool:
        if current is None:
            return True
        candidate_score = candidate["restricted_objective"]
        current_score = current["restricted_objective"]
        return (
            candidate_score["served_count"],
            -candidate_score["soft_lateness_scaled"],
            -candidate_score["cost_scaled"],
        ) >= (
            current_score["served_count"],
            -current_score["soft_lateness_scaled"],
            -current_score["cost_scaled"],
        )

    incumbent: dict[str, Any] | None = None
    solver, status_code, status_name = run_phase("serve", "MAX_SERVED")
    if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        incumbent = capture(solver, "serve")
        model.Add(served_expr == incumbent["restricted_objective"]["served_count"])
        solver, status_code, status_name = run_phase("soft_due", "MIN_LATENESS")
        if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            candidate = capture(solver, "soft_due")
            if better(candidate, incumbent):
                incumbent = candidate
            model.Add(
                late_expr == incumbent["restricted_objective"]["soft_lateness_scaled"]
            )
            solver, status_code, status_name = run_phase("cost", "MIN_COST")
            if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                candidate = capture(solver, "cost")
                if better(candidate, incumbent):
                    incumbent = candidate
    if incumbent is None:
        return {
            "status": "NO_ASSIGNMENT", "engine_status": status_name,
            "phases": phases, "ortools_version": ortools.__version__,
        }

    sequences = incumbent["sequences"]
    served_ids = set(incumbent["served_orders"])
    return {
        "status": "ASSIGNMENT_FOUND",
        "engine_status": status_name,
        "ortools_version": ortools.__version__,
        "sequences": sequences,
        "served_orders": sorted(served_ids),
        "dropped_orders": sorted(set(order_ids) - served_ids),
        "restricted_objective": incumbent["restricted_objective"],
        "selection_phase": incumbent["phase"],
        "phase_sequence_complete": len(phases) == 3 and all(
            item["engine_status"] in {"OPTIMAL", "FEASIBLE"} for item in phases
        ),
        "phases": phases,
    }


def solve_physical_route_master(
    state: DecisionState,
    route_columns: Sequence[Mapping[str, Any]],
    *,
    config: M1OrToolsStaticConfig,
    phase_time_limit_seconds: float | None = None,
) -> dict[str, Any]:
    """Select actual raw-path route columns with lexicographic CP-SAT phases.

    Every coefficient comes from a physically realized, resource-feasible
    route.  The caller owns source/path validation; this master only selects a
    disjoint vehicle/order plan inside that finite domain.
    """

    try:
        import ortools
        from ortools.sat.python import cp_model
    except (ImportError, OSError) as error:
        return {"status": "ENGINE_UNAVAILABLE", "diagnostic": str(error)}
    if ortools.__version__ != "9.15.6755":
        return {
            "status": "ENGINE_VERSION_MISMATCH",
            "required": "9.15.6755", "actual": ortools.__version__,
        }

    vehicles = {item.vehicle_id for item in state.vehicles}
    orders = {item.order_id for item in state.orders}
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(route_columns):
        if not isinstance(raw, Mapping):
            raise ValueError(f"physical route column {index} must be an object")
        vehicle_id = raw.get("vehicle_id")
        sequence = raw.get("order_sequence")
        if vehicle_id not in vehicles:
            raise ValueError(f"physical route column {index} has unknown vehicle")
        if (not isinstance(sequence, list) or not sequence
            or any(not isinstance(item, str) or item not in orders for item in sequence)
            or len(sequence) != len(set(sequence))):
            raise ValueError(f"physical route column {index} has invalid order sequence")
        lateness = _ceil(
            _finite(raw.get("total_soft_lateness_s"),
                    f"route column {index} lateness"),
            config.time_scale, f"route column {index} lateness",
        )
        cost = _ceil(
            _finite(raw.get("total_cost_vnd"), f"route column {index} cost"),
            config.cost_scale, f"route column {index} cost",
        )
        normalized.append({
            "index": index, "vehicle_id": vehicle_id,
            "orders": tuple(sequence), "lateness": lateness, "cost": cost,
        })
    if not normalized:
        return {
            "status": "NO_PHYSICAL_COLUMNS", "engine_status": "NOT_RUN",
            "phases": [], "ortools_version": ortools.__version__,
        }
    for name, values in (
        ("physical lateness objective", [item["lateness"] for item in normalized]),
        ("physical cost objective", [item["cost"] for item in normalized]),
    ):
        if sum(values) > INT64_MAX:
            raise ValueError(f"{name} aggregate can overflow signed int64")

    model = cp_model.CpModel()
    selected = [model.NewBoolVar(f"physical_route_{item['index']}")
                for item in normalized]
    for vehicle_id in sorted(vehicles):
        model.Add(sum(selected[pos] for pos, item in enumerate(normalized)
                      if item["vehicle_id"] == vehicle_id) <= 1)
    for order_id in sorted(orders):
        model.Add(sum(selected[pos] for pos, item in enumerate(normalized)
                      if order_id in item["orders"]) <= 1)

    served_expr = sum(len(item["orders"]) * selected[pos]
                      for pos, item in enumerate(normalized))
    late_expr = sum(item["lateness"] * selected[pos]
                    for pos, item in enumerate(normalized))
    cost_expr = sum(item["cost"] * selected[pos]
                    for pos, item in enumerate(normalized))
    phase_budget = phase_time_limit_seconds or max(
        0.05, config.time_limit_seconds / 3.0
    )
    phases: list[dict[str, Any]] = []

    def run_phase(name: str, objective: str) -> tuple[Any, int, str]:
        if objective == "MAX_SERVED":
            model.Maximize(served_expr)
        elif objective == "MIN_PHYSICAL_LATENESS":
            model.Minimize(late_expr)
        else:
            model.Minimize(cost_expr)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(phase_budget)
        solver.parameters.num_search_workers = config.num_search_workers
        solver.parameters.random_seed = config.random_seed
        status_code = solver.Solve(model)
        status_name = solver.StatusName(status_code)
        phases.append({
            "phase": name, "objective": objective,
            "coefficient_domain": "authenticated_physical_route_columns",
            "engine_status": status_name,
            "wall_time_seconds": solver.WallTime(),
            "branches": solver.NumBranches(), "conflicts": solver.NumConflicts(),
            "objective_value": solver.ObjectiveValue()
            if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
            "best_bound": solver.BestObjectiveBound()
            if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
        })
        return solver, status_code, status_name

    def capture(solver: Any, phase: str) -> dict[str, Any]:
        return {
            "selection_phase": phase,
            "selected_route_indexes": [
                item["index"] for pos, item in enumerate(normalized)
                if solver.Value(selected[pos])
            ],
            "restricted_objective": {
                "served_count": int(solver.Value(served_expr)),
                "soft_lateness_scaled": int(solver.Value(late_expr)),
                "cost_scaled": int(solver.Value(cost_expr)),
            },
        }

    def better(candidate: Mapping[str, Any], current: Mapping[str, Any] | None) -> bool:
        if current is None:
            return True
        candidate_score = candidate["restricted_objective"]
        current_score = current["restricted_objective"]
        return (
            candidate_score["served_count"],
            -candidate_score["soft_lateness_scaled"],
            -candidate_score["cost_scaled"],
        ) >= (
            current_score["served_count"],
            -current_score["soft_lateness_scaled"],
            -current_score["cost_scaled"],
        )

    incumbent: dict[str, Any] | None = None
    solver, status_code, status_name = run_phase("serve", "MAX_SERVED")
    if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        incumbent = capture(solver, "serve")
        model.Add(served_expr == incumbent["restricted_objective"]["served_count"])
        solver, status_code, status_name = run_phase(
            "soft_due", "MIN_PHYSICAL_LATENESS"
        )
        if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            candidate = capture(solver, "soft_due")
            if better(candidate, incumbent):
                incumbent = candidate
            model.Add(
                late_expr == incumbent["restricted_objective"]["soft_lateness_scaled"]
            )
            solver, status_code, status_name = run_phase("cost", "MIN_PHYSICAL_COST")
            if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                candidate = capture(solver, "cost")
                if better(candidate, incumbent):
                    incumbent = candidate
    if incumbent is None or not incumbent["selected_route_indexes"]:
        return {
            "status": "NO_SELECTION", "engine_status": status_name,
            "phases": phases, "ortools_version": ortools.__version__,
            "phase_sequence_complete": False,
            "restricted_optimality_proven": False,
            "restricted_objective": (
                incumbent["restricted_objective"] if incumbent is not None else None
            ),
            "selection_phase": (
                incumbent["selection_phase"] if incumbent is not None else None
            ),
        }

    return {
        "status": "PHYSICAL_PLAN_FOUND", "engine_status": status_name,
        "ortools_version": ortools.__version__,
        "selected_route_indexes": incumbent["selected_route_indexes"],
        "restricted_objective": incumbent["restricted_objective"],
        "selection_phase": incumbent["selection_phase"],
        "phase_sequence_complete": len(phases) == 3 and all(
            item["engine_status"] in {"OPTIMAL", "FEASIBLE"} for item in phases
        ),
        "restricted_optimality_proven": len(phases) == 3 and all(
            item["engine_status"] == "OPTIMAL" for item in phases
        ),
        "limited_phase": next(
            (item["phase"] for item in phases
             if item["engine_status"] not in {"OPTIMAL", "FEASIBLE"}),
            None,
        ),
        "phases": phases,
    }


__all__ = [
    "INT64_MAX", "MODEL_CONFIG_VERSION", "M1OrToolsStaticConfig",
    "OBJECTIVE_POLICY_VERSION", "SCALING_VERSION",
    "solve_physical_route_master", "solve_restricted_sequence_model",
]
