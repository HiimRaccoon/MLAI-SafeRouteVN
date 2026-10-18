"""Public contract lock and compatibility check (not an implementation hash).

Internal build changes are allowed only with current raw revalidation and the
same locked consumer contract. A breaking contract needs a reviewed version.
"""
from pathlib import Path
import hashlib
from .protocol import canonical,decode,require

PUBLIC_FILES=('optimization/runtime/sdk.py','optimization/runtime/job_view.py','optimization/runtime/protocol.py',
              'optimization/runtime/protocol.schema.json','optimization/runtime/response.schema.json',
              'optimization/runtime/execution_view.schema.json','optimization/runtime/contracts.py',
              'optimization/runtime/reference_consumer.mjs','optimization/runtime/trajectory_contract.py',
              'optimization/runtime/consumer_golden_corpus.json','optimization/runtime/consumer_golden.py','optimization/runtime/consumer_golden.mjs',
              'optimization/runtime/vectors.json','optimization/runtime/portable_tests.py','docs/step7_INTEGRATION_CROSSWALK.md')
def record(repo):
    repo=Path(repo)
    return {'schema_version':'task02-m2-runtime-handoff-contract-lock/1','api_v1':'FROZEN_EXTERNAL_CONTRACT_UNCHANGED',
      'sdk_version':'task02-m2-runtime-sdk/1','command_version':'task02-m2-runtime-command/1','response_version':'task02-m2-runtime-response/1',
      'execution_view_version':'task02-m2-execution-view/2','job_view_version':'task02-m2-runtime-job-view/1',
      'public_methods':['command','read_notifications','acknowledge','validate_session','backup','job_view','compare_profiles'],
      'server_extensions':['compute_profiles','verify_store'],
      'units':{'mass':'kg','distance':'m','duration':'s; exact action timestamps signed int64 microseconds','cost':'VND','rate':'VND/km','coordinates':'WGS84 [longitude,latitude]','time':'ISO +07:00','exposure':'relative proxy, not accident probability'},
      'exact_numbers':'Safe JSON integer or canonical signed int64 string; progress rational numerator/denominator int64 strings',
      'scopes':['OBSERVED_PREFIX_ONLY','PLANNED_SUFFIX_ONLY','projected_whole=observed+remaining_suffix'],
      'lifecycle':['QUEUED','RUNNING','COMPLETED','FAILED'],'simulation':{'execution_mode':'SIMULATED_REPLAY','real_world_observation':False},
      'upgrade':'Explicit server-admin CAS + raw current checker receipt + locked consumer compatibility; old workers fenced; historical result bytes immutable',
      'files':{r:{'bytes':(repo/r).stat().st_size,'sha256':hashlib.sha256((repo/r).read_bytes()).hexdigest()} for r in PUBLIC_FILES}}
def verify_lock(repo,expected_digest):
    path=Path(repo)/'optimization/runtime/HANDOFF_CONTRACT_LOCK.json'
    require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==expected_digest,'CONTRACT_UPGRADE_REQUIRED','HANDOFF_CONTRACT_LOCK','server-approved consumer lock required')
    require(decode(path.read_bytes())==record(repo),'CONTRACT_CHANGED','HANDOFF_CONTRACT_LOCK.files','public contract changed; review/version/adapter required')
    return decode(path.read_bytes())

def compatible(old,current):
    """Reviewed validation hardening is not a breaking valid-wire migration.

    File hashes change, but all public versions, methods, units and lifecycle
    must match. The actual Python/JS corpus and raw current checker receipts
    are additionally required by release acceptance, not inferred here.
    """
    keys=('schema_version','api_v1','sdk_version','command_version','response_version','execution_view_version','job_view_version','public_methods','units','exact_numbers','scopes','lifecycle','simulation','upgrade')
    require(all(old.get(k)==current.get(k) for k in keys),'CONTRACT_UPGRADE_REQUIRED','HANDOFF_CONTRACT_LOCK','public semantics/version changed; reviewed migration required')
    return {'compatible':True,'classification':'VALID_WIRE_UNCHANGED; MALFORMED_INPUT_REJECTION_HARDENED','consumer_rewrite_required':False}
