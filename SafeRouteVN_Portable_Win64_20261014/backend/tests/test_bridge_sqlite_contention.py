import asyncio
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from backend.services.runtime_bridge import BridgeFailure, public_failure_code
from backend.tests.test_runtime_gateway import gateway


def wrapped(cause, code='STORE_INVALID'):
    error = BridgeFailure(code)
    error.__cause__ = cause
    return error


def test_actual_sqlite_write_contention_is_busy(tmp_path):
    path = tmp_path / 'locked.sqlite'
    with sqlite3.connect(path) as owner:
        owner.execute('create table rows(value)')
        owner.execute('begin immediate')
        with sqlite3.connect(path, timeout=0.01) as reader:
            with pytest.raises(sqlite3.OperationalError) as caught:
                reader.execute('begin immediate')
            assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
            assert public_failure_code(wrapped(caught.value)) == 'RUNTIME_BUSY'


@pytest.mark.parametrize('number', [sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY | (2 << 8)])
def test_typed_extended_contention_is_busy(number):
    cause = sqlite3.OperationalError('private detail')
    cause.sqlite_errorcode = number
    assert public_failure_code(wrapped(cause)) == 'RUNTIME_BUSY'


def test_actual_invalid_database_stays_store_invalid(tmp_path):
    path = tmp_path / 'invalid.sqlite'
    path.write_bytes(b'not a SQLite database')
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.DatabaseError) as caught:
            db.execute('select * from sqlite_master').fetchall()
        assert public_failure_code(wrapped(caught.value)) == 'STORE_INVALID'


@pytest.mark.parametrize('cause', [ValueError('database is locked'), sqlite3.OperationalError('database is locked'), None])
def test_messages_and_untyped_errors_do_not_bypass_store_fencing(cause):
    assert public_failure_code(wrapped(cause)) == 'STORE_INVALID'


def test_busy_gateway_response_does_not_block_recovery(gateway, monkeypatch):
    monkeypatch.setattr('subprocess.run', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps({
        'schema_version': 'task02-m2-runtime-response/1', 'status': 'FAIL', 'diagnostics': [{'code': 'RUNTIME_BUSY'}]})))
    from backend.api.errors import ApiError
    with pytest.raises(ApiError) as caught:
        asyncio.run(gateway.job_view('session-busy', 'job-busy'))
    assert caught.value.code == 'RUNTIME_BUSY' and caught.value.status_code == 503
    assert not gateway.settings.metadata_path.exists()


@pytest.mark.parametrize('code', ['RUNTIME_BUSY', 'STORE_INVALID', 'RUNTIME_UNAVAILABLE'])
def test_native_poll_retries_only_typed_busy_get(monkeypatch, code):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    from verify_step8 import OfflineHarness, NativeHarness
    calls = []
    def reply(*args, **kwargs):
        calls.append(kwargs)
        return (503, {'diagnostics': [{'code': code}]}, 0.1) if len(calls) == 1 else (200, {'data': {}}, 0.1)
    monkeypatch.setattr(NativeHarness, 'http', reply)
    monkeypatch.setattr('verify_step8.time.sleep', lambda seconds: None)
    harness = object.__new__(OfflineHarness)
    harness.poll_retries = []
    status, _, _ = harness.http('/api/sessions/session-busy/jobs/job-busy')
    assert status == (200 if code == 'RUNTIME_BUSY' else 503)
    assert len(calls) == (2 if code == 'RUNTIME_BUSY' else 1)
    assert len(harness.poll_retries) == (1 if code == 'RUNTIME_BUSY' else 0)
