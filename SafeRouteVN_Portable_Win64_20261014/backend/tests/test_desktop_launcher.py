from pathlib import Path
import pytest
from backend.scripts.launch_local import choose_port, instance_lock
from backend.scripts.local_backend_control import installation_digest, save_control, stop_verified_backend, stop_local_services
import json


def test_port_selection_skips_occupied_ports_without_terminating_them():
    assert choose_port(5174,lambda p:p in (5174,5175))==5176
    with pytest.raises(RuntimeError):choose_port(5174,lambda p:True)


def test_instance_lock_is_released_on_exit(tmp_path):
    path=tmp_path/'launcher.lock'
    with instance_lock(path) as first:
        assert first
        with instance_lock(path) as second:assert not second
    with instance_lock(path) as next_run:assert next_run


def test_degraded_backend_restart_waits_for_owned_shutdown(tmp_path):
    (tmp_path/'backend').mkdir()
    (tmp_path/'installation.json').write_text('{}')
    stop = tmp_path/'receipts/launch_test/stop.request'
    stop.parent.mkdir(parents=True)
    value = {'run_id':'test', 'installation_sha256':installation_digest(tmp_path),
             'port':8000, 'stop_file':str(stop), 'status':'READY'}
    save_control(tmp_path, value)
    def finish(_):
        assert stop.is_file()
        value['status'] = 'STOPPED'
        save_control(tmp_path, value)
    stop_verified_backend(tmp_path, 8000, lambda _:False, sleep=finish)
    assert stop.is_file()


@pytest.mark.parametrize('field,bad', [('port',8001), ('installation_sha256','other'),
                                    ('stop_file','C:/unrelated/stop.request')])
def test_restart_rejects_unowned_backend_without_signalling(tmp_path, field, bad):
    (tmp_path/'backend').mkdir()
    (tmp_path/'installation.json').write_text('{}')
    stop = tmp_path/'receipts/launch_test/stop.request'
    stop.parent.mkdir(parents=True)
    value = {'run_id':'test', 'installation_sha256':installation_digest(tmp_path),
             'port':8000, 'stop_file':str(stop), 'status':'READY'}
    value[field] = bad
    save_control(tmp_path, value)
    with pytest.raises(RuntimeError, match='matching private shutdown record'):
        stop_verified_backend(tmp_path, 8000, lambda _:True)
    assert not stop.exists()


def test_local_selection_uses_verified_pointer_and_explicit_override(tmp_path):
    from backend.scripts.local_backend_control import selected_installation
    project=tmp_path/'project';project.mkdir()
    local=tmp_path/'.local/new';local.mkdir(parents=True)
    (local/'installation.json').write_text(json.dumps({'expected_build_sha256':'a'*64}))
    pointer={'schema_version':'saferoute-local-selection/1','project_root':str(project),
             'local_root':str(local),'build_sha256':'a'*64}
    path=tmp_path/'.local/saferoute-active.json';path.write_text(json.dumps(pointer))
    assert selected_installation(project)==local
    pointer['build_sha256']='b'*64;path.write_text(json.dumps(pointer))
    with pytest.raises(RuntimeError):selected_installation(project)
    assert selected_installation(project,local)==local


def test_cleanup_stops_launcher_and_waits_for_frontend_release(tmp_path):
    (tmp_path/'backend').mkdir(); (tmp_path/'installation.json').write_text('{}')
    logs=tmp_path/'receipts/desktop_test'; logs.mkdir(parents=True)
    stop=logs/'stop.request'; state_path=tmp_path/'backend/launcher_status.json'
    state={'status':'READY','logs':str(logs),'admin_url':'http://127.0.0.1:5174/admin',
           'installation_sha256':installation_digest(tmp_path),'stop_file':str(stop)}
    state_path.write_text(json.dumps(state))
    busy={5174}
    def finish(_):
        assert stop.exists()
        state['status']='STOPPED'; state_path.write_text(json.dumps(state)); busy.clear()
    stop_local_services(tmp_path,lambda p:p in busy,sleep=finish)
    assert not busy


def test_cleanup_rejects_modified_launcher_binding(tmp_path):
    (tmp_path/'backend').mkdir(); (tmp_path/'installation.json').write_text('{}')
    logs=tmp_path/'receipts/desktop_test'; logs.mkdir(parents=True)
    stop=logs/'stop.request'
    (tmp_path/'backend/launcher_status.json').write_text(json.dumps({
        'status':'READY','logs':str(logs),'admin_url':'http://127.0.0.1:5174/admin',
        'installation_sha256':'wrong','stop_file':str(stop)}))
    with pytest.raises(RuntimeError,match='shutdown binding differs'):
        stop_local_services(tmp_path,lambda _:True)
    assert not stop.exists()


def test_cleanup_legacy_launcher_uses_backend_stop_record(tmp_path):
    (tmp_path/'backend').mkdir(); (tmp_path/'installation.json').write_text('{}')
    logs=tmp_path/'receipts/desktop_old'; logs.mkdir(parents=True)
    state_path=tmp_path/'backend/launcher_status.json'
    state={'status':'READY','logs':str(logs),'admin_url':'http://127.0.0.1:5174/admin'}
    state_path.write_text(json.dumps(state))
    stop=tmp_path/'receipts/launch_old/stop.request'; stop.parent.mkdir()
    control={'run_id':'old','status':'READY','port':8000,'stop_file':str(stop),
             'installation_sha256':installation_digest(tmp_path)}
    save_control(tmp_path,control); busy={8000,5174}
    def finish(_):
        assert stop.exists()
        control['status']='STOPPED'; save_control(tmp_path,control)
        state['status']='FAILED'; state_path.write_text(json.dumps(state)); busy.clear()
    stop_local_services(tmp_path,lambda p:p in busy,sleep=finish)
    assert not busy
