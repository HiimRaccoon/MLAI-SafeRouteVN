"""Source-backed M1 S1/S5/S6 bridge to a real Google OR-Tools CP-SAT model."""

from __future__ import annotations

from datetime import datetime
import hashlib
import heapq
import json
import math
from time import monotonic
from typing import Any, Mapping

from optimization.matrix.matrix_builder import MatrixBuilder
from optimization.models.candidate_path import CandidatePath
from optimization.models.decision_state import DecisionState
from optimization.models.matrix_bundle import MatrixBundle
from optimization.solver.member1_static_cp_sat import (
    MODEL_CONFIG_VERSION,
    OBJECTIVE_POLICY_VERSION,
    SCALING_VERSION,
    M1OrToolsStaticConfig,
    solve_physical_route_master,
    solve_restricted_sequence_model,
)

from .member1_s0_graph import Member1RoadGraph, RoadDataError, RoadPath
from .member1_path_foundation import (
    CandidateFoundationLimits,
    find_time_source_witness,
)


SCHEMA_VERSION = "member1-ortools-static-solution/4"
SOLVER_VERSION = "member1-ortools-cp-sat-static-bridge/4"
MODEL_INPUT_VERSION = "member1-ortools-static-model-input/2"
SUPPORTED_SCENARIOS = frozenset({"S1", "S5", "S6"})


class ModelInputContractError(ValueError):
    """Structured fail-closed error for a cached model-input contract."""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(f"{code} at {path}: {message}")
        self.diagnostic = _diagnostic(
            code, message, severity="ERROR", path=path,
        )


def _diagnostic(code: str, message: str, *, severity: str = "WARNING",
                **context: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message,
            "context": context}


def _seconds(value: str, epoch: datetime) -> float:
    return (datetime.fromisoformat(value) - epoch).total_seconds()


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _matrix_version(state: DecisionState) -> str:
    return f"m1-{state.scenario_id.lower()}-ortools-model/2"


def _matrix_source(state: DecisionState, graph: Member1RoadGraph) -> dict[str, Any]:
    return {
        "model_input_version": MODEL_INPUT_VERSION,
        "scenario_id": state.scenario_id,
        "fixture_raw_sha256": state.fixture_raw_sha256,
        "routing_version": state.routing_version,
        "features_version": state.features_version,
        "context_version": state.context_version,
        "source_hashes": dict(graph.source_hashes),
        "matrix_role": "logical_sequence_proposal_not_turn_feasibility_proof",
    }


def _validate_state(state: DecisionState, graph: Member1RoadGraph) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def fail(code: str, path: str, message: str) -> None:
        issues.append(_diagnostic(code, message, severity="ERROR", path=path))

    if state.scenario_id not in SUPPORTED_SCENARIOS:
        fail("UNSUPPORTED", "scenario_id", "bridge supports static S1/S5/S6 only")
    if state.pending_events or state.current_plans or state.execution_updates:
        fail("UNSUPPORTED", "pending_events", "events and commitments are outside this gate")
    if (state.routing_version != graph.routing_version
        or state.features_version != graph.features_version
        or state.context_version != graph.context_version):
        fail("VERSION_MISMATCH", "source_versions",
             "DecisionState and opened SQLite versions differ")
    if not 1 <= len(state.orders) <= 8 or not 1 <= len(state.vehicles) <= 2:
        fail("UNSUPPORTED", "orders/vehicles", "bridge supports at most 8 orders and 2 vehicles")
    depot = graph.node(state.depot.graph_node_id)
    if depot is None or max(abs(depot[index] - state.depot.coordinates[index])
                            for index in range(2)) > 1e-6:
        fail("INVALID_DATA", "depot.graph_node_id", "depot differs from raw graph")
    for order in state.orders:
        point = graph.node(order.graph_node_id)
        if point is None or max(abs(point[index] - order.coordinates[index])
                                for index in range(2)) > 1e-6:
            fail("INVALID_DATA", f"orders.{order.order_id}.graph_node_id",
                 "order differs from raw graph")
        if order.status != "WAITING" or order.pickup_location_id != "DEPOT":
            fail("UNSUPPORTED", f"orders.{order.order_id}.status",
                 "only WAITING depot-pickup orders are supported")
    for vehicle in state.vehicles:
        if (vehicle.availability != "AVAILABLE" or vehicle.current_load_kg != 0
            or vehicle.onboard_order_ids or vehicle.committed_stop_id is not None
            or vehicle.current_position_node_id != state.depot.graph_node_id):
            fail("UNSUPPORTED", f"vehicles.{vehicle.vehicle_id}",
                 "vehicle must be available, empty, uncommitted and at depot")
    return issues


def build_ortools_model_input(
    state: DecisionState,
    graph: Member1RoadGraph,
    *,
    config: M1OrToolsStaticConfig | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Build authenticated pair candidates and a same-path D/T/R/G projection."""

    config = config or M1OrToolsStaticConfig()
    absolute_deadline = deadline or monotonic() + config.time_limit_seconds
    problems = _validate_state(state, graph)
    if problems:
        return {
            "schema_version": MODEL_INPUT_VERSION, "gate": "BLOCKED",
            "scenario_id": state.scenario_id, "pair_candidates": [],
            "matrix_bundle": None, "diagnostics": problems,
        }
    nodes = tuple(sorted({state.depot.graph_node_id,
                          *(order.graph_node_id for order in state.orders)}))
    candidates: list[CandidatePath] = []
    pair_candidates: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for origin in nodes:
        for destination in nodes:
            if origin == destination:
                continue
            try:
                result = find_time_source_witness(
                    graph, origin, destination, incoming_edge=None,
                    limits=CandidateFoundationLimits(
                        time_limit_seconds=config.time_limit_seconds,
                        max_states_per_search=config.max_road_states,
                        max_labels_per_search=config.max_road_labels,
                        max_labels_per_turn_state=4,
                        k_per_objective=1,
                        max_secondary_states_per_search=1,
                        max_secondary_labels_per_search=1,
                    ),
                    deadline=absolute_deadline,
                )
            except (RoadDataError, KeyError, TypeError, ValueError) as error:
                diagnostics.append(_diagnostic(
                    "PATH_SOURCE_INVALID", str(error), severity="ERROR",
                    origin=origin, destination=destination,
                ))
                continue
            if not result.paths:
                diagnostics.append(_diagnostic(
                    "PAIR_CANDIDATE_MISSING",
                    "No source-backed time candidate was found; no arc is invented.",
                    severity="ERROR", origin=origin, destination=destination,
                    search_limit=result.limit,
                ))
                continue
            path = result.paths[0]
            candidates.append(path.candidate)
            pair_candidates.append({
                "origin_node": origin, "destination_node": destination,
                "incoming_edge": None, "selected_path_id": path.candidate.path_id,
                "final_edge": path.final_edge, "edge_ids": list(path.edge_ids),
                "candidate_path": path.candidate.to_dict(),
                "feature_provenance": list(path.feature_provenance),
                "search": {
                    "settled_states": result.settled_states,
                    "generated_labels": result.generated_labels,
                    "dominated_prunes": result.dominated_prunes,
                    "cycle_prunes": result.cycle_prunes,
                    "limit": result.limit,
                },
            })
            if result.limit:
                diagnostics.append(_diagnostic(
                    "PAIR_PATH_DOMAIN_TRUNCATED",
                    "Pair candidate is a bounded witness, not complete path coverage.",
                    origin=origin, destination=destination, limit=result.limit,
                ))
    matrix = None
    if not any(item["severity"] == "ERROR" for item in diagnostics):
        builder = MatrixBuilder()
        matrix = builder.build(
            candidates,
            matrix_version=_matrix_version(state),
            node_order=nodes,
            source=_matrix_source(state, graph),
        )
        diagnostics.extend(item.to_dict() for item in builder.diagnostics)
    if matrix is None:
        diagnostics.append(_diagnostic(
            "MODEL_MATRIX_BLOCKED", "D/T/R/G projection could not be built.",
            severity="ERROR",
        ))
    payload = {
        "schema_version": MODEL_INPUT_VERSION,
        "gate": "READY_WITH_FINITE_PATH_DOMAIN" if matrix is not None else "BLOCKED",
        "scenario_id": state.scenario_id,
        "decision_epoch": state.decision_epoch,
        "source": {
            "authentication": state.source_authentication,
            "fixture_raw_sha256": state.fixture_raw_sha256,
            "receipt_sha256": state.receipt_sha256,
            "routing_version": state.routing_version,
            "features_version": state.features_version,
            "context_version": state.context_version,
            "delivery_area_version": state.delivery_area_version,
            "source_hashes": dict(graph.source_hashes),
        },
        "units": {
            "distance": "meter", "travel_time": "second",
            "demand": "kg", "cost_rate": "VND/km", "cost": "VND",
            "risk": "raw_relative_exposure_proxy",
            "geometry": "WGS84_longitude_latitude",
        },
        "config": config.to_dict(),
        "node_order": list(nodes),
        "pair_count": len(pair_candidates),
        "required_pair_count": len(nodes) * (len(nodes) - 1),
        "pair_candidates": pair_candidates,
        "matrix_bundle": matrix.to_dict() if matrix is not None else None,
        "diagnostics": diagnostics,
        "scope": (
            "finite time-candidate proposal projection; physical incoming-edge "
            "route columns are refined and selected by CP-SAT"
        ),
    }
    payload["content_sha256"] = _canonical_hash(payload)
    return payload


def validate_cached_model_input(
    payload: Mapping[str, Any], state: DecisionState, graph: Member1RoadGraph,
    *, config: M1OrToolsStaticConfig,
) -> dict[str, Any]:
    """Authenticate a prior same-scenario pool against raw SQLite and state."""

    def reject(code: str, path: str, message: str) -> None:
        raise ModelInputContractError(code, path, message)

    if not isinstance(payload, Mapping):
        reject("MODEL_INPUT_SCHEMA", "$", "cached model input must be an object")
    state_issues = _validate_state(state, graph)
    if state_issues:
        first = state_issues[0]
        reject(first["code"], first.get("context", {}).get("path", "state"),
               first["message"])
    document = dict(payload)
    claimed = document.pop("content_sha256", None)
    if not isinstance(claimed, str) or _canonical_hash(document) != claimed:
        reject("MODEL_INPUT_HASH_MISMATCH", "content_sha256",
               "cached model-input content hash mismatch")
    source = payload.get("source")
    expected_source = {
        "authentication": state.source_authentication,
        "fixture_raw_sha256": state.fixture_raw_sha256,
        "receipt_sha256": state.receipt_sha256,
        "routing_version": state.routing_version,
        "features_version": state.features_version,
        "context_version": state.context_version,
        "delivery_area_version": state.delivery_area_version,
        "source_hashes": dict(graph.source_hashes),
    }
    cached_config = payload.get("config")
    expected_config = config.to_dict()
    config_compatible = isinstance(cached_config, Mapping)
    if config_compatible:
        cached_comparable = dict(cached_config)
        expected_comparable = dict(expected_config)
        cached_version = cached_comparable.pop("version", None)
        expected_comparable.pop("version", None)
        # The /3 reserve controls orchestration after the path pool exists; it
        # does not change authenticated pair candidates or D/T/R/G.
        expected_comparable.pop("master_reserve_fraction", None)
        cached_comparable.pop("master_reserve_fraction", None)
        config_compatible = (
            cached_version in {
                "member1-ortools-static-model-config/2",
                MODEL_CONFIG_VERSION,
            }
            and cached_comparable == expected_comparable
        )
    if (payload.get("schema_version") != MODEL_INPUT_VERSION
        or payload.get("scenario_id") != state.scenario_id
        or payload.get("decision_epoch") != state.decision_epoch
        or source != expected_source
        or not config_compatible
        or payload.get("gate") != "READY_WITH_FINITE_PATH_DOMAIN"):
        reject("MODEL_INPUT_CONTRACT_MISMATCH", "schema_version/source/config",
               "cached model input differs from state/source/config contract")
    expected_nodes = tuple(sorted({
        state.depot.graph_node_id,
        *(order.graph_node_id for order in state.orders),
    }))
    raw_nodes = payload.get("node_order")
    if (not isinstance(raw_nodes, list)
        or any(type(item) is not int for item in raw_nodes)
        or len(raw_nodes) != len(set(raw_nodes))
        or tuple(raw_nodes) != expected_nodes):
        reject("MODEL_INPUT_NODE_DOMAIN", "node_order",
               "node_order must equal the unique sorted physical decision-node set")
    expected_pairs = tuple(
        (origin, destination)
        for origin in expected_nodes for destination in expected_nodes
        if origin != destination
    )
    pairs = payload.get("pair_candidates")
    if (not isinstance(pairs, list)
        or payload.get("pair_count") != len(pairs)
        or payload.get("required_pair_count") != len(expected_pairs)
        or len(pairs) != len(expected_pairs)):
        reject("MODEL_INPUT_PAIR_COVERAGE", "pair_candidates",
               "cached model-input pair count differs from required directed pairs")
    pair_map: dict[tuple[int, int], Mapping[str, Any]] = {}
    for index, item in enumerate(pairs):
        if not isinstance(item, Mapping):
            reject("MODEL_INPUT_PAIR_SCHEMA", f"pair_candidates[{index}]",
                   "cached pair must be an object")
        origin, destination = item.get("origin_node"), item.get("destination_node")
        if type(origin) is not int or type(destination) is not int:
            reject("MODEL_INPUT_PAIR_SCHEMA", f"pair_candidates[{index}]",
                   "pair endpoints must be integer graph node IDs")
        key = (origin, destination)
        if key not in expected_pairs:
            reject("MODEL_INPUT_PAIR_DOMAIN", f"pair_candidates[{index}]",
                   "pair is outside the required directed domain")
        if key in pair_map:
            reject("MODEL_INPUT_PAIR_DUPLICATE", f"pair_candidates[{index}]",
                   "directed pair appears more than once")
        if item.get("incoming_edge") is not None:
            reject("MODEL_INPUT_INCOMING_SCOPE", f"pair_candidates[{index}].incoming_edge",
                   "proposal pairs must start without incoming-edge context")
        try:
            reconstructed = graph.path_from_edge_ids(
                origin, item.get("edge_ids"), incoming_edge=None,
            )
        except (RoadDataError, KeyError, TypeError, ValueError) as error:
            reject("MODEL_INPUT_RAW_PATH", f"pair_candidates[{index}].edge_ids",
                   str(error))
        if (reconstructed.candidate.to_dict() != item.get("candidate_path")
            or list(reconstructed.edge_ids) != item.get("edge_ids")
            or reconstructed.final_edge != item.get("final_edge")
            or item.get("selected_path_id") != reconstructed.candidate.path_id
            or list(reconstructed.feature_provenance) != item.get("feature_provenance")
            or reconstructed.candidate.nodes[-1] != destination):
            reject("MODEL_INPUT_RAW_PATH", f"pair_candidates[{index}]",
                   "cached pair differs from raw SQLite reconstruction")
        pair_map[key] = item
    if set(pair_map) != set(expected_pairs):
        reject("MODEL_INPUT_PAIR_COVERAGE", "pair_candidates",
               "cached directed pair set is incomplete or contains extras")

    authenticated_candidates = [
        CandidatePath.from_dict(pair_map[key]["candidate_path"])
        for key in expected_pairs
    ]
    builder = MatrixBuilder()
    expected_matrix = builder.build(
        authenticated_candidates,
        matrix_version=_matrix_version(state),
        node_order=expected_nodes,
        source=_matrix_source(state, graph),
    )
    if expected_matrix is None:
        reject("MODEL_INPUT_MATRIX_REBUILD", "matrix_bundle",
               "authenticated raw paths could not rebuild D/T/R/G")
    matrix_payload = payload.get("matrix_bundle")
    try:
        MatrixBundle.from_dict(matrix_payload)
    except (KeyError, TypeError, ValueError) as error:
        reject("MODEL_INPUT_MATRIX_SCHEMA", "matrix_bundle", str(error))
    if matrix_payload != expected_matrix.to_dict():
        reject("MODEL_INPUT_MATRIX_SOURCE_MISMATCH", "matrix_bundle",
               "D/T/R/G or provenance differs from authenticated raw paths")
    expected_units = {
        "distance": "meter", "travel_time": "second", "demand": "kg",
        "cost_rate": "VND/km", "cost": "VND",
        "risk": "raw_relative_exposure_proxy",
        "geometry": "WGS84_longitude_latitude",
    }
    if payload.get("units") != expected_units:
        reject("MODEL_INPUT_UNIT_MISMATCH", "units",
               "cached unit contract differs from bridge contract")
    diagnostics = payload.get("diagnostics")
    if (not isinstance(diagnostics, list)
        or any(isinstance(item, Mapping) and item.get("severity") == "ERROR"
               for item in diagnostics)):
        reject("MODEL_INPUT_DIAGNOSTICS", "diagnostics",
               "READY cache must have a diagnostic list without source errors")
    return dict(payload)


def _prove_deadline_unreachable(
    order: Any, state: DecisionState, graph: Member1RoadGraph,
) -> bool:
    """Uncapped raw turn-state outbound lower bound, independent of CP-SAT."""

    forbidden = {(row[0], row[1]) for row in graph.db.execute(
        "SELECT inEdgeId,outEdgeId FROM forbidden_turns"
    )}
    epoch = datetime.fromisoformat(state.decision_epoch)
    depot_open = _seconds(state.depot.opening_time, epoch)
    for vehicle in state.vehicles:
        departure = max(0.0, depot_open, _seconds(vehicle.working_start, epoch))
        budget = min(_seconds(order.hard_deadline, epoch),
                     _seconds(vehicle.working_end, epoch)) \
            - order.service_time_seconds - departure
        if budget < -1e-9:
            continue
        initial = (state.depot.graph_node_id, None)
        best = {initial: 0.0}
        serial = 0
        queue = [(0.0, serial, *initial)]
        while queue:
            travel, _, node, incoming = heapq.heappop(queue)
            if travel != best.get((node, incoming)):
                continue
            if node == order.graph_node_id and (node, incoming) != initial:
                return False
            for row in graph.db.execute(
                "SELECT e.edgeId,e.toNodeId,f.travelTimeHours "
                "FROM edges e JOIN costs.features f USING(edgeId) "
                "WHERE e.fromNodeId=? ORDER BY e.edgeId", (node,),
            ):
                edge_id, target, hours = row
                if (incoming, edge_id) in forbidden:
                    continue
                if (isinstance(hours, bool) or not isinstance(hours, (int, float))
                    or not math.isfinite(hours) or hours < 0):
                    raise RoadDataError(f"invalid travelTimeHours on edge {edge_id}")
                following = travel + hours * 3600.0
                next_state = (int(target), str(edge_id))
                if following <= budget + 1e-9 and following < best.get(next_state, math.inf):
                    best[next_state] = following
                    serial += 1
                    heapq.heappush(queue, (following, serial, *next_state))
    return True


def _realize_route(
    state: DecisionState,
    graph: Member1RoadGraph,
    vehicle: Any,
    sequence: tuple[str, ...],
    *,
    config: M1OrToolsStaticConfig,
    deadline: float,
    telemetry: dict[str, Any],
    pair_seeds: Mapping[tuple[int, int], RoadPath],
) -> dict[str, Any] | None:
    orders = {order.order_id: order for order in state.orders}
    load = sum(orders[order_id].demand_kg for order_id in sequence)
    if load > vehicle.capacity_kg + 1e-9:
        return None
    epoch = datetime.fromisoformat(state.decision_epoch)
    departure = max(0.0, _seconds(state.depot.opening_time, epoch),
                    _seconds(vehicle.working_start, epoch))
    latest_return = min(_seconds(state.depot.closing_time, epoch),
                        _seconds(vehicle.working_end, epoch))
    depot = state.depot.graph_node_id

    def visit(index: int, node: int, incoming: str | None, clock: float,
              current_load: float, distance: float, travel: float,
              exposure: float, lateness: float,
              legs: list[dict[str, Any]], stops: list[dict[str, Any]]) -> dict[str, Any] | None:
        telemetry["route_states"] += 1
        if monotonic() >= deadline:
            telemetry["truncation_reasons"].add("TIME_LIMIT")
            return None
        returning = index == len(sequence)
        target = depot if returning else orders[sequence[index]].graph_node_id
        latest = latest_return if returning else min(
            _seconds(orders[sequence[index]].hard_deadline, epoch),
            _seconds(vehicle.working_end, epoch),
        ) - orders[sequence[index]].service_time_seconds
        if latest < clock - 1e-9:
            return None
        seed = pair_seeds.get((node, target))

        def physical_options():
            """Yield seed first, then lazily backtrack into raw alternatives."""

            yielded: set[tuple[str, ...]] = set()
            if (seed is not None and seed.edge_ids
                and (incoming, seed.edge_ids[0]) not in graph.forbidden
                and seed.candidate.distance <= vehicle.remaining_range_m - distance + 1e-6
                and seed.candidate.travel_time <= latest - clock + 1e-6):
                yielded.add(tuple(seed.edge_ids))
                telemetry["pair_seed_paths_used"] += 1
                yield seed
            # This query is intentionally lazy: accepted seed-only M1 routes
            # do not pay for alternates.  It runs only if the seed is unusable
            # or a downstream leg forces backtracking to this arrival edge.
            result = graph.find_paths(
                node, target, incoming_edge=incoming,
                max_road_states=config.max_road_states,
                max_road_labels=config.max_road_labels,
                max_arrival_options=config.max_arrival_options,
                max_distance_m=max(0.0, vehicle.remaining_range_m - distance),
                max_travel_time_s=max(0.0, latest - clock),
                deadline=deadline,
            )
            telemetry["road_queries"] += 1
            telemetry["road_settled_states"] += result.settled_states
            telemetry["road_generated_labels"] += result.generated_labels
            telemetry["road_dominated_prunes"] += result.dominated_prunes
            telemetry["road_resource_prunes"] += result.resource_prunes
            telemetry["road_peak_active_labels"] = max(
                telemetry["road_peak_active_labels"], result.peak_active_labels,
            )
            if result.limit:
                telemetry["truncation_reasons"].add(
                    "PATH_OPTION_LIMIT" if result.limit == "OPTION_LIMIT" else result.limit
                )
            for option in result.paths:
                identity = tuple(option.edge_ids)
                if identity not in yielded:
                    yielded.add(identity)
                    yield option

        for path in physical_options():
            new_distance = distance + path.candidate.distance
            arrival = clock + path.candidate.travel_time
            if new_distance > vehicle.remaining_range_m + 1e-6 or arrival > latest + 1e-6:
                continue
            leg = path.to_leg(incoming)
            if returning:
                return {
                    "vehicle_id": vehicle.vehicle_id,
                    "start_node": depot, "end_node": depot,
                    "order_sequence": list(sequence),
                    "node_sequence": [depot] + [orders[item].graph_node_id for item in sequence] + [depot],
                    "departure_s": departure, "return_s": arrival,
                    "load_before_pickup_kg": vehicle.current_load_kg,
                    "load_after_depot_pickup_kg": load + vehicle.current_load_kg,
                    "return_load_kg": current_load, "pickup_service_s": 0.0,
                    "legs": legs + [leg], "stops": stops,
                    "total_distance_m": new_distance,
                    "total_travel_time_s": travel + path.candidate.travel_time,
                    "total_exposure": exposure + path.candidate.risk_score,
                    "total_elapsed_time_s": arrival - departure,
                    "total_soft_lateness_s": lateness,
                    "total_cost_vnd": new_distance / 1000.0 * vehicle.cost_per_km_vnd,
                }
            order = orders[sequence[index]]
            service_start = max(arrival, _seconds(order.earliest, epoch))
            completion = service_start + order.service_time_seconds
            if completion > min(_seconds(order.hard_deadline, epoch),
                                _seconds(vehicle.working_end, epoch)) + 1e-6:
                continue
            next_load = current_load - order.demand_kg
            if next_load < -1e-9:
                continue
            soft_late = max(0.0, completion - _seconds(order.preferred_due, epoch))
            stop = {
                "order_id": order.order_id, "node_id": target,
                "arrival_s": arrival, "service_start_s": service_start,
                "completion_s": completion, "waiting_s": service_start - arrival,
                "load_after_delivery_kg": next_load,
                "soft_lateness_s": soft_late,
            }
            found = visit(
                index + 1, target, path.final_edge, completion, next_load,
                new_distance, travel + path.candidate.travel_time,
                exposure + path.candidate.risk_score, lateness + soft_late,
                legs + [leg], stops + [stop],
            )
            if found is not None:
                return found
        return None

    return visit(0, depot, None, departure, load + vehicle.current_load_kg,
                 0.0, 0.0, 0.0, 0.0, [], [])


def _physical_score(
    routes: list[dict[str, Any]], config: M1OrToolsStaticConfig,
) -> dict[str, int]:
    """Return the published lexicographic score from physical route columns."""

    return {
        "served_count": sum(len(route["order_sequence"]) for route in routes),
        "soft_lateness_scaled": sum(
            math.ceil(float(route["total_soft_lateness_s"]) * config.time_scale - 1e-12)
            for route in routes
        ),
        "cost_scaled": sum(
            math.ceil(float(route["total_cost_vnd"]) * config.cost_scale - 1e-12)
            for route in routes
        ),
    }


def _incumbent_snapshot(
    routes: list[dict[str, Any]],
    *,
    config: M1OrToolsStaticConfig,
    model_input_sha256: Any,
    origin: str,
    selection_stage: str,
    selected_route_indexes: list[int],
    domain_route_column_ids: list[str],
) -> dict[str, Any]:
    vehicles = [route["vehicle_id"] for route in routes]
    orders = [item for route in routes for item in route["order_sequence"]]
    if (not routes or len(vehicles) != len(set(vehicles))
        or len(orders) != len(set(orders))
        or any(not isinstance(item, str) or not item for item in domain_route_column_ids)
        or len(domain_route_column_ids) != len(set(domain_route_column_ids))
        or any(type(item) is not int or item < 0
               or item >= len(domain_route_column_ids)
               for item in selected_route_indexes)):
        raise ValueError("incumbent routes must be nonempty and vehicle/order disjoint")
    return {
        "available": True,
        "origin": origin,
        "selection_stage": selection_stage,
        "selected_route_column_ids": [route["route_column_id"] for route in routes],
        "selected_route_indexes": list(selected_route_indexes),
        "domain_route_column_count": len(domain_route_column_ids),
        "domain_route_column_ids": list(domain_route_column_ids),
        "model_input_sha256": model_input_sha256,
        "score": _physical_score(routes, config),
        "routes": routes,
    }


def _better_incumbent(
    candidate: Mapping[str, Any], current: Mapping[str, Any] | None,
) -> bool:
    if current is None:
        return True
    candidate_score = candidate["score"]
    current_score = current["score"]
    candidate_key = (
        candidate_score["served_count"],
        -candidate_score["soft_lateness_scaled"],
        -candidate_score["cost_scaled"],
    )
    current_key = (
        current_score["served_count"],
        -current_score["soft_lateness_scaled"],
        -current_score["cost_scaled"],
    )
    return candidate_key > current_key


def solve_member1_ortools_static(
    state: DecisionState,
    graph: Member1RoadGraph,
    *,
    config: M1OrToolsStaticConfig | None = None,
    model_input: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Let OR-Tools choose assignment/sequence, then realize raw turn paths."""

    config = config or M1OrToolsStaticConfig()
    started = monotonic()
    deadline = started + config.time_limit_seconds
    master_reserve_seconds = config.time_limit_seconds * config.master_reserve_fraction
    proposal_deadline = deadline - master_reserve_seconds
    model_input = (
        validate_cached_model_input(model_input, state, graph, config=config)
        if model_input is not None
        else build_ortools_model_input(state, graph, config=config, deadline=deadline)
    )
    versions = {
        "routing_version": state.routing_version,
        "features_version": state.features_version,
        "context_version": state.context_version,
        "delivery_area_version": state.delivery_area_version,
        "state_version": state.state_version,
        "state_schema_version": state.schema_version,
    }
    diagnostics = list(model_input.get("diagnostics", ()))
    telemetry: dict[str, Any] = {
        "limits": config.to_dict(), "route_states": 0, "road_queries": 0,
        "road_settled_states": 0, "road_generated_labels": 0,
        "road_dominated_prunes": 0, "road_resource_prunes": 0,
        "road_peak_active_labels": 0, "assignments_considered": 0,
        "pair_seed_paths_used": 0,
        "route_columns_generated": 0, "proposal_runs": [],
        "refinement_trace": [], "incumbent_updates": [],
        "physical_master_status": "NOT_RUN",
        "budget_scope": {
            "kind": "cooperative_wall_clock",
            "total_seconds": config.time_limit_seconds,
            "master_reserve_seconds": master_reserve_seconds,
            "covers": ["source_build", "proposal", "physical_realization", "master"],
            "validation_and_publication_outside_search_budget": True,
        },
        "truncation_reasons": {"FINITE_PATH_DOMAIN"},
    }
    base = {
        "schema_version": SCHEMA_VERSION, "solver_version": SOLVER_VERSION,
        "scenario_id": state.scenario_id, "decision_epoch": state.decision_epoch,
        "source_authentication": state.source_authentication,
        "source_versions": versions, "source_hashes": dict(graph.source_hashes),
        "receipt_sha256": state.receipt_sha256,
        "served_orders": [], "unserved_orders": [], "vehicle_routes": [],
        "metrics": {}, "diagnostics": diagnostics,
        "engine": {
            "name": "Google OR-Tools CP-SAT", "ortools_version": None,
            "model_config_version": MODEL_CONFIG_VERSION,
            "objective_policy_version": OBJECTIVE_POLICY_VERSION,
            "scaling_version": SCALING_VERSION,
            "warm_start_used": False,
            "restricted_model": True,
        },
        "path_pool": {
            "schema_version": MODEL_INPUT_VERSION,
            "content_sha256": model_input.get("content_sha256"),
            "pair_count": model_input.get("pair_count", 0),
            "required_pair_count": model_input.get("required_pair_count", 0),
            "gate": model_input.get("gate"),
        },
    }

    def finish(status: str, routes: list[dict[str, Any]] | None = None,
               phases: list[dict[str, Any]] | None = None,
               engine_status: str | None = None,
               restricted_objective: Mapping[str, Any] | None = None,
               incumbent: Mapping[str, Any] | None = None,
               termination_reason: str = "NO_WITNESS") -> dict[str, Any]:
        routes = routes or []
        served = sorted(item for route in routes for item in route["order_sequence"])
        all_orders = {order.order_id: order for order in state.orders}
        vehicles = tuple(state.vehicles)
        max_capacity = max((vehicle.capacity_kg for vehicle in vehicles), default=0.0)
        unserved = []
        if status in {"FEASIBLE", "PARTIAL"}:
            for order_id in sorted(set(all_orders) - set(served)):
                order = all_orders[order_id]
                if order.demand_kg > max_capacity + 1e-9:
                    reason = "CAPACITY_EXCEEDS_ALL_VEHICLES_NO_SPLIT"
                elif _prove_deadline_unreachable(order, state, graph):
                    reason = "HARD_COMPLETION_DEADLINE_UNREACHABLE_LOWER_BOUND"
                elif telemetry["truncation_reasons"]:
                    reason = "SEARCH_INCOMPLETE"
                else:
                    reason = "NOT_SERVED_BY_FOUND_WITNESS"
                unserved.append({"order_id": order_id, "reason": reason})
        if status == "FEASIBLE" and unserved:
            status = "PARTIAL" if served else "SEARCH_LIMIT"
        if status == "PARTIAL" and (not served or not unserved):
            status = "SEARCH_LIMIT"
        if status not in {"FEASIBLE", "PARTIAL"}:
            routes, served = [], []
        base["status"] = status
        base["vehicle_routes"] = routes
        base["served_orders"] = served
        base["unserved_orders"] = unserved if status in {"FEASIBLE", "PARTIAL"} else [
            {"order_id": item, "reason": "SEARCH_INCOMPLETE"}
            for item in sorted(all_orders)
        ]
        base["metrics"] = {
            key: sum(route[key] for route in routes)
            for key in ("total_distance_m", "total_travel_time_s", "total_exposure",
                        "total_elapsed_time_s", "total_soft_lateness_s", "total_cost_vnd")
        }
        truncations = sorted(telemetry["truncation_reasons"])
        base["search"] = {
            **{key: value for key, value in telemetry.items()
               if key != "truncation_reasons"},
            "truncated": bool(truncations), "truncation_reasons": truncations,
            "search_complete": False, "optimality_proven": False,
            "elapsed_seconds": monotonic() - started,
            "engine_status": engine_status,
            "phases": phases or [],
            "restricted_objective": dict(restricted_objective or {}),
            "incumbent": (
                {key: value for key, value in incumbent.items() if key != "routes"}
                if incumbent is not None else {
                    "available": False,
                    "origin": None,
                    "selection_stage": None,
                    "selected_route_column_ids": [],
                    "selected_route_indexes": [],
                    "domain_route_column_count": telemetry["route_columns_generated"],
                    "domain_route_column_ids": [],
                    "model_input_sha256": model_input.get("content_sha256"),
                    "score": None,
                }
            ),
            "termination": {
                "reason": termination_reason,
                "optimization_finished": bool(
                    incumbent is not None
                    and telemetry.get("physical_master_status") == "PHYSICAL_PLAN_FOUND"
                    and len(phases or []) == 3
                    and all(item.get("engine_status") in {"OPTIMAL", "FEASIBLE"}
                            for item in (phases or []))
                ),
                "restricted_optimality_proven": bool(
                    incumbent is not None
                    and incumbent.get("origin") == "PHYSICAL_MASTER"
                    and telemetry.get("physical_master_status") == "PHYSICAL_PLAN_FOUND"
                    and len(phases or []) == 3
                    and all(item.get("engine_status") == "OPTIMAL"
                            for item in (phases or []))
                ),
            },
            "witness_score": {
                "served_count": len(served),
                "soft_lateness_s": base["metrics"].get("total_soft_lateness_s", 0.0),
                "cost_vnd": base["metrics"].get("total_cost_vnd", 0.0),
            },
            "scope": (
                "bounded logical proposal generation plus CP-SAT selection over "
                "authenticated physical route columns"
            ),
        }
        base["solution_id"] = "m1-ortools-" + _canonical_hash({
            "scenario": state.scenario_id, "served": served,
            "routes": [[leg["edge_ids"] for leg in route["legs"]] for route in routes],
            "model_input": model_input.get("content_sha256"),
        })[:20]
        return base

    if model_input.get("gate") != "READY_WITH_FINITE_PATH_DOMAIN":
        diagnostics.append(_diagnostic(
            "MODEL_INPUT_BLOCKED", "Source-backed model input did not pass.",
            severity="ERROR",
        ))
        return finish("INVALID_DATA"), model_input
    matrix = MatrixBundle.from_dict(model_input["matrix_bundle"])
    pair_seeds = {
        (item["origin_node"], item["destination_node"]): RoadPath(
            CandidatePath.from_dict(item["candidate_path"]),
            tuple(item["edge_ids"]), tuple(item["feature_provenance"]),
        )
        for item in model_input["pair_candidates"]
    }
    forbidden_signatures: list[dict[str, list[str]]] = []
    last_engine_status = "NOT_RUN"
    last_phases: list[dict[str, Any]] = []
    vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in state.vehicles}
    physical_columns: list[dict[str, Any]] = []
    physical_column_indexes: dict[str, int] = {}
    incumbent: dict[str, Any] | None = None
    proposal_limit_reached = True
    for proposal_no in range(config.max_model_proposals):
        if monotonic() >= proposal_deadline:
            telemetry["truncation_reasons"].add("TIME_LIMIT")
            proposal_limit_reached = False
            break
        telemetry["assignments_considered"] += 1
        remaining = max(0.05, proposal_deadline - monotonic())
        logical = solve_restricted_sequence_model(
            state, matrix, config=config,
            forbidden_signatures=forbidden_signatures,
            phase_time_limit_seconds=max(0.05, remaining / 3.0),
        )
        telemetry["proposal_runs"].append({
            "proposal": proposal_no + 1,
            "engine_status": logical.get("engine_status"),
            "restricted_objective": logical.get("restricted_objective"),
            "selection_phase": logical.get("selection_phase"),
            "phase_sequence_complete": logical.get("phase_sequence_complete", False),
            "phases": logical.get("phases", []),
        })
        base["engine"]["ortools_version"] = logical.get("ortools_version")
        if logical.get("status") != "ASSIGNMENT_FOUND":
            if logical.get("engine_status") == "UNKNOWN":
                telemetry["truncation_reasons"].add("TIME_LIMIT")
            else:
                telemetry["proposal_domain_exhausted"] = True
            proposal_limit_reached = False
            break
        signature = {vehicle_id: list(sequence)
                     for vehicle_id, sequence in logical["sequences"].items()}
        trace = {
            "proposal": proposal_no + 1, "signature": signature,
            "route_columns": [], "unrealized_vehicle_sequences": [],
        }
        proposal_indexes: list[int] = []
        for vehicle_id in sorted(signature):
            sequence = tuple(signature[vehicle_id])
            if not sequence:
                continue
            route = _realize_route(
                state, graph, vehicle_by_id[vehicle_id], sequence,
                config=config, deadline=deadline, telemetry=telemetry,
                pair_seeds=pair_seeds,
            )
            if route is None:
                trace["unrealized_vehicle_sequences"].append({
                    "vehicle_id": vehicle_id, "order_sequence": list(sequence),
                    "reason": "NOT_REALIZED_WITHIN_DECLARED_PATH_BUDGET",
                })
                continue
            identity = _canonical_hash({
                "vehicle_id": vehicle_id,
                "order_sequence": route["order_sequence"],
                "legs": [leg["edge_ids"] for leg in route["legs"]],
            })
            route["route_column_id"] = f"physical-{identity[:20]}"
            trace["route_columns"].append(route["route_column_id"])
            if identity not in physical_column_indexes:
                physical_column_indexes[identity] = len(physical_columns)
                physical_columns.append(route)
                telemetry["route_columns_generated"] += 1
            proposal_indexes.append(physical_column_indexes[identity])
        telemetry["refinement_trace"].append(trace)
        if proposal_indexes:
            proposal_routes = [physical_columns[index] for index in proposal_indexes]
            proposal_incumbent = _incumbent_snapshot(
                proposal_routes,
                config=config,
                model_input_sha256=model_input.get("content_sha256"),
                origin="PROPOSAL_REALIZATION",
                selection_stage="proposal_realization",
                selected_route_indexes=proposal_indexes,
                domain_route_column_ids=[
                    item["route_column_id"] for item in physical_columns
                ],
            )
            if _better_incumbent(proposal_incumbent, incumbent):
                incumbent = proposal_incumbent
                telemetry["incumbent_updates"].append({
                    "origin": incumbent["origin"],
                    "selection_stage": incumbent["selection_stage"],
                    "proposal": proposal_no + 1,
                    "selected_route_column_ids": incumbent["selected_route_column_ids"],
                    "domain_route_column_count": incumbent["domain_route_column_count"],
                    "domain_route_column_ids": incumbent["domain_route_column_ids"],
                    "score": incumbent["score"],
                })
        forbidden_signatures.append(signature)
    if proposal_limit_reached:
        telemetry["truncation_reasons"].add("MODEL_PROPOSAL_LIMIT")

    master_attempted = False
    if physical_columns and monotonic() < deadline:
        master_attempted = True
        remaining = max(0.05, deadline - monotonic())
        physical = solve_physical_route_master(
            state, physical_columns, config=config,
            phase_time_limit_seconds=max(0.05, remaining / 3.0),
        )
        base["engine"]["ortools_version"] = physical.get("ortools_version")
        last_engine_status = physical.get("engine_status")
        last_phases = physical.get("phases", [])
        telemetry["physical_master_status"] = physical.get("status")
        if physical.get("status") == "PHYSICAL_PLAN_FOUND":
            selected = set(physical.get("selected_route_indexes", ()))
            routes = [route for index, route in enumerate(physical_columns)
                      if index in selected]
            master_incumbent = _incumbent_snapshot(
                routes,
                config=config,
                model_input_sha256=model_input.get("content_sha256"),
                origin="PHYSICAL_MASTER",
                selection_stage=physical.get("selection_phase", "serve"),
                selected_route_indexes=sorted(selected),
                domain_route_column_ids=[
                    item["route_column_id"] for item in physical_columns
                ],
            )
            if (_better_incumbent(master_incumbent, incumbent)
                or (incumbent is not None
                    and master_incumbent["score"] == incumbent["score"])):
                incumbent = master_incumbent
                telemetry["incumbent_updates"].append({
                    "origin": incumbent["origin"],
                    "selection_stage": incumbent["selection_stage"],
                    "selected_route_column_ids": incumbent["selected_route_column_ids"],
                    "domain_route_column_count": incumbent["domain_route_column_count"],
                    "domain_route_column_ids": incumbent["domain_route_column_ids"],
                    "score": incumbent["score"],
                })
            if not physical.get("phase_sequence_complete", False):
                telemetry["truncation_reasons"].add("TIME_LIMIT")
        elif physical.get("status") == "NO_SELECTION":
            telemetry["truncation_reasons"].add("PHYSICAL_MASTER_EMPTY_SELECTION")
    if monotonic() >= deadline:
        telemetry["truncation_reasons"].add("TIME_LIMIT")
    if incumbent is not None:
        routes = list(incumbent["routes"])
        served_count = incumbent["score"]["served_count"]
        status = "FEASIBLE" if served_count == len(state.orders) else "PARTIAL"
        unfinished = (
            monotonic() >= deadline
            or not master_attempted
            or telemetry.get("physical_master_status") != "PHYSICAL_PLAN_FOUND"
            or len(last_phases) != 3
            or any(item.get("engine_status") not in {"OPTIMAL", "FEASIBLE"}
                   for item in last_phases)
        )
        termination_reason = (
            "TIME_LIMIT_WITH_INCUMBENT" if monotonic() >= deadline
            else "MASTER_NOT_RUN_WITH_PROPOSAL_INCUMBENT" if not master_attempted
            else "PHYSICAL_MASTER_LIMIT_WITH_INCUMBENT" if unfinished
            else "RESTRICTED_PHASE_SEQUENCE_COMPLETED"
        )
        diagnostics.append(_diagnostic(
            "ORTOOLS_PHYSICAL_WITNESS_FOUND",
            "A source-backed physical incumbent was retained independently of search completion.",
            severity="INFO", proposals=len(forbidden_signatures),
            route_columns=len(physical_columns),
            incumbent_origin=incumbent["origin"],
            selection_stage=incumbent["selection_stage"],
        ))
        return finish(
            status,
            routes,
            last_phases,
            last_engine_status,
            incumbent["score"],
            incumbent,
            termination_reason,
        ), model_input
    if monotonic() >= deadline:
        status, termination_reason = "TIME_LIMIT", "TIME_LIMIT_NO_INCUMBENT"
    else:
        status, termination_reason = "SEARCH_LIMIT", "SEARCH_LIMIT_NO_INCUMBENT"
    return finish(
        status,
        phases=last_phases,
        engine_status=last_engine_status,
        termination_reason=termination_reason,
    ), model_input


__all__ = [
    "MODEL_INPUT_VERSION", "ModelInputContractError", "M1OrToolsStaticConfig", "SCHEMA_VERSION",
    "SOLVER_VERSION", "build_ortools_model_input", "solve_member1_ortools_static",
    "validate_cached_model_input",
]
