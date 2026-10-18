"""Async profile batches around the verified public SDK, without a local verdict."""
import hashlib
import json
import sqlite3
from types import SimpleNamespace

from backend.api.errors import ApiError
from backend.models.http import CancelJobRequest
from .profile_repository import PROFILES

JOB_TERMINAL = ("COMPLETED", "FAILED")


class ProfileService:
    def __init__(self, settings, repository, sessions, jobs, gateway):
        self.settings, self.repository, self.sessions, self.jobs, self.gateway = settings, repository, sessions, jobs, gateway

    @staticmethod
    def digest(body):
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def owned_session(self, session_id, actor):
        session = self.sessions.session(session_id)
        if session["owner_actor_id"] != actor.actor_id or getattr(actor, "role", None) != "dispatcher":
            raise ApiError(403, "FORBIDDEN", "session", "Session owner with dispatcher role required")
        return session

    async def create(self, session_id, body, actor):
        self.owned_session(session_id, actor)
        self.sessions.mutation_allowed(session_id)
        installation, digest = self.sessions.installation_identity(), self.digest(body.model_dump())
        try:
            previous = self.repository.find_request(actor.actor_id, session_id, body.request_id, digest, installation)
            if previous is not None:
                return previous["receipt"]
            # New weather runs compare a frozen observed frame. Pause automatic
            # playback before resolving the basis; an in-flight tick must settle.
            session=self.sessions.session(session_id)
            if session.get('run_id'):
                from .playback_repository import PlaybackRepository
                playback=PlaybackRepository(self.settings.metadata_path)
                playback.stop(session_id,'PROFILE_COMPARISON')
                if not playback.public(playback.get(session_id))['fully_paused']:
                    raise ApiError(409,'PLAYBACK_TICK_IN_PROGRESS','playback','Wait for the paused replay tick before comparing profiles')
            mapped = None
            if body.job_ids is not None:
                selected = [self.jobs.repository.job(session_id, jid) for jid in body.job_ids]
                mapped = {row["profile"]: row["job_id"] for row in selected}
                if set(mapped) != set(PROFILES):
                    raise ApiError(409, "PROFILE_SET_REQUIRED", "job_ids", "Exactly one FASTEST, BALANCED and SAFER job required")
            basis = (await self.gateway.resolve(session_id))["basis"]
            if body.expected_revision is not None and any(basis[k] != getattr(body.expected_revision, k) for k in ("head_version", "generation")):
                raise ApiError(409, "STALE_HEAD", "expected_revision", "Expected revision differs from the current server head")
            return self.repository.create(actor.actor_id, session_id, body.request_id, digest, installation,
                basis=basis, budget=self.settings.compute_budget_seconds, jobs=mapped)["receipt"]
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "profiles", "Comparison metadata unavailable; retry the same request ID") from error

    def poll(self, session_id, cid):
        self.sessions.session(session_id)
        try:
            row = self.repository.comparison(session_id, cid, self.sessions.installation_identity())
            return {"schema_version": "saferoute-m3-profile-comparison/1", "session_id": session_id,
                "comparison_id": cid, "mode": row["mode"], "status": row["state"], "input_basis": row["basis"],
                "jobs": [{"profile": m["profile"], "job_id": m["job_id"], "view": m["view"]} for m in row["members"]],
                "outcome": row["outcome"], "links": row["receipt"]["links"],
                "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False,
                "metric_scope": "FORECAST_ONLY", "exposure_is_proxy": True}
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "profiles", "Comparison metadata unavailable") from error

    async def cancel(self, session_id, cid, body, actor):
        self.owned_session(session_id, actor)
        self.sessions.mutation_allowed(session_id)
        try:
            receipt = self.repository.cancel(actor.actor_id, session_id, body.request_id, cid, self.sessions.installation_identity())
            # Fence known owned jobs immediately, even while the singleton is
            # awaiting compute. A response lost after SDK commit is retried with
            # the original durable JobService cancellation command.
            row = self.repository.comparison(session_id, cid, self.sessions.installation_identity())
            if row["state"] == "CANCEL_REQUESTED" and row["mode"] == "NEW_BATCH":
                for member in row["members"]:
                    if member["job_id"] is not None and not member["cancelled"]:
                        await self.cancel_member(row, member)
            return receipt
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "profiles", "Cancellation metadata unavailable; retry the same request ID") from error

    def submission_binding(self, row, member):
        body = {"comparison_id": row["comparison_id"], "profile": member["profile"], "basis": row["basis"]}
        return (row["actor_id"], row["session_id"], "optimize", member["submission_request_id"],
                self.jobs.digest("profile-batch-submit", body), row["installation_sha256"])

    async def submit_member(self, row, member):
        reserved = self.jobs.repository.reserve_request(*self.submission_binding(row, member), basis=row["basis"],
                    profile=member["profile"], budget_seconds=row["budget_seconds"])
        result = await self.jobs.execute_request(reserved)
        self.repository.submitted(row["comparison_id"], member["profile"], result["job_id"])

    async def cancel_member(self, row, member):
        await self.jobs.cancel(row["session_id"], member["job_id"], CancelJobRequest(request_id=member["cancel_request_id"]),
                               SimpleNamespace(actor_id=row["actor_id"]))
        self.repository.cancelled_member(row["comparison_id"], member["profile"])

    async def reconcile_one(self):
        """At most one SDK operation per tick; round robin unfinished groups.

        Root worker dispatch can prioritize already queued computes. This
        method creates no native compute and never accepts/advances a plan.
        """
        row = self.repository.next_pending()
        if row is None:
            return False
        cid, sid = row["comparison_id"], row["session_id"]
        try:
            self.sessions.session(sid)
            self.sessions.mutation_allowed(sid)
            self.repository.binding(row, self.sessions.installation_identity())
            cancelling = row["state"] == "CANCEL_REQUESTED"
            if cancelling and row["mode"] == "EXISTING_JOBS":
                self.repository.complete(cid, "FAILED" if row["error_code"] else "CANCELLED",
                    {"comparison": None, "reason": row["error_code"] or "COMPARISON_CANCELLED_EXISTING_JOBS_UNCHANGED"})
                return True
            if row["mode"] == "NEW_BATCH":
                # Resolve any uncertain submit before declaring cancellation.
                # With no previously reserved request, cancelled batches create
                # no new SDK jobs. Resuming a reserved request only retrieves
                # its original SDK command/commit; basis is never refreshed.
                for member in row["members"]:
                    if member["job_id"] is not None:
                        continue
                    try:
                        previous = self.jobs.repository.find_request(*self.submission_binding(row, member))
                    except ApiError as error:
                        if cancelling and error.status_code in (400, 404, 409, 422):
                            continue  # Durable SDK rejection created no child.
                        raise
                    if cancelling and previous is None:
                        continue
                    await self.submit_member(row, member)
                    if getattr(self.gateway, 'supports_profile_batch', False) is not True or cancelling:
                        return True
                # Re-read durable member bindings on the next tick. Native batch
                # dispatch, including partial-submit recovery, belongs to worker.
                if not cancelling and getattr(self.gateway, 'supports_profile_batch', False) is True and any(m['job_id'] is None for m in row['members']):
                    return True
            if cancelling:
                for member in row["members"]:
                    if member["job_id"] is not None and not member["cancelled"]:
                        await self.cancel_member(row, member)
                        return True
            pending = [m for m in row["members"] if m["job_id"] is not None and
                       (m["view"] is None or m["view"]["job_status"] not in JOB_TERMINAL)]
            if pending:
                member = pending[row["poll_cursor"] % len(pending)]
                view = await self.jobs.poll(sid, member["job_id"])
                if row["mode"] == "NEW_BATCH" and view["input_basis"] != row["basis"]:
                    raise ApiError(503, "COMPARISON_BINDING_CHANGED", "profiles", "Child differs from the persisted batch basis")
                self.repository.observed(cid, member["profile"], view)
                return True
            if cancelling:
                self.repository.complete(cid, "FAILED" if row["error_code"] else "CANCELLED",
                    {"comparison": None, "reason": row["error_code"] or "BATCH_CANCELLED"})
                return True
            views = [m["view"] for m in row["members"]]
            if any(v is None for v in views):
                raise ApiError(503, "COMPARISON_BINDING_CHANGED", "profiles", "Complete batch requires every typed child view")
            if any(v["job_status"] != "COMPLETED" or v["validation"].get("valid") is not True for v in views):
                outcome = {"comparison": None, "reason": "NO_CERTIFIED_WITNESS_FOR_EVERY_PROFILE"}
            else:
                jobs = [m["job_id"] for m in row["members"]]
                result = await self.gateway.compare_profiles(sid, jobs)
                self.check_comparison(row, result)
                outcome = {"comparison": result, "reason": result["reason"]}
            self.repository.complete(cid, "COMPLETED", outcome)
            return True
        except ApiError as error:
            if error.code in ("RUNTIME_BUSY", "RUNTIME_TIMEOUT", "REQUEST_IN_PROGRESS", "REQUEST_LEASE_LOST"):
                return False  # Retain original commands for a later tick.
            if error.status_code in (400, 404, 409, 422):
                self.repository.fail(cid, error.code)
                return True
            raise  # Authority/source/binding failures remain fail closed.
        finally:
            self.repository.touch(cid)

    def check_comparison(self, row, value):
        try:
            from .runtime_gateway import RuntimeGateway
            jobs = [m["job_id"] for m in row["members"]]
            RuntimeGateway.check_comparison(value, row["session_id"], jobs, self.settings.installation()["expected_build_sha256"])
            for member, result in zip(row["members"], value["jobs"]):
                if result["profile"] != member["profile"] or result["basis"] != member["view"]["input_basis"]:
                    raise ValueError("SDK comparison child binding")
        except (ValueError, TypeError, KeyError) as error:
            raise ApiError(503, "COMPARISON_BINDING_CHANGED", "profiles", "Public SDK comparison differs from typed child bindings") from error
