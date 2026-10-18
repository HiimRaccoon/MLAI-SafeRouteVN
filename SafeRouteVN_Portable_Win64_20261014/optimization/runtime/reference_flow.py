"""Native transport smoke for the LIMITED initial/dry prototype.

This runs new CP-SAT/road computation and observed replay, NOT historical
receipts. A successful smoke is not Step7 acceptance or M3/M4 handoff READY.
"""
import argparse,hashlib,json,os,platform,subprocess,sys,time
from pathlib import Path
from .build import inventory
from .protocol import VERSION,decode,require,canonical

def pin(path):return {'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

def run(snapshot,output,scenario,budget):
    repo=Path(__file__).resolve().parents[2];output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    installed=inventory(repo);(output/'production_inventory.json').write_bytes(canonical(installed))
    store=output/'private_state/authority.sqlite';session='native-'+scenario
    records=[]
    def invoke(op,key,**fields):
        value={'schema_version':VERSION,'operation':op,'command_id':key,**fields}
        if op!='capabilities':value['session_id']=session
        argv=[sys.executable,str(repo/'runtime_entry.py'),'--inventory',str(output/'production_inventory.json'),'--expected-build-sha256',installed['build_sha256'],'--snapshot-root',str(snapshot),'--store',str(store)]
        t=time.monotonic();p=subprocess.run(argv,cwd=repo,input=canonical(value),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        name=f'{len(records):02d}_{op}';(output/(name+'.request.json')).write_bytes(canonical(value));(output/(name+'.stdout.json')).write_bytes(p.stdout);(output/(name+'.stderr')).write_bytes(p.stderr)
        records.append({'operation':op,'argv':argv,'cwd':str(repo),'exit_code':p.returncode,'elapsed_seconds':time.monotonic()-t,'stdout':pin(output/(name+'.stdout.json')),'stderr':pin(output/(name+'.stderr'))})
        reply=decode(p.stdout)
        require(p.returncode==0 and reply['status']=='OK','NATIVE_SMOKE_FAILED',op,str(reply))
        return reply['value']
    invoke('capabilities','caps')
    boot=invoke('bootstrap','boot',scenario_id=scenario)
    first=invoke('get_head','read-initial')
    job=invoke('submit','request',basis=boot['basis'],profile='BALANCED',budget_seconds=budget)
    result=invoke('compute','compute',job_id=job['job_id'])
    require(result['status']=='COMPLETED' and result['validation']['valid'] is True,'NATIVE_WITNESS_MISSING','job',str(result.get('failure')))
    require(invoke('get_head','read-after-compute')==first,'COMPUTE_MUTATED_HEAD','head','computation must not advance observed state')
    accepted=invoke('accept','accept',job_id=job['job_id'],basis=boot['basis'])
    require(invoke('accept','accept',job_id=job['job_id'],basis=boot['basis'])==accepted,'IDEMPOTENCY','accept','retry differs')
    target='2026-09-27T21:01:00+07:00'
    moved=invoke('advance','move',basis=accepted['basis'],target_time=target)
    require(invoke('advance','move',basis=accepted['basis'],target_time=target)==moved,'IDEMPOTENCY','advance','retry differs')
    view=invoke('get_head','read-observed');(output/'execution_view.json').write_bytes(canonical(view))
    recovery=invoke('recover','restart')
    require(invoke('get_head','read-restarted')==view,'RECOVERY_CHANGED_HEAD','head','restart changed observed head')
    cancelled=invoke('submit','cancel-request',basis=recovery['basis'],budget_seconds=budget)
    invoke('cancel','cancel',job_id=cancelled['job_id'])
    # This event refusal is capability evidence, not a certified transition.
    summary={'schema_version':'task02-m2-native-limited-smoke/1','status':'INITIAL_DRY_SMOKE_PASS_NOT_STEP7_READY','scenario_id':scenario,
             'build_sha256':installed['build_sha256'],'platform':{'system':platform.system(),'python':platform.python_version(),'interpreter':sys.executable},
             'native_solver_executed':True,'historical_seed_used':False,'job_status':result['status'],'business_status':result['result']['status'],
             'served_orders':result['result']['served_orders'],'validation_valid':result['validation']['valid'],'records':records,
             'scope':{'general_m1_validated':False,'e4_run':False,'runtime_handoff_ready':False,'post_event_execution':False}}
    (output/'summary.json').write_bytes(canonical(summary));print(json.dumps({'status':summary['status'],'scenario_id':scenario,'output':str(output)}));return 0

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot-root',required=True,type=Path);p.add_argument('--output-root',required=True,type=Path);p.add_argument('--scenario-id',choices=['S1','S5','S6','S7','S8'],default='S7');p.add_argument('--budget-seconds',type=float,default=120);a=p.parse_args()
    return run(a.snapshot_root,a.output_root,a.scenario_id,a.budget_seconds)
if __name__=='__main__':raise SystemExit(main())
