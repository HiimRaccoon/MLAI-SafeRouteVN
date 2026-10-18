"""Whitelist packager for the review-only M1 Step-3 checkpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile


SOURCE_FILES = (
    "configs/member1_profiles_step3.json",
    "optimization/solver/member1_profile_master.py",
    "optimization/integration/member1_profiles.py",
    "optimization/integration/member1_profile_validation.py",
    "optimization/integration/member1_profile_runner.py",
    "optimization/integration/member1_profile_checkpoint.py",
    "optimization/integration/member1_profile_evidence.py",
    "optimization/integration/member1_profile_package.py",
    "optimization/integration/member1_s0_graph.py",
    "optimization/integration/member1_static_validation.py",
    "optimization/tests/test_member1_profiles_step3.py",
    "optimization/tests/test_member1_profiles_step3_review.py",
    "optimization/tests/test_member1_step3_complete_final_review.py",
    "optimization/tests/test_step3_closure_remaining_review.py",
    "optimization/tests/test_step3_binding_review.py",
    "optimization/tests/test_step3_binding_boundaries.py",
    "optimization/tests/member1_profile_legacy_source_gate.py",
    "optimization/tests/member1_profile_binding_source_gate.py",
    "docs/task02_member1_profiles_step3.md",
    "outputs/member1_profiles_step3_closure_checks/M1_PROFILES_STEP3_COMPLETE_PACKAGE_MANIFEST.json",
    "outputs/member1_profiles_step3_final_review/M1_PROFILES_STEP3_COMPLETE_PACKAGE_MANIFEST.json",
    "outputs/member1_profiles_step3_binding_closure/revalidation_normal.json",
    "outputs/member1_profiles_step3_binding_closure/revalidation_python_o.json",
    "outputs/member1_profiles_step3_binding_closure/evidence/reviewer_red_windows.log",
    "outputs/member1_profiles_step3_binding_closure/evidence/reviewer_red_windows.xml",
    "outputs/member1_profiles_step3_binding_closure/evidence/reviewer_green_windows.log",
    "outputs/member1_profiles_step3_binding_closure/evidence/reviewer_green_windows.xml",
    "outputs/member1_profiles_step3_binding_closure/evidence/full_pytest.xml",
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package(repo: Path, output: Path, runs: list[Path], aggregate: Path,
            evidence: Path) -> tuple[Path, Path]:
    evidence_payload = json.loads(evidence.read_bytes())
    evidence_records = evidence_payload.get("records")
    if not isinstance(evidence_records, list):
        raise ValueError("acceptance evidence records are missing")
    evidence_logs: list[str] = []
    for index, record in enumerate(evidence_records):
        for key in ("log_path", "stdout_path", "stderr_path"):
            value = record.get(key) if isinstance(record, dict) else None
            if not isinstance(value, str):
                raise ValueError(f"acceptance evidence {key} is missing at index {index}")
            declared = Path(value)
            resolved = (declared if declared.is_absolute()
                        else repo / Path(*PurePosixPath(value).parts)).resolve()
            if not resolved.is_relative_to(repo) or not resolved.is_file():
                raise ValueError(f"unsafe/missing acceptance evidence file: {value}")
            evidence_logs.append(str(resolved.relative_to(repo)).replace("\\", "/"))
    names = list(SOURCE_FILES) + [
        str(aggregate.relative_to(repo)).replace("\\", "/"),
        str(evidence.relative_to(repo)).replace("\\", "/"),
    ] + evidence_logs
    for run_dir in runs:
        names.extend(str(path.relative_to(repo)).replace("\\", "/")
                     for path in sorted(run_dir.glob("*.json")))
    if len(names) != len(set(names)):
        raise ValueError("duplicate whitelist entry")
    records = {}
    for name in names:
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(f"unsafe entry: {name}")
        data = (repo / Path(*pure.parts)).read_bytes()
        records[name] = {"bytes": len(data), "sha256": _sha(data)}
    manifest = {
        "schema_version": "task02-m1-profile-step3-package/3",
        "kind": "patch_for_full_task02_tree",
        "prerequisites": ["pinned Member1MappingAudit_20260928 snapshot",
                          "Python 3.12", "OR-Tools 9.15.6755"],
        "entry_count_excluding_this_manifest": len(records), "files": records,
    }
    manifest_path = aggregate.parent / "M1_PROFILES_STEP3_COMPLETE_PACKAGE_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                        indent=2, allow_nan=False) + "\n", encoding="utf-8")
    manifest_name = str(manifest_path.relative_to(repo)).replace("\\", "/")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as archive:
        for name in names + [manifest_name]:
            archive.write(repo / Path(*PurePosixPath(name).parts), arcname=name)
    with zipfile.ZipFile(output, "r") as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("ZIP CRC/duplicate validation failed")
    return output, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run", action="append", required=True, type=Path)
    parser.add_argument("--aggregate", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    output, manifest = package(repo, args.output.resolve(),
                               [item.resolve() for item in args.run],
                               args.aggregate.resolve(), args.evidence.resolve())
    print(json.dumps({"zip": str(output), "bytes": output.stat().st_size,
                      "sha256": _sha(output.read_bytes()),
                      "manifest": str(manifest)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
