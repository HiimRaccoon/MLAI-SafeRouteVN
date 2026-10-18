"""Durable M3 request receipts and queue metadata; never writes M2 authority."""
import json
import logging
import sqlite3
import time
from uuid import uuid4

from backend.api.errors import ApiError
from .session_repository import SessionRepository


logger = logging.getLogger("saferoute.worker")


class JobRepository(SessionRepository):
    def initialize(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS job_requests(
                actor_id TEXT NOT NULL, session_id TEXT NOT NULL, operation TEXT NOT NULL, request_id TEXT NOT NULL,
                request_sha256 TEXT NOT NULL, installation_sha256 TEXT NOT NULL, command_id TEXT NOT NULL UNIQUE,
                basis_json TEXT, profile TEXT, budget_seconds REAL, job_id TEXT,
                response_json TEXT, claim_token TEXT, claim_expires REAL NOT NULL DEFAULT 0,
                error_code TEXT, error_status INTEGER, created_at REAL NOT NULL,
                PRIMARY KEY(actor_id,session_id,operation,request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS compute_queue(
                job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, installation_sha256 TEXT NOT NULL,
                basis_json TEXT NOT NULL, profile TEXT NOT NULL, budget_seconds REAL NOT NULL,
                compute_command_id TEXT NOT NULL UNIQUE, stale_cancel_command_id TEXT NOT NULL UNIQUE,
                queue_state TEXT NOT NULL, claim_token TEXT, worker_id TEXT, error_code TEXT, created_at REAL NOT NULL)""")

    def healthy(self):
        with self._db() as db:
            db.execute("SELECT job_id FROM compute_queue LIMIT 1").fetchall()
            db.execute("SELECT command_id FROM job_requests LIMIT 1").fetchall()
            return db.execute("SELECT 1 FROM compute_queue WHERE queue_state='QUARANTINED' LIMIT 1").fetchone() is None

    def quarantined_jobs(self):
        with self._db() as db:
            return [self.decoded(row) for row in db.execute("SELECT * FROM compute_queue WHERE queue_state='QUARANTINED'").fetchall()]

    @staticmethod
    def decoded(row):
        if row is None:
            return None
        out = dict(row)
        for key in ("basis_json", "response_json"):
            if key in out:
                out[key.removesuffix("_json")] = json.loads(out[key]) if out[key] is not None else None
        return out

    @staticmethod
    def binding(row, request_sha256, installation_sha256):
        if row["request_sha256"] != request_sha256:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "request_id", "Request ID was already used with different content")
        if row["installation_sha256"] != installation_sha256:
            raise ApiError(409, "INSTALLATION_BINDING_CHANGED", "session", "Request belongs to another server installation")
        if row["error_code"]:
            raise ApiError(row["error_status"], row["error_code"], "request_id", "Original server operation was rejected; use a new request ID")

    def find_request(self, actor_id, session_id, operation, request_id, request_sha256, installation_sha256):
        with self._db() as db:
            row = db.execute("SELECT * FROM job_requests WHERE actor_id=? AND session_id=? AND operation=? AND request_id=?",
                             (actor_id, session_id, operation, request_id)).fetchone()
        if row is not None:
            self.binding(row, request_sha256, installation_sha256)
        return self.decoded(row)

    def reserve_request(self, actor_id, session_id, operation, request_id, request_sha256, installation_sha256,
                        *, basis=None, profile=None, budget_seconds=None, job_id=None):
        keys = (actor_id, session_id, operation, request_id)
        token, now = uuid4().hex, time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM job_requests WHERE actor_id=? AND session_id=? AND operation=? AND request_id=?", keys).fetchone()
            if row is not None:
                self.binding(row, request_sha256, installation_sha256)
                if row["response_json"] is not None:
                    return self.decoded(row)
                if row["claim_token"] and row["claim_expires"] > now:
                    raise ApiError(409, "REQUEST_IN_PROGRESS", "request_id", "Request is in progress; retry the same request ID")
            else:
                if operation == "optimize" and (basis is None or profile is None or budget_seconds is None):
                    raise ValueError("New submit requires a persisted server basis/profile/budget")
                db.execute("""INSERT INTO job_requests(actor_id,session_id,operation,request_id,request_sha256,
                    installation_sha256,command_id,basis_json,profile,budget_seconds,job_id,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (*keys, request_sha256, installation_sha256,
                    "m3-" + operation + "-" + uuid4().hex, json.dumps(basis, allow_nan=False) if basis is not None else None,
                    profile, budget_seconds, job_id, now))
            db.execute("""UPDATE job_requests SET claim_token=?,claim_expires=?
                WHERE actor_id=? AND session_id=? AND operation=? AND request_id=?""", (token, now + 90, *keys))
            return self.decoded(db.execute("SELECT * FROM job_requests WHERE actor_id=? AND session_id=? AND operation=? AND request_id=?", keys).fetchone())

    @staticmethod
    def request_keys(row):
        return (row["actor_id"], row["session_id"], row["operation"], row["request_id"])

    def _complete(self, db, row, data):
        changed = db.execute("""UPDATE job_requests SET response_json=?,job_id=?,claim_token=NULL,claim_expires=0
            WHERE actor_id=? AND session_id=? AND operation=? AND request_id=? AND claim_token=? AND claim_expires>?
            AND response_json IS NULL""", (json.dumps(data, allow_nan=False), data["job_id"], *self.request_keys(row), row["claim_token"], time.time())).rowcount
        if changed != 1:
            raise ApiError(503, "REQUEST_LEASE_LOST", "request_id", "Request lease expired; retry the same request ID")

    def complete_submission(self, row, data):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._complete(db, row, data)
            db.execute("""INSERT INTO compute_queue(job_id,session_id,installation_sha256,basis_json,profile,budget_seconds,
                compute_command_id,stale_cancel_command_id,queue_state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (data["job_id"], row["session_id"], row["installation_sha256"], row["basis_json"], row["profile"], row["budget_seconds"],
                 "m3-compute-" + uuid4().hex, "m3-stale-cancel-" + uuid4().hex, "QUEUED", time.time()))

    def complete_cancellation(self, row, data):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._complete(db, row, data)
            db.execute("UPDATE compute_queue SET queue_state='DONE',claim_token=NULL,worker_id=NULL WHERE job_id=? AND session_id=?",
                       (row["job_id"], row["session_id"]))

    def release_request(self, row):
        with self._db() as db:
            db.execute("""UPDATE job_requests SET claim_token=NULL,claim_expires=0
                WHERE actor_id=? AND session_id=? AND operation=? AND request_id=? AND claim_token=?""",
                (*self.request_keys(row), row["claim_token"]))

    def reject_request(self, row, error):
        with self._db() as db:
            db.execute("""UPDATE job_requests SET error_code=?,error_status=?,claim_token=NULL,claim_expires=0
                WHERE actor_id=? AND session_id=? AND operation=? AND request_id=? AND claim_token=?""",
                (error.code, error.status_code, *self.request_keys(row), row["claim_token"]))

    def pending_requests(self):
        with self._db() as db:
            rows = db.execute("""SELECT * FROM job_requests WHERE response_json IS NULL AND error_code IS NULL
                AND (claim_token IS NULL OR claim_expires<=?) ORDER BY created_at LIMIT 20""", (time.time(),)).fetchall()
        return [self.decoded(row) for row in rows]

    def job(self, session_id, job_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM compute_queue WHERE session_id=? AND job_id=?", (session_id, job_id)).fetchone()
        if row is None:
            raise ApiError(404, "JOB_NOT_FOUND", "job_id", "Job not found in this session")
        return self.decoded(row)

    def running_jobs(self):
        with self._db() as db:
            return [self.decoded(row) for row in db.execute("SELECT * FROM compute_queue WHERE queue_state='RUNNING'").fetchall()]

    def claim_job(self, worker_id):
        try:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM compute_queue WHERE queue_state='QUEUED' ORDER BY created_at LIMIT 1").fetchone()
                if row is None:
                    return None
                token = uuid4().hex
                db.execute("UPDATE compute_queue SET queue_state='RUNNING',claim_token=?,worker_id=? WHERE job_id=?", (token, worker_id, row["job_id"]))
                return self.decoded(db.execute("SELECT * FROM compute_queue WHERE job_id=?", (row["job_id"],)).fetchone())
        except sqlite3.OperationalError as error:
            # _db has rolled back and closed before we defer this atomic claim.
            # No SDK work has started, so the next poll may safely try again.
            code = getattr(error, "sqlite_errorcode", 0)
            if code & 0xff not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            logger.warning("Metadata database busy; deferring queue claim")
            return None

    def claim_profile_batch(self, comparison, worker_id):
        try:
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                current=db.execute('SELECT state FROM profile_comparisons WHERE comparison_id=?', (comparison['comparison_id'],)).fetchone()
                if current is None or current['state'] not in ('QUEUED','RUNNING'):
                    return []
                rows=[]
                for member in comparison['members']:
                    row=db.execute('SELECT * FROM compute_queue WHERE job_id=? AND session_id=?',
                                   (member['job_id'],comparison['session_id'])).fetchone()
                    if row is None or row['queue_state'] not in ('QUEUED','DONE'):
                        return []
                    if (json.loads(row['basis_json']) != comparison['basis'] or row['profile'] != member['profile']
                            or row['installation_sha256'] != comparison['installation_sha256']):
                        raise ApiError(503,'COMPARISON_BINDING_CHANGED','batch','Queue differs from comparison')
                    if row['queue_state']=='QUEUED':rows.append(row)
                claimed=[]
                for row in rows:
                    db.execute("UPDATE compute_queue SET queue_state='RUNNING',claim_token=?,worker_id=? WHERE job_id=?",
                               (uuid4().hex,worker_id,row['job_id']))
                    claimed.append(self.decoded(db.execute('SELECT * FROM compute_queue WHERE job_id=?',(row['job_id'],)).fetchone()))
                return claimed
        except sqlite3.OperationalError as error:
            # _db has rolled back and closed before we defer this atomic claim.
            # No SDK work has started, so the next poll may safely try again.
            code = getattr(error, "sqlite_errorcode", 0)
            if code & 0xff not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            logger.warning("Metadata database busy; deferring queue claim")
            return []

    def finish_job(self, row, *, error_code=None, quarantine=False):
        with self._db() as db:
            return db.execute("""UPDATE compute_queue SET queue_state=?,error_code=?,claim_token=NULL,worker_id=NULL
                WHERE job_id=? AND claim_token=? AND queue_state='RUNNING'""",
                ("QUARANTINED" if quarantine else "DONE", error_code, row["job_id"], row["claim_token"])).rowcount == 1

    def restore_job(self, row, terminal):
        with self._db() as db:
            db.execute("UPDATE compute_queue SET queue_state=?,claim_token=NULL,worker_id=NULL WHERE job_id=? AND queue_state='RUNNING'",
                       ("DONE" if terminal else "QUEUED", row["job_id"]))
