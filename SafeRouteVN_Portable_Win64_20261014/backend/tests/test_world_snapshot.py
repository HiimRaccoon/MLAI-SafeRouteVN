from copy import deepcopy

from backend.tests.test_sessions import client, setup, headers, load


def test_world_has_one_sdk_read_and_coherent_projections(client, setup):
    _, gateway = setup
    sid = load(client).json()['data']['session']['session_id']
    before = deepcopy(gateway.views[sid])
    gateway.reads.clear()
    result = client.get(f'/api/sessions/{sid}/world', headers=headers())
    assert result.status_code == 200, result.text
    world = result.json()['data']
    assert gateway.reads == [sid]
    assert world['state'] == before
    for kind in ('orders', 'vehicles', 'locations', 'events'):
        assert world[kind]['basis'] == before['basis']
    assert gateway.views[sid] == before


def test_world_requires_auth_and_session_owner(client):
    sid = load(client).json()['data']['session']['session_id']
    path = f'/api/sessions/{sid}/world'
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers('bob')).status_code == 403
