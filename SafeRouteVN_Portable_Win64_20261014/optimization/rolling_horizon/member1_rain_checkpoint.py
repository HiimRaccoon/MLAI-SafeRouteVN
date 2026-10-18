"""Content-bound Step6 acceptance; missing native source is BLOCKED, not PASS."""
import argparse
from datetime import datetime
import json,sys,zipfile
from pathlib import Path
from typing import Mapping
import xml.etree.ElementTree as ET
from optimization.models.member1_dynamic_state import require,DynamicError,finite_tree
from optimization.integration.member1_static_runner import _sha
from optimization.rolling_horizon.member1_motion_checkpoint import verify_command_record
from optimization.rolling_horizon.member1_command_contract import verify_semantics
from optimization.rolling_horizon.member1_rain_check import check,read

VERSION='task02-m1-temporal-rain-checkpoint/1'
PREFLIGHT_SHA256='03bf08e36aef7c7efd723eb984d3e6173d81c01cc838fdffd1b87ea0a1e729c7'
SOURCES=('m1_pyproj','m1_verify','task_audit','crosswalk','crosswalk_optimized','legacy_source','binding_source')


def recorded(path):
    envelope=read(path)
    require(isinstance(envelope,Mapping) and isinstance(envelope.get('records'),list) and len(envelope['records'])==1 and isinstance(envelope['records'][0],Mapping),'INVALID_DATA',str(path)+'.records','one native record required')
    return envelope['records'][0]


def verify_record(record,evidence,kind,argv,cwd,*,expected_exit=0):
    require(isinstance(record,Mapping),'INVALID_DATA','record','command object required');finite_tree(record,'record')
    require(record.get('kind')==kind and record.get('argv')==argv,'COMMAND_BINDING','record.argv','exact native argv/module/kind required')
    require(isinstance(argv,list) and bool(argv) and all(isinstance(a,str) and bool(a) for a in argv),'INVALID_DATA','record.argv','nonempty executable argument strings required')
    require(type(record.get('exit_code')) is int and record['exit_code']==expected_exit,'COMMAND_FAILED','record.exit_code','native command failed or receipt label was forged')
    for key in ('cwd','interpreter','platform','started_at_utc','ended_at_utc'):
        require(isinstance(record.get(key),str) and bool(record[key]),'INVALID_DATA','record.'+key,'native string required')
    require(record['platform'].startswith('Windows-') and Path(record['cwd']).resolve()==Path(cwd).resolve() and Path(record['interpreter']).resolve()==Path(argv[0]).resolve(),'COMMAND_PLATFORM','record.cwd','native executable/cwd/platform differs')
    require(type(record.get('duration_seconds')) in (int,float) and record['duration_seconds']>=0,'INVALID_DATA','record.duration_seconds','finite nonnegative duration required')
    for stream in ('stdout','stderr','log'):
        require(isinstance(record.get(stream+'_path'),str),'INVALID_DATA','record.'+stream+'_path','sidecar path required')
        require(type(record.get(stream+'_bytes')) is int and record[stream+'_bytes']>=0 and isinstance(record.get(stream+'_sha256'),str),'INVALID_DATA','record.'+stream+'_bytes','raw stream pin required')
    verify_command_record(record,Path(evidence),expected_exit=expected_exit)


def frozen(repo,evidence):
    path=evidence/'preflight_inventory.json'
    require(_sha(path)==PREFLIGHT_SHA256,'FROZEN_LEDGER_BINDING','preflight_inventory','pre-edit ledger changed')
    ledger=read(path);groups={};allowed={'optimization/rolling_horizon/member1_dynamic_planner.py','optimization/rolling_horizon/member1_dynamic_validation.py'}
    prior_allowed={'optimization/models/member1_dynamic_state.py','optimization/models/member1_dynamic_step5.schema.json','optimization/solver/member1_dynamic_master.py'}|{'optimization/rolling_horizon/member1_dynamic_'+n+'.py' for n in ('planner','validation','runner','check','checkpoint','package')}
    for group,row in ledger['groups'].items():
        archive=Path(row['zip']);require(_sha(archive)==row['sha256'] and archive.stat().st_size==row['bytes'],'FROZEN_ARCHIVE',group,'historical archive changed')
        with zipfile.ZipFile(archive) as z:require(z.testzip() is None and len(z.namelist())==len(set(z.namelist())),'FROZEN_ARCHIVE',group,'archive CRC/duplicates invalid')
        changed=[]
        for rel,pin in row['entries'].items():
            p=repo/rel
            if not p.is_file() or p.stat().st_size!=pin['bytes'] or _sha(p)!=pin['sha256']:changed.append(rel)
        permit=allowed|prior_allowed if group=='STEP5' else allowed if group=='STEP5_CLOSURE' else {'optimization/rolling_horizon/member1_motion_checkpoint.py'} if group=='STEP4' else set()
        require(set(changed)<=permit,'FROZEN_WORKING_TREE',group,'unexpected historical diff: '+str(changed))
        groups[group]={'entries':len(row['entries']),'archive_unchanged':True,'current_equal':len(row['entries'])-len(changed),'intentional_source_extensions':changed}
    for rel,pin in ledger['historical_outputs'].items():
        p=repo/rel;require(p.is_file() and p.stat().st_size==pin['bytes'] and _sha(p)==pin['sha256'],'FROZEN_OUTPUT',rel,'historical output changed')
    for rel,pin in ledger['historical_zips'].items():
        p=Path(rel);require(p.is_file() and p.stat().st_size==pin['bytes'] and _sha(p)==pin['sha256'],'FROZEN_ARCHIVE',rel,'historical ZIP changed')
    return {'groups':groups,'historical_outputs_unchanged':len(ledger['historical_outputs']),'historical_zips_unchanged':len(ledger['historical_zips'])}


def test_evidence(repo,evidence):
    rows={}
    for kind in ('focused_acceptance','related_final_corrected','full_acceptance_final'):
        folder=evidence/'regression';record=recorded(folder/(kind+'.record.json'))
        argv=[sys.executable,'-m','pytest','-q','--junitxml='+str(folder/(kind+'.xml'))]
        if kind=='full_acceptance_final':require(record['argv']==argv,'TEST_BINDING',kind,'full pytest must not select or exclude tests')
        verify_record(record,folder,kind,record['argv'],repo)
        require(record['argv'][:5]==argv,'TEST_BINDING',kind,'pytest/JUnit command differs')
        root=ET.parse(folder/(kind+'.xml')).getroot();suites=[root] if root.tag=='testsuite' else list(root)
        totals={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
        require(totals['tests']>0 and totals['failures']==totals['errors']==totals['skipped']==0,'REGRESSION',kind,'native tests must run and pass')
        rows[kind]=totals
    return rows


def verify(repo,snapshot,evidence,matrix):
    repo=Path(repo).resolve();snapshot=Path(snapshot).resolve();evidence=Path(evidence).resolve();diagnostics=[];sources={};runs={};tests=None;integrity=None
    for kind in SOURCES:
        try:
            folder=evidence/'native_sources_acceptance';record=recorded(folder/(kind+'.record.json'))
            # Existing source-contract checker enforces exact module, source
            # roots, M1 executable/cwd, output gate and -O independently.
            verify_record(record,folder,kind,record['argv'],snapshot if kind.startswith('m1_') else repo)
            verify_semantics(record,repo,snapshot);sources[kind]={'status':'PASS','exit_code':record['exit_code']}
        except (DynamicError,OSError,ValueError,KeyError) as e:
            sources[kind]={'status':'BLOCKED','message':str(e)};diagnostics.append({'code':getattr(e,'code','SOURCE_ACCEPTANCE'),'path':kind,'message':str(e)})
    for kind,optimized,root in [('s4_acceptance',False,'outputs/member1_rain_step6/accepted'),('s4_acceptance_optimized',True,'outputs/member1_rain_step6_optimized/accepted')]:
        try:
            folder=evidence/'native_final';record=recorded(folder/(kind+'.record.json'))
            argv=[sys.executable]+(['-O'] if optimized else [])+['-m','optimization.rolling_horizon.member1_rain_runner','--snapshot-root',str(snapshot),'--output-root',root]
            verify_record(record,folder,kind,argv,repo)
            stdout=read(record['stdout_path']);require(isinstance(stdout,Mapping) and stdout.get('status')=='VALIDATED','RUN_STDOUT',kind,'native result did not validate')
            directory=Path(stdout['output']).resolve();require(directory.parent==(repo/root).resolve() and directory.name==stdout.get('run_id'),'RUN_BINDING',kind,'native run/output root differs')
            manifest=read(directory/'manifest.json')
            require(manifest['platform']['system']=='Windows' and manifest['platform']['optimized_mode'] is optimized and Path(manifest['platform']['interpreter']).resolve()==Path(sys.executable).resolve(),'RUN_PLATFORM',kind,'native engine mode differs')
            at=datetime.strptime(manifest['run_id'].removeprefix('S4_RAIN_'),'%Y%m%dT%H%M%S%fZ').replace(tzinfo=datetime.fromisoformat(record['started_at_utc']).tzinfo)
            require(datetime.fromisoformat(record['started_at_utc'])<=at<=datetime.fromisoformat(record['ended_at_utc']),'RUN_TIME_BINDING',kind,'execution run ID timestamp outside native capture')
            require(stdout.get('targets')==manifest.get('targets'),'RUN_BINDING',kind,'stdout/manifest targets differ')
            current=check(snapshot,directory,_sha(directory/'manifest.json'))
            require(len(manifest['targets'])==3 and all(t['validation_valid'] is True for t in manifest['targets'].values()),'TARGET_COVERAGE',kind,'three independently valid profiles required')
            require(not any(v.get('run_id')==manifest['run_id'] for v in runs.values()),'DUPLICATE_RUN',kind,'transplanted/duplicate target run')
            runs[kind]={'run_id':manifest['run_id'],'manifest_sha256':_sha(directory/'manifest.json'),'directory':str(directory),'targets':manifest['targets'],'raw_revalidation':current}
        except (DynamicError,OSError,ValueError,KeyError) as e:diagnostics.append({'code':getattr(e,'code','RUN_ACCEPTANCE'),'path':kind,'message':str(e)})
    try:tests=test_evidence(repo,evidence)
    except (DynamicError,OSError,ValueError,KeyError) as e:diagnostics.append({'code':getattr(e,'code','TEST_ACCEPTANCE'),'path':'regression','message':str(e)})
    try:integrity=frozen(repo,evidence)
    except (DynamicError,OSError,ValueError,KeyError) as e:diagnostics.append({'code':getattr(e,'code','FROZEN_INTEGRITY'),'path':'frozen','message':str(e)})
    matrix_value=read(matrix);require(isinstance(matrix_value,Mapping),'INVALID_DATA','matrix','matrix object required')
    requirements=matrix_value.get('requirements');require(isinstance(requirements,list) and len(requirements)==60 and all(isinstance(r,Mapping) for r in requirements) and {r.get('id') for r in requirements}=={f'A{i:02}' for i in range(1,61)},'ACCEPTANCE_MATRIX','matrix','all sixty requirements required')
    ready=all(r.get('verdict')=='PASS' and r.get('code_paths') and r.get('test_paths') and r.get('evidence_paths') for r in requirements)
    return {'schema_version':VERSION,'gate':'M1_S4_TEMPORAL_RAIN_OVERLAY_READY' if ready and not diagnostics and len(runs)==2 else 'GATE_BLOCKED',
        'diagnostics':diagnostics,'sources':sources,'runs':runs,'regression':tests,'frozen':integrity,'acceptance_matrix_sha256':_sha(Path(matrix)),
        'current_checker_sha256':{p:_sha(repo/p) for p in ['optimization/rolling_horizon/member1_rain_check.py','optimization/rolling_horizon/member1_rain_checkpoint.py','optimization/rolling_horizon/member1_rain_validation.py']},
        'general_m1_validated':False,'e4_run':False,'production_calibrated':False,'historical_execution_hashes_unchanged':True}


def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot-root',required=True);p.add_argument('--evidence-root',required=True);p.add_argument('--matrix',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    try:result=verify(Path(__file__).resolve().parents[2],a.snapshot_root,a.evidence_root,a.matrix)
    except (DynamicError,OSError,ValueError,KeyError) as e:
        print(json.dumps({'gate':'GATE_BLOCKED','diagnostic':{'code':getattr(e,'code','CHECKPOINT_INPUT'),'path':getattr(e,'path','$'),'message':str(e)}}),file=sys.stderr);return 2
    output=Path(a.output);require(not output.exists(),'OUTPUT_EXISTS','output','new checkpoint path required')
    output.write_text(json.dumps(result,sort_keys=True,indent=2)+'\n',encoding='utf8');print(json.dumps({'gate':result['gate'],'output':str(output),'diagnostics':result['diagnostics']}));return 0 if result['gate']=='M1_S4_TEMPORAL_RAIN_OVERLAY_READY' else 2
if __name__=='__main__':raise SystemExit(main())
