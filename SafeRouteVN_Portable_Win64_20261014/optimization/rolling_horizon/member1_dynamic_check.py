"""Read-only bound artifact revalidation; never runs a planner or event apply.

The trusted manifest digest is supplied by the M2 checkpoint/persistence layer,
not derived from a candidate. This is a local pin, not a signature.
"""
import argparse,hashlib,json,sys
from pathlib import Path
from optimization.models.decision_state import DecisionState
from optimization.models.member1_dynamic_state import DynamicError,require,sha
from optimization.integration.member1_decision_state_adapter import load_pinned_initial_states
from optimization.integration.member1_static_runner import _verified_source,_sha
from optimization.integration.member1_s1_runner import _require_no_sqlite_sidecars
from optimization.integration.member1_s0_graph import Member1RoadGraph,RoadDataError
from optimization.integration.member1_profiles import load_profile_config
from optimization.rolling_horizon.member1_dynamic_validation import validate_dynamic

def read(path):
    value=json.loads(Path(path).read_bytes());require(isinstance(value,dict),'INVALID_DATA',str(path),'object required');return value

def check(snapshot,run,manifest_sha,*,candidate_before=None,candidate_transition=None,candidate_event=None,expected_head=None,candidate_solution=None):
    directory=Path(run).resolve();manifest_path=directory/'manifest.json'
    require(_sha(manifest_path)==manifest_sha,'MANIFEST_BINDING','manifest','trusted raw manifest digest differs')
    m=read(manifest_path)
    for name,record in m['files'].items():
        path=(directory/name).resolve();require(path.parent==directory,'ARTIFACT_PATH','files','flat safe artifact name required')
        require(path.stat().st_size==record['bytes'] and _sha(path)==record['sha256'],'ARTIFACT_BINDING',name,'raw payload bytes differ')
    batch=load_pinned_initial_states(snapshot);require(batch.get('source_gate')=='M1_SOURCE_CONTRACT_GATE_PASS','SOURCE_GATE','snapshot_root','receipt gate failed')
    state=DecisionState.from_dict(next(x for x in batch['states'] if x['scenario_id']==m['scenario_id']))
    require(read(directory/'initial_state.json')==state.to_dict(),'SOURCE_BINDING','initial_state','source initial differs')
    paths,source=_verified_source(Path(snapshot).resolve(),state)
    require(source==m['source_hashes_before']==m['source_hashes_after'],'SOURCE_BINDING','source_hashes','pinned raw source differs')
    b=read(candidate_before or directory/'before_state.json');t=read(candidate_transition or directory/'transition.json')
    authority=read(directory/'ledger.json');plan=read(directory/'pre_plan.json');accepted=read(directory/'accepted_plan_receipt.json')
    if expected_head is not None:require(expected_head==authority['root']['before']['content_sha256'],'STALE_HEAD','expected_head','persisted head differs')
    if candidate_event:
        e=read(candidate_event);trusted=state.pending_events[0].to_dict()
        require(e==trusted,'EVENT_CONFLICT' if e.get('event_id')==trusted['event_id'] else 'EVENT_BINDING','event','event differs from exactly-once trusted receipt')
    domain=read(directory/'dynamic_domain.json');repo=Path(__file__).resolve().parents[2];config=load_profile_config(repo/'configs/member1_profiles_step3.json')
    _require_no_sqlite_sidecars(paths,phase='before_open')
    try:
        with Member1RoadGraph(paths['network_sqlite_sha256'],paths['features_sqlite_sha256'],routing_version=state.routing_version,features_version=state.features_version,context_version=state.context_version,source_hashes=source) as graph:
            checks={}
            pre=validate_dynamic(state,graph,read(directory/'pre_domain.json'),read(directory/'pre_result.json'),config)
            require(pre['valid'],'OWN_PREPLAN_REQUIRED','pre_result',str(pre['diagnostics']))
            for profile in ('FASTEST','BALANCED','SAFER'):
                result=read(candidate_solution if candidate_solution and profile=='BALANCED' else directory/(profile.lower()+'_solution.json'))
                v=validate_dynamic(state,graph,domain,result,config,plan=plan,accepted=accepted,before=b,transition=t,authority=authority)
                if not v['valid']:
                    diagnostic=v['diagnostics'][0];raise DynamicError(diagnostic['code'],diagnostic['path'],diagnostic['message'])
                checks[profile]=v
    finally:_require_no_sqlite_sidecars(paths,phase='after_close')
    require({k:_sha(p) for k,p in paths.items()}==source,'SOURCE_CHANGED','source_hashes','source changed during revalidation')
    return {'status':'DYNAMIC_BOUND_REVALIDATION_PASS','run_id':m['run_id'],'checks':checks,'solver_executed':False,'event_applied':False}

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot-root',required=True);p.add_argument('--run-root',required=True);p.add_argument('--trusted-manifest-sha256',required=True)
    for key in ('candidate-before','candidate-transition','candidate-event','expected-head','candidate-solution'):p.add_argument('--'+key)
    a=p.parse_args()
    try:
        result=check(a.snapshot_root,a.run_root,a.trusted_manifest_sha256,candidate_before=a.candidate_before,candidate_transition=a.candidate_transition,candidate_event=a.candidate_event,expected_head=a.expected_head,candidate_solution=a.candidate_solution)
        print(json.dumps(result));return 0
    except (DynamicError,RoadDataError,OSError,ValueError) as e:
        print(json.dumps({'status':'FAIL','diagnostic':{'code':getattr(e,'code','SOURCE_OR_ARTIFACT_INVALID'),'path':getattr(e,'path','$'),'message':str(e)}}),file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
