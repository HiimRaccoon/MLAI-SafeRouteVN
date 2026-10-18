"""One JSON command over stdin; store/source/build config is server-side argv."""
import argparse,json,sys
from pathlib import Path
from .protocol import decode,RuntimeError,response
from .facade import Runtime
def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot-root',required=True,type=Path);p.add_argument('--store',required=True,type=Path);p.add_argument('--expected-build-sha256',required=True);a=p.parse_args()
    try:
        value=decode(sys.stdin.buffer.read())
        result=Runtime(a.snapshot_root,a.store,a.expected_build_sha256).execute(value)
    except (RuntimeError,OSError) as e:
        result=response(None,'FAIL',diagnostics=[{'severity':'ERROR','code':getattr(e,'code','RUNTIME_IO'),'path':getattr(e,'path','$'),'message':str(e)}])
    print(json.dumps(result,ensure_ascii=False,allow_nan=False));return 0 if result['status']=='OK' else 2
if __name__=='__main__':raise SystemExit(main())
