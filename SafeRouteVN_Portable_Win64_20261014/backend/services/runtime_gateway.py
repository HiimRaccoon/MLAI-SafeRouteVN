import asyncio
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sqlite3
from time import monotonic
from uuid import uuid4

from backend.api.errors import ApiError
from .settings import Settings
from .sdk_access_lock import SdkAccessBusy, SdkAccessLock

logger = logging.getLogger("saferoute.runtime")


class RuntimeGateway:
    supports_profile_batch = True
    def __init__(self, settings: Settings):
        self.settings = settings
        self._reads = {}
        from .persistent_bridge import PersistentBridge
        self._bridge = PersistentBridge() if settings.runtime_persistent_bridge else None

    def close(self):
        if self._bridge is not None:self._bridge.close()

    async def _read(self, operation, fields):
        # Share only concurrent reads. Never cache a completed state or mutation.
        key = (operation, tuple(sorted((k, v) for k, v in fields.items() if k != 'command_id')))
        task = self._reads.get(key)
        if task is None or task.done():
            task = asyncio.create_task(asyncio.to_thread(self._call, operation, fields))
            self._reads[key] = task
            def settled(done):
                if self._reads.get(key) is done:
                    self._reads.pop(key, None)
                if not done.cancelled():
                    done.exception()  # Retrieve failures even if all HTTP callers disconnected.
            task.add_done_callback(settled)
        return deepcopy(await asyncio.shield(task))

    async def capabilities(self):
        value = await self._read("capabilities", {})
        self.supports_profile_batch = 'compute_profiles' in value.get('server_extensions', [])
        return value

    async def bootstrap(self, session_id, scenario_id, command_id):
        return await asyncio.to_thread(self._call, "bootstrap", {
            "session_id": session_id, "scenario_id": scenario_id, "command_id": command_id})

    async def bootstrap_run(self, session_id, command_id, run_id, manifest_sha256):
        return await asyncio.to_thread(self._call, 'bootstrap_run', {
            'session_id':session_id,'command_id':command_id,'run_id':run_id,'manifest_sha256':manifest_sha256})

    async def resolve(self, session_id):
        return await self._read("resolve", {
            "session_id": session_id, "command_id": "m3-read-" + str(uuid4())})

    async def submit(self, session_id, command_id, basis, profile, budget_seconds):
        return await asyncio.to_thread(self._call, "submit", {"session_id": session_id, "command_id": command_id,
            "basis": basis, "profile": profile, "budget_seconds": budget_seconds})

    async def job_view(self, session_id, job_id):
        return await self._read("job_view", {"session_id": session_id, "job_id": job_id, "command_id": "m3-poll-" + uuid4().hex})

    async def job_forecast(self, session_id, job_id):
        return await self._read("job_forecast", {"session_id": session_id, "job_id": job_id, "command_id": "m3-forecast-" + uuid4().hex})

    async def compare_profiles(self, session_id, job_ids):
        return await asyncio.to_thread(self._call, "compare_profiles", {"session_id": session_id,
            "job_ids": job_ids, "command_id": "m3-compare-" + uuid4().hex})

    async def cancel(self, session_id, job_id, command_id):
        return await asyncio.to_thread(self._call, "cancel", {"session_id": session_id, "job_id": job_id, "command_id": command_id})

    async def compute(self, session_id, job_id, command_id, budget_seconds):
        return await asyncio.to_thread(self._call, "compute", {"session_id": session_id, "job_id": job_id, "command_id": command_id}, budget_seconds + 45)

    async def compute_batch(self, session_id, job_ids, command_id, budget_seconds):
        return await asyncio.to_thread(self._call, "compute_batch", {
            "session_id": session_id, "job_ids": job_ids, "command_id": command_id}, budget_seconds + 45)

    async def recover(self, session_id, command_id):
        return await asyncio.to_thread(self._call, "recover", {"session_id": session_id, "command_id": command_id})

    async def accept(self, session_id, job_id, command_id, basis):
        return await asyncio.to_thread(self._call, "accept", {"session_id": session_id, "job_id": job_id,
            "command_id": command_id, "basis": basis})

    async def advance(self, session_id, command_id, basis, target_time):
        return await asyncio.to_thread(self._call, "advance", {"session_id": session_id, "command_id": command_id,
            "basis": basis, "target_time": target_time})

    async def apply_event(self, session_id, command_id, basis, event_id):
        return await asyncio.to_thread(self._call, "apply_event", {"session_id": session_id, "command_id": command_id,
            "basis": basis, "event_id": event_id})

    async def read_notifications(self, session_id):
        return await asyncio.to_thread(self._call, "read_notifications", {"session_id": session_id, "command_id": "m3-notify-" + uuid4().hex})

    async def acknowledge(self, session_id, command_id, event_id):
        return await asyncio.to_thread(self._call, "acknowledge", {"session_id": session_id, "command_id": command_id, "event_id": event_id})

    async def validate_session(self, session_id):
        return await asyncio.to_thread(self._call, "validate_session", {"session_id": session_id, "command_id": "m3-validate-" + uuid4().hex})

    async def backup(self, new_server_path):
        return await asyncio.to_thread(self._call, "backup", {"new_server_path": str(new_server_path), "command_id": "m3-backup-" + uuid4().hex})

    async def inspect_sessions(self, session_ids, command_id):
        # The verified bridge opens its expensive global authority once for
        # two sessions. Every row still runs public recovery, raw validation,
        # resolve and notification verification. Time is bounded per session.
        values = []
        for offset in range(0, len(session_ids), 2):
            batch = session_ids[offset:offset + 2]
            values.extend(await asyncio.to_thread(self._call, "inspect_sessions",
                {"session_ids": batch, "command_id": command_id},
                operation_timeout=self.settings.runtime_replay_timeout_seconds * len(batch)))
        return values

    async def read_notifications_batch(self, session_ids):
        return await asyncio.to_thread(self._call, "read_notifications_batch", {"session_ids": session_ids, "command_id": "m3-notify-batch-" + uuid4().hex})

    async def acknowledge_batch(self, rows):
        return await asyncio.to_thread(self._call, "acknowledge_batch", {"rows": [{k: row[k] for k in ("session_id", "command_id", "event_id")} for row in rows], "command_id": "m3-ack-batch-" + uuid4().hex})

    @staticmethod
    def check_comparison(value, session_id, job_ids, build):
        from .artifact_repository import validate_basis, canonical_bytes
        if (not isinstance(value, dict) or set(value) != {"schema_version", "status", "reason", "jobs"}
                or value["schema_version"] != "task02-m2-runtime-comparison/1"
                or value["status"] not in ("COMPARABLE", "NON_COMPARABLE")
                or value["reason"] != (None if value["status"] == "COMPARABLE" else "AUTHENTICATED_BASIS_OR_PHYSICAL_DOMAIN_DIFFERS")
                or not isinstance(value["jobs"], list) or len(value["jobs"]) != 3 or len(job_ids) != 3
                or [j["job_id"] for j in value["jobs"]] != job_ids or len(set(job_ids)) != 3):
            raise ValueError("Exact public SDK comparison required")
        profiles = []
        for row in value["jobs"]:
            if (not isinstance(row, dict) or set(row) != {"job_id", "profile", "basis", "domain_sha256", "metrics"}
                    or not isinstance(row["domain_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", row["domain_sha256"])
                    or not isinstance(row["metrics"], dict)):
                raise ValueError("Exact public comparison child required")
            validate_basis(row["basis"], session_id, build)
            profiles.append(row["profile"])
        if set(profiles) != {"FASTEST", "BALANCED", "SAFER"}:
            raise ValueError("Three distinct profiles required")
        canonical_bytes(value)

    @staticmethod
    def check_validation(value, build):
        if (set(value) != {"validator_version", "valid", "checked_physical_mutations", "historical_execution_builds_preserved", "current_checker_build_sha256", "scope"}
                or value["validator_version"] != "task02-m2-bound-session-validator/2" or value["valid"] is not True
                or value["historical_execution_builds_preserved"] is not True or value["current_checker_build_sha256"] != build
                or type(value["checked_physical_mutations"]) is not int or value["checked_physical_mutations"] < 0
                or value["scope"] != "TRUSTED_LOG_RAW_PREFIX_EVENT_SUFFIX; SIMULATED_REPLAY; NOT_OPTIMALITY"):
            raise ValueError("Invalid public session validation")

    @staticmethod
    def check_inspection(row, build):
        from .artifact_repository import validate_execution, validate_basis
        from .outbox_repository import OutboxRepository
        if set(row) != {"session_id", "recovery", "validation", "execution_view", "notifications"}:
            raise ValueError("Exact public recovery inspection required")
        RuntimeGateway.check_validation(row["validation"], build)
        validate_execution(row["execution_view"], row["session_id"], build)
        recovery = row["recovery"]
        if set(recovery) != {"status", "fenced_jobs", "basis"} or recovery["status"] != "RECOVERED":
            raise ValueError("Exact public recovery receipt required")
        validate_basis(recovery["basis"], row["session_id"], build)
        if recovery["basis"] != row["execution_view"]["basis"] or not isinstance(recovery["fenced_jobs"], list):
            raise ValueError("Recovery and current head differ")
        if any(not isinstance(jid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", jid) for jid in recovery["fenced_jobs"]):
            raise ValueError("Invalid fenced job identifier")
        if not isinstance(row["notifications"], list):
            raise ValueError("Notification array required")
        for item in row["notifications"]:
            OutboxRepository.validate(row["session_id"], item)

    def _call(self, operation, fields, compute_timeout=None, operation_timeout=None):
        started = monotonic()
        access = None
        lock_wait_seconds = 0.0
        try:
            config = self.settings.installation()
            store = Path(config["authority_store_parent"]) / "backend_authority.sqlite"
            if not store.is_file() and self.settings.metadata_path.is_file():
                with sqlite3.connect(self.settings.metadata_path) as db:
                    if db.execute("SELECT 1 FROM sessions WHERE status='READY' LIMIT 1").fetchone():
                        raise ApiError(503, "STORE_MISSING", "runtime", "Existing session authority is missing; restore requires a fenced admin procedure")
            env = {key: value for key, value in os.environ.items() if key.upper() not in ("PYTHONPATH", "PYTHONHOME")}
            command = [config["runtime_python"], "-I", "-B", str(Path(__file__).with_name("runtime_bridge.py")),
                       "--installation", str(self.settings.installation_path)]
            if operation != "capabilities":
                command.extend(["--operation", operation])
            timeout = self.settings.runtime_bootstrap_timeout_seconds if operation in ("bootstrap", "bootstrap_run") else self.settings.runtime_timeout_seconds
            if operation == "capabilities":
                timeout = self.settings.runtime_capabilities_timeout_seconds
            elif operation in ("compute", "compute_batch"):
                timeout = compute_timeout
            elif operation == "accept":
                timeout = self.settings.runtime_accept_timeout_seconds
            elif operation in ("advance", "apply_event"):
                timeout = self.settings.runtime_replay_timeout_seconds
            elif operation in ("validate_session", "backup", "inspect_sessions", "read_notifications_batch", "acknowledge_batch", "read_notifications", "acknowledge", "compare_profiles"):
                timeout = self.settings.runtime_replay_timeout_seconds
            if operation_timeout is not None:
                if (operation != "inspect_sessions" or not isinstance(fields.get("session_ids"), list)
                        or not 1 <= len(fields["session_ids"]) <= 2
                        or operation_timeout != self.settings.runtime_replay_timeout_seconds * len(fields["session_ids"])):
                    raise ValueError("Bounded inspection timeout required")
                timeout = operation_timeout
            if operation not in ("compute", "compute_batch"):
                # RuntimeClient opens/validates the global Store under SQLite
                # write locks, even for reads. Serialize M3 bridge processes.
                # Compute remains concurrent so public poll/cancel can fence it.
                deadline = monotonic() + timeout
                access = SdkAccessLock(store)
                access.acquire(deadline)
                original_timeout = timeout
                timeout = deadline - monotonic()
                lock_wait_seconds = max(0.0, original_timeout - timeout)
                if timeout <= 0:
                    raise SdkAccessBusy("SDK access deadline expired")
            if self._bridge is not None and operation not in ('compute','compute_batch'):
                serve_command=command[:command.index('--operation')] if '--operation' in command else command
                result=self._bridge.request([*serve_command,'--serve'],operation,fields,cwd=config['runtime_root'],env=env,
                                            timeout=timeout,binding=self.settings.installation_identity())
            else:
                result = subprocess.run(command, cwd=config["runtime_root"], env=env, capture_output=True,
                                        input=json.dumps(fields, allow_nan=False) if fields else None,
                                        text=True, encoding="utf-8", timeout=timeout,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != 0:
                raise ValueError("Runtime verification failed")
            response = json.loads(result.stdout)
            if response["schema_version"] != "task02-m2-runtime-response/1":
                raise ValueError("Runtime response schema")
            if response["status"] == "FAIL":
                code = response.get("diagnostics", [{}])[0].get("code", "RUNTIME_REJECTED")
                if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
                    code = "RUNTIME_REJECTED"
                if code.startswith(("STORE", "JOURNAL", "RECEIPT", "OUTBOX", "BUILD", "SOURCE", "ENVIRONMENT")):
                    from .recovery_repository import RecoveryRepository
                    recovery = RecoveryRepository(self.settings.metadata_path)
                    recovery.initialize()
                    for sid in ([fields["session_id"]] if "session_id" in fields else fields.get("session_ids", [])):
                        recovery.record(sid, "BLOCKED", "sdk-gateway", self.settings.installation_identity(), diagnostic=code)
                        recovery.audit(sid, "sdk-gateway", fields.get("command_id"), operation, diagnostic=code)
                status = 409 if code in ("STALE_HEAD", "JOB_STALE", "JOB_LIFECYCLE", "IDEMPOTENCY_CONFLICT", "WITNESS_REQUIRED", "WITNESS_INVALID",
                    "TIME_REWIND", "EVENT_TRANSITION_REQUIRED", "EVENT_NOT_DUE", "EVENT_ALREADY_APPLIED", "EVENT_UNKNOWN", "EVENT_TYPE_UNSUPPORTED",
                    "ACCEPTED_PLAN_REQUIRED", "ACCEPTED_REPLAY_REQUIRED", "PLAN_NOT_ACTIVE",
                    "DELIVERY_CONFIRMATION_REQUIRED", "DELIVERY_NOT_READY", "CUSTODY_OWNER", "DAY_NOT_COMPLETE",
                    "DAY_ALREADY_STARTED", "DAY_ALREADY_MOVING", "DELIVERY_DAY_REQUIRED", "DAY_BOUNDARY",
                    "ORDER_EXISTS", "ORDER_ID", "ORDER_TEMPLATE", "CUSTOMER_REQUIRED", "DEPOT_ID", "DEPOT_EXISTS",
                    "VEHICLE_UNAVAILABLE", "NO_RECOVERY_PENDING", "PAUSE_PLAN_REQUIRED", "INVALID_DATA") else 503
                raise ApiError(status, code, "runtime", "Verified runtime rejected the server operation")
            if response["status"] != "OK":
                raise ValueError("Unknown runtime status")
            value = response["value"]
            if operation == "compute_batch":
                from .artifact_repository import validate_job
                if not isinstance(value,list) or [v['job_id'] for v in value] != fields['job_ids']:
                    raise ValueError('Batch job binding')
                for view in value:
                    validate_job(view, fields['session_id'], view['job_id'], config['expected_build_sha256'])
                return value
            if operation == "compare_profiles":
                self.check_comparison(value, fields["session_id"], fields["job_ids"], config["expected_build_sha256"])
                return value
            if operation in ("read_notifications", "acknowledge", "validate_session", "backup", "inspect_sessions", "read_notifications_batch", "acknowledge_batch"):
                from .outbox_repository import OutboxRepository
                if operation in ("read_notifications", "read_notifications_batch"):
                    groups = {fields["session_id"]: value} if operation == "read_notifications" else value
                    expected = {fields["session_id"]} if operation == "read_notifications" else set(fields["session_ids"])
                    if set(groups) != expected:
                        raise ValueError("Notification session binding")
                    for sid, items in groups.items():
                        if not isinstance(items, list):
                            raise ValueError("Notification list")
                        for item in items:
                            OutboxRepository.validate(sid, item)
                elif operation == "acknowledge":
                    if value != {"status": "ACKNOWLEDGED", "event_id": fields["event_id"]}:
                        raise ValueError("Acknowledgement receipt")
                elif operation == "acknowledge_batch":
                    if value != [{"status": "ACKNOWLEDGED", "event_id": r["event_id"]} for r in fields["rows"]]:
                        raise ValueError("Acknowledgement batch")
                elif operation == "validate_session":
                    self.check_validation(value, config["expected_build_sha256"])
                elif operation == "backup":
                    if value != {"status": "BACKUP_VERIFIED", "schema_version": "task02-m2-runtime-store/2", "build_sha256": config["expected_build_sha256"]}:
                        raise ValueError("Backup receipt")
                elif operation == "inspect_sessions":
                    if not isinstance(value, list) or [v["session_id"] for v in value] != fields["session_ids"]:
                        raise ValueError("Recovery session binding")
                    for row in value:
                        if "diagnostic_code" in row:
                            if set(row) != {"session_id", "diagnostic_code"} or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", row["diagnostic_code"]):
                                raise ValueError("Recovery diagnostic")
                            continue
                        self.check_inspection(row, config["expected_build_sha256"])
                return value
            if operation == "capabilities":
                if (value["schema_version"] != "task02-m2-runtime-capabilities/1"
                    or value["build_sha256"] != config["expected_build_sha256"]
                    or value["execution_mode"] != "SIMULATED_REPLAY" or value["real_world_observation"] is not False
                    or value["online_target_is_sla"] is not False):
                    raise ValueError("Unverified capability projection")
            else:
                if operation == "cancel":
                    if value["job_id"] != fields["job_id"] or value["status"] not in ("JOB_CANCELLED", "COMPLETED_IMMUTABLE"):
                        raise ValueError("Cancellation receipt")
                    return value
                binding = value["input_basis"] if operation in ("submit", "compute", "job_view", "job_forecast") else value["basis"]
                if (binding["session_id"] != fields["session_id"]
                        or binding["build_sha256"] != config["expected_build_sha256"]):
                    raise ValueError("Session/build binding")
                if operation == "job_forecast":
                    if (value['schema_version'] != 'task02-m2-job-forecast/1' or value['job_id'] != fields['job_id']
                        or value['session_id'] != fields['session_id'] or value['build_sha256'] != config['expected_build_sha256']
                        or value['job_view']['input_basis'] != binding or value['job_view']['job_id'] != fields['job_id']
                        or value['metric_scope'] != 'FORECAST_ONLY' or value['real_world_observation'] is not False):
                        raise ValueError('Forecast binding')
                elif operation in ("bootstrap", "bootstrap_run"):
                    if value["status"] != "BOOTSTRAPPED":
                        raise ValueError("Bootstrap projection")
                elif operation == "submit":
                    if value["status"] != "QUEUED" or not isinstance(value["job_id"], str):
                        raise ValueError("Submission receipt")
                elif operation in ("compute", "job_view"):
                    if value["schema_version"] != "task02-m2-runtime-job-view/1" or value["job_id"] != fields["job_id"]:
                        raise ValueError("Typed job view")
                elif operation == "recover":
                    if value["status"] != "RECOVERED":
                        raise ValueError("Recovery receipt")
                elif operation == "accept":
                    if value["status"] != "ACCEPTED" or value["job_id"] != fields["job_id"]:
                        raise ValueError("Acceptance receipt")
                elif operation == "advance":
                    if value["status"] not in ("ADVANCED", "NOOP"):
                        raise ValueError("Advance receipt")
                elif operation == "apply_event":
                    if value["status"] != "APPLIED" or value["event_id"] != fields["event_id"]:
                        raise ValueError("Event receipt")
                elif operation == "dispatch":
                    if value['status'] != 'DISPATCHED':raise ValueError('Dispatch receipt')
                elif (value["schema_version"] != "task02-m2-execution-view/2"
                        or value["execution_mode"] != "SIMULATED_REPLAY" or value["real_world_observation"] is not False):
                    raise ValueError("Execution view projection")
            return value
        except SdkAccessBusy as error:
            logger.warning("SDK access busy operation=%s deadline_seconds=%.3f", operation, timeout)
            raise ApiError(503, "RUNTIME_BUSY", "runtime", "Runtime access is busy; retry with the same request ID") from error
        except subprocess.TimeoutExpired as error:
            logger.warning("SDK timeout operation=%s access_wait_seconds=%.3f subprocess_timeout_seconds=%.3f",
                operation, lock_wait_seconds, timeout)
            raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "Runtime operation timed out; retry with the same request ID") from error
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError) as error:
            raise ApiError(503, "RUNTIME_UNAVAILABLE", "runtime", "Verified runtime is unavailable") from error
        finally:
            if access is not None:
                access.release()
            logger.info("SDK timing operation=%s total_seconds=%.3f access_wait_seconds=%.3f",
                        operation, monotonic() - started, lock_wait_seconds)
