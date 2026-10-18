"""Read-only current-checker raw validation of pinned S2/S3 Step5 executions.

Historical execution hashes remain historical. No solver or road search rerun,
no overwrite, no substitution of current code hashes for old execution hashes.
"""
import argparse,hashlib,json
from pathlib import Path
from optimization.models.decision_state import DecisionState
from optimization.models.member1_dynamic_state import require,DynamicError
from optimization.integration.member1_decision_state_adapter import load_pinned_initial_states
from optimization.integration.member1_static_runner import _verified_source,_sha
from optimization.integration.member1_s1_runner import _require_no_sqlite_sidecars,S1SidecarError
from optimization.integration.member1_s0_graph import Member1RoadGraph,RoadDataError
from optimization.integration.member1_profiles import load_profile_config
from optimization.rolling_horizon.member1_dynamic_validation import validate_dynamic

PINS={
 'outputs/member1_dynamic_step5_closure/final/S2_DYNAMIC_20261003T074115471461Z':'4dfc11daef457b493fb500834806fba2cca09a7b5d73f36ed89996e739f4a767',
 'outputs/member1_dynamic_step5_closure/final/S3_DYNAMIC_20261003T074127081973Z':'71daf9464eadbb86de183c34802ae1ef1ea1688efd31efb7b3d59473c1b78b34',
 'outputs/member1_dynamic_step5_closure_optimized/final/S3_DYNAMIC_20261003T074139705534Z':'5980e771b139e253107a9f0378e37e8628039c639712eaef1051fdc6d8752dde',
}

def check(snapshot,repo):
    batch=load_pinned_initial_states(snapshot);require(batch.get('status')=='INITIAL_STATE_READY','SOURCE_GATE','snapshot_root','receipt gate failed')
    states={s['scenario_id']:DecisionState.from_dict(s) for s in batch['states']};config=load_profile_config(repo/'configs/member1_profiles_step3.json');rows=[]
    for relative,pin in PINS.items():
        directory=repo/relative;require(_sha(directory/'manifest.json')==pin,'HISTORICAL_MANIFEST_BINDING',relative,'pinned manifest differs')
        manifest=json.loads((directory/'manifest.json').read_bytes())
        for name,record in manifest['files'].items():
            path=(directory/name).resolve();require(path.is_relative_to(directory.resolve()),'PATH_UNSAFE',name,'unsafe artifact reference')
            require(path.stat().st_size==record['bytes'] and _sha(path)==record['sha256'],'HISTORICAL_PAYLOAD_BINDING',name,'pinned payload differs')
        read=lambda name:json.loads((directory/name).read_bytes())
        state=states[manifest['scenario_id']];require(state.to_dict()==read('initial_state.json'),'SOURCE_BINDING','initial_state','raw receipt state differs')
        paths,hashes=_verified_source(snapshot,state);require(hashes==manifest['source_hashes_before']==manifest['source_hashes_after'],'SOURCE_BINDING','source_hashes','historical/current raw inputs differ')
        _require_no_sqlite_sidecars(paths,phase='before_open')
        checks={}
        try:
            with Member1RoadGraph(paths['network_sqlite_sha256'],paths['features_sqlite_sha256'],routing_version=state.routing_version,features_version=state.features_version,context_version=state.context_version,source_hashes=hashes) as graph:
                for profile in ('FASTEST','BALANCED','SAFER'):
                    out=validate_dynamic(state,graph,read('dynamic_domain.json'),read(profile.lower()+'_solution.json'),config,
                        plan=read('pre_plan.json'),accepted=read('accepted_plan_receipt.json'),before=read('before_state.json'),transition=read('transition.json'),authority=read('ledger.json'))
                    require(out.get('valid') is True,'LEGACY_REVALIDATION',relative+'.'+profile,str(out['diagnostics']));checks[profile]=out
        finally:_require_no_sqlite_sidecars(paths,phase='after_close')
        require(hashes=={k:_sha(p) for k,p in paths.items()},'SOURCE_CHANGED','snapshot_root','source changed during read-only revalidation')
        rows.append({'run_id':manifest['run_id'],'historical_manifest_sha256':pin,'historical_execution_code_sha256':manifest['code_sha256'],
                     'current_validations':checks,'source_unchanged':True})
    modules=['optimization/rolling_horizon/member1_dynamic_validation.py','optimization/rolling_horizon/member1_motion_validation.py','optimization/models/member1_dynamic_state.py','optimization/rolling_horizon/member1_rain_legacy_revalidation.py']
    return {'schema_version':'task02-m1-step5-current-raw-revalidation/1','status':'STEP5_CURRENT_RAW_REVALIDATION_PASS','runs':rows,
        'current_checker_sha256':{p:_sha(repo/p) for p in modules},'solver_rerun':False,'road_search_run':False}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot-root',required=True,type=Path);parser.add_argument('--output',required=True,type=Path);args=parser.parse_args()
    try:
        result=check(args.snapshot_root.resolve(),Path(__file__).resolve().parents[2]);args.output.parent.mkdir(parents=True,exist_ok=True)
        require(not args.output.exists(),'OUTPUT_EXISTS','output','new receipt path required')
        args.output.write_text(json.dumps(result,sort_keys=True,indent=2)+'\n',encoding='utf8');print(json.dumps({'status':result['status'],'output':str(args.output),'sha256':_sha(args.output)}));return 0
    except (DynamicError,RoadDataError,S1SidecarError,OSError,ValueError) as error:
        print(json.dumps({'status':'FAIL','diagnostic':{'code':getattr(error,'code','REVALIDATION_ERROR'),'path':getattr(error,'path','$'),'message':str(error)}}));return 2
if __name__=='__main__':raise SystemExit(main())
