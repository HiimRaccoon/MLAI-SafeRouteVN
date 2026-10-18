"""Public M3 SDK surface. Never accepts client state, paths or trust records.

Change Impact Analysis: additive facade SDK /1. Server-owned install/snapshot/
store are supplied once. M4 consumes the execution-view, not this private DB.
Administrative commands must not be exposed as unauthenticated HTTP endpoints.
"""
from .facade import Runtime
from .protocol import VERSION,require,sha

VERSION_SDK='task02-m2-runtime-sdk/1'

class RuntimeClient:
    def __init__(self,*,snapshot_root,store_path,expected_build_sha256):
        self.__runtime=Runtime(snapshot_root,store_path,expected_build_sha256)
    def command(self,operation,command_id,session_id=None,**fields):
        value={'schema_version':VERSION,'operation':operation,'command_id':command_id,**fields}
        if session_id is not None:value['session_id']=session_id
        return self.__runtime.execute(value)
    def read_notifications(self,session_id):return self.__runtime.store.notifications(session_id)
    def acknowledge(self,session_id,command_id,event_id):return self.__runtime.store.acknowledge(session_id,command_id,event_id)
    def validate_session(self,session_id):return self.__runtime.validate_session(session_id)
    def backup(self,new_server_path):return self.__runtime.store.backup(new_server_path)
    def verify_store(self):return self.__runtime.store.verify()
    def bootstrap_run(self,session_id,command_id,run_id,manifest_sha256):
        from .run_source import bootstrap
        return bootstrap(self.__runtime,session_id,command_id,run_id,manifest_sha256)
    def job_view(self,session_id,job_id):
        from .job_view import job_view
        return job_view(self.__runtime.store.job(session_id,job_id))
    def job_forecast(self,session_id,job_id):
        from .forecast import job_forecast
        return job_forecast(self.__runtime.store.job(session_id,job_id))
    def compare_profiles(self,session_id,job_ids):
        jobs=[self.__runtime.store.job(session_id,j) for j in job_ids]
        require(all(j['status']=='COMPLETED' and j['validation'].get('valid') is True for j in jobs),'WITNESS_REQUIRED','job_ids','only certified completed jobs can be compared')
        same=len({sha(j['basis']) for j in jobs})==1 and len({j['result']['domain_sha256'] for j in jobs})==1
        return {'schema_version':'task02-m2-runtime-comparison/1','status':'COMPARABLE' if same else 'NON_COMPARABLE','reason':None if same else 'AUTHENTICATED_BASIS_OR_PHYSICAL_DOMAIN_DIFFERS','jobs':[{'job_id':j['id'],'profile':j['request']['profile'],'basis':j['basis'],'domain_sha256':j['result']['domain_sha256'],'metrics':j['result']['metrics']} for j in jobs]}

    def compute_profiles(self,session_id,job_ids):
        """Server-owned batch; same public job views and independent leases."""
        from .profile_batch import compute_profiles
        from .job_view import job_view
        return [job_view(job) for job in compute_profiles(self.__runtime,session_id,job_ids)]

def validate_public_projection_capability(job):
    """No lossy route fabrication from temporal/reload/return-only trajectories.

    Frozen API v1 remains usable for its reviewed static examples. Generic
    execution is supplemental /2 until a reviewed v1 projection exists.
    """
    return {'status':'UNSUPPORTED','code':'SUPPLEMENTAL_EXECUTION_VIEW_REQUIRED','plan':None,'reason':'Use task02-m2-execution-view/2; no certified API v1 dynamic plan projection is advertised.'}
