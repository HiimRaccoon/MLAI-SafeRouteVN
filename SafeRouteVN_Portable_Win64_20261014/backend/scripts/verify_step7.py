"""Native M3 outbox, committed artifact and coordinated private backup checks.

Reuses independently hash-verified, unchanged M3-06 accepted replay sessions.
No new solve or borrowed solver seed is used. Administrative operations use
only the pinned public RuntimeClient in its isolated runtime interpreter.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from verify_step4 import NativeHarness, require
from verify_step5 import require_error
from verify_step6 import fresh_worker_ready, resume_verified_scenarios


ADMIN_CODE = r'''
import json, pathlib, runpy, sys
config=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
operation=sys.argv[2]; fields=json.loads(sys.argv[3])
root=pathlib.Path(config['runtime_root']).resolve(strict=True)
entry=runpy.run_path(str(root/'runtime_entry.py'))
entry['verify_install'](root,root/'production_inventory.json',config['expected_build_sha256'])
sys.path.insert(0,str(root))
from optimization.runtime.sdk import RuntimeClient
import optimization.runtime.sdk as sdk
if pathlib.Path(sdk.__file__).resolve()!=root/'optimization/runtime/sdk.py':
    raise ValueError('SDK import origin')
store=fields.pop('verified_backup_store',None)
if store is None:store=str(pathlib.Path(config['authority_store_parent'])/'backend_authority.sqlite')
try:
    client=RuntimeClient(snapshot_root=config['snapshot_root'],store_path=store,
        expected_build_sha256=config['expected_build_sha256'])
    if operation=='read_notifications':value=client.read_notifications(**fields)
    elif operation=='acknowledge':value=client.acknowledge(**fields)
    elif operation=='validate_session':value=client.validate_session(**fields)
    elif operation=='backup':value=client.backup(**fields)
    elif operation in ('recover','get_head'):value=client.command(operation,**fields)
    else:raise ValueError('Unknown native administrative operation')
    print(json.dumps({'schema_version':'saferoute-m3-native-sdk-admin/1','status':'OK','value':value},allow_nan=False))
except Exception as error:
    print(json.dumps({'schema_version':'saferoute-m3-native-sdk-admin/1','status':'FAIL',
        'code':getattr(error,'code','NATIVE_SDK_ERROR'),'type':type(error).__name__},allow_nan=False))
'''


def digest_file(path):
    raw = path.read_bytes()
    return {"path": str(path.resolve()), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


class Evidence:
    def __init__(self, output):
        self.root = output.parent / "frames"
        self.root.mkdir(parents=True, exist_ok=True)
        self.frames, self.sequence = [], 0

    def save(self, name, value):
        self.sequence += 1
        path = self.root / f"{self.sequence:02d}_{name}.json"
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        self.frames.append({"name": name, **digest_file(path)})
        return value


class AuditedHarness(NativeHarness):
    def __init__(self, project, settings, output, evidence):
        super().__init__(project, settings, output)
        self.evidence = evidence

    def state(self, session_id):
        status, body, elapsed = self.http(f"/api/sessions/{session_id}/state")
        if status != 200:
            self.evidence.save("http_state_rejection", {"status": status, "response": body, "seconds": elapsed})
        require(status == 200, "Native execution-state read failed: HTTP " + str(status))
        return body["data"], elapsed


class PublicAdmin:
    def __init__(self, settings, evidence, log):
        self.settings, self.config, self.evidence, self.log = settings, settings.installation(), evidence, log
        self.calls = []
        self.backup_store = None

    def call(self, operation, fields, *, name, expected_code=None, backup=False):
        fields = dict(fields)
        if backup:
            require(self.backup_store is not None and self.backup_store.is_file(), "Verified backup store is missing")
            fields["verified_backup_store"] = str(self.backup_store)
        started = time.monotonic()
        result = subprocess.run([self.config["runtime_python"], "-I", "-B", "-c", ADMIN_CODE,
            str(self.settings.installation_path), operation, json.dumps(fields, allow_nan=False)],
            cwd=self.config["runtime_root"], capture_output=True, text=True, encoding="utf-8", timeout=180,
            env={key: value for key, value in os.environ.items() if key.upper() not in ("PYTHONPATH", "PYTHONHOME")},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.log.write(json.dumps({"name": name, "operation": operation, "returncode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False) + "\n")
        self.log.flush()
        require(result.returncode == 0, "Isolated public SDK subprocess failed: " + name)
        response = json.loads(result.stdout)
        require(response["schema_version"] == "saferoute-m3-native-sdk-admin/1", "Native admin response schema differs")
        if expected_code is None:
            require(response["status"] == "OK", "Public SDK rejected " + name + ": " + str(response.get("code")))
        else:
            require(response["status"] == "FAIL" and response["code"] == expected_code,
                "Public SDK did not return the expected rejection: " + expected_code)
        self.calls.append({"name": name, "operation": operation, "seconds": round(time.monotonic() - started, 4),
            "backup_authority": backup, "status": response["status"]})
        self.evidence.save(name, response)
        return response.get("value")

    def state(self, session_id, name, *, backup=False):
        response = self.call("get_head", {"session_id": session_id, "command_id": "native-read-" + uuid4().hex},
            name=name, backup=backup)
        require(response["schema_version"] == "task02-m2-runtime-response/1" and response["status"] == "OK",
            "Public SDK current head command failed")
        return response["value"]


def raw_http(harness, path, *, actor="member3", timeout=180):
    headers = {}
    if actor:
        headers["Authorization"] = "Bearer " + harness.tokens[actor]
    try:
        response = urlopen(Request(harness.base_url + path, headers=headers), timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        return response.status, dict(response.headers.items()), response.read()


def start_distinct_worker(harness, previous_id, *, timeout=1800):
    from backend.services.heartbeat_io import read_heartbeat_json
    launched_at = datetime.now(timezone.utc)
    harness.worker = harness.spawn("start_worker.py", harness.worker_log)
    original_heartbeat = harness.heartbeat
    def guarded_heartbeat():
        try:
            heartbeat = read_heartbeat_json(harness.settings.heartbeat_path)
        except (OSError, ValueError, TypeError):
            heartbeat = {}
        if (heartbeat.get("worker_id") != previous_id and heartbeat.get("status") == "DEGRADED"
                and datetime.fromisoformat(heartbeat["updated_at"]) >= launched_at):
            raise RuntimeError("Fresh native worker degraded: " + str(heartbeat.get("last_error_code")))
        return original_heartbeat()
    harness.heartbeat = guarded_heartbeat
    try:
        ready, worker = fresh_worker_ready(harness, previous_id, launched_at=launched_at, timeout=timeout)
        worker["launched_at"] = launched_at.isoformat()
        worker["startup_elapsed_seconds"] = round((datetime.now(timezone.utc) - launched_at).total_seconds(), 4)
        return ready, worker
    except TimeoutError as error:
        raise TimeoutError("Fresh native worker readiness exceeded " + str(timeout) + " seconds") from error
    finally:
        harness.heartbeat = original_heartbeat


def previous_worker_id(settings):
    from backend.services.heartbeat_io import read_heartbeat_json
    try:
        return read_heartbeat_json(settings.heartbeat_path).get("worker_id")
    except (OSError, ValueError, TypeError):
        return None


def sdk_validation(value, build):
    require(value["validator_version"] == "task02-m2-bound-session-validator/2"
        and value["valid"] is True and value["historical_execution_builds_preserved"] is True
        and value["current_checker_build_sha256"] == build
        and value["scope"] == "TRUSTED_LOG_RAW_PREFIX_EVENT_SUFFIX; SIMULATED_REPLAY; NOT_OPTIMALITY",
        "Public independent whole-session validation evidence differs")


def notifications(harness, session_id, evidence, name, *, limit=500):
    """Read every public page; cursors are exact decimal strings on the wire."""
    cursor, rows, pages = "0", [], []
    while True:
        status, response, _ = harness.http(f"/api/sessions/{session_id}/notifications?after={cursor}&limit={limit}")
        require(status == 200 and response["status"] == "OK", "Notification HTTP projection failed")
        data = response["data"]
        require(data["schema_version"] == "saferoute-m3-notifications/1" and data["session_id"] == session_id
            and data["delivery_semantics"] == "DURABLE_DEDUP_BEFORE_SDK_ACK", "Notification public schema differs")
        batch = data["notifications"]
        require(isinstance(batch, list) and type(data["has_more"]) is bool and isinstance(data["next_cursor"], str)
            and data["next_cursor"].isdigit(), "Notification pagination types differ")
        for item in batch:
            require(item["session_id"] == session_id and isinstance(item["cursor"], str)
                and item["cursor"].isdigit() and int(item["cursor"]) > int(cursor)
                and item["status"] in ("PENDING_ACK", "ACKNOWLEDGED")
                and "command_id" not in item and "ack_receipt_json" not in item,
                "Notification projection leaked private metadata or invalid cursor")
        require(len({item["event_id"] for item in batch}) == len(batch), "Notification page duplicated event IDs")
        rows.extend(batch)
        pages.append(data)
        if not data["has_more"]:
            break
        require(int(data["next_cursor"]) > int(cursor), "Notification pagination made no progress")
        cursor = data["next_cursor"]
        require(len(pages) <= 1000, "Notification page count exceeded a bounded native read")
    require(len({item["event_id"] for item in rows}) == len(rows), "Durable notification history duplicated event IDs")
    evidence.save(name, {"pages": pages, "notifications": rows})
    return rows


def crash_boundary_audit(harness, admin, evidence, run_id, receipt):
    """Reopen committed M3 rows at two crash boundaries, using actual SDK ack."""
    from backend.services.outbox_repository import OutboxRepository
    from backend.services.worker_lock import WorkerLock
    stopped_worker_id = (harness.wait_idle(timeout=180)["worker_id"] if harness.worker is not None
        else previous_worker_id(harness.settings))
    harness.stop_worker()
    session_id = harness.load("S0", run_id + "-outbox-boundary-load")
    before = harness.state(session_id)[0]
    harness.stop_server()
    lock = WorkerLock(harness.settings.worker_lock_path)
    lock.acquire()
    installation_id = harness.settings.installation_identity()
    main = OutboxRepository(harness.settings.metadata_path)
    try:
        require(harness.worker is None and harness.server is None, "Administrative outbox audit requires stopped helpers")
        main.initialize()
        summaries = admin.call("read_notifications", {"session_id": session_id}, name="boundary_sdk_pending_initial")
        require(isinstance(summaries, list) and len(summaries) >= 2, "New native bootstrap did not expose real pending outbox rows")
        require(main.ingest(session_id, installation_id, summaries) == len(summaries),
            "Actual SDK summaries were not committed before any acknowledgement")
        pending_before = main.pending(session_id)
        # Discard repository objects: the next object reads only committed bytes.
        main = OutboxRepository(harness.settings.metadata_path)
        main.initialize()
        pending_reopened = main.pending(session_id)
        require(pending_reopened == pending_before and main.ingest(session_id, installation_id, summaries) == 0
            and len(main.list_notifications(session_id, limit=500)["notifications"]) == len(summaries),
            "Reopened M3 repository lost pending rows or duplicated redelivery")
        # A real SDK acknowledgement succeeds, but its M3 receipt is deliberately
        # not saved yet. Reopening then exercises retry of that exact stored key.
        first = pending_reopened[0]
        ack_fields = {"session_id": session_id, "command_id": first["command_id"], "event_id": first["event_id"]}
        first_receipt = admin.call("acknowledge", ack_fields, name="boundary_sdk_ack_before_m3_receipt")
        main = OutboxRepository(harness.settings.metadata_path)
        main.initialize()
        require(main.pending(session_id) == pending_reopened, "Uncommitted M3 ack receipt unexpectedly appeared after reopening")
        retry_receipt = admin.call("acknowledge", ack_fields, name="boundary_sdk_ack_exact_retry")
        require(retry_receipt == first_receipt, "Same persisted ack command did not return its exact receipt")
        main.complete_ack(session_id, first["event_id"], first["command_id"], retry_receipt)
        for index, row in enumerate(main.pending(session_id)):
            ack = admin.call("acknowledge", {"session_id": session_id, "command_id": row["command_id"],
                "event_id": row["event_id"]}, name=f"boundary_main_ack_{index}")
            main.complete_ack(session_id, row["event_id"], row["command_id"], ack)
        require(not main.pending(session_id), "Durable pending ack rows remain after completion")
        require(admin.call("read_notifications", {"session_id": session_id}, name="boundary_sdk_pending_after_ack") == [],
            "SDK acknowledgement generated another notification or left pending rows")
        different_event = pending_reopened[1]["event_id"]
        admin.call("acknowledge", {**ack_fields, "event_id": different_event}, name="boundary_changed_ack_payload",
            expected_code="IDEMPOTENCY_CONFLICT")
        after = admin.state(session_id, "boundary_state_after_ack")
        require(after == before, "Acknowledgement changed physical state or activation generation")
        validation = admin.call("validate_session", {"session_id": session_id}, name="boundary_public_validation")
        sdk_validation(validation, admin.config["expected_build_sha256"])
        evidence.save("boundary_committed_main_notifications", main.list_notifications(session_id, limit=500))
        receipt["checks"]["notification_crash_boundaries"] = {"status": "PASS", "session_id": session_id,
            "pending_count": len(summaries),
            "persist_before_ack_reopen_dedup": True, "sdk_ack_before_m3_receipt_exact_retry": True,
            "main_inbox_contains_same_summaries": True, "no_ack_generated_notifications": True,
            "full_execution_view_unchanged": True, "changed_ack_payload_rejected": True,
            "boundary_method": "DELIBERATE_RECEIPT_OMISSION_AND_REPOSITORY_REOPEN; NOT_PROCESS_FAULT_INJECTION"}
    finally:
        lock.release()
    return session_id, before, stopped_worker_id


def backup_metadata(source, target):
    require(source.is_file() and not target.exists(), "Metadata backup needs an existing source and new target")
    with sqlite3.connect("file:" + source.resolve().as_posix() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(target) as copied:
            original.backup(copied)
            require(copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Copied M3 metadata failed SQLite integrity check")


def coordinated_backup(harness, admin, evidence, states, run_id, receipt):
    from backend.services.worker_lock import WorkerLock
    heartbeat = harness.wait_idle(timeout=180)
    harness.stop_worker()
    harness.stop_server()
    lock = WorkerLock(harness.settings.worker_lock_path)
    lock.acquire()
    directory = harness.output.parent / ("private_backup_" + run_id)
    try:
        require(harness.worker is None and harness.server is None, "Private backup requires stopped API and worker")
        directory.mkdir(parents=True, exist_ok=False)
        for scenario in states:
            session_id = scenario["session_id"]
            recovery = admin.call("recover", {"session_id": session_id, "command_id": run_id + "-recover-" + scenario["scenario_id"]},
                name="backup_recover_" + scenario["scenario_id"])
            require(recovery["status"] == "OK" and recovery["value"]["status"] == "RECOVERED"
                and recovery["value"]["fenced_jobs"] == []
                and recovery["value"]["basis"] == scenario["final_state"]["basis"],
                "Coordinated recovery altered a completed head or fenced an unexpected active job")
            require(admin.state(session_id, "backup_original_state_" + scenario["scenario_id"]) == scenario["final_state"],
                "Public SDK recovery changed a prior accepted physical head")
            validation = admin.call("validate_session", {"session_id": session_id}, name="backup_original_validation_" + scenario["scenario_id"])
            sdk_validation(validation, admin.config["expected_build_sha256"])
        authority = directory / "backend_authority.sqlite"
        value = admin.call("backup", {"new_server_path": str(authority)}, name="private_global_sdk_backup")
        require(value == {"status": "BACKUP_VERIFIED", "schema_version": "task02-m2-runtime-store/2",
            "build_sha256": admin.config["expected_build_sha256"]}, "SDK private backup result differs")
        admin.backup_store = authority.resolve(strict=True)
        metadata = directory / "metadata.sqlite"
        backup_metadata(harness.settings.metadata_path, metadata)
        # Retain file digests before opening the copy through the public SDK.
        pinned = {"authority": digest_file(authority), "metadata": digest_file(metadata)}
        copied = []
        for scenario in states:
            session_id = scenario["session_id"]
            require(admin.state(session_id, "backup_copy_state_" + scenario["scenario_id"], backup=True) == scenario["final_state"],
                "Restored public SDK copy differs from the captured accepted physical head")
            validation = admin.call("validate_session", {"session_id": session_id}, name="backup_copy_validation_" + scenario["scenario_id"], backup=True)
            sdk_validation(validation, admin.config["expected_build_sha256"])
            copied.append({"scenario_id": scenario["scenario_id"], "session_id": session_id, "validation": validation})
        require(digest_file(authority) == pinned["authority"] and digest_file(metadata) == pinned["metadata"],
            "Read-only SDK validation modified the verified backup bytes")
        admin.call("backup", {"new_server_path": str(authority)}, name="private_backup_existing_target_rejected", expected_code="BACKUP_EXISTS")
        corrupt = directory / "corruption_probe.sqlite"
        original = authority.read_bytes()
        require(len(original) > 100 and original[:16] == b"SQLite format 3\x00", "Verified backup does not have a SQLite header")
        # This is a separate disposable corruption probe, never the authority
        # or verified backup. No SQL/private M2 store internals are accessed.
        corrupt.write_bytes(bytes([original[0] ^ 0xff]) + original[1:])
        corrupt_before = digest_file(corrupt)
        admin.backup_store = corrupt.resolve(strict=True)
        try:
            admin.call("validate_session", {"session_id": states[0]["session_id"]},
                name="corrupt_copy_public_sdk_rejection", expected_code="STORE_INVALID", backup=True)
        finally:
            admin.backup_store = authority.resolve(strict=True)
        require(digest_file(corrupt) == corrupt_before and digest_file(authority) == pinned["authority"],
            "Rejected SDK open changed corrupt evidence or the verified backup")
        receipt["checks"]["isolated_corrupt_copy"] = {"status": "PASS", "file": corrupt_before,
            "diagnostic": "STORE_INVALID", "original_and_verified_backup_unchanged": True,
            "corrupt_evidence_bytes_unchanged": True, "no_repair_or_recreation_attempted": True,
            "scope": "ISOLATED_BACKUP_COPY_HEADER_CORRUPTION; NOT_LIVE_AUTHORITY_FAULT_INJECTION"}
        backup_receipt = {"schema_version": "saferoute-m3-native-private-backup/1", "directory": str(directory.resolve()),
            "created_at": datetime.now(timezone.utc).isoformat(), "runtime_build_sha256": admin.config["expected_build_sha256"],
            "scope": "GLOBAL_M2_AUTHORITY_AND_M3_METADATA; ADMIN_PRIVATE_NOT_SESSION_DOWNLOAD",
            "coordination": "OWNED_API_AND_WORKER_STOPPED_WITH_EXCLUSIVE_WORKER_OS_LOCK", "sdk_result": value,
            "files": pinned, "verified_sessions": copied, "live_authority_not_replaced": True,
            "restore_scope": "PUBLIC_SDK_READ_AND_WHOLE_LINEAGE_VALIDATION_OF_COPY; NOT_LIVE_REPLACEMENT_DRILL"}
        evidence.save("private_backup_receipt", backup_receipt)
        receipt["checks"]["private_backup"] = {"status": "PASS", **backup_receipt}
    finally:
        lock.release()
    return heartbeat["worker_id"]


def no_credentials_or_server_paths(harness, text):
    for token in harness.tokens.values():
        require(token not in text, "Public response exposed a development credential")
    config = harness.settings.installation()
    private_paths = [str(harness.settings.auth_path), str(harness.settings.metadata_path),
        str(Path(config["authority_store_parent"]) / "backend_authority.sqlite"), config["runtime_python"],
        config["runtime_root"], config["snapshot_root"]]
    for private_path in private_paths:
        require(private_path not in text and json.dumps(private_path)[1:-1] not in text,
            "Public response exposed an absolute server-owned path")


def no_ephemeral_metadata(value):
    if isinstance(value, dict):
        require(not {"claim_token", "claim_expires", "auth_tokens", "token", "store_path", "runtime_root", "snapshot_root"} & set(value),
            "Public export exposed an ephemeral claim, credential or server installation path")
        for item in value.values():
            no_ephemeral_metadata(item)
    elif isinstance(value, list):
        for item in value:
            no_ephemeral_metadata(item)


def verify_artifact_content(harness, session_id, manifest, wrapper, expected_state):
    require(wrapper["schema_version"] == "saferoute-m3-artifact-content/1" and wrapper["session_id"] == session_id
        and wrapper["artifact_id"] == manifest["artifact_id"] and wrapper["encoding"] == "UTF-8"
        and isinstance(wrapper["content_utf8"], str), "Artifact content public wrapper differs")
    raw = wrapper["content_utf8"].encode("utf-8")
    require(len(raw) == wrapper["bytes"] and hashlib.sha256(raw).hexdigest() == wrapper["sha256"],
        "Saved artifact UTF-8 byte count or SHA-256 differs")
    bundle = json.loads(raw)
    require(bundle["schema_version"] == "saferoute-m3-artifact-bundle/1"
        and bundle["manifest"] == manifest and isinstance(bundle["files"], list), "Artifact bundle manifest binding differs")
    files = {item["name"]: item for item in bundle["files"]}
    require(len(files) == len(bundle["files"]) and len({item["name"] for item in manifest["files"]}) == len(manifest["files"])
        and set(files) == {item["name"] for item in manifest["files"]}, "Artifact file inventory differs")
    parsed = {}
    for entry in manifest["files"]:
        content = files[entry["name"]]["content_utf8"].encode("utf-8")
        require(entry["media_type"] == "application/json" and len(content) == entry["bytes"]
            and hashlib.sha256(content).hexdigest() == entry["sha256"], "Artifact member byte count or hash differs")
        parsed[entry["name"]] = json.loads(content)
    no_credentials_or_server_paths(harness, wrapper["content_utf8"])
    no_ephemeral_metadata(parsed)
    # File names are owned by the additive artifact schema, not by SDK internals.
    require(set(parsed) == {"session.json", "requests.json", "jobs.json", "acceptances.json", "events.json",
        "execution_frames.json", "accepted_trajectories.json", "notifications.json", "recovery.json", "validation.json", "provenance.json"},
        "Artifact schema did not export its complete eleven-document inventory")
    require(any(item["public_view"] == expected_state for item in parsed["execution_frames.json"]["frames"]),
        "Artifact did not preserve the exact current public execution frame")
    sdk_validation(parsed["validation.json"]["receipt"], harness.settings.installation()["expected_build_sha256"])
    require(parsed["validation.json"]["basis_before_and_after"] == expected_state["basis"]
        and parsed["accepted_trajectories.json"]["forecast"] is True
        and any(item["accepted_trajectory"] == expected_state["accepted_trajectory"]
            for item in parsed["accepted_trajectories.json"]["records"]), "Artifact current trajectory or independent validation binding differs")
    gaps = parsed["provenance.json"]["history_gaps"]
    require(isinstance(gaps["missing_exact_receipt_frame_ids"], list)
        and isinstance(gaps["missing_historical_accepted_job_ids"], list)
        and gaps["original_http_bodies_not_retained"] is True
        and gaps["no_private_authority_or_raw_job_records"] is True, "Artifact did not disclose its historical evidence gaps")
    return parsed


def verify_replay_audit(exported_receipts, history, session_id):
    message = "Artifact normalized replay audit differs from the complete committed native history"
    required = {"schema_version", "session_id", "mutation_id", "operation", "status", "recorded_at"}
    # Public replay audit contract: UI links and nested new-session metadata are
    # omitted; every originally present normalized lineage field is retained.
    normalized = required | {"source", "input_basis", "basis", "target_time", "event_id", "event_sha256",
                             "event_type", "new_session_id", "mode", "paused"}
    require(isinstance(exported_receipts, list) and isinstance(history, list)
        and len(exported_receipts) == len(history), message)

    def index(receipts):
        records = {}
        for receipt in receipts:
            require(isinstance(receipt, dict) and required <= set(receipt), message)
            require(receipt["schema_version"] == "saferoute-m3-replay-receipt/1"
                and receipt["session_id"] == session_id
                and all(isinstance(receipt[key], str) and receipt[key]
                    for key in ("mutation_id", "operation", "status", "recorded_at")), message)
            require(receipt["mutation_id"] not in records, message)
            records[receipt["mutation_id"]] = receipt
        return records

    exported, originals = index(exported_receipts), index(history)
    require(set(exported) == set(originals), message)
    for mutation_id, receipt in exported.items():
        original = originals[mutation_id]
        expected = {key: original[key] for key in normalized if key in original}
        require(set(receipt) == set(expected)
            and json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
                == json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False), message)


def artifact_case(harness, scenario, run_id, evidence):
    session_id = scenario["session_id"]
    prefix = f"/api/sessions/{session_id}/artifacts"
    body = {"request_id": run_id + "-export-" + scenario["scenario_id"]}
    status, response, seconds = harness.http(prefix, body, timeout=300)
    require(status == 201 and response["status"] == "OK", "Native artifact create did not return HTTP 201")
    manifest = response["data"]
    require(manifest["schema_version"] == "saferoute-m3-artifact-manifest/1" and manifest["session_id"] == session_id
        and isinstance(manifest["artifact_id"], str) and isinstance(manifest["files"], list)
        and manifest["status"] == "READY" and manifest["request_id"] == body["request_id"]
        and manifest["captured_basis"] == scenario["final_state"]["basis"]
        and manifest["runtime_build_sha256"] == harness.settings.installation()["expected_build_sha256"]
        and manifest["execution_mode"] == "SIMULATED_REPLAY" and manifest["real_world_observation"] is False,
        "Public artifact manifest differs")
    evidence.save(scenario["scenario_id"] + "_artifact_manifest", manifest)
    artifact_path = prefix + "/" + manifest["artifact_id"]
    status, response, _ = harness.http(artifact_path)
    require(status == 200 and response["data"] == manifest, "Saved artifact metadata differs from creation receipt")
    status, headers, raw = raw_http(harness, artifact_path + "/content")
    require(status == 200 and "no-store" in next((value for key, value in headers.items() if key.lower() == "cache-control"), ""),
        "Artifact content must be authenticated and no-store")
    response = json.loads(raw)
    wrapper = response["data"]
    parsed = verify_artifact_content(harness, session_id, manifest, wrapper, scenario["final_state"])
    public_jobs = [item["public_view"] for item in parsed["jobs.json"]["public_job_views"]]
    require(all(any(view == old["job_view"] for view in public_jobs) for old in scenario["solves"]),
        "Artifact did not preserve both verified native typed witnessed job views")
    event_audit = [item["receipt"] for item in parsed["events.json"]["replay_audit"]]
    verify_replay_audit(event_audit, scenario["replay_history"], session_id)
    require(parsed["provenance.json"]["fixture_sha256"] == scenario["fixture_sha256"], "Artifact fixture provenance differs")
    require(parsed["requests.json"]["full_session_history"] is True and parsed["acceptances.json"]["full_session_history"] is True,
        "Artifact truncated its committed request or acceptance audit")
    evidence.save(scenario["scenario_id"] + "_artifact_content", wrapper)
    status, retried, _ = harness.http(prefix, body, timeout=300)
    require(status == 201 and retried["data"] == manifest, "Artifact retry changed its immutable manifest or identity")
    status, listed, _ = harness.http(prefix)
    require(status == 200 and listed["data"]["schema_version"] == "saferoute-m3-artifact-list/1"
        and sum(item["artifact_id"] == manifest["artifact_id"] for item in listed["data"]["artifacts"]) == 1,
        "Artifact retry duplicated the session's artifact inventory")
    status, _, _ = harness.http(prefix, {**body, "new_server_path": "browser-must-not-select-a-path"})
    require(status == 422, "Artifact body accepted an unowned filesystem path")
    require_error(harness, prefix, {"request_id": run_id + "-forbidden-export"}, 403, "FORBIDDEN", actor="member4")
    require_error(harness, artifact_path, None, 403, "FORBIDDEN", actor="member4")
    require_error(harness, artifact_path + "/content", None, 403, "FORBIDDEN", actor="member4")
    require(harness.state(session_id)[0] == scenario["final_state"], "Artifact capture changed accepted physical state")
    return {"scenario_id": scenario["scenario_id"], "session_id": session_id, "status": "M3_STEP7_NATIVE_ARTIFACT_PASS",
        "path": prefix, "body": body, "manifest": manifest, "content_sha256": wrapper["sha256"],
        "content_bytes": wrapper["bytes"], "content_wrapper": wrapper, "file_names": sorted(parsed),
        "capture_seconds": round(seconds, 4), "immutable_request_retry": True, "owner_access_checked": True,
        "full_execution_view_unchanged": True, "history_gaps": parsed["provenance.json"]["history_gaps"],
        "limitations": manifest["limitations"], "source_validation": parsed["validation.json"]["receipt"]}


def verify_restart_artifacts(harness, exports, evidence):
    for exported in exports:
        session_id, artifact_id = exported["session_id"], exported["manifest"]["artifact_id"]
        status, retried, _ = harness.http(exported["path"], exported["body"], timeout=300)
        require(status == 201 and retried["data"] == exported["manifest"], "Restart lost stable artifact request receipt")
        status, response, _ = harness.http(f"/api/sessions/{session_id}/artifacts/{artifact_id}/content", timeout=180)
        require(status == 200 and response["data"] == exported["content_wrapper"], "Restart changed stored immutable artifact UTF-8 bytes")
        evidence.save(exported["scenario_id"] + "_artifact_content_after_restart", response["data"])
        exported["restart_same_manifest_and_content"] = True


def source_receipt(settings, explicit):
    if explicit is not None:
        return explicit.resolve(strict=True)
    candidates = []
    for directory in (settings.installation_path.parent / "receipts").glob("m3_step6*"):
        path = directory / "native_replay.json"
        try:
            value = json.loads(path.read_bytes())
            if value.get("status") == "M3_STEP6_NATIVE_REPLAY_PASS":
                candidates.append(path)
        except (OSError, ValueError):
            continue
    require(candidates, "No successful native M3-06 receipt is available; supply --source-receipt")
    return max(candidates, key=lambda path: path.stat().st_mtime).resolve(strict=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-receipt", type=Path,
        help="Successful M3-06 native replay receipt; defaults to the newest successful private receipt")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project))
    from backend.services.settings import Settings
    settings = Settings.from_environment()
    config = settings.installation()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run_id = "m3-step7-" + uuid4().hex
    receipt = {"schema_version": "saferoute-m3-step7-native-audit/1", "status": "RUNNING", "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(), "project_root": str(project.resolve()),
        "runtime_build_sha256": config["expected_build_sha256"], "execution_mode": "SIMULATED_REPLAY",
        "real_world_observation": False, "new_solver_calls": 0, "checks": {}, "exports": [], "scenarios": [],
        "notes": ["Existing accepted S2/S3/S4 native replay heads are reused only after receipt ancestry, all frame bytes/hashes, fixtures and live authority agree.",
            "No new solve, synthetic solver outcome, private M2 SQL or copied planner state is used.",
            "Crash boundaries omit the M3 ack receipt deliberately and reopen committed repositories; this is not process fault injection.",
            "The private backup is global and contains other authority sessions. It is never served as an owner artifact.",
            "Backup recovery verifies an isolated public SDK copy and M3 metadata; it does not replace the live authority.",
            "Historical export gaps and proxy/WHAT-IF limits remain as declared by the exported manifest.",
            "Observed operation timings are evidence for this run, not an SLA."]}
    evidence = Evidence(args.output)
    harness = AuditedHarness(project, settings, args.output, evidence)
    admin_log = args.output.with_suffix(".admin.log").open("w", encoding="utf-8")
    admin = PublicAdmin(settings, evidence, admin_log)
    failed = None
    scenarios, exports = [], []
    try:
        source = source_receipt(settings, args.source_receipt)
        previous = json.loads(source.read_bytes())
        require(previous["status"] == "M3_STEP6_NATIVE_REPLAY_PASS", "Audit source must be an overall successful native replay receipt")
        prior_budget = previous["compute_budget_seconds"]
        receipt["prior_native_compute_budget_seconds"] = prior_budget
        harness.start_server()
        audit_session, audit_initial, old_worker_id = crash_boundary_audit(harness, admin, evidence, run_id, receipt)
        harness.start_server()
        ready, worker = start_distinct_worker(harness, old_worker_id)
        receipt["checks"]["initial_readiness"] = {"status": "PASS", "http_status": 200, "data": ready,
            "worker_evidence": worker, "startup_wait_bound_seconds": 1800}
        status, catalog, _ = harness.http("/api/scenarios")
        require(status == 200, "Verified HTTP catalog unavailable")
        scenarios, provenance = resume_verified_scenarios(source, harness, catalog["data"], config, prior_budget, ["S2", "S3", "S4"])
        require({item["scenario_id"] for item in scenarios} == {"S2", "S3", "S4"}, "Audit source does not cover all required native replay scenarios")
        receipt["prior_native_evidence"] = provenance
        baseline_notifications = {}
        for scenario in scenarios:
            name, sid = scenario["scenario_id"], scenario["session_id"]
            evidence.save(name + "_current_execution", scenario["final_state"])
            rows = notifications(harness, sid, evidence, name + "_notifications")
            require(rows and all(item["status"] == "ACKNOWLEDGED" for item in rows),
                "Real worker startup did not persist and acknowledge existing native replay outbox")
            require(notifications(harness, sid, evidence, name + "_notifications_paginated", limit=3) == rows,
                "Cursor pagination lost, reordered or duplicated durable notifications")
            baseline_notifications[sid] = rows
            require_error(harness, f"/api/sessions/{sid}/notifications", None, 403, "FORBIDDEN", actor="member4")
            exported = artifact_case(harness, scenario, run_id, evidence)
            exports.append(exported)
            receipt["exports"].append({key: value for key, value in exported.items() if key != "content_wrapper"})
            receipt["scenarios"].append({"scenario_id": name, "session_id": sid,
                "source_fixture_sha256": scenario["fixture_sha256"], "source_final_basis": scenario["final_state"]["basis"],
                "physical_head_verified_again": True, "no_new_solve": True, "notification_count": len(rows)})
            print(name + " NATIVE_NOTIFICATIONS_AND_IMMUTABLE_ARTIFACT_PASS", flush=True)
        require(harness.state(audit_session)[0] == audit_initial, "Real worker startup changed the notification boundary session head")
        audit_rows = notifications(harness, audit_session, evidence, "boundary_notifications_http")
        require(len(audit_rows) == receipt["checks"]["notification_crash_boundaries"]["pending_count"]
            and all(item["status"] == "ACKNOWLEDGED" for item in audit_rows),
            "Worker startup lost or duplicated manually persisted native ack evidence")
        baseline_notifications[audit_session] = audit_rows
        receipt["checks"]["worker_outbox_and_export"] = {"status": "PASS", "reused_scenarios": ["S2", "S3", "S4"],
            "all_durable_rows_acknowledged": True, "cursor_pagination_exact": True, "no_duplicate_event_ids": True,
            "owner_access_checked": True, "sdk_backed_artifact_count": len(exports)}
        all_states = scenarios + [{"scenario_id": "OUTBOX_AUDIT", "session_id": audit_session, "final_state": audit_initial}]
        old_worker_id = coordinated_backup(harness, admin, evidence, all_states, run_id, receipt)
        # Public M3 metadata repository verifies the copied immutable artifacts;
        # this accesses no physical authority table.
        from backend.services.artifact_repository import ArtifactRepository
        copied_metadata = Path(receipt["checks"]["private_backup"]["files"]["metadata"]["path"])
        copied_artifacts = ArtifactRepository(copied_metadata)
        for exported in exports:
            saved = copied_artifacts.get(exported["session_id"], exported["manifest"]["artifact_id"])
            require(saved["manifest"] == exported["manifest"] and saved["bundle_sha256"] == exported["content_sha256"]
                and saved["bytes"] == exported["content_bytes"], "Copied M3 metadata lost exact artifact manifest or bundle bytes")
        require(digest_file(copied_metadata) == receipt["checks"]["private_backup"]["files"]["metadata"],
            "Reading copied M3 metadata changed the pinned backup bytes")
        receipt["checks"]["private_backup"]["copied_m3_artifacts_verified"] = len(exports)
        harness.start_server()
        ready, worker = start_distinct_worker(harness, old_worker_id)
        receipt["checks"]["restart_readiness"] = {"status": "PASS", "http_status": 200, "data": ready,
            "worker_evidence": worker, "startup_wait_bound_seconds": 1800}
        for scenario in all_states:
            name, sid = scenario["scenario_id"], scenario["session_id"]
            require(harness.state(sid)[0] == scenario["final_state"], "Restart/recovery changed a captured physical head")
            require(notifications(harness, sid, evidence, name + "_notifications_after_restart") == baseline_notifications[sid],
                "Restart duplicated, lost or changed committed notification rows")
            if "restart_retry" in scenario:
                retry = scenario["restart_retry"]
                status, response, _ = harness.http(retry["path"], retry["body"], timeout=180)
                require(status == 200 and response["data"]["receipt"] == retry["receipt"]
                    and response["data"]["execution_view"] == scenario["final_state"], "Restart lost the historical event receipt or current frame")
                status, history, _ = harness.http(f"/api/sessions/{sid}/replay/history")
                require(status == 200 and history["data"]["history"] == scenario["replay_history"], "Restart changed committed native replay history")
        verify_restart_artifacts(harness, exports, evidence)
        for exported, recorded in zip(exports, receipt["exports"]):
            recorded["restart_same_manifest_and_content"] = exported["restart_same_manifest_and_content"]
        receipt["checks"]["restart_persistence"] = {"status": "PASS", "exact_heads_preserved": len(all_states),
            "historical_replay_receipts_and_history_preserved": True, "notification_rows_exact_and_deduped": True,
            "immutable_artifact_bytes_preserved": True}
        status, openapi, _ = harness.http("/openapi.json", actor=None)
        required_routes = ("/api/sessions/{session_id}/notifications", "/api/sessions/{session_id}/artifacts",
            "/api/sessions/{session_id}/artifacts/{artifact_id}", "/api/sessions/{session_id}/artifacts/{artifact_id}/content")
        require(status == 200 and all(path in openapi["paths"] for path in required_routes), "Native OpenAPI lacks outbox/artifact routes")
        require(not any("backup" in path or "recover" in path for path in openapi["paths"]), "Administrative recovery or backup was exposed over HTTP")
        openapi_path = args.output.parent / "openapi.json"
        openapi_path.write_text(json.dumps(openapi, ensure_ascii=False, indent=2), encoding="utf-8")
        receipt["checks"]["openapi"] = {"status": "PASS", **digest_file(openapi_path), "admin_operations_not_http_routes": True}
    except Exception as error:
        failed = error
        receipt["failure"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        harness.close()
        admin_log.close()
        receipt["server_running_after_test"] = False
        receipt["worker_running_after_test"] = False
        receipt["frames"] = evidence.frames
        receipt["native_sdk_calls"] = admin.calls
    try:
        check = subprocess.run([sys.executable, "-B", str(project / "docs/M2_STEP7_VERIFY_20261004.py"),
            "--project-root", str(project)], cwd=project, capture_output=True, text=True, encoding="utf-8", timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        verified = json.loads(check.stdout)
        require(check.returncode == 0 and verified["status"] == "TEAM_HANDOFF_BYTES_VERIFIED", "Frozen M2 handoff changed")
        receipt["checks"]["frozen_handoff"] = {"status": "PASS", "result": verified}
    except Exception as error:
        failed = failed or error
        receipt["checks"]["frozen_handoff"] = {"status": "FAIL", "type": type(error).__name__, "message": str(error)}
    receipt["status"] = "M3_STEP7_NATIVE_AUDIT_PASS" if failed is None else "M3_STEP7_NATIVE_AUDIT_FAIL"
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"status": receipt["status"], "receipt": str(args.output), "native_artifact_count": len(exports),
        "new_solver_calls": 0, "server_stopped": True, "worker_stopped": True}), flush=True)
    return 0 if failed is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
