"""Durable acceptance requests and audit; runtime authority remains SDK-owned."""
import json
import time
from uuid import uuid4

from backend.api.errors import ApiError
from .job_repository import JobRepository


class PlanRepository(JobRepository):
    def initialize(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS accept_requests(
                actor_id TEXT NOT NULL, session_id TEXT NOT NULL, request_id TEXT NOT NULL,
                request_sha256 TEXT NOT NULL, installation_sha256 TEXT NOT NULL,
                command_id TEXT NOT NULL UNIQUE, acceptance_id TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL, basis_json TEXT NOT NULL, response_json TEXT,
                claim_token TEXT, claim_expires REAL NOT NULL DEFAULT 0,
                error_code TEXT, error_status INTEGER, created_at REAL NOT NULL,
                PRIMARY KEY(actor_id,session_id,request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS accept_audit(
                acceptance_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, session_id TEXT NOT NULL,
                request_id TEXT NOT NULL, command_id TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL,
                receipt_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
                UNIQUE(actor_id,session_id,request_id))""")

    def healthy(self):
        with self._db() as db:
            db.execute("SELECT acceptance_id FROM accept_requests LIMIT 1").fetchall()
            db.execute("SELECT acceptance_id FROM accept_audit LIMIT 1").fetchall()
        return True

    def find_request(self, actor_id, session_id, request_id, request_sha256, installation_sha256):
        with self._db() as db:
            row = db.execute("SELECT * FROM accept_requests WHERE actor_id=? AND session_id=? AND request_id=?",
                (actor_id, session_id, request_id)).fetchone()
        if row is not None:
            self.binding(row, request_sha256, installation_sha256)
        return self.decoded(row)

    def reserve_request(self, actor_id, session_id, request_id, request_sha256, installation_sha256, *, job_id=None, basis=None):
        keys, now, token = (actor_id, session_id, request_id), time.time(), uuid4().hex
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accept_requests WHERE actor_id=? AND session_id=? AND request_id=?", keys).fetchone()
            if row is not None:
                self.binding(row, request_sha256, installation_sha256)
                if row["response_json"] is not None:
                    return self.decoded(row)
                if row["claim_token"] and row["claim_expires"] > now:
                    raise ApiError(409, "REQUEST_IN_PROGRESS", "request_id", "Acceptance is in progress; retry the same request ID")
            else:
                if job_id is None or basis is None:
                    raise ValueError("New acceptance requires the certified job and server basis")
                db.execute("""INSERT INTO accept_requests(actor_id,session_id,request_id,request_sha256,installation_sha256,
                    command_id,acceptance_id,job_id,basis_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (*keys, request_sha256, installation_sha256, "m3-accept-" + uuid4().hex,
                     "accept-" + uuid4().hex, job_id, json.dumps(basis, allow_nan=False), now))
            db.execute("UPDATE accept_requests SET claim_token=?,claim_expires=? WHERE actor_id=? AND session_id=? AND request_id=?",
                (token, now + 120, *keys))
            return self.decoded(db.execute("SELECT * FROM accept_requests WHERE actor_id=? AND session_id=? AND request_id=?", keys).fetchone())

    @staticmethod
    def request_keys(row):
        return row["actor_id"], row["session_id"], row["request_id"]

    def complete_acceptance(self, row, receipt):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE accept_requests SET response_json=?,claim_token=NULL,claim_expires=0
                WHERE actor_id=? AND session_id=? AND request_id=? AND claim_token=? AND claim_expires>?
                AND response_json IS NULL""", (json.dumps(receipt, allow_nan=False), *self.request_keys(row),
                    row["claim_token"], time.time())).rowcount
            if changed != 1:
                raise ApiError(503, "REQUEST_LEASE_LOST", "request_id", "Acceptance lease expired; retry the same request ID")
            db.execute("""INSERT INTO accept_audit(acceptance_id,actor_id,session_id,request_id,command_id,job_id,receipt_json,recorded_at)
                VALUES(?,?,?,?,?,?,?,?)""", (row["acceptance_id"], row["actor_id"], row["session_id"], row["request_id"],
                    row["command_id"], row["job_id"], json.dumps(receipt, allow_nan=False), receipt["recorded_at"]))

    def release_request(self, row):
        with self._db() as db:
            db.execute("""UPDATE accept_requests SET claim_token=NULL,claim_expires=0
                WHERE actor_id=? AND session_id=? AND request_id=? AND claim_token=?""",
                (*self.request_keys(row), row["claim_token"]))

    def reject_request(self, row, error):
        with self._db() as db:
            db.execute("""UPDATE accept_requests SET error_code=?,error_status=?,claim_token=NULL,claim_expires=0
                WHERE actor_id=? AND session_id=? AND request_id=? AND claim_token=?""",
                (error.code, error.status_code, *self.request_keys(row), row["claim_token"]))

    def pending_requests(self):
        with self._db() as db:
            rows = db.execute("""SELECT * FROM accept_requests WHERE response_json IS NULL AND error_code IS NULL
                AND (claim_token IS NULL OR claim_expires<=?) ORDER BY created_at LIMIT 20""", (time.time(),)).fetchall()
        return [self.decoded(row) for row in rows]

    def audit(self, session_id):
        with self._db() as db:
            rows = db.execute("SELECT receipt_json FROM accept_audit WHERE session_id=? ORDER BY recorded_at DESC,acceptance_id DESC LIMIT 100",
                (session_id,)).fetchall()
        return [json.loads(row["receipt_json"]) for row in rows]
