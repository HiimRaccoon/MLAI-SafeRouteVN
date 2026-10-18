import csv
import hashlib
import json
import time
from pathlib import Path

import pytest
from deployment.funnel_runtime import sync_accounts, login_code, FIELDS

def test_csv_sync_preserves_admin_and_revokes_removed_driver(tmp_path):
    auth = tmp_path / 'auth.json'
    admin = {'actor_id': 'admin', 'role': 'dispatcher', 'token_sha256': 'a'*64}
    auth.write_text(json.dumps({'schema_version':'saferoute-m3-auth/1','tokens':[admin]}))
    account = tmp_path / 'drivers.csv'
    row = dict(driver_id='TX01', session_id='session-one', enabled='1', expires_at=str(int(time.time())+600),
               token='x'*48, login_code=login_code('https://server.tailbe6594.ts.net','x'*48))
    with account.open('w', newline='', encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f, fieldnames=FIELDS); writer.writeheader(); writer.writerow(row)
    sync_accounts(account, auth)
    rows=json.loads(auth.read_text())['tokens']
    assert rows[0] == admin
    assert rows[1]['token_sha256'] == hashlib.sha256(('x'*48).encode()).hexdigest()
    assert rows[1]['session_id'] == 'session-one'
    with account.open('w', newline='', encoding='utf-8-sig') as f:
        csv.DictWriter(f, fieldnames=FIELDS).writeheader()
    sync_accounts(account, auth)
    assert json.loads(auth.read_text())['tokens'] == [admin]

def test_grant_rejects_non_tailnet_origin():
    with pytest.raises(ValueError): login_code('https://evil.example','x'*48)

def test_invalid_csv_pauses_gateway_recovers_and_stops_promptly(tmp_path, monkeypatch):
    import threading
    from deployment import funnel_runtime as runtime
    project=tmp_path/'project';local=tmp_path/'local'
    (project/'deployment').mkdir(parents=True)
    (local/'backend').mkdir(parents=True)
    (project/'deployment/funnel.json').write_text(json.dumps({'public_origin':'https://server.tailbe6594.ts.net'}))
    (project/'deployment/driver_gateway.mjs').write_text('setInterval(()=>{},1000);')
    (local/'backend/auth_tokens.json').write_text(json.dumps({'schema_version':'saferoute-m3-auth/1','tokens':[
        {'actor_id':'admin','role':'dispatcher','token_sha256':'a'*64}]}))
    accounts=tmp_path/'drivers.csv'
    accounts.write_text(','.join(FIELDS)+'\n')
    real_popen=runtime.subprocess.Popen
    node=runtime.PROJECT/'portable/node/node.exe'
    children=[]
    def spawn(command, **kwargs):
        child=real_popen([str(node),*command[1:]],**kwargs);children.append(child);return child
    monkeypatch.setattr(runtime,'PROJECT',project)
    monkeypatch.setattr(runtime,'ACCOUNTS',accounts)
    monkeypatch.setattr(runtime.subprocess,'Popen',spawn)
    stop=tmp_path/'stop'
    errors=[]
    def run():
        try: runtime.serve(local,'http://127.0.0.1:8000',stop)
        except Exception as error: errors.append(error)
    thread=threading.Thread(target=run)
    def wait_for(predicate):
        deadline=time.monotonic()+8
        while not predicate() and time.monotonic()<deadline: time.sleep(.05)
        assert predicate()
    thread.start()
    try:
        wait_for(lambda: len(children)==1)
        accounts.write_text('broken csv\n')
        wait_for(lambda: children[0].poll() is not None)
        assert thread.is_alive()
        accounts.write_text(','.join(FIELDS)+'\n')
        wait_for(lambda: len(children)==2)
    finally:
        start=time.monotonic();stop.touch();thread.join(6)
    assert not thread.is_alive() and not errors
    assert time.monotonic()-start<6
    assert all(child.poll() is not None for child in children)
