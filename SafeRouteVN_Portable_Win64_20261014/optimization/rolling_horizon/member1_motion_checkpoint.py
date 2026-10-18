"""Verify a Step-4 native evidence set against bytes, source and current code.

This is local hash-pinned reproducibility, not a signature or an atomic snapshot.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

CHECKPOINT_VERSION = "task02-m1-motion-closure-checkpoint/2"
RUN_KINDS = ("certified_S1", "certified_S7", "certified_S5", "certified_S6",
             "certified_S1_optimized")
BOUNDARY_KINDS = ("parent_same", "parent_forward", "direct_boundary")
KINDS = ("m1_pyproj", "m1_verify", "task_audit", "crosswalk",
         "crosswalk_optimized", "legacy_source", "binding_source") + RUN_KINDS + BOUNDARY_KINDS
NEGATIVE = {
    "neg_parent_tampered": ("PARENT_INVALID", "parent_state"),
    "neg_receipt_context": ("ACCEPTANCE_BINDING", "accepted_plan_receipt"),
    "neg_time_optimized": ("TIME_FORMAT", "target_time"),
    "neg_missing_parent": ("TRUSTED_PARENT_REQUIRED", "parent_state"),
}
PINNED_DB = {
    "network_sqlite_sha256": "d4f2412884d7809cba79f86b65f6677c025ebf3e0ead357d3fc3f9cea553a204",
    "features_sqlite_sha256": "8870d537c4f3eb3dca07a177951c66abfe204fe61039852109b92f4751981469",
}


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"OBJECT_REQUIRED: {path}")
    return value


def verify_command_record(record: dict, evidence_dir: Path, *, expected_exit: int = 0) -> None:
    expected_status = "PASS" if expected_exit == 0 else "FAIL"
    if type(record.get("exit_code")) is not int or record.get("exit_code") != expected_exit or record.get("status") != expected_status:
        raise ValueError(f"COMMAND_FAILED: {record.get('kind')}")
    start = datetime.fromisoformat(record["started_at_utc"])
    end = datetime.fromisoformat(record["ended_at_utc"])
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError(f"COMMAND_TIME: {record.get('kind')}")
    if not isinstance(record.get("argv"), list) or not record["argv"]:
        raise ValueError(f"COMMAND_ARGV: {record.get('kind')}")
    for name in ("stdout", "stderr", "log"):
        path = Path(record[f"{name}_path"]).resolve()
        if not path.is_relative_to(evidence_dir.resolve()):
            raise ValueError(f"COMMAND_LOG_PATH: {record.get('kind')}:{name}")
        if path.stat().st_size != record[f"{name}_bytes"] or _hash(path) != record[f"{name}_sha256"]:
            raise ValueError(f"COMMAND_LOG_BINDING: {record.get('kind')}:{name}")
    expected_log = (f"command_json: {json.dumps(record['argv'], ensure_ascii=False)}\n"
                    f"cwd: {record['cwd']}\nplatform: {record['platform']}\n"
                    f"exit_code: {record['exit_code']}\n"
                    f"status: {expected_status}\n"
                    f"duration_seconds: {record['duration_seconds']:.9f}\n--- stdout ---\n").encode("utf-8")
    expected_log += Path(record["stdout_path"]).read_bytes() + b"\n--- stderr ---\n"
    expected_log += Path(record["stderr_path"]).read_bytes()
    if Path(record["log_path"]).read_bytes() != expected_log:
        raise ValueError(f"COMMAND_TRANSCRIPT: {record.get('kind')}")


def verify(repo_root: Path, snapshot_root: Path, evidence_dir: Path) -> dict:
    repo = repo_root.resolve(); snapshot = snapshot_root.resolve(); evidence = evidence_dir.resolve()
    commands: dict[str, dict] = {}
    for kind in KINDS:
        envelope = _json(evidence / f"{kind}.record.json")
        records = envelope.get("records")
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise ValueError(f"COMMAND_RECORD: {kind}")
        record = records[0]
        if record.get("kind") != kind:
            raise ValueError(f"COMMAND_KIND: {kind}")
        verify_command_record(record, evidence)
        from .member1_command_contract import verify_semantics
        verify_semantics(record, repo, snapshot)
        if Path(record["cwd"]).resolve() != (snapshot if kind.startswith("m1_") else repo):
            raise ValueError(f"COMMAND_CWD: {kind}")
        if kind.startswith("m1_") and Path(record["interpreter"]).resolve() != (snapshot / ".venv/Scripts/python.exe").resolve():
            raise ValueError(f"COMMAND_INTERPRETER: {kind}")
        if kind.endswith("optimized") and "-O" not in record["argv"]:
            raise ValueError(f"COMMAND_OPTIMIZED: {kind}")
        commands[kind] = record

    for kind, (code, path) in NEGATIVE.items():
        envelope = _json(evidence / f"{kind}.record.json")
        records = envelope.get("records")
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise ValueError(f"NEGATIVE_RECORD: {kind}")
        record = records[0]
        if record.get("kind") != kind or Path(record.get("cwd", "")).resolve() != repo:
            raise ValueError(f"NEGATIVE_COMMAND_BINDING: {kind}")
        verify_command_record(record, evidence, expected_exit=2)
        verify_semantics(record, repo, snapshot, expected_exit=2)
        if kind.endswith("optimized") and "-O" not in record["argv"]:
            raise ValueError(f"NEGATIVE_OPTIMIZED: {kind}")
        response = json.loads(Path(record["stderr_path"]).read_bytes())
        diagnostic = response.get("diagnostic", {})
        if response.get("status") != "FAIL" or diagnostic.get("code") != code or diagnostic.get("path") != path:
            raise ValueError(f"NEGATIVE_DIAGNOSTIC: {kind}")

    db_root = snapshot / "scenarios/cached_context/hcmc/member1-tdbt-v1"
    db_paths = {"network_sqlite_sha256": db_root / "routing/network.sqlite",
                "features_sqlite_sha256": db_root / "features/features.sqlite"}
    for key, path in db_paths.items():
        if _hash(path) != PINNED_DB[key]:
            raise ValueError(f"SOURCE_HASH: {key}")
        for suffix in ("-wal", "-shm", "-journal"):
            if Path(str(path) + suffix).exists():
                raise ValueError(f"SOURCE_SIDECAR: {path}{suffix}")

    runs: dict[str, dict] = {}; ids: set[str] = set()
    for kind in RUN_KINDS:
        stdout = Path(commands[kind]["stdout_path"]).read_text(encoding="utf-8")
        line = json.loads(stdout)
        if line.get("status") != "READY" or line.get("gate") != "M1_MOTION_REPLAY_VALIDATED":
            raise ValueError(f"RUN_STDOUT: {kind}")
        directory = Path(line["output"]).resolve()
        if not directory.is_relative_to(repo / "outputs/member1_motion_step4_closure"):
            raise ValueError(f"RUN_PATH: {kind}")
        manifest_path = directory / "manifest.json"
        manifest = _json(manifest_path)
        if (manifest.get("run_id") != line.get("run_id") or directory.name != manifest["run_id"]
                or manifest["run_id"] in ids):
            raise ValueError(f"RUN_ID: {kind}")
        ids.add(manifest["run_id"])
        if (manifest.get("gate") != "M1_MOTION_REPLAY_VALIDATED" or
                manifest.get("snapshot_verified") is not True or
                manifest.get("all_valid") is not True or
                manifest.get("solver_rerun") is not False or
                manifest.get("road_search_run") is not False):
            raise ValueError(f"RUN_GATE: {kind}")
        scenario = "S1" if kind.endswith("optimized") else kind.removeprefix("certified_")
        if manifest.get("scenario_id") != scenario:
            raise ValueError(f"RUN_SCENARIO: {kind}")
        for key, expected in PINNED_DB.items():
            if manifest["source_hashes_before"].get(key) != expected or manifest["source_hashes_after"].get(key) != expected:
                raise ValueError(f"RUN_SOURCE_HASH: {kind}:{key}")
        if manifest["source_hashes_before"] != manifest["source_hashes_after"]:
            raise ValueError(f"RUN_SOURCE_CHANGED: {kind}")
        for name, item in manifest["files"].items():
            path = directory / name
            if path.stat().st_size != item["bytes"] or _hash(path) != item["sha256"]:
                raise ValueError(f"RUN_FILE_BINDING: {kind}:{name}")
        for name, digest in manifest["code_sha256"].items():
            path = (repo / name).resolve()
            if not path.is_relative_to(repo) or _hash(path) != digest:
                raise ValueError(f"RUN_CODE_BINDING: {kind}:{name}")
        validations = _json(directory / "validations.json")["validations"]
        if len(validations) != manifest["state_count"] or any(item.get("valid") is not True for item in validations):
            raise ValueError(f"RUN_VALIDATIONS: {kind}")
        runs[kind] = {"run_id": manifest["run_id"], "manifest_sha256": _hash(manifest_path),
                      "state_count": manifest["state_count"], "scenario_id": scenario}
    boundaries: dict[str, dict] = {}; states: dict[str, dict] = {}
    for kind in BOUNDARY_KINDS:
        response = json.loads(Path(commands[kind]["stdout_path"]).read_bytes())
        directory = Path(response["output"]).resolve()
        if (response.get("status") != "READY" or not directory.is_relative_to(repo / "outputs/member1_motion_step4_closure")
                or response.get("run_id") != directory.name or response["run_id"] in ids):
            raise ValueError(f"BOUNDARY_RUN_BINDING: {kind}")
        ids.add(response["run_id"])
        manifest = _json(directory / "manifest.json")
        if manifest.get("all_valid") is not True or manifest.get("state_count") != 1:
            raise ValueError(f"BOUNDARY_RUN_VALIDATION: {kind}")
        for name, item in manifest["files"].items():
            path = directory / name
            if path.stat().st_size != item["bytes"] or _hash(path) != item["sha256"]:
                raise ValueError(f"BOUNDARY_RUN_FILE: {kind}:{name}")
        states[kind] = _json(directory / "motion_states.json")["states"][0]
        boundaries[kind] = {"run_id": response["run_id"],
                            "manifest_sha256": _hash(directory / "manifest.json")}
    parent = _json(evidence / "parent_s1_2115.json")
    if states["parent_same"] != parent:
        raise ValueError("BOUNDARY_NOOP_CHANGED")
    for key in ("orders", "vehicles", "execution_history", "execution_metrics", "executed_prefix_sha256"):
        if states["parent_forward"][key] != states["direct_boundary"][key]:
            raise ValueError(f"BOUNDARY_COMPOSITION: {key}")
    if states["parent_forward"]["state_version"] != states["direct_boundary"]["state_version"] + 1:
        raise ValueError("BOUNDARY_LINEAGE_VERSION")
    return {"schema_version": CHECKPOINT_VERSION, "status": "PASS",
            "source_database_sha256": PINNED_DB, "commands": list(KINDS),
            "negative_commands": NEGATIVE, "runs": runs, "boundary_runs": boundaries,
            "scope": {"solver_rerun": False, "road_search_run": False,
                      "general_m1_validated": False, "event_replay": False}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.repo_root, args.snapshot_root, args.evidence_dir)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "PASS", "output": str(args.output),
                          "sha256": _hash(args.output), "runs": result["runs"]}))
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "FAIL", "diagnostic": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
