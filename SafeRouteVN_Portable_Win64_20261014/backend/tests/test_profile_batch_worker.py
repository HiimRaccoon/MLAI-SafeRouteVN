import asyncio
from copy import deepcopy
from fastapi.testclient import TestClient
from backend.api.errors import ApiError
from backend.services.compute_worker import ComputeWorker
from backend.tests.test_profiles import profile_setup, profile_app, batch
from backend.tests.test_sessions import setup


def test_native_batch_path_submits_once_observes_all_and_finishes_comparison(profile_setup):
    settings,gateway=profile_setup
    gateway.supports_profile_batch=True
    calls=[]
    async def compute_batch(sid,ids,command,budget):
        calls.append((sid,list(ids),command))
        for jid in ids:gateway.completed(gateway.jobs[(sid,jid)])
        return [deepcopy(gateway.jobs[(sid,jid)]) for jid in ids]
    gateway.compute_batch=compute_batch
    with TestClient(profile_app(settings,gateway)) as client:
        sid,cid=batch(client)
        async def scenario():
            worker=ComputeWorker(settings,gateway=gateway)
            await worker.startup()
            try:
                for _ in range(6):
                    await worker.run_once()
                    if worker.profiles.comparison(sid,cid)['state']=='COMPLETED':break
                row=worker.profiles.comparison(sid,cid)
                assert row['state']=='COMPLETED'
                assert len(calls)==1 and len(calls[0][1])==3
                assert row['outcome']['comparison']['status']=='COMPARABLE'
                assert all(m['view']['job_status']=='COMPLETED' for m in row['members'])
                assert all(worker.jobs.job(sid,jid)['queue_state']=='DONE' for jid in calls[0][1])
            finally:await worker.shutdown()
        asyncio.run(scenario())


def test_batch_busy_preserves_job_commands_and_retries(profile_setup):
    settings,gateway=profile_setup
    gateway.supports_profile_batch=True
    calls=[]
    async def compute_batch(sid,ids,command,budget):
        calls.append((list(ids),command))
        if len(calls)==1:raise ApiError(503,'RUNTIME_BUSY','batch','busy')
        for jid in ids:gateway.completed(gateway.jobs[(sid,jid)])
        return [deepcopy(gateway.jobs[(sid,jid)]) for jid in ids]
    gateway.compute_batch=compute_batch
    with TestClient(profile_app(settings,gateway)) as client:
        sid,cid=batch(client)
        async def scenario():
            worker=ComputeWorker(settings,gateway=gateway);await worker.startup()
            try:
                await worker.run_once()  # submit the bounded three-member batch
                assert await worker.run_once() is False
                assert worker.status=='READY'
                assert all(worker.jobs.job(sid,jid)['queue_state']=='QUEUED' for jid in calls[0][0])
                await worker.run_once()
                assert calls[0]==calls[1]
                assert worker.profiles.comparison(sid,cid)['state']=='COMPLETED'
            finally:await worker.shutdown()
        asyncio.run(scenario())


def test_committed_profile_is_visible_before_other_profiles_finish(profile_setup):
    settings,gateway=profile_setup;gateway.supports_profile_batch=True
    with TestClient(profile_app(settings,gateway)) as client:
        sid,cid=batch(client)
        async def scenario():
            release=asyncio.Event();observed=asyncio.Event()
            async def compute_batch(sid,ids,command,budget):
                gateway.completed(gateway.jobs[(sid,ids[0])])
                await release.wait()
                for jid in ids:gateway.completed(gateway.jobs[(sid,jid)])
                return [deepcopy(gateway.jobs[(sid,jid)]) for jid in ids]
            gateway.compute_batch=compute_batch
            worker=ComputeWorker(settings,gateway=gateway);worker.profile_observation_interval=.01
            await worker.startup()
            original=worker.profiles.observed
            def observe(*args):original(*args);observed.set()
            worker.profiles.observed=observe
            async def dispatch():
                for _ in range(6):await worker.run_once()
            task=asyncio.create_task(dispatch())
            try:
                await asyncio.wait_for(observed.wait(),5)
                row=worker.profiles.comparison(sid,cid)
                assert row['members'][0]['view']['job_status']=='COMPLETED'
                assert row['state']!='COMPLETED' and not task.done()
            finally:
                release.set();await task;await worker.shutdown()
        asyncio.run(scenario())
