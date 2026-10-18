"""Same neutral vectors as the M4 Node runner; no model/SQLite dependency."""
import argparse,json
from pathlib import Path
from copy import deepcopy
from .protocol import decode,require,RuntimeError
from .contracts import validate_view,validate_job_view
def main():
    p=argparse.ArgumentParser();p.add_argument('--examples',type=Path,default=Path('examples'));a=p.parse_args();results=[]
    try:
        vectors=decode(Path(__file__).with_name('consumer_golden_corpus.json').read_bytes())
        for c in vectors['cases']:
            value=deepcopy(c['payload'] if 'payload' in c else decode((a.examples/(c['base']+'_execution_view.json')).read_bytes()))
            if 'path' in c:
                parent=value
                for key in c['path'][:-1]:parent=parent[key]
                if c.get('remove'):del parent[c['path'][-1]]
                else:parent[c['path'][-1]]=c['value']
            issues=(validate_job_view if c.get('kind')=='job' else validate_view)(value);require((not issues)==c['valid'],'GOLDEN_PARITY',c['id'],str(issues))
            if 'expected_diagnostic' in c:require(bool(issues) and all(issues[0].get(k)==v for k,v in c['expected_diagnostic'].items()),'GOLDEN_PARITY',c['id'],str(issues))
            results.append({'id':c['id'],'accepted':not issues,'diagnostics':issues})
    except (RuntimeError,OSError) as e:
        print(json.dumps({'status':'GOLDEN_FAIL','diagnostics':[{'code':getattr(e,'code','IO'),'path':getattr(e,'path','$'),'message':str(e)}]}));return 2
    print(json.dumps({'status':'GOLDEN_PASS','cases':len(results),'results':results}));return 0
if __name__=='__main__':raise SystemExit(main())
