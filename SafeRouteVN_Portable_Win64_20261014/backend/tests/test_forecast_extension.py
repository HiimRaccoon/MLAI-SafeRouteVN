from copy import deepcopy
import sqlite3

import pytest

from backend.tests.test_sessions import setup, headers, code
from backend.tests.test_jobs import jobs_setup, jobs_client, submitted
from optimization.runtime.forecast import job_forecast


def projection(gateway, sid, jid):
    return job_forecast({'id': jid, 'basis': gateway.jobs[(sid, jid)]['input_basis'],
                         'status': 'QUEUED', 'request': {'profile': 'BALANCED'}})


def test_forecast_is_read_only_owned_and_submission_bound(jobs_client, jobs_setup):
    gateway = jobs_setup[1]
    sid, submitted_job = submitted(jobs_client)
    jid = submitted_job['job_id']
    value = projection(gateway, sid, jid)

    async def forecast(session_id, job_id):
        assert (session_id, job_id) == (sid, jid)
        return deepcopy(value)

    gateway.job_forecast = forecast
    path = f'/api/sessions/{sid}/jobs/{jid}/forecast'
    before = deepcopy(gateway.views)
    assert jobs_client.get(path).status_code == 401
    assert jobs_client.get(path, headers=headers('bob')).status_code == 403
    result = jobs_client.get(path, headers=headers())
    assert result.status_code == 200, result.text
    assert result.json()['data']['trajectory'] is None
    assert gateway.views == before and gateway.computes == []
    value['profile'] = 'SAFER'
    code(jobs_client.get(path, headers=headers()), 503, 'JOB_BINDING_CHANGED')


def test_forecast_metadata_failure_is_503(jobs_client, monkeypatch):
    sid, job = submitted(jobs_client)

    def fail(*args):
        raise sqlite3.OperationalError('busy')

    monkeypatch.setattr(jobs_client.app.state.job_service.repository, 'job', fail)
    code(jobs_client.get(f"/api/sessions/{sid}/jobs/{job['job_id']}/forecast", headers=headers()),
         503, 'METADATA_UNAVAILABLE')


def test_runtime_forecast_requires_validated_witness_and_preserves_source(jobs_client, jobs_setup):
    sid, submitted_job = submitted(jobs_client)
    jid = submitted_job['job_id']
    job = {'id': jid, 'basis': submitted_job['input_basis'], 'request': {'profile': 'BALANCED'},
           'status': 'COMPLETED', 'result': {'status': 'FEASIBLE', 'profile': 'BALANCED',
            'domain_sha256': 'a'*64, 'vehicle_routes': [], 'metrics': {'distance_m': 0},
            'served_orders': [], 'unserved_orders': []}}
    with pytest.raises(Exception, match='witness'):
        job_forecast(job)
    job['validation'] = {'valid': True, 'validator_version': 'test'}
    before = deepcopy(job)
    result = job_forecast(job)
    assert result['trajectory']['forecast'] is True
    assert result['metric_scope'] == 'FORECAST_ONLY'
    assert result['real_world_observation'] is False
    assert job == before
    job['result']['profile'] = 'SAFER'
    with pytest.raises(Exception, match='profile'):
        job_forecast(job)
