import asyncio
from datetime import datetime, timezone
import json
import logging
import sqlite3
import threading
import time
from uuid import uuid4

from backend.api.errors import ApiError
from .job_repository import JobRepository
from .job_service import JobService
from .heartbeat_io import replace_heartbeat_file
from .plan_repository import PlanRepository
from .plan_service import PlanService
from .replay_repository import ReplayRepository
from .replay_service import ReplayService
from .runtime_gateway import RuntimeGateway
from .scenario_catalog import ScenarioCatalog
from .session_repository import SessionRepository
from .session_service import SessionService
from .worker_lock import WorkerLock
from .outbox_repository import OutboxRepository
from .outbox_service import OutboxService
from .recovery_repository import RecoveryRepository
from .artifact_repository import ArtifactRepository
from .profile_repository import ProfileRepository
from .profile_service import ProfileService
from .playback_repository import PlaybackRepository
from .playback_service import PlaybackService

logger = logging.getLogger("saferoute.worker")
TERMINAL = ("COMPLETED", "FAILED")


class ComputeWorker:
    def __init__(self, settings, *, gateway=None, catalog=None):
        self.settings, self.worker_id = settings, "worker-" + uuid4().hex
        self.gateway = gateway or RuntimeGateway(settings)
        self.catalog = catalog or ScenarioCatalog(settings)
        self.sessions = SessionRepository(settings.metadata_path)
        self.jobs = JobRepository(settings.metadata_path)
        self.session_service = SessionService(settings, self.sessions, self.catalog, self.gateway)
        self.job_service = JobService(settings, self.jobs, self.session_service, self.gateway)
        self.plans = PlanRepository(settings.metadata_path)
        self.plan_service = PlanService(settings, self.plans, self.session_service, self.job_service, self.gateway)
        self.replays = ReplayRepository(settings.metadata_path)
        self.replay_service = ReplayService(settings, self.replays, self.session_service, self.gateway)
        self.outbox = OutboxRepository(settings.metadata_path)
        self.outbox_service = OutboxService(settings, self.outbox, self.gateway, self.session_service)
        self.recovery = RecoveryRepository(settings.metadata_path)
        self.evidence = ArtifactRepository(settings.metadata_path)
        self.profiles = ProfileRepository(settings.metadata_path)
        self.profile_service = ProfileService(settings, self.profiles, self.session_service, self.job_service, self.gateway)
        self.playback = PlaybackRepository(settings.metadata_path)
        self.playback_service = PlaybackService(settings, self.playback, self.session_service, self.replay_service, self.gateway)
        self.replay_service.playback = self.playback_service
        self.next_outbox_poll = 0
        self.outbox_poll_order = []
        self.lock = WorkerLock(settings.worker_lock_path)
        self.status, self.last_error, self.current_job = "STARTING", None, None
        self.started, self.heartbeat_task = False, None
        self.active_compute = None
        self.active_recovery = None
        self.active_outbox = None
        self.active_reconciliation = None
        self.active_cancel = None
        self.active_playback = None
        self.heartbeat_write_lock = threading.Lock()
        self.installation_sha256, self.build_sha256 = None, None

    def write_heartbeat(self):
        with self.heartbeat_write_lock:
            self._write_heartbeat()

    def _write_heartbeat(self):
        if self.settings.installation_identity() != self.installation_sha256:
            self.status, self.last_error = "DEGRADED", "INSTALLATION_BINDING_CHANGED"
        value = {"schema_version": "saferoute-m3-worker-heartbeat/1", "worker_id": self.worker_id,
            "status": self.status, "updated_at": datetime.now(timezone.utc).isoformat(),
            "build_sha256": self.build_sha256,
            "installation_sha256": self.installation_sha256, "current_job_id": self.current_job,
            "last_error_code": self.last_error}
        path = self.settings.heartbeat_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + self.worker_id + ".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        replace_heartbeat_file(temporary, path)

    async def heartbeat_loop(self):
        try:
            while True:
                await asyncio.to_thread(self.write_heartbeat)
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.status, self.last_error = "DEGRADED", "HEARTBEAT_UNAVAILABLE"
            logger.error("Worker heartbeat failed exception_type=%s", type(error).__name__)

    @staticmethod
    async def drain_sdk_task(task):
        # Repeated cancellation must never release the singleton while an old
        # shielded SDK operation can still fence or publish a job.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break

    async def owned_sdk_task(self, attribute, pending):
        # Track the whole operation, including any durable M3 receipt after
        # the SDK call. Cancellation cannot release ownership prematurely.
        task = asyncio.create_task(pending)
        setattr(self, attribute, task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self.drain_sdk_task(task)
            raise
        finally:
            if not task.done():
                await self.drain_sdk_task(task)
            setattr(self, attribute, None)

    async def sync_outbox(self, session_ids, summaries=None):
        return await self.owned_sdk_task("active_outbox", self.outbox_service.sync_many(session_ids, summaries))

    def outbox_poll_batch(self):
        ready = set(self.recovery.pollable_sessions())
        self.outbox_poll_order = [sid for sid in self.outbox_poll_order if sid in ready]
        self.outbox_poll_order.extend(sorted(ready - set(self.outbox_poll_order)))
        return self.outbox_poll_order[:1]

    async def startup(self):
        self.lock.acquire()  # Recovery is forbidden until this exclusive lock is held.
        try:
            self.installation_sha256 = self.settings.installation_identity()
            self.build_sha256 = self.settings.installation()["expected_build_sha256"]
            self.sessions.initialize()
            self.jobs.initialize()
            self.plans.initialize()
            self.replays.initialize()
            self.outbox.initialize()
            self.recovery.initialize()
            self.evidence.initialize()
            self.profiles.initialize()
            self.playback.initialize()
            self.playback_service.fence_restart()
            self.catalog.initialize()
            await self.gateway.capabilities()
            self.heartbeat_task = asyncio.create_task(self.heartbeat_loop())
            session_ids = self.recovery.ready_sessions()
            if self.settings.deferred_session_recovery:
                required = self.recovery.startup_required_sessions()
                for sid in session_ids:
                    self.recovery.record(sid, "DEFERRED", self.worker_id, self.installation_sha256)
                session_ids = [sid for sid in session_ids if sid in required]
            for sid in session_ids:
                self.recovery.record(sid, "RECOVERING", self.worker_id, self.installation_sha256)
            await self.inspect_and_validate(session_ids)
            running = self.jobs.running_jobs()
            for row in running:
                try:
                    self.recovery.assert_mutation_allowed(row["session_id"], allow_validated_recovery=True)
                except ApiError:
                    continue  # Preserve blocked RUNNING metadata for a fenced admin review.
                view = await self.job_service.poll(row["session_id"], row["job_id"])
                self.jobs.restore_job(row, view["job_status"] in TERMINAL)
            if self.status != "DEGRADED":
                self.session_service.allow_validated_recovery = True
                if await self.reconcile_pending() is False:
                    raise ApiError(503, "RUNTIME_BUSY", "runtime", "Startup coordination is busy; retry after the current SDK operation")
            quarantined = self.jobs.quarantined_jobs()
            if quarantined:
                self.status, self.last_error = "DEGRADED", quarantined[0]["error_code"] or "QUEUE_QUARANTINED"
                for row in quarantined:
                    self.recovery.record(row["session_id"], "BLOCKED", self.worker_id, self.installation_sha256,
                        diagnostic=row["error_code"] or "QUEUE_QUARANTINED")
            self.started = True
            if self.status != "DEGRADED":
                self.recovery.complete_verification(self.worker_id, self.installation_sha256)
                self.status = "READY"
            self.session_service.allow_validated_recovery = False
            await asyncio.to_thread(self.write_heartbeat)
        except BaseException as error:
            self.session_service.allow_validated_recovery = False
            code = error.code if isinstance(error, ApiError) else "RECOVERY_INTERRUPTED" if isinstance(error, asyncio.CancelledError) else "RECOVERY_UNAVAILABLE"
            self.last_error = code
            try:
                for sid in self.recovery.ready_sessions():
                    self.recovery.record(sid, "BLOCKED", self.worker_id, self.installation_sha256, diagnostic=code)
                    self.recovery.audit(sid, self.worker_id, None, "STARTUP_FAILED", diagnostic=code)
            except Exception:
                pass  # Existing evidence stays intact when the metadata itself is unavailable.
            await self.shutdown()
            raise

    async def inspect_and_validate(self, session_ids):
        if session_ids:
            command_id = "m3-startup-recover-" + uuid4().hex
            for sid in session_ids:
                self.recovery.audit(sid, self.worker_id, command_id + ":" + sid, "RECOVERY_STARTED")
                try:
                    self.session_service.session(sid)
                except ApiError as error:
                    for blocked in session_ids:
                        self.recovery.record(blocked, "BLOCKED", self.worker_id, self.installation_sha256, diagnostic=error.code)
                        self.recovery.audit(blocked, self.worker_id, command_id + ":" + blocked, "RECOVERY_FAILED", diagnostic=error.code)
                    raise
            task = self.active_recovery = asyncio.create_task(self.gateway.inspect_sessions(session_ids, command_id))
            try:
                inspections = await asyncio.shield(task)
            except asyncio.CancelledError:
                await self.drain_sdk_task(task)
                raise
            except ApiError as error:
                for sid in session_ids:
                    self.recovery.record(sid, "BLOCKED", self.worker_id, self.installation_sha256, diagnostic=error.code)
                    self.recovery.audit(sid, self.worker_id, command_id, "RECOVERY_FAILED", diagnostic=error.code)
                self.status, self.last_error = "DEGRADED", error.code
                inspections = []
            finally:
                if not task.done():
                    await self.drain_sdk_task(task)
                self.active_recovery = None
            verified, notifications = [], {}
            for item in inspections:
                sid = item["session_id"]
                code = item.get("diagnostic_code")
                if code:
                    self.recovery.record(sid, "BLOCKED", self.worker_id, self.installation_sha256, diagnostic=code)
                    self.recovery.audit(sid, self.worker_id, command_id + ":" + sid, "RECOVERY_FAILED", diagnostic=code)
                    self.status, self.last_error = "DEGRADED", code
                    continue
                try:
                    RuntimeGateway.check_validation(item["validation"], self.build_sha256)
                except (ValueError, TypeError, KeyError):
                    code = "SESSION_VALIDATION_INVALID"
                    self.recovery.record(sid, "BLOCKED", self.worker_id, self.installation_sha256, diagnostic=code)
                    self.recovery.audit(sid, self.worker_id, command_id + ":" + sid, "RECOVERY_FAILED", diagnostic=code)
                    self.status, self.last_error = "DEGRADED", code
                    continue
                try:
                    RuntimeGateway.check_inspection(item, self.build_sha256)
                    self.evidence.observe_execution(sid, item["execution_view"], source="WORKER_RECOVERY", reference_id=command_id)
                except (ValueError, KeyError, TypeError, ApiError) as error:
                    code = error.code if isinstance(error, ApiError) else "SESSION_RECOVERY_INVALID"
                    self.recovery.record(sid, "BLOCKED", self.worker_id, self.installation_sha256, diagnostic=code)
                    self.recovery.audit(sid, self.worker_id, command_id + ":" + sid, "RECOVERY_FAILED", diagnostic=code)
                    self.status, self.last_error = "DEGRADED", code
                    continue
                self.recovery.record(sid, "VALIDATED", self.worker_id, self.installation_sha256,
                    validation=item["validation"], basis=item["execution_view"]["basis"])
                self.recovery.audit(sid, self.worker_id, command_id + ":" + sid, "RECOVER_VALIDATE", response={"recovery": item["recovery"], "validation": item["validation"]})
                verified.append(sid)
                notifications[sid] = item["notifications"]
            try:
                await self.sync_outbox(verified, notifications)
            except ApiError as error:
                self.status, self.last_error = "DEGRADED", error.code
                for sid in verified:
                    self.recovery.record(sid, "BLOCKED", self.worker_id, self.installation_sha256, diagnostic=error.code)
                    self.recovery.audit(sid, self.worker_id, command_id, "OUTBOX_FAILED", diagnostic=error.code)
            self.next_outbox_poll = time.monotonic() + 10

    async def recover(self, session_id):
        # A cancelled await must not let an old recovery command outlive the
        # singleton and fence a job dispatched by the next lock owner.
        command_id = "m3-recover-" + uuid4().hex
        self.recovery.audit(session_id, self.worker_id, command_id, "RECOVERY_STARTED")
        task = self.active_recovery = asyncio.create_task(self.gateway.recover(session_id, command_id))
        try:
            result = await asyncio.shield(task)
            self.recovery.audit(session_id, self.worker_id, command_id, "RECOVER", response=result)
            return result
        except asyncio.CancelledError:
            await self.drain_sdk_task(task)
            raise
        finally:
            if not task.done():
                await self.drain_sdk_task(task)
            self.active_recovery = None

    async def reconcile_pending(self):
        return await self.owned_sdk_task("active_reconciliation", self._reconcile_pending())

    async def _reconcile_pending(self):
        for row in self.replays.pending_requests():
            try:
                await self.replay_service.reconcile(row)
            except ApiError as error:
                if error.code == "RUNTIME_BUSY":
                    return False
                if error.code in ("REQUEST_IN_PROGRESS", "STALE_HEAD", "TIME_REWIND", "EVENT_TRANSITION_REQUIRED", "EVENT_NOT_DUE",
                                  "EVENT_ALREADY_APPLIED", "ACCEPTED_PLAN_REQUIRED", "ACCEPTED_REPLAY_REQUIRED", "IDEMPOTENCY_CONFLICT",
                                  "WITNESS_REQUIRED", "WITNESS_INVALID", "PLAN_NOT_ACTIVE", "EVENT_UNKNOWN", "EVENT_TYPE_UNSUPPORTED"):
                    continue
                self.status, self.last_error = "DEGRADED", error.code
                return
        for row in self.plans.pending_requests():
            try:
                await self.plan_service.reconcile(row)
            except ApiError as error:
                if error.code == "RUNTIME_BUSY":
                    return False
                if error.code in ("REQUEST_IN_PROGRESS", "STALE_HEAD", "JOB_STALE", "IDEMPOTENCY_CONFLICT",
                                  "JOB_LIFECYCLE", "WITNESS_REQUIRED", "WITNESS_INVALID"):
                    continue
                self.status, self.last_error = "DEGRADED", error.code
                return
        for row in self.jobs.pending_requests():
            try:
                await self.job_service.reconcile(row)
            except ApiError as error:
                if error.code == "RUNTIME_BUSY":
                    return False
                if error.code in ("REQUEST_IN_PROGRESS", "STALE_HEAD", "IDEMPOTENCY_CONFLICT"):
                    continue
                self.status, self.last_error = "DEGRADED", error.code
                break

    async def run_once(self):
        if not self.started:
            raise RuntimeError("Worker startup/singleton lock required")
        if self.status != "READY":
            return False
        requested = self.recovery.requested_sessions()
        if requested:
            # Only the singleton may fence a saved session. API requests wait
            # for this verification before reading or mutating it.
            for sid in requested:
                self.recovery.record(sid, "RECOVERING_ON_DEMAND", self.worker_id, self.installation_sha256)
            await self.inspect_and_validate(requested)
            if self.status == "READY":
                self.recovery.complete_verification(self.worker_id, self.installation_sha256)
            await asyncio.to_thread(self.write_heartbeat)
            return True
        if time.monotonic() >= self.next_outbox_poll:
            try:
                batch = self.outbox_poll_batch()
                if batch:
                    await self.sync_outbox(batch)
                    self.outbox_poll_order = self.outbox_poll_order[len(batch):] + batch
                self.next_outbox_poll = time.monotonic() + 10
            except ApiError as error:
                if error.code == "RUNTIME_BUSY":
                    return False
                self.status, self.last_error = "DEGRADED", error.code
                for sid in self.recovery.ready_sessions():
                    self.recovery.record(sid, "BLOCKED", self.worker_id, self.installation_sha256, diagnostic=error.code)
                    self.recovery.audit(sid, self.worker_id, None, "OUTBOX_FAILED", diagnostic=error.code)
                await asyncio.to_thread(self.write_heartbeat)
                return False
        if await self.reconcile_pending() is False:
            return False
        if self.status != "READY":
            return False
        if getattr(self.gateway, 'supports_profile_batch', False) is True:
            comparison = self.profiles.next_pending()
            if comparison is not None and comparison['mode'] == 'NEW_BATCH' and comparison['state'] in ('QUEUED','RUNNING'):
                if any(m['job_id'] is None for m in comparison['members']):
                    return await self.owned_sdk_task('active_reconciliation', self.profile_service.reconcile_one())
                rows = self.jobs.claim_profile_batch(comparison, self.worker_id)
                if rows:
                    return await self.run_profile_batch(comparison, rows)
        row = self.jobs.claim_job(self.worker_id)
        if row is None:
            try:
                progressed = await self.owned_sdk_task("active_reconciliation", self.profile_service.reconcile_one())
                if progressed:
                    return True
                return await self.owned_sdk_task("active_playback", self.playback_service.run_once())
            except ApiError as error:
                if error.code == "RUNTIME_BUSY":
                    return False
                self.status, self.last_error = "DEGRADED", error.code
                await asyncio.to_thread(self.write_heartbeat)
                return False
        self.current_job = row["job_id"]
        try:
            self.session_service.session(row["session_id"])
            self.session_service.mutation_allowed(row["session_id"])
            view = await self.job_service.poll(row["session_id"], row["job_id"])
            if view["job_status"] == "RUNNING":
                # Only this singleton owns dispatch: a pre-existing runtime RUNNING lease is orphaned.
                await self.recover(row["session_id"])
                view = await self.job_service.poll(row["session_id"], row["job_id"])
            if view["job_status"] not in TERMINAL:
                self.active_compute = asyncio.create_task(self.gateway.compute(row["session_id"], row["job_id"], row["compute_command_id"], row["budget_seconds"]))
                view = await asyncio.shield(self.active_compute)
            if view["input_basis"] != row["basis"]:
                raise ApiError(503, "JOB_BINDING_CHANGED", "worker", "Compute view differs from the persisted submit binding")
            if view["job_status"] not in TERMINAL:
                raise ApiError(503, "COMPUTE_NOT_TERMINAL", "worker", "Compute did not reach a terminal runtime state")
            self.evidence.observe_job(row["session_id"], row["job_id"], view, source="WORKER_TERMINAL", reference_id=row["compute_command_id"])
            self.jobs.finish_job(row)
            logger.info(json.dumps({"event": "compute_finished", "worker_id": self.worker_id, "job_id": row["job_id"],
                                   "job_status": view["job_status"], "business_status": view["business_status"]}))
        except ApiError as error:
            try:
                if await self.handle_failure(row, error) is False:
                    return False
            except ApiError as secondary:
                if secondary.code == "RUNTIME_BUSY":
                    self.jobs.restore_job(row, terminal=False)
                    return False
                self.quarantine(row, secondary.code)
        except asyncio.CancelledError:
            # Keep singleton ownership until the shielded bridge finishes; abrupt process death is fenced on restart.
            if self.active_compute is not None and not self.active_compute.done():
                await self.drain_sdk_task(self.active_compute)
            raise
        finally:
            self.active_compute = None
            self.current_job = None
            await asyncio.to_thread(self.write_heartbeat)
        return True

    def quarantine(self, row, code):
        self.status, self.last_error = "DEGRADED", code
        self.jobs.finish_job(row, error_code=code, quarantine=True)
        self.recovery.record(row["session_id"], "BLOCKED", self.worker_id, self.installation_sha256, diagnostic=code)
        self.recovery.audit(row["session_id"], self.worker_id, row["compute_command_id"], "COMPUTE_QUARANTINED", diagnostic=code)

    async def run_profile_batch(self, comparison, rows):
        sid=comparison['session_id'];ids=[m['job_id'] for m in comparison['members']]
        self.current_job=rows[0]['job_id']
        try:
            self.session_service.session(sid)
            self.session_service.mutation_allowed(sid)
            views=await self.owned_sdk_task('active_compute', self.compute_observed_batch(comparison, ids))
            if [v['job_id'] for v in views] != ids or any(v['input_basis'] != comparison['basis'] or v['job_status'] not in TERMINAL for v in views):
                raise ApiError(503,'COMPARISON_BINDING_CHANGED','batch','Batch result differs from original basis/jobs')
            by_id={r['job_id']:r for r in rows}
            for member,view in zip(comparison['members'],views):
                self.evidence.observe_job(sid,view['job_id'],view,source='WORKER_TERMINAL',reference_id='m3-batch:'+comparison['comparison_id'])
                if view['job_id'] in by_id:self.jobs.finish_job(by_id[view['job_id']])
                self.profiles.observed(comparison['comparison_id'],member['profile'],view)
            await self.owned_sdk_task('active_reconciliation',self.profile_service.reconcile_one())
            return True
        except ApiError as error:
            if error.code in ('RUNTIME_BUSY','RUNTIME_TIMEOUT','JOB_LIFECYCLE'):
                if error.code != 'RUNTIME_BUSY':
                    try:
                        await self.recover(sid)
                    except ApiError as secondary:
                        if secondary.code != 'RUNTIME_BUSY':
                            for row in rows:self.quarantine(row,secondary.code)
                            return False
                for row in rows:self.jobs.restore_job(row,terminal=False)
                return False
            for row in rows:
                try:await self.handle_failure(row,error)
                except ApiError as secondary:
                    if secondary.code=='RUNTIME_BUSY':self.jobs.restore_job(row,terminal=False)
                    else:self.quarantine(row,secondary.code)
            return False
        finally:
            self.current_job=None
            await asyncio.to_thread(self.write_heartbeat)

    async def compute_observed_batch(self, comparison, ids):
        """Expose each committed witness while later profiles are still validating.

        This never publishes an uncommitted worker incumbent or completes the
        comparison early. Cancellation keeps the compute task owned and drained.
        """
        sid=comparison['session_id']
        task=asyncio.create_task(self.gateway.compute_batch(
            sid,ids,'m3-batch:'+comparison['comparison_id'],comparison['budget_seconds']))
        remaining=list(comparison['members'])
        try:
            while not task.done():
                done,_=await asyncio.wait([task],timeout=getattr(self,'profile_observation_interval',2.))
                if done:break
                if not remaining:continue
                member=remaining[0]
                try:
                    view=await self.gateway.job_view(sid,member['job_id'])
                except ApiError as error:
                    if error.code in ('RUNTIME_BUSY','RUNTIME_TIMEOUT'):continue
                    raise
                if view['job_id']!=member['job_id'] or view['input_basis']!=comparison['basis']:
                    raise ApiError(503,'COMPARISON_BINDING_CHANGED','batch','Observed witness differs from batch binding')
                if view['job_status'] in TERMINAL:
                    self.evidence.observe_job(sid,view['job_id'],view,source='WORKER_TERMINAL',reference_id='m3-batch:'+comparison['comparison_id'])
                    self.profiles.observed(comparison['comparison_id'],member['profile'],view)
                    remaining.pop(0)
            return await task
        finally:
            if not task.done():await self.drain_sdk_task(task)

    async def handle_failure(self, row, error):
        if error.code == "RUNTIME_BUSY":
            self.jobs.restore_job(row, terminal=False)
            return False
        if error.code == "JOB_STALE":
            await self.owned_sdk_task("active_cancel", self.gateway.cancel(row["session_id"], row["job_id"], row["stale_cancel_command_id"]))
            self.jobs.finish_job(row, error_code=error.code)
            return
        if error.code.startswith(("STORE", "JOURNAL", "RECEIPT", "OUTBOX", "BUILD", "SOURCE", "ENVIRONMENT", "INSTALLATION", "CATALOG", "JOB_BINDING")):
            self.quarantine(row, error.code)
            return
        if error.code in ("RUNTIME_TIMEOUT", "JOB_LIFECYCLE"):
            # subprocess.run has killed/waited for the timed-out bridge before orphan recovery.
            await self.recover(row["session_id"])
        view = await self.job_service.poll(row["session_id"], row["job_id"])
        if view["job_status"] in TERMINAL:
            self.jobs.finish_job(row, error_code=error.code)
        else:
            self.quarantine(row, error.code)

    async def run(self):
        try:
            for attempt in range(4):
                try:
                    await self.startup()
                    if self.status == 'DEGRADED' and self.last_error in ('RUNTIME_BUSY', 'RUNTIME_TIMEOUT'):
                        raise ApiError(503, self.last_error, 'recovery', 'Transient startup contention')
                    break
                except ApiError as error:
                    if error.code not in ('RUNTIME_BUSY', 'RUNTIME_TIMEOUT') or attempt == 3:
                        raise
                    await self.shutdown()
                    self.status, self.last_error = 'STARTING', None
                    await asyncio.sleep(min(2 ** attempt, 4))
            while True:
                if not await self.run_once():
                    await asyncio.sleep(1)
        finally:
            await self.shutdown()

    async def shutdown(self):
        try:
            if self.active_recovery is not None and not self.active_recovery.done():
                await self.drain_sdk_task(self.active_recovery)
            if self.active_compute is not None and not self.active_compute.done():
                await self.drain_sdk_task(self.active_compute)
            if self.active_outbox is not None and not self.active_outbox.done():
                await self.drain_sdk_task(self.active_outbox)
            if self.active_reconciliation is not None and not self.active_reconciliation.done():
                await self.drain_sdk_task(self.active_reconciliation)
            if self.active_cancel is not None and not self.active_cancel.done():
                await self.drain_sdk_task(self.active_cancel)
            if self.active_playback is not None and not self.active_playback.done():
                await self.drain_sdk_task(self.active_playback)
            try:
                self.playback_service.fence_restart()
            except (sqlite3.Error, OSError):
                self.last_error = "PLAYBACK_PAUSE_UNAVAILABLE"
                logger.error("Playback shutdown fence unavailable; next startup must fence before dispatch")
            if self.heartbeat_task is not None:
                self.heartbeat_task.cancel()
                try:
                    await self.heartbeat_task
                except (asyncio.CancelledError, Exception):
                    pass
                self.heartbeat_task = None
        finally:
            if self.lock.handle is not None:
                self.status = "STOPPED"
                try:
                    await asyncio.to_thread(self.write_heartbeat)
                except Exception as error:
                    logger.error("Worker final heartbeat failed exception_type=%s", type(error).__name__)
                finally:
                    self.lock.release()
            self.started = False
            if isinstance(self.gateway,RuntimeGateway):
                await asyncio.to_thread(self.gateway.close)
