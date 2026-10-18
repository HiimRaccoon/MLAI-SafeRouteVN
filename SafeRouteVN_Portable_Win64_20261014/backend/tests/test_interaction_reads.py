import asyncio,threading
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pytest
from backend.tests.test_runtime_gateway import gateway


@pytest.mark.parametrize('method',['job_view','job_forecast'])
def test_identical_inflight_reads_are_shared_but_sequential_reads_are_fresh(gateway,method):
    started=threading.Event();release=threading.Event();calls=[]
    def call(operation,fields):
        calls.append((operation,fields['session_id'],fields['job_id']))
        started.set();assert release.wait(5)
        return {'nested':{'generation':len(calls)}}
    async def check():
        first=asyncio.create_task(getattr(gateway,method)('session','job'))
        await asyncio.to_thread(started.wait,5)
        second=asyncio.create_task(getattr(gateway,method)('session','job'))
        await asyncio.sleep(.01);release.set()
        a,b=await asyncio.gather(first,second)
        assert len(calls)==1 and a==b
        a['nested']['generation']=999
        assert b['nested']['generation']==1
        c=await getattr(gateway,method)('session','job')
        assert len(calls)==2 and c['nested']['generation']==2
        await getattr(gateway,method)('other-session','job')
        await getattr(gateway,method)('session','other-job')
        assert len(calls)==4
    with patch.object(gateway,'_call',side_effect=call):asyncio.run(check())


def test_completed_solver_poll_never_starts_ai_but_explicit_explanations_do():
    from backend.api.routers import profiles
    group={'status':'COMPLETED','comparison_id':'comparison'}
    explain=Mock();explain.prepare.return_value={'status':'PENDING'}
    service=Mock();service.poll.return_value=group
    request=SimpleNamespace(state=SimpleNamespace(request_id='request'),
        app=SimpleNamespace(state=SimpleNamespace(profile_service=service,explain_service=explain)))
    asyncio.run(profiles.poll('session','comparison',request,None))
    explain.prepare.assert_not_called()
    asyncio.run(profiles.explanations('session','comparison',request,None))
    explain.prepare.assert_called_once_with(group)
