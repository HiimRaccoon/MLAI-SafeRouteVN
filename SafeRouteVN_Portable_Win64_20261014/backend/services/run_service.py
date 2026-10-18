"""Durable run creation jobs; authenticated owner and immutable source binding."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import sqlite3
from uuid import uuid4

from backend.api.errors import ApiError
from optimization.runtime.run_source import read_manifest, artifact

# Local product setting: retain the implementation, disable live weather fetch.
LIVE_WEATHER_ENABLED = False


class RunService:
    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions
        self.tasks = set()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.settings.metadata_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def initialize(self):
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS run_builds(run_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL, epoch TEXT NOT NULL, installation TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL, manifest_sha256 TEXT, UNIQUE(actor_id,request_id))')
            db.execute("UPDATE run_builds SET status='INTERRUPTED', message='Server stopped during creation; create a new run.' WHERE status='BUILDING'")

    def row(self, run_id, actor_id=None):
        with self.db() as db:
            row = db.execute('SELECT * FROM run_builds WHERE run_id=?',(run_id,)).fetchone()
        if row is None: raise ApiError(404,'RUN_NOT_FOUND','run_id','Run not found')
        if actor_id is not None and row['actor_id'] != actor_id: raise ApiError(403,'FORBIDDEN','run_id','Run owner required')
        if row['installation'] != self.sessions.installation_identity(): raise ApiError(409,'INSTALLATION_BINDING_CHANGED','run_id','Run belongs to another build')
        return dict(row)

    def manifest(self, run_id, actor_id=None):
        row=self.row(run_id,actor_id)
        if row['status'] != 'READY': raise ApiError(409,'RUN_NOT_READY','run_id',row['message'])
        try: return read_manifest(self.settings.project_root,run_id,row['manifest_sha256'])
        except (ValueError,OSError,KeyError) as error: raise ApiError(503,'SOURCE_CHANGED','run_id','Registered run source changed') from error

    def fixture(self,row):
        manifest=self.manifest(row['run_id'])
        if row['catalog_sha256'] != self.row(row['run_id'])['manifest_sha256']:
            raise ApiError(503,'SOURCE_CHANGED','run_id','Session run binding changed')
        try:
            fixture=json.loads(artifact(self.settings.project_root,manifest,'fixture').read_bytes())
            if manifest['artifacts']['fixture']['sha256'] != row['fixture_sha256']: raise ValueError('Fixture digest')
            return fixture
        except (ValueError,OSError,KeyError) as error: raise ApiError(503,'SOURCE_CHANGED','run_id','Run fixture changed') from error

    def public(self,run_id,actor_id):
        row=self.row(run_id,actor_id)
        data={key:row[key] for key in ('run_id','status','message','epoch','manifest_sha256')}
        data['scenario_id']=json.loads(row['body'])['scenario_id']
        source = json.loads(row['body'])
        data['source_kind'] = source.get('source_kind', 'WEATHER_RUN')
        data['name'] = source.get('name', data['scenario_id'])
        data['online_weather_enabled']=LIVE_WEATHER_ENABLED and os.getenv('SAFEROUTE_OFFLINE_MODE') != 'loopback-only'
        if row['status']=='READY':
            manifest=self.manifest(run_id,actor_id)
            data.update(weather=manifest['weather'],traffic_source=manifest['traffic_source'],temporal_model=manifest['temporal_model'],
                fixture_sha256=manifest['artifacts']['fixture']['sha256'])
        return data

    async def create(self,body,actor):
        if not LIVE_WEATHER_ENABLED:
            raise ApiError(409, 'WEATHER_DISABLED', 'weather', 'Chức năng lấy thời tiết thời gian thực đã tắt.')
        self.sessions.catalog.fixture(body.scenario_id)
        payload=body.model_dump(exclude={'request_id'}); fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM run_builds WHERE actor_id=? AND request_id=?',(actor.actor_id,body.request_id)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint: raise ApiError(409,'IDEMPOTENCY_CONFLICT','request_id','Request content changed')
                run_id=old['run_id']; fresh=False
            else:
                if os.getenv('SAFEROUTE_OFFLINE_MODE') == 'loopback-only':
                    raise ApiError(409,'OFFLINE_MODE','weather','Start the launcher without --offline to fetch weather.')
                if db.execute("SELECT 1 FROM run_builds WHERE status='BUILDING'").fetchone():
                    raise ApiError(409,'RUN_BUILD_BUSY','run','Another weather run is being built')
                run_id='run-'+uuid4().hex; fresh=True
                epoch=datetime.now(timezone(timedelta(hours=7))).replace(microsecond=0).isoformat()
                db.execute('INSERT INTO run_builds VALUES(?,?,?,?,?,?,?,?,?,?)',(run_id,actor.actor_id,body.request_id,fingerprint,json.dumps(payload),epoch,
                    self.sessions.installation_identity(),'BUILDING','Đang chuẩn bị phiên mới...',None))
        if fresh:
            task=asyncio.create_task(asyncio.to_thread(self.build,run_id)); self.tasks.add(task); task.add_done_callback(self.tasks.discard)
        return self.public(run_id,actor.actor_id)

    def build(self,run_id):
        from optimization.runtime.run_builder import build
        row=self.row(run_id); body=json.loads(row['body'])
        def progress(message):
            with self.db() as db: db.execute('UPDATE run_builds SET message=? WHERE run_id=?',(message,run_id))
        try:
            digest=build(self.settings.project_root,run_id,body['scenario_id'],row['epoch'],body['event_after_minutes'],progress=progress)
            with self.db() as db: db.execute("UPDATE run_builds SET status='READY',message='Sẵn sàng tạo session.',manifest_sha256=? WHERE run_id=?",(digest,run_id))
        except Exception as error:
            # The failed directory is retained for diagnosis; it is never offered as READY.
            with self.db() as db: db.execute("UPDATE run_builds SET status='FAILED',message=? WHERE run_id=?",(type(error).__name__+': '+str(error)[:350],run_id))
