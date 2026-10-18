"""Verify externally installed inventory BEFORE importing package initializers.

Only stdlib is imported before integrity checking. Expected digest is server
installation configuration, never a field supplied by an API command.
"""
import argparse,hashlib,json,sys
from pathlib import Path,PurePosixPath

def verify_install(root,inventory_path,expected):
    record=json.loads(inventory_path.read_bytes())
    if not isinstance(record,dict) or not isinstance(record.get('files'),dict):raise ValueError('BUILD_SCHEMA: inventory object/file map required')
    body={k:v for k,v in record.items() if k!='build_sha256'}
    raw=json.dumps(body,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    if record.get('build_sha256')!=expected or hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('BUILD_BINDING: external installed digest differs')
    if 'requirements-runtime.lock.txt' not in record['files']:raise ValueError('BUILD_CLOSURE: dependency lock must be pinned before package imports')
    for name,pin in record['files'].items():
        if not isinstance(name,str) or not isinstance(pin,dict) or type(pin.get('bytes')) is not int or pin['bytes']<0 or not isinstance(pin.get('sha256'),str):raise ValueError('BUILD_SCHEMA: invalid file pin')
        p=PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or ':' in name or '\\' in name:raise ValueError('BUILD_PATH: '+name)
        f=root/name
        if not f.is_file() or f.stat().st_size!=pin['bytes'] or hashlib.sha256(f.read_bytes()).hexdigest()!=pin['sha256']:raise ValueError('BUILD_CHANGED: '+name)

def main():
    p=argparse.ArgumentParser();p.add_argument('--inventory',required=True,type=Path);p.add_argument('--expected-build-sha256',required=True);p.add_argument('--snapshot-root',required=True);p.add_argument('--store',required=True);args=p.parse_args()
    try:verify_install(Path(__file__).resolve().parent,args.inventory,args.expected_build_sha256)
    except (OSError,ValueError,KeyError) as e:
        print(json.dumps({'schema_version':'task02-m2-runtime-response/1','command_id':None,'status':'FAIL','value':None,'diagnostics':[{'code':'BUILD_INTEGRITY','path':'inventory','message':str(e),'severity':'ERROR'}]}));return 2
    from optimization.runtime.cli import main as runtime_main
    sys.argv=[sys.argv[0],'--snapshot-root',args.snapshot_root,'--store',args.store,'--expected-build-sha256',args.expected_build_sha256]
    return runtime_main()
if __name__=='__main__':raise SystemExit(main())
