"""Content-based Step5 checkpoint. It cannot promote an incomplete matrix.

Native logs and local digest pins are evidence, not digital signatures.
Independent bound raw revalidation is run here, without a solver/search rerun.
"""
import argparse,hashlib,json,zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from optimization.models.member1_dynamic_state import require,DynamicError
from optimization.rolling_horizon.member1_motion_checkpoint import verify_command_record
from optimization.rolling_horizon.member1_command_contract import verify_semantics
from optimization.rolling_horizon.member1_dynamic_check import check

VERSION='task02-m1-event-dynamic-checkpoint/2'
SOURCES=['m1_pyproj','m1_verify','task_audit','crosswalk','crosswalk_optimized','legacy_source','binding_source']
PREFLIGHT_SHA256='dc2291295f3cf8985f9239fe7dab58eda92f5c75236590ea493e09ba8de52803'
CLOSURE_PREFLIGHT_SHA256='b3831dc993f6cf57cc1db4892e5f0269b1530d01f6f42d80485893f608310199'
CLOSURE_SOURCE_DIFF={'optimization/models/member1_dynamic_state.py','optimization/models/member1_dynamic_step5.schema.json','optimization/solver/member1_dynamic_master.py'}|{'optimization/rolling_horizon/member1_dynamic_'+n+'.py' for n in ('planner','validation','runner','check','checkpoint','package')}

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_bytes())

def frozen_inventory(repo,evidence):
    require(digest(evidence/'preflight_inventory.json') in (PREFLIGHT_SHA256,CLOSURE_PREFLIGHT_SHA256),'FROZEN_LEDGER_BINDING','preflight_inventory','trusted pre-edit ledger changed (local digest, not a signature)')
    baseline=read(evidence/'preflight_inventory.json');rows={}
    for name,record in baseline['groups'].items():
        archive=Path(record['zip'])
        require(digest(archive)==record['sha256'] and archive.stat().st_size==record['bytes'],'FROZEN_ARCHIVE',name,'historical ZIP changed')
        with zipfile.ZipFile(archive) as z:
            require(z.testzip() is None and len(z.namelist())==len(set(z.namelist())),'FROZEN_ARCHIVE',name,'CRC/duplicates invalid')
        changed=[]
        for rel,pin in record['entries'].items():
            path=repo/rel
            if not path.is_file() or path.stat().st_size!=pin['bytes'] or digest(path)!=pin['sha256']:changed.append(rel)
        allowed={'optimization/rolling_horizon/member1_motion_checkpoint.py'} if name=='STEP4' else CLOSURE_SOURCE_DIFF if name=='STEP5' else set()
        require(set(changed)<=allowed,'FROZEN_WORKING_TREE',name,'unexpected changed legacy file: '+str(changed))
        rows[name]={'entries':len(record['entries']),'archive_unchanged':True,'current_equal':len(record['entries'])-len(changed),'intentional_source_extension':changed}
    for rel,pin in baseline['historical_outputs'].items():
        path=repo/rel
        require(path.is_file() and digest(path)==pin['sha256'] and path.stat().st_size==pin['bytes'],'FROZEN_OUTPUT',rel,'historical output changed')
    for path,pin in baseline.get('historical_zips',{}).items():
        archive=Path(path)
        require(archive.is_file() and digest(archive)==pin['sha256'] and archive.stat().st_size==pin['bytes'],'FROZEN_ARCHIVE',path,'historical ZIP changed')
    return {'groups':rows,'historical_outputs_unchanged':len(baseline['historical_outputs'])}

def test_records(repo,evidence):
    rows={}
    for kind in ('focused_final','related','full_final'):
        record=read(evidence/'regression'/(kind+'.record.json'))['records'][0]
        verify_command_record(record,evidence/'regression')
        require(record.get('kind')==kind and record['platform'].startswith('Windows-') and Path(record['cwd']).resolve()==repo,'TEST_BINDING',kind,'native test record required')
        require(record['argv'][:5]==[record['interpreter'],'-m','pytest','-q','--junitxml='+str((evidence/'regression'/(kind+'.xml')).resolve())],'TEST_BINDING',kind,'pytest command/JUnit differs')
        if kind=='full_final':require(len(record['argv'])==5,'TEST_BINDING',kind,'full suite may not select/exclude tests')
        root=ET.parse(evidence/'regression'/(kind+'.xml')).getroot()
        suites=[root] if root.tag=='testsuite' else list(root)
        totals={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
        require(totals['tests']>0 and totals['errors']==0 and totals['failures']==0 and totals['skipped']==0,'REGRESSION',kind,'tests must really run and pass without skips')
        rows[kind]=totals
    return rows

def verify(repo,snapshot,evidence,matrix):
    repo=Path(repo).resolve();snapshot=Path(snapshot).resolve();evidence=Path(evidence).resolve()
    for kind in SOURCES:
        record=read(evidence/'native_sources_final'/(kind+'.record.json'))['records'][0]
        require(record.get('kind')==kind,'COMMAND_KIND',kind,'wrong source kind')
        verify_command_record(record,evidence/'native_sources_final');verify_semantics(record,repo,snapshot)
    runs={}
    for kind,scenario,optimized in [('dynamic_S2','S2',False),('dynamic_S3','S3',False),('dynamic_S3_optimized','S3',True)]:
        record=read(evidence/'native_targets_certified'/(kind+'.record.json'))['records'][0]
        verify_command_record(record,evidence/'native_targets_certified')
        require(record.get('kind')==kind,'COMMAND_KIND',kind,'wrong command record kind')
        argv=record['argv'];prefix=[record['interpreter']]+(['-O'] if optimized else [])+['-m','optimization.rolling_horizon.member1_dynamic_runner','--snapshot-root',str(snapshot),'--scenario-id',scenario,'--profiles','FASTEST','BALANCED','SAFER','--event-time','2026-09-27T21:15:00+07:00','--output-root']
        require(argv[:-1]==prefix,'COMMAND_BINDING',kind,'exact runner argv required')
        require(record['platform'].startswith('Windows-') and Path(record['cwd']).resolve()==repo,'COMMAND_PLATFORM',kind,'native Windows/cwd required')
        body=read(record['stdout_path']);directory=Path(body['output']).resolve();m=read(directory/'manifest.json')
        require(directory.parent==(repo/argv[-1]).resolve() and body['run_id']==m['run_id'] and body['targets']==m['targets'],'RUN_BINDING',kind,'stdout/output/manifest differ')
        require(body['status']=='READY' and m['snapshot_verified'] is True and m['scenario_id']==scenario and m['gate']==scenario+'_EVENT_DYNAMIC_VALIDATED','RUN_GATE',kind,'scenario gate not met')
        require(m['platform']['system']=='Windows' and m['platform']['optimized_mode'] is optimized and Path(m['platform']['interpreter']).resolve()==Path(argv[0]).resolve(),'RUN_PLATFORM',kind,'native optimization/interpreter differs')
        require(m['code_sha256']==m['code_sha256_after'],'CODE_CHANGED',kind,'execution code changed during run')
        for path,h in m['code_sha256'].items():require(digest(repo/path)==h and (repo/path).stat().st_size==m['code_bytes'][path],'CODE_BINDING',path,'current/execution code differs')
        current=check(snapshot,directory,digest(directory/'manifest.json'))
        require(current['status']=='DYNAMIC_BOUND_REVALIDATION_PASS','VALIDATION',kind,'raw independent whole trajectory did not pass')
        domains={t['domain_sha256'] for t in m['targets'].values()}
        require(len(m['targets'])==3 and len(domains)==1 and all(t['validation_valid'] is True for t in m['targets'].values()),'TARGET_COVERAGE',kind,'shared domain/three validated profiles required')
        if scenario=='S2':require(any('O009' in t['served_orders'] for t in m['targets'].values()),'URGENT_PICKUP',kind,'no native O009 witness')
        else:require(all(t['status']=='PARTIAL' and any(x['order_id']=='O001' and x['reason']=='CUSTODY_BLOCKED' for x in t['unserved_orders']) for t in m['targets'].values()),'CUSTODY_EXERCISE',kind,'missing blocked O001')
        runs[kind]={'run_id':m['run_id'],'directory':str(directory),'manifest_sha256':digest(directory/'manifest.json'),'raw_revalidation':current,'targets':m['targets']}
    negative={}
    for kind,code in [('event_conflict','EVENT_CONFLICT'),('before_rehash','AUTHORITY_BINDING'),('stale_head','STALE_HEAD'),('custody_transfer','TRANSITION_AUTHORITY'),('bad_context','AUTHORITY_BINDING')]:
        record=read(evidence/'native_negative_final'/(kind+'.record.json'))['records'][0]
        verify_command_record(record,evidence/'native_negative_final',expected_exit=2)
        require(record.get('kind')==kind and record['platform'].startswith('Windows-') and Path(record['cwd']).resolve()==repo,'NEGATIVE_BINDING',kind,'native negative record required')
        argv=record['argv'];require(argv[:3]==[record['interpreter'],'-m','optimization.rolling_horizon.member1_dynamic_check'],'NEGATIVE_BINDING',kind,'read-only bound checker command required')
        flags=argv[3:];require(len(flags)%2==0 and len(flags[::2])==len(set(flags[::2])),'NEGATIVE_BINDING',kind,'duplicate/malformed flags')
        args=dict(zip(flags[::2],flags[1::2]));require(args.get('--snapshot-root')==str(snapshot),'NEGATIVE_BINDING',kind,'wrong snapshot')
        target=runs['dynamic_S3' if kind=='custody_transfer' else 'dynamic_S2']
        require(args.get('--run-root')==target['directory'] and args.get('--trusted-manifest-sha256')==target['manifest_sha256'],'NEGATIVE_BINDING',kind,'wrong persisted source target')
        body=read(record['stderr_path']);d=body.get('diagnostic',{})
        require(body.get('status')=='FAIL' and d.get('code')==code and isinstance(d.get('path'),str) and bool(d['path']),'NEGATIVE_RESULT',kind,'wrong/missing negative diagnostic')
        negative[kind]=d
    if digest(evidence/'preflight_inventory.json')==CLOSURE_PREFLIGHT_SHA256:
        kind='forecast_guard'
        record=read(evidence/'native_negative_final'/(kind+'.record.json'))['records'][0]
        verify_command_record(record,evidence/'native_negative_final',expected_exit=2)
        require(record.get('kind')==kind and record['platform'].startswith('Windows-') and Path(record['cwd']).resolve()==repo,'NEGATIVE_BINDING',kind,'native guard command required')
        target=runs['dynamic_S2'];expected=[record['interpreter'],'-m','optimization.rolling_horizon.member1_dynamic_check','--snapshot-root',str(snapshot),'--run-root',target['directory'],'--trusted-manifest-sha256',target['manifest_sha256'],'--candidate-solution']
        require(record['argv'][:-1]==expected,'NEGATIVE_BINDING',kind,'guard must bind genuine persisted target')
        body=read(record['stderr_path']);d=body.get('diagnostic',{})
        require(body.get('status')=='FAIL' and d.get('code')=='FORECAST_OBSERVATION' and d.get('path')=='post_event_state','NEGATIVE_RESULT',kind,'missing observed/forecast guard')
        negative[kind]=d
    requirements=read(matrix)
    require(isinstance(requirements,list) and {r['id'] for r in requirements}=={f'A{i:02}' for i in range(1,51)},'ACCEPTANCE_MATRIX','matrix','all 50 rows required')
    ready=all(r['verdict']=='PASS' and r.get('test_or_review') and r.get('evidence') for r in requirements)
    tests=test_records(repo,evidence);frozen=frozen_inventory(repo,evidence)
    trust_files=['optimization/rolling_horizon/'+n+'.py' for n in ('member1_dynamic_check','member1_dynamic_checkpoint','member1_dynamic_package','member1_dynamic_evidence','member1_command_contract','member1_motion_checkpoint','member1_motion_command_evidence')]
    return {'schema_version':VERSION,'gate':'M1_S2_S3_EVENT_DYNAMIC_READY' if ready else 'GATE_BLOCKED','matrix_sha256':digest(matrix),'runs':runs,'negative_cli':negative,'regression':tests,'frozen':frozen,'current_checker_sha256':{p:digest(repo/p) for p in trust_files},'source_windows_verified':True,'general_m1_validated':False,'e4_run':False,'production_calibrated':False,'s4_event_replay_validated':False,'s2_s4_event_replay_not_validated':True,'api_v1_changed':False}

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot-root',required=True);p.add_argument('--evidence-root',required=True);p.add_argument('--matrix',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    try:result=verify(Path(__file__).resolve().parents[2],a.snapshot_root,a.evidence_root,a.matrix)
    except (DynamicError,OSError,ValueError,KeyError) as e:result={'schema_version':VERSION,'gate':'GATE_BLOCKED','diagnostic':{'code':getattr(e,'code','CHECKPOINT_INVALID'),'path':getattr(e,'path','$'),'message':str(e)}}
    Path(a.output).write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8');print(json.dumps({'gate':result['gate'],'output':a.output}));return 0 if result['gate']=='M1_S2_S3_EVENT_DYNAMIC_READY' else 2
if __name__=='__main__':raise SystemExit(main())
