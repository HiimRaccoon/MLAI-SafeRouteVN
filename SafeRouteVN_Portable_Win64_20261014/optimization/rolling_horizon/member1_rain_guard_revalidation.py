"""Current checker /2 revalidation of externally pinned Step6 executions.

Change Impact Analysis: new read-only entrypoint; strict rain_check CODE_BINDING
is unchanged. Historical validator bytes are authenticated from the reviewed
archive, not relabelled as current execution. No solver/search/producer calls.
"""
import argparse,ast,hashlib,json,platform,sys,zipfile
from importlib.util import resolve_name
from importlib.metadata import version
from datetime import datetime,timezone
from pathlib import Path,PurePosixPath
from typing import Mapping
from optimization.models.decision_state import DecisionState,StateContractError
from optimization.models.member1_dynamic_state import require,DynamicError
from optimization.integration.member1_decision_state_adapter import load_pinned_initial_states
from optimization.integration.member1_static_runner import _verified_source,_sha
from optimization.integration.member1_s1_runner import _require_no_sqlite_sidecars,S1SidecarError
from optimization.integration.member1_s0_graph import Member1RoadGraph,RoadDataError
from optimization.integration.member1_profiles import load_profile_config
from optimization.rolling_horizon.member1_rain_source import load_source_model,PINS
from optimization.rolling_horizon.member1_rain_check import read
from optimization.rolling_horizon.member1_rain_validation import (
    validate_rain,validate_forecast_sample,check_temporal_route,independent_segments,close,VALIDATOR_VERSION)

VERSION='task02-m1-step6-current-checker-revalidation/1'
BASELINE_SHA256='15c4a5074819e708a8380af0b2658849e8ce86413e1f7d0eb00450c2cfe762e7'
TARGETS={
 'S4_RAIN_20261003T122053740265Z':('outputs/member1_rain_step6/accepted','80b9a286598e5f4813275bd872b8d3acf155e69a100cb28a7c0c05526bd0cbf4',19,False),
 'S4_RAIN_20261003T122055082991Z':('outputs/member1_rain_step6_optimized/accepted','a02919aa28c3aa10b38550008c833063661509a8314114b40b56c38fcda3f6a4',23,True)}
GUARD='optimization/rolling_horizon/member1_rain_validation.py'

def safe(name):
    require(isinstance(name,str) and bool(name),'PATH_UNSAFE','reference','relative path required')
    p=PurePosixPath(name)
    require(not p.is_absolute() and '..' not in p.parts and ':' not in name and '\\' not in name,'PATH_UNSAFE',name,'safe relative path required')
    return name

def pin(path):return {'bytes':path.stat().st_size,'sha256':_sha(path)}

def code_dependencies(repo):
    """Pin transitive local imports, including pure trust/config primitives.

    A superset includes legacy loaders' imports. None of their optimizer entry
    points is called. External Python packages are separately version-recorded.
    """
    todo=['optimization/rolling_horizon/member1_rain_guard_revalidation.py'];seen=set()
    while todo:
        rel=todo.pop()
        if rel in seen:continue
        seen.add(rel);tree=ast.parse((repo/rel).read_text(encoding='utf8'))
        for n in ast.walk(tree):
            module=n.module if isinstance(n,ast.ImportFrom) else None
            if isinstance(n,ast.ImportFrom) and n.level:
                module=resolve_name('.'*n.level+(n.module or ''),'.'.join(PurePosixPath(rel).parent.parts))
            names=[module] if module else [a.name for a in n.names] if isinstance(n,ast.Import) else []
            if isinstance(n,ast.ImportFrom) and module:
                # Package-style imports ("from integration import audit") are
                # real trust dependencies too, not just the package __init__.
                names += [module+'.'+a.name for a in n.names
                          if (repo/(module+'.'+a.name).replace('.','/')).with_suffix('.py').is_file()]
            for name in names:
                if name.startswith(('optimization.','shared.')):
                    part=name.replace('.','/');path=repo/(part+'.py')
                    if not path.is_file():path=repo/part/'__init__.py'
                    require(path.is_file(),'CHECKER_DEPENDENCY',name,'local import unavailable')
                    todo.append(path.relative_to(repo).as_posix())
    seen.update(('configs/member1_rain_step6.json','configs/member1_profiles_step3.json',
                 'configs/member1_dynamic_step5.json','optimization/integration/member1_trusted_receipt.json'))
    return {name:pin(repo/name) for name in sorted(seen)}

def historical_binding(repo,directory,archive):
    """External pins defeat copied payload+manifest self-consistency forgery."""
    require(directory.name in TARGETS,'TARGET_BINDING','run_id','only the two reviewed execution targets allowed')
    relative,digest,column_count,optimized=TARGETS[directory.name]
    require(_sha(archive)==BASELINE_SHA256,'BASELINE_BINDING','baseline_zip','external reviewed archive differs')
    require(_sha(directory/'manifest.json')==digest,'HISTORICAL_MANIFEST_BINDING','manifest.json','external execution manifest pin differs')
    m=read(directory/'manifest.json')
    require(isinstance(m,Mapping),'INVALID_DATA','manifest','object required')
    require(m.get('run_id')==directory.name and m.get('scenario_id')=='S4' and m.get('schema_version')=='task02-m1-temporal-rain-run-manifest/1','HISTORICAL_BINDING','manifest.run_id','execution identity differs')
    require(m.get('gate')=='S4_TEMPORAL_WITNESSES_VALIDATED' and m.get('snapshot_verified') is True,'HISTORICAL_GATE','manifest.gate','verified execution required')
    require(isinstance(m.get('platform'),Mapping) and m['platform'].get('system')=='Windows' and m['platform'].get('optimized_mode') is optimized,'HISTORICAL_PLATFORM','manifest.platform','historical native mode differs')
    for key in ('files','code_sha256','code_sha256_after','code_bytes'):
        require(isinstance(m.get(key),Mapping),'INVALID_DATA','manifest.'+key,'typed pin table required')
    require(m['code_sha256']==m['code_sha256_after'] and set(m['code_sha256'])==set(m['code_bytes']),'CODE_BINDING','manifest.code_sha256','historical before/after code differs')
    require(len(m['files'])==28 and len(m['code_sha256'])==34,'HISTORICAL_BINDING','manifest.files','complete reviewed execution records required')
    with zipfile.ZipFile(archive) as z:
        names=z.namelist();require(len(names)==len(set(names)) and z.testzip() is None,'BASELINE_BINDING','baseline_zip','CRC/duplicates invalid')
        for name in names:safe(name)
        require(z.read(relative+'/'+directory.name+'/manifest.json')==(directory/'manifest.json').read_bytes(),'HISTORICAL_MANIFEST_BINDING','manifest.json','archive manifest differs')
        for name,record in m['files'].items():
            safe(name);require(isinstance(record,Mapping) and type(record.get('bytes')) is int,'INVALID_DATA','manifest.files.'+name,'raw pin required')
            require(pin(directory/name)==record,'HISTORICAL_PAYLOAD_BINDING',name,'historical payload differs')
        for name,digest in m['code_sha256'].items():
            safe(name);require(isinstance(digest,str) and type(m['code_bytes'][name]) is int,'INVALID_DATA','manifest.code_sha256.'+name,'code hash/bytes required')
            expected={'bytes':m['code_bytes'][name],'sha256':digest}
            if name in names:
                raw=z.read(name)
                require({'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}==expected,'CODE_BINDING',name,'archive execution code differs')
            else:require(pin(repo/name)==expected,'CODE_BINDING',name,'dependency outside archive lacks matching execution bytes')
            # Only new certification rules may differ; engine/domain/scoring
            # and all other original execution inputs must still match bytes.
            if name!=GUARD:require(pin(repo/name)==expected,'ENGINE_INPUT_CHANGED',name,'revalidation-only requires identical execution dependencies')
    require(VALIDATOR_VERSION=='task02-m1-independent-temporal-rain-validator/2','VERSION_MISMATCH','current_checker.validator_version','current guard /2 required')
    return m,column_count

def revalidate_run(snapshot,repo,directory,archive):
    m,expected_columns=historical_binding(repo,directory,archive)
    require(version('ortools')==m.get('ortools_version'),'ENGINE_INPUT_CHANGED','ortools_version','engine package version differs from reviewed execution')
    before_code=code_dependencies(repo);get=lambda name:read(directory/name)
    batch=load_pinned_initial_states(snapshot)
    require(batch.get('status')=='INITIAL_STATE_READY' and batch.get('source_gate')=='M1_SOURCE_CONTRACT_GATE_PASS','SOURCE_GATE','snapshot_root','receipt-gated nine fixtures required')
    state=DecisionState.from_dict(next(s for s in batch['states'] if s['scenario_id']=='S4'))
    require(state.to_dict()==get('initial_state.json'),'SOURCE_BINDING','initial_state.json','state differs from authenticated fixture')
    paths,_=_verified_source(snapshot,state)
    paths={**paths,**{'primary:'+rel:snapshot/rel for rel in PINS}}
    source={k:_sha(p) for k,p in paths.items()}
    require(source==m['source_hashes_before']==m['source_hashes_after'],'SOURCE_BINDING','source_hashes','historical/current raw inputs differ')
    config=load_profile_config(repo/'configs/member1_profiles_step3.json')
    require(_sha(repo/'configs/member1_rain_step6.json')==m['temporal_config_sha256'] and _sha(repo/'configs/member1_profiles_step3.json')==m['profile_config_sha256'],'CONFIG_BINDING','config','engine config changed')
    _require_no_sqlite_sidecars(paths,phase='before_open')
    checks={};samples_count=0
    try:
        with Member1RoadGraph(paths['network_sqlite_sha256'],paths['features_sqlite_sha256'],routing_version=state.routing_version,features_version=state.features_version,context_version=state.context_version,source_hashes=source) as graph:
            model=load_source_model(snapshot,state);overlay=get('overlay.json');tx=get('transition.json');head=tx['after_state'];domain=get('temporal_domain.json')
            require(isinstance(domain,Mapping) and isinstance(domain.get('columns'),list) and len(domain['columns'])==expected_columns,'DOMAIN_COVERAGE','temporal_domain.columns','complete reviewed domain required')
            for profile in ('FASTEST','BALANCED','SAFER'):
                result=get(profile.lower()+'_solution.json')
                v=validate_rain(state,graph,domain,result,config,model=model,overlay=overlay,plan=get('pre_plan.json'),accepted=get('accepted_plan_receipt.json'),before=get('before_state.json'),transition=tx,authority=get('authority_export.json'))
                require(v.get('valid') is True,'RAW_REVALIDATION',profile,str(v['diagnostics']))
                samples=get(profile.lower()+'_forecast_samples.json')
                require(isinstance(samples,Mapping) and isinstance(samples.get('samples'),list) and len(samples['samples'])==6,'SAMPLE_COVERAGE',profile+'.samples','six reviewed forecast queries required')
                rows=[validate_forecast_sample(state,graph,model,overlay,head,result,s) for s in samples['samples']]
                require(all(x['valid'] is True for x in rows),'RAW_REVALIDATION',profile+'.samples',str(rows));samples_count+=len(rows)
                checks[profile]={'validation':v,'forecast_validations':rows,'status':result['status'],'served_orders':result['served_orders'],'unserved_orders':result['unserved_orders'],'whole_metrics':result['whole_trajectory_metrics']}
            probes=get('raw_edge_probes.json')
            require(isinstance(probes,Mapping) and isinstance(probes.get('probes'),list) and len(probes['probes'])==12,'PROBE_COVERAGE','raw_edge_probes','12 exact reviewed probes required')
            for i,p in enumerate(probes['probes']):
                path=f'raw_edge_probes.probes[{i}]';require(isinstance(p,Mapping) and isinstance(p.get('evaluation'),Mapping),'INVALID_DATA',path,'probe/evaluation objects required')
                row=model.ingredients(graph,p['edge_id']);e=p['evaluation']
                require(p.get('ingredients')==row and p.get('affected') is (p['edge_id'] in overlay['cost_table']),'PROBE_BINDING',path+'.ingredients','raw ingredient/affected evidence differs')
                segments=independent_segments(row,overlay,p['edge_id'],e['start_us'],0,None)
                require(e.get('segments')==segments and e.get('end_us')==segments[-1]['end_us'],'PROBE_BINDING',path+'.segments','raw rational boundaries differ')
                close(e.get('exposure'),sum(x['exposure'] for x in segments),path+'.exposure');close(e.get('distance_m'),sum(x['distance_m'] for x in segments),path+'.distance_m')
            cf=get('counterfactual.json')
            require(isinstance(cf,Mapping) and isinstance(cf.get('vehicle_routes'),list),'INVALID_DATA','counterfactual','route array required')
            for route in cf['vehicle_routes']:check_temporal_route(state,graph,route,{o.order_id:o for o in state.orders},head,model,overlay)
            def wet(routes):return sum(s['layer']=='WET' for r in routes for a in r['actions'] if a['kind']=='EDGE' for s in a['temporal_segments'])
            wet_scope={'selected':{p:wet(get(p.lower()+'_solution.json')['vehicle_routes']) for p in checks},'pool_segments':wet(domain['columns']),'counterfactual_segments':wet(cf['vehicle_routes'])}
    finally:_require_no_sqlite_sidecars(paths,phase='after_close')
    require(source=={k:_sha(p) for k,p in paths.items()},'SOURCE_CHANGED','snapshot_root','source changed during raw checks')
    require(before_code==code_dependencies(repo),'CURRENT_CHECKER_CHANGED','current_checker','validation dependency changed during raw checks')
    # Recheck external manifest, payloads and execution dependencies after I/O.
    historical_binding(repo,directory,archive)
    return {'run_id':m['run_id'],'historical_manifest_sha256':TARGETS[m['run_id']][1],
      'historical_execution_code_sha256':m['code_sha256'],'historical_execution_code_bytes':m['code_bytes'],
      'historical_telemetry':{'elapsed_seconds':m['elapsed_seconds'],'stages':m['stages'],'limits':m['limits']},
      'current_checker_version':VALIDATOR_VERSION,'current_checker_before':before_code,'current_checker_after':before_code,
      'source_hashes_before':source,'source_hashes_after':source,'source_unchanged':True,'sidecars_before_open_absent':True,'sidecars_after_close_absent':True,
      'counts':{'columns':expected_columns,'profiles':3,'samples':samples_count,'probes':12,'counterfactuals':1},'profiles':checks,'wet_scope':wet_scope,
      'counterfactual_validation':'RAW_FEASIBLE_FIXED_SEQUENCE_FORECAST','solver_rerun':False,'road_search_run':False}

def validate_receipt(value):
    require(isinstance(value,Mapping),'INVALID_DATA','receipt','receipt object required')
    require(value.get('schema_version')==VERSION and value.get('status')=='STEP6_CURRENT_CHECKER_REVALIDATION_PASS','RECEIPT_BINDING','receipt.status','current check receipt required')
    require(value.get('baseline_zip_sha256')==BASELINE_SHA256 and value.get('solver_rerun') is False and value.get('road_search_run') is False,'RECEIPT_BINDING','receipt.baseline_zip_sha256','external baseline/no-rerun identity required')
    rows=value.get('runs');require(isinstance(rows,list) and len(rows)==2 and all(isinstance(x,Mapping) and isinstance(x.get('run_id'),str) for x in rows),'INVALID_DATA','receipt.runs','two typed target records required')
    require({r['run_id'] for r in rows}==set(TARGETS) and len({r['run_id'] for r in rows})==2,'TARGET_BINDING','receipt.runs','exact unique reviewed targets required')
    for i,r in enumerate(rows):
        path=f'receipt.runs[{i}]';target=TARGETS[r['run_id']]
        require(r.get('historical_manifest_sha256')==target[1] and r.get('current_checker_version')==VALIDATOR_VERSION,'RECEIPT_BINDING',path,'execution/current checker identity differs')
        require(r.get('counts')=={'columns':target[2],'profiles':3,'samples':18,'probes':12,'counterfactuals':1},'RECEIPT_COVERAGE',path+'.counts','raw coverage differs')
        require(all(type(v) is int for v in r['counts'].values()),'INVALID_DATA',path+'.counts','native integer counts, no boolean aliases')
        for before,after in [('current_checker_before','current_checker_after'),('source_hashes_before','source_hashes_after')]:
            require(isinstance(r.get(before),Mapping) and bool(r[before]) and r[before]==r.get(after),'RECEIPT_BINDING',path+'.'+before,'stable before/after pins required')
        require(r.get('solver_rerun') is False and r.get('road_search_run') is False,'RECEIPT_BINDING',path,'checker is not an execution')
        for flag in ('source_unchanged','sidecars_before_open_absent','sidecars_after_close_absent'):
            require(r.get(flag) is True,'RECEIPT_BINDING',path+'.'+flag,'raw boundary certification required')
        profiles=r.get('profiles')
        require(isinstance(profiles,Mapping) and set(profiles)=={'FASTEST','BALANCED','SAFER'},'RECEIPT_COVERAGE',path+'.profiles','exact profile validations required')
        for profile,v in profiles.items():
            p=path+'.profiles.'+profile
            require(isinstance(v,Mapping) and isinstance(v.get('validation'),Mapping),'INVALID_DATA',p,'validation record required')
            require(v['validation'].get('valid') is True and v['validation'].get('validator_version')==VALIDATOR_VERSION and v['validation'].get('diagnostics')==[],'RECEIPT_VALIDATION',p+'.validation','current raw validator must certify witness')
            require(v.get('status')=='FEASIBLE' and v.get('served_orders')==[f'O{j:03d}' for j in range(1,9)] and v.get('unserved_orders')==[],'RECEIPT_COVERAGE',p,'full eight order witness required')
            sv=v.get('forecast_validations')
            require(isinstance(sv,list) and len(sv)==6 and all(isinstance(x,Mapping) and x.get('valid') is True and x.get('validator_version')==VALIDATOR_VERSION and x.get('diagnostics')==[] for x in sv),'RECEIPT_VALIDATION',p+'.forecast_validations','six current sample checks required')
    return value

def check(snapshot,archive,repo=None):
    repo=Path(repo or Path(__file__).resolve().parents[2]).resolve();snapshot=Path(snapshot).resolve();archive=Path(archive).resolve()
    started=datetime.now(timezone.utc).isoformat();before=code_dependencies(repo)
    rows=[revalidate_run(snapshot,repo,repo/rel/run,archive) for run,(rel,_,_,_) in TARGETS.items()]
    require(before==code_dependencies(repo),'CURRENT_CHECKER_CHANGED','current_checker','batch checker changed')
    return validate_receipt({'schema_version':VERSION,'status':'STEP6_CURRENT_CHECKER_REVALIDATION_PASS','baseline_zip_sha256':BASELINE_SHA256,
      'runs':rows,'solver_rerun':False,'road_search_run':False,'current_checker_version':VALIDATOR_VERSION,
      'platform':{'system':platform.system(),'python':platform.python_version(),'interpreter':sys.executable,'optimized_mode':sys.flags.optimize>0},
      'package_versions':{p:version(p) for p in ('ortools','pytest','jsonschema','psutil')},
      'started_at_utc':started,'ended_at_utc':datetime.now(timezone.utc).isoformat(),
      'scope':'PINNED_DOMAINS_RAW_FEASIBILITY; LOCAL_PINS_NOT_SIGNATURES; NOT_GLOBAL_OPTIMALITY_OR_ATOMIC_FILESYSTEM_SNAPSHOT'})

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot-root',required=True,type=Path);p.add_argument('--baseline-zip',required=True,type=Path);p.add_argument('--output',required=True,type=Path);a=p.parse_args()
    try:
        require(not a.output.exists(),'OUTPUT_EXISTS','output','new receipt only')
        result=check(a.snapshot_root,a.baseline_zip);a.output.parent.mkdir(parents=True,exist_ok=True)
        with a.output.open('x',encoding='utf8') as f:f.write(json.dumps(result,sort_keys=True,indent=2,allow_nan=False)+'\n')
        print(json.dumps({'status':result['status'],'output':str(a.output.resolve()),**pin(a.output),'counts':{'columns':42,'profiles':6,'samples':36,'probes':24},'solver_rerun':False,'road_search_run':False}));return 0
    except (DynamicError,StateContractError,RoadDataError,S1SidecarError,OSError,ValueError,zipfile.BadZipFile) as e:
        print(json.dumps({'status':'FAIL','diagnostic':{'code':getattr(e,'code','REVALIDATION_ERROR'),'path':getattr(e,'path','$'),'message':str(e)}}),file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
