"""Actual installed-environment attestation, separate from source/build pins.

Change Impact Analysis: no optimization changes. Server startup rejects lock,
Python ABI/platform or distribution mismatches. Import/CP-SAT smoke is an
explicit native preflight, not a substitute for ZIP-clean runtime execution.
No Application Control setting or DLL loading policy is changed.
"""
from pathlib import Path
from importlib import metadata
import hashlib,platform,re,sys,sysconfig
from .protocol import require

VERSION='task02-m2-runtime-environment-attestation/1'
def attest(repo,*,smoke=False):
    lock=Path(repo)/'requirements-runtime.lock.txt';raw=lock.read_bytes();expected={}
    for i,line in enumerate(raw.decode('utf8').splitlines()):
        line=line.strip()
        if not line or line.startswith('#'):continue
        match=re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)',line)
        require(match is not None,'ENVIRONMENT_LOCK','requirements-runtime.lock.txt:'+str(i+1),'exact distribution version required')
        name,version=match.groups();require(name not in expected,'ENVIRONMENT_LOCK',name,'duplicate dependency');expected[name]=version
    require(sys.version_info[:2]==(3,12) and platform.system()=='Windows','ENVIRONMENT_MISMATCH','interpreter','approved Windows CPython 3.12 required')
    actual={};records={}
    for name,version in expected.items():
        try:dist=metadata.distribution(name)
        except metadata.PackageNotFoundError as e:
            from .protocol import RuntimeError
            raise RuntimeError('ENVIRONMENT_MISMATCH','dependencies.'+name,'declared package missing') from e
        actual[name]=dist.version
        require(dist.version==version,'ENVIRONMENT_MISMATCH','dependencies.'+name,'installed distribution differs from lock')
        text=dist.read_text('RECORD')
        records[name]=hashlib.sha256(text.encode()).hexdigest() if text is not None else None
    value={'schema_version':VERSION,'interpreter':str(Path(sys.executable).resolve()),'python':sys.version,'platform':platform.platform(),'architecture':platform.machine(),'abi':sysconfig.get_config_var('SOABI'),'lock_sha256':hashlib.sha256(raw).hexdigest(),'dependencies':actual,'installed_record_sha256':records,'smoke_executed':smoke,'status':'ENVIRONMENT_READY','trust_scope':'SERVER_APPROVED_INSTALL; OS_ACCESS_TRUST; RECORD_DIGEST_NOT_DIGITAL_SIGNATURE'}
    if smoke:
        import base64
        native_files={}
        for name in expected:
            dist=metadata.distribution(name)
            for f in dist.files or ():
                if Path(str(f)).suffix.lower() not in ('.dll','.pyd'):continue
                location=Path(dist.locate_file(f));raw_native=location.read_bytes();digest=hashlib.sha256(raw_native).digest()
                require(f.hash is not None and f.hash.mode=='sha256' and base64.urlsafe_b64encode(digest).rstrip(b'=').decode()==f.hash.value,'ENVIRONMENT_FILE','dependencies.'+name+'.'+str(f),'native installed bytes differ from distribution RECORD')
                native_files[name+':'+str(f)]={'bytes':len(raw_native),'sha256':digest.hex()}
        value['native_distribution_files']=native_files
        import pandas
        from ortools.sat.python import cp_model
        model=cp_model.CpModel();x=model.new_bool_var('x');model.add(x==1);solver=cp_model.CpSolver();status=solver.solve(model)
        require(status==cp_model.OPTIMAL and solver.value(x)==1,'ENVIRONMENT_ENGINE','cp_sat','native CP-SAT smoke failed')
        value['pandas_import_path']=pandas.__file__;value['cp_sat_smoke_status']=solver.status_name(status)
    return value
