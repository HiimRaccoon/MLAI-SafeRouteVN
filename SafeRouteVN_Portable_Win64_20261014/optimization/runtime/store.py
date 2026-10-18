"""Durable local M2 authority. Internal methods are not transport entrypoints.

Change Impact Analysis: new store /1, isolated from every historical authority.
OS access to this store is a trust prerequisite; hashes detect corruption, not
hostile administrators. No search or physical validation occurs in a DB tx.
"""
from contextlib import contextmanager
from pathlib import Path
from collections import OrderedDict
from threading import Lock
import hashlib,json,sqlite3,time,uuid,zlib
from .protocol import require,RuntimeError,identifier,digest,basis,canonical,sha,copy,int64,decode

VERSION='task02-m2-runtime-store/2'
PAYLOAD_MAGIC=b'SRVZ1\x00'
_VERIFIED_PAYLOADS=OrderedDict()
_PAYLOAD_LOCK=Lock()
_STRICT_PAYLOADS=OrderedDict()
_STRICT_HASHES=OrderedDict()
_STRICT_PAYLOAD_BYTES=0
_STRICT_PAYLOAD_LIMIT=32*1024*1024
_JOURNAL_HASHES=OrderedDict()

def strict_payload(raw,path):
    """Reuse strict-decoding evidence only for the same actual stored bytes.

    Cache immutable UTF-8, never mutable objects or database validity. Each
    consumer receives a fresh JSON tree. Changed bytes, including malformed
    replacements, must pass the strict decoder again. Bound memory per process.
    """
    global _STRICT_PAYLOAD_BYTES
    require(isinstance(raw,(str,bytes,bytearray)),'STORE_INVALID',path,'persisted bytes/text required')
    packed=raw.encode('utf8') if isinstance(raw,str) else bytes(raw)
    key=(isinstance(raw,str),hashlib.sha256(packed).digest())
    with _PAYLOAD_LOCK:
        encoded=_STRICT_PAYLOADS.get(key)
        if encoded is not None:_STRICT_PAYLOADS.move_to_end(key)
        checked=key in _STRICT_HASHES
        if checked:_STRICT_HASHES.move_to_end(key)
    if encoded is not None:return json.loads(encoded)
    encoded=payload_bytes(raw,path)
    if checked:return json.loads(encoded)
    value=decode(encoded)
    with _PAYLOAD_LOCK:
        _STRICT_HASHES[key]=True
        if len(_STRICT_HASHES)>8192:_STRICT_HASHES.popitem(last=False)
    if len(encoded)<=_STRICT_PAYLOAD_LIMIT:
        with _PAYLOAD_LOCK:
            if key not in _STRICT_PAYLOADS:
                _STRICT_PAYLOADS[key]=encoded;_STRICT_PAYLOAD_BYTES+=len(encoded)
                while _STRICT_PAYLOAD_BYTES>_STRICT_PAYLOAD_LIMIT or len(_STRICT_PAYLOADS)>8192:
                    _,removed=_STRICT_PAYLOADS.popitem(last=False);_STRICT_PAYLOAD_BYTES-=len(removed)
    return value

def verified_journal_body_hash(row,body,parent):
    # Authenticate every row's actual bytes and chain fields on every scan;
    # only repeated canonicalization of already verified content is avoided.
    if row['parent']!=parent:return None
    raw=row['body'];packed=raw.encode('utf8') if isinstance(raw,str) else bytes(raw)
    key=(row['session'],row['kind'],parent,row['digest'],isinstance(raw,str),hashlib.sha256(packed).digest())
    with _PAYLOAD_LOCK:
        if key in _JOURNAL_HASHES:
            _JOURNAL_HASHES.move_to_end(key);return _JOURNAL_HASHES[key]
    if row['digest']!=sha({'session_id':row['session'],'kind':row['kind'],'body':body,'parent':parent}):return None
    body_hash=sha(body)
    with _PAYLOAD_LOCK:
        _JOURNAL_HASHES[key]=body_hash
        if len(_JOURNAL_HASHES)>4096:_JOURNAL_HASHES.popitem(last=False)
    return body_hash

def payload_bytes(raw,path):
    if isinstance(raw,str):return raw.encode('utf8')
    require(isinstance(raw,(bytes,bytearray)),'STORE_INVALID',path,'persisted bytes/text required')
    raw=bytes(raw)
    if raw.startswith(PAYLOAD_MAGIC):
        try:
            inflater=zlib.decompressobj();value=inflater.decompress(raw[len(PAYLOAD_MAGIC):],256*1024*1024+1)
            require(len(value)<=256*1024*1024 and inflater.eof and not inflater.unused_data and not inflater.unconsumed_tail,
                    'STORE_INVALID',path,'invalid or oversized compressed payload')
            return value
        except zlib.error as error:raise RuntimeError('STORE_INVALID',path,'invalid compressed payload') from error
    return raw

def encode_payload(raw):
    # Journal digests continue to commit to canonical, uncompressed UTF-8.
    # Existing TEXT rows remain readable; compression is entirely internal.
    return PAYLOAD_MAGIC+zlib.compress(raw,level=1) if len(raw)>=65536 else raw.decode('utf8')

def sqlite_failure_code(error):
    number=getattr(error,'sqlite_errorcode',None)
    return 'RUNTIME_BUSY' if isinstance(error,sqlite3.OperationalError) and type(number) is int and (number & 255) in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED) else 'STORE_INVALID'

def committed_hash(raw,expected,path):
    """Fast path for canonical bytes committed by the trusted writer.

    The causal journal authenticates the original canonical payload. A byte
    mismatch still passes through strict decoding for legacy noncanonical JSON;
    malformed data never gets repaired. Consumers always decode typed objects.
    """
    require(isinstance(raw,(str,bytes,bytearray)),'STORE_INVALID',path,'persisted bytes/text required')
    packed=raw.encode('utf8') if isinstance(raw,str) else bytes(raw)
    # Authenticate the actual stored bytes on every read. Cache only successful
    # decompression/digest checks, never decoded mutable objects or DB validity.
    key=(isinstance(raw,str),hashlib.sha256(packed).digest(),expected)
    with _PAYLOAD_LOCK:
        if key in _VERIFIED_PAYLOADS:
            _VERIFIED_PAYLOADS.move_to_end(key);return True
    encoded=payload_bytes(raw,path)
    valid=hashlib.sha256(encoded).hexdigest()==expected or sha(persisted(raw,path))==expected
    if valid:
        with _PAYLOAD_LOCK:
            _VERIFIED_PAYLOADS[key]=True
            if len(_VERIFIED_PAYLOADS)>512:_VERIFIED_PAYLOADS.popitem(last=False)
    return valid

def persisted(raw,path):
    """Strict persisted-object boundary; never repairs or relabels bad bytes.

    Change Impact Analysis: Store/2 valid wire unchanged. Malformed JSON,
    duplicate keys, unsafe numeric values and non-object roots now have typed
    diagnostics. Rollback/cleanup remains separate from data classification.
    """
    require(isinstance(raw,(str,bytes,bytearray)),'STORE_INVALID',path,'persisted JSON bytes/text required')
    try:value=strict_payload(raw,path)
    except RuntimeError as e:
        raise RuntimeError('STORE_INVALID',path,'invalid persisted JSON: '+str(e)) from e
    require(isinstance(value,dict),'STORE_INVALID',path,'persisted object required')
    if path=='jobs.request':
        require(set(value)=={'operation','basis','profile','budget_seconds'} and value['operation']=='submit','STORE_INVALID',path,'typed persisted submit required')
        require(isinstance(value['profile'],str) and value['profile'] in ('FASTEST','BALANCED','SAFER'),'STORE_INVALID',path+'.profile','known profile required')
        from .protocol import number
        number(value['budget_seconds'],path+'.budget_seconds',positive=True);require(value['budget_seconds']<=600,'STORE_INVALID',path+'.budget_seconds','bounded budget required');basis(value['basis'])
    return value

class Store:
    def __init__(self,path,build_sha256):
        digest(build_sha256,'build_sha256');self.path=Path(path);self.build=build_sha256;self._ready=False
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.transaction() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,root TEXT NOT NULL,head TEXT NOT NULL,version INTEGER NOT NULL,generation INTEGER NOT NULL,active_job TEXT);
              CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,session TEXT NOT NULL,request TEXT NOT NULL,basis TEXT NOT NULL,status TEXT NOT NULL,lease TEXT,expires REAL,result TEXT,validation TEXT,failure TEXT);
              CREATE TABLE IF NOT EXISTS receipts(session TEXT NOT NULL,key TEXT NOT NULL,digest TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(session,key));
              CREATE TABLE IF NOT EXISTS journal(seq INTEGER PRIMARY KEY,session TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,parent TEXT NOT NULL,digest TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,body TEXT NOT NULL,ack INTEGER NOT NULL DEFAULT 0);
            ''')
            row=db.execute('SELECT value FROM metadata WHERE key=?',('schema_version',)).fetchone()
            if row is None:db.execute('INSERT INTO metadata VALUES(?,?)',('schema_version',VERSION))
            else:require(row[0]==VERSION,'STORE_VERSION','store.schema_version','unsupported store; migration must be explicit')
            row=db.execute('SELECT value FROM metadata WHERE key=?',('build_sha256',)).fetchone()
            if row is None:db.execute('INSERT INTO metadata VALUES(?,?)',('build_sha256',self.build))
            else:require(row[0]==self.build,'BUILD_UPGRADE_REQUIRED','store.build_sha256','cross-build open requires explicit validated upgrade')
        self._ready=True
        self.verify()

    @contextmanager
    def transaction(self,*,read=False):
        db=None
        try:
            db=sqlite3.connect(self.path,timeout=getattr(self,'busy_timeout_seconds',10),isolation_level=None)
            db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA synchronous=FULL')
            if read:db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN' if read else 'BEGIN IMMEDIATE')
            if self._ready:
                row=db.execute('SELECT value FROM metadata WHERE key=?',('build_sha256',)).fetchone()
                require(row is not None and row[0]==self.build,'BUILD_FENCED','store.build_sha256','old worker/connection belongs to a former install')
            yield db;db.commit()
        except sqlite3.DatabaseError as e:
            if db is not None:db.rollback()
            raise RuntimeError(sqlite_failure_code(e),'store',str(e)) from e
        except BaseException:
            # Cleanup only; never converts programming errors into data errors.
            if db is not None:db.rollback()
            raise
        finally:
            if db is not None:db.close()

    def _row(self,db,session):
        identifier(session,'session_id');r=db.execute('SELECT * FROM sessions WHERE id=?',(session,)).fetchone()
        require(r is not None,'SESSION_NOT_FOUND','session_id','unknown authority session');return r
    def _basis(self,row):
        root=persisted(row['root'],'sessions.root');head=persisted(row['head'],'sessions.head')
        require(isinstance(root,dict) and isinstance(root.get('source_hashes'),dict),'STORE_INVALID','sessions.root','typed source root required')
        require(isinstance(head,dict),'STORE_INVALID','sessions.head','typed physical head required')
        identifier(head.get('context_version'),'sessions.head.context_version')
        return {'session_id':row['id'],'root_sha256':sha(root),'head_sha256':sha(head),'head_version':str(row['version']),
                'generation':str(row['generation']),'source_sha256':sha(root['source_hashes']),
                'context_version':head['context_version'],'overlay_sha256':head.get('overlay_sha256'),'build_sha256':self.build}
    def _cas(self,row,expected):
        basis(expected);require(self._basis(row)==expected,'STALE_HEAD','basis','head/version/generation/source/context/build changed')
    def _receipt(self,db,session,key,payload):
        identifier(key,'command_id');r=db.execute('SELECT digest,body FROM receipts WHERE session=? AND key=?',(session,key)).fetchone()
        if r:
            events=[persisted(x[0],'journal.body') for x in db.execute('SELECT body FROM journal WHERE session=? AND kind=?',(session,'RECEIPT'))]
            event=next((x for x in events if x['key']==key),None)
            require(event is not None and event['request_sha256']==r['digest'] and event['outcome']==persisted(r['body'],'persisted.body'),'RECEIPT_CORRUPT','receipts','retry row differs from committed outcome')
            require(r['digest']==sha(payload),'IDEMPOTENCY_CONFLICT','command_id','same key with different operation or payload');return persisted(r['body'],'persisted.body')
    def receipt(self,session,key,payload):
        """Read persisted retry BEFORE checking an input basis that may be old."""
        with self.transaction(read=True) as db:
            self._row(db,session)
            return self._receipt(db,session,key,payload)
    def _log(self,db,session,kind,body):
        row=db.execute('SELECT digest FROM journal ORDER BY seq DESC LIMIT 1').fetchone();parent=row[0] if row else '0'*64
        value={'session_id':session,'kind':kind,'body':body,'parent':parent};hashed=sha(value)
        db.execute('INSERT INTO journal(session,kind,body,parent,digest) VALUES(?,?,?,?,?)',(session,kind,canonical(body).decode(),parent,hashed))
        out={'event_id':hashed,'session_id':session,'kind':kind,'body_sha256':sha(body)}
        if kind!='OUTBOX_ACK' and body.get('notification',True):db.execute('INSERT INTO outbox(id,body) VALUES(?,?)',(hashed,canonical(out).decode()))
        return hashed
    def _save_receipt(self,db,session,key,payload,body):
        db.execute('INSERT INTO receipts VALUES(?,?,?,?)',(session,key,sha(payload),canonical(body).decode()))
        self._log(db,session,'RECEIPT',{'key':key,'request_sha256':sha(payload),'outcome':body})
        return body

    def _bootstrap(self,session,root,head,key):
        """Called only after facade's raw source check, not arbitrary transport data."""
        identifier(session,'session_id');payload={'operation':'bootstrap','root':root,'head':head}
        with self.transaction() as db:
            prior=self._receipt(db,session,key,payload)
            if prior:return prior
            require(db.execute('SELECT 1 FROM sessions WHERE id=?',(session,)).fetchone() is None,'SESSION_EXISTS','session_id','authority already exists')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,NULL)',(session,canonical(root).decode(),canonical(head).decode(),1,0))
            self._log(db,session,'BOOTSTRAP',{'root':root,'head':head,'version':'1','generation':'0','active_job':None})
            result={'status':'BOOTSTRAPPED','basis':self._basis(self._row(db,session))}
            return self._save_receipt(db,session,key,payload,result)

    def head(self,session):
        with self.transaction(read=True) as db:
            r=self._row(db,session);return {'basis':self._basis(r),'root':persisted(r['root'],'sessions.root'),'head':persisted(r['head'],'sessions.head'),'active_job':r['active_job']}

    def submit(self,session,key,expected,profile,budget):
        payload={'operation':'submit','basis':expected,'profile':profile,'budget_seconds':budget}
        with self.transaction() as db:
            row=self._row(db,session);prior=self._receipt(db,session,key,payload)
            if prior:return prior
            self._cas(row,expected)
            job='job-'+uuid.uuid4().hex
            db.execute('INSERT INTO jobs(id,session,request,basis,status) VALUES(?,?,?,?,?)',(job,session,canonical(payload).decode(),canonical(expected).decode(),'QUEUED'))
            self._log(db,session,'SUBMIT',{'job_id':job,'request':payload,'basis':expected})
            return self._save_receipt(db,session,key,payload,{'status':'QUEUED','job_id':job,'input_basis':expected})

    def job(self,session,job):
        identifier(job,'job_id')
        with self.transaction(read=True) as db:
            self._row(db,session);r=db.execute('SELECT * FROM jobs WHERE id=? AND session=?',(job,session)).fetchone()
            require(r is not None,'JOB_NOT_FOUND','job_id','unknown job in this session')
            return {k:persisted(r[k],'jobs.'+k) if k in ('request','basis','result','validation','failure') and r[k] is not None else r[k] for k in r.keys()}

    def claim(self,session,job):
        return self._claim_batch(session,[job])[job]

    def _claim_batch(self,session,job_ids):
        """Verify once and acquire the whole bounded batch in one transaction.

        Leases start after the history check, so an expanding audit history
        cannot consume the first profile's search time while claiming the rest.
        Every CLAIM still has its own lease and causal journal entry.
        """
        require(isinstance(job_ids,list) and 1<=len(job_ids)<=3 and len(set(job_ids))==len(job_ids),
                'INVALID_DATA','job_ids','unique bounded jobs required')
        with self.transaction() as db:
            self._verify(db)
            row=self._row(db,session);records=[]
            for job in job_ids:
                r=db.execute('SELECT * FROM jobs WHERE id=? AND session=?',(job,session)).fetchone()
                require(r is not None,'JOB_NOT_FOUND','job_id','unknown job')
                require(r['status']=='QUEUED','JOB_LIFECYCLE','job_id','only queued work may be claimed')
                self._cas(row,persisted(r['basis'],'jobs.basis'));records.append(r)
            started=time.time();leases={}
            for r in records:
                job=r['id'];lease=uuid.uuid4().hex
                expiry=started+persisted(r['request'],'jobs.request')['budget_seconds']
                db.execute('UPDATE jobs SET status=?,lease=?,expires=? WHERE id=?',('RUNNING',lease,expiry,job))
                self._log(db,session,'CLAIM',{'job_id':job,'lease':lease,'expires':expiry})
                leases[job]=lease
            return leases

    def _publish(self,session,job,lease,result,validation,*,_before_commit=None):
        """Trusted facade only, after independently checking native output."""
        result_bytes=canonical(result);validation_bytes=canonical(validation)
        result_hash=hashlib.sha256(result_bytes).hexdigest();validation_hash=hashlib.sha256(validation_bytes).hexdigest()
        result_payload=encode_payload(result_bytes)
        with self.transaction() as db:
            row=self._row(db,session);r=db.execute('SELECT * FROM jobs WHERE id=? AND session=?',(job,session)).fetchone()
            require(r is not None,'JOB_NOT_FOUND','job_id','unknown job')
            require(r['status']=='RUNNING' and r['lease']==lease and r['expires']>time.time(),'WORKER_FENCED','job_id','cancelled/expired/recovered worker cannot publish')
            self._cas(row,persisted(r['basis'],'jobs.basis'))
            db.execute('UPDATE jobs SET status=?,result=?,validation=?,lease=NULL WHERE id=?',('COMPLETED',result_payload,validation_bytes.decode(),job))
            self._log(db,session,'COMPLETED',{'job_id':job,'result_sha256':result_hash,'validation_sha256':validation_hash})
            if _before_commit is not None:_before_commit()
        return self.job(session,job)

    def fail(self,session,job,code,message,*,expected_lease=None):
        with self.transaction() as db:
            self._verify(db)
            self._row(db,session);r=db.execute('SELECT status,lease FROM jobs WHERE id=? AND session=?',(job,session)).fetchone()
            require(r is not None,'JOB_NOT_FOUND','job_id','unknown job')
            if r['status'] in ('COMPLETED','FAILED'):return
            if expected_lease is not None and r['lease']!=expected_lease:return
            fault={'severity':'ERROR','code':code,'path':'job_id','message':message}
            db.execute('UPDATE jobs SET status=?,failure=?,lease=NULL WHERE id=?',('FAILED',canonical(fault).decode(),job))
            self._log(db,session,'FAILED',{'job_id':job,'failure':fault})

    def cancel(self,session,key,job):
        payload={'operation':'cancel','job_id':job}
        with self.transaction() as db:
            self._verify(db)
            self._row(db,session);prior=self._receipt(db,session,key,payload)
            if prior:return prior
            r=db.execute('SELECT status FROM jobs WHERE id=? AND session=?',(job,session)).fetchone();require(r is not None,'JOB_NOT_FOUND','job_id','unknown job')
            outcome='COMPLETED_IMMUTABLE' if r['status']=='COMPLETED' else 'JOB_CANCELLED'
            if r['status'] not in ('COMPLETED','FAILED'):
                fault={'severity':'ERROR','code':outcome,'path':'job_id','message':'cancelled; physical commits are not undone'}
                db.execute('UPDATE jobs SET status=?,failure=?,lease=NULL WHERE id=?',('FAILED',canonical(fault).decode(),job))
                self._log(db,session,'CANCEL',{'job_id':job,'failure':fault})
            return self._save_receipt(db,session,key,payload,{'status':outcome,'job_id':job})

    def _accept(self,session,key,job,expected,*,_before_commit=None):
        payload={'operation':'accept','job_id':job,'basis':expected}
        with self.transaction() as db:
            row=self._row(db,session);prior=self._receipt(db,session,key,payload)
            if prior:return prior
            self._cas(row,expected);r=db.execute('SELECT * FROM jobs WHERE id=? AND session=?',(job,session)).fetchone()
            require(r is not None and r['status']=='COMPLETED','JOB_LIFECYCLE','job_id','completed independently validated result required')
            require(persisted(r['basis'],'jobs.basis')==expected,'STALE_HEAD','job_id','job input differs from current basis')
            v=persisted(r['validation'],'jobs.validation');result=persisted(r['result'],'jobs.result')
            require(isinstance(v,dict) and isinstance(result,dict) and v.get('valid') is True and isinstance(result.get('status'),str) and result['status'] in ('FEASIBLE','PARTIAL','RETURN_ONLY'),'WITNESS_REQUIRED','job_id','no-witness result cannot activate')
            generation=row['generation']+1
            require(generation<(1<<63),'COUNTER_OVERFLOW','generation','activation counter overflow')
            db.execute('UPDATE sessions SET generation=?,active_job=? WHERE id=?',(generation,job,session))
            receipt={'status':'ACCEPTED','job_id':job,'basis':self._basis(self._row(db,session))}
            self._log(db,session,'ACTIVATE',{'job_id':job,'generation':str(generation),'head':persisted(row['head'],'sessions.head'),'version':str(row['version'])})
            self._save_receipt(db,session,key,payload,receipt)
            if _before_commit is not None:_before_commit()
            return receipt

    def _event(self,session,key,expected,event_id,verified_head,proof,*,_before_commit=None):
        """Trusted facade candidate only; no external caller supplies this head.

        Event and pending-set mutation commit together, independent of whether
        any subsequent computation succeeds. Old plan is suspended, not erased.
        """
        payload={'operation':'apply_event','basis':expected,'event_id':event_id}
        with self.transaction() as db:
            row=self._row(db,session);prior=self._receipt(db,session,key,payload)
            if prior:return prior
            self._cas(row,expected);old=persisted(row['head'],'sessions.head')
            require(event_id in {e['event_id'] for e in old['pending_events']},'EVENT_ALREADY_APPLIED','event_id','event not pending in current head')
            require(proof.get('event_id')==event_id,'EVENT_BINDING','event_id','verified event differs')
            version=row['version']+1;require(version<(1<<63),'COUNTER_OVERFLOW','head_version','counter overflow')
            db.execute('UPDATE sessions SET head=?,version=?,active_job=NULL WHERE id=?',(canonical(verified_head).decode(),version,session))
            self._log(db,session,'APPLY_EVENT',{'head':verified_head,'version':str(version),'generation':str(row['generation']),'active_job':None,'proof':proof,'suspended_job_id':row['active_job']})
            answer={'status':'APPLIED','event_id':event_id,'event_sha256':proof['event_sha256'],'basis':self._basis(self._row(db,session))}
            self._save_receipt(db,session,key,payload,answer)
            if _before_commit is not None:_before_commit()
            return answer

    def dispatch(self,session,key,expected,action,data,verified_head,*,keep_active=False):
        payload={'operation':'dispatch','basis':expected,'action':action,'data':data}
        with self.transaction() as db:
            row=self._row(db,session);prior=self._receipt(db,session,key,payload)
            if prior:return prior
            self._cas(row,expected)
            version=row['version']+1
            active=row['active_job'] if keep_active else None
            db.execute('UPDATE sessions SET head=?,version=?,active_job=? WHERE id=?',(canonical(verified_head).decode(),version,active,session))
            self._log(db,session,'DISPATCH',{'head':verified_head,'version':str(version),'generation':str(row['generation']),'active_job':active,'action':action,'data':data})
            return self._save_receipt(db,session,key,payload,{'status':'DISPATCHED','basis':self._basis(self._row(db,session))})

    def journal(self,session):
        self.verify()
        with self.transaction(read=True) as db:
            self._row(db,session)
            return [{'kind':r['kind'],'body':persisted(r['body'],'persisted.body'),'digest':r['digest']} for r in db.execute('SELECT * FROM journal WHERE session=? ORDER BY seq',(session,))]

    def _advance(self,session,key,expected,target,verified_head,*,_before_commit=None):
        payload={'operation':'advance','basis':expected,'target_time':target}
        with self.transaction() as db:
            row=self._row(db,session);prior=self._receipt(db,session,key,payload)
            if prior:return prior
            self._cas(row,expected);old=persisted(row['head'],'sessions.head')
            if old['current_time']==target:
                require(old==verified_head,'NOOP_CHANGED','head','no-op must preserve exact head')
                return self._save_receipt(db,session,key,payload,{'status':'NOOP','basis':expected})
            version=row['version']+1;require(version<(1<<63),'COUNTER_OVERFLOW','head_version','head counter overflow')
            db.execute('UPDATE sessions SET head=?,version=? WHERE id=?',(canonical(verified_head).decode(),version,session))
            self._log(db,session,'ADVANCE',{'head':verified_head,'version':str(version),'generation':str(row['generation']),'active_job':row['active_job']})
            receipt={'status':'ADVANCED','basis':self._basis(self._row(db,session))}
            self._save_receipt(db,session,key,payload,receipt)
            if _before_commit is not None:_before_commit()
            return receipt

    def recover(self,session):
        self.verify()
        with self.transaction() as db:
            self._verify(db)
            self._row(db,session)
            ids=[r[0] for r in db.execute('SELECT id FROM jobs WHERE session=? AND status=?',(session,'RUNNING'))]
            for job in ids:
                fault={'severity':'ERROR','code':'WORKER_RECOVERED_FENCED','path':'job_id','message':'restart fences old lease; resubmit from current head'}
                db.execute('UPDATE jobs SET status=?,failure=?,lease=NULL WHERE id=?',('FAILED',canonical(fault).decode(),job))
                self._log(db,session,'RECOVER',{'job_id':job,'failure':fault})
        return {'status':'RECOVERED','fenced_jobs':ids,'basis':self.head(session)['basis']}

    def verify(self):
        with self.transaction(read=True) as db:return self._verify(db)

    def _verify(self,db):
        """Verify within the caller's write-lock boundary; no nested tx.

        Change Impact Analysis: valid Store/2 wire and journal unchanged.
        Cleanup/claim/cancel/recovery cannot race a verify on another lease
        or mutate an authority whose materialized rows differ from its log.
        """
        require(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok','STORE_INVALID','store','SQLite integrity check failed')
        parent='0'*64;latest={};roots={};jobs={};job_states={};receipts={};outbox={};acked=set()
        for r in db.execute('SELECT * FROM journal ORDER BY seq'):
            body=persisted(r['body'],'journal.body');require(isinstance(body,dict),'JOURNAL_CORRUPT','journal.body','object required')
            kinds={'BOOTSTRAP','ACTIVATE','ADVANCE','APPLY_EVENT','DISPATCH','RECEIPT','OUTBOX_ACK','SUBMIT','CLAIM','COMPLETED','FAILED','CANCEL','RECOVER','BUILD_UPGRADE'}
            require(r['kind'] in kinds,'JOURNAL_CORRUPT','journal.kind','unknown committed mutation')
            needed={'BOOTSTRAP':('root','head','version','generation'),'ACTIVATE':('head','version','generation','job_id'),'ADVANCE':('head','version','generation','active_job'),
                    'APPLY_EVENT':('head','version','generation','active_job','proof'),'DISPATCH':('head','version','generation','active_job','action','data'),'RECEIPT':('key','request_sha256','outcome'),'OUTBOX_ACK':('event_id',),
                    'SUBMIT':('job_id','request','basis'),'CLAIM':('job_id','lease','expires'),'COMPLETED':('job_id','result_sha256','validation_sha256'),
                    'FAILED':('job_id','failure'),'CANCEL':('job_id','failure'),'RECOVER':('job_id','failure'),'BUILD_UPGRADE':('old_build_sha256','new_build_sha256','compatibility')}[r['kind']]
            require(all(k in body for k in needed),'JOURNAL_CORRUPT','journal.body','missing mutation fields')
            for key in ('key','job_id','event_id'):
                if key in body:identifier(body[key],'journal.body.'+key)
            for key in ('version','generation'):
                if key in body:require(int64(body[key],'journal.body.'+key)>=0,'JOURNAL_CORRUPT','journal.body.'+key,'nonnegative exact counter')
            for key in ('head','root','request','basis','proof','failure','compatibility','outcome'):
                if key in body:require(isinstance(body[key],dict),'JOURNAL_CORRUPT','journal.body.'+key,'object required')
            if 'basis' in body:basis(body['basis'])
            if 'head' in body:
                identifier(body['head'].get('context_version'),'journal.body.head.context_version')
                if 'current_time' in body['head']:require(isinstance(body['head']['current_time'],str),'JOURNAL_CORRUPT','journal.body.head.current_time','typed time required')
            if 'root' in body:
                require(isinstance(body['root'].get('source_hashes'),dict),'JOURNAL_CORRUPT','journal.body.root.source_hashes','typed source required')
                if 'initial' in body['root']:require(isinstance(body['root']['initial'],dict),'JOURNAL_CORRUPT','journal.body.root.initial','typed initial state required')
            if 'job_id' in body and r['kind'] in ('CLAIM','COMPLETED','FAILED','CANCEL','RECOVER'):
                require(body['job_id'] in job_states,'JOURNAL_CORRUPT','journal.body.job_id','lifecycle mutation precedes submit')
            body_hash=verified_journal_body_hash(r,body,parent)
            require(body_hash is not None,'JOURNAL_CORRUPT','journal','causal journal chain differs');parent=r['digest']
            if r['kind'] in ('BOOTSTRAP','ACTIVATE','ADVANCE','APPLY_EVENT','DISPATCH'):latest[r['session']]=body
            if r['kind']=='BOOTSTRAP':roots[r['session']]=body['root']
            if r['kind']=='RECEIPT':
                key=(r['session'],body['key']);require(key not in receipts,'JOURNAL_CORRUPT','receipts','duplicate committed idempotency key');receipts[key]=body
            if r['kind']=='OUTBOX_ACK':acked.add(body['event_id'])
            elif body.get('notification',True):outbox[r['digest']]={'event_id':r['digest'],'session_id':r['session'],'kind':r['kind'],'body_sha256':body_hash}
            if r['kind']=='SUBMIT':
                jobs[body['job_id']]=(r['session'],body)
                job_states[body['job_id']]=('QUEUED',None,None,None)
            elif r['kind']=='CLAIM':job_states[body['job_id']]=('RUNNING',body['lease'],body['expires'],None)
            elif r['kind']=='COMPLETED':job_states[body['job_id']]=('COMPLETED',None,None,body)
            elif r['kind'] in ('FAILED','CANCEL','RECOVER'):job_states[body['job_id']]=('FAILED',None,None,body)
        session_rows=list(db.execute('SELECT * FROM sessions'))
        require({r['id'] for r in session_rows}==set(roots),'JOURNAL_CORRUPT','sessions','session inventory differs from bootstrap journal')
        for r in session_rows:
            require(r['id'] in latest,'JOURNAL_CORRUPT','sessions','head without causal journal')
            b=latest[r['id']]
            require(persisted(r['root'],'sessions.root')==roots[r['id']],'JOURNAL_CORRUPT','sessions.root','immutable root differs from bootstrap journal')
            require(persisted(r['head'],'sessions.head')==b['head'] and r['version']==int(b['version']) and r['generation']==int(b['generation']) and r['active_job']==(b['job_id'] if 'job_id' in b else b.get('active_job')),'JOURNAL_CORRUPT','sessions.head','materialized head differs from committed journal')
        rows=list(db.execute('SELECT * FROM jobs'))
        require({r['id'] for r in rows}==set(jobs),'JOURNAL_CORRUPT','jobs','job inventory differs from journal')
        for r in rows:
            session,submitted=jobs[r['id']];status,lease,expires,outcome=job_states[r['id']]
            require(r['session']==session and persisted(r['request'],'jobs.request')==submitted['request'] and persisted(r['basis'],'jobs.basis')==submitted['basis'],'JOURNAL_CORRUPT','jobs.input','immutable job input differs from journal')
            require(r['status']==status and r['lease']==lease,'JOURNAL_CORRUPT','jobs.status','job lifecycle differs from journal')
            if status in ('QUEUED','RUNNING'):require(all(r[k] is None for k in ('result','validation','failure')),'JOURNAL_CORRUPT','jobs.result','pending job cannot have published outcome')
            if status=='FAILED':require(r['result'] is None and r['validation'] is None,'JOURNAL_CORRUPT','jobs.result','failed job cannot have a decision')
            if status=='COMPLETED':require(r['failure'] is None,'JOURNAL_CORRUPT','jobs.failure','completed job cannot have failure')
            if status=='RUNNING':require(r['expires']==expires,'JOURNAL_CORRUPT','jobs.expires','lease expiry differs')
            if status=='COMPLETED':
                require(r['result'] is not None and r['validation'] is not None and committed_hash(r['result'],outcome['result_sha256'],'jobs.result') and committed_hash(r['validation'],outcome['validation_sha256'],'jobs.validation'),'JOURNAL_CORRUPT','jobs.result','published result or validation differs')
            if status=='FAILED':require(r['failure'] is not None and persisted(r['failure'],'jobs.failure')==outcome['failure'],'JOURNAL_CORRUPT','jobs.failure','failure differs')
        receipt_rows=list(db.execute('SELECT * FROM receipts'))
        require({(r['session'],r['key']) for r in receipt_rows}==set(receipts),'RECEIPT_CORRUPT','receipts','receipt inventory differs')
        for r in receipt_rows:
            trusted=receipts[(r['session'],r['key'])]
            require(r['digest']==trusted['request_sha256'] and persisted(r['body'],'receipts.body')==trusted['outcome'],'RECEIPT_CORRUPT','receipts','receipt outcome/digest differs')
        rows=list(db.execute('SELECT * FROM outbox'))
        require({r['id'] for r in rows}==set(outbox) and acked.issubset(outbox),'OUTBOX_CORRUPT','outbox','notification inventory differs')
        for r in rows:require(persisted(r['body'],'outbox.body')==outbox[r['id']] and type(r['ack']) is int and r['ack']==int(r['id'] in acked),'OUTBOX_CORRUPT','outbox','notification or acknowledgement differs')
        return True

    def cached_domain(self,session,expected):
        """Immutable same-session/same-basis evidence, not a client cache."""
        with self.transaction(read=True) as db:
            self._cas(self._row(db,session),expected)
            for row in db.execute('SELECT * FROM jobs WHERE session=? AND status=? ORDER BY rowid DESC',(session,'COMPLETED')):
                if persisted(row['basis'],'jobs.basis')==expected and persisted(row['validation'],'jobs.validation').get('valid') is True:
                    return {'job_id':row['id'],'result':persisted(row['result'],'jobs.result')}
        return None

    def notifications(self,session):
        return self.notifications_many([session])[session]

    def notifications_many(self,sessions):
        """One authenticated read snapshot for a worker's session batch."""
        with self.transaction(read=True) as db:
            self._verify(db)
            groups={}
            for session in sessions:
                self._row(db,session);groups[session]=[]
            for row in db.execute('SELECT body FROM outbox WHERE ack=0 ORDER BY rowid'):
                body=persisted(row['body'],'persisted.body')
                if body['session_id'] in groups:groups[body['session_id']].append(body)
            return groups

    def acknowledge(self,session,key,event_id):
        digest(event_id,'event_id');payload={'operation':'outbox_ack','event_id':event_id}
        with self.transaction() as db:
            self._row(db,session);prior=self._receipt(db,session,key,payload)
            if prior:return prior
            r=db.execute('SELECT body,ack FROM outbox WHERE id=?',(event_id,)).fetchone()
            require(r is not None and persisted(r['body'],'persisted.body')['session_id']==session,'OUTBOX_NOT_FOUND','event_id','notification outside authority')
            if not r['ack']:
                db.execute('UPDATE outbox SET ack=1 WHERE id=?',(event_id,));self._log(db,session,'OUTBOX_ACK',{'event_id':event_id})
            # Ack retry is idempotent; does not generate a new notification.
            body={'status':'ACKNOWLEDGED','event_id':event_id}
            db.execute('INSERT INTO receipts VALUES(?,?,?,?)',(session,key,sha(payload),canonical(body).decode()))
            # RECEIPT entries for ack carry the same verification without an
            # outbox emission, preventing an infinite notification-ack loop.
            self._log(db,session,'RECEIPT',{'key':key,'request_sha256':sha(payload),'outcome':body,'notification':False})
            return body

    def backup(self,destination):
        """Administrative SDK operation, never a client-supplied source path."""
        self.verify();target=Path(destination)
        require(not target.exists(),'BACKUP_EXISTS','destination','backup must be a new file')
        target.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as source,sqlite3.connect(target) as dest:source.backup(dest)
        Store(target,self.build).verify()
        return {'status':'BACKUP_VERIFIED','schema_version':VERSION,'build_sha256':self.build}

    def upgrade(self,new_build,compatibility_validator,*,_before_commit=None):
        """Explicit server-admin migration. Historical result bytes stay old.

        The callable must raw-revalidate active heads/results and lock the
        consumer contract before upgrading. It runs outside the transaction.
        A concurrent mutation causes the CAS below to reject the upgrade.
        """
        digest(new_build,'new_build_sha256');self.verify()
        with self.transaction() as db:
            checkpoint=db.execute('SELECT digest FROM journal ORDER BY seq DESC LIMIT 1').fetchone()
            records=[r[0] for r in db.execute('SELECT id FROM sessions')]
        checks=compatibility_validator([self.head(s) for s in records])
        require(isinstance(checks,dict) and checks.get('valid') is True,'UPGRADE_INCOMPATIBLE','upgrade','current checker/contract compatibility failed')
        old=self.build
        with self.transaction() as db:
            current=db.execute('SELECT digest FROM journal ORDER BY seq DESC LIMIT 1').fetchone()
            require(current==checkpoint,'UPGRADE_STALE','upgrade','authority changed during compatibility check')
            for r in db.execute('SELECT id,session FROM jobs WHERE status=?',('RUNNING',)).fetchall():
                fault={'severity':'ERROR','code':'BUILD_UPGRADED_FENCED','path':'job_id','message':'old execution lease cannot publish after explicit install upgrade'}
                db.execute('UPDATE jobs SET status=?,failure=?,lease=NULL WHERE id=?',('FAILED',canonical(fault).decode(),r['id']))
                self._log(db,r['session'],'RECOVER',{'job_id':r['id'],'failure':fault})
            self._log(db,'admin','BUILD_UPGRADE',{'old_build_sha256':old,'new_build_sha256':new_build,'compatibility':checks})
            db.execute('UPDATE metadata SET value=? WHERE key=?',(new_build,'build_sha256'))
            if _before_commit is not None:_before_commit()
        self.build=new_build;self.verify()
        return {'status':'UPGRADED','old_build_sha256':old,'new_build_sha256':new_build,'current_checker_receipt':checks,'historical_execution_preserved':True}
