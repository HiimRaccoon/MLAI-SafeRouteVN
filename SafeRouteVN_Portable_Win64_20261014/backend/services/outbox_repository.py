"""M3 durable inbox for the SDK outbox; commit before acknowledgement."""
from datetime import datetime, timezone
import json
import re
from uuid import uuid4

from backend.api.errors import ApiError
from .session_repository import SessionRepository


class OutboxRepository(SessionRepository):
    def initialize(self):
        super().initialize()
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS notifications (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT, installation_sha256 TEXT NOT NULL,
                session_id TEXT NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL,
                body_sha256 TEXT NOT NULL, command_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, acked_at TEXT, ack_receipt_json TEXT,
                UNIQUE(installation_sha256, session_id, event_id))""")

    @staticmethod
    def validate(session_id, item):
        if (not isinstance(item, dict) or set(item) != {"session_id", "event_id", "kind", "body_sha256"}
                or item["session_id"] != session_id
                or not isinstance(item["kind"], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", item["kind"])
                or any(not isinstance(item[k], str) or not re.fullmatch(r"[a-f0-9]{64}", item[k]) for k in ("event_id", "body_sha256"))):
            raise ApiError(503, "OUTBOX_INVALID", "notifications", "Invalid SDK notification summary")

    def ingest(self, session_id, installation_id, notifications):
        if not isinstance(notifications, list):
            raise ApiError(503, "OUTBOX_INVALID", "notifications", "Invalid SDK notification list")
        count = 0
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in notifications:
                self.validate(session_id, item)
                old = db.execute("SELECT * FROM notifications WHERE installation_sha256=? AND session_id=? AND event_id=?",
                                 (installation_id, session_id, item["event_id"])).fetchone()
                if old:
                    if old["kind"] != item["kind"] or old["body_sha256"] != item["body_sha256"]:
                        raise ApiError(503, "OUTBOX_CONFLICT", "notifications", "Stored notification hash or kind changed")
                    continue
                db.execute("INSERT INTO notifications(installation_sha256,session_id,event_id,kind,body_sha256,command_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (installation_id, session_id, item["event_id"], item["kind"], item["body_sha256"],
                     "m3-ack-" + uuid4().hex, datetime.now(timezone.utc).isoformat()))
                count += 1
        return count

    @staticmethod
    def decode(row):
        value = dict(row)
        encoded = value.pop("ack_receipt_json")
        value["ack_receipt"] = json.loads(encoded) if encoded else None
        return value

    def pending(self, session_id):
        with self._db() as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM notifications WHERE session_id=? AND ack_receipt_json IS NULL ORDER BY cursor", (session_id,))]

    def complete_ack(self, session_id, event_id, command_id, receipt):
        if receipt != {"status": "ACKNOWLEDGED", "event_id": event_id}:
            raise ApiError(503, "OUTBOX_ACK_INVALID", "notifications", "Invalid SDK acknowledgement receipt")
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        with self._db() as db:
            row = db.execute("SELECT * FROM notifications WHERE session_id=? AND event_id=? AND command_id=?", (session_id, event_id, command_id)).fetchone()
            if row is None or (row["ack_receipt_json"] is not None and row["ack_receipt_json"] != encoded):
                raise ApiError(503, "OUTBOX_ACK_CONFLICT", "notifications", "Acknowledgement has no matching durable notification")
            db.execute("UPDATE notifications SET ack_receipt_json=?,acked_at=COALESCE(acked_at,?) WHERE cursor=?",
                       (encoded, datetime.now(timezone.utc).isoformat(), row["cursor"]))

    def list_notifications(self, session_id, after=0, limit=100):
        if type(after) is not int or not 0 <= after < (1 << 63) or type(limit) is not int or not 1 <= limit <= 500:
            raise ApiError(422, "INVALID_CURSOR", "after", "Canonical nonnegative int64 cursor and limit 1..500 required")
        with self._db() as db:
            rows = db.execute("SELECT * FROM notifications WHERE session_id=? AND cursor>? ORDER BY cursor LIMIT ?", (session_id, after, limit + 1)).fetchall()
        items = [{"cursor": str(r["cursor"]), "session_id": r["session_id"], "event_id": r["event_id"], "kind": r["kind"],
                  "body_sha256": r["body_sha256"], "created_at": r["created_at"], "acked_at": r["acked_at"],
                  "status": "ACKNOWLEDGED" if r["ack_receipt_json"] else "PENDING_ACK"} for r in rows[:limit]]
        return {"notifications": items, "next_cursor": items[-1]["cursor"] if items else str(after), "has_more": len(rows) > limit}

    def healthy(self):
        with self._db() as db:
            db.execute("SELECT cursor,ack_receipt_json FROM notifications LIMIT 1").fetchall()
        return True
