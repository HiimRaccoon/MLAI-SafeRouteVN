import sqlite3
from backend.api.errors import ApiError


class OutboxService:
    def __init__(self, settings, repository, gateway, session_service):
        self.settings, self.repository, self.gateway, self.sessions = settings, repository, gateway, session_service

    async def sync(self, session_id):
        self.sessions.session(session_id)
        try:
            summaries = await self.gateway.read_notifications(session_id)
            count = self.repository.ingest(session_id, self.sessions.installation_identity(), summaries)
            for row in self.repository.pending(session_id):
                receipt = await self.gateway.acknowledge(session_id, row["command_id"], row["event_id"])
                self.repository.complete_ack(session_id, row["event_id"], row["command_id"], receipt)
            return count
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "OUTBOX_METADATA_UNAVAILABLE", "notifications", "Notification persistence failed; acknowledgement is pending") from error

    async def sync_many(self, session_ids, summaries=None):
        try:
            return await self._sync_many(session_ids, summaries)
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "OUTBOX_METADATA_UNAVAILABLE", "notifications", "Notification persistence failed; acknowledgement is pending") from error

    async def _sync_many(self, session_ids, summaries=None):
        for sid in session_ids:
            self.sessions.session(sid)
        if summaries is None:
            summaries = await self.gateway.read_notifications_batch(session_ids)
        rows = []
        for sid in session_ids:
            self.repository.ingest(sid, self.sessions.installation_identity(), summaries[sid])
            rows.extend(self.repository.pending(sid))
        if rows:
            receipts = await self.gateway.acknowledge_batch(rows)
            if len(receipts) != len(rows):
                raise ApiError(503, "OUTBOX_ACK_INVALID", "notifications", "Missing batch acknowledgement receipts")
            for row, receipt in zip(rows, receipts):
                self.repository.complete_ack(row["session_id"], row["event_id"], row["command_id"], receipt)

    def list(self, session_id, after=0, limit=100):
        self.sessions.session(session_id)
        try:
            return {"schema_version": "saferoute-m3-notifications/1", "session_id": session_id,
                    "delivery_semantics": "DURABLE_DEDUP_BEFORE_SDK_ACK", **self.repository.list_notifications(session_id, after, limit)}
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "OUTBOX_METADATA_UNAVAILABLE", "notifications", "Notification history unavailable") from error
