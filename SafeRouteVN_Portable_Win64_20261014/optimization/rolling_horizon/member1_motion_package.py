"""Whitelist and verify the Step-4 closure review ZIP (patch, not runtime)."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZIP_DEFLATED

PACKAGE_VERSION = "task02-m1-motion-closure-package/1"
SOURCE_FILES = (
    "optimization/models/motion_state.py",
    "optimization/rolling_horizon/member1_motion_acceptance.py",
    "optimization/rolling_horizon/member1_motion_replay.py",
    "optimization/rolling_horizon/member1_motion_validation.py",
    "optimization/rolling_horizon/member1_motion_runner.py",
    "optimization/rolling_horizon/member1_motion_command_evidence.py",
    "optimization/rolling_horizon/member1_motion_checkpoint.py",
    "optimization/rolling_horizon/member1_motion_package.py",
    "optimization/tests/test_member1_motion_replay.py",
    "optimization/tests/test_step4_review_counterexamples.py",
    "optimization/tests/test_step4_closure_invariants.py",
    "docs/task02_member1_motion_state_replay.md",
    "docs/STEP4_CLOSURE_ACCEPTANCE_MATRIX.md",
)
RUN_KINDS = ("certified_S1", "certified_S7", "certified_S5", "certified_S6",
             "certified_S1_optimized")
COMMAND_KINDS = ("m1_pyproj", "m1_verify", "task_audit", "crosswalk",
                 "crosswalk_optimized", "legacy_source", "binding_source") + RUN_KINDS + (
                     "parent_same", "parent_forward", "direct_boundary",
                     "neg_parent_tampered", "neg_receipt_context", "neg_time_optimized",
                     "neg_missing_parent")
MANIFEST_REL = "outputs/member1_motion_step4_closure/M1_MOTION_STEP4_CLOSURE_PACKAGE_MANIFEST.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(repo_root: Path, zip_path: Path) -> dict:
    repo = repo_root.resolve()
    evidence = Path("outputs/member1_motion_step4_closure/evidence")
    checkpoint = json.loads((repo / evidence / "certified_native/checkpoint.json").read_bytes())
    if checkpoint.get("status") != "PASS" or set(checkpoint.get("runs", {})) != set(RUN_KINDS):
        raise ValueError("CHECKPOINT_REQUIRED")
    if set(checkpoint.get("boundary_runs", {})) != {"parent_same", "parent_forward", "direct_boundary"}:
        raise ValueError("BOUNDARY_CHECKPOINT_REQUIRED")
    frozen = json.loads((repo / evidence / "frozen_integrity.json").read_bytes())
    expected_counts = {"B1.1": 200, "B2 FinalCorrection": 12, "E4 historical": 53,
                       "Step3 Binding": 94, "API v1": 34, "Motion Step4 previous outputs": 65}
    if (frozen.get("status") != "PASS" or
            {item["name"]: item["entries_matched"] for item in frozen.get("checks", [])} != expected_counts or
            any(item["entries_matched"] != item["entries_checked"] for item in frozen["checks"])):
        raise ValueError("FROZEN_INTEGRITY_REQUIRED")
    names = list(SOURCE_FILES)
    for kind, run in checkpoint["runs"].items():
        folder = "certified_optimized" if kind.endswith("optimized") else "certified"
        run_root = f"outputs/member1_motion_step4_closure/{folder}/{run['run_id']}"
        names.extend(f"{run_root}/{file}" for file in (
            "accepted_plan_receipt.json", "motion_states.json", "validations.json", "manifest.json"))
    for kind, run in checkpoint["boundary_runs"].items():
        folder = "certified_direct_boundary" if kind == "direct_boundary" else "certified_incremental"
        run_root = f"outputs/member1_motion_step4_closure/{folder}/{run['run_id']}"
        names.extend(f"{run_root}/{file}" for file in (
            "accepted_plan_receipt.json", "motion_states.json", "validations.json", "manifest.json"))
    for kind in COMMAND_KINDS:
        names.extend(f"{evidence.as_posix()}/certified_native/{kind}.{suffix}"
                     for suffix in ("record.json", "stdout.log", "stderr.log", "log"))
    names.extend(f"{evidence.as_posix()}/certified_native/{file}" for file in
                 ("binding_source_result.json", "checkpoint.json",
                  "parent_s1_2115.json", "parent_s1_2115_tampered_rehashed.json",
                  "acceptance_context_tampered.json"))
    names.extend(f"{evidence.as_posix()}/{file}" for file in (
        "reviewer_red.stdout.log", "reviewer_red.stderr.log", "reviewer_red.xml",
        "full_windows_closure.stdout.log", "full_windows_closure.stderr.log",
        "full_windows_closure.xml", "frozen_integrity.json",
        "native_history_counterexample.json"))
    if len(names) != len(set(names)):
        raise ValueError("DUPLICATE_PACKAGE_PATH")
    files: dict[str, dict] = {}
    for name in names:
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or str(pure) != name:
            raise ValueError(f"UNSAFE_PACKAGE_PATH: {name}")
        path = (repo / name).resolve()
        if not path.is_relative_to(repo):
            raise ValueError(f"OUTSIDE_REPO: {name}")
        data = path.read_bytes()
        files[name] = {"bytes": len(data), "sha256": _sha(data)}
    manifest = {"schema_version": PACKAGE_VERSION, "status": "PASS",
                "checkpoint_sha256": _sha((repo / evidence / "certified_native/checkpoint.json").read_bytes()),
                "patch_for_full_task02_tree": True, "sqlite_included": False,
                "manifest_self_hash_excluded": True, "files": files}
    manifest_data = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    target_manifest = repo / MANIFEST_REL
    target_manifest.parent.mkdir(parents=True, exist_ok=True)
    target_manifest.write_bytes(manifest_data)
    zip_path = zip_path.resolve()
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for name in names:
            archive.write(repo / name, name)
        archive.writestr(MANIFEST_REL, manifest_data)
    with ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if len(infos) != len(names) + 1 or len({item.filename for item in infos}) != len(infos):
            raise ValueError("PACKAGE_ENTRY_COUNT_OR_DUPLICATE")
        if archive.testzip() is not None:
            raise ValueError("PACKAGE_CRC")
        for info in infos:
            name = info.filename
            expected = files.get(name)
            data = archive.read(info)
            if name == MANIFEST_REL:
                if data != manifest_data:
                    raise ValueError("PACKAGE_MANIFEST_BYTES")
            elif expected is None or len(data) != expected["bytes"] or _sha(data) != expected["sha256"]:
                raise ValueError(f"PACKAGE_ENTRY_BINDING: {name}")
    return {"status": "PASS", "zip_path": str(zip_path), "zip_bytes": zip_path.stat().st_size,
            "zip_sha256": _sha(zip_path.read_bytes()), "entry_count": len(names) + 1,
            "manifest_sha256": _sha(manifest_data), "manifest_bytes": len(manifest_data)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--zip", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.repo_root, args.zip), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "FAIL", "diagnostic": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
