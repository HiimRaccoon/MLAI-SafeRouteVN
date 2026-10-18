"""Portable Python consumer gate: no SQLite, adapter, solver or model import.

Run: python -m optimization.runtime.portable_tests [--view FILE ...]
Failures are explicit under python -O as well. Not a source/VRP certificate.
"""
import argparse,json
from pathlib import Path
from copy import deepcopy
from .protocol import decode,int64,RuntimeError,require
from .contracts import validate_view

def main():
    p=argparse.ArgumentParser();p.add_argument('--view',action='append',type=Path,default=[]);a=p.parse_args();count=0
    try:
        vectors=decode(Path(__file__).with_name('vectors.json').read_bytes())
        for v in vectors['raw_vectors']:
            try:decode(v['raw']);ok=True
            except RuntimeError:ok=False
            require(ok==v['valid'],'VECTOR_DRIFT','raw_vectors','raw decoder differs');count+=1
        for v in vectors['exact_integer_vectors']:
            try:int64(v['value'],'exact_integer');ok=True
            except RuntimeError:ok=False
            require(ok==v['valid'],'VECTOR_DRIFT','exact_integer_vectors','exact integer differs');count+=1
        for path in a.view:
            value=decode(path.read_bytes());errors=validate_view(value);require(not errors,'CONSUMER_INVALID',str(path),str(errors));count+=1
            for mutate in ('simulation','timestamp','coverage','load'):
                bad=deepcopy(value)
                if mutate=='simulation':bad['real_world_observation']=True
                elif mutate=='timestamp':bad['current_time']='2026-02-30T21:00:00+07:00'
                elif mutate=='coverage':bad['order_ids']+=bad['order_ids'][:1]
                elif bad['vehicles']:bad['vehicles'][0]['current_load_kg']=True
                else:continue
                require(bool(validate_view(bad)),'COUNTEREXAMPLE_ACCEPTED',mutate,'consumer accepted malformed view');count+=1
    except (RuntimeError,OSError) as e:
        print(json.dumps({'status':'PORTABLE_FAIL','diagnostics':[{'code':getattr(e,'code','IO'),'path':getattr(e,'path','$'),'message':str(e)}]}));return 2
    print(json.dumps({'status':'PORTABLE_PYTHON_PASS','cases':count,'source_authenticated':False,'vrp_feasibility_certified':False}));return 0
if __name__=='__main__':raise SystemExit(main())
