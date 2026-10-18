"""Byte-preserving native source commands for Step5, no solver certificate."""
import argparse,json,sys
from pathlib import Path
from optimization.rolling_horizon.member1_motion_command_evidence import capture
from optimization.rolling_horizon.member1_command_contract import verify_semantics
from optimization.rolling_horizon.member1_motion_checkpoint import verify_command_record

def sources(snapshot,output):
    repo=Path(__file__).resolve().parents[2];snapshot=Path(snapshot).resolve();output=Path(output).resolve()
    m1=str(snapshot/'.venv/Scripts/python.exe');py=sys.executable
    commands=[('m1_pyproj',[m1,'-c','import pyproj; print(pyproj.__version__)'],snapshot),
        ('m1_verify',[m1,'-m','geo_data.cli','verify-scenarios','--scenarios-root','scenarios','--suite-id','thu-duc-binh-thanh-v1'],snapshot),
        ('task_audit',[py,'-m','optimization.integration.member1_mapping_audit','--snapshot-root',str(snapshot)],repo),
        ('crosswalk',[py,'-m','optimization.tests.member1_decision_state_real_source_gate','--snapshot-root',str(snapshot)],repo),
        ('crosswalk_optimized',[py,'-O','-m','optimization.tests.member1_decision_state_real_source_gate','--snapshot-root',str(snapshot)],repo),
        ('legacy_source',[py,'-m','optimization.tests.member1_profile_legacy_source_gate','--snapshot-root',str(snapshot),'--repo-root',str(repo)],repo),
        ('binding_source',[py,'-m','optimization.tests.member1_profile_binding_source_gate','--snapshot-root',str(snapshot),'--repo-root',str(repo),'--output',str(output/'step3_revalidation.json')],repo)]
    records=[];verdicts=[]
    for kind,command,cwd in commands:
        record=capture(kind,command,cwd,output,output/(kind+'.record.json'));records.append(record)
        try:
            verify_command_record(record,output);verify_semantics(record,repo,snapshot)
            verdict='PASS'
        except (OSError,ValueError,KeyError) as e:verdict='FAIL: '+str(e)
        verdicts.append(verdict)
        print(json.dumps({'kind':kind,'exit_code':record['exit_code'],'verdict':verdict}),flush=True)
    (output/'source_records.json').write_text(json.dumps({'records':records},indent=2)+'\n',encoding='utf-8')
    return 0 if all(v=='PASS' for v in verdicts) else 2

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot-root',required=True);p.add_argument('--output-root',required=True);a=p.parse_args()
    return sources(a.snapshot_root,a.output_root)
if __name__=='__main__':raise SystemExit(main())
