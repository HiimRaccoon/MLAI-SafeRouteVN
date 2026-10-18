"""Receive the sealed Step 7 packages and run M3-01 checks.

Preparation uses only the standard library. All installs, logs and authority
stores are outside the team tree. M2 files and the M1 snapshot are never edited.
Use --native only in the separately installed, exact-lock runtime environment.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import sys
import sysconfig
import zipfile

BUILD = "80694f511dc735d0b6a1a0a830edd7f6267df395e87dad0ccf180914b49d5a41"
PACKAGES = {
    "runtime": ("SafeRouteVN_TASK02_Step7_Final_Release_Runtime_Windows_20261004.zip",
                "d5345e75db2b41914cf4d0ed96a83e50e9c251637065ae1264eb1f33f41cbe4e", 149),
    "integration": ("SafeRouteVN_TASK02_Step7_Final_Release_Integration_Windows_20261004.zip",
                    "d59917dded655398a04f3856d1ae73c658cc3dec3002a3467fb24e8d4a45b5e7", 182),
}
SOURCE_PINS = {
    "routing/network.sqlite": (378368000, "d4f2412884d7809cba79f86b65f6677c025ebf3e0ead357d3fc3f9cea553a204"),
    "features/features.sqlite": (1111019520, "8870d537c4f3eb3dca07a177951c66abfe204fe61039852109b92f4751981469"),
    "travel/travel.sqlite": (155467776, "a454a76a224243745ec2bc747611a4eded67446db6591028626ea741e25ef865"),
}
VN = timezone(timedelta(hours=7))


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def prepare_package(team: Path, local: Path, kind: str) -> dict:
    name, expected, count = PACKAGES[kind]
    package = team / "docs" / name
    actual = sha(package)
    if actual != expected:
        raise ValueError(f"PACKAGE_HASH_MISMATCH: {name}")
    destination = local / (kind + "_STEP7_80694f51")
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != count or len(names) != len(set(names)):
            raise ValueError("PACKAGE_ENTRY_COUNT_OR_DUPLICATE: " + kind)
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                    or ":" in entry.filename or entry.is_dir()
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("UNSAFE_ARCHIVE_PATH: " + entry.filename)
        if archive.testzip() is not None:
            raise ValueError("PACKAGE_CRC: " + kind)
        if destination.exists():
            disk = {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()}
            if disk != set(names):
                raise ValueError("EXISTING_INSTALL_FILE_SET_CHANGED: " + str(destination))
        else:
            destination.mkdir(parents=True)
            for entry in entries:
                target = destination.joinpath(*PurePosixPath(entry.filename).parts)
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise ValueError("ARCHIVE_ESCAPE")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(entry))
        for entry in entries:
            target = destination / entry.filename
            if target.is_symlink() or target.read_bytes() != archive.read(entry):
                raise ValueError("INSTALLED_BYTES_CHANGED: " + entry.filename)
    return {"status": "PASS", "package": str(package), "sha256": actual,
            "entries": count, "installed_root": str(destination), "all_installed_bytes_match": True}


def production_inventory(root: Path) -> dict:
    record = json.loads((root / "production_inventory.json").read_bytes())
    body = {key: value for key, value in record.items() if key != "build_sha256"}
    actual = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if record["build_sha256"] != BUILD or actual != BUILD:
        raise ValueError("PRODUCTION_INVENTORY_DIGEST")
    paths = set()
    for file in (root / "optimization").rglob("*"):
        relative = file.relative_to(root)
        if file.is_file() and file.suffix in (".py", ".json") and not {"tests", "evaluation", "__pycache__"}.intersection(relative.parts):
            paths.add(relative.as_posix())
    paths.update(p.relative_to(root).as_posix() for p in (root / "configs").rglob("*.json"))
    paths.update(p.relative_to(root).as_posix() for p in (root / "optimization/runtime").glob("*.mjs"))
    for name in ("shared/__init__.py", "shared/contracts/__init__.py", "shared/contracts/task02_api_v1.py",
                 "shared/contracts/task02_api_v1.schema.json", "runtime_entry.py", "requirements-runtime.lock.txt"):
        if (root / name).is_file():
            paths.add(name)
    if paths != set(record["files"]):
        raise ValueError("PRODUCTION_INVENTORY_DOMAIN")
    for name, pin in record["files"].items():
        file = root / name
        if file.stat().st_size != pin["bytes"] or sha(file) != pin["sha256"]:
            raise ValueError("PRODUCTION_PIN_CHANGED: " + name)
    return {"status": "PASS", "build_sha256": BUILD, "production_pins": len(paths), "domain_matches": True}


def map_check(team: Path) -> dict:
    mapping = json.loads((team / "docs/M2_STEP7_UPLOAD_MAP_20261004.json").read_bytes())
    problems = []
    for row in mapping["files"]:
        file = team / row["team_relative_path"]
        if not file.is_file():
            problems.append({"code": "TEAM_FILE_MISSING", "path": row["team_relative_path"]})
        elif file.stat().st_size != row["bytes"] or sha(file) != row["sha256"]:
            problems.append({"code": "TEAM_FILE_CHANGED", "path": row["team_relative_path"]})
    return {"status": "PASS" if not problems else "INCOMPLETE", "mapped_files": len(mapping["files"]),
            "problems": problems, "blocks_isolated_zip_install": False}


def source_check(team: Path) -> dict:
    source = team / "scenarios/cached_context/hcmc/member1-tdbt-v1"
    files = []
    for relative, (size, expected) in SOURCE_PINS.items():
        file = source / relative
        actual = sha(file)
        if file.stat().st_size != size or actual != expected:
            raise ValueError("M1_SOURCE_PIN_MISMATCH: " + relative)
        files.append({"path": str(file), "bytes": size, "sha256": actual, "status": "PASS"})
    sidecars = [str(p) for p in source.rglob("*") if p.is_file() and p.name.endswith(("-wal", "-shm", "-journal"))]
    if sidecars:
        raise ValueError("M1_SQLITE_SIDECARS: " + ",".join(sidecars))
    catalog = json.loads((team / "scenarios/manifests/thu-duc-binh-thanh-v1.json").read_bytes())
    if catalog.get("complete") is not True or set(catalog["scenarios"]) != {f"S{i}" for i in range(9)}:
        raise ValueError("M1_CATALOG_INCOMPLETE")
    for scenario, record in catalog["scenarios"].items():
        fixture = team / "scenarios" / record["file"]
        if sha(fixture) != record["sha256"]:
            raise ValueError("M1_FIXTURE_PIN: " + scenario)
    return {"status": "PASS", "snapshot_root": str(team), "databases": files, "sidecars": sidecars,
            "fixture_hashes_verified": 9, "catalog_sha256": sha(team / "scenarios/manifests/thu-duc-binh-thanh-v1.json"),
            "source_files_modified": False, "filesystem_acl_changed": False}


def environment_check(runtime: Path) -> dict:
    missing = []
    actual = {}
    for line in (runtime / "requirements-runtime.lock.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, expected = line.split("==", 1)
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = None
        actual[name] = version
        if version != expected:
            missing.append({"package": name, "expected": expected, "actual": version})
    interpreter_ok = sys.version_info[:2] == (3, 12) and platform.system() == "Windows"
    return {"status": "PASS" if interpreter_ok and not missing else "BLOCKED", "interpreter": sys.executable,
            "python": sys.version, "base_prefix": sys.base_prefix, "platform": platform.platform(),
            "architecture": platform.machine(), "abi": sysconfig.get_config_var("SOABI"),
            "exact_dependencies": actual, "mismatches": missing, "interpreter_ok": interpreter_ok}


def run_check(name: str, argv: list[str], cwd: Path, log_root: Path, timeout: int = 300) -> dict:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    try:
        result = subprocess.run(argv, cwd=cwd, env=environment, capture_output=True, timeout=timeout)
        code, stdout, stderr = result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as error:
        code, stdout, stderr = -1, error.stdout or b"", error.stderr or b""
    (log_root / (name + ".stdout.log")).write_bytes(stdout)
    (log_root / (name + ".stderr.log")).write_bytes(stderr)
    parsed = None
    try:
        parsed = json.loads(stdout)
    except (ValueError, UnicodeDecodeError):
        pass
    value = {"status": "PASS" if code == 0 else "FAIL", "exit_code": code, "argv": argv,
             "cwd": str(cwd), "stdout_log": str(log_root / (name + ".stdout.log")),
             "stderr_log": str(log_root / (name + ".stderr.log"))}
    if parsed is not None:
        if name.startswith("m4_reference_") and isinstance(parsed, dict):
            value["result"] = {key: parsed.get(key) for key in ("status", "consumer_contract", "label", "real_world_observation", "source_or_vrp_authentication_performed")}
            value["result"]["map_feature_count"] = len(parsed.get("map", {}).get("features", []))
            value["result"]["driver_count"] = len(parsed.get("drivers", []))
        else:
            value["result"] = parsed
    print(json.dumps({"check": name, "status": value["status"], "exit_code": code}), flush=True)
    return value


def native_checks(team: Path, snapshot: Path, local: Path, runtime: Path, integration: Path, logs: Path, m1_python: str | None) -> dict:
    checks = {}
    python = str(Path(sys.executable).resolve())
    views = sum((["--view", f"examples/S{i}_execution_view.json"] for i in (2, 3, 4)), [])
    commands = [
        ("team_handoff_verifier", [python, "-B", "docs/M2_STEP7_VERIFY_20261004.py", "--project-root", str(team)], team),
        ("portable_python", [python, "-B", "-m", "optimization.runtime.portable_tests", *views], integration),
        ("portable_python_optimized", [python, "-B", "-O", "-m", "optimization.runtime.portable_tests", *views], integration),
        ("golden_python", [python, "-B", "-m", "optimization.runtime.consumer_golden", "--examples", "examples"], integration),
        ("golden_python_optimized", [python, "-B", "-O", "-m", "optimization.runtime.consumer_golden", "--examples", "examples"], integration),
        ("portable_js", ["node", "optimization/runtime/consumer_test.mjs"], integration),
        ("golden_js", ["node", "optimization/runtime/consumer_golden.mjs", "examples"], integration),
        ("api_v1", [python, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "shared/contracts/test_task02_api_v1_portable.py"], integration),
        ("source_gate", [python, "-B", "-m", "optimization.integration.member1_decision_state_adapter", "--snapshot-root", str(snapshot),
                         "--output-root", str(logs / "source_gate")], runtime),
    ]
    commands.extend((f"m4_reference_S{i}", ["node", "optimization/tests/runtime_m4_flow.mjs", f"examples/S{i}_execution_view.json"], integration) for i in (2, 3, 4))
    if m1_python:
        commands.append(("m1_verify_scenarios", [m1_python, "-B", "-m", "geo_data.cli", "verify-scenarios",
                                               "--scenarios-root", str(snapshot / "scenarios"), "--suite-id", "thu-duc-binh-thanh-v1"], team))
    for name, argv, cwd in commands:
        checks[name] = run_check(name, argv, cwd, logs)
    smoke = "from pathlib import Path; import json; from optimization.runtime.environment import attest; print(json.dumps(attest(Path.cwd(),smoke=True)))"
    checks["native_environment_smoke"] = run_check("native_environment_smoke", [python, "-B", "-c", smoke], runtime, logs)
    if checks["source_gate"]["status"] != "PASS" or checks["native_environment_smoke"]["status"] != "PASS":
        return checks
    store_dir = local / "state" / logs.name
    store_dir.mkdir(parents=True, exist_ok=False)
    store = store_dir / "g0_authority.sqlite"
    code = """import json, runpy
from pathlib import Path
launcher = runpy.run_path('runtime_entry.py')
launcher['verify_install'](Path.cwd(), Path('production_inventory.json'), BUILD)
from optimization.runtime.sdk import RuntimeClient
client = RuntimeClient(snapshot_root=SNAPSHOT, store_path=STORE, expected_build_sha256=BUILD)
session = 'm3-g0-s0'
boot = client.command('bootstrap', 'm3-g0-bootstrap', session, scenario_id='S0')
if boot['status'] != 'OK':
    print(json.dumps({'status':'G0_BOOTSTRAP_FAIL','response':boot})); raise SystemExit(2)
view = client.command('resolve', 'm3-g0-resolve', session)
if view['status'] != 'OK':
    print(json.dumps({'status':'G0_RESOLVE_FAIL','response':view})); raise SystemExit(2)
value = view['value']
if (value['schema_version'] != 'task02-m2-execution-view/2' or value['execution_mode'] != 'SIMULATED_REPLAY'
    or value['real_world_observation'] is not False or len(value['order_ids']) != 3 or len(value['vehicles']) != 2
    or value['active_job_id'] is not None or value['observed_metrics'] is not None):
    raise ValueError('G0_VIEW_INVARIANT')
print(json.dumps({'status':'G0_BOOTSTRAP_RESOLVE_PASS','session_id':session,'store_path':STORE,
                  'bootstrap_basis':boot['value']['basis'],'view':value}))
"""
    code = f"BUILD={BUILD!r}\nSNAPSHOT={str(snapshot)!r}\nSTORE={str(store)!r}\n" + code
    checks["g0_bootstrap_resolve"] = run_check("g0_bootstrap_resolve", [python, "-B", "-c", code], runtime, logs)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--team-root", required=True, type=Path)
    parser.add_argument("--local-root", required=True, type=Path)
    parser.add_argument("--snapshot-root", type=Path, help="Read-only root containing the pinned scenarios directory; defaults to team-root")
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--m1-python")
    args = parser.parse_args()
    team, local = args.team_root.resolve(), args.local_root.resolve()
    snapshot = args.snapshot_root.resolve() if args.snapshot_root else team
    if local.is_relative_to(team) or team.is_relative_to(local):
        parser.error("local-root must be outside the team tree")
    local.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(VN).strftime("%Y%m%d_%H%M%S_%f")
    logs = local / "receipts" / stamp
    logs.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": "saferoute-m3-step1-preflight/1", "started_at": datetime.now(VN).isoformat(),
              "team_root": str(team), "snapshot_root": str(snapshot), "local_root": str(local), "receipt_root": str(logs), "build_sha256": BUILD,
              "deployment_review": "PENDING_LEADER_REVIEW", "native_requested": args.native, "checks": {}}
    try:
        for kind in PACKAGES:
            report["checks"][kind + "_package"] = prepare_package(team, local, kind)
        runtime = Path(report["checks"]["runtime_package"]["installed_root"])
        integration = Path(report["checks"]["integration_package"]["installed_root"])
        report["checks"]["production_inventory"] = production_inventory(runtime)
        report["checks"]["team_layout"] = map_check(team)
        report["checks"]["m1_raw_source"] = source_check(snapshot)
        report["checks"]["environment"] = environment_check(runtime)
        if args.native and report["checks"]["environment"]["status"] == "PASS":
            report["checks"].update(native_checks(team, snapshot, local, runtime, integration, logs, args.m1_python))
            report["checks"]["source_post_native"] = source_check(snapshot)
            report["checks"]["inventory_post_native"] = production_inventory(runtime)
            required = ["source_gate", "native_environment_smoke", "g0_bootstrap_resolve", "api_v1",
                        "portable_python", "portable_python_optimized", "golden_python", "golden_python_optimized", "portable_js", "golden_js"]
            if args.m1_python:
                required.append("m1_verify_scenarios")
            report["status"] = "G0_TECHNICAL_PASS" if all(report["checks"].get(name, {}).get("status") == "PASS" for name in required) else "G0_BLOCKED"
        else:
            report["status"] = "PREPARED_NATIVE_NOT_RUN" if not args.native else "G0_BLOCKED_ENVIRONMENT"
        handoff = report["checks"].get("team_handoff_verifier", {})
        complete = (report["status"] == "G0_TECHNICAL_PASS" and report["checks"]["team_layout"]["status"] == "PASS"
                    and handoff.get("status") == "PASS" and handoff.get("result", {}).get("team_root_production_inventory_matches") is True
                    and report["checks"].get("m1_verify_scenarios", {}).get("result", {}).get("verified") is True)
        report["step1_input_completeness"] = "COMPLETE_VERIFIED" if complete else "NOT_FULLY_VERIFIED"
        configuration = {"schema_version": "saferoute-m3-server-installation/1", "runtime_root": str(runtime),
                         "integration_root": str(integration), "runtime_python": sys.executable,
                         "snapshot_root": str(snapshot), "expected_build_sha256": BUILD,
                         "authority_store_parent": str(local / "state"), "latest_preflight_receipt": str(logs / "preflight.json"),
                         "technical_preflight_status": report["status"], "deployment_approved": False,
                         "client_config_override_allowed": False}
        write_json(local / "installation.json", configuration)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        report.update(status="PREFLIGHT_BLOCKED", error=str(error))
    report["finished_at"] = datetime.now(VN).isoformat()
    write_json(logs / "preflight.json", report)
    write_json(team / "docs/M3_STEP1_PREFLIGHT_20261005.json", report)
    print(json.dumps({"status": report["status"], "report": str(team / "docs/M3_STEP1_PREFLIGHT_20261005.json"),
                      "receipt_root": str(logs), "error": report.get("error")}), flush=True)
    return 0 if report["status"] in ("G0_TECHNICAL_PASS", "PREPARED_NATIVE_NOT_RUN") else 2


if __name__ == "__main__":
    raise SystemExit(main())
