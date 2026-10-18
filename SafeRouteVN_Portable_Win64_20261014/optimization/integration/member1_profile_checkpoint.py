"""Build a deterministic Step-3 acceptance manifest from selected Windows runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _domain_digest(value: dict[str, Any]) -> str:
    body = dict(value)
    body.pop("content_sha256", None)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_REQUIRED_EVIDENCE = {"full_pytest", "m1_verify_scenarios", "task02_source_audit",
                      "crosswalk_normal", "crosswalk_python_o", "s7_profile_python_o",
                      "legacy_raw_revalidation"}
_BINDING_EVIDENCE = {"step3_binding_revalidation",
                     "step3_binding_revalidation_python_o"}

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HISTORICAL_PACKAGE_MANIFEST = (
    _REPO_ROOT / "outputs/member1_profiles_step3_closure_checks/"
    "M1_PROFILES_STEP3_COMPLETE_PACKAGE_MANIFEST.json"
)
_HISTORICAL_PACKAGE_MANIFEST_SHA256 = (
    "491ca5c786be2259e8558f061bdface9de12b443ad83d5592bbcaa51d7df6c59"
)
_HISTORICAL_BASELINE_ZIP_SHA256 = (
    "954d191eb51173de6d878b0ba6c9cc278b95d1ba0cd39b6b82af79eb57ae89d6"
)
_HISTORICAL_RUN_MANIFESTS = {
    "S1_PROFILES_20261002T084936626621Z":
        "outputs/member1_profiles_step3_closure_checks/"
        "S1_PROFILES_20261002T084936626621Z/manifest.json",
    "S7_PROFILES_20261002T084937293047Z":
        "outputs/member1_profiles_step3_closure_checks/"
        "S7_PROFILES_20261002T084937293047Z/manifest.json",
    "S8_PROFILES_20261002T084938130004Z":
        "outputs/member1_profiles_step3_closure_checks/"
        "S8_PROFILES_20261002T084938130004Z/manifest.json",
    "S7_PROFILES_20261002T085629330286Z":
        "outputs/member1_profiles_step3_closure_checks_optimized_evidence/"
        "S7_PROFILES_20261002T085629330286Z/manifest.json",
}
_LEGACY_PACKAGE_MANIFEST = (
    _REPO_ROOT / "outputs/member1_profiles_step3_final_review/"
    "M1_PROFILES_STEP3_COMPLETE_PACKAGE_MANIFEST.json"
)
_LEGACY_PACKAGE_MANIFEST_SHA256 = (
    "9373c03c4d9e9813d400350daa8e23da5a43aec2bc1e474df213f452fbd888ba"
)
_LEGACY_BASELINE_ZIP_SHA256 = (
    "b257175aad97dd301b7a6e806dbb777096eb6b2fa2fccb8d5c10576ce42a2dc3"
)
_LEGACY_RUN_MANIFESTS = {
    "S1_PROFILES_20261002T014721576293Z":
        "outputs/member1_profiles_step3_final_review/"
        "S1_PROFILES_20261002T014721576293Z/manifest.json",
    "S7_PROFILES_20261002T013724835268Z":
        "outputs/member1_profiles_step3_final_review/"
        "S7_PROFILES_20261002T013724835268Z/manifest.json",
    "S8_PROFILES_20261002T014135038138Z":
        "outputs/member1_profiles_step3_final_review/"
        "S8_PROFILES_20261002T014135038138Z/manifest.json",
    "S7_PROFILES_20261002T020516689511Z":
        "outputs/member1_profiles_step3_final_review_optimized_evidence/"
        "S7_PROFILES_20261002T020516689511Z/manifest.json",
}


def _path_key(value: str) -> str:
    return value.replace("/", "\\").rstrip("\\").casefold()


def _historical_acceptance() -> tuple[list[dict[str, Any]],
                                      dict[str, dict[str, Any]]]:
    """Load the immutable local receipt for the reviewed historical executions.

    The pin is a local consistency anchor, not a signature.  It deliberately
    binds historical manifests to the reviewed Closure Checks package while
    current checker hashes are recorded separately in the new checkpoint.
    """
    sources = (
        (_HISTORICAL_PACKAGE_MANIFEST, _HISTORICAL_PACKAGE_MANIFEST_SHA256,
         _HISTORICAL_BASELINE_ZIP_SHA256, _HISTORICAL_RUN_MANIFESTS),
        (_LEGACY_PACKAGE_MANIFEST, _LEGACY_PACKAGE_MANIFEST_SHA256,
         _LEGACY_BASELINE_ZIP_SHA256, _LEGACY_RUN_MANIFESTS),
    )
    receipts: list[dict[str, Any]] = []
    pins: dict[str, dict[str, Any]] = {}
    for package_path, package_sha, baseline_sha, run_paths in sources:
        if not package_path.is_file() or _sha(package_path) != package_sha:
            raise ValueError(
                "historical Step-3 package manifest pin is unavailable or changed"
            )
        value = json.loads(package_path.read_bytes())
        files = value.get("files") if isinstance(value, dict) else None
        if not isinstance(files, dict):
            raise ValueError("historical Step-3 package manifest file table is invalid")
        receipt = {
            "baseline_zip_sha256": baseline_sha,
            "package_manifest": {
                "path": str(package_path.relative_to(_REPO_ROOT)).replace("\\", "/"),
                "bytes": package_path.stat().st_size,
                "sha256": package_sha,
            },
        }
        receipts.append(receipt)
        for run_id, relative in run_paths.items():
            expected = files.get(relative)
            if (not isinstance(expected, dict)
                    or type(expected.get("bytes")) is not int
                    or expected["bytes"] <= 0
                    or not isinstance(expected.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"])):
                raise ValueError(f"historical run manifest pin is missing: {run_id}")
            pins[run_id] = {
                "path": relative,
                "package_manifest_sha256": package_sha,
                "baseline_zip_sha256": baseline_sha,
                **expected,
            }
    return receipts, pins


def _verify_historical_manifest(path: Path, run_id: str,
                                pins: dict[str, dict[str, Any]]) -> None:
    expected = pins.get(run_id)
    if (expected is None or path.stat().st_size != expected["bytes"]
            or _sha(path) != expected["sha256"]):
        raise ValueError(f"historical execution manifest pin mismatch: {run_id}")


def _declared_stream(item: dict[str, Any], key: str, log_path: Path,
                     transcript_value: bytes) -> None:
    path_value = item.get(f"{key}_path")
    expected_bytes = item.get(f"{key}_bytes")
    expected_sha = item.get(f"{key}_sha256")
    if (not isinstance(path_value, str) or type(expected_bytes) is not int
            or expected_bytes < 0 or not isinstance(expected_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)):
        raise ValueError(f"invalid {key} evidence metadata")
    declared = Path(path_value)
    candidates = [declared, log_path.parent / declared.name]
    existing = next((path for path in candidates if path.is_file()), None)
    if existing is not None:
        value = existing.read_bytes()
        if value != transcript_value:
            raise ValueError(
                f"evidence {key} sidecar contradicts combined transcript: {log_path}"
            )
    else:
        value = transcript_value
    if len(value) != expected_bytes or hashlib.sha256(value).hexdigest() != expected_sha:
        raise ValueError(f"evidence {key} hash/size contradicts transcript: {log_path}")


def _validate_log_claim(item: dict[str, Any], log_path: Path) -> dict[str, Any] | None:
    """Bind a receipt to the exact native transcript and captured streams."""
    raw = log_path.read_bytes()
    stdout_marker = b"--- stdout ---\n"
    stderr_marker = b"\n--- stderr ---\n"
    if stdout_marker not in raw:
        raise ValueError(f"native evidence lacks stdout marker: {log_path}")
    header_raw, remainder = raw.split(stdout_marker, 1)
    stdout_size = item.get("stdout_bytes")
    if type(stdout_size) is not int or stdout_size < 0 or len(remainder) < stdout_size:
        raise ValueError(f"native evidence stdout length is invalid: {log_path}")
    stdout_value = remainder[:stdout_size]
    suffix = remainder[stdout_size:]
    if not suffix.startswith(stderr_marker):
        raise ValueError(f"native evidence stream boundary is invalid: {log_path}")
    stderr_value = suffix[len(stderr_marker):]
    header: dict[str, str] = {}
    for raw_line in header_raw.decode("utf-8", errors="strict").splitlines():
        if ":" not in raw_line:
            raise ValueError(f"native evidence header is malformed: {log_path}")
        key, value = raw_line.split(":", 1)
        if key in header:
            raise ValueError(f"native evidence header is duplicated: {key}")
        header[key] = value.strip()
    expected_headers = {"command_json", "cwd", "platform", "exit_code", "status",
                        "duration_seconds"}
    if set(header) != expected_headers:
        raise ValueError(f"native evidence header fields are invalid: {log_path}")
    try:
        native_argv = json.loads(header["command_json"])
        native_exit = int(header["exit_code"])
        native_duration = float(header["duration_seconds"])
    except (json.JSONDecodeError, ValueError, OverflowError) as error:
        raise ValueError(f"native evidence header value is invalid: {log_path}") from error
    if native_argv != item["argv"]:
        raise ValueError(f"evidence argv contradicts native command_json: {log_path}")
    if native_exit != item["exit_code"]:
        raise ValueError(f"evidence exit contradicts raw log: {log_path}")
    expected_status = "PASS" if item["status"] == "PASS" else (
        "ENVIRONMENT_BLOCKED" if item["status"] == "BLOCKED" else "FAIL")
    if header["status"] != expected_status:
        raise ValueError(f"evidence status contradicts raw log: {log_path}")
    if (_path_key(header["cwd"]) != _path_key(item["cwd"])
            or header["platform"] != item["platform"]
            or _path_key(item["interpreter"]) != _path_key(item["argv"][0])):
        raise ValueError(f"evidence execution identity contradicts raw log: {log_path}")
    if not math.isfinite(native_duration) or abs(native_duration - item["duration_seconds"]) > 1e-6:
        raise ValueError(f"evidence duration contradicts raw log: {log_path}")
    _declared_stream(item, "stdout", log_path, stdout_value)
    _declared_stream(item, "stderr", log_path, stderr_value)
    text = raw.decode("utf-8", errors="replace")
    blocked_markers = ("ENVIRONMENT_BLOCKED", "APPLICATION CONTROL", "IMPORTERROR")
    upper = text.upper()
    if item["status"] == "PASS" and any(marker in upper for marker in blocked_markers):
        raise ValueError(f"PASS evidence contradicts blocked/error raw log: {log_path}")
    if item["kind"] in {"s7_profile_python_o", "step3_binding_revalidation",
                        "step3_binding_revalidation_python_o"}:
        try:
            value = json.loads(stdout_value)
        except json.JSONDecodeError as error:
            raise ValueError("native evidence stdout is not one JSON result") from error
        if not isinstance(value, dict):
            raise ValueError("native evidence stdout result must be an object")
        return value
    return None


def _option_value(argv: list[str], name: str) -> str | None:
    indexes = [index for index, value in enumerate(argv) if value == name]
    if len(indexes) != 1 or indexes[0] + 1 >= len(argv):
        return None
    value = argv[indexes[0] + 1]
    return value if value and not value.startswith("--") else None


def _contains_tokens(argv: list[str], expected: tuple[str, ...]) -> bool:
    return any(tuple(argv[index:index + len(expected)]) == expected
               for index in range(len(argv) - len(expected) + 1))


def _validate_command_shape(kind: str, argv: list[Any], cwd: str,
                            interpreter: str) -> None:
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ValueError(f"invalid argv for evidence kind {kind}")
    values = list(argv)
    commands = {
        "full_pytest": ("-m", "pytest", "-q"),
        "m1_verify_scenarios": ("-m", "geo_data.cli", "verify-scenarios"),
        "task02_source_audit": ("-m", "optimization.integration.member1_mapping_audit"),
        "crosswalk_normal": ("-m", "optimization.tests.member1_decision_state_real_source_gate"),
        "crosswalk_python_o": ("-O", "-m", "optimization.tests.member1_decision_state_real_source_gate"),
        "s7_profile_python_o": ("-O", "-m", "optimization.integration.member1_profile_runner"),
        "legacy_raw_revalidation": ("-m", "optimization.tests.member1_profile_legacy_source_gate"),
        "step3_binding_revalidation": (
            "-m", "optimization.tests.member1_profile_binding_source_gate"),
        "step3_binding_revalidation_python_o": (
            "-O", "-m", "optimization.tests.member1_profile_binding_source_gate"),
    }
    expected = commands.get(kind)
    if expected is None or not _contains_tokens(values, expected):
        raise ValueError(f"argv does not match evidence kind {kind}")
    if kind == "full_pytest" and values[1:4] != ["-m", "pytest", "-q"]:
        raise ValueError("full pytest command prefix is not canonical")
    if kind == "m1_verify_scenarios":
        if (_option_value(values, "--scenarios-root") != "scenarios"
                or _option_value(values, "--suite-id") != "thu-duc-binh-thanh-v1"
                or _path_key(interpreter) != _path_key(
                    str(Path(cwd) / ".venv/Scripts/python.exe"))
                or _path_key(cwd).split("\\")[-1] != "member1mappingaudit_20260928"):
            raise ValueError("M1 source command identity is not the pinned Windows gate")
    else:
        task_suffix = ".cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe"
        if (not _path_key(interpreter).endswith(task_suffix)
                or _path_key(cwd).split("\\")[-1] != "saferoutevn_project_structure_plan_aligned"):
            raise ValueError(f"TASK-02 execution identity is invalid for {kind}")
    if kind != "m1_verify_scenarios" and kind != "full_pytest" \
            and _option_value(values, "--snapshot-root") is None:
        raise ValueError(f"snapshot root is missing for evidence kind {kind}")
    if kind == "s7_profile_python_o" and (
            _option_value(values, "--scenario-id") != "S7"
            or _option_value(values, "--output-root") is None):
        raise ValueError("optimized S7 command target/output is invalid")
    if kind == "crosswalk_normal" and "-O" in values:
        raise ValueError("normal crosswalk evidence unexpectedly uses optimized mode")
    if kind == "step3_binding_revalidation" and "-O" in values:
        raise ValueError("normal binding revalidation unexpectedly uses optimized mode")
    if kind in {"step3_binding_revalidation", "step3_binding_revalidation_python_o"} \
            and _option_value(values, "--output") is None:
        raise ValueError("binding revalidation output is missing")


def _load_evidence(paths: list[Path], *, require_pass: bool
                   ) -> tuple[list[dict[str, Any]], set[str], bool]:
    if not paths:
        raise ValueError("checkpoint requires command evidence; manifest labels are insufficient")
    records: list[dict[str, Any]] = []
    kinds: set[str] = set()
    for path in paths:
        raw = json.loads(path.read_bytes())
        values = raw.get("records") if isinstance(raw, dict) else None
        if not isinstance(values, list):
            raise ValueError(f"evidence records are missing: {path}")
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                raise ValueError(f"invalid/nonpassing evidence {path}[{index}]")
            status = item.get("status")
            duration = item.get("duration_seconds")
            duration_value = None
            if type(duration) in (int, float):
                try:
                    duration_value = float(duration)
                except OverflowError:
                    duration_value = None
            if (not isinstance(item.get("kind"), str)
                    or type(item.get("exit_code")) is not int
                    or not isinstance(status, str)
                    or status not in {"PASS", "FAIL", "BLOCKED"}
                    or not isinstance(item.get("argv"), list)
                    or not isinstance(item.get("interpreter"), str)
                    or not isinstance(item.get("cwd"), str)
                    or not isinstance(item.get("platform"), str)
                    or duration_value is None or not math.isfinite(duration_value)
                    or duration_value < 0
                    or not isinstance(item.get("log_path"), str)
                    or type(item.get("log_bytes")) is not int
                    or not isinstance(item.get("log_sha256"), str)):
                raise ValueError(f"invalid/nonpassing evidence {path}[{index}]")
            if item["kind"] in kinds:
                raise ValueError(f"duplicate evidence kind: {item['kind']}")
            log_path = Path(item["log_path"])
            if not log_path.is_absolute():
                log_path = (Path.cwd() / log_path).resolve()
            if (not log_path.is_file()
                    or log_path.stat().st_size != item["log_bytes"]
                    or _sha(log_path) != item["log_sha256"]):
                raise ValueError(f"evidence log hash/size mismatch {path}[{index}]")
            _validate_command_shape(item["kind"], item["argv"], item["cwd"],
                                    item["interpreter"])
            native_result = _validate_log_claim(item, log_path)
            records.append({**item, "evidence_file": str(path),
                            "evidence_sha256": _sha(path),
                            "evidence_bytes": path.stat().st_size,
                            **({"native_result": native_result}
                               if native_result is not None else {})})
            kinds.add(item["kind"])
    missing = _REQUIRED_EVIDENCE - kinds
    if missing:
        raise ValueError(f"checkpoint evidence is incomplete: {sorted(missing)}")
    all_pass = all(item["exit_code"] == 0 and item["status"] == "PASS"
                   for item in records)
    if require_pass and not all_pass:
        raise ValueError("checkpoint evidence contains a failed or blocked command")
    return records, kinds, all_pass


def _validate_optimized_evidence(item: dict[str, Any],
                                 reference_manifest: dict[str, Any],
                                 historical_pins: dict[str, dict[str, Any]]) -> None:
    result = item.get("native_result")
    if not isinstance(result, dict) or result.get("gate") != "S7_PROFILES_VALIDATED":
        raise ValueError("optimized S7 native result/gate is invalid")
    run_id = result.get("run_id")
    if not isinstance(run_id, str) or not run_id.startswith("S7_PROFILES_"):
        raise ValueError("optimized S7 native run ID is invalid")
    candidates: list[Path] = []
    output_dir = result.get("output_dir")
    if isinstance(output_dir, str):
        candidates.append(Path(output_dir))
    candidates.append(Path.cwd() / "outputs/member1_profiles_step3_final_review_optimized_evidence" / run_id)
    run = next((path for path in candidates if (path / "manifest.json").is_file()), None)
    if run is None:
        raise ValueError("optimized S7 run artifact is unavailable")
    manifest_path = run / "manifest.json"
    _verify_historical_manifest(manifest_path, run_id, historical_pins)
    manifest = json.loads(manifest_path.read_bytes())
    if (not isinstance(manifest, dict) or manifest.get("run_id") != run_id
            or manifest.get("scenario_id") != "S7"
            or manifest.get("gate") != result.get("gate")
            or manifest.get("tradeoff") != result.get("tradeoff")
            or manifest.get("snapshot_verified") is not True
            or manifest.get("source_hashes_before") != manifest.get("source_hashes_after")):
        raise ValueError("optimized S7 native result is not bound to its manifest/source")
    platform_value = manifest.get("platform")
    if not isinstance(platform_value, dict) or platform_value.get("optimized_mode") is not True:
        raise ValueError("optimized S7 manifest lacks optimized-mode evidence")
    code = manifest.get("code_sha256")
    if (not isinstance(code, dict) or not code
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in code.values())):
        raise ValueError("optimized S7 code binding is invalid")
    source_before = manifest.get("source_hashes_before")
    source_after = manifest.get("source_hashes_after")
    reference_source_before = reference_manifest.get("source_hashes_before")
    reference_source_after = reference_manifest.get("source_hashes_after")
    if (not isinstance(source_before, dict) or not source_before
            or source_before != source_after
            or source_before != reference_source_before
            or source_after != reference_source_after
            or code != reference_manifest.get("code_sha256")
            or manifest.get("fixture_raw_sha256")
            != reference_manifest.get("fixture_raw_sha256")
            or manifest.get("receipt_sha256")
            != reference_manifest.get("receipt_sha256")
            or manifest.get("config_sha256")
            != reference_manifest.get("config_sha256")):
        raise ValueError(
            "optimized S7 source/code/fixture/config binding differs from main S7"
        )
    if (source_before.get("fixture_s7_sha256") != manifest.get("fixture_raw_sha256")
            or source_before.get("receipt_sha256") != manifest.get("receipt_sha256")
            or code.get("configs/member1_profiles_step3.json")
            != manifest.get("config_sha256")):
        raise ValueError("optimized S7 manifest provenance is internally inconsistent")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("optimized S7 payload binding is missing")
    for name, expected in files.items():
        artifact = run / name
        if (not isinstance(name, str) or not isinstance(expected, dict)
                or not artifact.is_file()
                or expected != {"bytes": artifact.stat().st_size, "sha256": _sha(artifact)}):
            raise ValueError(f"optimized S7 payload binding mismatch: {name}")
    domain = json.loads((run / "shared_domain.json").read_bytes())
    if (not isinstance(domain, dict)
            or domain.get("source_hashes") != source_before
            or domain.get("content_sha256") != manifest.get("domain_sha256")
            or _domain_digest(domain) != domain.get("content_sha256")):
        raise ValueError("optimized S7 domain source/content binding is inconsistent")
    for name in ("fastest", "balanced", "safer"):
        solution = json.loads((run / f"{name}_solution.json").read_bytes())
        if (not isinstance(solution, dict)
                or solution.get("source_hashes") != domain.get("source_hashes")
                or solution.get("physical_domain", {}).get("content_sha256")
                != domain.get("content_sha256")):
            raise ValueError(
                f"optimized S7 solution source/domain binding is inconsistent: {name}"
            )
        validation = json.loads((run / f"{name}_validation.json").read_bytes())
        if validation.get("valid") is not True:
            raise ValueError(f"optimized S7 witness is not independently valid: {name}")


def _validate_binding_revalidation(item: dict[str, Any], *,
                                   optimized_mode: bool) -> dict[str, Any]:
    result = item.get("native_result")
    if (not isinstance(result, dict)
            or result.get("status") != "STEP3_BINDING_REVALIDATION_PASS"
            or result.get("target_count") != 12
            or not isinstance(result.get("output"), str)
            or type(result.get("output_bytes")) is not int
            or not isinstance(result.get("output_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", result["output_sha256"])):
        raise ValueError("binding revalidation native result is invalid")
    receipt_path = Path(result["output"])
    if not receipt_path.is_absolute():
        receipt_path = (Path.cwd() / receipt_path).resolve()
    if (not receipt_path.is_file()
            or receipt_path.stat().st_size != result["output_bytes"]
            or _sha(receipt_path) != result["output_sha256"]):
        raise ValueError("binding revalidation receipt hash/size mismatch")
    receipt = json.loads(receipt_path.read_bytes())
    if not isinstance(receipt, dict):
        raise ValueError("binding revalidation receipt root is invalid")
    execution = receipt.get("execution")
    runs = receipt.get("runs")
    current = receipt.get("current_checker")
    historical = receipt.get("historical_execution")
    if (receipt.get("schema_version")
            != "task02-m1-profile-step3-binding-revalidation/1"
            or receipt.get("status") != "STEP3_BINDING_REVALIDATION_PASS"
            or receipt.get("target_count") != 12
            or not isinstance(execution, dict)
            or execution.get("optimized_mode") is not optimized_mode
            or not isinstance(runs, list) or len(runs) != 4
            or not isinstance(current, dict)
            or current.get("validator_version")
            != "task02-m1-profile-independent-validator/4"
            or not isinstance(historical, dict)
            or historical.get("solver_rerun") is not False
            or historical.get("road_search_rerun") is not False
            or historical.get("historical_counters_reused_without_relabelling") is not True):
        raise ValueError("binding revalidation receipt metadata is inconsistent")
    targets = [target for run in runs if isinstance(run, dict)
               for target in run.get("targets", ()) if isinstance(target, dict)]
    identities = {(run.get("run_id"), run.get("scenario_id"))
                  for run in runs if isinstance(run, dict)}
    if (len(identities) != 4 or len(targets) != 12
            or any(target.get("valid") is not True for target in targets)):
        raise ValueError("binding revalidation receipt lacks twelve valid targets")
    expected_checkers = {
        "optimization/integration/member1_profile_validation.py":
            _REPO_ROOT / "optimization/integration/member1_profile_validation.py",
        "optimization/integration/member1_profile_checkpoint.py": Path(__file__).resolve(),
        "optimization/integration/member1_static_validation.py":
            _REPO_ROOT / "optimization/integration/member1_static_validation.py",
    }
    code = current.get("code_sha256")
    if (not isinstance(code, dict)
            or code != {name: {"bytes": path.stat().st_size, "sha256": _sha(path)}
                        for name, path in expected_checkers.items()}):
        raise ValueError("binding revalidation current checker provenance is invalid")
    return {"path": str(receipt_path), "bytes": receipt_path.stat().st_size,
            "sha256": _sha(receipt_path), "optimized_mode": optimized_mode,
            "target_count": len(targets)}


def build_checkpoint(output: Path, runs: list[Path],
                     evidence_files: list[Path] | None = None, *,
                     allow_blocked: bool = False,
                     require_binding_revalidation: bool = False) -> Path:
    evidence, kinds, all_pass = _load_evidence(list(evidence_files or []),
                                               require_pass=not allow_blocked)
    if require_binding_revalidation:
        missing_binding = _BINDING_EVIDENCE - kinds
        if missing_binding:
            raise ValueError(
                f"binding revalidation evidence is incomplete: {sorted(missing_binding)}"
            )
    binding_receipts: list[dict[str, Any]] = []
    if kinds.intersection(_BINDING_EVIDENCE):
        if not _BINDING_EVIDENCE.issubset(kinds):
            raise ValueError("binding revalidation normal/-O evidence must be paired")
        for evidence_kind, optimized_mode in (
                ("step3_binding_revalidation", False),
                ("step3_binding_revalidation_python_o", True)):
            record = next(item for item in evidence
                          if item.get("kind") == evidence_kind)
            binding_receipts.append(
                _validate_binding_revalidation(record, optimized_mode=optimized_mode)
            )
    optimized = next((item for item in evidence
                      if item.get("kind") == "s7_profile_python_o"), None)
    if optimized is None:
        raise ValueError("optimized S7 execution evidence is missing")
    historical_receipts, historical_pins = _historical_acceptance()
    optimized_reference: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []
    for run in runs:
        manifest_path = run / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        run_id = manifest.get("run_id") if isinstance(manifest, dict) else None
        if not isinstance(run_id, str):
            raise ValueError(f"run manifest identity is invalid: {run}")
        _verify_historical_manifest(manifest_path, run_id, historical_pins)
        if not manifest.get("gate", "").endswith("_PROFILES_VALIDATED"):
            raise ValueError(f"selected run did not pass: {run}")
        required = {"shared_domain.json",
                    *(f"{name}_solution.json" for name in ("fastest", "balanced", "safer")),
                    *(f"{name}_validation.json" for name in ("fastest", "balanced", "safer"))}
        if any(not (run / name).is_file() for name in required):
            raise FileNotFoundError(f"run payload is incomplete: {run}")
        files = {}
        declared = manifest.get("files")
        if not isinstance(declared, dict):
            raise ValueError(f"run manifest lacks payload hashes: {run}")
        for name in sorted(required):
            path = run / name
            actual = {"bytes": path.stat().st_size, "sha256": _sha(path)}
            if declared.get(name) != actual:
                raise ValueError(f"run payload hash mismatch: {path}")
            files[name] = actual
        for name, expected in sorted(declared.items()):
            path = run / name
            if (not path.is_file() or not isinstance(expected, dict)
                    or expected != {"bytes": path.stat().st_size, "sha256": _sha(path)}):
                raise ValueError(f"declared run artifact hash mismatch: {path}")
            files[name] = expected
        domain = json.loads((run / "shared_domain.json").read_bytes())
        if domain.get("content_sha256") != manifest.get("domain_sha256"):
            raise ValueError(f"domain identity mismatch: {run}")
        for name in ("fastest", "balanced", "safer"):
            solution = json.loads((run / f"{name}_solution.json").read_bytes())
            validation = json.loads((run / f"{name}_validation.json").read_bytes())
            if (solution.get("status") != "FEASIBLE" or validation.get("valid") is not True
                    or solution.get("physical_domain", {}).get("content_sha256") != domain.get("content_sha256")):
                raise ValueError(f"unvalidated profile witness: {run}/{name}")
        if manifest.get("snapshot_verified") is not True:
            raise ValueError(f"snapshot was not verified: {run}")
        if manifest.get("scenario_id") == "S7":
            optimized_reference = manifest
        records.append({"run_id": manifest["run_id"], "scenario_id": manifest["scenario_id"],
                        "gate": manifest["gate"], "domain_sha256": manifest["domain_sha256"],
                        "tradeoff": manifest["tradeoff"], "files": files})
    if (len(records) != 3
            or len({item["run_id"] for item in records}) != 3
            or len({item["scenario_id"] for item in records}) != 3
            or {item["scenario_id"] for item in records} != {"S1", "S7", "S8"}):
        raise ValueError("checkpoint requires exactly S1/S7/S8")
    if optimized_reference is None:
        raise ValueError("checkpoint main S7 reference is missing")
    _validate_optimized_evidence(optimized, optimized_reference, historical_pins)
    optimized_result = optimized.get("native_result")
    optimized_run_id = (optimized_result.get("run_id")
                        if isinstance(optimized_result, dict) else None)
    selected_run_ids = {item["run_id"] for item in records}
    if isinstance(optimized_run_id, str):
        selected_run_ids.add(optimized_run_id)
    selected_pins = {
        run_id: historical_pins[run_id] for run_id in sorted(selected_run_ids)
    }
    selected_package_hashes = {
        item["package_manifest_sha256"] for item in selected_pins.values()
    }
    checker_files = {
        "optimization/integration/member1_profile_validation.py":
            _REPO_ROOT / "optimization/integration/member1_profile_validation.py",
        "optimization/integration/member1_profile_checkpoint.py": Path(__file__).resolve(),
    }
    value = {
        "schema_version": ("task02-m1-profile-step3-checkpoint/3"
                           if require_binding_revalidation else
                           "task02-m1-profile-step3-checkpoint/2"),
        "gate": ("M1_STATIC_PROFILES_S1_S7_S8_READY" if all_pass else
                 "M1_STATIC_PROFILES_S1_S7_S8_FAILED"
                 if any(item["status"] == "FAIL" for item in evidence) else
                 "M1_STATIC_PROFILES_S1_S7_S8_ENVIRONMENT_BLOCKED"),
        "runs": sorted(records, key=lambda item: item["scenario_id"]),
        "normalization": {"version": "task02-m1-common-benchmark-normalization/1",
                          "common_across_scenarios_and_profiles": True,
                          "production_calibrated": False},
        "acceptance_evidence": evidence,
        "historical_execution_receipt": {
            "accepted_packages": [
                item for item in historical_receipts
                if item["package_manifest"]["sha256"] in selected_package_hashes
            ],
            "run_manifests": selected_pins,
            "solver_rerun": False,
            "historical_counters_reused_without_relabelling": True,
        },
        "current_checker_provenance": {
            name: {"bytes": path.stat().st_size, "sha256": _sha(path)}
            for name, path in checker_files.items()
        },
        **({"binding_revalidation": binding_receipts}
           if binding_receipts else {}),
        "limits": {"finite_shared_physical_domain": True,
                   "global_optimality_proven": False, "search_complete": False,
                   "general_m1_validated": False, "e4_run": False,
                   "production_calibration_configured": False,
                   "s2_s4_event_replay_validated": False},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                                 allow_nan=False) + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run", action="append", required=True, type=Path)
    parser.add_argument("--evidence", action="append", required=True, type=Path)
    parser.add_argument("--allow-blocked", action="store_true")
    parser.add_argument("--require-binding-revalidation", action="store_true")
    args = parser.parse_args()
    build_checkpoint(args.output.resolve(), [item.resolve() for item in args.run],
                     [item.resolve() for item in args.evidence],
                     allow_blocked=args.allow_blocked,
                     require_binding_revalidation=args.require_binding_revalidation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
