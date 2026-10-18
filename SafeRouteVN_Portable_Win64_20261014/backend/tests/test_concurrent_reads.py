import asyncio
import threading
from copy import deepcopy

from backend.tests.test_runtime_gateway import gateway


def test_parallel_reads_share_one_call_but_later_reads_are_fresh(gateway, monkeypatch):
    calls = []
    entered, release = threading.Event(), threading.Event()
    def read(operation, fields):
        calls.append((operation, fields))
        entered.set()
        assert release.wait(5)
        return {'basis': {'head_version': str(len(calls))}}
    monkeypatch.setattr(gateway, '_call', read)
    async def check():
        a = asyncio.create_task(gateway.resolve('s1'))
        await asyncio.to_thread(entered.wait, 5)
        b = asyncio.create_task(gateway.resolve('s1'))
        await asyncio.sleep(0.01)
        release.set()
        first, second = await asyncio.gather(a,b)
        assert len(calls) == 1 and first == second
        first['basis']['head_version'] = 'mutated'
        assert second['basis']['head_version'] == '1'
        assert (await gateway.resolve('s1'))['basis']['head_version'] == '2'
        assert (await gateway.resolve('s2'))['basis']['head_version'] == '3'
    asyncio.run(check())


def test_cancelling_one_reader_does_not_cancel_shared_work(gateway, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def read(*args):
        calls.append(args);entered.set();assert release.wait(5);return {'ready':True}
    monkeypatch.setattr(gateway,'_call',read)
    async def check():
        first=asyncio.create_task(gateway.capabilities())
        await asyncio.to_thread(entered.wait,5)
        second=asyncio.create_task(gateway.capabilities())
        await asyncio.sleep(0.01)
        first.cancel()
        await asyncio.gather(first,return_exceptions=True)
        release.set()
        assert await second == {'ready':True}
        assert len(calls)==1
    asyncio.run(check())
