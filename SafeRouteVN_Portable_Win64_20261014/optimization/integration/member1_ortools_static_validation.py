"""Independent raw-SQLite validator boundary for the M1 OR-Tools bridge."""

from __future__ import annotations

import math
from typing import Any, Mapping

from optimization.models.decision_state import DecisionState
from optimization.solver.member1_static_cp_sat import (
    MODEL_CONFIG_VERSION,
    OBJECTIVE_POLICY_VERSION,
    SCALING_VERSION,
)

from .member1_ortools_static import MODEL_INPUT_VERSION, SCHEMA_VERSION, SOLVER_VERSION
from .member1_s0_graph import Member1RoadGraph
from .member1_static_validation import validate_static_solution


VALIDATOR_VERSION = "member1-ortools-static-independent-validator/4"
_V2_SCHEMA_VERSION = "member1-ortools-static-solution/2"
_V2_SOLVER_VERSION = "member1-ortools-cp-sat-static-bridge/2"
_V2_MODEL_CONFIG_VERSION = "member1-ortools-static-model-config/2"
_V3_SCHEMA_VERSION = "member1-ortools-static-solution/3"
_V3_SOLVER_VERSION = "member1-ortools-cp-sat-static-bridge/3"
_V3_MODEL_CONFIG_VERSION = "member1-ortools-static-model-config/3"


def validate_ortools_static_solution(
    result: Mapping[str, Any], state: DecisionState, graph: Member1RoadGraph,
) -> dict[str, Any]:
    """Validate the witness from raw SQLite; never invoke solver/path search."""

    version_pair = (result.get("schema_version"), result.get("solver_version")) \
        if isinstance(result, Mapping) else (None, None)
    v2 = version_pair == (_V2_SCHEMA_VERSION, _V2_SOLVER_VERSION)
    v3 = version_pair == (_V3_SCHEMA_VERSION, _V3_SOLVER_VERSION)
    current = version_pair == (SCHEMA_VERSION, SOLVER_VERSION)
    supported = v2 or v3 or current
    checked = validate_static_solution(
        result, state, graph,
        expected_schema_version=(
            _V2_SCHEMA_VERSION if v2 else _V3_SCHEMA_VERSION if v3 else SCHEMA_VERSION
        ),
        expected_solver_version=(
            _V2_SOLVER_VERSION if v2 else _V3_SOLVER_VERSION if v3 else SOLVER_VERSION
        ),
        reported_validator_version=VALIDATOR_VERSION,
    )
    issues = list(checked.get("diagnostics", ()))

    def bad(code: str, message: str, *, path: str, **context: Any) -> None:
        issues.append({
            "severity": "ERROR", "code": code, "message": message,
            "context": {"path": path, **context},
        })

    if not isinstance(result, Mapping):
        bad("SOLUTION_SCHEMA", "solution must be an object", path="$")
    else:
        status = result.get("status")
        if not supported:
            bad(
                "SOLUTION_VERSION",
                "solution/solver version is not supported by this validator",
                path="schema_version/solver_version",
            )
        if not isinstance(status, str):
            bad("SOLUTION_STATUS_TYPE", "status must be a string", path="status")
        engine = result.get("engine")
        if not isinstance(engine, Mapping):
            bad("ENGINE_METADATA", "engine metadata is required", path="engine")
        else:
            expected = {
                "name": "Google OR-Tools CP-SAT",
                "ortools_version": "9.15.6755",
                "model_config_version": (
                    _V2_MODEL_CONFIG_VERSION if v2 else
                    _V3_MODEL_CONFIG_VERSION if v3 else MODEL_CONFIG_VERSION
                ),
                "objective_policy_version": OBJECTIVE_POLICY_VERSION,
                "scaling_version": SCALING_VERSION,
                "warm_start_used": False,
                "restricted_model": True,
            }
            for key, value in expected.items():
                if engine.get(key) != value:
                    bad("ENGINE_METADATA", "engine claim differs from bridge contract",
                        path=f"engine.{key}", expected=value, actual=engine.get(key))
        pool = result.get("path_pool")
        if not isinstance(pool, Mapping):
            bad("PATH_POOL_METADATA", "path-pool metadata is required", path="path_pool")
        else:
            digest = pool.get("content_sha256")
            if (pool.get("schema_version") != MODEL_INPUT_VERSION
                or pool.get("gate") != "READY_WITH_FINITE_PATH_DOMAIN"
                or type(pool.get("pair_count")) is not int
                or type(pool.get("required_pair_count")) is not int
                or pool.get("pair_count") != pool.get("required_pair_count")
                or not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)):
                bad("PATH_POOL_METADATA", "path-pool identity/coverage is invalid",
                    path="path_pool")
        search = result.get("search")
        if isinstance(search, Mapping):
            phases = search.get("phases")
            engine_status = search.get("engine_status")
            allowed_engine_statuses = {
                "OPTIMAL", "FEASIBLE", "UNKNOWN", "INFEASIBLE"
            } | ({"NOT_RUN", "MODEL_INVALID"} if (v3 or current) else set())
            if engine_status is not None and (
                not isinstance(engine_status, str)
                or engine_status not in allowed_engine_statuses
            ):
                bad("ENGINE_STATUS_TYPE", "engine_status is invalid",
                    path="search.engine_status")
            if v2 and isinstance(status, str) and status in {"FEASIBLE", "PARTIAL"}:
                if (not isinstance(phases, list) or len(phases) != 3
                    or [item.get("phase") for item in phases
                        if isinstance(item, Mapping)] != ["serve", "soft_due", "cost"]
                    or any(not isinstance(item, Mapping)
                           or not isinstance(item.get("engine_status"), str)
                           or item.get("engine_status") not in {"OPTIMAL", "FEASIBLE"}
                           for item in phases)):
                    bad("ENGINE_PHASE_METADATA",
                        "witness requires three successful CP-SAT objective phases",
                        path="search.phases")
            elif v3 or current:
                _validate_incumbent_search_contract(
                    result,
                    status,
                    search,
                    phases,
                    engine_status,
                    bad,
                    require_ordered_domain=current,
                )
            if search.get("optimality_proven") is not False:
                bad("OPTIMALITY_CLAIM_UNSUPPORTED",
                    "finite path domain cannot prove global optimality",
                    path="search.optimality_proven")
            if search.get("search_complete") is not False:
                bad("SEARCH_COMPLETENESS_CLAIM_UNSUPPORTED",
                    "finite path domain cannot claim complete global search",
                    path="search.search_complete")

    return {
        "status": "FAILED" if issues else checked.get("status", "NOT_RUN"),
        "valid": False if issues else checked.get("valid"),
        "diagnostics": issues,
        "validator_version": VALIDATOR_VERSION,
        "scope": (
            "raw-SQLite witness feasibility, engine/path metadata consistency; "
            "not global optimality or complete path coverage"
        ),
    }


def _validate_incumbent_search_contract(
    result: Mapping[str, Any],
    status: Any,
    search: Mapping[str, Any],
    phases: Any,
    engine_status: Any,
    bad: Any,
    *,
    require_ordered_domain: bool,
) -> None:
    """Validate truthful incumbent/phase binding without re-running a solver."""

    phase_names = ("serve", "soft_due", "cost")
    success = {"OPTIMAL", "FEASIBLE"}
    phase_status: dict[str, str] = {}
    phase_records: dict[str, tuple[int, Mapping[str, Any]]] = {}
    if not isinstance(phases, list):
        bad("ENGINE_PHASE_METADATA", "phases must be an array", path="search.phases")
        phases = []
    else:
        names: list[str] = []
        terminal_seen = False
        for index, item in enumerate(phases):
            path = f"search.phases[{index}]"
            if not isinstance(item, Mapping):
                bad("ENGINE_PHASE_METADATA", "phase entry must be an object", path=path)
                continue
            name = item.get("phase")
            native = item.get("engine_status")
            if not isinstance(name, str) or name not in phase_names:
                bad("ENGINE_PHASE_METADATA", "phase name is invalid", path=f"{path}.phase")
                continue
            if not isinstance(native, str) or native not in {
                "OPTIMAL", "FEASIBLE", "UNKNOWN", "INFEASIBLE", "MODEL_INVALID"
            }:
                bad(
                    "ENGINE_PHASE_METADATA",
                    "phase engine status is invalid",
                    path=f"{path}.engine_status",
                )
                continue
            names.append(name)
            phase_status[name] = native
            phase_records[name] = (index, item)
            if terminal_seen:
                bad(
                    "ENGINE_PHASE_METADATA",
                    "no phase may run after a non-solution phase",
                    path=path,
                )
            terminal_seen = native not in success
            objective = item.get("objective_value")
            bound = item.get("best_bound")
            if native in success:
                if type(objective) not in (int, float) or not math.isfinite(float(objective)):
                    bad(
                        "ENGINE_PHASE_METADATA",
                        "successful phase requires a finite objective",
                        path=f"{path}.objective_value",
                    )
            elif objective is not None or bound is not None:
                bad(
                    "ENGINE_PHASE_METADATA",
                    "phase without a solution cannot claim objective/bound",
                    path=path,
                )
        if names != list(phase_names[:len(names)]):
            bad(
                "ENGINE_PHASE_METADATA",
                "physical phases must form the serve/soft_due/cost prefix",
                path="search.phases",
            )
        if phases and isinstance(phases[-1], Mapping) \
            and engine_status != phases[-1].get("engine_status"):
            bad(
                "ENGINE_STATUS_INCONSISTENT",
                "engine_status must describe the last physical phase attempt",
                path="search.engine_status",
            )
        if not phases and engine_status != "NOT_RUN":
            bad(
                "ENGINE_STATUS_INCONSISTENT",
                "engine_status must be NOT_RUN when physical master did not run",
                path="search.engine_status",
            )

    witness = isinstance(status, str) and status in {"FEASIBLE", "PARTIAL"}
    incumbent = search.get("incumbent")
    if not isinstance(incumbent, Mapping):
        bad("INCUMBENT_METADATA", "incumbent metadata is required", path="search.incumbent")
        incumbent = {}
    available = incumbent.get("available")
    if type(available) is not bool:
        bad(
            "INCUMBENT_METADATA",
            "incumbent.available must be boolean",
            path="search.incumbent.available",
        )
    if witness and available is not True:
        bad(
            "INCUMBENT_REQUIRED",
            "a witness status requires a retained physical incumbent",
            path="search.incumbent.available",
        )
    if not witness and available is True:
        bad(
            "INCUMBENT_STATUS_CONFLICT",
            "a no-witness status cannot claim an available incumbent",
            path="search.incumbent.available",
        )
    origin = incumbent.get("origin")
    termination = search.get("termination")
    if not isinstance(termination, Mapping):
        bad(
            "SEARCH_TERMINATION_METADATA",
            "termination metadata is required",
            path="search.termination",
        )
        termination = {}
    reason = termination.get("reason")
    if not isinstance(reason, str) or not reason:
        bad(
            "SEARCH_TERMINATION_METADATA",
            "termination reason must be a nonempty string",
            path="search.termination.reason",
        )
    with_incumbent_reasons = {
        "TIME_LIMIT_WITH_INCUMBENT",
        "MASTER_NOT_RUN_WITH_PROPOSAL_INCUMBENT",
        "PHYSICAL_MASTER_LIMIT_WITH_INCUMBENT",
        "RESTRICTED_PHASE_SEQUENCE_COMPLETED",
    }
    without_incumbent_reasons = {
        "TIME_LIMIT_NO_INCUMBENT", "SEARCH_LIMIT_NO_INCUMBENT", "NO_WITNESS",
    }
    if isinstance(reason, str):
        if available is True and reason not in with_incumbent_reasons:
            bad(
                "SEARCH_TERMINATION_INCONSISTENT",
                "available incumbent requires a with-incumbent termination reason",
                path="search.termination.reason",
            )
        if available is False and reason not in without_incumbent_reasons:
            bad(
                "SEARCH_TERMINATION_INCONSISTENT",
                "missing incumbent requires a no-incumbent termination reason",
                path="search.termination.reason",
            )
    finished = termination.get("optimization_finished")
    if type(finished) is not bool:
        bad(
            "SEARCH_TERMINATION_METADATA",
            "optimization_finished must be boolean",
            path="search.termination.optimization_finished",
        )
    completed_phases = (
        isinstance(phases, list)
        and len(phases) == 3
        and all(isinstance(item, Mapping)
                and isinstance(item.get("engine_status"), str)
                and item.get("engine_status") in success for item in phases)
    )
    physical_master_status = search.get("physical_master_status")
    if require_ordered_domain and (
        not isinstance(physical_master_status, str)
        or physical_master_status not in {
            "NOT_RUN", "PHYSICAL_PLAN_FOUND", "NO_SELECTION", "NO_PHYSICAL_COLUMNS",
            "ENGINE_UNAVAILABLE", "ENGINE_VERSION_MISMATCH",
        }
    ):
        bad(
            "PHYSICAL_MASTER_STATUS",
            "physical_master_status is invalid",
            path="search.physical_master_status",
        )
    expected_finished = completed_phases and (
        not require_ordered_domain or physical_master_status == "PHYSICAL_PLAN_FOUND"
    )
    if type(finished) is bool and finished != expected_finished:
        bad(
            "SEARCH_TERMINATION_INCONSISTENT",
            "optimization_finished must match completed physical selection phases",
            path="search.termination.optimization_finished",
        )
    restricted_optimality = termination.get("restricted_optimality_proven")
    if type(restricted_optimality) is not bool:
        bad(
            "SEARCH_TERMINATION_METADATA",
            "restricted_optimality_proven must be boolean",
            path="search.termination.restricted_optimality_proven",
        )
    expected_restricted_optimality = (
        isinstance(phases, list)
        and len(phases) == 3
        and all(isinstance(item, Mapping)
                and item.get("engine_status") == "OPTIMAL" for item in phases)
        and origin == "PHYSICAL_MASTER"
        and (not require_ordered_domain
             or physical_master_status == "PHYSICAL_PLAN_FOUND")
    )
    if (type(restricted_optimality) is bool
        and restricted_optimality != expected_restricted_optimality):
        bad(
            "RESTRICTED_OPTIMALITY_INCONSISTENT",
            "restricted optimality is limited to three OPTIMAL physical phases",
            path="search.termination.restricted_optimality_proven",
        )
    if isinstance(reason, str) and available is True:
        if (reason == "MASTER_NOT_RUN_WITH_PROPOSAL_INCUMBENT"
            and (origin != "PROPOSAL_REALIZATION" or phases)):
            bad(
                "SEARCH_TERMINATION_INCONSISTENT",
                "master-not-run reason requires a proposal incumbent and no master phase",
                path="search.termination.reason",
            )
        if reason == "RESTRICTED_PHASE_SEQUENCE_COMPLETED" and not expected_finished:
            bad(
                "SEARCH_TERMINATION_INCONSISTENT",
                "completed reason requires completed physical master phases",
                path="search.termination.reason",
            )
        if reason == "PHYSICAL_MASTER_LIMIT_WITH_INCUMBENT" and expected_finished:
            bad(
                "SEARCH_TERMINATION_INCONSISTENT",
                "master-limit reason conflicts with a completed physical selection",
                path="search.termination.reason",
            )
    if not witness or available is not True:
        return

    selection_stage = incumbent.get("selection_stage")
    if (not isinstance(origin, str)
        or origin not in {"PROPOSAL_REALIZATION", "PHYSICAL_MASTER"}):
        bad("INCUMBENT_METADATA", "incumbent origin is invalid", path="search.incumbent.origin")
    if origin == "PROPOSAL_REALIZATION":
        if selection_stage != "proposal_realization":
            bad(
                "INCUMBENT_METADATA",
                "proposal incumbent must identify proposal_realization stage",
                path="search.incumbent.selection_stage",
            )
    elif origin == "PHYSICAL_MASTER":
        if (not isinstance(selection_stage, str)
            or selection_stage not in phase_names
            or phase_status.get(selection_stage) not in success):
            bad(
                "INCUMBENT_PHASE_BINDING",
                "master incumbent must bind to a successful attempted phase",
                path="search.incumbent.selection_stage",
            )
    routes = result.get("vehicle_routes")
    route_ids = [route.get("route_column_id") for route in routes] \
        if isinstance(routes, list) and all(isinstance(route, Mapping) for route in routes) else []
    selected_ids = incumbent.get("selected_route_column_ids")
    selected_indexes = incumbent.get("selected_route_indexes")
    domain_count = incumbent.get("domain_route_column_count")
    selected_ids_valid = (
        isinstance(selected_ids, list)
        and all(isinstance(item, str) and item for item in selected_ids)
        and len(selected_ids) == len(set(selected_ids))
    )
    if not selected_ids_valid or selected_ids != route_ids:
        bad(
            "INCUMBENT_DOMAIN_BINDING",
            "incumbent column IDs must match the published routes",
            path="search.incumbent.selected_route_column_ids",
        )
    selected_indexes_valid = (
        isinstance(selected_indexes, list)
        and all(type(item) is int and item >= 0 for item in selected_indexes)
        and len(selected_indexes) == len(set(selected_indexes))
        and len(selected_indexes) == len(route_ids)
    )
    if not selected_indexes_valid:
        bad(
            "INCUMBENT_DOMAIN_BINDING",
            "selected route indexes must be unique nonnegative integers",
            path="search.incumbent.selected_route_indexes",
        )
    domain_count_valid = (
        type(domain_count) is int
        and domain_count >= len(route_ids)
        and (not selected_indexes_valid
             or all(item < domain_count for item in selected_indexes))
    )
    if not domain_count_valid:
        bad(
            "INCUMBENT_DOMAIN_BINDING",
            "incumbent domain count does not cover selected indexes",
            path="search.incumbent.domain_route_column_count",
        )
    path_pool = result.get("path_pool")
    expected_hash = path_pool.get("content_sha256") if isinstance(path_pool, Mapping) else None
    if incumbent.get("model_input_sha256") != expected_hash:
        bad(
            "INCUMBENT_SOURCE_BINDING",
            "incumbent must bind to the authenticated model input",
            path="search.incumbent.model_input_sha256",
        )
    trace = search.get("refinement_trace")
    ordered_trace_ids: list[str] = []
    trace_seen: set[str] = set()
    if isinstance(trace, list):
        for item in trace:
            columns = item.get("route_columns") if isinstance(item, Mapping) else None
            if isinstance(columns, list):
                for column in columns:
                    if isinstance(column, str) and column not in trace_seen:
                        trace_seen.add(column)
                        ordered_trace_ids.append(column)
    else:
        bad(
            "INCUMBENT_DOMAIN_BINDING",
            "refinement trace must be an array",
            path="search.refinement_trace",
        )
    claimed_domain = incumbent.get("domain_route_column_ids")
    if require_ordered_domain:
        claimed_domain_valid = (
            isinstance(claimed_domain, list)
            and all(isinstance(item, str) and item for item in claimed_domain)
            and len(claimed_domain) == len(set(claimed_domain))
            and type(domain_count) is int
            and len(claimed_domain) == domain_count
        )
        if not claimed_domain_valid:
            bad(
                "INCUMBENT_DOMAIN_BINDING",
                "ordered incumbent domain must contain unique nonempty column IDs",
                path="search.incumbent.domain_route_column_ids",
            )
            ordered_domain: list[str] = []
        else:
            ordered_domain = list(claimed_domain)
            if ordered_domain != ordered_trace_ids[:domain_count]:
                bad(
                    "INCUMBENT_DOMAIN_BINDING",
                    "ordered incumbent domain must equal the first-seen trace prefix",
                    path="search.incumbent.domain_route_column_ids",
                )
    else:
        ordered_domain = (
            ordered_trace_ids[:domain_count] if type(domain_count) is int else []
        )
    if type(domain_count) is int and len(ordered_trace_ids) < domain_count:
        bad(
            "INCUMBENT_DOMAIN_BINDING",
            "refinement trace does not cover the incumbent domain prefix",
            path="search.incumbent.domain_route_column_count",
        )
    if selected_ids_valid and any(item not in trace_seen for item in selected_ids):
        bad(
            "INCUMBENT_DOMAIN_BINDING",
            "selected route column is absent from refinement trace",
            path="search.incumbent.selected_route_column_ids",
        )
    if selected_ids_valid and selected_indexes_valid and domain_count_valid:
        mapped = [ordered_domain[index] for index in selected_indexes] \
            if len(ordered_domain) >= domain_count else []
        if mapped != selected_ids:
            bad(
                "INCUMBENT_DOMAIN_BINDING",
                "selected indexes must map to the retained column IDs in domain order",
                path="search.incumbent.selected_route_indexes",
                expected_column_ids=selected_ids,
                actual_column_ids=mapped,
            )
    score = incumbent.get("score")
    if not isinstance(score, Mapping):
        bad("INCUMBENT_SCORE", "incumbent score is required", path="search.incumbent.score")
    elif isinstance(routes, list):
        raw_lateness = [route.get("total_soft_lateness_s")
                        for route in routes if isinstance(route, Mapping)]
        raw_cost = [route.get("total_cost_vnd")
                    for route in routes if isinstance(route, Mapping)]
        numeric_score = (
            len(raw_lateness) == len(routes)
            and len(raw_cost) == len(routes)
            and all(type(value) in (int, float) and math.isfinite(float(value))
                    and value >= 0 for value in raw_lateness + raw_cost)
        )
        if not numeric_score:
            bad(
                "INCUMBENT_SCORE",
                "physical route score inputs must be finite nonnegative numbers",
                path="vehicle_routes",
            )
            return
        expected_score = {
            "served_count": len(result.get("served_orders", ())),
            "soft_lateness_scaled": sum(
                math.ceil(float(value) * 1000 - 1e-12) for value in raw_lateness
            ),
            "cost_scaled": sum(
                math.ceil(float(value) - 1e-12) for value in raw_cost
            ),
        }
        if dict(score) != expected_score:
            bad(
                "INCUMBENT_SCORE",
                "incumbent score differs from physical routes",
                path="search.incumbent.score",
                expected=expected_score,
            )
        if origin == "PHYSICAL_MASTER" and isinstance(selection_stage, str) \
            and selection_stage in phase_records:
            phase_index, phase_record = phase_records[selection_stage]
            objective_key = {
                "serve": "served_count",
                "soft_due": "soft_lateness_scaled",
                "cost": "cost_scaled",
            }[selection_stage]
            objective_value = phase_record.get("objective_value")
            if (type(objective_value) not in (int, float)
                or not math.isfinite(float(objective_value))
                or not math.isclose(
                    float(objective_value), float(expected_score[objective_key]),
                    rel_tol=0.0, abs_tol=1e-9,
                )):
                bad(
                    "INCUMBENT_PHASE_OBJECTIVE_BINDING",
                    "retained master stage objective differs from its physical score",
                    path=f"search.phases[{phase_index}].objective_value",
                    expected=expected_score[objective_key],
                    actual=objective_value,
                )
        restricted = search.get("restricted_objective")
        if not isinstance(restricted, Mapping) or dict(restricted) != expected_score:
            bad(
                "INCUMBENT_SCORE",
                "restricted objective must equal the retained physical score",
                path="search.restricted_objective",
                expected=expected_score,
            )


__all__ = ["VALIDATOR_VERSION", "validate_ortools_static_solution"]
