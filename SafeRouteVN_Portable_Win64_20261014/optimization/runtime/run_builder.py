"""Build a new demo run with fresh model weather and frozen estimated costs.

Uses only the standard library and the already reviewed primary formulas.
All outputs are private until manifest.json is atomically published last.
"""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .run_source import SCHEMA, file_sha, run_directory, load_state
from .protocol import sha, instant, require
from optimization.rolling_horizon.member1_rain_source import primary_travel, primary_risk

VN = timezone(timedelta(hours=7))
BASE = 'scenarios/cached_context/hcmc/member1-tdbt-v1'
TIME_FIELDS = {'currentTime', 'openingTime', 'closingTime', 'workingStart', 'workingEnd',
               'earliest', 'preferredDue', 'hardDeadline', 'pickedUpAt', 'deliveredAt',
               'timestamp', 'startTime', 'endTime', 'positionTimestamp'}


def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False), encoding='utf8')
    temp.replace(path)


def rebase(fixture, epoch, event_after_minutes=15):
    result = deepcopy(fixture)
    origin = instant(result['initialState']['currentTime'], 'origin')
    delta = instant(epoch, 'epoch') - origin
    def shift(value, offset):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in TIME_FIELDS and item is not None:
                    value[key] = (instant(item, key) + offset).isoformat()
                else: shift(item, offset)
        elif isinstance(value, list):
            for item in value: shift(item, offset)
    shift(result, delta)
    for event in result['events']:
        offset = instant(epoch, 'epoch') + timedelta(minutes=event_after_minutes) - instant(event['timestamp'], 'event')
        shift(event, offset)
    # Historical tradeoff/QA claims do not certify this new context.
    result.pop('tradeoffEvidence', None)
    result['validation'] = {'integrated': False, 'jointVRPFeasibilityProven': False}
    return result


def fetch_weather(regions, epoch, *, opener=urlopen, sleep=time.sleep):
    at = instant(epoch, 'epoch'); wanted = int(at.replace(minute=0, second=0, microsecond=0).timestamp())
    params = {'latitude': ','.join(str(r['latitude']) for r in regions),
              'longitude': ','.join(str(r['longitude']) for r in regions),
              'hourly': 'precipitation,wind_speed_10m,visibility,weather_code',
              'timeformat': 'unixtime', 'timezone': 'Asia/Ho_Chi_Minh', 'wind_speed_unit': 'kmh',
              'precipitation_unit': 'mm', 'start_date': at.date().isoformat(), 'end_date': at.date().isoformat()}
    request = Request('https://api.open-meteo.com/v1/forecast?' + urlencode(params),
                      headers={'User-Agent': 'SafeRouteVN-demo/1 (weather model snapshots)'})
    for attempt in range(3):
        try:
            with opener(request, timeout=45) as response:
                raw = json.load(response)
            break
        except (HTTPError, URLError, TimeoutError) as error:
            if attempt == 2 or isinstance(error, HTTPError) and error.code != 429 and error.code < 500: raise
            sleep(2 ** (attempt + 1))
    fetched = datetime.now(VN).isoformat()
    payloads = raw if isinstance(raw, list) else [raw]
    require(len(payloads) == len(regions), 'WEATHER_INVALID', 'regions', 'Incomplete weather response')
    records = []
    fields = {'precipitation': ('precipitationMm', 'mm'), 'wind_speed_10m': ('windSpeedKph', 'km/h'),
              'visibility': ('visibilityM', 'm'), 'weather_code': ('weatherCode', 'wmo code')}
    for region, payload in zip(regions, payloads):
        hourly, units = payload['hourly'], payload['hourly_units']; times = hourly['time']
        require(units['time'] == 'unixtime' and wanted in times and all(b-a == 3600 for a,b in zip(times,times[1:])),
                'WEATHER_INVALID', 'time', 'Hourly series does not cover run epoch')
        index = times.index(wanted); values = {}; missing = []
        for key, (field, unit) in fields.items():
            require(str(units[key]).lower() == unit and len(hourly[key]) == len(times), 'WEATHER_INVALID', key, 'Weather units/coverage differ')
            value = hourly[key][index]
            require(value is None or type(value) in (int,float) and math.isfinite(value) and value >= 0,
                    'WEATHER_INVALID', key, 'Nonfinite/negative weather value')
            if value is None: missing.append(field)
            values[field] = value
        records.append({**values, 'regionId': region['regionId'], 'validAt': datetime.fromtimestamp(wanted,VN).isoformat(),
            'fetchedAt': fetched, 'precipitationIntervalHours': 1, 'precipitationInterval': 'preceding-hour-ending-at-validAt',
            'fallbackUsed': False, 'sourceTimestamp': None, 'model': None,
            'missingFlags': missing + ['sourceTimestamp','model'], 'provider': 'Open-Meteo Forecast',
            'requestedLatitude': region['latitude'], 'requestedLongitude': region['longitude'],
            'providerLatitude': payload.get('latitude'), 'providerLongitude': payload.get('longitude')})
    return {'at': epoch, 'regions': records, 'sourceType': 'REAL-derived', 'providerAttribution': 'Weather data by Open-Meteo, CC BY 4.0'}, raw


def build(root, run_id, scenario_id, epoch, event_after_minutes=15, *, weather_fetch=fetch_weather, progress=lambda message: None):
    root = Path(root).resolve(); output = run_directory(root, run_id); output.mkdir(parents=True, exist_ok=True)
    require(not (output/'manifest.json').exists(), 'RUN_EXISTS', 'run_id', 'Published runs are immutable')
    require(scenario_id in ('S0','S1','S2','S3','S4') and type(event_after_minutes) is int and 1 <= event_after_minutes <= 120,
            'RUN_INVALID', 'scenario', 'Supported demo scenario and event delay required')
    instant(epoch, 'epoch'); base = root / BASE
    receipt = json.loads((root/'optimization/integration/member1_trusted_receipt.json').read_bytes())
    template = root / f'scenarios/fixtures/thu-duc-binh-thanh-v1/{scenario_id}.json'
    require(file_sha(template) == receipt['fixture_raw_sha256'][scenario_id], 'SOURCE_CHANGED', 'template', 'Historical template changed')
    from optimization.integration.member1_s0_runner import PINNED_SOURCE_HASHES
    require(file_sha(base/'routing/network.sqlite') == PINNED_SOURCE_HASHES['network_sqlite_sha256'], 'SOURCE_CHANGED', 'network', 'Routing source changed')
    # Formulas consume the verified baseline profiles and mapping, not client input.
    for stage, names in {'routing':['network.sqlite'], 'weather-plan':['regions.json','mapping.sqlite'],
                         'travel':['profile.json'], 'features':['risk_model.json']}.items():
        manifest = json.loads((base/stage/'manifest.json').read_bytes())
        require(manifest['version'] == receipt['stage_versions'][stage], 'SOURCE_CHANGED', stage, 'Baseline stage version changed')
        for name in names:
            require(file_sha(base/stage/name) == manifest['files'][name]['sha256'], 'SOURCE_CHANGED', name, 'Baseline artifact changed')
    regions = json.loads((base/'weather-plan/regions.json').read_bytes())['regions']
    progress('Đang lấy dự báo thời tiết cho các vùng...')
    weather, raw = weather_fetch(regions, epoch)
    write(output/'weather.json', weather); write(output/'weather_raw.json', raw)
    context = sha(weather); by_region = {r['regionId']:r for r in weather['regions']}
    require(set(by_region) == {r['regionId'] for r in regions} and all(not r['fallbackUsed'] for r in by_region.values()),
            'WEATHER_INVALID', 'regions', 'Fresh complete weather required')
    profile = json.loads((base/'travel/profile.json').read_bytes()); risk = json.loads((base/'features/risk_model.json').read_bytes())
    write(output/'travel_profile.json', profile); write(output/'risk_model.json', risk)
    hour = instant(epoch,'epoch').hour
    bucket = next(b for b in profile['timeBuckets'] if b['startHour'] <= hour < b['endHour'])
    routing = json.loads((base/'routing/manifest.json').read_bytes())
    version = sha({'context':context,'routing':routing['version'],'at':epoch,'profile':profile,'risk':risk})
    temporary = output/'features.partial.sqlite'
    if temporary.exists(): temporary.unlink()  # Only this unpublished run's known temporary file.
    progress('Đang tính lại thời gian và exposure trên mạng đường...')
    with closing(sqlite3.connect((base/'routing/network.sqlite').resolve().as_uri()+'?mode=ro',uri=True)) as source, closing(sqlite3.connect(temporary)) as target:
        source.row_factory = sqlite3.Row
        source.execute('ATTACH DATABASE ? AS grid', ((base/'weather-plan/mapping.sqlite').resolve().as_uri()+'?mode=ro',))
        target.execute('CREATE TABLE features(edgeId TEXT PRIMARY KEY,travelTimeHours REAL NOT NULL,relativeExposure REAL NOT NULL,edgeProxy REAL NOT NULL,regionId TEXT NOT NULL,payloadJson TEXT NOT NULL)')
        count = 0; batch = []
        for edge in source.execute('SELECT e.*,w.tagsJson,r.regionId FROM edges e JOIN ways w USING(osmWayId) JOIN grid.edge_regions r USING(edgeId) ORDER BY e.edgeId'):
            tags=json.loads(edge['tagsJson']); region=by_region[edge['regionId']]
            speed,base_time,travel,flags=primary_travel(edge,tags,profile,bucket)
            flags=sorted(set(flags+json.loads(edge['missingFlagsJson'])))
            factors=primary_risk(edge,tags,region,bucket,risk); duration=travel*factors['weatherTravelMultiplier']
            require(math.isfinite(duration) and duration>0 and math.isfinite(factors['relativeExposure']), 'FEATURE_INVALID','edge','Invalid edge costs')
            payload={'edgeId':edge['edgeId'],'fromNodeId':edge['fromNodeId'],'toNodeId':edge['toNodeId'],
                'lengthKm':edge['lengthKm'],'widthM':edge['widthM'],'baseSpeedKph':speed,'baseTravelTimeHours':base_time,
                'travelTimeHours':duration,'travelMultiplier':bucket['travelMultiplier']*factors['weatherTravelMultiplier'],
                'timeBucket':bucket['id'],'weatherRegionId':edge['regionId'],'weatherValidAt':region['validAt'],
                'weatherFallbackUsed':False,**factors,'missingFlags':sorted(set(flags+factors['missingFlags'])),
                'fallbackUsed':bool(flags or factors['missingFlags']),'travelSourceType':'ESTIMATED',
                'graphVersion':routing['graphVersion'],'contextVersion':context,'riskModelVersion':risk['riskModelVersion']}
            batch.append((edge['edgeId'],duration,factors['relativeExposure'],factors['edgeProxy'],edge['regionId'],json.dumps(payload,separators=(',',':'),allow_nan=False)))
            count+=1
            if len(batch)==5000: target.executemany('INSERT INTO features VALUES(?,?,?,?,?,?)',batch); batch=[]
            if count%100000==0: progress(f'Đã tính {count:,}/{routing["edgeCount"]:,} cạnh đường')
        target.executemany('INSERT INTO features VALUES(?,?,?,?,?,?)',batch)
        require(count==routing['edgeCount'],'FEATURE_INVALID','coverage','Missing graph edges')
        target.commit()
    temporary.replace(output/'features.sqlite')
    feature_hash=file_sha(output/'features.sqlite')
    write(output/'features_manifest.json',{'schemaVersion':'member1-edge-features/1','complete':True,'version':version,
        'routingVersion':routing['version'],'contextVersion':context,'at':epoch,'edgeCount':count,
        'files':{'features.sqlite':{'sha256':feature_hash,'bytes':(output/'features.sqlite').stat().st_size}}})
    fixture=rebase(json.loads(template.read_bytes()),epoch,event_after_minutes)
    fixture.update(contextVersion=context,featuresVersion=version)
    write(output/'fixture.json',fixture)
    paths={'network':base/'routing/network.sqlite','routing_manifest':base/'routing/manifest.json',
        'features':output/'features.sqlite','features_manifest':output/'features_manifest.json','fixture':output/'fixture.json',
        'weather':output/'weather.json','weather_raw':output/'weather_raw.json','risk_model':output/'risk_model.json','travel_profile':output/'travel_profile.json'}
    artifacts={role:{'path':p.relative_to(root).as_posix(),'sha256':file_sha(p),'bytes':p.stat().st_size} for role,p in paths.items()}
    now=datetime.now(VN); oldest=min(r['fetchedAt'] for r in weather['regions']); valid=min(r['validAt'] for r in weather['regions'])
    require(0 <= (now-instant(oldest,'fetchedAt')).total_seconds() <= 7200 and 0 <= (now-instant(valid,'validAt')).total_seconds() <=7200,
            'WEATHER_STALE','weather','Capture expired before publication')
    manifest={'schema_version':SCHEMA,'run_id':run_id,'scenario_id':scenario_id,'epoch':epoch,'artifacts':artifacts,
        'weather':{'fetched_at':oldest,'valid_at':valid,'expires_at':(min(instant(oldest,'fetchedAt'),instant(valid,'validAt'))+timedelta(hours=2)).isoformat(),
            'provider':'Open-Meteo Forecast','region_count':len(regions),'fallback_used':False,'source_timestamp':None},
        'traffic_source':'ESTIMATED','temporal_model':'FROZEN_DECISION_EPOCH','execution_mode':'SIMULATED_REPLAY',
        'event_after_minutes':event_after_minutes,'created_at':now.isoformat(),'feasibility':'NOT_YET_SOLVED',
        'template_sha256':receipt['fixture_raw_sha256'][scenario_id]}
    write(output/'manifest.json',manifest)
    digest=file_sha(output/'manifest.json'); load_state(root,run_id,digest)
    progress('Dữ liệu phiên mới đã được kiểm chứng.')
    return digest
