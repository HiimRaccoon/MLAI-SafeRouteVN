"""Windows/read-only runner for the M1 Step-3 profile batch."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import platform
from pathlib import Path
import sys
from time import monotonic
from typing import Any

from optimization.models.decision_state import DecisionState
from optimization.solver.member1_profile_master import solve_profile_master

from .member1_decision_state_adapter import load_pinned_initial_states
from .member1_profile_validation import VALIDATOR_VERSION, validate_profile_solution
from .member1_profiles import (DOMAIN_VERSION, PROFILE_ORDER, SOLUTION_VERSION,
                               SOLVER_VERSION, build_shared_profile_domain,
                               load_profile_config, solve_profiles)
from .member1_s0_graph import Member1RoadGraph
from .member1_s1_runner import _require_no_sqlite_sidecars
from .member1_static_runner import _verified_source

MANIFEST_VERSION = "task02-m1-profile-run-manifest/3"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                               allow_nan=False) + "\n", encoding="utf-8")


def _comparison(solutions: dict[str, dict[str, Any]], config: dict[str, Any],
                domain: dict[str, Any]) -> dict[str, Any]:
    refs = config["references"]
    rows = []
    for name in PROFILE_ORDER:
        solution = solutions[name]
        routes = solution["vehicle_routes"]
        service = sum(stop["completion_s"] - stop["service_start_s"]
                      for route in routes for stop in route["stops"])
        waiting = sum(stop["waiting_s"] for route in routes for stop in route["stops"])
        outbound = {"distance_m": sum(route["legs"][0]["distance_m"] for route in routes),
                    "travel_time_s": sum(route["legs"][0]["travel_time_s"] for route in routes),
                    "exposure": sum(route["legs"][0]["exposure"] for route in routes)}
        returned = {"distance_m": sum(route["legs"][-1]["distance_m"] for route in routes),
                    "travel_time_s": sum(route["legs"][-1]["travel_time_s"] for route in routes),
                    "exposure": sum(route["legs"][-1]["exposure"] for route in routes)}
        metrics = solution["metrics"]
        normalized = {"time": metrics["total_travel_time_s"] / refs["travel_time_s"],
                      "distance": metrics["total_distance_m"] / refs["distance_m"],
                      "risk": metrics["total_exposure"] / refs["relative_exposure_proxy"]}
        seed_ids = set(domain.get("sources", {}).get("seed_route_column_ids", ()))
        seed_columns = [item for item in domain["columns"]
                        if item.get("route_column_id") in seed_ids]
        seed_metrics = None
        if seed_columns:
            native_seed = solve_profile_master(
                sorted({order for item in domain["columns"] for order in item["order_sequence"]}),
                sorted({item["vehicle_id"] for item in domain["columns"]}), seed_columns,
                profile_name=name, profile=config["profiles"][name],
                references=config["references"], score_scale=config["score_scale"],
                time_limit_seconds=config["limits"]["master_seconds"],
                random_seed=config["limits"]["random_seed"],
                num_search_workers=config["limits"]["num_search_workers"])
            picked = [seed_columns[index] for index in native_seed.get("selected_route_indexes", ())]
            seed_metrics = {key: sum(item[key] for item in picked) for key in
                            ("total_distance_m", "total_travel_time_s", "total_exposure",
                             "total_soft_lateness_s", "total_cost_vnd")}
        rows.append({"profile": name, "status": solution["status"],
                     "served_orders": solution["served_orders"],
                     "unserved_orders": solution["unserved_orders"],
                     "route_column_ids": [route["route_column_id"] for route in routes],
                     "edge_signatures": [[leg["edge_ids"] for leg in route["legs"]]
                                         for route in routes],
                     "metrics": metrics, "service_time_s": service,
                     "waiting_time_s": waiting, "outbound": outbound, "return": returned,
                     "normalized": normalized,
                     "weighted_score_scaled": solution["profile"]["score_scaled"],
                     "authenticated_seed_metrics": seed_metrics,
                     "seed_to_final_delta": ({key: metrics[key] - seed_metrics[key]
                                               for key in seed_metrics} if seed_metrics else None),
                     "restricted_model_optimal": solution["search"]["restricted_model_optimal"],
                     "termination_reason": solution["search"]["termination_reason"]})
    observed = {tuple(row["route_column_ids"]): row for row in rows}
    sensitivity = []
    for time_weight, risk_weight in ((0.5, 0.5), (0.7, 0.3)):
        ranked = []
        for key, row in observed.items():
            score = time_weight * row["normalized"]["time"] + risk_weight * row["normalized"]["risk"]
            ranked.append({"route_column_ids": list(key), "score": score})
        ranked.sort(key=lambda item: (item["score"], item["route_column_ids"]))
        sensitivity.append({"weights": {"time": time_weight, "risk": risk_weight},
                            "ranking": ranked,
                            "scope": "rerank of complete observed witnesses; no road search"})
    return {"schema_version": "task02-m1-profile-comparison/2",
            "domain_sha256": domain["content_sha256"], "rows": rows,
            "sensitivity": sensitivity,
            "exposure_semantics": "relative exposure proxy; not accident probability"}


def run_profile_batch(snapshot_root: str | Path, output_root: str | Path, *,
                      scenario_id: str, repo_root: str | Path | None = None,
                      config_path: str | Path | None = None,
                      domain_seconds: float = 600.0) -> Path:
    if scenario_id not in {"S1", "S7", "S8"}:
        raise ValueError("scenario_id must be S1, S7, or S8")
    repo = Path(repo_root or Path(__file__).resolve().parents[2]).resolve()
    config_file = Path(config_path or repo / "configs" / "member1_profiles_step3.json").resolve()
    config = load_profile_config(config_file)
    root = Path(snapshot_root).resolve()
    batch = load_pinned_initial_states(root)
    if batch.get("status") != "INITIAL_STATE_READY" or batch.get("source_gate") != "M1_SOURCE_CONTRACT_GATE_PASS":
        raise ValueError("M1 receipt/source gate failed")
    raw = next((item for item in batch["states"] if item["scenario_id"] == scenario_id), None)
    if raw is None:
        raise ValueError("DecisionState is absent")
    state = DecisionState.from_dict(raw)
    paths, before = _verified_source(root, state)
    _require_no_sqlite_sidecars(paths, phase="before_open")
    started = monotonic()
    run_id = f"{scenario_id}_PROFILES_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        with Member1RoadGraph(
            paths["network_sqlite_sha256"], paths["features_sqlite_sha256"],
            routing_version=state.routing_version, features_version=state.features_version,
            context_version=state.context_version, source_hashes=before,
        ) as graph:
            domain_started = monotonic()
            domain = build_shared_profile_domain(
                state, graph, snapshot_root=root, repo_root=repo, config=config,
                deadline=monotonic() + float(domain_seconds),
            )
            domain_elapsed = monotonic() - domain_started
            master_started = monotonic()
            solutions = solve_profiles(state, domain, config)
            master_elapsed = monotonic() - master_started
            validation_started = monotonic()
            validations = {name: validate_profile_solution(value, state, graph, domain, config)
                           for name, value in solutions.items()}
            validation_elapsed = monotonic() - validation_started
    finally:
        _require_no_sqlite_sidecars(paths, phase="after_close")
    after = {key: _sha(path) for key, path in paths.items()}
    snapshot_verified = before == after
    ready = (snapshot_verified and len(solutions) == 3
             and all(item.get("status") == "FEASIBLE" for item in solutions.values())
             and all(item.get("valid") is True for item in validations.values())
             and len({item["physical_domain"]["content_sha256"] for item in solutions.values()}) == 1)
    destination = Path(output_root).resolve() / run_id
    destination.mkdir(parents=True, exist_ok=False)
    documents: dict[str, Any] = {"shared_domain.json": domain,
                                 "profile_comparison.json": _comparison(solutions, config, domain)}
    for name in PROFILE_ORDER:
        documents[f"{name.lower()}_solution.json"] = solutions[name]
        documents[f"{name.lower()}_validation.json"] = validations[name]
    for name, value in documents.items():
        _write(destination / name, value)
    selected = {name: [item["route_column_id"] for item in solutions[name]["vehicle_routes"]]
                for name in PROFILE_ORDER}
    duplicates = []
    for left_pos, left in enumerate(PROFILE_ORDER):
        for right in PROFILE_ORDER[left_pos + 1:]:
            if selected[left] == selected[right]:
                duplicates.append([left, right])
    profile_metrics = {name: solutions[name]["metrics"] for name in PROFILE_ORDER}
    fastest_time = profile_metrics["FASTEST"]["total_travel_time_s"]
    safer_risk = profile_metrics["SAFER"]["total_exposure"]
    tradeoff = {
        "status": ("PASS" if (profile_metrics["SAFER"]["total_exposure"] < profile_metrics["FASTEST"]["total_exposure"]
                                     and profile_metrics["SAFER"]["total_travel_time_s"] >= fastest_time)
                   else "TRADEOFF_NOT_DEMONSTRATED_WITHIN_LIMITS"),
        "fastest_travel_time_s": fastest_time, "fastest_exposure": profile_metrics["FASTEST"]["total_exposure"],
        "safer_travel_time_s": profile_metrics["SAFER"]["total_travel_time_s"], "safer_exposure": safer_risk,
        "duplicate_profile_pairs": duplicates,
    }
    code_files = [Path(__file__), Path(__file__).with_name("member1_profiles.py"),
                  Path(__file__).with_name("member1_profile_validation.py"),
                  repo / "optimization" / "solver" / "member1_profile_master.py",
                  repo / "optimization" / "integration" / "member1_s0_graph.py",
                  repo / "optimization" / "integration" / "member1_static_validation.py",
                  repo / "optimization" / "integration" / "member1_static_runner.py",
                  repo / "optimization" / "integration" / "member1_s1_runner.py",
                  repo / "optimization" / "integration" / "member1_decision_state_adapter.py",
                  repo / "optimization" / "integration" / "member1_mapping_audit.py",
                  repo / "optimization" / "models" / "decision_state.py", config_file]
    manifest = {
        "schema_version": MANIFEST_VERSION, "run_id": run_id, "scenario_id": scenario_id,
        "gate": f"{scenario_id}_PROFILES_VALIDATED" if ready else f"{scenario_id}_PROFILES_BLOCKED",
        "profiles": list(PROFILE_ORDER), "solution_schema_version": SOLUTION_VERSION,
        "solver_version": SOLVER_VERSION, "validator_version": VALIDATOR_VERSION,
        "domain_version": DOMAIN_VERSION, "domain_sha256": domain["content_sha256"],
        "domain_column_count": len(domain["columns"]), "profile_metrics": profile_metrics,
        "selected_route_column_ids": selected, "tradeoff": tradeoff,
        "snapshot_verified": snapshot_verified, "source_hashes_before": before,
        "source_hashes_after": after, "fixture_raw_sha256": state.fixture_raw_sha256,
        "receipt_sha256": state.receipt_sha256, "config_sha256": _sha(config_file),
        "elapsed_seconds": monotonic() - started,
        "stage_elapsed_seconds": {"domain_generation": domain_elapsed,
                                  "profile_masters_total": master_elapsed,
                                  "independent_validation_total": validation_elapsed},
        "platform": {"system": platform.system(), "python": platform.python_version(),
                     "interpreter": str(Path(sys.executable).resolve()), "optimized_mode": not __debug__},
        "code_sha256": {str(path.relative_to(repo)).replace("\\", "/"): _sha(path) for path in code_files},
        "files": {name: {"bytes": (destination / name).stat().st_size,
                         "sha256": _sha(destination / name)} for name in documents},
        "finite_shared_domain": True, "global_optimality_proven": False,
        "search_complete": False, "production_calibration_configured": False,
        "general_m1_validated": False, "e4_run": False,
        "s2_s4_event_replay_validated": False,
    }
    _write(destination / "manifest.json", manifest)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Run receipt-gated M1 Step-3 profiles")
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--scenario-id", required=True, choices=("S1", "S7", "S8"))
    parser.add_argument("--domain-seconds", type=float, default=600.0)
    args = parser.parse_args()
    try:
        destination = run_profile_batch(args.snapshot_root, args.output_root,
                                        scenario_id=args.scenario_id,
                                        domain_seconds=args.domain_seconds)
        manifest = json.loads((destination / "manifest.json").read_bytes())
        print(json.dumps({"run_id": manifest["run_id"], "gate": manifest["gate"],
                          "tradeoff": manifest["tradeoff"], "output_dir": str(destination)},
                         ensure_ascii=False))
        return 0 if manifest["gate"].endswith("_PROFILES_VALIDATED") else 2
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({"gate": f"{args.scenario_id}_PROFILES_BLOCKED",
                          "diagnostic": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MANIFEST_VERSION", "run_profile_batch"]
