"""Durable receipt of public SDK summaries before authority acknowledgment.

The fake exposes exactly the SDK notification summary and models its stable
acknowledgment receipts. It never invents a delivery or changes a physical head.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.api.errors import ApiError
from backend.api.main import create_app
from backend.services.outbox_repository import OutboxRepository
from backend.services.outbox_service import OutboxService
from backend.tests.test_plans import PlanGateway
from backend.tests.test_sessions import code, headers, load, setup


def notification(session_id, label="one", kind="ADVANCE"):
    return {"event_id": hashlib.sha256((session_id + ":event:" + label).encode()).hexdigest(),
        "session_id": session_id, "kind": kind,
        "body_sha256": hashlib.sha256((session_id + ":body:" + label).encode()).hexdigest()}


class OutboxGateway(PlanGateway):
    def __init__(self, build):
        super().__init__(build)
        self.notifications, self.acknowledged, self.ack_receipts = [], set(), {}
        self.notification_reads, self.ack_calls = [], []
        self.before_ack = None
        self.fail_ack_before_commit = self.fail_ack_after_commit = False
        self.empty_poll = False
        self.corrupt_ack_receipt = None

    async def read_notifications(self, session_id):
        self.notification_reads.append(session_id)
        if self.empty_poll:
            return []
        return deepcopy([event for event in self.notifications if event["session_id"] == session_id
                         and event["event_id"] not in self.acknowledged])

    async def acknowledge(self, session_id, command_id, event_id):
        self.ack_calls.append((session_id, command_id, event_id))
        if self.before_ack:
            self.before_ack(session_id, command_id, event_id)
        if self.fail_ack_before_commit:
            self.fail_ack_before_commit = False
            raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "Ack interrupted before authority commit")
        key = (session_id, command_id)
        if key not in self.ack_receipts:
            self.acknowledged.add(event_id)
            self.ack_receipts[key] = {"status": "ACKNOWLEDGED", "event_id": event_id}
        if self.fail_ack_after_commit:
            self.fail_ack_after_commit = False
            raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "Ack committed but response interrupted")
        if self.corrupt_ack_receipt is not None:
            return deepcopy(self.corrupt_ack_receipt)
        return deepcopy(self.ack_receipts[key])

    async def read_notifications_batch(self, session_ids):
        return {session_id: await self.read_notifications(session_id) for session_id in session_ids}

    async def acknowledge_batch(self, rows):
        return [await self.acknowledge(row["session_id"], row["command_id"], row["event_id"]) for row in rows]


@pytest.fixture
def outbox_setup(setup):
    settings, original = setup
    return settings, OutboxGateway(original.build)


@pytest.fixture
def parts(outbox_setup):
    settings, gateway = outbox_setup
    with TestClient(create_app(settings, gateway=gateway), raise_server_exceptions=False) as client:
        session_id = load(client).json()["data"]["session"]["session_id"]
        repo = OutboxRepository(settings.metadata_path)
        repo.initialize()
        service = OutboxService(settings, repo, gateway, client.app.state.session_service)
        yield settings, gateway, repo, service, session_id, client


def page(repo, session_id, after=0, limit=100):
    return repo.list_notifications(session_id, after=after, limit=limit)


def test_notification_is_committed_before_public_sdk_ack_and_head_is_unchanged(parts):
    settings, gateway, repo, service, session_id, _ = parts
    event = notification(session_id)
    gateway.notifications = [event]
    original_view = deepcopy(gateway.views[session_id])
    observed = []

    def before_ack(sid, command_id, event_id):
        pending = repo.pending(sid)
        assert len(pending) == 1 and pending[0]["event_id"] == event_id
        assert pending[0]["command_id"] == command_id and pending[0]["ack_receipt"] is None
        assert page(repo, sid)["notifications"][0]["body_sha256"] == event["body_sha256"]
        observed.append(event_id)

    gateway.before_ack = before_ack
    asyncio.run(service.sync(session_id))
    assert observed == [event["event_id"]] and repo.pending(session_id) == []
    saved = page(repo, session_id)["notifications"]
    assert len(saved) == 1 and saved[0]["acked_at"] is not None and saved[0]["created_at"]
    assert gateway.views[session_id] == original_view and gateway.computes == []
    assert len(gateway.ack_receipts) == 1


@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
def test_ack_ambiguity_retries_old_command_even_when_next_poll_is_empty(parts, boundary):
    _, gateway, repo, service, session_id, _ = parts
    event = notification(session_id)
    gateway.notifications = [event]
    setattr(gateway, "fail_ack_" + boundary, True)
    with pytest.raises(ApiError) as caught:
        asyncio.run(service.sync(session_id))
    assert caught.value.code == "RUNTIME_TIMEOUT"
    pending = repo.pending(session_id)
    assert len(pending) == 1 and pending[0]["ack_receipt"] is None
    original_command = gateway.ack_calls[0]
    original_cursor = page(repo, session_id)["notifications"][0]["cursor"]
    gateway.empty_poll = True
    asyncio.run(service.sync(session_id))
    assert gateway.ack_calls == [original_command, original_command]
    assert len(gateway.ack_receipts) == 1 and repo.pending(session_id) == []
    saved = page(repo, session_id)
    assert len(saved["notifications"]) == 1 and saved["notifications"][0]["cursor"] == original_cursor


def test_restart_retains_summary_and_stable_pending_ack_command(outbox_setup):
    settings, gateway = outbox_setup
    with TestClient(create_app(settings, gateway=gateway)) as first:
        session_id = load(first).json()["data"]["session"]["session_id"]
        repo = OutboxRepository(settings.metadata_path)
        repo.initialize()
        event = notification(session_id)
        gateway.notifications = [event]
        service = OutboxService(settings, repo, gateway, first.app.state.session_service)
        gateway.fail_ack_after_commit = True
        with pytest.raises(ApiError):
            asyncio.run(service.sync(session_id))
        original = gateway.ack_calls[0]
        original_cursor = page(repo, session_id)["notifications"][0]["cursor"]
    with TestClient(create_app(settings, gateway=gateway)) as restarted:
        new_repo = OutboxRepository(settings.metadata_path)
        new_repo.initialize()
        new_service = OutboxService(settings, new_repo, gateway, restarted.app.state.session_service)
        asyncio.run(new_service.sync(session_id))
        assert new_repo.pending(session_id) == []
        assert page(new_repo, session_id)["notifications"][0]["cursor"] == original_cursor
    assert gateway.ack_calls == [original, original] and len(gateway.ack_receipts) == 1


def test_local_store_failure_prevents_acknowledgment(parts, monkeypatch):
    _, gateway, repo, service, session_id, _ = parts
    gateway.notifications = [notification(session_id)]

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("Injected local inbox write interruption")

    monkeypatch.setattr(repo, "ingest", unavailable)
    with pytest.raises(ApiError) as caught:
        asyncio.run(service.sync(session_id))
    assert caught.value.status_code == 503
    assert gateway.ack_calls == [] and gateway.ack_receipts == {}
    assert page(repo, session_id)["notifications"] == []


def test_duplicate_poll_does_not_duplicate_updates_or_emit_ack_loop(parts):
    _, gateway, repo, service, session_id, _ = parts
    event = notification(session_id)
    gateway.notifications = [event, deepcopy(event)]
    asyncio.run(service.sync(session_id))
    first = page(repo, session_id)
    assert len(first["notifications"]) == 1 and len(gateway.ack_receipts) == 1
    asyncio.run(service.sync(session_id))
    assert page(repo, session_id) == first and len(gateway.ack_calls) == 1
    assert gateway.notifications == [event, event]


@pytest.mark.parametrize("receipt", [None, {}, {"status": "ACKNOWLEDGED"},
    {"status": "ACKNOWLEDGED", "event_id": "wrong"}, {"status": "OTHER", "event_id": "wrong"}])
def test_wrong_ack_projection_never_marks_metadata_acked(parts, receipt):
    _, gateway, repo, service, session_id, _ = parts
    event = notification(session_id)
    gateway.notifications = [event]
    # A sentinel wrapper can return None as a malformed SDK response too.
    original_ack = gateway.acknowledge

    async def malformed(sid, command_id, event_id):
        await original_ack(sid, command_id, event_id)
        return deepcopy(receipt)

    gateway.acknowledge = malformed
    with pytest.raises(ApiError) as caught:
        asyncio.run(service.sync(session_id))
    assert caught.value.status_code == 503
    assert len(repo.pending(session_id)) == 1 and page(repo, session_id)["notifications"][0]["acked_at"] is None
    original = gateway.ack_calls[0]
    gateway.acknowledge = original_ack
    asyncio.run(service.sync(session_id))
    assert gateway.ack_calls == [original, original] and repo.pending(session_id) == []


def test_source_change_blocks_notification_poll_and_ack_before_any_sdk_calls(parts):
    settings, gateway, _, service, session_id, _ = parts
    gateway.notifications = [notification(session_id)]
    path = settings.project_root / "scenarios/fixtures/thu-duc-binh-thanh-v1/S0.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ApiError) as caught:
        asyncio.run(service.sync(session_id))
    assert caught.value.code == "SOURCE_CHANGED"
    assert gateway.notification_reads == [] and gateway.ack_calls == []


def test_repository_deduplicates_immutable_summary_and_cursor(parts):
    settings, _, repo, _, session_id, _ = parts
    event = notification(session_id)
    assert repo.ingest(session_id, settings.installation_identity(), [event, deepcopy(event)]) == 1
    original = page(repo, session_id)
    original_command = repo.pending(session_id)[0]["command_id"]
    assert repo.ingest(session_id, settings.installation_identity(), [event]) == 0
    assert page(repo, session_id) == original and repo.pending(session_id)[0]["command_id"] == original_command


@pytest.mark.parametrize("field,value", [("kind", "ACTIVATE"), ("body_sha256", "f" * 64)])
def test_existing_event_id_cannot_change_committed_summary(parts, field, value):
    settings, _, repo, _, session_id, _ = parts
    event = notification(session_id)
    repo.ingest(session_id, settings.installation_identity(), [event])
    original = page(repo, session_id)
    altered = {**event, field: value}
    with pytest.raises(ApiError) as caught:
        repo.ingest(session_id, settings.installation_identity(), [altered])
    assert caught.value.code == "OUTBOX_CONFLICT" and page(repo, session_id) == original


@pytest.mark.parametrize("change", [
    {"event_id": "bad"}, {"event_id": "A" * 64}, {"event_id": 42}, {"body_sha256": None},
    {"body_sha256": {}}, {"kind": None}, {"kind": {}}, {"session_id": "another-session"},
    {"private_body": {"token": "secret"}},
])
def test_malformed_notification_batch_is_atomic_and_has_no_pending_ack(parts, change):
    settings, gateway, repo, _, session_id, _ = parts
    first = notification(session_id, "valid-first")
    second = {**notification(session_id, "invalid-second"), **change}
    with pytest.raises(ApiError):
        repo.ingest(session_id, settings.installation_identity(), [first, second])
    assert page(repo, session_id)["notifications"] == [] and repo.pending(session_id) == []
    assert gateway.ack_calls == []


def test_conflicting_duplicate_within_batch_rolls_back_every_summary(parts):
    settings, _, repo, _, session_id, _ = parts
    event = notification(session_id)
    with pytest.raises(ApiError) as caught:
        repo.ingest(session_id, settings.installation_identity(), [event, {**event, "body_sha256": "e" * 64}])
    assert caught.value.code == "OUTBOX_CONFLICT"
    assert page(repo, session_id)["notifications"] == [] and repo.pending(session_id) == []


def test_concurrent_ingest_assigns_one_cursor_per_distinct_event(parts):
    settings, _, repo, _, session_id, _ = parts
    batch = [notification(session_id, str(number)) for number in range(3)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        counts = list(executor.map(lambda _: repo.ingest(session_id, settings.installation_identity(), batch), range(4)))
    assert sum(counts) == 3
    saved = page(repo, session_id)["notifications"]
    assert len(saved) == 3 and len({str(row["cursor"]) for row in saved}) == 3
    assert len({row["command_id"] for row in repo.pending(session_id)}) == 3


def test_concurrent_sync_uses_same_committed_ack_commands_without_duplicate_events(parts):
    _, gateway, repo, service, session_id, _ = parts
    gateway.notifications = [notification(session_id)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: asyncio.run(service.sync(session_id)), range(2)))
    assert len(page(repo, session_id)["notifications"]) == 1 and repo.pending(session_id) == []
    assert len(gateway.ack_receipts) == 1
    assert len(set(gateway.ack_calls)) == 1


def test_cursor_pagination_is_ordered_stable_and_scoped_to_owned_session(parts):
    settings, _, repo, _, session_id, _ = parts
    rows = [notification(session_id, str(number)) for number in range(5)]
    repo.ingest(session_id, settings.installation_identity(), rows)
    other = "another-session"
    repo.ingest(other, settings.installation_identity(), [notification(other)])
    first = page(repo, session_id, limit=2)
    assert first["has_more"] is True and len(first["notifications"]) == 2
    second = page(repo, session_id, after=int(first["next_cursor"]), limit=2)
    last = page(repo, session_id, after=int(second["next_cursor"]), limit=2)
    events = [row["event_id"] for result in (first, second, last) for row in result["notifications"]]
    assert events == [row["event_id"] for row in rows]
    assert last["has_more"] is False
    assert all(row["event_id"] != notification(other)["event_id"] for result in (first, second, last) for row in result["notifications"])
    assert page(repo, session_id, after=int(last["next_cursor"]))["notifications"] == []


def test_complete_ack_checks_stored_command_and_receipt_before_updating(parts):
    settings, _, repo, _, session_id, _ = parts
    event = notification(session_id)
    repo.ingest(session_id, settings.installation_identity(), [event])
    row = repo.pending(session_id)[0]
    good = {"status": "ACKNOWLEDGED", "event_id": event["event_id"]}
    with pytest.raises(ApiError):
        repo.complete_ack(session_id, event["event_id"], "wrong-command", good)
    assert len(repo.pending(session_id)) == 1
    repo.complete_ack(session_id, event["event_id"], row["command_id"], good)
    committed = page(repo, session_id)
    repo.complete_ack(session_id, event["event_id"], row["command_id"], good)
    assert page(repo, session_id) == committed and repo.pending(session_id) == []


def test_batch_database_failure_rolls_back_summaries_before_any_authority_ack(parts):
    settings, gateway, repo, service, session_id, _ = parts
    first, second = notification(session_id, "first"), notification(session_id, "second")
    gateway.notifications = [first, second]
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("CREATE TRIGGER reject_second_notification BEFORE INSERT ON notifications "
                   "WHEN NEW.event_id='" + second["event_id"] + "' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(ApiError) as caught:
        asyncio.run(service.sync(session_id))
    assert caught.value.code == "OUTBOX_METADATA_UNAVAILABLE"
    assert page(repo, session_id)["notifications"] == [] and repo.pending(session_id) == []
    assert gateway.ack_calls == [] and gateway.ack_receipts == {}
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("DROP TRIGGER reject_second_notification")
    asyncio.run(service.sync(session_id))
    assert len(page(repo, session_id)["notifications"]) == 2 and len(gateway.ack_receipts) == 2


def test_http_read_is_owner_scoped_public_cursor_summary_and_never_polls_or_acknowledges(parts):
    settings, gateway, repo, _, session_id, client = parts
    event = notification(session_id)
    repo.ingest(session_id, settings.installation_identity(), [event])
    response = client.get(f"/api/sessions/{session_id}/notifications", headers=headers())
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["schema_version"] == "saferoute-m3-notifications/1"
    assert data["session_id"] == session_id and data["delivery_semantics"] == "DURABLE_DEDUP_BEFORE_SDK_ACK"
    item = data["notifications"][0]
    assert set(item) == {"cursor", "session_id", "event_id", "kind", "body_sha256", "created_at", "acked_at", "status"}
    assert item["status"] == "PENDING_ACK" and item["acked_at"] is None
    assert isinstance(item["cursor"], str) and item["cursor"].isdecimal()
    assert data["next_cursor"] == item["cursor"] and data["has_more"] is False
    assert gateway.notification_reads == [] and gateway.ack_calls == []
    assert len(repo.pending(session_id)) == 1


@pytest.mark.parametrize("actor", ["bob", "viewer"])
def test_http_foreign_owner_cannot_read_notifications_before_sdk_or_metadata_access(parts, actor, monkeypatch):
    _, gateway, repo, _, session_id, client = parts

    def forbidden_read(*args, **kwargs):
        raise AssertionError("Notification rows were read before owner authorization")

    monkeypatch.setattr(client.app.state.outbox_repository, "list_notifications", forbidden_read)
    code(client.get(f"/api/sessions/{session_id}/notifications", headers=headers(actor)), 403, "FORBIDDEN")
    assert gateway.notification_reads == [] and gateway.ack_calls == []


def test_owned_viewer_can_read_notifications_without_authority_mutation(parts):
    settings, gateway, repo, _, session_id, client = parts
    repo.ingest(session_id, settings.installation_identity(), [notification(session_id)])
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("UPDATE sessions SET owner_actor_id='viewer' WHERE session_id=?", (session_id,))
    response = client.get(f"/api/sessions/{session_id}/notifications", headers=headers("viewer"))
    assert response.status_code == 200, response.text
    assert len(response.json()["data"]["notifications"]) == 1
    assert gateway.notification_reads == [] and gateway.ack_calls == []


def test_http_missing_auth_and_session_are_typed_and_do_not_invoke_sdk(parts):
    _, gateway, _, _, session_id, client = parts
    code(client.get(f"/api/sessions/{session_id}/notifications"), 401, "UNAUTHORIZED")
    code(client.get("/api/sessions/missing/notifications", headers=headers()), 404, "SESSION_NOT_FOUND")
    assert gateway.notification_reads == [] and gateway.ack_calls == []


@pytest.mark.parametrize("query", [
    "after=-1", "after=01", "after=1.0", "after=+1", "after=9223372036854775808",
    "after=99999999999999999999", "limit=0", "limit=501", "limit=nan",
])
def test_http_cursor_and_limit_reject_noncanonical_or_out_of_range_values(parts, query):
    _, gateway, _, _, session_id, client = parts
    response = client.get(f"/api/sessions/{session_id}/notifications?{query}", headers=headers())
    assert response.status_code == 422, response.text
    assert gateway.notification_reads == [] and gateway.ack_calls == []


def test_http_read_verifies_fixture_source_without_returning_unbound_history(parts):
    settings, gateway, repo, _, session_id, client = parts
    repo.ingest(session_id, settings.installation_identity(), [notification(session_id)])
    path = settings.project_root / "scenarios/fixtures/thu-duc-binh-thanh-v1/S0.json"
    path.write_bytes(path.read_bytes() + b" ")
    code(client.get(f"/api/sessions/{session_id}/notifications", headers=headers()), 503, "SOURCE_CHANGED")
    assert len(repo.pending(session_id)) == 1
    assert gateway.notification_reads == [] and gateway.ack_calls == []


def test_batch_sync_stores_all_session_summaries_before_first_sdk_ack(parts):
    _, gateway, repo, service, session_id, client = parts
    other_id = load(client, "S1", "load-other").json()["data"]["session"]["session_id"]
    gateway.notifications = [notification(session_id), notification(other_id)]
    initial_views = deepcopy(gateway.views)

    def both_durable(sid, command_id, event_id):
        assert len(page(repo, session_id)["notifications"]) == 1
        assert len(page(repo, other_id)["notifications"]) == 1
        assert any(row["command_id"] == command_id and row["event_id"] == event_id for row in repo.pending(sid))

    gateway.before_ack = both_durable
    asyncio.run(service.sync_many([session_id, other_id]))
    assert repo.pending(session_id) == repo.pending(other_id) == []
    assert len(gateway.ack_receipts) == 2 and gateway.views == initial_views


def test_partial_ack_batch_failure_retries_only_pending_original_commands(parts):
    _, gateway, repo, service, session_id, _ = parts
    gateway.notifications = [notification(session_id, str(number)) for number in range(3)]
    original_ack = gateway.acknowledge
    calls = 0

    async def interrupted_second(sid, command_id, event_id):
        nonlocal calls
        calls += 1
        receipt = await original_ack(sid, command_id, event_id)
        if calls == 2:
            raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "Second ack committed but response interrupted")
        return receipt

    gateway.acknowledge = interrupted_second
    with pytest.raises(ApiError):
        asyncio.run(service.sync(session_id))
    stored = page(repo, session_id)["notifications"]
    assert [row["status"] for row in stored] == ["ACKNOWLEDGED", "PENDING_ACK", "PENDING_ACK"]
    pending_commands = [(row["session_id"], row["command_id"], row["event_id"]) for row in repo.pending(session_id)]
    gateway.acknowledge = original_ack
    gateway.empty_poll = True
    asyncio.run(service.sync(session_id))
    assert gateway.ack_calls[2:] == pending_commands
    assert repo.pending(session_id) == [] and len(gateway.ack_receipts) == 3
    assert [row["cursor"] for row in page(repo, session_id)["notifications"]] == [row["cursor"] for row in stored]


def test_batch_ack_metadata_failure_is_typed_and_replays_the_stored_authority_command(parts):
    settings, gateway, repo, service, session_id, _ = parts
    gateway.notifications = [notification(session_id)]
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("CREATE TRIGGER reject_local_ack BEFORE UPDATE ON notifications "
                   "BEGIN SELECT RAISE(ABORT, 'injected ack persistence failure'); END")
    with pytest.raises(ApiError) as caught:
        asyncio.run(service.sync_many([session_id]))
    assert caught.value.code == "OUTBOX_METADATA_UNAVAILABLE"
    pending = repo.pending(session_id)
    assert len(pending) == 1 and pending[0]["ack_receipt"] is None
    assert len(gateway.ack_receipts) == 1
    original = gateway.ack_calls[0]
    cursor = page(repo, session_id)["next_cursor"]
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("DROP TRIGGER reject_local_ack")
    asyncio.run(service.sync_many([session_id]))
    assert gateway.ack_calls == [original, original] and len(gateway.ack_receipts) == 1
    assert repo.pending(session_id) == [] and page(repo, session_id)["next_cursor"] == cursor
