import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.dependencies import session_owner
from backend.api.errors import ApiError
from backend.api.main import create_app
from backend.services.compute_worker import ComputeWorker
from backend.services.auth import Actor
from backend.tests.test_sessions import load, setup
from backend.tests.test_worker import worker_setup, seed_job
from backend.tests.test_recovery import recovery_row


def saved_session(settings, gateway):
    with TestClient(create_app(settings, gateway=gateway)) as client:
        return load(client).json()['data']['session']['session_id']


def test_idle_history_does_not_delay_startup_and_is_fenced_until_opened(worker_setup):
    settings, gateway = worker_setup
    sid = saved_session(settings, gateway)
    settings = replace(settings, deferred_session_recovery=True)

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.status == 'READY'
            assert gateway.recoveries == []
            assert recovery_row(settings, sid)['status'] == 'DEFERRED'
            assert worker.recovery.healthy()
            assert sid not in worker.outbox_poll_batch()
            with pytest.raises(ApiError, match='Session writes blocked'):
                worker.recovery.assert_mutation_allowed(sid)
            assert worker.recovery.request_verification(sid) is False
            assert await worker.run_once() is True
            assert len(gateway.recoveries) == 1
            assert recovery_row(settings, sid)['status'] == 'VERIFIED'
            worker.recovery.assert_mutation_allowed(sid)
            assert worker.recovery.request_verification(sid) is True
        finally:
            await worker.shutdown()
    asyncio.run(scenario())


def test_unfinished_job_is_recovered_before_readiness_even_in_deferred_mode(worker_setup):
    settings, gateway = worker_setup
    sid, _ = seed_job(settings, gateway)
    settings = replace(settings, deferred_session_recovery=True)

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.status == 'READY'
            assert recovery_row(settings, sid)['status'] == 'VERIFIED'
            assert [entry[0] for entry in gateway.recoveries] == [sid]
            assert await worker.run_once() is True
            assert len(gateway.computes) == 1
        finally:
            await worker.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('role', ['dispatcher', 'driver'])
def test_authorized_http_access_waits_for_worker_verification(worker_setup, role):
    settings, gateway = worker_setup
    sid = saved_session(settings, gateway)
    settings = replace(settings, deferred_session_recovery=True)

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            actor = Actor(worker.sessions.owner(sid), 'dispatcher') if role == 'dispatcher' else Actor('funnel-TX01', 'driver', sid)
            state = SimpleNamespace(settings=settings, sessions=worker.sessions, recovery_repository=worker.recovery)
            request = SimpleNamespace(app=SimpleNamespace(state=state))
            foreign = Actor('other-owner', 'dispatcher') if role == 'dispatcher' else Actor('funnel-TX02', 'driver', 'other-session')
            with pytest.raises(ApiError) as rejected:
                await session_owner(sid, request, foreign)
            assert rejected.value.status_code == 403
            assert worker.recovery.requested_sessions() == []
            pending = asyncio.create_task(session_owner(sid, request, actor))
            await asyncio.sleep(0)
            assert not pending.done()
            assert worker.recovery.requested_sessions() == [sid]
            gateway.block_recover = True
            checking = asyncio.create_task(worker.run_once())
            await gateway.recover_started.wait()
            assert worker.recovery.healthy()
            with pytest.raises(ApiError):
                worker.recovery.assert_mutation_allowed(sid)
            gateway.release_recover.set()
            await checking
            assert await pending is actor
            assert recovery_row(settings, sid)['status'] == 'VERIFIED'
        finally:
            await worker.shutdown()
    asyncio.run(scenario())


def test_readiness_poll_does_not_call_sdk_while_worker_is_starting(worker_setup):
    settings, gateway = worker_setup
    calls = []
    original = gateway.capabilities

    async def counted():
        calls.append(True)
        return await original()
    gateway.capabilities = counted
    with TestClient(create_app(settings, gateway=gateway)) as client:
        calls.clear()
        response = client.get('/ready')
        assert response.status_code == 503
        assert calls == []


def test_failed_deferred_verification_keeps_session_blocked(worker_setup):
    settings, gateway = worker_setup
    sid = saved_session(settings, gateway)
    settings = replace(settings, deferred_session_recovery=True)
    gateway.recover_error = 'STORE_INVALID'

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            worker.recovery.request_verification(sid)
            await worker.run_once()
            assert worker.status == 'DEGRADED'
            assert recovery_row(settings, sid)['status'] == 'BLOCKED'
            with pytest.raises(ApiError) as rejected:
                worker.recovery.request_verification(sid)
            assert rejected.value.code == 'STORE_INVALID'
            with pytest.raises(ApiError):
                worker.recovery.assert_mutation_allowed(sid)
        finally:
            await worker.shutdown()
    asyncio.run(scenario())
