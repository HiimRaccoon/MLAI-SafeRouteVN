"""Pinned primary M1 formulas, without loading M1's blocked GIS DLLs.

This is a pure faithful port of risk_values/travel_values, not a new model.
Both producer and checker read raw SQLite/tags and independently verify the
baseline features. Formula sharing is declared; hand oracles prevent common
mode assumptions. Source pins come from the Leader's Step6 reference receipt.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import hashlib, json, math, re
from typing import Mapping
from optimization.models.member1_dynamic_state import require,sha,finalize,finite_tree
from optimization.models.member1_rain import EdgeRates,OVERLAY_VERSION,POLICY,number,integer
from optimization.rolling_horizon.member1_motion_replay import _offset_us,_parse_time

RUN='scenarios/cached_context/hcmc/member1-tdbt-v1/'
PINS={
 'geo_data/features/risk_proxy.py':('45e8fed9d7b12d7a2f379f1881404305b1d5c84ca81f873fdb378b5ec124c859',3424),
 'geo_data/features/travel_time.py':('f55a6159ecb938c3dbac73eb7ec24eb11b3b1723ba47a9493b299a207daf191b',5994),
 'geo_data/features/edge_features.py':('b2702624e72877989fee6d303fe2c8bf2d26b7860339108d8ef4f94c7a6c4acd',5795),
 'scenarios/fixtures/thu-duc-binh-thanh-v1/S4.json':('34b100c4018e4b33aa11d4f49ee2234fb41dedafe305da1f9e3e6db881c94517',86952),
 RUN+'features/risk_model.json':('77828d58f6d87d9aa8b49979e4fca14b174e220511b6df5ee7b7711f5ee72b10',1096),
 RUN+'travel/profile.json':('12a4701f8f75659f445d4994bc34dfc0befd830a5c116945daa894658be0f7dc',2008),
 RUN+'weather/weather_context.json':('4038941ccdf51d04d471e31050e5c58da2cf50c089e6a681fd558e1c0f9e17b9',36853),
 RUN+'features/manifest.json':('ba87cf810c91123cf48ba45a3ee157a4fb1b74a6b1961f045ccd1e33ea22b5aa',1760),
 RUN+'travel/manifest.json':('9d1879ce00da7b6224410a873e42d5472b025ba367d43a6f6ef7d9231940b6d3',1339),
 RUN+'weather/manifest.json':('68ae1a6c8cd2fd0e9a09c4673bee67e248d81d51fa78f28ccf4f06ffea7db77b',6336),
}

def quantize_hours(hours):
    number(hours,'edge.travel_hours',positive=True)
    value=int((Decimal(str(hours))*3600*1000000).to_integral_value(rounding=ROUND_CEILING))
    return integer(value,'edge.full_duration_us',positive=True)

def near(actual,expected,path):
    number(actual,path)
    require(abs(actual-expected)<=max(1e-12,abs(expected)*1e-10),'BASELINE_REPRODUCTION',path,'pinned primary formula disagrees with frozen features')

def clamp(v):return min(1.,max(0.,v))

def primary_travel(edge,tags,profile,bucket):
    flags=[];highway=edge['highway'];speed=profile['speedKphByHighway'].get(highway,profile['fallbackSpeedKph'])
    if highway not in profile['speedKphByHighway']:flags.append('highway:fallback-speed')
    surface=tags.get('surface');speed*=profile['surfaceSpeedMultiplier'].get(surface,profile['unknownSurfaceMultiplier'])
    if surface not in profile['surfaceSpeedMultiplier']:flags.append('surface:missing-or-unknown')
    direction=edge['direction'];keys=(f'maxspeed:motorcycle:{direction}','maxspeed:motorcycle',f'maxspeed:{direction}','maxspeed')
    raw=next((tags[k] for k in keys if k in tags),None)
    match=re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(km/h|kmh|kph|mph)?\s*',raw) if isinstance(raw,str) else None
    limit=float(match[1])*(1.609344 if match[2]=='mph' else 1) if match else None
    if limit is not None and math.isfinite(limit) and limit>0:speed=min(speed,limit)
    else:flags.append('maxspeed:missing-or-unparsed')
    if any(k.startswith('maxspeed') and 'conditional' in k for k in tags):flags.append('maxspeed:conditional-not-evaluated')
    number(speed,'edge.speed',positive=True);number(edge['lengthKm'],'edge.lengthKm',positive=True)
    base=edge['lengthKm']/speed
    return speed,base,base*bucket['travelMultiplier'],flags

def primary_risk(edge,tags,weather,bucket,config):
    flags=[];fallback=config['missingFactor'];highway=config['highwayFactor'].get(edge['highway'],fallback)
    if edge['highway'] not in config['highwayFactor']:flags.append('risk:highway-fallback')
    width=edge['widthM']
    if width is None:width_factor=fallback;flags.append('risk:width-fallback')
    else:number(width,'edge.widthM',positive=True);width_factor=clamp(1-width/config['widthReferenceM'])
    surface=config['surfaceFactor'].get(tags.get('surface'),fallback)
    if tags.get('surface') not in config['surfaceFactor']:flags.append('risk:surface-fallback')
    road=sum(config['roadWeights'][k]*v for k,v in [('highway',highway),('width',width_factor),('surface',surface)])
    parts=[]
    for key,reference in [('precipitationMm','rainReferenceMmPerHour'),('windSpeedKph','windReferenceKph'),('visibilityM','visibilityReferenceM')]:
        value=weather[key]
        if value is None:parts.append(fallback);flags.append('risk:'+key+'-fallback')
        else:
            number(value,'weather.'+key);ratio=value/config[reference]
            if key=='precipitationMm':number(weather['precipitationIntervalHours'],'weather.precipitationIntervalHours',positive=True);ratio/=weather['precipitationIntervalHours']
            parts.append(clamp(1-ratio if key=='visibilityM' else ratio))
    if weather['fallbackUsed']:flags.append('weather:stale-fallback')
    factors={'roadFactor':road,'timeFactor':bucket['timeFactor'],'weatherFactor':max(parts),'trafficFactor':bucket['trafficFactor']}
    proxy=clamp(sum(config['weights'][k]*v for k,v in factors.items()))
    return {**factors,'edgeProxy':proxy,'relativeExposure':edge['lengthKm']*proxy,
            'weatherTravelMultiplier':1+config['weatherDelayScale']*factors['weatherFactor'],'missingFlags':flags,'sourceType':'PROXY'}

@dataclass
class SourceModel:
    risk:dict
    profile:dict
    weather:dict
    epoch:str
    source_pins:dict=field(default_factory=dict)
    def __post_init__(self):
        for key in ('risk','profile','weather','source_pins'):
            require(isinstance(getattr(self,key),Mapping),'INVALID_DATA','source_model.'+key,'source object required')
        finite_tree({'risk':self.risk,'profile':self.profile,'weather':self.weather})
        self.risk=deepcopy(self.risk);self.profile=deepcopy(self.profile);self.weather=deepcopy(self.weather)
        require(isinstance(self.weather.get('regions'),list),'WEATHER_SCHEMA','weather.regions','region array required')
        self.regions={}
        for i,r in enumerate(self.weather['regions']):
            require(isinstance(r,Mapping) and isinstance(r.get('regionId'),str),'WEATHER_SCHEMA',f'weather.regions[{i}]','typed region required')
            require(r['regionId'] not in self.regions,'WEATHER_SCHEMA',f'weather.regions[{i}].regionId','duplicate region')
            self.regions[r['regionId']]=r
        hour=_parse_time(self.epoch,'epoch').hour
        buckets=[b for b in self.profile['timeBuckets'] if b['startHour']<=hour<b['endHour']]
        require(len(buckets)==1,'PROFILE_BINDING','timeBuckets','frozen decision bucket required');self.bucket=buckets[0]
        self.cache={}

    def ingredients(self,graph,edge_id):
        require(isinstance(edge_id,str) and bool(edge_id),'INVALID_DATA','edge_id','nonempty directed edge ID required')
        if edge_id in self.cache:return self.cache[edge_id]
        raw=graph.edge(edge_id);require(raw is not None,'EDGE_MISSING','edge_id',str(edge_id))
        checked=graph._checked_edge(raw);payload=checked['payload']
        source=graph.db.execute('SELECT e.*,w.tagsJson FROM edges e JOIN ways w USING(osmWayId) WHERE e.edgeId=?',(edge_id,)).fetchone()
        require(source is not None,'EDGE_MISSING','edge_id','raw way/edge missing')
        edge=dict(source);tags=json.loads(edge['tagsJson']);require(isinstance(tags,Mapping),'PATH_SOURCE_INVALID','edge.tagsJson','OSM tags object required')
        weather=self.regions.get(payload.get('weatherRegionId'));require(weather is not None,'WEATHER_SCHEMA','edge.weatherRegionId','source region absent')
        speed,base,preweather,travel_flags=primary_travel(edge,tags,self.profile,self.bucket)
        baseline=primary_risk(edge,tags,weather,self.bucket,self.risk)
        wet_weather={**weather,'precipitationMm':12,'precipitationIntervalHours':1}
        wet=primary_risk(edge,tags,wet_weather,self.bucket,self.risk)
        dry_hours=preweather*baseline['weatherTravelMultiplier'];wet_hours=preweather*wet['weatherTravelMultiplier']
        source_flags=json.loads(edge['missingFlagsJson'])
        require(isinstance(source_flags,list) and all(isinstance(f,str) for f in source_flags),'PATH_SOURCE_INVALID','edge.'+edge_id+'.missingFlagsJson','string flags array required')
        flags=sorted(set(source_flags+travel_flags+baseline['missingFlags']))
        for k,x in [('baseSpeedKph',speed),('baseTravelTimeHours',base),('travelTimeHours',dry_hours),('travelMultiplier',self.bucket['travelMultiplier']*baseline['weatherTravelMultiplier'])]:near(payload.get(k),x,'edge.'+edge_id+'.'+k)
        for k in ('roadFactor','timeFactor','weatherFactor','trafficFactor','edgeProxy','relativeExposure','weatherTravelMultiplier'):near(payload.get(k),baseline[k],'edge.'+edge_id+'.'+k)
        near(raw['travelTimeHours'],dry_hours,'edge.'+edge_id+'.SQLite.travelTimeHours');near(raw['relativeExposure'],baseline['relativeExposure'],'edge.'+edge_id+'.SQLite.relativeExposure')
        require(payload.get('timeBucket')==self.bucket['id'] and payload.get('missingFlags')==flags and payload.get('fallbackUsed') is bool(flags),'BASELINE_REPRODUCTION','edge.'+edge_id+'.flags','baseline bucket/flags mismatch')
        require(payload.get('weatherFallbackUsed')==weather['fallbackUsed'] and payload.get('weatherValidAt')==weather['validAt'],'BASELINE_REPRODUCTION','edge.'+edge_id+'.weather','baseline weather provenance differs')
        result={'baseline':{'duration_us':quantize_hours(raw['travelTimeHours']),'travel_hours':dry_hours,**baseline},
            'wet':{'duration_us':quantize_hours(wet_hours),'travel_hours':wet_hours,**wet},
            'length_m':edge['lengthKm']*1000,'from_node':edge['fromNodeId'],'to_node':edge['toNodeId'],
            'pre_weather_travel_hours':preweather,'time_bucket':self.bucket['id'],
            'feature_flags':{k:payload.get(k) for k in ('missingFlags','fallbackUsed','weatherFallbackUsed','weatherRegionId','weatherValidAt','sourceType','travelSourceType','riskModelVersion')},
            'geometry':[[p.longitude,p.latitude] for p in checked['points']],'feature_payload':payload}
        self.cache[edge_id]=result;return result

def load_source_model(snapshot,state):
    if state.source_authentication == 'RUN_MANIFEST_VERIFIED':
        from optimization.runtime.run_source import verified_source
        paths, hashes = verified_source(snapshot, state)
        return SourceModel(json.loads(paths['risk_model_sha256'].read_bytes()),
            json.loads(paths['travel_profile_sha256'].read_bytes()),
            json.loads(paths['weather_sha256'].read_bytes()), state.decision_epoch,
            {key: {'sha256': hashes[key], 'bytes': path.stat().st_size} for key,path in paths.items()})
    snapshot=Path(snapshot);raws={};pins={}
    for rel,(digest,size) in PINS.items():
        raw=(snapshot/rel).read_bytes()
        require(len(raw)==size and hashlib.sha256(raw).hexdigest()==digest,'SOURCE_MISMATCH',rel,'Step6 independent reference pin differs')
        raws[rel]=raw;pins[rel]={'sha256':digest,'bytes':size}
    require(state.scenario_id=='S4' and state.fixture_raw_sha256==PINS['scenarios/fixtures/thu-duc-binh-thanh-v1/S4.json'][0],'SOURCE_BINDING','fixture_raw_sha256','own pinned S4 state required')
    return SourceModel(json.loads(raws[RUN+'features/risk_model.json']),json.loads(raws[RUN+'travel/profile.json']),json.loads(raws[RUN+'weather/weather_context.json']),state.decision_epoch,pins)

def event_interval(state):
    require(len(state.pending_events)==1,'EVENT_BINDING','pending_events','one S4 event required')
    event=state.pending_events[0];payload=event.to_dict()['payload'];finite_tree(payload,'event.payload')
    require(event.event_type=='LOCAL_RAIN_WHAT_IF' and event.scenario_id==state.scenario_id,'EVENT_BINDING','event','own S4 rain event required')
    require(payload.get('requiresFeatureRecompute') is True and payload.get('contextDelta')=={'precipitationMm':12,'precipitationIntervalHours':1},'EVENT_BINDING','event.contextDelta','absolute pinned rain required')
    require(payload.get('eventId')==event.event_id and payload.get('timestamp')==event.timestamp,'EVENT_BINDING','event.timestamp','raw payload/typed envelope identity differs')
    edges=payload.get('affectedEdgeIds')
    require(isinstance(edges,list) and all(isinstance(e,str) and bool(e) for e in edges) and len(edges)==len(set(edges)),'INVALID_DATA','event.affectedEdgeIds','unique exact directed IDs required')
    epoch=_parse_time(state.decision_epoch,'epoch')
    start=_offset_us(epoch,payload.get('startTime'),'event.startTime',signed=True);end=_offset_us(epoch,payload.get('endTime'),'event.endTime',signed=True)
    require(start<end and payload.get('timestamp')==payload.get('startTime'),'EVENT_BINDING','event.endTime','half-open rain interval required')
    return event,edges,start,end

def build_overlay(state,graph,model):
    event,edges,start,end=event_interval(state)
    table={}
    for edge_id in edges:
        row=model.ingredients(graph,edge_id)
        table[edge_id]={k:deepcopy(row[k]) for k in ('baseline','wet','length_m','from_node','to_node','pre_weather_travel_hours','time_bucket','feature_flags')}
        table[edge_id]['expired']=deepcopy(row['baseline'])
    return finalize({'schema_version':OVERLAY_VERSION,'scenario_id':state.scenario_id,'initial_state_sha256':sha(state.to_dict()),
        'event_id':event.event_id,'event_sha256':sha(event.to_dict()),'temporal_policy':POLICY,'start_us':start,'end_us':end,
        'affected_edge_ids':edges,'affected_edges_sha256':sha(edges),'edge_count':len(edges),'cost_table':table,
        'source_pins':model.source_pins,'source_hashes':graph.source_hashes,'source_versions':{'routing':state.routing_version,'features':state.features_version,'context':state.context_version},
        'fixture_raw_sha256':state.fixture_raw_sha256,'catalog_raw_sha256':state.catalog_raw_sha256,'receipt_sha256':state.receipt_sha256,
        'traffic_policy':'frozen-decision-epoch','synthetic':True,'expiry_policy':'SYNTHETIC_RETURN_TO_PINNED_BASELINE',
        'units':{'distance':'m','duration':'signed us','exposure':'relative exposure proxy (km × edgeProxy)','rain':'absolute mm per hour'}})

def source_edge_rates(state,graph,model,overlay,edge_id):
    row=model.ingredients(graph,edge_id)
    return EdgeRates(edge_id,row['from_node'],row['to_node'],row['length_m'],row['baseline']['duration_us'],
        row['wet']['duration_us'],row['baseline']['edgeProxy'],row['wet']['edgeProxy'],overlay['start_us'],overlay['end_us'],
        edge_id in overlay['cost_table'])
