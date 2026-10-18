"""Strict internal wire boundary; does not mutate the frozen public API.

Change Impact Analysis: additive runtime-only contract. Exact integers are
canonical decimal strings on this wire, never rounded through JS Number.
"""
from collections.abc import Mapping
from datetime import datetime,timedelta
from fractions import Fraction
import hashlib,json,math,re

VERSION='task02-m2-runtime-command/1'
RESPONSE_VERSION='task02-m2-runtime-response/1'
VIEW_VERSION='task02-m2-execution-view/1'
OPERATIONS=('capabilities','bootstrap','resolve','submit','compute','get_job','cancel','apply_event','accept','advance','get_head','recover','dispatch')
PROFILES=('FASTEST','BALANCED','SAFER')
SAFE_INTEGER=(1<<53)-1

class RuntimeError(ValueError):
    def __init__(self,code,path,message):
        super().__init__(message);self.code,self.path=code,path
    def diagnostic(self):return {'severity':'ERROR','code':self.code,'path':self.path,'message':str(self)}

def require(ok,code,path,message):
    if not ok:raise RuntimeError(code,path,message)

def identifier(value,path):
    require(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}',value) is not None,'INVALID_DATA',path,'bounded nonempty identifier required')
    return value

def digest(value,path):
    require(isinstance(value,str) and re.fullmatch('[a-f0-9]{64}',value) is not None,'INVALID_DATA',path,'lowercase SHA-256 required')
    return value

def int64(value,path):
    require(isinstance(value,str) and len(value)<=20 and re.fullmatch(r'0|-?[1-9][0-9]*',value) is not None,'INVALID_DATA',path,'canonical decimal string required')
    n=int(value);require(-(1<<63)<=n<(1<<63),'INVALID_DATA',path,'signed int64 required');return n

def number(value,path,*,positive=False):
    require(type(value) in (int,float),'INVALID_DATA',path,'native finite number required')
    try:valid=math.isfinite(float(value)) and (value>0 if positive else value>=0)
    except OverflowError:valid=False
    require(valid,'INVALID_DATA',path,'finite nonnegative number required');return value

def instant(value,path):
    require(isinstance(value,str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+07:00',value) is not None,'INVALID_DATA',path,'ISO +07:00, at most six fractional digits required')
    try:d=datetime.fromisoformat(value)
    except ValueError as e:raise RuntimeError('INVALID_DATA',path,'invalid timestamp') from e
    require(d.utcoffset()==timedelta(hours=7),'INVALID_DATA',path,'+07:00 required');return d

def offset(epoch,time):
    delta=instant(time,'time')-instant(epoch,'epoch')
    n=(delta.days*86400+delta.seconds)*1000000+delta.microseconds
    require(-(1<<63)<=n<(1<<63),'INVALID_DATA','time','signed int64 time required');return n

def tree(value,path='$',active=None,depth=0):
    require(depth<96,'INVALID_DATA',path,'JSON depth exceeded')
    if value is None or type(value) in (str,bool):return
    if type(value) is int:
        require(-SAFE_INTEGER<=value<=SAFE_INTEGER,'INVALID_DATA',path,'exact large integers must use decimal strings');return
    if type(value) is float:
        require(math.isfinite(value),'INVALID_DATA',path,'nonfinite JSON number');return
    active=set() if active is None else active
    if isinstance(value,(Mapping,list)):
        require(id(value) not in active,'INVALID_DATA',path,'cyclic JSON');active.add(id(value))
        if isinstance(value,Mapping):
            for k,v in value.items():
                require(isinstance(k,str),'INVALID_DATA',path,'string object key required');tree(v,path+'.'+k,active,depth+1)
        else:
            for i,v in enumerate(value):tree(v,f'{path}[{i}]',active,depth+1)
        active.remove(id(value))
    elif type(value) is int:require(-SAFE_INTEGER<=value<=SAFE_INTEGER,'INVALID_DATA',path,'exact large integers must use decimal strings')
    elif type(value) is float:require(math.isfinite(value),'INVALID_DATA',path,'nonfinite JSON number')
    else:require(value is None or type(value) in (str,bool),'INVALID_DATA',path,'JSON value required')

def decode(raw):
    def pairs(items):
        out={}
        for k,v in items:
            require(k not in out,'DUPLICATE_JSON_KEY',k,'duplicate object key');out[k]=v
        return out
    def bad(value):raise RuntimeError('INVALID_DATA','$','nonfinite JSON token '+value)
    try:out=json.loads(raw,object_pairs_hook=pairs,parse_constant=bad)
    except (json.JSONDecodeError,UnicodeError,ValueError,RecursionError) as e:
        if isinstance(e,RuntimeError):raise
        raise RuntimeError('INVALID_DATA','$','invalid JSON transport') from e
    tree(out);return out

def canonical(value):
    tree(value);return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf8')
def sha(value):return hashlib.sha256(canonical(value)).hexdigest()
def copy(value):return decode(canonical(value))

def command(value):
    require(isinstance(value,Mapping),'INVALID_DATA','$','command object required');tree(value)
    allowed={'schema_version','operation','command_id','session_id','scenario_id','job_id','basis','profile','target_time','event_id','budget_seconds','action','data'}
    require(not(set(value)-allowed),'UNKNOWN_FIELD','$','unknown command fields')
    require(value.get('schema_version')==VERSION,'VERSION_MISMATCH','schema_version','runtime command /1 required')
    op=value.get('operation');require(isinstance(op,str) and op in OPERATIONS,'INVALID_DATA','operation','known operation required')
    extras={'bootstrap':{'scenario_id'},'submit':{'basis','profile','budget_seconds'},'compute':{'job_id'},'get_job':{'job_id'},'cancel':{'job_id'},'accept':{'job_id','basis'},'advance':{'basis','target_time'},'apply_event':{'basis','event_id'}}
    extras['dispatch']={'basis','action','data'}
    permitted={'schema_version','operation','command_id'}|({'session_id'} if op!='capabilities' else set())|extras.get(op,set())
    require(not(set(value)-permitted),'UNKNOWN_FIELD','$','field is not meaningful for this operation')
    identifier(value.get('command_id'),'command_id')
    if op!='capabilities':identifier(value.get('session_id'),'session_id')
    if op=='bootstrap':
        s=value.get('scenario_id');require(isinstance(s,str) and s in tuple('S'+str(i) for i in range(9)),'INVALID_DATA','scenario_id','pinned scenario ID required')
    if op in ('compute','get_job','cancel','accept'):identifier(value.get('job_id'),'job_id')
    if op in ('submit','accept','advance','apply_event','dispatch'):
        require(isinstance(value.get('basis'),Mapping),'INVALID_DATA','basis','server-resolved basis required')
        basis(value['basis'])
    if op=='advance':instant(value.get('target_time'),'target_time')
    if op=='apply_event':identifier(value.get('event_id'),'event_id')
    if op=='dispatch':
        identifier(value.get('action'),'action')
        require(isinstance(value.get('data'),dict),'INVALID_DATA','data','object required')
    if 'profile' in value:require(isinstance(value['profile'],str) and value['profile'] in PROFILES,'INVALID_DATA','profile','locked profile required')
    if 'budget_seconds' in value:
        number(value['budget_seconds'],'budget_seconds',positive=True);require(value['budget_seconds']<=600,'INVALID_DATA','budget_seconds','budget at most existing 600 s cap')
    return value

def basis(value):
    keys={'session_id','root_sha256','head_sha256','head_version','generation','source_sha256','context_version','overlay_sha256','build_sha256'}
    require(isinstance(value,Mapping) and set(value)==keys,'INVALID_DATA','basis','exact immutable basis fields required')
    identifier(value['session_id'],'basis.session_id')
    for k in ('root_sha256','head_sha256','source_sha256','build_sha256'):digest(value[k],'basis.'+k)
    require(value['overlay_sha256'] is None or isinstance(value['overlay_sha256'],str),'INVALID_DATA','basis.overlay_sha256','null or SHA-256')
    if value['overlay_sha256'] is not None:digest(value['overlay_sha256'],'basis.overlay_sha256')
    identifier(value['context_version'],'basis.context_version')
    for k in ('head_version','generation'):require(int64(value[k],'basis.'+k)>=0,'INVALID_DATA','basis.'+k,'nonnegative counter required')
    return value

def wire_exact(value):
    """Only internal head counters/times/progress; valid API v1 is not touched."""
    if isinstance(value,Mapping):return {k:wire_exact(v) for k,v in value.items()}
    if isinstance(value,list):return [wire_exact(v) for v in value]
    if type(value) is int and abs(value)>SAFE_INTEGER:return str(value)
    if isinstance(value,Fraction):return {'numerator':str(value.numerator),'denominator':str(value.denominator)}
    return value

def response(command_id,status,value=None,diagnostics=()):
    command_id=command_id if isinstance(command_id,str) and len(command_id)<=200 else None
    return {'schema_version':RESPONSE_VERSION,'command_id':command_id,'status':status,'value':value,'diagnostics':list(diagnostics)}
