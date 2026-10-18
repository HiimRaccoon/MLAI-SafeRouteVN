"""Bound raw revalidation CLI. Trusted manifest hash is external to artifacts."""
import argparse,hashlib,json,sys
from pathlib import Path,PurePosixPath
from typing import Mapping
from optimization.models.decision_state import DecisionState,StateContractError
from optimization.models.member1_dynamic_state import require,finite_tree,DynamicError
from optimization.integration.member1_decision_state_adapter import load_pinned_initial_states
from optimization.integration.member1_static_runner import _verified_source,_sha
from optimization.integration.member1_s1_runner import _require_no_sqlite_sidecars,S1SidecarError
from optimization.integration.member1_s0_graph import Member1RoadGraph,RoadDataError
from optimization.integration.member1_profiles import load_profile_config
from optimization.rolling_horizon.member1_rain_source import load_source_model,PINS
from optimization.rolling_horizon.member1_rain_validation import validate_rain,validate_forecast_sample,independent_segments,close


def read(path):
    def pairs(items):
        result={}
        for k,v in items:
            require(k not in result,'DUPLICATE_JSON_KEY',str(path)+'.'+k,'duplicate JSON key');result[k]=v
        return result
    value=json.loads(Path(path).read_bytes(),object_pairs_hook=pairs);finite_tree(value,str(path));return value


def check(snapshot,run_root,trusted_manifest_sha256):
    snapshot=Path(snapshot).resolve();directory=Path(run_root).resolve();repo=Path(__file__).resolve().parents[2]
    require(isinstance(trusted_manifest_sha256,str) and len(trusted_manifest_sha256)==64,'INVALID_DATA','trusted_manifest_sha256','external trusted raw digest required')
    require(_sha(directory/'manifest.json')==trusted_manifest_sha256,'TRUSTED_MANIFEST_BINDING','manifest.json','not the externally trusted execution manifest')
    manifest=read(directory/'manifest.json');require(isinstance(manifest,Mapping),'INVALID_DATA','manifest','object required')
    require(manifest.get('schema_version')=='task02-m1-temporal-rain-run-manifest/1' and manifest.get('gate')=='S4_TEMPORAL_WITNESSES_VALIDATED' and manifest.get('snapshot_verified') is True,'RUN_GATE','manifest.gate','native run is not certified')
    require(manifest.get('run_id')==directory.name and manifest.get('scenario_id')=='S4','RUN_BINDING','run_id','own unique S4 run required')
    require(isinstance(manifest.get('files'),Mapping) and isinstance(manifest.get('code_sha256'),Mapping) and isinstance(manifest.get('code_bytes'),Mapping),'INVALID_DATA','manifest.files','artifact/code/byte object required')
    for key in ('files','code_sha256'):
        for name,record in manifest[key].items():
            require(isinstance(name,str),'INVALID_DATA','manifest.'+key,'string reference required')
            p=PurePosixPath(name);require(not p.is_absolute() and '..' not in p.parts and '\\' not in name,'PATH_UNSAFE',name,'safe relative reference required')
            if key=='files':require(isinstance(record,Mapping) and isinstance(record.get('sha256'),str) and 'bytes' in record,'INVALID_DATA','manifest.files.'+name,'hash/byte record required')
            else:require(isinstance(record,str) and name in manifest['code_bytes'],'INVALID_DATA','manifest.code_sha256.'+name,'hash and byte binding required')
            path=(directory/name) if key=='files' else repo/name
            expected=record['sha256'] if key=='files' else record
            require(path.is_file() and _sha(path)==expected,'PAYLOAD_BINDING' if key=='files' else 'CODE_BINDING',name,'raw execution bytes differ')
            size=record['bytes'] if key=='files' else manifest['code_bytes'][name]
            require(type(size) is int and size>=0 and path.stat().st_size==size,'PAYLOAD_BINDING',name,'raw byte count differs')
    require(manifest.get('code_sha256')==manifest.get('code_sha256_after'),'CODE_CHANGED','manifest.code_sha256','execution code changed during run')
    batch=load_pinned_initial_states(snapshot);require(batch.get('status')=='INITIAL_STATE_READY' and batch.get('source_gate')=='M1_SOURCE_CONTRACT_GATE_PASS','SOURCE_GATE','snapshot_root','receipt-gated source is required')
    state=DecisionState.from_dict(next(s for s in batch['states'] if s['scenario_id']=='S4'))
    get=lambda name:read(directory/name)
    require(state.to_dict()==get('initial_state.json'),'SOURCE_BINDING','initial_state.json','loaded state differs from pinned fixture bytes')
    paths,base=_verified_source(snapshot,state);paths={**paths,**{'primary:'+rel:snapshot/rel for rel in PINS}}
    source={k:_sha(p) for k,p in paths.items()}
    require(source==manifest.get('source_hashes_before')==manifest.get('source_hashes_after'),'SOURCE_BINDING','manifest.source_hashes','independently pinned actual source differs')
    config=load_profile_config(repo/'configs/member1_profiles_step3.json')
    require(_sha(repo/'configs/member1_rain_step6.json')==manifest.get('temporal_config_sha256') and _sha(repo/'configs/member1_profiles_step3.json')==manifest.get('profile_config_sha256'),'CONFIG_BINDING','manifest.config','execution config differs')
    _require_no_sqlite_sidecars(paths,phase='before_open');validations={}
    try:
        with Member1RoadGraph(paths['network_sqlite_sha256'],paths['features_sqlite_sha256'],routing_version=state.routing_version,features_version=state.features_version,context_version=state.context_version,source_hashes=source) as graph:
            model=load_source_model(snapshot,state);overlay=get('overlay.json');head=get('transition.json')['after_state']
            for profile in ('FASTEST','BALANCED','SAFER'):
                result=get(profile.lower()+'_solution.json')
                v=validate_rain(state,graph,get('temporal_domain.json'),result,config,model=model,overlay=overlay,
                    plan=get('pre_plan.json'),accepted=get('accepted_plan_receipt.json'),before=get('before_state.json'),transition=get('transition.json'),authority=get('authority_export.json'))
                require(v.get('valid') is True,'RAW_REVALIDATION',profile,str(v['diagnostics']))
                samples=get(profile.lower()+'_forecast_samples.json')
                require(isinstance(samples,Mapping) and isinstance(samples.get('samples'),list) and bool(samples['samples']),'INVALID_DATA','forecast_samples','raw temporal samples required')
                for sample in samples['samples']:
                    sv=validate_forecast_sample(state,graph,model,overlay,head,result,sample)
                    require(sv['valid'] is True,'RAW_REVALIDATION',profile+'.forecast_samples',str(sv['diagnostics']))
                validations[profile]=v
            probes=get('raw_edge_probes.json');require(isinstance(probes,Mapping) and isinstance(probes.get('probes'),list),'INVALID_DATA','raw_probes','raw probe records required')
            require(all(isinstance(p,Mapping) and isinstance(p.get('evaluation'),Mapping) for p in probes['probes']),'INVALID_DATA','raw_probes.probes','probe/evaluation objects required')
            require(any(p.get('affected') is True for p in probes['probes']) and any(p.get('affected') is False for p in probes['probes']),'PROBE_COVERAGE','raw_probes','affected and unaffected evidence required')
            for p in probes['probes']:
                row=model.ingredients(graph,p['edge_id']);evaluation=p['evaluation']
                expected=independent_segments(row,overlay,p['edge_id'],evaluation['start_us'],0,None)
                require(evaluation.get('segments')==expected,'PROBE_BINDING','raw_probes.segments','independent raw piecewise probe differs')
                close(evaluation.get('exposure'),sum(s['exposure'] for s in expected),'raw_probes.exposure')
                close(evaluation.get('distance_m'),sum(s['distance_m'] for s in expected),'raw_probes.distance_m')
    finally:_require_no_sqlite_sidecars(paths,phase='after_close')
    require(source=={k:_sha(p) for k,p in paths.items()},'SOURCE_CHANGED','snapshot_root','source changed during raw revalidation')
    return {'schema_version':'task02-m1-temporal-rain-bound-revalidation/1','status':'S4_TEMPORAL_RAW_REVALIDATION_PASS',
            'run_id':manifest['run_id'],'trusted_manifest_sha256':trusted_manifest_sha256,'validations':validations,
            'source_unchanged':True,'solver_rerun':False,'road_search_run':False,'scope':'S4_FORECAST_FEASIBILITY_NOT_NATIVE_SOURCE_CLI_ACCEPTANCE'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot-root',required=True);parser.add_argument('--run-root',required=True);parser.add_argument('--trusted-manifest-sha256',required=True);a=parser.parse_args()
    try:print(json.dumps(check(a.snapshot_root,a.run_root,a.trusted_manifest_sha256)));return 0
    except (DynamicError,StateContractError,RoadDataError,S1SidecarError,OSError,ValueError) as error:
        print(json.dumps({'status':'FAIL','diagnostic':{'code':getattr(error,'code','RAIN_CHECKER'),'path':getattr(error,'path','$'),'message':str(error)}}),file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
