"""Validate uploaded native fixtures and publish isolated historical run sources."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import sqlite3
from uuid import uuid4

from backend.api.errors import ApiError
from .display_metadata import display_metadata, ORDER_FIELDS, VEHICLE_FIELDS
from optimization.integration.member1_decision_state_adapter import _parse_fixture
from optimization.runtime.run_builder import BASE, write
from optimization.runtime.run_source import SCHEMA, file_sha, run_directory, load_state


def validate_fixture(value, baseline, network):
    """No user paths or source certificates are trusted. Validate before publication."""
    if type(value) is not dict:
        raise ValueError('Cần một object scenario JSON.')
    f = deepcopy(value)
    if len(json.dumps(f, allow_nan=False).encode()) > 900_000:
        raise ValueError('Scenario quá lớn (tối đa 900 KB).')
    for field in ('schemaVersion', 'routingVersion', 'featuresVersion', 'contextVersion', 'deliveryAreaVersion'):
        if f.get(field) != baseline[field]:
            raise ValueError(f'{field}: phải khớp mạng đường và context của file mẫu.')
    s = f['initialState']
    for key, maximum in (('orders', 100), ('vehicles', 20), ('locations', 1)):
        if not isinstance(s[key], list) or not 1 <= len(s[key]) <= maximum:
            raise ValueError(f'initialState.{key}: cần từ 1 đến {maximum} phần tử.')
    if not isinstance(f['events'], list) or len(f['events']) > 20:
        raise ValueError('events: tối đa 20 sự kiện.')
    for item in s['orders']:
        display_metadata(item, ORDER_FIELDS)
    for item in s['vehicles']:
        display_metadata(item, VEHICLE_FIELDS)
    for event in f['events']:
        if isinstance(event, dict) and event.get('type') == 'URGENT_ORDER':
            display_metadata(event['orderPayload'], ORDER_FIELDS)
    state = _parse_fixture(f)  # Domain validation: time, capacity, custody, duplicate IDs, events.
    ids = set()
    orders = list(s['orders'])
    for e in f['events']:
        if datetime.fromisoformat(e['timestamp']) < datetime.fromisoformat(s['currentTime']):
            raise ValueError('Sự kiện không được xảy ra trước thời điểm bắt đầu.')
        if e['type'] == 'URGENT_ORDER':
            orders.append(e['orderPayload'])
        elif e['type'] == 'LOCAL_RAIN_WHAT_IF':
            # The runtime verifies rain formulas, geometry and edge evidence.
            if not e['affectedEdgeIds'] or len(e['affectedEdgeIds']) > 10000:
                raise ValueError('Rain event cần danh sách cạnh đường hợp lệ.')
            rings=e['polygon']['coordinates']
            if (not rings or len(rings)>10 or any(not isinstance(r,list) or not 4<=len(r)<=1000 or r[0]!=r[-1] or
                any(not isinstance(p,list) or len(p)!=2 or any(type(n) not in (int,float) or not math.isfinite(n) for n in p)
                    or abs(p[0])>180 or abs(p[1])>90 for p in r) for r in rings)):
                raise ValueError('Polygon mưa cần vòng tọa độ hợp lệ và khép kín.')
            if datetime.fromisoformat(e['endTime'])<=datetime.fromisoformat(e['startTime']):
                raise ValueError('Thời điểm kết thúc mưa phải sau khi bắt đầu.')
    for o in orders:
        if o['id'] in ids or o['pickupLocationId'] != state.depot.location_id:
            raise ValueError('ID đơn trùng hoặc depot lấy hàng không tồn tại.')
        ids.add(o['id'])
    points = s['locations'] + orders + [v['currentPosition'] for v in s['vehicles']]
    for obj in s['locations'] + orders + s['vehicles'] + f['events']:
        ident = obj.get('id', obj.get('eventId'))
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', ident):
            raise ValueError('ID chỉ được chứa chữ, số, dấu chấm, gạch ngang hoặc gạch dưới.')
    with closing(sqlite3.connect(network.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        for p in points:
            node = db.execute('SELECT longitude,latitude FROM nodes WHERE nodeId=?', (p['graphNodeId'],)).fetchone()
            if node is None or abs(node[0]-p['longitude']) > 1e-5 or abs(node[1]-p['latitude']) > 1e-5:
                raise ValueError('graphNodeId hoặc tọa độ không khớp mạng đường; dùng điểm từ file mẫu.')
        for e in f['events']:
            if e['type'] == 'LOCAL_RAIN_WHAT_IF':
                for edge in e['affectedEdgeIds']:
                    if db.execute('SELECT 1 FROM edges WHERE edgeId=?', (edge,)).fetchone() is None:
                        raise ValueError('Sự kiện mưa tham chiếu cạnh đường không tồn tại.')
    # Uploaded benchmark/feasibility claims are not certificates.
    f.pop('tradeoffEvidence', None)
    f['validation'] = {'integrated': False, 'jointVRPFeasibilityProven': False}
    return f


def publish_import(settings, catalog, run_id, fixture):
    root = settings.project_root
    base = root / BASE
    paths = {'network': base/'routing/network.sqlite', 'routing_manifest': base/'routing/manifest.json',
             'features': base/'features/features.sqlite', 'features_manifest': base/'features/manifest.json',
             'weather': base/'weather/weather_context.json', 'weather_raw': base/'weather/weather_context.json',
             'risk_model': base/'features/risk_model.json', 'travel_profile': base/'travel/profile.json'}
    # Each baseline file is pinned by its stage manifest. Runtime rechecks lineage on load.
    for role, path in paths.items():
        if role.endswith('_manifest'):
            continue
        stage = json.loads((path.parent/'manifest.json').read_bytes())
        pin = stage['files'][path.name]
        if file_sha(path) != pin['sha256']:
            raise ValueError('Baseline source changed: ' + role)
    output = run_directory(root, run_id)
    output.mkdir(parents=True, exist_ok=False)
    write(output/'fixture.json', fixture)
    paths['fixture'] = output/'fixture.json'
    artifacts = {role: {'path': p.relative_to(root).as_posix(), 'bytes': p.stat().st_size, 'sha256': file_sha(p)} for role, p in paths.items()}
    epoch = fixture['initialState']['currentTime']
    manifest = {'schema_version': SCHEMA, 'run_id': run_id, 'scenario_id': fixture['scenarioId'],
                'epoch': epoch, 'artifacts': artifacts, 'source_kind': 'IMPORTED_SCENARIO',
                'weather': {'fetched_at': epoch, 'valid_at': epoch, 'expires_at': epoch,
                            'provider': 'Historical scenario snapshot', 'region_count': 0, 'fallback_used': False},
                'traffic_source': 'ESTIMATED', 'temporal_model': 'FROZEN_DECISION_EPOCH',
                'execution_mode': 'SIMULATED_REPLAY', 'feasibility': 'NOT_YET_SOLVED',
                'created_at': datetime.now(timezone.utc).isoformat()}
    write(output/'manifest.json', manifest)
    digest = file_sha(output/'manifest.json')
    load_state(root, run_id, digest)
    return digest


def import_scenario(service, body, actor):
    baseline = service.sessions.catalog.fixture('S0')
    try:
        fixture = validate_fixture(body.fixture, baseline, service.settings.project_root/BASE/'routing/network.sqlite')
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        raise ApiError(422, 'INVALID_SCENARIO', 'fixture', str(error)[:300]) from error
    payload = {'scenario_id': fixture['scenarioId'], 'source_kind': 'IMPORTED_SCENARIO', 'name': body.name}
    fingerprint = hashlib.sha256(json.dumps([payload, fixture], sort_keys=True, allow_nan=False).encode()).hexdigest()
    # Serialize registration + publication, so a repeated request cannot publish twice.
    with service.db() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM run_builds WHERE actor_id=? AND request_id=?', (actor.actor_id, body.request_id)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'request_id', 'Request content changed')
            rid = old['run_id']
        else:
            rid = 'run-' + uuid4().hex
            try:
                digest = publish_import(service.settings, service.sessions.catalog, rid, fixture)
            except (ValueError, OSError, KeyError) as error:
                raise ApiError(422, 'IMPORT_SOURCE_INVALID', 'fixture', str(error)[:300]) from error
            db.execute('INSERT INTO run_builds VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (rid, actor.actor_id, body.request_id, fingerprint, json.dumps(payload), fixture['initialState']['currentTime'],
                        service.sessions.installation_identity(), 'READY', 'Scenario đã import và kiểm tra dữ liệu; chưa kiểm chứng nghiệm tối ưu.', digest))
    return service.public(rid, actor.actor_id)
