"""Durable comparison orchestration; M2 alone owns jobs and physical authority."""
import json
import time
from uuid import uuid4

from backend.api.errors import ApiError
from .session_repository import SessionRepository

PROFILES = ("FASTEST", "BALANCED", "SAFER")
TERMINAL = ("COMPLETED", "CANCELLED", "FAILED")


class ProfileRepository(SessionRepository):
    def initialize(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS profile_comparisons(
                comparison_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                request_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, installation_sha256 TEXT NOT NULL,
                mode TEXT NOT NULL, basis_json TEXT NOT NULL, budget_seconds REAL NOT NULL,
                state TEXT NOT NULL, outcome_json TEXT, error_code TEXT, receipt_json TEXT NOT NULL,
                poll_cursor INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(actor_id,session_id,request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS profile_members(
                comparison_id TEXT NOT NULL, profile TEXT NOT NULL, submission_request_id TEXT NOT NULL,
                cancel_request_id TEXT NOT NULL, job_id TEXT, view_json TEXT, cancelled INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(comparison_id,profile))""")
            db.execute("""CREATE TABLE IF NOT EXISTS profile_cancel_requests(
                actor_id TEXT NOT NULL, session_id TEXT NOT NULL, request_id TEXT NOT NULL,
                comparison_id TEXT NOT NULL, installation_sha256 TEXT NOT NULL, response_json TEXT NOT NULL,
                PRIMARY KEY(actor_id,session_id,request_id))""")

    def healthy(self):
        with self._db() as db:
            for table in ("profile_comparisons", "profile_members", "profile_cancel_requests"):
                db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchall()
        return True

    @staticmethod
    def decoded(row):
        value = dict(row)
        for name in ("basis", "outcome", "receipt", "view", "response"):
            if name + "_json" in value:
                raw = value.pop(name + "_json")
                value[name] = json.loads(raw) if raw is not None else None
        return value

    @staticmethod
    def binding(row, installation):
        if row["installation_sha256"] != installation:
            raise ApiError(409, "INSTALLATION_BINDING_CHANGED", "comparison", "Comparison belongs to another installation")

    def find_request(self, actor, session, request, digest, installation):
        with self._db() as db:
            row = db.execute("SELECT * FROM profile_comparisons WHERE actor_id=? AND session_id=? AND request_id=?",
                             (actor, session, request)).fetchone()
        if row is None:
            return None
        self.binding(row, installation)
        if row["request_sha256"] != digest:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "request_id", "Request ID already has different content")
        return self.decoded(row)

    def create(self, actor, session, request, digest, installation, *, basis, budget, jobs=None):
        now, cid = time.time(), "comparison-" + uuid4().hex
        prefix = f"/api/sessions/{session}/profiles/comparisons/{cid}"
        receipt = {"schema_version": "saferoute-m3-profile-submission/1", "session_id": session,
            "comparison_id": cid, "mode": "EXISTING_JOBS" if jobs else "NEW_BATCH", "input_basis": basis,
            "profiles": list(PROFILES), "links": {"poll": prefix, "cancel": prefix + "/cancel"}}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM profile_comparisons WHERE actor_id=? AND session_id=? AND request_id=?",
                             (actor, session, request)).fetchone()
            if row is not None:
                self.binding(row, installation)
                if row["request_sha256"] != digest:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "request_id", "Request ID already has different content")
                return self.decoded(row)
            db.execute("""INSERT INTO profile_comparisons(comparison_id,session_id,actor_id,request_id,request_sha256,
                installation_sha256,mode,basis_json,budget_seconds,state,receipt_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (cid, session, actor, request, digest, installation,
                receipt["mode"], json.dumps(basis, allow_nan=False), budget, "QUEUED", json.dumps(receipt, allow_nan=False), now, now))
            for profile in PROFILES:
                db.execute("""INSERT INTO profile_members(comparison_id,profile,submission_request_id,cancel_request_id,job_id)
                    VALUES(?,?,?,?,?)""", (cid, profile, "m3-profile-submit:" + cid + ":" + profile,
                    "m3-profile-cancel:" + cid + ":" + profile, jobs.get(profile) if jobs else None))
        return self.comparison(session, cid, installation)

    def comparison(self, session, cid, installation=None):
        with self._db() as db:
            row = db.execute("SELECT * FROM profile_comparisons WHERE session_id=? AND comparison_id=?", (session, cid)).fetchone()
            if row is None:
                raise ApiError(404, "COMPARISON_NOT_FOUND", "comparison_id", "Comparison is not in this session")
            if installation is not None:
                self.binding(row, installation)
            value = self.decoded(row)
            members = [self.decoded(r) for r in db.execute("SELECT * FROM profile_members WHERE comparison_id=?", (cid,)).fetchall()]
            value["members"] = sorted(members, key=lambda m: PROFILES.index(m["profile"]))
            return value

    def next_pending(self):
        with self._db() as db:
            row = db.execute("""SELECT session_id,comparison_id FROM profile_comparisons
                WHERE state NOT IN ('COMPLETED','CANCELLED','FAILED') ORDER BY updated_at,created_at LIMIT 1""").fetchone()
        return self.comparison(row["session_id"], row["comparison_id"]) if row else None

    def touch(self, cid):
        with self._db() as db:
            db.execute("UPDATE profile_comparisons SET updated_at=? WHERE comparison_id=?", (time.time(), cid))

    def submitted(self, cid, profile, job_id):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT job_id FROM profile_members WHERE comparison_id=? AND profile=?", (cid, profile)).fetchone()
            if old is None or old["job_id"] not in (None, job_id):
                raise ApiError(503, "COMPARISON_BINDING_CHANGED", "comparison", "Child job differs from its durable binding")
            db.execute("UPDATE profile_members SET job_id=? WHERE comparison_id=? AND profile=?", (job_id, cid, profile))
            db.execute("UPDATE profile_comparisons SET state='RUNNING' WHERE comparison_id=? AND state='QUEUED'", (cid,))

    def observed(self, cid, profile, view):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE profile_members SET view_json=? WHERE comparison_id=? AND profile=? AND job_id=?",
                (json.dumps(view, allow_nan=False), cid, profile, view["job_id"])).rowcount
            if changed != 1:
                raise ApiError(503, "COMPARISON_BINDING_CHANGED", "comparison", "Observed child differs")
            db.execute("UPDATE profile_comparisons SET poll_cursor=poll_cursor+1 WHERE comparison_id=?", (cid,))

    def cancelled_member(self, cid, profile):
        with self._db() as db:
            db.execute("UPDATE profile_members SET cancelled=1,view_json=NULL WHERE comparison_id=? AND profile=?", (cid, profile))

    def fail(self, cid, code):
        with self._db() as db:
            db.execute("UPDATE profile_comparisons SET state='CANCEL_REQUESTED',error_code=? WHERE comparison_id=? AND state NOT IN ('COMPLETED','CANCELLED','FAILED')",
                       (code, cid))

    def complete(self, cid, state, outcome):
        with self._db() as db:
            # A concurrently accepted cancellation wins over computed forecasts.
            allowed = "state='CANCEL_REQUESTED'" if state in ("CANCELLED", "FAILED") else "state IN ('QUEUED','RUNNING')"
            return db.execute(f"UPDATE profile_comparisons SET state=?,outcome_json=? WHERE comparison_id=? AND {allowed}",
                (state, json.dumps(outcome, allow_nan=False), cid)).rowcount == 1

    def cancel(self, actor, session, request, cid, installation):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM profile_cancel_requests WHERE actor_id=? AND session_id=? AND request_id=?",
                             (actor, session, request)).fetchone()
            if old is not None:
                self.binding(old, installation)
                if old["comparison_id"] != cid:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "request_id", "Cancellation already targets another comparison")
                return json.loads(old["response_json"])
            row = db.execute("SELECT * FROM profile_comparisons WHERE session_id=? AND comparison_id=?", (session, cid)).fetchone()
            if row is None:
                raise ApiError(404, "COMPARISON_NOT_FOUND", "comparison_id", "Comparison is not in this session")
            self.binding(row, installation)
            immutable = row["state"] in TERMINAL
            data = {"schema_version": "saferoute-m3-profile-cancellation/1", "session_id": session,
                "comparison_id": cid, "status": "COMPLETED_IMMUTABLE" if immutable else "CANCEL_REQUESTED",
                "affects_existing_jobs": False}
            db.execute("INSERT INTO profile_cancel_requests VALUES(?,?,?,?,?,?)",
                       (actor, session, request, cid, installation, json.dumps(data, allow_nan=False)))
            if not immutable:
                db.execute("UPDATE profile_comparisons SET state='CANCEL_REQUESTED' WHERE comparison_id=?", (cid,))
            return data
