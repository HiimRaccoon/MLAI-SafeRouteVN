"""Durable scheduling metadata only; physical advance is a normal M2 command.

A pending tick and its replay_requests row are reserved in one transaction.
Pausing fences NEW reservations. A reserved tick may already have committed in
M2 and must settle using its exact original command, basis and target.
"""
from datetime import datetime, timezone
import hashlib
import json
import time
from uuid import uuid4

from backend.api.errors import ApiError
from backend.models.playback import PLAYBACK_SPEEDS
from .replay_repository import ReplayRepository


class PlaybackRepository(ReplayRepository):
    def initialize(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS playback_controllers(
                session_id TEXT PRIMARY KEY, installation_sha256 TEXT NOT NULL,
                actor_id TEXT NOT NULL, epoch INTEGER NOT NULL DEFAULT 0,
                speed INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL,
                reason TEXT NOT NULL, basis_json TEXT, end_time TEXT,
                next_due REAL NOT NULL DEFAULT 0, pending_actor TEXT,
                pending_request TEXT, updated_at TEXT NOT NULL)""")
            if 'auto_deliver' not in {r[1] for r in db.execute('PRAGMA table_info(playback_controllers)')}:
                db.execute('ALTER TABLE playback_controllers ADD COLUMN auto_deliver INTEGER NOT NULL DEFAULT 0')
            db.execute("""CREATE TABLE IF NOT EXISTS playback_controls(
                actor_id TEXT NOT NULL, session_id TEXT NOT NULL,
                operation TEXT NOT NULL, request_id TEXT NOT NULL,
                request_sha256 TEXT NOT NULL, installation_sha256 TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                PRIMARY KEY(actor_id,session_id,operation,request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS playback_audit(
                receipt_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                recorded_at TEXT NOT NULL, receipt_json TEXT NOT NULL)""")

    def healthy(self):
        with self._db() as db:
            db.execute("SELECT session_id FROM playback_controllers LIMIT 1").fetchall()
            db.execute("SELECT request_id FROM playback_controls LIMIT 1").fetchall()
            db.execute("SELECT receipt_id FROM playback_audit LIMIT 1").fetchall()
        return True

    @staticmethod
    def stamp():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def state(row):
        if row is None:
            return None
        value = dict(row)
        value["basis"] = json.loads(value["basis_json"]) if value["basis_json"] else None
        return value

    @staticmethod
    def public(row):
        if row is None:
            return {"mode": "STEP", "paused": True, "fully_paused": True,
                "in_flight": False, "reason": "NOT_STARTED", "speed": 1,
                "controller_revision": "0", "next_tick_at": None, "end_time": None, "auto_deliver": False}
        pending = row["pending_request"] is not None
        running = row["status"] == "RUNNING"
        return {"mode": "AUTOMATIC" if running else "STEP", "paused": not running,
            "fully_paused": not running and not pending, "in_flight": pending,
            "reason": row["reason"], "speed": row["speed"], "auto_deliver": bool(row["auto_deliver"]),
            "controller_revision": str(row["epoch"]),
            "next_tick_at": datetime.fromtimestamp(row["next_due"], timezone.utc).isoformat() if running else None,
            "end_time": row["end_time"]}

    def get(self, session_id, installation=None):
        with self._db() as db:
            row = db.execute("SELECT * FROM playback_controllers WHERE session_id=?", (session_id,)).fetchone()
        if row is not None and installation is not None and row["installation_sha256"] != installation:
            raise ApiError(409, "INSTALLATION_BINDING_CHANGED", "playback", "Playback belongs to another server installation")
        return self.state(row)

    def find_control(self, actor, session_id, operation, request_id, digest, installation):
        with self._db() as db:
            row = db.execute("SELECT * FROM playback_controls WHERE actor_id=? AND session_id=? AND operation=? AND request_id=?",
                (actor, session_id, operation, request_id)).fetchone()
        if row is not None:
            self.control_binding(row, digest, installation)
            return json.loads(row["receipt_json"])

    @staticmethod
    def control_binding(row, digest, installation):
        if row["request_sha256"] != digest:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "request_id", "Playback request ID was already used with different content")
        if row["installation_sha256"] != installation:
            raise ApiError(409, "INSTALLATION_BINDING_CHANGED", "playback", "Control belongs to another installation")

    @staticmethod
    def audit(db, session_id, event, *, details=None):
        receipt = {"schema_version": "saferoute-m3-playback-receipt/1",
            "receipt_id": "playback-" + uuid4().hex, "session_id": session_id,
            "operation": event, "source": "M3_PLAYBACK_CONTROL",
            "recorded_at": PlaybackRepository.stamp(), **(details or {})}
        db.execute("INSERT INTO playback_audit VALUES(?,?,?,?)",
            (receipt["receipt_id"], session_id, receipt["recorded_at"], json.dumps(receipt, allow_nan=False)))
        return receipt

    def control(self, actor, session_id, operation, request_id, digest, installation, *, speed=None, basis=None, end_time=None, now=None, enabled=None):
        now = time.time() if now is None else now
        keys = (actor, session_id, operation, request_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM playback_controls WHERE actor_id=? AND session_id=? AND operation=? AND request_id=?", keys).fetchone()
            if previous is not None:
                self.control_binding(previous, digest, installation)
                return json.loads(previous["receipt_json"])
            row = db.execute("SELECT * FROM playback_controllers WHERE session_id=?", (session_id,)).fetchone()
            if row is not None and row["installation_sha256"] != installation:
                raise ApiError(409, "INSTALLATION_BINDING_CHANGED", "playback", "Controller belongs to another installation")
            if operation not in ("start", "speed", "pause", "auto_deliver"):
                raise ValueError("Unknown playback control")
            if operation == "start" and row is not None and row["pending_request"]:
                raise ApiError(409, "PLAYBACK_TICK_IN_PROGRESS", "playback", "Reserved advance must settle before a new start")
            if row is None:
                db.execute("""INSERT INTO playback_controllers(session_id,installation_sha256,actor_id,status,reason,updated_at)
                    VALUES(?,?,?,'PAUSED','NOT_STARTED',?)""", (session_id, installation, actor, self.stamp()))
            if operation == "start":
                if basis is None or end_time is None or speed not in PLAYBACK_SPEEDS:
                    raise ValueError("Start requires validated basis, endpoint and speed")
                db.execute("""UPDATE playback_controllers SET actor_id=?,epoch=epoch+1,speed=?,status='RUNNING',reason='RUNNING',
                    basis_json=?,end_time=?,next_due=?,updated_at=? WHERE session_id=?""",
                    (actor, speed, json.dumps(basis, allow_nan=False), end_time, now + 1 / min(speed, 8), self.stamp(), session_id))
            elif operation == "speed":
                if speed not in PLAYBACK_SPEEDS:
                    raise ValueError("Unsupported playback speed")
                db.execute("UPDATE playback_controllers SET epoch=epoch+1,speed=?,next_due=?,updated_at=? WHERE session_id=?",
                    (speed, now + 1 / min(speed, 8), self.stamp(), session_id))
            elif operation == 'auto_deliver':
                if type(enabled) is not bool:
                    raise ValueError('Auto Deliver requires a boolean')
                db.execute('UPDATE playback_controllers SET epoch=epoch+1,auto_deliver=?,updated_at=? WHERE session_id=?',
                    (int(enabled), self.stamp(), session_id))
            else:
                db.execute("UPDATE playback_controllers SET epoch=epoch+1,status='PAUSED',reason='USER_PAUSE',next_due=0,updated_at=? WHERE session_id=?",
                    (self.stamp(), session_id))
            current = db.execute("SELECT * FROM playback_controllers WHERE session_id=?", (session_id,)).fetchone()
            receipt = self.audit(db, session_id, operation, details={"controller": self.public(current),
                "request_id": request_id, "actor_id": actor})
            db.execute("INSERT INTO playback_controls VALUES(?,?,?,?,?,?,?)", (*keys, digest, installation, json.dumps(receipt, allow_nan=False)))
            return receipt

    def stop(self, session_id, reason):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM playback_controllers WHERE session_id=?", (session_id,)).fetchone()
            if row is None or (row["status"] == "PAUSED" and row["reason"] == reason):
                return
            db.execute("UPDATE playback_controllers SET epoch=epoch+1,status='PAUSED',reason=?,next_due=0,updated_at=? WHERE session_id=?",
                (reason, self.stamp(), session_id))
            self.audit(db, session_id, "stop", details={"reason": reason, "in_flight": row["pending_request"] is not None})

    def fence_restart(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT session_id,pending_request FROM playback_controllers WHERE status='RUNNING'").fetchall()
            for row in rows:
                db.execute("UPDATE playback_controllers SET epoch=epoch+1,status='PAUSED',reason='PAUSED_WORKER_RESTART',next_due=0,updated_at=? WHERE session_id=?",
                    (self.stamp(), row["session_id"]))
                self.audit(db, row["session_id"], "stop", details={"reason": "PAUSED_WORKER_RESTART", "in_flight": row["pending_request"] is not None})

    def candidate(self, now=None):
        now = time.time() if now is None else now
        with self._db() as db:
            # Settle pending commands before starting another session's tick.
            row = db.execute("""SELECT * FROM playback_controllers WHERE next_due<=? AND
                (pending_request IS NOT NULL OR status='RUNNING') ORDER BY CASE WHEN pending_request IS NOT NULL THEN 0 ELSE 1 END,
                next_due,session_id LIMIT 1""", (now,)).fetchone()
        return self.state(row)

    def pending(self, state):
        if state["pending_request"] is None:
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM replay_requests WHERE actor_id=? AND session_id=? AND operation IN ('advance','deliver') AND request_id=?",
                (state["pending_actor"], state["session_id"], state["pending_request"])).fetchone()
        if row is None:
            raise ApiError(503, "PLAYBACK_METADATA_INVALID", "playback", "Reserved playback command is missing")
        return ReplayRepository.decoded(row)

    def reserve_tick(self, state, basis, target, current_time, *, stop_reason=None, now=None, delivery=None):
        now = time.time() if now is None else now
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute("SELECT * FROM playback_controllers WHERE session_id=?", (state["session_id"],)).fetchone()
            if latest is None or latest["status"] != "RUNNING" or latest["epoch"] != state["epoch"] or latest["pending_request"] or latest["next_due"] > now:
                return None  # Pause/speed/start won the metadata CAS before reservation.
            if json.loads(latest["basis_json"]) != basis:
                raise ApiError(409, "STALE_HEAD", "playback", "Playback input basis changed")
            if delivery is not None and not latest['auto_deliver']:
                return None
            request = "autoplay-" + uuid4().hex
            command, mutation = "m3-advance-" + uuid4().hex, "replay-" + uuid4().hex
            payload = {"target_time": target, "current_time": current_time,
                "playback_stop_reason": stop_reason}
            if delivery is not None:
                payload['delivery'] = delivery
            digest = hashlib.sha256(json.dumps({"basis": basis, "target_time": target}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            db.execute("""INSERT INTO replay_requests(actor_id,session_id,operation,request_id,request_sha256,
                installation_sha256,command_id,mutation_id,basis_json,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (latest["actor_id"], state["session_id"], 'deliver' if delivery is not None else 'advance', request, digest,
                    latest["installation_sha256"], command, mutation, json.dumps(basis, allow_nan=False), json.dumps(payload, allow_nan=False), now))
            db.execute("UPDATE playback_controllers SET pending_actor=?,pending_request=?,updated_at=? WHERE session_id=?",
                (latest["actor_id"], request, self.stamp(), state["session_id"]))
            return ReplayRepository.decoded(db.execute("SELECT * FROM replay_requests WHERE command_id=?", (command,)).fetchone())

    def settle(self, state, row, *, now=None):
        now = time.time() if now is None else now
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM playback_controllers WHERE session_id=?", (state["session_id"],)).fetchone()
            if current is None or current["pending_actor"] != row["actor_id"] or current["pending_request"] != row["request_id"]:
                return False
            saved = db.execute("SELECT * FROM replay_requests WHERE actor_id=? AND session_id=? AND operation IN ('advance','deliver') AND request_id=?",
                (row["actor_id"], row["session_id"], row["request_id"])).fetchone()
            if saved["response_json"] is None and saved["error_code"] is None:
                return False
            response = json.loads(saved["response_json"]) if saved["response_json"] else None
            reason = saved["error_code"] or row["payload"].get("playback_stop_reason")
            status, final_reason = current["status"], current["reason"]
            if reason and status == "RUNNING":
                status, final_reason = "PAUSED", reason
            basis = json.dumps(response["basis"], allow_nan=False) if response else current["basis_json"]
            db.execute("""UPDATE playback_controllers SET basis_json=?,pending_actor=NULL,pending_request=NULL,
                status=?,reason=?,next_due=?,updated_at=? WHERE session_id=?""",
                (basis, status, final_reason, now + 1 / min(current["speed"], 8) if status == "RUNNING" else 0, self.stamp(), state["session_id"]))
            self.audit(db, state["session_id"], "tick_settled", details={"mutation_id": saved["mutation_id"],
                "status": response["status"] if response else "REJECTED", "reason": reason,
                "target_time": row["payload"]["target_time"], "controller": self.public(db.execute("SELECT * FROM playback_controllers WHERE session_id=?", (state["session_id"],)).fetchone())})
            return True

    def backoff(self, session_id, seconds=2):
        with self._db() as db:
            db.execute("UPDATE playback_controllers SET next_due=?,updated_at=? WHERE session_id=?", (time.time() + seconds, self.stamp(), session_id))

    def history(self, session_id):
        with self._db() as db:
            rows = db.execute("SELECT receipt_json FROM playback_audit WHERE session_id=? ORDER BY recorded_at DESC,receipt_id DESC LIMIT 100", (session_id,)).fetchall()
        return [json.loads(row["receipt_json"]) for row in rows]
