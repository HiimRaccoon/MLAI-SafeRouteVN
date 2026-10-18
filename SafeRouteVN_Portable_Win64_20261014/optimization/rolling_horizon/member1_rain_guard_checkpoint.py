"""Guard-closure evidence gate; no solver execution or historical rewriting.

Change Impact Analysis: separate /1 guard checkpoint. Local receipts are audit
evidence, not signatures. Actual command streams, external pins and raw-check
receipts are bound; a caller's receipt claims alone never grant PASS.
"""
import argparse,json,sys,zipfile
from datetime import datetime
from pathlib import Path
from typing import Mapping
import xml.etree.ElementTree as ET
from optimization.models.member1_dynamic_state import require,DynamicError
from optimization.rolling_horizon.member1_rain_check import read
from optimization.rolling_horizon.member1_rain_checkpoint import recorded,verify_record
from optimization.rolling_horizon.member1_command_contract import verify_semantics
from optimization.rolling_horizon.member1_rain_guard_revalidation import validate_receipt,code_dependencies,pin,BASELINE_SHA256,TARGETS,VALIDATOR_VERSION

VERSION='task02-m1-step6-guard-checkpoint/1'

def verify_current_record(record,folder,repo,snapshot,archive,output,optimized):
    kind='current_checker_acceptance_optimized' if optimized else 'current_checker_acceptance'
    argv=[sys.executable]+(['-O'] if optimized else [])+['-m','optimization.rolling_horizon.member1_rain_guard_revalidation','--snapshot-root',str(snapshot),'--baseline-zip',str(archive),'--output',str(output)]
    verify_record(record,folder,kind,argv,repo)
    body=json.loads(Path(record['stdout_path']).read_bytes())
    require(isinstance(body,Mapping) and body.get('status')=='STEP6_CURRENT_CHECKER_REVALIDATION_PASS','RECEIPT_BINDING','stdout.status','native raw check must PASS')
    require(Path(body.get('output','')).resolve()==output.resolve() and {k:body.get(k) for k in ('bytes','sha256')}==pin(output),'RECEIPT_BINDING','stdout.output','actual receipt bytes differ')
    value=validate_receipt(read(output))
    platform=value.get('platform')
    require(isinstance(platform,Mapping) and platform.get('system')=='Windows' and platform.get('optimized_mode') is optimized and Path(platform.get('interpreter','')).resolve()==Path(sys.executable).resolve(),'RECEIPT_PLATFORM','receipt.platform','current native checker mode differs')
    require(datetime.fromisoformat(record['started_at_utc'])<=datetime.fromisoformat(value['started_at_utc'])<=datetime.fromisoformat(value['ended_at_utc'])<=datetime.fromisoformat(record['ended_at_utc']),'RECEIPT_TIME','receipt.started_at_utc','check must occur inside its native command')
    deps=code_dependencies(repo)
    for r in value['runs']:
        require(r['current_checker_before']==deps==r['current_checker_after'],'CURRENT_CHECKER_BINDING','receipt.current_checker','current dependencies differ from actual checks')
    return value

def verify_frozen(repo,evidence,trusted_ledger_sha256):
    path=evidence/'preflight_inventory.json'
    require(pin(path)['sha256']==trusted_ledger_sha256,'FROZEN_LEDGER_BINDING','preflight_inventory','external pre-edit ledger pin differs')
    ledger=read(path);rows={}
    old_extensions={'optimization/rolling_horizon/member1_dynamic_planner.py','optimization/rolling_horizon/member1_dynamic_validation.py'}
    prior={'optimization/models/member1_dynamic_state.py','optimization/models/member1_dynamic_step5.schema.json','optimization/solver/member1_dynamic_master.py'}|{'optimization/rolling_horizon/member1_dynamic_'+n+'.py' for n in ('planner','validation','runner','check','checkpoint','package')}
    for group,row in ledger['groups'].items():
        require(pin(Path(row['zip']))=={k:row[k] for k in ('bytes','sha256')},'FROZEN_ARCHIVE',group,'historical checkpoint changed')
        changed=[rel for rel,p in row['entries'].items() if not (repo/rel).is_file() or pin(repo/rel)!=p]
        allowed=old_extensions|prior if group=='STEP5' else old_extensions if group=='STEP5_CLOSURE' else {'optimization/rolling_horizon/member1_motion_checkpoint.py'} if group=='STEP4' else set()
        require(set(changed)<=allowed,'FROZEN_WORKING_TREE',group,'unexpected checkpoint change: '+str(changed))
        rows[group]={'total':len(row['entries']),'equal':len(row['entries'])-len(changed),'prior_intentional_source_extensions':changed}
    for group in ('historical_outputs','historical_zips'):
        for rel,p in ledger[group].items():
            target=repo/rel if group=='historical_outputs' else Path(rel)
            require(target.is_file() and pin(target)==p,'FROZEN_BYTES',rel,'historical bytes changed')
    return {'groups':rows,'historical_outputs_unchanged':len(ledger['historical_outputs']),'historical_zips_unchanged':len(ledger['historical_zips'])}

def verify_tests(repo,evidence):
    rows={}
    for kind in ('focused_acceptance_final','related_acceptance_final','full_acceptance_final'):
        folder=evidence/'regression';record=recorded(folder/(kind+'.record.json'))
        argv=[sys.executable,'-m','pytest','-q','--junitxml='+str(folder/(kind+'.xml'))]
        require(record['argv'][:5]==argv and (kind!='full_acceptance_final' or record['argv']==argv),'TEST_BINDING',kind,'full pytest cannot deselect/skip tests')
        verify_record(record,folder,kind,record['argv'],repo)
        root=ET.parse(folder/(kind+'.xml')).getroot();suites=[root] if root.tag=='testsuite' else list(root)
        row={k:sum(int(x.get(k,0)) for x in suites) for k in ('tests','failures','errors','skipped')}
        require(row['tests']>0 and row['failures']==row['errors']==row['skipped']==0,'REGRESSION',kind,'actual tests must all pass')
        rows[kind]=row
    return rows

def verify(repo,snapshot,archive,evidence,ledger_sha):
    require(pin(archive)['sha256']==BASELINE_SHA256,'BASELINE_BINDING','baseline_zip','reviewed archive required')
    sources={}
    folder=evidence/'native_sources_acceptance'
    for kind in ('m1_pyproj','m1_verify','task_audit','crosswalk','crosswalk_optimized'):
        record=recorded(folder/(kind+'.record.json'))
        verify_record(record,folder,kind,record['argv'],snapshot if kind.startswith('m1_') else repo)
        verify_semantics(record,repo,snapshot);sources[kind]={'exit_code':0,'status':'PASS'}
    folder=evidence/'native_final';record=recorded(folder/'step5_current_revalidation.record.json')
    output=repo/'outputs/member1_rain_guard_closure/receipts/step5_current_raw.json'
    argv=[sys.executable,'-m','optimization.rolling_horizon.member1_rain_legacy_revalidation','--snapshot-root',str(snapshot),'--output',str(output)]
    verify_record(record,folder,'step5_current_revalidation',argv,repo)
    body=json.loads(Path(record['stdout_path']).read_bytes());legacy=read(output)
    require(isinstance(body,Mapping) and body.get('status')=='STEP5_CURRENT_RAW_REVALIDATION_PASS' and Path(body.get('output','')).resolve()==output and body.get('sha256')==pin(output)['sha256'],'RECEIPT_BINDING','step5.stdout','actual Step5 output required')
    require(isinstance(legacy,Mapping) and legacy.get('status')=='STEP5_CURRENT_RAW_REVALIDATION_PASS' and isinstance(legacy.get('runs'),list) and len(legacy['runs'])==3,'LEGACY_REVALIDATION','step5.runs','three historical executions required')
    require(all(isinstance(r,Mapping) and isinstance(r.get('current_validations'),Mapping) and set(r['current_validations'])=={'FASTEST','BALANCED','SAFER'} and all(v.get('valid') is True for v in r['current_validations'].values()) for r in legacy['runs']),'LEGACY_REVALIDATION','step5.validations','nine raw profile checks required')
    checks={}
    for optimized in (False,True):
        kind='current_checker_acceptance_optimized' if optimized else 'current_checker_acceptance'
        output=repo/('outputs/member1_rain_guard_closure/receipts/current_checker_accepted_'+('optimized' if optimized else 'normal')+'.json')
        checks[kind]=verify_current_record(recorded(folder/(kind+'.record.json')),folder,repo,snapshot,archive,output,optimized)
    integrity=verify_frozen(repo,evidence,ledger_sha);tests=verify_tests(repo,evidence)
    matrix=read(repo/'outputs/member1_rain_step6/STEP6_ACCEPTANCE_FILLED.json')
    require(isinstance(matrix,Mapping),'INVALID_DATA','historical_matrix','reviewed A01-A60 matrix required')
    return {'schema_version':VERSION,'gate':'STEP6_GUARD_ACCEPTANCE_G01_G07_PASS','validator_version':VALIDATOR_VERSION,'baseline_zip_sha256':BASELINE_SHA256,
      'historical_manifest_pins':{run:v[1] for run,v in TARGETS.items()},'current_receipts':{k:pin(repo/('outputs/member1_rain_guard_closure/receipts/current_checker_accepted_'+('optimized' if k.endswith('optimized') else 'normal')+'.json')) for k in checks},
      'source_gates':sources,'legacy_raw_profile_checks':9,'tests':tests,'frozen_integrity':integrity,
      'current_checker_dependencies':code_dependencies(repo),'solver_rerun':False,'road_search_run':False,
      'G01_G08':{**{f'G{i:02d}':'PASS' for i in range(1,8)},'G08':'PENDING_PACKAGE_INTEGRITY'},
      'historical_A01_A60':{'path':'outputs/member1_rain_step6/STEP6_ACCEPTANCE_FILLED.json',**pin(repo/'outputs/member1_rain_step6/STEP6_ACCEPTANCE_FILLED.json'),'scope':'historical execution plus CURRENT_CHECKER_REVALIDATION; no solver or road search rerun'},
      'limits':['GENERAL_M1_NOT_VALIDATED','E4_NOT_RUN','PRODUCTION_CALIBRATION_UNCONFIGURED','FINITE_DOMAIN_NOT_GLOBAL_OPTIMAL','SYNTHETIC_RAIN_NOT_OBSERVED','LATENCY_707_718_SECONDS_HISTORICAL_NOT_REALTIME']}

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot-root',required=True,type=Path);p.add_argument('--baseline-zip',required=True,type=Path);p.add_argument('--preflight-sha256',required=True);p.add_argument('--output',required=True,type=Path);a=p.parse_args()
    try:
        require(not a.output.exists(),'OUTPUT_EXISTS','output','new checkpoint required')
        repo=Path(__file__).resolve().parents[2];result=verify(repo,a.snapshot_root.resolve(),a.baseline_zip.resolve(),repo/'outputs/member1_rain_guard_closure/evidence',a.preflight_sha256)
        with a.output.open('x',encoding='utf8') as f:f.write(json.dumps(result,sort_keys=True,indent=2,allow_nan=False)+'\n')
        print(json.dumps({'gate':result['gate'],'output':str(a.output),**pin(a.output)}));return 0
    except (DynamicError,OSError,ValueError,KeyError,zipfile.BadZipFile) as e:
        print(json.dumps({'status':'BLOCKED','diagnostic':{'code':getattr(e,'code','GUARD_ACCEPTANCE'),'path':getattr(e,'path','$'),'message':str(e)}}),file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
