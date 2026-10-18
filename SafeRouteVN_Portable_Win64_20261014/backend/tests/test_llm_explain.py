import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.services.llm_explain import ExplainService, evidence, citations, render_prose, ALIASES
from backend.services.comparison_narrative import comparison_narrative
from backend.tests.test_comparison_narrative import group
from backend.tests.test_artifact_extensions import raw, BUILD


def settings(tmp_path):
    return SimpleNamespace(metadata_path=tmp_path/'cache.sqlite', ollama_enabled=True,
        ollama_url='http://127.0.0.1:11434', ollama_model='qwen3:4b', ollama_timeout_seconds=5,
        installation=lambda: {'expected_build_sha256': BUILD})


def test_generation_deduplicates_and_survives_service_restart(tmp_path, group, monkeypatch):
    calls=[]
    async def post(self, url, **kwargs):
        calls.append(kwargs['json'])
        assert kwargs['json']['think'] is False
        return httpx.Response(200, request=httpx.Request('POST',url), json={'done':True,'response':json.dumps({p:f'Thời gian {{{p}.minutes}}, chi phí {{{p}.cost_vnd}}.' for p in ALIASES})})
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    async def run():
        service=ExplainService(settings(tmp_path))
        a=service.prepare(group); b=service.prepare(group)
        assert a['status']==b['status']=='PENDING' and len(service.tasks)==1
        assert a['profiles']=={}  # No canned review while the model is pending.
        await asyncio.gather(*service.tasks.values())
        result=ExplainService(settings(tmp_path)).prepare(group)
        assert result['status']=='READY' and len(calls)==1
        values=citations(evidence(comparison_narrative(group)))
        for alias,p in ALIASES.items():
            assert result['profiles'][p]['sentences']==[f'Thời gian {values[alias+".minutes"]}, chi phí {values[alias+".cost_vnd"]}.']
            assert result['profiles'][p]['fact_ids']==[p+'.minutes',p+'.cost_vnd']
        assert result['basis']==group['input_basis']
    asyncio.run(run())


@pytest.mark.parametrize('response', ['not json', json.dumps({'FASTEST':['invented']}), json.dumps({p:['goal','goal','travel'] for p in ('FASTEST','BALANCED','SAFER')})])
def test_bad_model_output_has_no_canned_review(tmp_path,group,monkeypatch,response):
    async def post(self,url,**kw):
        return httpx.Response(200,request=httpx.Request('POST',url),json={'done':True,'response':response})
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    async def run():
        s=ExplainService(settings(tmp_path)); initial=s.prepare(group)
        await asyncio.gather(*s.tasks.values())
        result=s.prepare(group)
        assert result['status']=='FALLBACK' and result['profiles']==initial['profiles']
        assert result['profiles']=={}
        assert not s.tasks
    asyncio.run(run())


def test_generation_deadline_includes_waiting_for_busy_model(tmp_path,group):
    async def run():
        s=ExplainService(settings(tmp_path));s.settings.ollama_timeout_seconds=.02
        await s.gate.acquire()
        s.prepare(group)
        await asyncio.wait_for(asyncio.gather(*s.tasks.values()),1)
        result=s.prepare(group)
        assert result['status']=='FALLBACK' and result['profiles']=={}
        s.gate.release()
    asyncio.run(run())


def test_noncomparable_and_disabled_never_generate(tmp_path,group):
    s=ExplainService(settings(tmp_path));s.settings.ollama_enabled=False
    assert s.prepare(group)['status']=='FALLBACK'
    group['outcome']['comparison']['status']='NON_COMPARABLE'
    group['outcome']['comparison']['reason']='AUTHENTICATED_BASIS_OR_PHYSICAL_DOMAIN_DIFFERS'
    group['outcome']['reason']='AUTHENTICATED_BASIS_OR_PHYSICAL_DOMAIN_DIFFERS'
    group['outcome']['comparison']['jobs'][2]['domain_sha256']='e'*64
    result=s.prepare(group)
    assert result['status']=='WAITING' and result['profiles']=={} and not s.tasks


def test_new_evidence_cannot_reuse_prior_explanation(tmp_path,group,monkeypatch):
    async def post(self,url,**kw):
        raise httpx.ConnectError('Offline')
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    async def run():
        s=ExplainService(settings(tmp_path));s.prepare(group)
        await asyncio.gather(*s.tasks.values())
        group['outcome']['comparison']['jobs'][0]['metrics']['total_exposure']=999
        assert s.prepare(group)['status']=='PENDING'
        await s.close()
    asyncio.run(run())


def test_citations_keep_optimize_precision_and_concise_comparison(group):
    metrics=group['outcome']['comparison']['jobs'][0]['metrics']
    metrics.update(total_travel_time_s=11274.84,total_cost_vnd=191652.62,total_exposure=25.5)
    facts=evidence(comparison_narrative(group))
    result=render_prose('Thời gian {A.minutes} thấp hơn {B.minutes}. Chi phí {A.cost_vnd} cao hơn {B.cost_vnd}. Exposure {A.exposure_proxy}.','A',facts)
    assert result['sentences']==['Thời gian 187.914 phút thấp hơn BALANCED. Chi phí 191,652.62 VND cao hơn BALANCED.']
    assert 'FASTEST.exposure_proxy' not in result['fact_ids']


def test_repeated_metric_does_not_crowd_out_second_numeric_fact(group):
    facts=evidence(comparison_narrative(group))
    result=render_prose('Thời gian {A.minutes} bằng B. Thời gian {A.minutes} bằng C. Chi phí {A.cost_vnd} bằng B và C.','A',facts)
    assert result['sentences'][0].count('Thời gian')==1
    assert 'FASTEST.cost_vnd' in result['fact_ids']


@pytest.mark.parametrize('prose',[
    'Thời gian 999 phút, chi phí {A.cost_vnd}.',
    'Thời gian {A.minutes}, chi phí {A.invented}.',
    'Thời gian {B.minutes}, chi phí {B.cost_vnd}.',
    'Thời gian {A.minutes}; không có khác biệt.',
    'Thời gian {A.minutes}, chi phí {A.cost_vnd}. {',
])
def test_invented_or_unbound_numbers_are_rejected(group,prose):
    with pytest.raises(ValueError):
        render_prose(prose,'A',evidence(comparison_narrative(group)))
