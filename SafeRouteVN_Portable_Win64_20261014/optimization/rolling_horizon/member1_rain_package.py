"""Whitelist Step6 patch packaging. BLOCKED never becomes READY here."""
import argparse,json,zipfile
from pathlib import Path,PurePosixPath
from typing import Mapping
from optimization.models.member1_dynamic_state import require,DynamicError
from optimization.integration.member1_static_runner import _sha
from optimization.rolling_horizon.member1_rain_check import read

VERSION='task02-m1-temporal-rain-patch-package/1'
CODE=['optimization/models/member1_rain.py','optimization/rolling_horizon/member1_dynamic_planner.py',
      'optimization/rolling_horizon/member1_dynamic_validation.py','optimization/rolling_horizon/member1_column_validation.py']
CODE+=['optimization/rolling_horizon/member1_rain_'+n+'.py' for n in
       ('source','temporal','transition','planner','validation','replay','runner','check','checkpoint','package','legacy_revalidation')]
CODE+=['configs/member1_rain_step6.json','optimization/tests/test_member1_rain_step6.py','docs/task02_member1_step6_temporal_rain.md']


def safe(name):
    require(isinstance(name,str),'PACKAGE_PATH','entry','string path required')
    p=PurePosixPath(name)
    require(bool(name) and not p.is_absolute() and '..' not in p.parts and '\\' not in name and ':' not in name,'PACKAGE_PATH',name,'safe relative archive path required')
    require(not any(x in p.parts for x in ('.venv','.test-deps','__pycache__','.pytest_cache')) and p.suffix not in ('.sqlite','.db','.pyc'),'PACKAGE_SCOPE',name,'database/cache/runtime not deliverable')
    return name


def package(repo,checkpoint_path,expected_sha,archive):
    repo=Path(repo).resolve();checkpoint_path=Path(checkpoint_path).resolve();archive=Path(archive).resolve()
    require(checkpoint_path.is_relative_to(repo) and _sha(checkpoint_path)==expected_sha,'CHECKPOINT_BINDING','checkpoint','external reviewed digest required')
    checkpoint=read(checkpoint_path);require(isinstance(checkpoint,Mapping) and checkpoint.get('gate') in ('GATE_BLOCKED','M1_S4_TEMPORAL_RAIN_OVERLAY_READY'),'PACKAGE_GATE','checkpoint.gate','explicit honest gate required')
    require(not archive.exists(),'OUTPUT_EXISTS','archive','do not overwrite history')
    names=set(CODE);names.add(checkpoint_path.relative_to(repo).as_posix())
    names.add('outputs/member1_rain_step6/STEP6_ACCEPTANCE_FILLED.json')
    evidence=repo/'outputs/member1_rain_step6/evidence'
    selected=[]
    selected += [evidence/n for n in ('preflight_inventory.json','legacy_json_restoration.json','step5_current_raw_revalidation.json','frozen_post_inventory.json','source_diff.json')]
    for kind in ('baseline_legacy','red_interfaces_collected','red_used_edge_dominance','red_unaffected_layer','focused_acceptance','related_final_corrected','full_acceptance_final'):
        selected += [evidence/'regression'/(kind+suffix) for suffix in ('.record.json','.stdout.log','.stderr.log','.log','.xml')]
    for kind in ('m1_pyproj','m1_verify'):
        selected += [evidence/'native_initial'/(kind+suffix) for suffix in ('.record.json','.stdout.log','.stderr.log','.log')]
        selected += [evidence/'native_final_source'/(kind+suffix) for suffix in ('.record.json','.stdout.log','.stderr.log','.log')]
    selected += [p for p in (evidence/'native_sources_acceptance').rglob('*') if p.is_file() and p.suffix in ('.json','.log')]
    for kind in ('s4_acceptance','s4_acceptance_optimized','rain_negative_optimized','environment_versions','step5_revalidation_corrected'):
        selected += [evidence/'native_final'/(kind+suffix) for suffix in ('.record.json','.stdout.log','.stderr.log','.log')]
    for p in selected:
        require(p.is_file(),'PACKAGE_EVIDENCE',str(p),'selected native evidence missing')
        names.add(p.relative_to(repo).as_posix())
    for kind,row in checkpoint.get('runs',{}).items():
        directory=Path(row['directory']).resolve()
        require(directory.is_relative_to(repo/'outputs') and directory.name==row['run_id'],'RUN_BINDING',kind,'bound new run required')
        manifest=read(directory/'manifest.json')
        require(_sha(directory/'manifest.json')==row['manifest_sha256'],'RUN_BINDING',kind,'checkpoint manifest differs')
        names.add((directory/'manifest.json').relative_to(repo).as_posix())
        for rel,pin in manifest['files'].items():
            safe(rel);p=directory/rel
            require(p.stat().st_size==pin['bytes'] and _sha(p)==pin['sha256'],'PAYLOAD_BINDING',rel,'run payload differs')
            names.add(p.relative_to(repo).as_posix())
    names=sorted(safe(n) for n in names)
    records={n:{'sha256':_sha(repo/n),'bytes':(repo/n).stat().st_size} for n in names}
    package_path=checkpoint_path.with_name('STEP6_PACKAGE_MANIFEST.json');require(not package_path.exists(),'OUTPUT_EXISTS','package_manifest','unique new package receipt required')
    metadata={'schema_version':VERSION,'gate':checkpoint['gate'],'checkpoint_sha256':expected_sha,'files':records,
              'scope':'PATCH_FOR_FULL_TASK02_TREE_NOT_STANDALONE_RUNTIME','package_manifest_self_hash':'EXCLUDED_TO_AVOID_CIRCULARITY',
              'prerequisites':['full TASK-02 baseline Step5 closure','pinned read-only M1 databases and primary reference files','pinned test dependencies'],
              'general_m1_validated':False,'e4_run':False,'production_calibrated':False}
    package_path.write_text(json.dumps(metadata,sort_keys=True,indent=2)+'\n',encoding='utf8')
    names.append(package_path.relative_to(repo).as_posix());archive.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in names:z.write(repo/name,name)
    with zipfile.ZipFile(archive) as z:
        require(len(z.namelist())==len(names)==len(set(z.namelist())) and z.testzip() is None,'ARCHIVE_INTEGRITY','archive','CRC or duplicate entry failure')
        import hashlib
        for name in z.namelist():
            safe(name);raw=z.read(name)
            require(len(raw)==(repo/name).stat().st_size and hashlib.sha256(raw).hexdigest()==_sha(repo/name),'ARCHIVE_BINDING',name,'archive entry differs from working tree')
    return {'archive':str(archive),'sha256':_sha(archive),'bytes':archive.stat().st_size,'entries':len(names),'gate':checkpoint['gate'],
            'package_manifest':str(package_path),'package_manifest_sha256':_sha(package_path),'crc':'PASS','entry_binding':'PASS'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--checkpoint',required=True);p.add_argument('--trusted-checkpoint-sha256',required=True);p.add_argument('--archive',required=True);a=p.parse_args()
    try:print(json.dumps(package(Path(__file__).resolve().parents[2],a.checkpoint,a.trusted_checkpoint_sha256,a.archive)));return 0
    except (DynamicError,OSError,ValueError) as e:
        print(json.dumps({'status':'FAIL','diagnostic':{'code':getattr(e,'code','PACKAGE'),'path':getattr(e,'path','$'),'message':str(e)}}));return 2
if __name__=='__main__':raise SystemExit(main())
