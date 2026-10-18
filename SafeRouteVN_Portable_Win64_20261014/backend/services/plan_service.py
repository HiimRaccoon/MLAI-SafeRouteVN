from datetime import datetime, timezone
import sqlite3

from backend.api.errors import ApiError
from .job_service import JobService


class PlanService:
    def __init__(self, settings, repository, sessions, jobs, gateway):
        self.settings, self.repository, self.sessions, self.jobs, self.gateway = settings, repository, sessions, jobs, gateway

    async def accept(self, session_id, job_id, body, actor):
        self.sessions.session(session_id)
        self.sessions.mutation_allowed(session_id)
        installation = self.sessions.installation_identity()
        digest = JobService.digest("accept", body.model_dump(), job_id)
        try:
            self.jobs.repository.job(session_id, job_id)
            previous = self.repository.find_request(actor.actor_id, session_id, body.request_id, digest, installation)
            basis = None
            if previous is None:
                basis = (await self.gateway.resolve(session_id))["basis"]
                if any(basis[key] != getattr(body.expected_revision, key) for key in ("head_version", "generation")):
                    raise ApiError(409, "STALE_HEAD", "expected_revision", "Expected revision differs from the current server head")
                view = await self.jobs.poll(session_id, job_id)
                if view["input_basis"] != basis:
                    raise ApiError(409, "STALE_HEAD", "job_id", "Job was computed from another authority basis; optimize again")
                if (view["job_status"] != "COMPLETED" or view["validation"].get("valid") is not True
                        or view["plan_available"] is not True):
                    raise ApiError(409, "WITNESS_REQUIRED", "job_id", "Only a completed certified plan witness can be accepted")
            row = self.repository.reserve_request(actor.actor_id, session_id, body.request_id, digest, installation, job_id=job_id, basis=basis)
            receipt = await self.execute_request(row)
            # Receipt is already durable. This view is current, independently of
            # the historical acceptance basis returned on a later retry.
            view = await self.gateway.resolve(session_id)
            self.sessions.observe(view, "ACCEPT_CURRENT_OBSERVATION", receipt["acceptance_id"])
            return {"schema_version": "saferoute-m3-acceptance-view/1", "receipt": receipt, "execution_view": view}
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "accept", "Acceptance metadata is unavailable; retry the same request ID") from error

    async def execute_request(self, row):
        if row["response"] is not None:
            return row["response"]
        try:
            result = await self.gateway.accept(row["session_id"], row["job_id"], row["command_id"], row["basis"])
            expected = {**row["basis"], "generation": str(int(row["basis"]["generation"]) + 1)}
            if result["status"] != "ACCEPTED" or result["job_id"] != row["job_id"] or result["basis"] != expected:
                raise ApiError(503, "ACCEPT_BINDING_CHANGED", "runtime", "Acceptance receipt differs from the persisted job and basis")
            prefix = f'/api/sessions/{row["session_id"]}'
            receipt = {"schema_version": "saferoute-m3-plan-acceptance/1", "acceptance_id": row["acceptance_id"],
                "session_id": row["session_id"], "job_id": row["job_id"], "status": result["status"],
                "input_basis": row["basis"], "basis": result["basis"], "recorded_at": datetime.now(timezone.utc).isoformat(),
                "links": {"state": prefix + "/state", "job": prefix + "/jobs/" + row["job_id"]}}
            self.repository.complete_acceptance(row, receipt)
            return receipt
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

    def audit(self, session_id):
        self.sessions.session(session_id)
        try:
            return {"schema_version": "saferoute-m3-acceptance-audit/1", "session_id": session_id,
                "acceptances": self.repository.audit(session_id)}
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "acceptances", "Acceptance audit is unavailable") from error
