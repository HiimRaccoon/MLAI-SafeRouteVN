"""Portable supplemental trajectory semantics; NOT a physical authenticator.

Change Impact Analysis: valid execution-view/2 wire unchanged. Enforces exact
times, ordered actions and drawable directed geometry also in the JS consumer.
Only raw-source validators certify turns, travel rates, deadlines and custody.
"""
from collections.abc import Mapping
from fractions import Fraction
import re
from .protocol import require,identifier,number,int64,SAFE_INTEGER

ACTION_FIELDS={
 'EDGE':{'edge_id','from_node','to_node','incoming_edge','fraction_start','fraction_start_exact','fraction_end','geometry','feature_payload','distance_m','exposure','overlay_sha256','temporal_policy','temporal_segments'},
 'PICKUP':{'order_id','node_id','load_after_kg'},
 'SERVICE':{'order_id','node_id','load_after_kg','continuation'},
 'WAIT':{'node_id','order_id','reason'},
}
def exact(value,path):
    if isinstance(value,str):return int64(value,path)
    require(type(value) is int and abs(value)<=SAFE_INTEGER,'INVALID_DATA',path,'safe integer or canonical int64 string required');return value
def node(value,path):
    require(type(value) is int and 0<value<=SAFE_INTEGER,'INVALID_DATA',path,'positive safe JSON graph node ID required');return value
def coordinates(value,path):
    require(isinstance(value,list) and len(value)==2,'INVALID_DATA',path,'WGS84 [longitude,latitude] required')
    for i,n in enumerate(value):
        require(type(n) in (int,float) and abs(n)<=(180 if i==0 else 90),'INVALID_DATA',path+f'[{i}]','finite WGS84 bound')
def trajectory(value,path='accepted_trajectory'):
    if value is None:return
    fields={'job_id','profile','forecast','domain_sha256','vehicle_routes'}
    require(isinstance(value,Mapping) and set(value)==fields,'INVALID_DATA',path,'exact trajectory fields required')
    from .protocol import digest
    identifier(value['job_id'],path+'.job_id');digest(value['domain_sha256'],path+'.domain_sha256')
    require(isinstance(value['profile'],str) and value['profile'] in ('FASTEST','BALANCED','SAFER') and value['forecast'] is True,'INVALID_DATA',path+'.profile','forecast/profile required')
    routes=value['vehicle_routes'];require(isinstance(routes,list),'INVALID_DATA',path+'.vehicle_routes','array required');vehicles=set();served=set()
    for i,r in enumerate(routes):
        p=f'{path}.vehicle_routes[{i}]';require(isinstance(r,Mapping),'INVALID_DATA',p,'route object required')
        required={'vehicle_id','order_sequence','actions','start_us','return_us','start_node','end_node'}
        optional={'route_column_id','return_load_kg','total_distance_m','total_travel_time_s','total_exposure','total_cost_vnd','total_soft_lateness_s'}
        require(required<=set(r) and set(r)<=required|optional,'INVALID_DATA',p,'route fields required; unknown fields rejected')
        vid=identifier(r['vehicle_id'],p+'.vehicle_id');require(vid not in vehicles,'INVALID_DATA',p+'.vehicle_id','duplicate vehicle');vehicles.add(vid)
        seq=r['order_sequence'];require(isinstance(seq,list),'INVALID_DATA',p+'.order_sequence','array required')
        for j,o in enumerate(seq):
            identifier(o,p+f'.order_sequence[{j}]');require(o not in served,'INVALID_DATA',p+'.order_sequence','duplicate planned service');served.add(o)
        start,end=exact(r['start_us'],p+'.start_us'),exact(r['return_us'],p+'.return_us');require(end>=start,'INVALID_DATA',p+'.return_us','route time reversed')
        for k in ('start_node','end_node'):node(r[k],p+'.'+k)
        for k in optional&set(r):
            if k=='route_column_id':identifier(r[k],p+'.'+k)
            elif k=='return_load_kg':require(type(r[k]) in (int,float) and -1e-9<=r[k]<float('inf'),'INVALID_DATA',p+'.'+k,'finite source load within physical checker tolerance required')
            else:number(r[k],p+'.'+k)
        actions=r['actions'];require(isinstance(actions,list),'INVALID_DATA',p+'.actions','array required');previous=start
        for j,a in enumerate(actions):
            q=f'{p}.actions[{j}]';require(isinstance(a,Mapping),'INVALID_DATA',q,'action object required')
            kind=a.get('kind');require(isinstance(kind,str) and kind in ACTION_FIELDS,'INVALID_DATA',q+'.kind','known action kind required')
            require({'kind','start_us','end_us'}<=set(a) and set(a)<={'kind','start_us','end_us'}|ACTION_FIELDS[kind],'INVALID_DATA',q,'exact action shape required')
            b,e=exact(a['start_us'],q+'.start_us'),exact(a['end_us'],q+'.end_us');require(b>=previous and e>=b and e<=end,'INVALID_DATA',q+'.end_us','ordered nonoverlapping action times required');previous=e
            if kind=='EDGE':
                for k in ('edge_id','from_node','to_node','incoming_edge','fraction_start','fraction_end','geometry','feature_payload','distance_m','exposure'):require(k in a,'INVALID_DATA',q+'.'+k,'EDGE field required')
                identifier(a['edge_id'],q+'.edge_id')
                for k in ('from_node','to_node'):node(a[k],q+'.'+k)
                if a['incoming_edge'] is not None:identifier(a['incoming_edge'],q+'.incoming_edge')
                for k in ('distance_m','exposure','fraction_start','fraction_end'):number(a[k],q+'.'+k)
                require(a['fraction_start']<=a['fraction_end']<=1,'INVALID_DATA',q+'.fraction_end','bounded progress required')
                if 'fraction_start_exact' in a:
                    f=a['fraction_start_exact'];require(isinstance(f,str) and re.fullmatch(r'(?:0|[1-9][0-9]{0,18})(?:/[1-9][0-9]{0,18})?',f) is not None,'INVALID_DATA',q+'.fraction_start_exact','canonical nonnegative rational required')
                    parts=f.split('/');numerator=int(parts[0]);denominator=int(parts[1]) if len(parts)==2 else 1
                    require(0<=numerator<=denominator<(1<<63),'INVALID_DATA',q+'.fraction_start_exact','bounded exact int64 progress')
                geometry=a['geometry'];require(isinstance(geometry,list) and len(geometry)>=2,'INVALID_DATA',q+'.geometry','drawable directed line required')
                for k,xy in enumerate(geometry):coordinates(xy,q+f'.geometry[{k}]')
                require(isinstance(a['feature_payload'],Mapping),'INVALID_DATA',q+'.feature_payload','source feature object required')
                # Optional temporal metadata is portable contract data, not
                # source authentication or a mandated temporal policy version.
                if 'overlay_sha256' in a:digest(a['overlay_sha256'],q+'.overlay_sha256')
                if 'temporal_policy' in a:require(isinstance(a['temporal_policy'],str) and bool(a['temporal_policy']),'INVALID_DATA',q+'.temporal_policy','nonempty temporal policy required')
                if 'temporal_segments' in a:
                    require(isinstance(a['temporal_segments'],list),'INVALID_DATA',q+'.temporal_segments','segment array required')
                    prior=b
                    for k,s in enumerate(a['temporal_segments']):
                        z=q+f'.temporal_segments[{k}]';require(isinstance(s,Mapping),'INVALID_DATA',z,'segment object required')
                        sb,se=exact(s.get('start_us'),z+'.start_us'),exact(s.get('end_us'),z+'.end_us');require(sb>=prior and se>=sb and se<=e,'INVALID_DATA',z+'.end_us','ordered segment times required');prior=se
                        require(s.get('edge_id')==a['edge_id'] and s.get('layer') in ('BASELINE','WET','BASELINE_AFTER_EXPIRY'),'INVALID_DATA',z+'.edge_id','edge/layer binding required')
            elif kind in ('PICKUP','SERVICE'):
                for k in ('order_id','node_id','load_after_kg'):require(k in a,'INVALID_DATA',q+'.'+k,'custody action field required')
                identifier(a['order_id'],q+'.order_id');node(a['node_id'],q+'.node_id');require(type(a['load_after_kg']) in (int,float) and -1e-9<=a['load_after_kg']<float('inf'),'INVALID_DATA',q+'.load_after_kg','finite source load within physical checker tolerance required')
                if kind=='PICKUP':require(b==e,'INVALID_DATA',q+'.end_us','static pickup has zero duration')
                if 'continuation' in a:require(type(a['continuation']) is bool,'INVALID_DATA',q+'.continuation','boolean required')
            else:
                if 'node_id' in a:node(a['node_id'],q+'.node_id')
                if 'order_id' in a:identifier(a['order_id'],q+'.order_id')
                if 'reason' in a:require(isinstance(a['reason'],str) and bool(a['reason']),'INVALID_DATA',q+'.reason','meaningful reason required')
        require(not actions or previous==end,'INVALID_DATA',p+'.return_us','return must coincide with final action')
