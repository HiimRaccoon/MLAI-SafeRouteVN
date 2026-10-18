"""Whitelist-only Step5 patch for the full TASK-02 tree, not a runtime bundle.

The external checkpoint digest must be supplied by the reviewer/persistence
layer. Revalidation checks its content; a mutable READY string is insufficient.
"""
import argparse,hashlib,json,zipfile
from pathlib import Path,PurePosixPath
from optimization.models.member1_dynamic_state import require,DynamicError
from optimization.rolling_horizon.member1_dynamic_checkpoint import verify,digest,read

FILES=['optimization/models/member1_dynamic_state.py','optimization/models/member1_dynamic_step5.schema.json',
       'optimization/solver/member1_dynamic_master.py','configs/member1_dynamic_step5.json',
       'optimization/rolling_horizon/member1_command_contract.py','optimization/rolling_horizon/member1_motion_checkpoint.py',
       'optimization/tests/test_member1_dynamic_step5.py','optimization/tests/test_member1_step5_checkpoint_review.py',
       'docs/task02_member1_step5_event_dynamic.md','docs/task02_member1_step5_acceptance.json','docs/task02_member1_step5_existing_source.patch']
FILES += ['optimization/rolling_horizon/member1_dynamic_'+n+'.py' for n in ('transition','planner','validation','runner','check','evidence','checkpoint','package')]
CLOSURE_FILES=['optimization/tests/test_step5_review_counterexamples.py','optimization/tests/test_step5_closure_adjacent.py','docs/task02_member1_step5_closure.md','docs/task02_member1_step5_closure_acceptance.json','docs/task02_member1_step5_closure_source.patch']

def build(repo,snapshot,evidence,checkpoint,pin,output,*,matrix=None):
    repo=Path(repo).resolve();evidence=Path(evidence).resolve();checkpoint=Path(checkpoint).resolve();output=Path(output).resolve()
    require(digest(checkpoint)==pin,'CHECKPOINT_BINDING','checkpoint','external trusted digest differs')
    matrix=Path(matrix).resolve() if matrix else repo/'docs/task02_member1_step5_acceptance.json'
    current=verify(repo,snapshot,evidence,matrix)
    require(current==read(checkpoint) and current['gate']=='M1_S2_S3_EVENT_DYNAMIC_READY','CHECKPOINT_BINDING','checkpoint','content/current raw revalidation differs or gate is blocked')
    require(not output.exists(),'OUTPUT_EXISTS','output','never overwrite a historical ZIP')
    paths=[repo/p for p in FILES+(CLOSURE_FILES if matrix.name=='task02_member1_step5_closure_acceptance.json' else [])]+[checkpoint]
    for target in current['runs'].values():
        directory=Path(target['directory']);m=read(directory/'manifest.json')
        paths += [directory/name for name in m['files']]+[directory/'manifest.json']
    paths += [evidence/n for n in ('preflight_inventory.json','final_inventory.json','failed_attempt_inventory.json','environment.json','native_summary.json')]
    if matrix.name=='task02_member1_step5_closure_acceptance.json':paths.append(evidence/'historical_revalidation.json')
    for folder in ('native_sources_final','native_targets_certified','native_negative_final','regression','native_sources_initial'):
        paths += [p for p in (evidence/folder).rglob('*') if p.is_file()]
    entries={}
    for path in paths:
        path=path.resolve();rel=path.relative_to(repo).as_posix();parts=PurePosixPath(rel).parts
        require(not rel.startswith('/') and '..' not in parts and '\\' not in rel and ':' not in rel,'PACKAGE_PATH',rel,'unsafe relative path')
        require(path.suffix not in ('.sqlite','.pyc') and not any(x in parts for x in ('__pycache__','.pytest_cache','.venv','trial','certified','accepted','native_targets_final','native_targets')),'PACKAGE_PATH',rel,'runtime/cache/failed experiment forbidden')
        require(rel not in entries,'PACKAGE_DUPLICATE',rel,'duplicate entry')
        entries[rel]={'bytes':path.stat().st_size,'sha256':digest(path)}
    package={'schema_version':'task02-m1-step5-patch-package/2','checkpoint_sha256':pin,'gate':current['gate'],'files':entries,'prerequisites':['full TASK-02 working tree and frozen predecessor runs','pinned read-only Member1 snapshot and both SQLite','Python 3.12 / pytest 8.4.2 / OR-Tools 9.15.6755'],'scope':{'api_v1_changed':False,'general_m1_validated':False,'e4_run':False,'production_calibrated':False,'generic_multi_event_scheduler':False}}
    package_path=evidence/'package_manifest.json';package_path.write_text(json.dumps(package,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    paths.append(package_path);output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for path in paths:z.write(path,path.relative_to(repo).as_posix())
    with zipfile.ZipFile(output) as z:
        require(z.testzip() is None and len(z.namelist())==len(set(z.namelist())),'ZIP_CRC','zip','CRC/duplicate failure')
        for name in z.namelist():
            require(not PurePosixPath(name).is_absolute() and '..' not in PurePosixPath(name).parts and '\\' not in name and ':' not in name,'ZIP_PATH',name,'unsafe archive path')
            raw=z.read(name);path=repo/name
            require(hashlib.sha256(raw).hexdigest()==digest(path) and len(raw)==path.stat().st_size,'ZIP_BINDING',name,'entry differs from working tree')
    return {'zip':str(output),'sha256':digest(output),'bytes':output.stat().st_size,'entries':len(paths),'crc':'PASS','entry_hashes':'PASS','checkpoint_sha256':pin}

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot-root',required=True);p.add_argument('--evidence-root',required=True);p.add_argument('--checkpoint',required=True);p.add_argument('--trusted-checkpoint-sha256',required=True);p.add_argument('--output',required=True);p.add_argument('--matrix');a=p.parse_args()
    try:
        result=build(Path(__file__).resolve().parents[2],a.snapshot_root,a.evidence_root,a.checkpoint,a.trusted_checkpoint_sha256,a.output,matrix=a.matrix);print(json.dumps(result));return 0
    except (DynamicError,OSError,ValueError) as e:
        print(json.dumps({'status':'GATE_BLOCKED','code':getattr(e,'code','PACKAGE_INVALID'),'path':getattr(e,'path','$'),'message':str(e)}));return 2
if __name__=='__main__':raise SystemExit(main())
