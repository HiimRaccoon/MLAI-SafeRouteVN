"""Immutable, server-published run sources, independent of the historical suite.

The manifest digest is pinned in M3 metadata before bootstrap and in the M2
journal thereafter. A manifest is provenance, never a feasibility certificate.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re

from optimization.integration.member1_decision_state_adapter import _depot, _order, _vehicle, _event
from optimization.models.decision_state import DecisionState, STATE_SCHEMA_VERSION
from .protocol import require, instant

SCHEMA = 'saferoute-run-source/1'
AUTH = 'RUN_MANIFEST_VERIFIED'


def file_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run_directory(root, run_id):
    require(isinstance(run_id, str) and re.fullmatch(r'run-[a-f0-9]{32}', run_id),
            'RUN_ID_INVALID', 'run_id', 'Server run identifier required')
    base = (Path(root) / 'scenarios/runs').resolve()
    path = (base / run_id).resolve()
    require(path.parent == base, 'RUN_PATH_INVALID', 'run_id', 'Run must remain in its registry')
    return path


def read_manifest(root, run_id, expected):
    path = run_directory(root, run_id) / 'manifest.json'
    require(file_sha(path) == expected, 'SOURCE_CHANGED', 'manifest', 'Run manifest differs from registered digest')
    value = json.loads(path.read_bytes())
    require(value.get('schema_version') == SCHEMA and value.get('run_id') == run_id,
            'RUN_MANIFEST_INVALID', 'manifest', 'Run manifest identity differs')
    instant(value['epoch'], 'epoch')
    return value


def artifact(root, manifest, role):
    entry = manifest['artifacts'][role]
    relative = Path(entry['path'])
    path = (Path(root) / relative).resolve()
    require(not relative.is_absolute() and path.is_relative_to(Path(root).resolve()),
            'RUN_PATH_INVALID', role, 'Source path outside configured snapshot')
    require(path.stat().st_size == entry['bytes'] and file_sha(path) == entry['sha256'],
            'SOURCE_CHANGED', role, 'Registered source bytes changed')
    return path


def load_state(root, run_id, expected):
    m = read_manifest(root, run_id, expected)
    fixture = json.loads(artifact(root, m, 'fixture').read_bytes())
    s = fixture['initialState']
    require(s['currentTime'] == m['epoch'] and fixture['scenarioId'] == m['scenario_id'],
            'RUN_BINDING', 'fixture', 'Run epoch/scenario differs')
    require(not s['currentPlans'] and not fixture['executionUpdates'], 'RUN_BINDING', 'fixture', 'Fresh initial state required')
    events = tuple(replace(_event(e, i, scenario_id=fixture['scenarioId'], fixture=fixture), source_run=run_id)
                   for i, e in enumerate(fixture['events']))
    state = DecisionState(schema_version=STATE_SCHEMA_VERSION, suite_id=run_id,
        scenario_id=fixture['scenarioId'], source_run=run_id, source_authentication=AUTH,
        fixture_raw_sha256=m['artifacts']['fixture']['sha256'], catalog_raw_sha256=expected,
        receipt_sha256=expected, receipt_version=SCHEMA, routing_version=fixture['routingVersion'],
        features_version=fixture['featuresVersion'], context_version=fixture['contextVersion'],
        delivery_area_version=fixture['deliveryAreaVersion'], state_version=s['stateVersion'],
        current_time=s['currentTime'], decision_epoch=s['currentTime'], depot=_depot(s['locations']),
        orders=tuple(_order(o, i) for i, o in enumerate(s['orders'])),
        vehicles=tuple(_vehicle(v, i) for i, v in enumerate(s['vehicles'])),
        current_plans=(), execution_updates=(), pending_events=events,
        event_application_status='NOT_MATERIALIZED' if events else 'NOT_APPLICABLE',
        source_kind=m.get('source_kind', 'WEATHER_RUN'))
    verified_source(root, state)
    return state


def verified_source(root, state):
    m = read_manifest(root, state.source_run, state.receipt_sha256)
    require(state.source_kind is None or state.source_kind == m.get('source_kind', 'WEATHER_RUN'),
            'RUN_BINDING', 'source_kind', 'Run source kind differs from verified manifest')
    roles = {'network_sqlite_sha256': 'network', 'features_sqlite_sha256': 'features',
             'routing_manifest_sha256': 'routing_manifest', 'features_manifest_sha256': 'features_manifest',
             f'fixture_{state.scenario_id.lower()}_sha256': 'fixture',
             'weather_sha256': 'weather', 'weather_raw_sha256': 'weather_raw',
             'risk_model_sha256': 'risk_model', 'travel_profile_sha256': 'travel_profile'}
    paths = {key: artifact(root, m, role) for key, role in roles.items()}
    manifest_path = run_directory(root, state.source_run) / 'manifest.json'
    paths.update(catalog_sha256=manifest_path, receipt_sha256=manifest_path)
    hashes = {key: m['artifacts'][role]['sha256'] for key, role in roles.items()}
    hashes.update(catalog_sha256=state.receipt_sha256, receipt_sha256=state.receipt_sha256)
    routing = json.loads(paths['routing_manifest_sha256'].read_bytes())
    features = json.loads(paths['features_manifest_sha256'].read_bytes())
    require(m['epoch'] == state.decision_epoch and m['scenario_id'] == state.scenario_id
            and hashes[f'fixture_{state.scenario_id.lower()}_sha256'] == state.fixture_raw_sha256
            and state.catalog_raw_sha256 == state.receipt_sha256
            and routing['version'] == state.routing_version
            and features['version'] == state.features_version and features['contextVersion'] == state.context_version
            and features['at'] == state.decision_epoch
            and features['files']['features.sqlite']['sha256'] == hashes['features_sqlite_sha256']
            and routing['files']['network.sqlite']['sha256'] == hashes['network_sqlite_sha256'],
            'RUN_BINDING', 'source', 'Run state/stage lineage differs')
    return paths, hashes


def bootstrap(runtime, session_id, command_id, run_id, manifest_sha256):
    from .execution import initial_observation
    from .protocol import identifier, digest
    identifier(session_id, 'session_id'); identifier(command_id, 'command_id'); digest(manifest_sha256, 'manifest_sha256')
    state = load_state(runtime.snapshot, run_id, manifest_sha256)
    _, hashes = verified_source(runtime.snapshot, state)
    physical = initial_observation(state)
    head = {'current_time': state.current_time, 'context_version': state.context_version, 'overlay_sha256': None,
            'orders': physical['orders'], 'pending_events': physical['pending_events'],
            'execution_mode': 'SIMULATED_REPLAY', 'real_world_observation': False, 'observation': None}
    return runtime.store._bootstrap(session_id, {'initial': state.to_dict(), 'source_hashes': hashes, 'receipt_gated': True}, head, command_id)
