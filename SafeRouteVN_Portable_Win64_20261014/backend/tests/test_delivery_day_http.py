from copy import deepcopy
from fastapi.testclient import TestClient
from backend.api.main import create_app
from backend.tests.test_sessions import setup,headers


def test_dispatch_owner_revision_and_retry_keep_exact_basis(setup):
    settings,gateway=setup;calls=[]
    def dispatch(operation,fields):
        calls.append(deepcopy(fields))
        return {'status':'DISPATCHED','basis':fields['basis']}
    gateway._call=dispatch
    with TestClient(create_app(settings,gateway=gateway)) as client:
        loaded=client.post('/api/scenarios/S0/load',json={'request_id':'load'},headers=headers()).json()['data']
        sid=loaded['session']['session_id'];basis=loaded['execution_view']['basis']
        body={'request_id':'operation','basis':basis,'expected_revision':{k:basis[k] for k in ('head_version','generation')},'action':'enable','data':{}}
        endpoint=f'/api/sessions/{sid}/delivery-day'
        assert client.post(endpoint,json=body,headers=headers('bob')).status_code==403
        assert client.post(endpoint,json=body).status_code==401
        assert not calls
        assert client.post(endpoint,json=body,headers=headers()).status_code==200
        # A later server head cannot silently replace the original retry basis.
        gateway.views[sid]['basis']['head_version']='2'
        gateway.views[sid]['basis']['head_sha256']='f'*64
        assert client.post(endpoint,json=body,headers=headers()).status_code==200
        assert calls[0]==calls[1]
        body['basis']={**body['basis'],'session_id':'another-session'}
        assert client.post(endpoint,json=body,headers=headers()).status_code==409


def test_dynamic_orders_and_multiple_depots_are_projected_from_runtime(setup):
    settings,gateway=setup
    with TestClient(create_app(settings,gateway=gateway)) as client:
        loaded=client.post('/api/scenarios/S0/load',json={'request_id':'load'},headers=headers()).json()['data']
        sid=loaded['session']['session_id'];view=gateway.views[sid]
        view['order_ids'].append('NEW')
        view['unserved'].append({'order_id':'NEW','reason':'QUEUED_NEXT_DAY'})
        view['delivery_day']={'catalog':{'NEW':{'demand_kg':1,'priority':1,'pickup_location_id':'DEPOT-2','delivery_region_id':'r','graph_node_id':456,'coordinates':[106.71,10.81],'service_time_seconds':60,'earliest':view['current_time'],'preferred_due':view['current_time'],'hard_deadline':view['current_time']}},
            'depots':[{'location_id':key,'graph_node_id':123,'coordinates':[106.7,10.8],'opening_time':view['current_time'],'closing_time':view['current_time']} for key in ('DEPOT','DEPOT-2')]}
        orders=client.get(f'/api/sessions/{sid}/orders',headers=headers()).json()['data']['orders']
        assert next(o for o in orders if o['order_id']=='NEW')['pickup_location_id']=='DEPOT-2'
        locations=client.get(f'/api/sessions/{sid}/locations',headers=headers()).json()['data']['locations']
        assert len([x for x in locations if x['kind']=='DEPOT'])==2


def test_execution_evidence_accepts_only_the_typed_day_extension(setup):
    import pytest
    from backend.services.artifact_repository import validate_execution
    settings,gateway=setup
    with TestClient(create_app(settings,gateway=gateway)) as client:
        loaded=client.post('/api/scenarios/S0/load',json={'request_id':'load'},headers=headers()).json()['data']
        sid=loaded['session']['session_id'];view=deepcopy(loaded['execution_view'])
        view.update(delivery_barrier=None,delivery_day={'catalog':{oid:{} for oid in view['order_ids']},'customers':{},'queue':{},'confirmations':{},'complete':False})
        validate_execution(view,sid)
        view['delivery_day']['catalog']['UNKNOWN']={}
        with pytest.raises(ValueError):validate_execution(view,sid)
