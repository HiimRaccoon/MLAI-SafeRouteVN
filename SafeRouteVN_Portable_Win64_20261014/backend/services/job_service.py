import hashlib
import json
import sqlite3

from backend.api.errors import ApiError


class JobService:
    def __init__(self, settings, repository, sessions, gateway):
        self.settings, self.repository, self.sessions, self.gateway = settings, repository, sessions, gateway

    @staticmethod
    def digest(operation, body, job_id=None):
        return hashlib.sha256(json.dumps({"operation": operation, "body": body, "job_id": job_id},
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    async def optimize(self, session_id, body, actor):
        self.sessions.session(session_id)
        self.sessions.mutation_allowed(session_id)
        installation = self.sessions.installation_identity()
        digest = self.digest("optimize", body.model_dump())
        try:
            previous = self.repository.find_request(actor.actor_id, session_id, "optimize", body.request_id, digest, installation)
            basis = None
            if previous is None:
                basis = (await self.gateway.resolve(session_id))["basis"]
                if body.expected_revision is not None and any(basis[key] != getattr(body.expected_revision, key) for key in ("head_version", "generation")):
                    raise ApiError(409, "STALE_HEAD", "expected_revision", "Expected revision differs from the current server head")
            row = self.repository.reserve_request(actor.actor_id, session_id, "optimize", body.request_id, digest, installation,
                basis=basis, profile=body.profile, budget_seconds=self.settings.compute_budget_seconds)
            return await self.execute_request(row)
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "jobs", "Job metadata is unavailable; retry the same request ID") from error

    async def cancel(self, session_id, job_id, body, actor):
        self.sessions.session(session_id)
        self.sessions.mutation_allowed(session_id)
        try:
            self.repository.job(session_id, job_id)
            row = self.repository.reserve_request(actor.actor_id, session_id, "cancel", body.request_id,
                self.digest("cancel", body.model_dump(), job_id), self.sessions.installation_identity(), job_id=job_id)
            return await self.execute_request(row)
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "jobs", "Job metadata is unavailable; retry the same request ID") from error

    async def execute_request(self, row):
        if row["response"] is not None:
            return row["response"]
        try:
            prefix = f'/api/sessions/{row["session_id"]}/jobs/'
            if row["operation"] == "optimize":
                result = await self.gateway.submit(row["session_id"], row["command_id"], row["basis"], row["profile"], row["budget_seconds"])
                if result["input_basis"] != row["basis"]:
                    raise ApiError(503, "SUBMIT_BINDING_CHANGED", "runtime", "Submit receipt differs from the persisted input")
                data = {"schema_version": "saferoute-m3-job-submission/1", "session_id": row["session_id"], "job_id": result["job_id"],
                    "profile": row["profile"], "input_basis": row["basis"], "links": {"poll": prefix + result["job_id"], "cancel": prefix + result["job_id"] + "/cancel"}}
                self.repository.complete_submission(row, data)
            else:
                result = await self.gateway.cancel(row["session_id"], row["job_id"], row["command_id"])
                data = {"schema_version": "saferoute-m3-job-cancellation/1", "session_id": row["session_id"], "job_id": row["job_id"],
                        "status": result["status"], "links": {"poll": prefix + row["job_id"]}}
                self.repository.complete_cancellation(row, data)
            return data
        except ApiError as error:
            if error.status_code in (400, 404, 409, 422):
                self.repository.reject_request(row, error)
            raise
        finally:
            self.repository.release_request(row)

    async def reconcile(self, pending):
        self.sessions.session(pending["session_id"])
        self.sessions.mutation_allowed(pending["session_id"])
        row = self.repository.reserve_request(*self.repository.request_keys(pending), pending["request_sha256"], self.sessions.installation_identity())
        return await self.execute_request(row)

    async def poll(self, session_id, job_id):
        self.sessions.session(session_id)
        try:
            row = self.repository.job(session_id, job_id)
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "jobs", "Job metadata is unavailable") from error
        view = await self.gateway.job_view(session_id, job_id)
        if view["input_basis"] != row["basis"]:
            raise ApiError(503, "JOB_BINDING_CHANGED", "runtime", "Job view differs from the persisted submit binding")
        if row["queue_state"] == "QUARANTINED" and view["job_status"] not in ("COMPLETED", "FAILED"):
            raise ApiError(503, row["error_code"] or "QUEUE_QUARANTINED", "queue", "Queue dispatch is paused for verified recovery")
        return view

    async def forecast(self, session_id, job_id):
        self.sessions.session(session_id)
        try:
            row = self.repository.job(session_id, job_id)
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "jobs", "Job metadata is unavailable") from error
        value = await self.gateway.job_forecast(session_id, job_id)
        if value['input_basis'] != row['basis'] or value['profile'] != row['profile']:
            raise ApiError(503, 'JOB_BINDING_CHANGED', 'forecast', 'Forecast differs from persisted submission')
        return value
