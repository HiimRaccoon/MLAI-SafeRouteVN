"""CP-SAT selector over one authenticated physical route-column domain.

This is additive to the frozen static bridge /4.  It never searches the road
graph and therefore cannot invent a physical arc or reset turn state.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from itertools import product
import math
from typing import Any, Mapping, Sequence

PROFILE_MASTER_VERSION = "task02-m1-profile-route-column-master/2"
INT64_MAX = 9_223_372_036_854_775_807


def _finite(value: Any, path: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)) or value < 0:
        raise ValueError(f"{path} must be finite and nonnegative")
    return float(value)


def scaled_profile_score(route: Mapping[str, Any], profile: Mapping[str, Any],
                         references: Mapping[str, Any], scale: int) -> int:
    """Score one complete depot-return column with decimal half-up rounding."""

    if type(scale) is not int or scale <= 0:
        raise ValueError("score scale must be a positive integer")
    travel = Decimal(str(_finite(route.get("total_travel_time_s"), "travel")))
    distance = Decimal(str(_finite(route.get("total_distance_m"), "distance")))
    risk = Decimal(str(_finite(route.get("total_exposure"), "exposure")))
    tref = Decimal(str(_finite(references.get("travel_time_s"), "Tref")))
    dref = Decimal(str(_finite(references.get("distance_m"), "Dref")))
    rref = Decimal(str(_finite(references.get("relative_exposure_proxy"), "Rref")))
    if min(tref, dref, rref) <= 0:
        raise ValueError("normalization references must be positive")
    score = (
        Decimal(str(_finite(profile.get("time"), "time weight"))) * travel / tref
        + Decimal(str(_finite(profile.get("risk"), "risk weight"))) * risk / rref
        + Decimal(str(_finite(profile.get("distance"), "distance weight"))) * distance / dref
    )
    result = int((score * Decimal(scale)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if result > INT64_MAX:
        raise ValueError("profile score exceeds signed int64")
    return result


def _selection_vector(indexes: Sequence[int], normalized: Sequence[Mapping[str, Any]],
                      tie_break: Sequence[str]) -> tuple[int, ...]:
    """Return the one authoritative active-profile comparison tuple.

    All objective tiers are integer tiers identical to those supplied to
    CP-SAT.  This comparator is used for an authenticated seed and every
    feasible phase incumbent, so a later FEASIBLE result cannot erase a
    stronger witness merely because it was produced later.
    """

    picked = [normalized[index] for index in indexes]
    return (
        sum(item["served"] for item in picked),
        -sum(item["late"] for item in picked),
        -sum(item["profile"] for item in picked),
        *(-sum(item[key] for item in picked) for key in tie_break),
        -sum(index + 1 for index in indexes),
    )


def _feasible_seed(normalized: Sequence[Mapping[str, Any]], vehicle_ids: Sequence[str],
                   tie_break: Sequence[str], *, max_combinations: int = 300_000
                   ) -> tuple[list[int] | None, int, bool]:
    """Build and validate a fleet seed from the frozen route-column domain.

    The current M1 gate has at most two vehicles.  Enumerating at most one
    column per vehicle is therefore bounded and validates fleet-level vehicle
    and order uniqueness rather than assuming individually valid columns can
    be combined.  A cap is explicit for future larger domains.
    """

    positions = {vehicle: [None] + [i for i, item in enumerate(normalized)
                                    if item["vehicle"] == vehicle]
                 for vehicle in sorted(set(vehicle_ids))}
    best: list[int] | None = None
    best_vector: tuple[int, ...] | None = None
    checked = 0
    truncated = False
    for choices in product(*(positions[vehicle] for vehicle in sorted(positions))):
        checked += 1
        if checked > max_combinations:
            truncated = True
            break
        indexes = sorted(index for index in choices if index is not None)
        orders = [order for index in indexes for order in normalized[index]["orders"]]
        if len(orders) != len(set(orders)):
            continue
        vector = _selection_vector(indexes, normalized, tie_break)
        if best_vector is None or vector > best_vector:
            best, best_vector = indexes, vector
    return best, checked, truncated


def solve_profile_master(order_ids: Sequence[str], vehicle_ids: Sequence[str],
                         columns: Sequence[Mapping[str, Any]], *, profile_name: str,
                         profile: Mapping[str, Any], references: Mapping[str, Any],
                         score_scale: int, time_limit_seconds: float = 60.0,
                         random_seed: int = 0, num_search_workers: int = 1) -> dict[str, Any]:
    """Lexicographically maximize coverage then minimize lateness/profile score."""

    import ortools
    from ortools.sat.python import cp_model

    if not columns:
        return {"status": "NO_COLUMNS", "engine_status": "NOT_RUN", "phases": []}
    normalized = []
    orders = set(order_ids)
    vehicles = set(vehicle_ids)
    for index, raw in enumerate(columns):
        sequence = raw.get("order_sequence")
        if raw.get("vehicle_id") not in vehicles or not isinstance(sequence, list) or not sequence:
            raise ValueError(f"column {index} identity is invalid")
        if len(sequence) != len(set(sequence)) or any(item not in orders for item in sequence):
            raise ValueError(f"column {index} order sequence is invalid")
        normalized.append({
            "index": index, "vehicle": raw["vehicle_id"], "orders": tuple(sequence),
            "served": len(sequence),
            "late": math.ceil(_finite(raw.get("total_soft_lateness_s"), "lateness") * 1000 - 1e-12),
            "profile": scaled_profile_score(raw, profile, references, score_scale),
            "time": math.ceil(_finite(raw.get("total_travel_time_s"), "travel") * 1000 - 1e-12),
            "distance": math.ceil(_finite(raw.get("total_distance_m"), "distance") * 1000 - 1e-12),
            "risk": math.ceil(_finite(raw.get("total_exposure"), "risk") * 1000000 - 1e-12),
        })
    for key in ("late", "profile", "time", "distance", "risk"):
        if sum(item[key] for item in normalized) > INT64_MAX:
            raise ValueError(f"aggregate {key} objective exceeds signed int64")
    model = cp_model.CpModel()
    chosen = [model.NewBoolVar(f"column_{item['index']}") for item in normalized]
    for vehicle in sorted(vehicles):
        model.Add(sum(chosen[p] for p, item in enumerate(normalized) if item["vehicle"] == vehicle) <= 1)
    for order in sorted(orders):
        model.Add(sum(chosen[p] for p, item in enumerate(normalized) if order in item["orders"]) <= 1)
    expressions = {
        "served": sum(item["served"] * chosen[p] for p, item in enumerate(normalized)),
        "lateness": sum(item["late"] * chosen[p] for p, item in enumerate(normalized)),
        "profile": sum(item["profile"] * chosen[p] for p, item in enumerate(normalized)),
        "time": sum(item["time"] * chosen[p] for p, item in enumerate(normalized)),
        "distance": sum(item["distance"] * chosen[p] for p, item in enumerate(normalized)),
        "risk": sum(item["risk"] * chosen[p] for p, item in enumerate(normalized)),
        "plan_key": sum((item["index"] + 1) * chosen[p]
                        for p, item in enumerate(normalized)),
    }
    phases = [("serve", "served", True), ("soft_due", "lateness", False),
              ("profile", "profile", False)]
    for tie in profile.get("tie_break", []):
        phases.append((f"tie_{tie}", tie, False))
    phases.append(("deterministic_plan_key", "plan_key", False))
    tie_break = tuple(profile.get("tie_break", ()))
    incumbent, seed_combinations, seed_truncated = _feasible_seed(
        normalized, vehicle_ids, tie_break)
    incumbent_vector = (_selection_vector(incumbent, normalized, tie_break)
                        if incumbent is not None else None)
    incumbent_origin = "AUTHENTICATED_DOMAIN_FLEET_SEED" if incumbent else None
    records = []
    for phase_name, key, maximize in phases:
        model.Maximize(expressions[key]) if maximize else model.Minimize(expressions[key])
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(0.01, float(time_limit_seconds) / len(phases))
        solver.parameters.num_search_workers = num_search_workers
        solver.parameters.random_seed = random_seed
        status = solver.Solve(model)
        native = solver.StatusName(status)
        record = {"phase": phase_name, "objective_key": key, "maximize": maximize,
                  "engine_status": native,
                  "objective_value": solver.ObjectiveValue() if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None}
        records.append(record)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break
        candidate = [item["index"] for p, item in enumerate(normalized) if solver.Value(chosen[p])]
        candidate_vector = _selection_vector(candidate, normalized, tie_break)
        record["selected_route_indexes"] = candidate
        record["selection_vector"] = list(candidate_vector)
        if incumbent_vector is None or candidate_vector > incumbent_vector:
            incumbent, incumbent_vector = candidate, candidate_vector
            incumbent_origin = f"ENGINE_PHASE:{phase_name}"
        value = int(solver.Value(expressions[key]))
        # A merely FEASIBLE objective must not lock out a stronger known
        # witness.  Preserve the incumbent bound; exact equality is safe only
        # when the engine proved this phase optimal.
        incumbent_value = ({"served": incumbent_vector[0],
                            "lateness": -incumbent_vector[1],
                            "profile": -incumbent_vector[2]}.get(key))
        if key in tie_break:
            incumbent_value = -incumbent_vector[3 + tie_break.index(key)]
        if key == "plan_key":
            incumbent_value = -incumbent_vector[-1]
        if status == cp_model.OPTIMAL:
            model.Add(expressions[key] == value)
        elif incumbent_value is not None:
            model.Add(expressions[key] >= incumbent_value if maximize
                      else expressions[key] <= incumbent_value)
    if incumbent is None or not incumbent:
        return {"status": "NO_SELECTION", "engine_status": records[-1]["engine_status"], "phases": records,
                "ortools_version": ortools.__version__,
                "seed_combinations_checked": seed_combinations,
                "seed_enumeration_truncated": seed_truncated,
                "termination_reason": "NO_FEASIBLE_NONEMPTY_INCUMBENT"}
    return {"status": "PROFILE_PLAN_FOUND", "engine_status": records[-1]["engine_status"],
            "selected_route_indexes": incumbent, "phases": records,
            "ortools_version": ortools.__version__, "profile": profile_name,
            "restricted_model_optimal": (len(records) == len(phases)
                                           and all(item["engine_status"] == "OPTIMAL" for item in records)),
            "incumbent_origin": incumbent_origin,
            "incumbent_vector": list(incumbent_vector),
            "seed_combinations_checked": seed_combinations,
            "seed_enumeration_truncated": seed_truncated,
            "termination_reason": ("ALL_PHASES_OPTIMAL" if len(records) == len(phases)
                                    and all(item["engine_status"] == "OPTIMAL" for item in records)
                                    else "BEST_KNOWN_FEASIBLE_INCUMBENT_RETAINED")}


__all__ = ["PROFILE_MASTER_VERSION", "scaled_profile_score", "solve_profile_master"]
