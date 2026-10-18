"""Receipt-gated M1 Step-3 profile domain and solution projection.

The physical domain is built once per scenario.  FASTEST, BALANCED and SAFER
only select columns from that same immutable domain.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
from itertools import permutations
import json
import math
from pathlib import Path
from time import monotonic
from typing import Any, Mapping

from optimization.models.decision_state import DecisionState
from optimization.solver.member1_profile_master import (
    PROFILE_MASTER_VERSION, scaled_profile_score, solve_profile_master,
)

from .member1_s0_graph import Member1RoadGraph, RoadDataError

DOMAIN_VERSION = "task02-m1-shared-physical-profile-domain/3"
SOLUTION_VERSION = "task02-m1-profile-solution/3"
SOLVER_VERSION = "task02-m1-ortools-profile-route-column/3"
PROFILE_ORDER = ("FASTEST", "BALANCED", "SAFER")


class ProposalInfeasible(ValueError):
    """A bounded proposal cannot be realized; the raw source is still valid."""

    def __init__(self, message: str, *, traces: list[dict[str, Any]] | None = None,
                 stop_reason: str = "NO_FULL_BOUNDED_WITNESS") -> None:
        super().__init__(message)
        self.traces = list(traces or ())
        self.stop_reason = stop_reason

_LOCKED_POLICY = {
    "schema_version": "task02-m1-profile-config/1",
    "policy_version": "task02-m1-profile-policy/1",
    "normalization_version": "task02-m1-common-benchmark-normalization/1",
    "score_scale": 1_000_000,
    "rounding": "ROUND_HALF_UP",
    "production_calibrated": False,
    "references": {"travel_time_s": 7911.17435157269,
                   "distance_m": 49796.995379420696,
                   "relative_exposure_proxy": 16.2080775839896},
    "profiles": {
        "FASTEST": {"time": 1.0, "risk": 0.0, "distance": 0.0,
                    "tie_break": ["distance", "risk"]},
        "BALANCED": {"time": 0.6, "risk": 0.4, "distance": 0.0,
                     "tie_break": ["time", "risk", "distance"]},
        "SAFER": {"time": 0.0, "risk": 1.0, "distance": 0.0,
                  "tie_break": ["time", "distance"]},
    },
    "trusted_s1": {
        "run_id": "S1_ORTOOLS_STATIC_20261001T135318224322Z",
        "manifest_sha256": "6e614a95d75a96554c591ce6e2a4931d6bbf70efea579297b50a298f5f5fc3ef",
        "solution_sha256": "18d89440fd96e7fbf4f8a5dc07ed1c55052e29b77ee34e68cf77ab99edce1d8c",
    },
    "domain_limits": {"domain_seconds": 600.0, "per_query_seconds": 15.0,
                      "max_proposals": 96, "max_columns": 512,
                      "max_road_states": 250000, "max_road_labels": 500000,
                      "max_arrival_options": 8},
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_profile_config(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_bytes())
    if not isinstance(value, Mapping):
        raise ValueError("profile config root must be an object")
    for key, expected in _LOCKED_POLICY.items():
        if value.get(key) != expected:
            raise ValueError(f"locked profile config mismatch at {key}")
    limits = value.get("limits")
    if not isinstance(limits, Mapping):
        raise ValueError("profile limits must be an object")
    expected_limits = {"master_seconds": 60.0, "num_search_workers": 1,
                       "random_seed": 0}
    if any(limits.get(key) != expected for key, expected in expected_limits.items()):
        raise ValueError("locked profile limits mismatch")
    for key, reference in value["references"].items():
        if type(reference) not in (int, float) or not math.isfinite(reference) or reference <= 0:
            raise ValueError(f"normalization reference is invalid: {key}")
    return value


def _seconds(value: str, epoch: datetime) -> float:
    return (datetime.fromisoformat(value) - epoch).total_seconds()


def _reauthenticate_route(route: Mapping[str, Any], graph: Member1RoadGraph) -> dict[str, Any]:
    result = deepcopy(dict(route))
    previous = None
    cursor = result["start_node"]
    fresh = []
    for index, leg in enumerate(result["legs"]):
        path = graph.path_from_edge_ids(cursor, tuple(leg["edge_ids"]), incoming_edge=previous)
        if path.candidate.nodes[-1] != leg["to_node"]:
            raise RoadDataError(f"route leg {index} endpoint mismatch")
        fresh.append(path.to_leg(previous))
        previous, cursor = path.final_edge, path.candidate.nodes[-1]
    result["legs"] = fresh
    result["total_distance_m"] = sum(item["distance_m"] for item in fresh)
    result["total_travel_time_s"] = sum(item["travel_time_s"] for item in fresh)
    result["total_exposure"] = sum(item["exposure"] for item in fresh)
    return result


def _single_order_column(state: DecisionState, graph: Member1RoadGraph,
                         vehicle: Any, order: Any, outbound_edges: list[str], *,
                         label: str, deadline: float,
                         return_objective: str = "time",
                         return_option_rank: int = 0) -> tuple[dict[str, Any], dict[str, Any]]:
    depot = state.depot.graph_node_id
    outbound = graph.path_from_edge_ids(depot, tuple(outbound_edges), incoming_edge=None)
    if outbound.candidate.nodes[-1] != order.graph_node_id:
        raise ProposalInfeasible(f"{label} outbound endpoint mismatch")
    epoch = datetime.fromisoformat(state.decision_epoch)
    departure = max(0.0, _seconds(state.depot.opening_time, epoch),
                    _seconds(vehicle.working_start, epoch))
    remaining_distance = vehicle.remaining_range_m - outbound.candidate.distance
    arrival = departure + outbound.candidate.travel_time
    service_start = max(arrival, _seconds(order.earliest, epoch))
    completion = service_start + order.service_time_seconds
    delivery_cutoff = min(_seconds(order.hard_deadline, epoch),
                          _seconds(vehicle.working_end, epoch))
    if completion > delivery_cutoff + 1e-9:
        raise ProposalInfeasible(f"{label} completes after customer hard deadline")
    return_cutoff = min(_seconds(vehicle.working_end, epoch),
                        _seconds(state.depot.closing_time, epoch))
    common = dict(incoming_edge=outbound.final_edge, max_road_states=250000,
                  max_road_labels=500000, max_arrival_options=1,
                  max_distance_m=remaining_distance,
                  max_travel_time_s=max(0.0, return_cutoff - completion),
                  deadline=min(deadline, monotonic() + 15.0))
    result = (graph.find_paths(order.graph_node_id, depot, **common)
              if return_objective == "time"
              else graph.find_objective_paths(order.graph_node_id, depot,
                                               objective=return_objective, **common))
    trace = {"objective": return_objective, "leg_index": 1,
             "origin": order.graph_node_id, "destination": depot,
             "incoming_edge": outbound.final_edge,
             "option_count": len(result.paths), "limit": result.limit,
             "settled_states": result.settled_states,
             "generated_labels": result.generated_labels,
             "dominated_prunes": result.dominated_prunes,
             "resource_prunes": result.resource_prunes}
    if not result.paths:
        raise ProposalInfeasible(
            f"{label} has no authenticated depot-return witness ({result.limit})",
            traces=[trace], stop_reason="BOUNDED_QUERY_LIMIT"
            if result.limit in {"SEARCH_LIMIT", "TIME_LIMIT", "STATE_LIMIT",
                                "LABEL_LIMIT", "OPTION_LIMIT"}
            else "NO_AUTHENTICATED_RETURN_WITNESS")
    back = result.paths[min(return_option_rank, len(result.paths) - 1)]
    return_s = completion + back.candidate.travel_time
    distance = outbound.candidate.distance + back.candidate.distance
    identity = hashlib.sha256(_canonical({"vehicle": vehicle.vehicle_id, "label": label,
                                         "out": outbound.edge_ids, "back": back.edge_ids})).hexdigest()[:20]
    route = {
        "route_column_id": f"profile-{identity}", "vehicle_id": vehicle.vehicle_id,
        "start_node": depot, "end_node": depot,
        "node_sequence": [depot, order.graph_node_id, depot],
        "order_sequence": [order.order_id], "departure_s": departure,
        "return_s": return_s, "pickup_service_s": 0.0,
        "load_before_pickup_kg": vehicle.current_load_kg,
        "load_after_depot_pickup_kg": vehicle.current_load_kg + order.demand_kg,
        "return_load_kg": vehicle.current_load_kg,
        "legs": [outbound.to_leg(None), back.to_leg(outbound.final_edge)],
        "stops": [{"order_id": order.order_id, "node_id": order.graph_node_id,
                   "arrival_s": arrival, "service_start_s": service_start,
                   "completion_s": completion, "waiting_s": service_start - arrival,
                   "load_after_delivery_kg": vehicle.current_load_kg,
                   "soft_lateness_s": max(0.0, completion - _seconds(order.preferred_due, epoch))}],
        "total_distance_m": distance,
        "total_travel_time_s": outbound.candidate.travel_time + back.candidate.travel_time,
        "total_exposure": outbound.candidate.risk_score + back.candidate.risk_score,
        "total_elapsed_time_s": return_s - departure,
        "total_soft_lateness_s": max(0.0, completion - _seconds(order.preferred_due, epoch)),
        "total_cost_vnd": distance / 1000.0 * vehicle.cost_per_km_vnd,
        "profile_domain_source": label,
    }
    return route, trace


def _sequence_columns(state: DecisionState, graph: Member1RoadGraph,
                      vehicle: Any, order_ids: list[str], *, objective: str,
                      label: str, deadline: float, max_arrival_options: int = 8,
                      max_columns: int = 8) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Boundedly realize a stop sequence while preserving arrival-edge state.

    A path that is locally best can make the next turn impossible.  This DFS
    therefore retains up to ``max_arrival_options`` raw alternatives per leg
    and backtracks across later legs, return, time and range constraints.
    ``RoadDataError`` is deliberately not swallowed: it denotes invalid raw
    source/input.  An exhausted but valid bounded proposal is reported with
    :class:`ProposalInfeasible`.
    """

    orders = {item.order_id: item for item in state.orders}
    if not order_ids or any(item not in orders for item in order_ids):
        raise ProposalInfeasible("route proposal contains unknown orders")
    assigned = [orders[item] for item in order_ids]
    loaded = vehicle.current_load_kg + sum(item.demand_kg for item in assigned)
    if loaded > vehicle.capacity_kg + 1e-9:
        raise ProposalInfeasible("route proposal exceeds vehicle capacity")
    epoch = datetime.fromisoformat(state.decision_epoch)
    depot = state.depot.graph_node_id
    departure = max(0.0, _seconds(state.depot.opening_time, epoch),
                    _seconds(vehicle.working_start, epoch))
    close = min(_seconds(state.depot.closing_time, epoch),
                _seconds(vehicle.working_end, epoch))
    traces: list[dict[str, Any]] = []
    realized: list[dict[str, Any]] = []
    global_deadline_reached = False

    def visit(position: int, cursor: int, incoming: str | None, clock: float,
              load: float, distance: float, travel: float, exposure: float,
              lateness: float, legs: list[dict[str, Any]],
              stops: list[dict[str, Any]], nodes: list[int]) -> None:
        nonlocal global_deadline_reached
        if len(realized) >= max_columns:
            return
        if monotonic() >= deadline:
            global_deadline_reached = True
            return
        order = assigned[position] if position < len(assigned) else None
        target = depot if order is None else order.graph_node_id
        time_budget = close - clock
        if order is not None:
            time_budget = min(time_budget, _seconds(order.hard_deadline, epoch)
                              - order.service_time_seconds - clock)
        if time_budget < -1e-9 or vehicle.remaining_range_m - distance < -1e-6:
            return
        common = dict(incoming_edge=incoming, max_road_states=250000,
                      max_road_labels=500000,
                      max_arrival_options=max(1, max_arrival_options),
                      max_distance_m=max(0.0, vehicle.remaining_range_m - distance),
                      max_travel_time_s=max(0.0, time_budget),
                      deadline=min(deadline, monotonic() + 15.0))
        search = (graph.find_paths(cursor, target, **common)
                  if objective == "time"
                  else graph.find_objective_paths(cursor, target,
                                                   objective=objective, **common))
        traces.append({"objective": objective, "leg_index": position,
                       "origin": cursor, "destination": target,
                       "incoming_edge": incoming, "option_count": len(search.paths),
                       "limit": search.limit, "settled_states": search.settled_states,
                       "generated_labels": search.generated_labels,
                       "dominated_prunes": search.dominated_prunes,
                       "resource_prunes": search.resource_prunes})
        for path in search.paths:
            new_clock = clock + path.candidate.travel_time
            new_distance = distance + path.candidate.distance
            if new_distance > vehicle.remaining_range_m + 1e-6:
                continue
            leg = path.to_leg(incoming)
            new_legs = legs + [leg]
            new_nodes = nodes + [target]
            new_travel = travel + path.candidate.travel_time
            new_exposure = exposure + path.candidate.risk_score
            if order is None:
                if new_clock <= close + 1e-9:
                    identity = hashlib.sha256(_canonical({"vehicle": vehicle.vehicle_id,
                        "orders": order_ids,
                        "edges": [item["edge_ids"] for item in new_legs]})).hexdigest()[:20]
                    realized.append({
                        "route_column_id": f"profile-{identity}",
                        "vehicle_id": vehicle.vehicle_id,
                        "start_node": depot, "end_node": depot,
                        "node_sequence": new_nodes,
                        "order_sequence": list(order_ids), "departure_s": departure,
                        "return_s": new_clock, "pickup_service_s": 0.0,
                        "load_before_pickup_kg": vehicle.current_load_kg,
                        "load_after_depot_pickup_kg": loaded, "return_load_kg": load,
                        "legs": new_legs, "stops": stops,
                        "total_distance_m": new_distance,
                        "total_travel_time_s": new_travel,
                        "total_exposure": new_exposure,
                        "total_elapsed_time_s": new_clock - departure,
                        "total_soft_lateness_s": lateness,
                        "total_cost_vnd": new_distance / 1000.0 * vehicle.cost_per_km_vnd,
                        "profile_domain_source": label,
                    })
                continue
            arrival = new_clock
            start = max(arrival, _seconds(order.earliest, epoch))
            completion = start + order.service_time_seconds
            if completion > min(_seconds(order.hard_deadline, epoch),
                                _seconds(vehicle.working_end, epoch)) + 1e-9:
                continue
            late = max(0.0, completion - _seconds(order.preferred_due, epoch))
            next_load = load - order.demand_kg
            visit(position + 1, target, path.final_edge, completion, next_load,
                  new_distance, new_travel, new_exposure, lateness + late,
                  new_legs, stops + [{"order_id": order.order_id, "node_id": target,
                      "arrival_s": arrival, "service_start_s": start,
                      "completion_s": completion, "waiting_s": start - arrival,
                      "load_after_delivery_kg": next_load, "soft_lateness_s": late}],
                  new_nodes)

    visit(0, depot, None, departure, loaded, 0.0, 0.0, 0.0, 0.0, [], [], [depot])
    if not realized:
        if not traces and global_deadline_reached:
            stop_reason = "NOT_STARTED_GLOBAL_DEADLINE"
        elif global_deadline_reached:
            stop_reason = "GLOBAL_DEADLINE_AFTER_QUERY"
        elif any(item.get("limit") in {"SEARCH_LIMIT", "TIME_LIMIT", "STATE_LIMIT",
                                        "LABEL_LIMIT", "OPTION_LIMIT"}
                 for item in traces):
            stop_reason = "BOUNDED_QUERY_LIMIT"
        else:
            stop_reason = "NO_FULL_BOUNDED_WITNESS"
        raise ProposalInfeasible(f"{label} has no full bounded witness",
                                 traces=traces, stop_reason=stop_reason)
    realized.sort(key=lambda item: (item["total_travel_time_s"],
                                    item["total_distance_m"], item["total_exposure"],
                                    [leg["edge_ids"] for leg in item["legs"]]))
    return realized, traces


def _sequence_column(state: DecisionState, graph: Member1RoadGraph,
                     vehicle: Any, order_ids: list[str], *, objective: str,
                     option_rank: int, label: str, deadline: float
                     ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    columns, traces = _sequence_columns(
        state, graph, vehicle, order_ids, objective=objective, label=label,
        deadline=deadline, max_arrival_options=8, max_columns=max(8, option_rank + 1))
    return columns[min(option_rank, len(columns) - 1)], traces


def _physical_key(route: Mapping[str, Any]) -> tuple[Any, ...]:
    return (route.get("vehicle_id"), tuple(route.get("order_sequence", ())),
            tuple(tuple(leg.get("edge_ids", ())) for leg in route.get("legs", ())))


def _bounded_sequences(seed: list[str], max_proposals: int) -> list[list[str]]:
    """Deterministic sequence neighbourhood; exhaustive only for tiny routes."""
    values: list[tuple[str, ...]] = []
    if len(seed) <= 7:
        values.extend(permutations(seed))
    else:
        values.extend((tuple(seed), tuple(reversed(seed))))
        for index in range(len(seed) - 1):
            altered = list(seed)
            altered[index], altered[index + 1] = altered[index + 1], altered[index]
            values.append(tuple(altered))
    unique: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(list(value))
        if len(unique) >= max_proposals:
            break
    return unique


def build_shared_profile_domain(state: DecisionState, graph: Member1RoadGraph, *,
                                snapshot_root: str | Path, repo_root: str | Path,
                                config: Mapping[str, Any], deadline: float) -> dict[str, Any]:
    if state.scenario_id not in {"S1", "S7", "S8"} or state.pending_events:
        raise ValueError("profile Step-3 supports static S1/S7/S8 only")
    columns, telemetry = [], []
    domain_limits = config.get("domain_limits", _LOCKED_POLICY["domain_limits"])
    sources: dict[str, Any]
    if state.scenario_id == "S1":
        trusted = config["trusted_s1"]
        run = Path(repo_root) / "outputs" / "member1_ortools_static_validator_closure" / trusted["run_id"]
        manifest_path, solution_path = run / "manifest.json", run / "solution.json"
        if _sha(manifest_path) != trusted["manifest_sha256"] or _sha(solution_path) != trusted["solution_sha256"]:
            raise ValueError("trusted S1 witness pin mismatch")
        manifest = json.loads(manifest_path.read_bytes())
        if manifest.get("gate") != "S1_ORTOOLS_VALIDATED_FULL_8_OF_8" or manifest.get("validation_valid") is not True:
            raise ValueError("trusted S1 witness gate mismatch")
        solution = json.loads(solution_path.read_bytes())
        seeds = [_reauthenticate_route(item, graph) for item in solution["vehicle_routes"]]
        columns.extend(seeds)
        seed_ids = [item["route_column_id"] for item in seeds]
        vehicles = {item.vehicle_id: item for item in state.vehicles}
        for seed in seeds:
            vehicle = vehicles[seed["vehicle_id"]]
            for proposal_index, sequence in enumerate(_bounded_sequences(
                    list(seed["order_sequence"]), domain_limits["max_proposals"])):
                for objective in ("time", "distance", "exposure"):
                    label = f"S1_{objective.upper()}_PROPOSAL_{proposal_index}"
                    try:
                        routes, traces = _sequence_columns(
                            state, graph, vehicle, sequence, objective=objective,
                            label=label, deadline=deadline,
                            max_arrival_options=domain_limits["max_arrival_options"])
                        columns.extend(routes)
                        telemetry.append({"vehicle_id": vehicle.vehicle_id,
                                          "label": label, "status": "CANDIDATE",
                                          "candidate_count": len(routes), "legs": traces})
                    except ProposalInfeasible as error:
                        telemetry.append({"vehicle_id": vehicle.vehicle_id,
                                          "label": label,
                                          "status": "NO_CANDIDATE_WITHIN_LIMITS",
                                          "diagnostic": str(error),
                                          "stop_reason": error.stop_reason,
                                          "query_started": bool(error.traces),
                                          "legs": error.traces})
        sources = {"kind": "PINNED_VALIDATED_S1_COLUMNS", "run_id": trusted["run_id"],
                   "manifest_sha256": trusted["manifest_sha256"],
                   "solution_sha256": trusted["solution_sha256"],
                   "seed_route_column_ids": seed_ids}
    else:
        fixture_path = Path(snapshot_root) / "scenarios" / "fixtures" / "thu-duc-binh-thanh-v1" / f"{state.scenario_id}.json"
        if _sha(fixture_path) != state.fixture_raw_sha256:
            raise ValueError("profile fixture raw hash mismatch")
        fixture = json.loads(fixture_path.read_bytes())
        evidence = fixture.get("tradeoffEvidence")
        if not isinstance(evidence, Mapping):
            raise ValueError("tradeoffEvidence is required")
        order = state.orders[0]
        seed_ids = []
        for vehicle in state.vehicles:
            for label, key in (("FASTEST_EVIDENCE", "fastest"), ("SAFER_EVIDENCE", "safer")):
                for objective in ("time", "distance", "exposure"):
                    try:
                        route, trace = _single_order_column(
                            state, graph, vehicle, order, list(evidence[key]["edgeIds"]),
                            label=f"{label}_{objective.upper()}_RETURN", deadline=deadline,
                            return_objective=objective)
                        columns.append(route)
                        seed_ids.append(route["route_column_id"])
                        telemetry.append({"vehicle_id": vehicle.vehicle_id,
                                          "label": label, "status": "CANDIDATE", **trace})
                    except ProposalInfeasible as error:
                        message = str(error)
                        telemetry.append({"vehicle_id": vehicle.vehicle_id,
                                          "label": label, "objective": objective,
                                          "status": "NO_CANDIDATE_WITHIN_LIMITS",
                                          "diagnostic": message,
                                          "stop_reason": error.stop_reason,
                                          "query_started": bool(error.traces),
                                          "legs": error.traces})
            for objective in ("time", "distance", "exposure"):
                label = f"{state.scenario_id}_{objective.upper()}_FRESH_PHYSICAL"
                try:
                    routes, traces = _sequence_columns(
                        state, graph, vehicle, [order.order_id], objective=objective,
                        label=label, deadline=deadline,
                        max_arrival_options=domain_limits["max_arrival_options"])
                    columns.extend(routes)
                    telemetry.append({"vehicle_id": vehicle.vehicle_id, "label": label,
                                      "status": "CANDIDATE",
                                      "candidate_count": len(routes), "legs": traces})
                except ProposalInfeasible as error:
                    telemetry.append({"vehicle_id": vehicle.vehicle_id, "label": label,
                                      "status": "NO_CANDIDATE_WITHIN_LIMITS",
                                      "diagnostic": str(error),
                                      "stop_reason": error.stop_reason,
                                      "query_started": bool(error.traces),
                                      "legs": error.traces})
        sources = {"kind": "PINNED_M1_TRADEOFF_EVIDENCE_PLUS_RAW_RETURN",
                   "fixture_raw_sha256": state.fixture_raw_sha256,
                   "tradeoff_verified_by_m1": fixture.get("validation", {}).get("tradeoffVerified") is True,
                   "seed_route_column_ids": sorted(set(seed_ids))}
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    origins: dict[tuple[Any, ...], list[str]] = {}
    for route in columns:
        key = _physical_key(route)
        origins.setdefault(key, []).append(str(route.get("profile_domain_source")))
        unique.setdefault(key, route)
    columns = []
    for key, route in unique.items():
        item = deepcopy(route)
        item["profile_domain_origins"] = sorted(set(origins[key]))
        columns.append(item)
    columns.sort(key=lambda item: (item["vehicle_id"], item["order_sequence"],
                                   [leg["edge_ids"] for leg in item["legs"]]))
    if len(columns) > 512:
        raise ValueError("profile domain exceeds locked 512-column limit")
    # Stable identity is over physical content and bounded-search telemetry,
    # never a wall-clock timestamp.
    body = {"schema_version": DOMAIN_VERSION, "scenario_id": state.scenario_id,
            "state_fixture_sha256": state.fixture_raw_sha256,
            "state_identity": {"schema_version": state.schema_version,
                               "state_version": state.state_version,
                               "decision_epoch": state.decision_epoch,
                               "context_version": state.context_version},
            "source_hashes": dict(graph.source_hashes), "sources": sources,
            "columns": columns, "generation_telemetry": telemetry,
            "scope": {"finite": True, "shared_by_profiles": list(PROFILE_ORDER),
                      "includes_full_depot_return": True, "global_path_completeness": False,
                      "search_objectives": ["time", "distance", "exposure"],
                      "max_road_states": 250000, "max_road_labels": 500000,
                      "max_arrival_options": 8, "max_columns": 512}}
    body["content_sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    return body


def _domain_road_metrics(domain: Mapping[str, Any]) -> dict[str, int | None]:
    traces: list[Mapping[str, Any]] = []
    for record in domain.get("generation_telemetry", ()):
        if not isinstance(record, Mapping):
            continue
        legs = record.get("legs")
        if isinstance(legs, list):
            traces.extend(item for item in legs if isinstance(item, Mapping))
        elif "settled_states" in record:
            traces.append(record)
    return {
        "road_queries": len(traces),
        "road_settled_states": sum(int(item.get("settled_states", 0)) for item in traces),
        "road_generated_labels": sum(int(item.get("generated_labels", 0)) for item in traces),
        "road_dominated_prunes": sum(int(item.get("dominated_prunes", 0)) for item in traces),
        "road_resource_prunes": sum(int(item.get("resource_prunes", 0)) for item in traces),
        "route_states": None,
        "road_peak_active_labels": None,
    }


def solve_profiles(state: DecisionState, domain: Mapping[str, Any],
                   config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    columns = domain["columns"]
    output = {}
    road_metrics = _domain_road_metrics(domain)
    for name in PROFILE_ORDER:
        master_started = monotonic()
        native = solve_profile_master(
            [item.order_id for item in state.orders], [item.vehicle_id for item in state.vehicles],
            columns, profile_name=name, profile=config["profiles"][name],
            references=config["references"], score_scale=config["score_scale"],
            time_limit_seconds=config["limits"]["master_seconds"],
            random_seed=config["limits"]["random_seed"],
            num_search_workers=config["limits"]["num_search_workers"],
        )
        master_elapsed = monotonic() - master_started
        selected = [deepcopy(columns[index]) for index in native.get("selected_route_indexes", [])]
        served = sorted(order for route in selected for order in route["order_sequence"])
        all_orders = sorted(item.order_id for item in state.orders)
        status = "FEASIBLE" if served == all_orders else "PARTIAL" if served else "SEARCH_LIMIT"
        score = sum(scaled_profile_score(route, config["profiles"][name],
                                         config["references"], config["score_scale"])
                    for route in selected)
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
            "source_hashes": dict(domain["source_hashes"]), "receipt_sha256": state.receipt_sha256,
            "status": status, "served_orders": served,
            "unserved_orders": ([{"order_id": item, "reason": "SEARCH_INCOMPLETE"}
                                  for item in all_orders if item not in served] if served else []),
            "vehicle_routes": selected,
            "metrics": {key: sum(route[key] for route in selected) for key in
                        ("total_distance_m", "total_travel_time_s", "total_exposure",
                         "total_elapsed_time_s", "total_soft_lateness_s", "total_cost_vnd")},
            "profile": {"name": name, "policy_version": config["policy_version"],
                        "normalization_version": config["normalization_version"],
                        "weights": config["profiles"][name], "references": config["references"],
                        "score_scale": config["score_scale"], "score_scaled": score,
                        "production_calibrated": False},
            "physical_domain": {"schema_version": DOMAIN_VERSION,
                                "content_sha256": domain["content_sha256"],
                                "column_count": len(columns), "shared_by_profiles": list(PROFILE_ORDER)},
            "engine": {"name": "Google OR-Tools CP-SAT", "ortools_version": native.get("ortools_version"),
                       "master_version": PROFILE_MASTER_VERSION, "restricted_model": True},
            "search": {"engine_status": native.get("engine_status"), "phases": native.get("phases", []),
                       "selected_route_indexes": native.get("selected_route_indexes", []),
                       "selected_route_column_ids": [item["route_column_id"] for item in selected],
                       "incumbent_origin": native.get("incumbent_origin"),
                       "incumbent_vector": native.get("incumbent_vector"),
                       "termination_reason": native.get("termination_reason"),
                       "seed_combinations_checked": native.get("seed_combinations_checked", 0),
                       "seed_enumeration_truncated": native.get("seed_enumeration_truncated", False),
                       "search_complete": False, "optimality_proven": False,
                       "restricted_model_optimal": native.get("restricted_model_optimal", False),
                       "truncated": True, "truncation_reasons": ["FINITE_SHARED_PHYSICAL_DOMAIN"],
                       "limits": dict(config["limits"]), **road_metrics,
                       "counter_scope": {"road": "shared_domain_generation",
                                         "route_states": "NOT_RECORDED",
                                         "road_peak_active_labels": "NOT_RECORDED",
                                         "elapsed_seconds": "profile_master_only"},
                       "assignments_considered": native.get("seed_combinations_checked", 0),
                       "elapsed_seconds": master_elapsed},
            "diagnostics": [{"severity": "WARNING", "code": "FINITE_SHARED_PHYSICAL_DOMAIN",
                             "message": "Profile comparison is restricted to the authenticated shared columns.",
                             "context": {"path": "physical_domain"}}],
        }
        result["solution_id"] = "m1-profile-" + hashlib.sha256(_canonical({
            "scenario": state.scenario_id, "profile": name, "domain": domain["content_sha256"],
            "routes": [item["route_column_id"] for item in selected]})).hexdigest()[:20]
        output[name] = result
    return output


__all__ = ["DOMAIN_VERSION", "PROFILE_ORDER", "SOLUTION_VERSION", "SOLVER_VERSION",
           "build_shared_profile_domain", "load_profile_config", "solve_profiles"]
