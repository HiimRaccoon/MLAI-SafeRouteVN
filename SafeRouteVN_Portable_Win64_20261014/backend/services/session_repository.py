from pathlib import Path
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import time
from uuid import uuid4

from backend.api.errors import ApiError


class SessionRepository:
    """M3 access metadata only. No physical orders/loads/head are stored here."""
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS driver_session_successors(
                source_session_id TEXT PRIMARY KEY, target_session_id TEXT NOT NULL,
                owner_actor_id TEXT NOT NULL, installation_sha256 TEXT NOT NULL)""")
            db.execute("CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, owner_actor_id TEXT NOT NULL)")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
            for name in ("scenario_id", "installation_sha256", "fixture_sha256", "catalog_sha256", "created_at", "status", "run_id"):
                if name not in columns:
                    db.execute(f"ALTER TABLE sessions ADD COLUMN {name} TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS load_requests(
                actor_id TEXT NOT NULL, request_id TEXT NOT NULL, request_sha256 TEXT NOT NULL,
                session_id TEXT NOT NULL UNIQUE, command_id TEXT NOT NULL UNIQUE,
                response_json TEXT, claim_token TEXT, claim_expires REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(actor_id, request_id), FOREIGN KEY(session_id) REFERENCES sessions(session_id))""")

    def healthy(self):
        with self._db() as db:
            db.execute("SELECT session_id FROM sessions LIMIT 1").fetchall()
        return True

    def register(self, session_id: str, actor_id: str):
        with self._db() as db:
            db.execute("INSERT INTO sessions(session_id,owner_actor_id) VALUES(?,?)", (session_id, actor_id))

    def owner(self, session_id: str):
        with self._db() as db:
            row = db.execute("SELECT owner_actor_id FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return row[0] if row else None

    def record(self, session_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise ApiError(404, "SESSION_NOT_FOUND", "session_id", "Session not found")
        return dict(row)

    def reserve_load(self, actor_id, request_id, request_sha256, scenario_id, installation_sha256, fixture_sha256, catalog_sha256, run_id=None):
        now, token = time.time(), uuid4().hex
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM load_requests WHERE actor_id=? AND request_id=?", (actor_id, request_id)).fetchone()
            if row:
                row = dict(row)
                if row["request_sha256"] != request_sha256:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "request_id", "Request ID was already used with different content")
                session = dict(db.execute("SELECT * FROM sessions WHERE session_id=?", (row["session_id"],)).fetchone())
                if (session["installation_sha256"] != installation_sha256 or session["fixture_sha256"] != fixture_sha256
                        or session["catalog_sha256"] != catalog_sha256):
                    raise ApiError(409, "INSTALLATION_BINDING_CHANGED", "session", "Existing request belongs to another server installation")
                if row["response_json"] is not None:
                    return {**row, "session": session, "cached": json.loads(row["response_json"])}
                if row["claim_token"] and row["claim_expires"] > now:
                    raise ApiError(409, "REQUEST_IN_PROGRESS", "request_id", "Load is in progress; retry the same request ID")
            else:
                session = {"session_id": "session-" + uuid4().hex, "owner_actor_id": actor_id, "scenario_id": scenario_id,
                    "installation_sha256": installation_sha256, "fixture_sha256": fixture_sha256, "catalog_sha256": catalog_sha256,
                    "created_at": datetime.now(timezone.utc).isoformat(), "status": "PENDING", "run_id": run_id}
                columns = ",".join(session)
                db.execute(f"INSERT INTO sessions({columns}) VALUES({','.join('?' for _ in session)})", tuple(session.values()))
                row = {"actor_id": actor_id, "request_id": request_id, "request_sha256": request_sha256,
                       "session_id": session["session_id"], "command_id": "m3-bootstrap-" + uuid4().hex}
                db.execute("INSERT INTO load_requests(actor_id,request_id,request_sha256,session_id,command_id) VALUES(?,?,?,?,?)",
                           tuple(row.values()))
            db.execute("UPDATE load_requests SET claim_token=?, claim_expires=? WHERE actor_id=? AND request_id=?",
                       (token, now + 120, actor_id, request_id))
            return {**row, "claim_token": token, "session": session, "cached": None}

    def complete_load(self, reservation, data, *, replace_session_id=None):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if replace_session_id is not None:
                source = db.execute('SELECT rowid,* FROM sessions WHERE session_id=?', (replace_session_id,)).fetchone()
                target = db.execute('SELECT rowid,* FROM sessions WHERE session_id=?', (reservation['session_id'],)).fetchone()
                if (not source or not target or source['owner_actor_id'] != reservation['actor_id']
                        or target['owner_actor_id'] != source['owner_actor_id']
                        or source['installation_sha256'] != target['installation_sha256']
                        or source['status'] != 'READY' or target['rowid'] <= source['rowid']):
                    raise ApiError(409, 'ASSIGNMENT_CONFLICT', 'replace_session_id', 'Source session binding changed')
                existing = db.execute('SELECT target_session_id FROM driver_session_successors WHERE source_session_id=?', (replace_session_id,)).fetchone()
                reset = db.execute("SELECT 1 FROM replay_requests WHERE session_id=? AND operation='reset' AND response_json IS NOT NULL AND error_code IS NULL", (replace_session_id,)).fetchone()
                if existing or reset:
                    raise ApiError(409, 'ASSIGNMENT_CONFLICT', 'replace_session_id', 'Session was already replaced; refresh the admin page')
                db.execute('INSERT INTO driver_session_successors VALUES(?,?,?,?)',
                    (replace_session_id, reservation['session_id'], source['owner_actor_id'], source['installation_sha256']))
            changed = db.execute("""UPDATE load_requests SET response_json=?, claim_token=NULL, claim_expires=0
                WHERE actor_id=? AND request_id=? AND claim_token=? AND claim_expires>? AND response_json IS NULL""",
                (json.dumps(data, allow_nan=False, separators=(",", ":")), reservation["actor_id"], reservation["request_id"],
                 reservation["claim_token"], time.time())).rowcount
            if changed != 1:
                raise ApiError(503, "BOOTSTRAP_LEASE_LOST", "request_id", "Load lease expired; retry the same request ID")
            db.execute("UPDATE sessions SET status='READY' WHERE session_id=?", (reservation["session_id"],))

    def release_load(self, reservation):
        with self._db() as db:
            db.execute("UPDATE load_requests SET claim_token=NULL, claim_expires=0 WHERE actor_id=? AND request_id=? AND claim_token=?",
                       (reservation["actor_id"], reservation["request_id"], reservation["claim_token"]))
