"""Queue claims defer real SQLite contention without losing or duplicating work."""
import json
import sqlite3

import pytest

from backend.services.job_repository import JobRepository
from backend.services.profile_repository import ProfileRepository


@pytest.fixture
def queue(tmp_path, monkeypatch):
    repo = JobRepository(tmp_path / "metadata.sqlite")
    repo.initialize()
    ProfileRepository(repo.path).initialize()
    with repo._db() as db:
        db.execute("""INSERT INTO compute_queue VALUES
            ('job-1','session-1','installation','{}','FASTEST',10,
             'compute-1','cancel-1','QUEUED',NULL,NULL,NULL,0)""")
    connect = sqlite3.connect

    def short_wait(*args, **kwargs):
        kwargs['timeout'] = 0.01
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", short_wait)
    return repo


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("reader", [False, True])
def test_claim_defers_write_or_commit_lock_then_claims_once(queue, batch, reader):
    comparison = {'comparison_id': 'comparison-1', 'session_id': 'session-1',
                  'installation_sha256': 'installation', 'basis': {},
                  'members': [{'job_id': 'job-1', 'profile': 'FASTEST'}]}
    if batch:
        with queue._db() as db:
            db.execute("""INSERT INTO profile_comparisons VALUES
                ('comparison-1','session-1','actor','request','digest','installation',
                 'NEW_BATCH','{}',10,'QUEUED',NULL,NULL,?,0,0,0)""", (json.dumps({}),))

    def claim():
        return queue.claim_profile_batch(comparison, 'worker-1') if batch else queue.claim_job('worker-1')

    with sqlite3.connect(queue.path) as blocker:
        blocker.execute('BEGIN' if reader else 'BEGIN IMMEDIATE')
        if reader:
            blocker.execute('SELECT * FROM compute_queue').fetchall()
        assert claim() == ([] if batch else None)
        blocker.rollback()
    row = queue.job('session-1', 'job-1')
    assert row['queue_state'] == 'QUEUED' and row['claim_token'] is None
    claimed = claim()
    assert (claimed[0] if batch else claimed)['job_id'] == 'job-1'
    assert claim() == ([] if batch else None)


def test_claim_does_not_hide_database_errors(queue):
    with queue._db() as db:
        db.execute('DROP TABLE compute_queue')
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        queue.claim_job('worker-1')
