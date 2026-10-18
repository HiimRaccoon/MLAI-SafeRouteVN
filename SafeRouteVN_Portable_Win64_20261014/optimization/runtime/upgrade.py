"""Server-admin controlled build change, not a client command.

Explicit schema /2 only. Guarded prototype /1 store migration is not silently
assumed; historical journals/payloads must be independently authorized first.
"""
from pathlib import Path
import hashlib
from .build import inventory,verify
from .facade import Runtime
from .store import Store
from .protocol import require
from .handoff import verify_lock

def controlled_upgrade(*,snapshot_root,store_path,previous_build_sha256,new_build_sha256,expected_contract_lock_sha256,repo_root=None):
    repo=Path(repo_root or Path(__file__).resolve().parents[2]);current=inventory(repo);verify(repo,current,new_build_sha256)
    lock=repo/'optimization/runtime/HANDOFF_CONTRACT_LOCK.json'
    verify_lock(repo,expected_contract_lock_sha256)
    store=Store(store_path,previous_build_sha256)
    runtime=Runtime.__new__(Runtime);runtime.repo=repo;runtime.snapshot=Path(snapshot_root);runtime.build=current;runtime.store=store
    def revalidate(heads):
        receipts=[runtime.validate_session(h['basis']['session_id']) for h in heads]
        return {'valid':all(r['valid'] is True for r in receipts),'current_checker_build_sha256':new_build_sha256,'consumer_contract_lock_sha256':expected_contract_lock_sha256,'historical_execution_build_sha256':previous_build_sha256,'receipts':receipts}
    return store.upgrade(new_build_sha256,revalidate)
