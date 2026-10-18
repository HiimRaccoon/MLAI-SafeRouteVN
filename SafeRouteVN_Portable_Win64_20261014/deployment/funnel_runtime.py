"""Loopback Driver gateway and operator-owned accounts for Tailscale Funnel."""
import argparse
import base64
import csv
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import time
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
ACCOUNTS = PROJECT.parent / 'SafeRouteVN_Driver_Accounts/drivers-funnel.csv'
FIELDS = ['driver_id', 'session_id', 'enabled', 'expires_at', 'token', 'login_code']
ORIGIN = re.compile(r'https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.tailbe6594\.ts\.net')

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)

def login_code(origin, token):
    if not ORIGIN.fullmatch(origin) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
        raise ValueError('Invalid Funnel origin or token')
    data=json.dumps({'origin':origin, 'token':token},separators=(',',':')).encode()
    return 'SRVN2.'+base64.urlsafe_b64encode(data).decode().rstrip('=')

def read_accounts(path):
    with path.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if reader.fieldnames != FIELDS: raise ValueError('Invalid Driver CSV columns')
        rows=list(reader)
    actors=set();tokens=set()
    for row in rows:
        if (None in row or any(v is None for v in row.values())
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',row['driver_id'])
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}',row['session_id'])
            or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',row['token'])
            or row['enabled'] not in ('0','1') or not row['expires_at'].isdigit()
            or row['driver_id'] in actors or row['token'] in tokens):
            raise ValueError('Invalid Driver CSV record')
        actors.add(row['driver_id']); tokens.add(row['token'])
    return rows

def sync_accounts(accounts, auth_path):
    rows=read_accounts(accounts)
    auth=json.loads(auth_path.read_bytes())
    existing=[r for r in auth['tokens'] if not (r['role']=='driver' and r['actor_id'].startswith('funnel-'))]
    for row in rows:
        existing.append({'actor_id':'funnel-'+row['driver_id'], 'role':'driver', 'session_id':row['session_id'],
            'expires_at':int(row['expires_at']), 'revoked':row['enabled']!='1',
            'token_sha256':hashlib.sha256(row['token'].encode()).hexdigest()})
    if existing != auth['tokens']:
        auth['tokens']=existing; write_json(auth_path,auth)

def issue(local, origin, driver_id, session_id=None):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', driver_id):
        raise ValueError('Invalid Driver ID')
    access=json.loads((local/'backend/dev_access.json').read_bytes())
    actor=next(r['actor_id'] for r in access['credentials'] if r['role']=='dispatcher')
    with sqlite3.connect(f"{(local/'backend/metadata.sqlite').as_uri()}?mode=ro",uri=True) as db:
        sessions=[r[0] for r in db.execute("SELECT session_id FROM sessions WHERE status='READY' AND owner_actor_id=? ORDER BY rowid DESC",(actor,))]
    if not sessions: raise ValueError('Open a scenario in Admin before issuing a Driver code')
    session_id=session_id or sessions[0]
    if session_id not in sessions: raise ValueError('Session is not READY or is not owned by this admin')
    rows=read_accounts(ACCOUNTS) if ACCOUNTS.exists() else []
    row=next((r for r in rows if r['driver_id']==driver_id),None)
    if row is None:
        row={'driver_id':driver_id,'token':secrets.token_urlsafe(36)}; rows.append(row)
    row.update(session_id=session_id,enabled='1',expires_at=str(int(time.time())+30*86400),
               login_code=login_code(origin,row['token']))
    ACCOUNTS.parent.mkdir(parents=True,exist_ok=True)
    temp=ACCOUNTS.with_name(ACCOUNTS.name+'.'+uuid4().hex+'.tmp')
    with temp.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows)
    temp.replace(ACCOUNTS)
    # The running supervisor is the sole writer of the authentication store.
    print(json.dumps({'driver_id':driver_id,'session_id':session_id,'accounts_file':str(ACCOUNTS)}))

def serve(local, backend, stop_file=None):
    config=json.loads((PROJECT/'deployment/funnel.json').read_bytes())
    if not ORIGIN.fullmatch(config['public_origin']): raise ValueError('Invalid Funnel origin')
    if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}',backend): raise ValueError('Invalid local backend')
    config.update(backend=backend,dist=str(PROJECT/'frontend/dist-driver'),port=8788)
    runtime=local/'backend/funnel-gateway.json';write_json(runtime,config)
    child=None
    failed=False
    try:
        while True:
            if stop_file is not None and stop_file.exists(): return
            try:
                if ACCOUNTS.exists(): sync_accounts(ACCOUNTS,local/'backend/auth_tokens.json')
                else:
                    auth_path=local/'backend/auth_tokens.json'
                    auth=json.loads(auth_path.read_bytes())
                    rows=[r for r in auth['tokens'] if not (r['role']=='driver' and r['actor_id'].startswith('funnel-'))]
                    if rows != auth['tokens']:
                        auth['tokens']=rows;write_json(auth_path,auth)
                if child is None or child.poll() is not None:
                    child=subprocess.Popen([str(PROJECT/'portable/node/node.exe'),str(PROJECT/'deployment/driver_gateway.mjs'),str(runtime)],
                        cwd=PROJECT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if failed: print('DRIVER_GATEWAY_RECOVERED',flush=True)
                failed=False
            except (OSError,ValueError,KeyError,TypeError):
                # Fail closed for remote access; keep Admin/worker running while
                # the operator fixes or finishes saving the external CSV.
                if child is not None and child.poll() is None:
                    child.terminate();child.wait(timeout=10)
                child=None
                if not failed: print('DRIVER_GATEWAY_PAUSED: check accounts CSV or local auth file',flush=True)
                failed=True
            time.sleep(2)
    finally:
        if child is not None and child.poll() is None: child.terminate();child.wait(timeout=10)

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['serve','issue'])
    parser.add_argument('--local-root',type=Path,required=True)
    parser.add_argument('--backend')
    parser.add_argument('--origin')
    parser.add_argument('--driver-id',default='TX01')
    parser.add_argument('--session')
    parser.add_argument('--stop-file',type=Path)
    args=parser.parse_args()
    if args.command=='serve': serve(args.local_root,args.backend,args.stop_file)
    else: issue(args.local_root,args.origin,args.driver_id,args.session)
