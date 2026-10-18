"""Concrete M3 reference persistence/transport; not backend implementation.

Runtime retains sole physical authority. M3 records accepted request digests
and responses, deduplicates outbox notifications, and passes identifiers to SDK.
Client payload cannot select snapshot/store/install paths or manufacture state.
"""
import sqlite3,json
from pathlib import Path
from .protocol import sha,canonical,require

class ReferenceTransport:
    def __init__(self,client,persistence_path):
        self.client=client;self.path=Path(persistence_path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.executescript('CREATE TABLE IF NOT EXISTS requests(session TEXT,key TEXT,digest TEXT,response TEXT,PRIMARY KEY(session,key)); CREATE TABLE IF NOT EXISTS notifications(event_id TEXT PRIMARY KEY,body TEXT NOT NULL);')
    def command(self,operation,command_id,session_id=None,**fields):
        namespace=session_id or 'server-capabilities';digest=sha({'operation':operation,'session_id':session_id,'fields':fields})
        with sqlite3.connect(self.path) as db:row=db.execute('SELECT digest,response FROM requests WHERE session=? AND key=?',(namespace,command_id)).fetchone()
        if row:
            require(row[0]==digest,'IDEMPOTENCY_CONFLICT','command_id','M3 persisted accepted request differs')
            return json.loads(row[1])
        response=self.client.command(operation,command_id,session_id,**fields)
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT OR IGNORE INTO requests VALUES(?,?,?,?)',(namespace,command_id,digest,canonical(response).decode()))
            saved=db.execute('SELECT digest,response FROM requests WHERE session=? AND key=?',(namespace,command_id)).fetchone()
            require(saved[0]==digest,'IDEMPOTENCY_CONFLICT','command_id','concurrent M3 request conflict')
        return json.loads(saved[1])
    def poll_outbox(self,session_id):
        rows=self.client.read_notifications(session_id)
        for row in rows:
            with sqlite3.connect(self.path) as db:
                db.execute('INSERT OR IGNORE INTO notifications VALUES(?,?)',(row['event_id'],canonical(row).decode()))
                existing=db.execute('SELECT body FROM notifications WHERE event_id=?',(row['event_id'],)).fetchone()
                require(json.loads(existing[0])==row,'OUTBOX_CONFLICT','event_id','same notification ID changed')
            self.client.acknowledge(session_id,'ack-'+row['event_id'],row['event_id'])
        return rows
