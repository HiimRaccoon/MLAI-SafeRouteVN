"""Supplemental persisted-job view for M3; not a frozen API v1 route projection.

Change Impact Analysis: /1 supplemental view, no changes to shared/contracts.
Exact basis identifies server state. NO_SERVICE is internal only and requires
its independent certificate. No-witness is never a list of dropped orders.
"""
from collections.abc import Mapping
from .protocol import require,identifier,basis,tree

VERSION='task02-m2-runtime-job-view/1'

def job_view(job):
    require(isinstance(job,Mapping),'INVALID_DATA','job','persisted job object required')
    tree(job);identifier(job.get('id'),'job.id');basis(job.get('basis'))
    status=job.get('status');require(isinstance(status,str) and status in ('QUEUED','RUNNING','COMPLETED','FAILED'),'INVALID_DATA','job.status','known lifecycle required')
    value={'schema_version':VERSION,'job_id':job['id'],'job_status':status,'input_basis':job['basis'],'business_status':None,'internal_status':None,'diagnostics':[],
           'validation':{'status':'NOT_RUN','valid':None},'coverage_evaluated':False,'served_orders':[],'unserved_orders':[],'plan_available':False,
           'execution_view_required':True,'public_api_v1_dynamic_plan_available':False}
    if status=='FAILED':
        require(isinstance(job.get('failure'),Mapping),'INVALID_DATA','job.failure','failure reason required');value['diagnostics']=[job['failure']]
    elif status=='COMPLETED':
        result=job.get('result');require(isinstance(result,Mapping),'INVALID_DATA','job.result','completed result required')
        internal=result.get('status');require(isinstance(internal,str) and internal in ('FEASIBLE','PARTIAL','RETURN_ONLY','NO_SERVICE','SEARCH_LIMIT','TIME_LIMIT','UNSUPPORTED','INVALID_DATA'),'INVALID_DATA','job.result.status','known outcome required')
        value['internal_status']=internal
        if internal in ('FEASIBLE','PARTIAL','RETURN_ONLY'):
            require(isinstance(job.get('validation'),Mapping) and job['validation'].get('valid') is True,'WITNESS_REQUIRED','job.validation','independently certified witness required')
            value.update(validation={'status':'VALIDATED','valid':True,'validator_version':job['validation'].get('validator_version')},coverage_evaluated=True,
                         served_orders=result['served_orders'],unserved_orders=result['unserved_orders'],plan_available=True,
                         business_status=internal if internal!='RETURN_ONLY' else 'UNSUPPORTED')
            if internal=='RETURN_ONLY':value['diagnostics']=[{'severity':'ERROR','code':'RETURN_ONLY_SUPPLEMENT_REQUIRED','path':'plan','message':'Mandatory physical continuation exists; no new deliveries. Use the supplemental execution view.'}]
        else:
            value['business_status']='UNSUPPORTED' if internal=='NO_SERVICE' else internal
            value['diagnostics']=result.get('diagnostics',[])
            require(isinstance(value['diagnostics'],list) and value['diagnostics'],'DIAGNOSTIC_REQUIRED','job.result.diagnostics','no-witness must carry its reason')
    return value
