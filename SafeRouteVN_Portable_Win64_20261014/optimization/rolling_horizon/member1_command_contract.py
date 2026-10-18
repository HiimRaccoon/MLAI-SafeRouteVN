"""Typed command/result binding; raw transcript verification is a separate layer.

Local records are audit evidence, not signatures. Explicit checks work under -O.
"""
from __future__ import annotations
import json
from pathlib import Path
import re
import math

CATALOG_VERSION = '2f034859196a05c864f2681b428ac0428f4253f316f6f4824009a2b58d0a7472'
SOURCE_MODULES = {
    'task_audit': 'optimization.integration.member1_mapping_audit',
    'crosswalk': 'optimization.tests.member1_decision_state_real_source_gate',
    'crosswalk_optimized': 'optimization.tests.member1_decision_state_real_source_gate',
    'legacy_source': 'optimization.tests.member1_profile_legacy_source_gate',
    'binding_source': 'optimization.tests.member1_profile_binding_source_gate',
}

def _require(ok, code, path):
    if not ok:
        raise ValueError(f'{code}: {path}')

def parse_flags(tokens, allowed, repeated=()):
    result = {}
    for index in range(0, len(tokens), 2):
        _require(index + 1 < len(tokens), 'COMMAND_ARGUMENT', 'argv')
        flag, value = tokens[index:index+2]
        _require(flag in allowed and not value.startswith('--'), 'COMMAND_ARGUMENT', flag)
        _require(flag not in result or flag in repeated, 'DUPLICATE_ARGUMENT', flag)
        result.setdefault(flag, []).append(value)
    return result

def verify_semantics(record, repo, snapshot, *, expected_exit=0):
    _require(isinstance(record,dict),'COMMAND_RECORD','$')
    kind = record.get('kind')
    _require(isinstance(kind,str) and bool(kind),'COMMAND_KIND','kind')
    argv = record.get('argv')
    _require(isinstance(argv, list) and all(isinstance(x, str) and x for x in argv), 'COMMAND_ARGV', 'argv')
    _require(isinstance(record.get('platform'), str) and record['platform'].startswith('Windows-'), 'COMMAND_PLATFORM', 'platform')
    _require(bool(argv) and isinstance(record.get('interpreter'),str) and isinstance(record.get('cwd'),str),'COMMAND_RECORD','interpreter/cwd')
    _require(Path(argv[0]).resolve() == Path(record['interpreter']).resolve(), 'COMMAND_INTERPRETER', 'argv[0]')
    m1 = kind.startswith('m1_')
    _require(Path(record['cwd']).resolve() == (snapshot if m1 else repo), 'COMMAND_CWD', 'cwd')
    if m1:
        _require(Path(argv[0]).resolve() == (snapshot/'.venv/Scripts/python.exe').resolve(), 'COMMAND_INTERPRETER', 'argv[0]')
    optimized = kind.endswith('optimized') or kind == 'neg_missing_parent'
    tokens = argv[1:]
    if tokens[:1] == ['-O']:
        _require(optimized, 'COMMAND_OPTIMIZED', 'argv')
        tokens = tokens[1:]
    else:
        _require(not optimized, 'COMMAND_OPTIMIZED', 'argv')
    stdout = Path(record['stdout_path']).read_text(encoding='utf-8')
    if kind == 'm1_pyproj':
        _require(tokens == ['-c', 'import pyproj; print(pyproj.__version__)'], 'COMMAND_MODULE', 'argv')
        _require(re.fullmatch(r'\d+\.\d+\.\d+\s*', stdout) is not None, 'SOURCE_STDOUT', 'stdout')
        return
    _require(len(tokens) >= 2 and tokens[0] == '-m', 'COMMAND_MODULE', 'argv')
    module = 'geo_data.cli' if kind == 'm1_verify' else SOURCE_MODULES.get(kind, 'optimization.rolling_horizon.member1_motion_runner')
    _require(tokens[1] == module, 'COMMAND_MODULE', 'argv')
    tail = tokens[2:]
    if kind == 'm1_verify':
        _require(tail[:1] == ['verify-scenarios'], 'COMMAND_SUBCOMMAND', 'argv')
        flags = parse_flags(tail[1:], {'--scenarios-root', '--suite-id'})
        _require(flags == {'--scenarios-root':['scenarios'], '--suite-id':['thu-duc-binh-thanh-v1']}, 'COMMAND_ARGUMENT', 'argv')
    elif kind in SOURCE_MODULES:
        flags = parse_flags(tail, {'--snapshot-root', '--repo-root', '--output'})
        _require(flags.get('--snapshot-root') == [str(snapshot)], 'COMMAND_SNAPSHOT', '--snapshot-root')
        if '--repo-root' in flags:
            _require(Path(flags['--repo-root'][0]).resolve() == repo, 'COMMAND_REPO', '--repo-root')
    else:
        flags = parse_flags(tail, {'--snapshot-root','--repo-root','--output-root','--scenario-id','--target-time', '--parent-state','--parent-ancestor','--accepted-plan-receipt'}, {'--target-time','--parent-ancestor'})
        _require(flags.get('--snapshot-root') == [str(snapshot)], 'COMMAND_SNAPSHOT', '--snapshot-root')
        _require(flags.get('--repo-root') == [str(repo)], 'COMMAND_REPO', '--repo-root')
        _require('--target-time' in flags and '--output-root' in flags, 'COMMAND_ARGUMENT', 'argv')
    if expected_exit:
        body = json.loads(Path(record['stderr_path']).read_bytes())
        _require(isinstance(body, dict) and body.get('status') == 'FAIL', 'NEGATIVE_STDOUT', 'stderr')
        return
    body = json.loads(stdout)
    _require(isinstance(body, dict), 'SOURCE_STDOUT', 'stdout')
    if kind == 'm1_verify':
        _require(body.get('verified') is True and body.get('integrated') is False and type(body.get('scenarios')) is int and body['scenarios'] == 9 and body.get('suiteId') == 'thu-duc-binh-thanh-v1' and body.get('version') == CATALOG_VERSION, 'SOURCE_STDOUT', 'm1_verify')
    elif kind == 'task_audit':
        _require(body.get('status') == 'M1_SOURCE_CONTRACT_GATE_PASS' and body.get('receipt_export_gate_pass') is True and body.get('integrated_solver_validated') is False and type(body.get('scenarios')) is int and body['scenarios'] == 9 and type(body.get('semantic_cases_checked')) is int and body['semantic_cases_checked'] == 9 and body.get('diagnostics') == [], 'SOURCE_STDOUT', kind)
    elif kind.startswith('crosswalk'):
        rows = body.get('rows')
        _require(isinstance(rows,list) and len(rows)==9 and all(isinstance(x,dict) for x in rows), 'SOURCE_COVERAGE', kind)
        _require(all(isinstance(x.get('scenario_id'),str) for x in rows),'SOURCE_COVERAGE',kind)
        _require(sorted(x.get('scenario_id','') for x in rows)==[f'S{i}' for i in range(9)] and body.get('status')=='DECISION_STATE_ADAPTER_READY' and body.get('source_gate')=='M1_SOURCE_CONTRACT_GATE_PASS' and body.get('integrated_solver_validated') is False, 'SOURCE_STDOUT', kind)
        receipt=json.loads((repo/'optimization/integration/member1_trusted_receipt.json').read_bytes())
        for i,row in enumerate(sorted(rows,key=lambda x:x['scenario_id'])):
            _require(type(row.get('orders')) is int and row['orders']==(3 if i==0 else 1 if i in (7,8) else 8) and type(row.get('pending_events')) is int and row['pending_events']==(1 if i in (2,3,4) else 0),'SOURCE_COVERAGE',f'rows[{i}]')
            _require(isinstance(row.get('fixture_raw_sha256'),str) and row['fixture_raw_sha256']==receipt['fixture_raw_sha256'][f'S{i}'],'SOURCE_BINDING',f'rows[{i}].fixture_raw_sha256')
            expected_mass=[11.077,25.434,25.434,25.434,25.434,25.434,38.024,1.111,2.542][i]
            value=row.get('demand_kg')
            _require(type(value) in (int,float) and 0<=value<=100 and math.isfinite(value) and abs(value-expected_mass)<1e-8,'SOURCE_COVERAGE',f'rows[{i}].demand_kg')
    elif kind == 'legacy_source':
        checks=body.get('checks')
        _require(isinstance(checks,list) and len(checks)==5 and all(isinstance(x,dict) and isinstance(x.get('name'),str) and x.get('valid') is True for x in checks) and {x.get('name') for x in checks}=={'S0','S1_BOUNDED','S1_ORTOOLS_V4','S5_ORTOOLS_V4','S6_ORTOOLS_V4'} and body.get('status')=='LEGACY_RAW_REVALIDATION_PASS', 'SOURCE_STDOUT', kind)
    elif kind == 'binding_source':
        _require(body.get('status')=='STEP3_BINDING_REVALIDATION_PASS' and type(body.get('target_count')) is int and body['target_count']==12 and flags.get('--output')==[body.get('output')], 'SOURCE_STDOUT', kind)
        artifact=Path(body['output']);_require(artifact.is_file() and type(body.get('output_bytes')) is int and artifact.stat().st_size==body['output_bytes'],'SOURCE_ARTIFACT',kind)
        import hashlib
        _require(hashlib.sha256(artifact.read_bytes()).hexdigest()==body.get('output_sha256'),'SOURCE_ARTIFACT',kind)
        payload=json.loads(artifact.read_bytes());_require(payload.get('status')=='STEP3_BINDING_REVALIDATION_PASS' and type(payload.get('target_count')) is int and payload['target_count']==12,'SOURCE_ARTIFACT',kind)
    else:
        _require(body.get('status')=='READY' and body.get('gate')=='M1_MOTION_REPLAY_VALIDATED', 'RUN_STDOUT', kind)
        directory=Path(body['output']).resolve()
        _require(directory.parent==Path(flags['--output-root'][0]).resolve(), 'COMMAND_OUTPUT', kind)
        manifest=json.loads((directory/'manifest.json').read_bytes())
        _require(manifest.get('target_times')==flags['--target-time'] and [manifest.get('scenario_id')]==flags.get('--scenario-id'), 'COMMAND_TARGET', kind)
        platform=manifest.get('platform',{})
        _require(platform.get('system')=='Windows' and platform.get('optimized_mode') is optimized and Path(platform.get('interpreter','')).resolve()==Path(argv[0]).resolve(), 'RUN_PLATFORM', kind)
