"""Recheck a native latency receipt; a changed raw column must miss the cache."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from optimization.runtime.store import persisted
from optimization.runtime.validation import RawValidationCache, validate_runtime_result, route_check
from optimization.models.decision_state import DecisionState
from optimization.models.member1_dynamic_state import sha
from optimization.integration.member1_static_runner import _verified_source
from optimization.integration.member1_s0_graph import Member1RoadGraph
from optimization.integration.member1_profiles import load_profile_config


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--receipt',type=Path,required=True)
    args=parser.parse_args()
    folder=args.receipt.resolve()
    with sqlite3.connect((folder/'authority/backend_authority.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        job=db.execute("SELECT * FROM jobs WHERE status='COMPLETED' ORDER BY rowid DESC LIMIT 1").fetchone()
        root=persisted(db.execute('SELECT root FROM sessions WHERE id=?',(job['session'],)).fetchone()[0],'sessions.root')
        result=persisted(job['result'],'jobs.result')
    state=DecisionState.from_dict(root['initial'])
    paths,hashes=_verified_source(ROOT,state)
    config=load_profile_config(ROOT/'configs/member1_profiles_step3.json')
    cache=RawValidationCache()
    with Member1RoadGraph(paths['network_sqlite_sha256'],paths['features_sqlite_sha256'],
            routing_version=state.routing_version,features_version=state.features_version,
            context_version=state.context_version,source_hashes=hashes) as graph:
        def validate(value):
            return validate_runtime_result(state,graph,value['domain'],value,config,
                anchor=value['validation_anchor'],snapshot=ROOT,overlay=value.get('overlay'),_raw_cache=cache)
        with patch('optimization.runtime.validation.route_check',wraps=route_check) as checked:
            assert validate(result)['valid']
            calls=checked.call_count
            assert calls==len(result['domain']['columns'])
            assert validate(result)['valid'] and checked.call_count==calls
            bad=deepcopy(result)
            # Rehash the changed domain: the cache must not accept a caller's
            # self-consistent digest as a substitute for the raw physical check.
            bad['domain']['columns'][0]['total_distance_m']+=1000
            material=dict(bad['domain']);material.pop('content_sha256')
            bad['domain']['content_sha256']=bad['domain_sha256']=sha(material)
            rejected=validate(bad)
            assert rejected['valid'] is False and checked.call_count>calls
    report={'status':'PASS','columns_checked':calls,'repeat_reused_raw_checks':True,
            'rehash_of_tampered_column_rejected':True,'diagnostics':rejected['diagnostics']}
    (folder/'validation-cache-check.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(report))


if __name__=='__main__':main()
