"""One console for local API, worker and Vite; credentials stay server-side."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4
import webbrowser
try:
    from .local_backend_control import stop_verified_backend, selected_installation, stop_local_services, installation_digest
except ImportError:
    from local_backend_control import stop_verified_backend, selected_installation, stop_local_services, installation_digest


def port_open(port):
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def choose_port(preferred, occupied=port_open):
    for port in range(preferred, min(preferred + 20, 65536)):
        if not occupied(port):
            return port
    raise RuntimeError('No free frontend port; close an old frontend first')


@contextmanager
def instance_lock(path):
    handle = path.open('a+b')
    handle.seek(0)
    if os.fstat(handle.fileno()).st_size == 0:
        handle.write(b'0'); handle.flush()
    handle.seek(0)
    locked = False
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            pass
        yield locked
    finally:
        handle.close()  # OS releases the lock even after a crash.


class OwnedProcesses:
    """Windows closes the process tree if the launcher console is closed."""
    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [('per_process', ctypes.c_int64), ('per_job', ctypes.c_int64),
                        ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t), ('max_working', ctypes.c_size_t),
                        ('active_limit', wintypes.DWORD), ('affinity', ctypes.c_size_t),
                        ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        info = Extended(); info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise RuntimeError('Cannot create the launcher process group')

    def add(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            process.terminate(); process.wait(timeout=10)
            raise RuntimeError('Cannot attach owned child to launcher process group')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle); self.handle = None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--local-root', type=Path)
    parser.add_argument('--backend-port', type=int, default=8000)
    parser.add_argument('--frontend-port', type=int, default=5174)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--offline', action='store_true', help='Disable online weather capture')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--stop', action='store_true', help='Stop this installation and release its SafeRoute ports')
    action.add_argument('--restart', action='store_true', help='Stop this installation before starting it again')
    parser.add_argument('--stop-file', type=Path, help='New absolute file; creating it stops this launcher')
    args = parser.parse_args()
    if not all(1024 <= p <= 65515 for p in (args.backend_port, args.frontend_port)):
        parser.error('Ports must be between 1024 and 65515')
    project = Path(__file__).resolve().parents[2]
    local = selected_installation(project,args.local_root)
    config = json.loads((local/'installation.json').read_bytes())
    if Path(config['snapshot_root']).resolve() != project:
        raise RuntimeError('Installation belongs to another source directory')
    if args.stop or args.restart:
        print('Stopping verified SafeRoute services...', flush=True)
        stop_local_services(local, port_open)
        if args.stop:
            remaining = [p for p in (args.backend_port, args.frontend_port) if port_open(p)]
            if remaining:
                raise RuntimeError('Ports still occupied without matching active SafeRoute ownership: ' + str(remaining))
            print('SafeRoute stopped; ports released.', flush=True)
            return
    if args.stop_file and (not args.stop_file.is_absolute() or args.stop_file.exists()):
        raise RuntimeError('--stop-file must be a new absolute path')
    private = local/'backend'
    credentials = json.loads((private/'dev_access.json').read_bytes())['credentials']
    credential = next(c for c in credentials if c['role']=='dispatcher')
    records = json.loads((private/'auth_tokens.json').read_bytes())['tokens']
    if not any(r['actor_id']==credential['actor_id'] and r['role']=='dispatcher' and
               r['token_sha256']==hashlib.sha256(credential['token'].encode()).hexdigest() for r in records):
        raise RuntimeError('Local credentials do not match auth_tokens.json')
    bundled_node = project/'portable/node/node.exe'
    node = str(bundled_node) if bundled_node.is_file() else shutil.which('node')
    vite = project/'frontend/node_modules/vite/bin/vite.js'
    if not node or not vite.is_file():
        raise RuntimeError('Node/dependencies missing. Run npm.cmd ci in frontend first')
    state_file = private/'launcher_status.json'
    with instance_lock(private/'launcher.lock') as owned:
        if not owned:
            state = json.loads(state_file.read_bytes()) if state_file.is_file() else {}
            print('SafeRoute launcher is already running: ' + state.get('admin_url','startup in progress'), flush=True)
            if not args.no_browser and state.get('status')=='READY':
                webbrowser.open(state['admin_url'])
            return
        port = choose_port(args.frontend_port, lambda p:p==args.backend_port or port_open(p))
        origin = f'http://127.0.0.1:{port}'
        backend = f'http://127.0.0.1:{args.backend_port}'
        run_dir = local/'receipts'/('desktop_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_')+uuid4().hex[:8])
        run_dir.mkdir(parents=True)
        backend_stop = run_dir/'backend.stop'
        launcher_stop = run_dir/'stop.request'
        state = {'status':'STARTING','pid':os.getpid(),'admin_url':origin+'/admin', 'driver_url':origin+'/driver',
                 'logs':str(run_dir),'backend_owned':False, 'stop_file':str(launcher_stop),
                 'installation_sha256':installation_digest(local)}
        def save():
            temporary = state_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(state,indent=2),encoding='utf-8')
            temporary.replace(state_file)
        save()
        opener=build_opener(ProxyHandler({}))
        def api(path):
            with opener.open(Request(backend+path,headers={'Authorization':'Bearer '+credential['token']}),timeout=15) as response:
                return json.load(response)['data']
        def stop_requested():
            return launcher_stop.exists() or (args.stop_file is not None and args.stop_file.exists())
        children=[]; logs=[]; group=OwnedProcesses()
        def start(command,name,env,cwd):
            log=(run_dir/name).open('w',encoding='utf-8');logs.append(log)
            flags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)
            child=subprocess.Popen(command,cwd=cwd,env=env,stdout=log,stderr=log,creationflags=flags)
            group.add(child);children.append(child);return child
        try:
            print('SafeRoute local | starting API, worker and frontend',flush=True)
            print('Logs: '+str(run_dir),flush=True)
            env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','SAFEROUTE_CORS_ORIGINS':origin,
                 'SAFEROUTE_COMPUTE_BUDGET_SECONDS':os.environ.get('SAFEROUTE_COMPUTE_BUDGET_SECONDS','180')}
            if port_open(args.backend_port):
                try:
                    ready = api('/ready')['ready'] is True
                except OSError:
                    ready = False
                if not ready:
                    print('Existing backend is not READY; requesting verified graceful restart.', flush=True)
                    stop_verified_backend(local, args.backend_port, port_open)
                else:
                    if api('/api/runtime/capabilities')['build_sha256'] != config['expected_build_sha256']:
                        raise RuntimeError('Port is used by a different backend build')
                    print('Using the existing verified backend; it will remain running on exit.',flush=True)
            if not port_open(args.backend_port):
                state['backend_owned']=True;save()
                start([sys.executable,'-X','utf8','-B',str(project/'backend/scripts/run_backend.py'),'--local-root',str(local),
                       '--port',str(args.backend_port),*(['--offline'] if args.offline else []),'--stop-file',str(backend_stop)],'backend.log',env,project)
                deadline=time.monotonic()+1800
                while True:
                    if stop_requested(): return
                    if any(c.poll() is not None for c in children):raise RuntimeError('Backend exited; see backend.log')
                    try:
                        if api('/ready')['ready'] is True:break
                    except (OSError,ValueError,KeyError):pass
                    if time.monotonic()>deadline:raise TimeoutError('Backend startup timeout')
                    time.sleep(2)
            if (project/'deployment/funnel.json').is_file():
                start([sys.executable,'-X','utf8','-B',str(project/'deployment/funnel_runtime.py'),
                       'serve','--local-root',str(local),'--backend',backend,'--stop-file',str(backend_stop)], 'funnel-gateway.log',env,project)
            frontend_env={**env,'VITE_DISPATCH_MODE':'backend','VITE_M3_BASE_URL':origin,'VITE_LOCAL_PROXY':'1',
                          'SAFEROUTE_LOCAL_ACCESS_FILE':str(private/'dev_access.json'),
                          'SAFEROUTE_LOCAL_AUTH_FILE':str(private/'auth_tokens.json'),'SAFEROUTE_LOCAL_BACKEND_URL':backend}
            start([node,str(vite),'--host','127.0.0.1','--port',str(port),'--strictPort'],'frontend.log',frontend_env,project/'frontend')
            deadline=time.monotonic()+60
            while True:
                if stop_requested():return
                if any(c.poll() is not None for c in children):raise RuntimeError('Child exited; see launcher logs')
                try:
                    with opener.open(origin+'/admin',timeout=2) as response:
                        if response.status==200:break
                except OSError:pass
                if time.monotonic()>deadline:raise TimeoutError('Frontend startup timeout')
                time.sleep(0.25)
            state['status']='READY';save()
            print('READY | Auto authentication enabled (no token to paste)',flush=True)
            print('Admin: '+state['admin_url']+'\nDriver: '+state['driver_url'],flush=True)
            print('Press Ctrl+C here to stop the processes started by this launcher.',flush=True)
            if not args.no_browser:webbrowser.open(state['admin_url'])
            while not stop_requested():
                if any(c.poll() is not None for c in children):raise RuntimeError('Owned process stopped; see logs')
                time.sleep(0.3)
        except KeyboardInterrupt:
            pass
        except Exception:
            state['status']='FAILED';raise
        finally:
            print('Stopping owned processes...',flush=True)
            backend_stop.touch(exist_ok=True)
            # Frontend was started last. Do not touch any pre-existing server.
            if children and not (len(children)==1 and state['backend_owned']):
                frontend=children[-1]
                if frontend.poll() is None:frontend.terminate()
            deadline=time.monotonic()+240
            for child in reversed(children):
                try:child.wait(timeout=max(1,deadline-time.monotonic()))
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=10)
            group.close()
            for log in logs:log.close()
            if state['status']!='FAILED':state['status']='STOPPED'
            save()


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('START FAILED: '+str(error),file=sys.stderr)
        raise SystemExit(1)
