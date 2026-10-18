from datetime import datetime, timezone
import json
from uuid import uuid4
from backend.api.errors import ApiError
from .session_repository import SessionRepository


class RecoveryRepository(SessionRepository):
    def initialize(self):
        super().initialize()
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS session_recovery (
                session_id TEXT PRIMARY KEY,status TEXT NOT NULL,diagnostic_code TEXT,
                validation_json TEXT,basis_json TEXT,checked_at TEXT,worker_id TEXT,
                installation_sha256 TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS recovery_audit (
                audit_id TEXT PRIMARY KEY,session_id TEXT,worker_id TEXT,command_id TEXT,
                operation TEXT,recorded_at TEXT,response_json TEXT,diagnostic_code TEXT)""")

    def ready_sessions(self):
        with self._db() as db:
            return [r[0] for r in db.execute("SELECT session_id FROM sessions WHERE status='READY' ORDER BY created_at,session_id")]

    def startup_required_sessions(self):
        # Unfinished commands must be fenced and reconciled before readiness,
        # including commands whose HTTP claim has not expired yet.
        with self._db() as db:
            ids = {r[0] for r in db.execute("SELECT session_id FROM compute_queue WHERE queue_state!='DONE'")}
            for table in ('job_requests', 'accept_requests', 'replay_requests'):
                ids.update(r[0] for r in db.execute(f"SELECT session_id FROM {table} WHERE response_json IS NULL AND error_code IS NULL"))
            ids.update(r[0] for r in db.execute("SELECT session_id FROM profile_comparisons WHERE state NOT IN ('COMPLETED','CANCELLED','FAILED')"))
            ids.update(r[0] for r in db.execute("SELECT session_id FROM playback_controllers WHERE pending_request IS NOT NULL"))
        return ids

    def request_verification(self, session_id):
        with self._db() as db:
            db.execute("UPDATE session_recovery SET status='REQUESTED' WHERE session_id=? AND status='DEFERRED'", (session_id,))
            row = db.execute("SELECT status,diagnostic_code FROM session_recovery WHERE session_id=?", (session_id,)).fetchone()
        if row is None or row['status'] == 'VERIFIED':
            return True
        if row['status'] == 'BLOCKED':
            raise ApiError(503, row['diagnostic_code'] or 'SESSION_RECOVERY_FAILED', 'recovery', 'Session recovery failed')
        return False

    def requested_sessions(self):
        with self._db() as db:
            return [r[0] for r in db.execute("SELECT session_id FROM session_recovery WHERE status='REQUESTED' ORDER BY checked_at LIMIT 1")]

    def pollable_sessions(self):
        with self._db() as db:
            return [r[0] for r in db.execute("""SELECT s.session_id FROM sessions s
                LEFT JOIN session_recovery r ON r.session_id=s.session_id
                WHERE s.status='READY' AND (r.status IS NULL OR r.status='VERIFIED') ORDER BY s.created_at,s.session_id""")]

    def record(self, session_id, status, worker_id, installation, *, validation=None, basis=None, diagnostic=None):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO session_recovery VALUES(?,?,?,?,?,?,?,?)", (session_id, status, diagnostic,
                json.dumps(validation, sort_keys=True) if validation is not None else None,
                json.dumps(basis, sort_keys=True) if basis is not None else None,
                datetime.now(timezone.utc).isoformat(), worker_id, installation))

    def audit(self, session_id, worker_id, command_id, operation, response=None, diagnostic=None):
        with self._db() as db:
            db.execute("INSERT INTO recovery_audit VALUES(?,?,?,?,?,?,?,?)", (uuid4().hex,session_id,worker_id,command_id,operation,
                datetime.now(timezone.utc).isoformat(), json.dumps(response, sort_keys=True, allow_nan=False) if response is not None else None, diagnostic))

    def assert_mutation_allowed(self, session_id, *, allow_validated_recovery=False):
        with self._db() as db:
            row = db.execute("SELECT status,diagnostic_code FROM session_recovery WHERE session_id=?", (session_id,)).fetchone()
        if row and row["status"] != "VERIFIED" and not (allow_validated_recovery and row["status"] == "VALIDATED"):
            raise ApiError(503, row["diagnostic_code"] or "SESSION_RECOVERING", "recovery", "Session writes blocked pending verified server recovery")

    def complete_verification(self, worker_id, installation):
        with self._db() as db:
            db.execute("UPDATE session_recovery SET status='VERIFIED' WHERE status='VALIDATED' AND worker_id=? AND installation_sha256=?", (worker_id, installation))

    def healthy(self):
        with self._db() as db:
            return db.execute("SELECT 1 FROM session_recovery WHERE status NOT IN ('VERIFIED','DEFERRED','REQUESTED','RECOVERING_ON_DEMAND') LIMIT 1").fetchone() is None
