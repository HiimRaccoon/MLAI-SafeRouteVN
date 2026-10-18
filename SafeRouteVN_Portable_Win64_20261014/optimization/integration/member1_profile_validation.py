"""Independent raw-SQLite validator for M1 Step-3 profile witnesses.

The arithmetic and finite-domain oracle intentionally do not import the
solver scorer or master. Validation certifies a physical incumbent and its
metadata; restricted optimality is checked only when it is claimed.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from itertools import product
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from optimization.models.decision_state import DecisionState

from .member1_profiles import DOMAIN_VERSION, PROFILE_ORDER, SOLUTION_VERSION, SOLVER_VERSION
from .member1_s0_graph import Member1RoadGraph
from .member1_static_validation import validate_static_solution

VALIDATOR_VERSION = "task02-m1-profile-independent-validator/4"
_MASTER_VERSION = "task02-m1-profile-route-column-master/2"
_ENGINE = "Google OR-Tools CP-SAT"
_NATIVE = {"OPTIMAL", "FEASIBLE", "INFEASIBLE", "MODEL_INVALID", "UNKNOWN"}
_INT64_MAX = 9_223_372_036_854_775_807


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _number(value: Any, path: str) -> Decimal:
    if type(value) not in (int, float) or value < 0:
        raise ValueError(f"{path} must be finite and nonnegative")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must be finite and nonnegative")
    return Decimal(str(value))


def _finite_float(value: Any) -> float | None:
    """Return a finite numeric projection without leaking integer overflow."""
    if type(value) not in (int, float):
        return None
    try:
        projected = float(value)
    except OverflowError:
        return None
    return projected if math.isfinite(projected) else None


def _fleet_selection_error(indexes: Sequence[int],
                           columns: Sequence[Mapping[str, Any]],
                           state: DecisionState) -> str | None:
    vehicle_ids = {item.vehicle_id for item in state.vehicles}
    order_ids = {item.order_id for item in state.orders}
    used_vehicles: set[str] = set()
    used_orders: set[str] = set()
    for index in indexes:
        column = columns[index]
        vehicle = column.get("vehicle_id")
        sequence = column.get("order_sequence")
        if not isinstance(vehicle, str) or vehicle not in vehicle_ids:
            return f"column {index} references an unknown vehicle"
        if vehicle in used_vehicles:
            return f"vehicle {vehicle} is selected more than once"
        used_vehicles.add(vehicle)
        if (not isinstance(sequence, list) or not sequence
                or any(not isinstance(item, str) or not item for item in sequence)):
            return f"column {index} order sequence is invalid"
        if len(sequence) != len(set(sequence)):
            return f"column {index} repeats an order"
        unknown = set(sequence) - order_ids
        if unknown:
            return f"column {index} references unknown orders {sorted(unknown)}"
        duplicate = used_orders.intersection(sequence)
        if duplicate:
            return f"orders are selected more than once {sorted(duplicate)}"
        used_orders.update(sequence)
    return None


def _independent_score(route: Mapping[str, Any], profile: Mapping[str, Any],
                       refs: Mapping[str, Any], scale: int) -> int:
    if type(scale) is not int or isinstance(scale, bool) or scale <= 0:
        raise ValueError("score scale must be a positive integer")
    tref = _number(refs.get("travel_time_s"), "references.travel_time_s")
    dref = _number(refs.get("distance_m"), "references.distance_m")
    rref = _number(refs.get("relative_exposure_proxy"), "references.relative_exposure_proxy")
    if min(tref, dref, rref) <= 0:
        raise ValueError("normalization references must be positive")
    score = (
        _number(profile.get("time"), "profile.time")
        * _number(route.get("total_travel_time_s"), "route.total_travel_time_s") / tref
        + _number(profile.get("risk"), "profile.risk")
        * _number(route.get("total_exposure"), "route.total_exposure") / rref
        + _number(profile.get("distance"), "profile.distance")
        * _number(route.get("total_distance_m"), "route.total_distance_m") / dref
    )
    result = int((score * Decimal(scale)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if result > _INT64_MAX:
        raise ValueError("profile score exceeds signed int64")
    return result


def _domain_digest(domain: Mapping[str, Any]) -> str:
    body = dict(domain)
    body.pop("content_sha256", None)
    return hashlib.sha256(_canonical(body)).hexdigest()


def _vector(indexes: Sequence[int], columns: Sequence[Mapping[str, Any]],
            profile: Mapping[str, Any], refs: Mapping[str, Any], scale: int
            ) -> tuple[int, ...]:
    picked = [columns[index] for index in indexes]
    ties = list(profile.get("tie_break", ()))
    raw = {
        "time": sum(math.ceil(float(item["total_travel_time_s"]) * 1000 - 1e-12)
                    for item in picked),
        "distance": sum(math.ceil(float(item["total_distance_m"]) * 1000 - 1e-12)
                        for item in picked),
        "risk": sum(math.ceil(float(item["total_exposure"]) * 1_000_000 - 1e-12)
                    for item in picked),
    }
    return (
        sum(len(item["order_sequence"]) for item in picked),
        -sum(math.ceil(float(item["total_soft_lateness_s"]) * 1000 - 1e-12)
             for item in picked),
        -sum(_independent_score(item, profile, refs, scale) for item in picked),
        *(-raw[key] for key in ties),
        -sum(index + 1 for index in indexes),
    )


def _phase_value(key: str, indexes: Sequence[int],
                 columns: Sequence[Mapping[str, Any]], profile: Mapping[str, Any],
                 refs: Mapping[str, Any], scale: int) -> int:
    picked = [columns[index] for index in indexes]
    if key == "served":
        return sum(len(item["order_sequence"]) for item in picked)
    if key == "lateness":
        return sum(math.ceil(float(item["total_soft_lateness_s"]) * 1000 - 1e-12)
                   for item in picked)
    if key == "profile":
        return sum(_independent_score(item, profile, refs, scale) for item in picked)
    if key == "time":
        return sum(math.ceil(float(item["total_travel_time_s"]) * 1000 - 1e-12)
                   for item in picked)
    if key == "distance":
        return sum(math.ceil(float(item["total_distance_m"]) * 1000 - 1e-12)
                   for item in picked)
    if key == "risk":
        return sum(math.ceil(float(item["total_exposure"]) * 1_000_000 - 1e-12)
                   for item in picked)
    if key == "plan_key":
        return sum(index + 1 for index in indexes)
    raise ValueError(f"unknown phase objective {key}")


def _oracle(columns: Sequence[Mapping[str, Any]], vehicle_ids: Sequence[str],
            profile: Mapping[str, Any], refs: Mapping[str, Any], scale: int,
            *, max_combinations: int = 300_000) -> tuple[tuple[int, ...] | None, bool]:
    choices = {vehicle: [None] + [index for index, item in enumerate(columns)
                                  if item.get("vehicle_id") == vehicle]
               for vehicle in sorted(set(vehicle_ids))}
    best = None
    checked = 0
    for values in product(*(choices[item] for item in sorted(choices))):
        checked += 1
        if checked > max_combinations:
            return best, False
        indexes = sorted(index for index in values if index is not None)
        orders = [order for index in indexes for order in columns[index].get("order_sequence", ())]
        if len(orders) != len(set(orders)):
            continue
        vector = _vector(indexes, columns, profile, refs, scale)
        if best is None or vector > best:
            best = vector
    return best, True


def _authenticate_domain_column(column: Mapping[str, Any], state: DecisionState,
                                graph: Member1RoadGraph) -> list[dict[str, Any]]:
    """Independently certify one route column against raw SQLite.

    This intentionally uses the static validator's raw-edge feasibility
    machinery, not profile proposal/search/scoring code.  It authenticates
    every domain column before any restricted-model certificate is trusted.
    """
    sequence = column.get("order_sequence")
    if (not isinstance(sequence, list) or not sequence
            or any(not isinstance(item, str) or not item for item in sequence)):
        return [{"severity": "ERROR", "code": "PROFILE_COLUMN_SCHEMA",
                 "message": "order_sequence must be a nonempty array of order IDs",
                 "context": {"path": "order_sequence"}}]
    served = list(sequence)
    all_orders = [item.order_id for item in state.orders]
    unserved = [item for item in all_orders if item not in served]
    metrics = {key: column.get(key) for key in
               ("total_distance_m", "total_travel_time_s", "total_exposure",
                "total_elapsed_time_s", "total_soft_lateness_s", "total_cost_vnd")}
    result = {
        "schema_version": SOLUTION_VERSION, "solver_version": SOLVER_VERSION,
        "scenario_id": state.scenario_id, "decision_epoch": state.decision_epoch,
        "source_authentication": state.source_authentication,
        "source_versions": {"routing_version": state.routing_version,
                            "features_version": state.features_version,
                            "context_version": state.context_version,
                            "delivery_area_version": state.delivery_area_version,
                            "state_version": state.state_version,
                            "state_schema_version": state.schema_version},
        "source_hashes": dict(graph.source_hashes), "receipt_sha256": state.receipt_sha256,
        "status": "FEASIBLE" if len(served) == len(all_orders) else "PARTIAL",
        "served_orders": served,
        "unserved_orders": [{"order_id": item,
                              "reason": "NOT_SERVED_BY_FOUND_WITNESS"}
                             for item in unserved],
        "vehicle_routes": [dict(column)], "metrics": metrics,
        "search": {"optimality_proven": False, "search_complete": False,
                   "truncated": True,
                   "truncation_reasons": ["FINITE_SHARED_PHYSICAL_DOMAIN"],
                   "limits": {}, "route_states": 0, "road_queries": 0,
                   "road_settled_states": 0, "road_generated_labels": 0,
                   "road_dominated_prunes": 0, "road_resource_prunes": 0,
                   "road_peak_active_labels": 0, "assignments_considered": 0,
                   "elapsed_seconds": 0.0},
    }
    checked = validate_static_solution(
        result, state, graph, expected_schema_version=SOLUTION_VERSION,
        expected_solver_version=SOLVER_VERSION,
        reported_validator_version=VALIDATOR_VERSION,
        supported_scenarios=frozenset({"S1", "S7", "S8"}),
    )
    return list(checked.get("diagnostics", ())) if checked.get("valid") is not True else []


def validate_profile_solution(result: Mapping[str, Any], state: DecisionState,
                              graph: Member1RoadGraph, domain: Mapping[str, Any],
                              config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"status": "FAILED", "valid": False,
                "diagnostics": [{"severity": "ERROR", "code": "PROFILE_SCHEMA",
                                 "message": "solution root must be an object",
                                 "context": {"path": "$"}}],
                "validator_version": VALIDATOR_VERSION,
                "scope": "physical incumbent feasibility and binding; not global optimality"}
    base = validate_static_solution(
        result, state, graph, expected_schema_version=SOLUTION_VERSION,
        expected_solver_version=SOLVER_VERSION, reported_validator_version=VALIDATOR_VERSION,
        supported_scenarios=frozenset({"S1", "S7", "S8"}),
        allow_unrecorded_counters=True,
    )
    issues = list(base.get("diagnostics", ()))

    def bad(code: str, message: str, path: str) -> None:
        issues.append({"severity": "ERROR", "code": code, "message": message,
                       "context": {"path": path}})

    profile = result.get("profile")
    name = profile.get("name") if isinstance(profile, Mapping) else None
    if not isinstance(name, str) or name not in PROFILE_ORDER:
        bad("PROFILE_METADATA", "profile name is invalid", "profile.name")
    elif (profile.get("policy_version") != config.get("policy_version")
          or profile.get("normalization_version") != config.get("normalization_version")
          or profile.get("references") != config.get("references")
          or profile.get("weights") != config.get("profiles", {}).get(name)
          or profile.get("score_scale") != config.get("score_scale")
          or profile.get("production_calibrated") is not False):
        bad("PROFILE_METADATA", "profile policy/reference/scale binding mismatch", "profile")

    columns = domain.get("columns") if isinstance(domain, Mapping) else None
    digest = None
    domain_columns_valid = True
    if not isinstance(domain, Mapping):
        bad("PROFILE_DOMAIN_BINDING", "domain must be an object", "physical_domain")
    else:
        try:
            digest = _domain_digest(domain)
        except (TypeError, ValueError, OverflowError) as error:
            bad("PROFILE_DOMAIN_BINDING", str(error), "physical_domain")
        if (domain.get("schema_version") != DOMAIN_VERSION
                or domain.get("scenario_id") != state.scenario_id
                or domain.get("state_fixture_sha256") != state.fixture_raw_sha256
                or domain.get("source_hashes") != dict(graph.source_hashes)
                or digest != domain.get("content_sha256")):
            bad("PROFILE_DOMAIN_BINDING", "domain body/state/source digest mismatch", "physical_domain")
        expected_identity = {"schema_version": state.schema_version,
                             "state_version": state.state_version,
                             "decision_epoch": state.decision_epoch,
                             "context_version": state.context_version}
        if domain.get("state_identity") != expected_identity:
            bad("PROFILE_DOMAIN_STATE_BINDING",
                "domain state identity differs from the trusted DecisionState",
                "physical_domain.state_identity")
    if not isinstance(columns, list):
        bad("PROFILE_DOMAIN_BINDING", "domain columns must be an array", "physical_domain.columns")
        columns = []
        domain_columns_valid = False
    physical = result.get("physical_domain")
    if (not isinstance(physical, Mapping) or physical.get("schema_version") != DOMAIN_VERSION
            or physical.get("content_sha256") != digest
            or physical.get("column_count") != len(columns)
            or physical.get("shared_by_profiles") != list(PROFILE_ORDER)):
        bad("PROFILE_DOMAIN_BINDING", "solution is not bound to the trusted shared domain",
            "physical_domain")

    domain_by_id: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(columns):
        if not isinstance(item, Mapping):
            bad("PROFILE_DOMAIN_COLUMN_SCHEMA", "domain column must be an object",
                f"physical_domain.columns[{index}]")
            domain_columns_valid = False
            continue
        sequence = item.get("order_sequence")
        if (not isinstance(sequence, list) or not sequence
                or any(not isinstance(value, str) or not value for value in sequence)):
            bad("PROFILE_DOMAIN_COLUMN_SCHEMA",
                "order_sequence must be a nonempty array of order IDs",
                f"physical_domain.columns[{index}].order_sequence")
            domain_columns_valid = False
            continue
        identifier = item.get("route_column_id")
        if not isinstance(identifier, str) or not identifier or identifier in domain_by_id:
            bad("PROFILE_DOMAIN_BINDING", "domain column ID is invalid or duplicated",
                f"domain.columns[{index}].route_column_id")
            domain_columns_valid = False
        else:
            domain_by_id[identifier] = item
            for diagnostic in _authenticate_domain_column(item, state, graph):
                context = dict(diagnostic.get("context") or {})
                context["path"] = f"physical_domain.columns[{index}]." + str(
                    context.get("path", "physical_route"))
                issues.append({"severity": "ERROR", "code": "PROFILE_DOMAIN_PHYSICAL_INVALID",
                               "message": diagnostic.get("message", "raw column failed validation"),
                               "context": context})
    routes = result.get("vehicle_routes")
    if not isinstance(routes, list):
        bad("PROFILE_SCHEMA", "vehicle_routes must be an array", "vehicle_routes")
        routes = []
    route_ids: list[str] = []
    for index, route in enumerate(routes):
        identifier = route.get("route_column_id") if isinstance(route, Mapping) else None
        if not isinstance(identifier, str) or not identifier:
            bad("PROFILE_COLUMN_BINDING", "route column ID must be a nonempty string",
                f"vehicle_routes[{index}].route_column_id")
            continue
        route_ids.append(identifier)
        candidate = domain_by_id.get(identifier)
        if candidate is None or route != candidate:
            bad("PROFILE_COLUMN_BINDING", "selected route differs from domain column",
                f"vehicle_routes[{index}]")

    if name in PROFILE_ORDER:
        try:
            expected_score = sum(_independent_score(item, config["profiles"][name],
                                                     config["references"], config["score_scale"])
                                 for item in routes if isinstance(item, Mapping))
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            bad("PROFILE_SCORE", str(error), "profile.score_scaled")
        else:
            if not isinstance(profile, Mapping) or profile.get("score_scaled") != expected_score:
                bad("PROFILE_SCORE", "profile score differs from independently recomputed columns",
                    "profile.score_scaled")

    engine = result.get("engine")
    if (not isinstance(engine, Mapping) or engine.get("name") != _ENGINE
            or engine.get("master_version") != _MASTER_VERSION
            or engine.get("restricted_model") is not True
            or not isinstance(engine.get("ortools_version"), str)):
        bad("PROFILE_ENGINE", "engine identity/version is invalid", "engine")

    search = result.get("search")
    restricted = False
    selected_indexes: list[int] = []
    if not isinstance(search, Mapping):
        bad("PROFILE_SEARCH_METADATA", "search metadata must be an object", "search")
    else:
        native = search.get("engine_status")
        if not isinstance(native, str) or native not in _NATIVE:
            bad("PROFILE_SEARCH_METADATA", "engine status is invalid", "search.engine_status")
        phases = search.get("phases")
        expected_names = ["serve", "soft_due", "profile"]
        if name in PROFILE_ORDER:
            expected_names += [f"tie_{item}" for item in config["profiles"][name]["tie_break"]]
        expected_names += ["deterministic_plan_key"]
        if not isinstance(phases, list) or not phases:
            bad("PROFILE_SEARCH_METADATA", "phase telemetry must be a nonempty array", "search.phases")
            phases = []
        feasible_phase_records: dict[str, tuple[list[int], tuple[int, ...]]] = {}
        fixed_optimal_objectives: dict[str, int] = {}
        for index, phase in enumerate(phases):
            path = f"search.phases[{index}]"
            if not isinstance(phase, Mapping):
                bad("PROFILE_SEARCH_METADATA", "phase sequence/status is invalid",
                    path)
                continue
            expected_phase = expected_names[index] if index < len(expected_names) else None
            expected_key = ({"serve": "served", "soft_due": "lateness",
                             "profile": "profile",
                             "deterministic_plan_key": "plan_key"}.get(expected_phase)
                            if expected_phase else None)
            if expected_phase and expected_phase.startswith("tie_"):
                expected_key = expected_phase[4:]
            expected_maximize = expected_phase == "serve"
            engine_status = phase.get("engine_status")
            if (expected_phase is None or phase.get("phase") != expected_phase
                    or phase.get("objective_key") != expected_key
                    or phase.get("maximize") is not expected_maximize
                    or not isinstance(engine_status, str) or engine_status not in _NATIVE):
                bad("PROFILE_SEARCH_METADATA", "phase sequence/objective/status is invalid", path)
                continue
            phase_indexes = phase.get("selected_route_indexes")
            if engine_status in {"OPTIMAL", "FEASIBLE"}:
                if (not isinstance(phase_indexes, list)
                        or any(type(item) is not int or item < 0 or item >= len(columns)
                               for item in phase_indexes)
                        or len(phase_indexes) != len(set(phase_indexes))):
                    bad("PROFILE_SEARCH_METADATA", "phase route indexes are invalid",
                        f"{path}.selected_route_indexes")
                    continue
                fleet_error = (_fleet_selection_error(phase_indexes, columns, state)
                               if domain_columns_valid else
                               "domain columns are not structurally valid")
                if fleet_error is not None:
                    bad("PROFILE_PHASE_FLEET_INVALID", fleet_error,
                        f"{path}.selected_route_indexes")
                    continue
                try:
                    vector = _vector(phase_indexes, columns, config["profiles"][name],
                                     config["references"], config["score_scale"])
                    objective = _phase_value(expected_key, phase_indexes, columns,
                                             config["profiles"][name], config["references"],
                                             config["score_scale"])
                except (KeyError, TypeError, ValueError, OverflowError) as error:
                    bad("PROFILE_SEARCH_METADATA", str(error), path)
                    continue
                if phase.get("selection_vector") != list(vector):
                    bad("PROFILE_SEARCH_METADATA", "phase selection vector is inconsistent",
                        f"{path}.selection_vector")
                value = phase.get("objective_value")
                projected = _finite_float(value)
                if projected is None or abs(projected - objective) > 1e-6:
                    bad("PROFILE_SEARCH_METADATA", "phase objective value is inconsistent",
                        f"{path}.objective_value")
                # CP-SAT fixes every phase proved OPTIMAL with an equality
                # constraint before solving the following phase.  Recompute
                # those fixed objectives from authenticated columns instead
                # of trusting the recorded phase history.  Merely FEASIBLE
                # phases deliberately do not become equality constraints.
                for fixed_key, fixed_value in fixed_optimal_objectives.items():
                    current_value = _phase_value(
                        fixed_key, phase_indexes, columns,
                        config["profiles"][name], config["references"],
                        config["score_scale"])
                    if current_value != fixed_value:
                        bad(
                            "PROFILE_PHASE_HISTORY",
                            "phase selection contradicts a previously fixed optimal objective",
                            f"{path}.selected_route_indexes",
                        )
                if engine_status == "OPTIMAL":
                    fixed_optimal_objectives[expected_key] = objective
                feasible_phase_records[expected_phase] = (phase_indexes, vector)
            elif phase_indexes is not None or phase.get("selection_vector") is not None:
                bad("PROFILE_SEARCH_METADATA", "non-feasible phase cannot claim a selection", path)
        if len(phases) > len(expected_names):
            bad("PROFILE_SEARCH_METADATA", "unexpected extra phase telemetry", "search.phases")
        if phases and isinstance(phases[-1], Mapping) and native != phases[-1].get("engine_status"):
            bad("PROFILE_SEARCH_METADATA", "engine status differs from final phase",
                "search.engine_status")
        raw_indexes = search.get("selected_route_indexes")
        if (not isinstance(raw_indexes, list)
                or any(type(item) is not int or item < 0 or item >= len(columns)
                       for item in raw_indexes)
                or len(raw_indexes) != len(set(raw_indexes))):
            bad("PROFILE_SEARCH_METADATA", "selected route indexes are invalid",
                "search.selected_route_indexes")
        else:
            selected_indexes = raw_indexes
            fleet_error = (_fleet_selection_error(raw_indexes, columns, state)
                           if domain_columns_valid else
                           "domain columns are not structurally valid")
            if fleet_error is not None:
                bad("PROFILE_SELECTED_FLEET_INVALID", fleet_error,
                    "search.selected_route_indexes")
            indexed_ids = ([columns[index].get("route_column_id") for index in raw_indexes]
                           if domain_columns_valid else [])
            if (not domain_columns_valid or indexed_ids != route_ids
                    or search.get("selected_route_column_ids") != route_ids):
                bad("PROFILE_SEARCH_METADATA", "selected indexes/IDs differ from routes",
                    "search.selected_route_indexes")
        restricted = search.get("restricted_model_optimal") is True
        if type(search.get("restricted_model_optimal")) is not bool:
            bad("PROFILE_SEARCH_METADATA", "restricted optimality flag must be boolean",
                "search.restricted_model_optimal")
        if restricted and (len(phases) != len(expected_names)
                           or any(not isinstance(item, Mapping)
                                  or item.get("engine_status") != "OPTIMAL"
                                  for item in phases)):
            bad("PROFILE_SEARCH_METADATA", "restricted optimum claim lacks all optimal phases",
                "search.restricted_model_optimal")
        if (search.get("search_complete") is not False
                or search.get("optimality_proven") is not False):
            bad("PROFILE_SEARCH_SCOPE", "finite domain cannot claim global completion/optimality",
                "search")
        if not isinstance(search.get("incumbent_origin"), str):
            bad("PROFILE_SEARCH_METADATA", "incumbent origin is missing", "search.incumbent_origin")
        origin = search.get("incumbent_origin")
        if origin == "AUTHENTICATED_DOMAIN_FLEET_SEED":
            pass
        elif isinstance(origin, str) and origin.startswith("ENGINE_PHASE:"):
            origin_phase = origin.split(":", 1)[1]
            record = feasible_phase_records.get(origin_phase)
            if record is None or record[0] != selected_indexes:
                bad("PROFILE_SEARCH_METADATA", "incumbent origin is not bound to selected routes",
                    "search.incumbent_origin")
        else:
            bad("PROFILE_SEARCH_METADATA", "incumbent origin is not recognized",
                "search.incumbent_origin")
        if name in PROFILE_ORDER and selected_indexes:
            try:
                incumbent_vector = list(_vector(
                    selected_indexes, columns, config["profiles"][name],
                    config["references"], config["score_scale"]))
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                bad("PROFILE_SEARCH_METADATA", str(error), "search.incumbent_vector")
            else:
                if search.get("incumbent_vector") != incumbent_vector:
                    bad("PROFILE_SEARCH_METADATA", "incumbent vector differs from selected routes",
                        "search.incumbent_vector")
        termination = search.get("termination_reason")
        if not isinstance(termination, str) or termination not in {
                "ALL_PHASES_OPTIMAL", "BEST_KNOWN_FEASIBLE_INCUMBENT_RETAINED"}:
            bad("PROFILE_SEARCH_METADATA", "termination reason is missing", "search.termination_reason")
        elif termination == "ALL_PHASES_OPTIMAL" and not (
                len(phases) == len(expected_names)
                and all(isinstance(item, Mapping)
                        and item.get("engine_status") == "OPTIMAL" for item in phases)):
            bad("PROFILE_SEARCH_METADATA", "all-phases-optimal termination is contradictory",
                "search.termination_reason")
        elif termination == "BEST_KNOWN_FEASIBLE_INCUMBENT_RETAINED" and restricted:
            bad("PROFILE_SEARCH_METADATA", "retained-incumbent termination contradicts optimum flag",
                "search.termination_reason")

    if (restricted and name in PROFILE_ORDER and selected_indexes
            and domain_columns_valid):
        try:
            selected_vector = _vector(selected_indexes, columns, config["profiles"][name],
                                      config["references"], config["score_scale"])
            best_vector, complete = _oracle(columns, [item.vehicle_id for item in state.vehicles],
                                            config["profiles"][name], config["references"],
                                            config["score_scale"])
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            bad("PROFILE_SELECTION", str(error), "vehicle_routes")
        else:
            if not complete:
                bad("PROFILE_SELECTION_CERTIFICATE", "independent oracle budget was exhausted",
                    "search.restricted_model_optimal")
            elif selected_vector != best_vector:
                bad("PROFILE_SELECTION", "restricted optimum claim differs from independent oracle",
                    "vehicle_routes")

    valid = not issues and base.get("valid") is True
    return {"status": "PASSED" if valid else "FAILED", "valid": valid,
            "diagnostics": issues, "validator_version": VALIDATOR_VERSION,
            "scope": "physical incumbent feasibility, independent score and binding; not global optimality"}


__all__ = ["VALIDATOR_VERSION", "validate_profile_solution"]
