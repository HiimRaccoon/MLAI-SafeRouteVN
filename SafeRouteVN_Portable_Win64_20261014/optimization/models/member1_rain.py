"""Pure typed temporal DTO primitives. Not a source authentication API."""
from dataclasses import dataclass
from fractions import Fraction
import math
from typing import Mapping
from optimization.models.member1_dynamic_state import observed_fields
from optimization.models.member1_dynamic_state import DynamicError, require, finite_tree, sha, finalize

POLICY='task02-m1-temporal-edge-piecewise-rate/1'
OVERLAY_VERSION='task02-m1-rain-overlay/1'
STATE_VERSION='task02-m1-temporal-rain-state/1'
TRANSITION_VERSION='task02-m1-rain-transition/1'
DOMAIN_VERSION='task02-m1-temporal-rain-domain/1'
RESULT_VERSION='task02-m1-temporal-rain-forecast/1'
SOLVER_VERSION='task02-m1-temporal-rain-route-columns/1'


def integer(value,path,*,positive=False):
    require(type(value) is int and -(2**63)<value<2**63 and (not positive or value>0),
            'INVALID_DATA',path,'signed int64 microseconds/integer required; no bool alias')
    return value


def number(value,path,*,positive=False):
    require(type(value) in (int,float),'INVALID_DATA',path,'finite number required')
    try:valid=math.isfinite(float(value)) and (value>0 if positive else value>=0)
    except OverflowError:valid=False
    require(valid,'INVALID_DATA',path,'finite nonnegative number required')
    return value


def fraction(value,path):
    require(isinstance(value,(Fraction,str)) or type(value) in (int,float),
            'INVALID_DATA',path,'rational progress required')
    if type(value) in (int,float):number(value,path)
    try:out=value if isinstance(value,Fraction) else Fraction(str(value))
    except (ValueError,ZeroDivisionError) as error:
        raise DynamicError('INVALID_DATA',path,'invalid rational progress') from error
    require(0<=out<=1,'INVALID_DATA',path,'progress outside directed edge')
    return out


@dataclass(frozen=True)
class EdgeRates:
    edge_id:str
    from_node:int
    to_node:int
    distance_m:float
    baseline_us:int
    wet_us:int
    baseline_proxy:float
    wet_proxy:float
    start_us:int
    end_us:int
    affected:bool=True
    def __post_init__(self):
        require(isinstance(self.edge_id,str) and bool(self.edge_id),'INVALID_DATA','edge_id','directed ID required')
        for k in ('from_node','to_node','baseline_us','wet_us'):integer(getattr(self,k),k,positive=True)
        for k in ('start_us','end_us'):integer(getattr(self,k),k)
        require(self.start_us<self.end_us,'INVALID_DATA','end_us','half-open interval required')
        for k in ('distance_m','baseline_proxy','wet_proxy'):number(getattr(self,k),k)
        require(self.baseline_proxy<=1 and self.wet_proxy<=1,'INVALID_DATA','wet_proxy','proxy factors in [0,1]')
        require(type(self.affected) is bool,'INVALID_DATA','affected','boolean required')

    def layer(self,clock):
        if self.affected and self.start_us<=clock<self.end_us:return 'WET',self.wet_us,self.wet_proxy,self.end_us
        if clock<self.start_us:return 'BASELINE',self.baseline_us,self.baseline_proxy,self.start_us
        if clock<self.end_us:return 'BASELINE',self.baseline_us,self.baseline_proxy,self.end_us
        return 'BASELINE_AFTER_EXPIRY',self.baseline_us,self.baseline_proxy,None


def check_state(value):
    require(isinstance(value,Mapping),'INVALID_DATA','after_state','temporal state object required')
    finite_tree(value,'after_state')
    require(value.get('schema_version')==STATE_VERSION,'VERSION_MISMATCH','after_state.schema_version','rain state /1 required')
    require(value.get('scenario_id')=='S4','SOURCE_BINDING','after_state.scenario_id','own S4 state required')
    integer(value.get('head_version'),'after_state.head_version',positive=True)
    integer(value.get('state_version'),'after_state.state_version',positive=True)
    require(value.get('state_version')==value['head_version'],'STATE_BINDING','after_state.state_version','head and observed version agree')
    observed_fields(value)
    body=dict(value);body.pop('content_sha256',None)
    require(value.get('content_sha256')==sha(body),'STATE_DIGEST','after_state.content_sha256','state differs')
    require(value.get('temporal_policy')==POLICY and value.get('forecast_materialized') is False,
            'FORECAST_OBSERVATION','after_state.temporal_policy','only observed head, not a forecast observation')
    return value
