"""M3 subprocess bridge: verify the installed M2 package before importing it."""
import argparse
import json
from pathlib import Path
import runpy
import sys
from uuid import uuid4
import re
import sqlite3


class BridgeFailure(Exception):
    def __init__(self, code):
        self.code = code


def public_failure_code(error):
    code = getattr(error, 'code', None)
    if code == 'STORE_INVALID':
        # M2 wraps SQLite errors in STORE_INVALID. Only a typed SQLite
        # contention cause is transient; malformed/corrupt stores stay fenced.
        cause = error.__cause__
        seen = set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if isinstance(cause, sqlite3.OperationalError):
                number = getattr(cause, 'sqlite_errorcode', None)
                if type(number) is int and number & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    return 'RUNTIME_BUSY'
            cause = cause.__cause__
    return code


def dispatch(client, operation, fields):
    if operation == 'bootstrap_run':
        if set(fields) != {'session_id','command_id','run_id','manifest_sha256'}:
            raise ValueError('Server run bootstrap fields')
        value = client.bootstrap_run(**fields)
        return {'schema_version':'task02-m2-runtime-response/1','command_id':fields['command_id'],'status':'OK','value':value,'diagnostics':[]}
    if operation == "capabilities":
        result = client.command("capabilities", "m3-capabilities-" + str(uuid4()))
        if result['status']=='OK':
            result['value']['server_extensions']=['compute_profiles'] if callable(getattr(client,'compute_profiles',None)) else []
    else:
        extra = {"bootstrap": {"scenario_id"}, "submit": {"basis", "profile", "budget_seconds"},
                 "dispatch": {"basis", "action", "data"},
                 "compute": {"job_id"}, "job_view": {"job_id"}, "job_forecast": {"job_id"}, "cancel": {"job_id"}, "accept": {"job_id", "basis"},
                  "advance": {"basis", "target_time"}, "apply_event": {"basis", "event_id"}, "acknowledge": {"event_id"}, "compare_profiles": {"job_ids"}, "compute_batch": {"job_ids"}}
        required = {"command_id", "session_id"} | extra.get(operation, set())
        if operation == "backup":
            required = {"command_id", "new_server_path"}
        elif operation in ("inspect_sessions", "read_notifications_batch"):
            required = {"command_id", "session_ids"}
        elif operation == "acknowledge_batch":
            required = {"command_id", "rows"}
        if not isinstance(fields, dict) or set(fields) != required:
            raise ValueError("Server bridge fields")
        if operation in ("read_notifications", "acknowledge", "validate_session", "backup", "inspect_sessions", "read_notifications_batch", "acknowledge_batch", "compare_profiles", "compute_batch"):
            if operation == "compute_batch":
                value = client.compute_profiles(fields["session_id"], fields["job_ids"])
            elif operation == "compare_profiles":
                value = client.compare_profiles(fields["session_id"], fields["job_ids"])
            elif operation == "read_notifications":
                value = client.read_notifications(fields["session_id"])
            elif operation == "acknowledge":
                value = client.acknowledge(fields["session_id"], fields["command_id"], fields["event_id"])
            elif operation == "validate_session":
                value = client.validate_session(fields["session_id"])
            elif operation == "backup":
                # Admin-only bridge call. There is deliberately no HTTP route.
                value = client.backup(fields["new_server_path"])
            elif operation == "read_notifications_batch":
                value = {sid: client.read_notifications(sid) for sid in fields["session_ids"]}
            elif operation == "acknowledge_batch":
                value = [client.acknowledge(row["session_id"], row["command_id"], row["event_id"]) for row in fields["rows"]]
            else:
                value = []
                for sid in fields["session_ids"]:
                    try:
                        recovery = client.command("recover", fields["command_id"] + ":" + sid, session_id=sid)
                        if recovery["status"] != "OK":
                            value.append({"session_id": sid, "diagnostic_code": recovery["diagnostics"][0]["code"]})
                            continue
                        value.append({"session_id": sid, "recovery": recovery["value"], "validation": client.validate_session(sid),
                                      "execution_view": client.command("resolve", fields["command_id"] + ":view:" + sid, session_id=sid)["value"],
                                      "notifications": client.read_notifications(sid)})
                    except Exception as error:
                        code = public_failure_code(error)
                        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
                            raise
                        value.append({"session_id": sid, "diagnostic_code": code})
            result = {"schema_version": "task02-m2-runtime-response/1", "command_id": fields["command_id"], "status": "OK", "value": value, "diagnostics": []}
        elif operation in ("job_view", "job_forecast"):
            value = getattr(client, operation)(fields["session_id"], fields["job_id"])
            result = {"schema_version": "task02-m2-runtime-response/1", "command_id": fields["command_id"], "status": "OK", "value": value, "diagnostics": []}
        else:
            result = client.command(operation, **fields)
            if operation == "compute" and result["status"] == "OK":
                # The command result is a private record: forward only the public typed projection.
                result["value"] = client.job_view(fields["session_id"], fields["job_id"])
    return result


def serve(args, client, config, entry, root, store):
    """One bounded request at a time on private inherited stdio pipes."""
    original=args.installation.read_bytes()
    allowed={'capabilities','bootstrap','bootstrap_run','resolve','submit','job_view','job_forecast','cancel','dispatch',
             'recover','accept','advance','apply_event','read_notifications','acknowledge',
             'validate_session','backup','inspect_sessions','read_notifications_batch',
             'acknowledge_batch','compare_profiles'}
    for raw in iter(lambda:sys.stdin.buffer.readline(1048577),b''):
        try:
            if len(raw)>1048576 or not raw.endswith(b'\n'):
                raise BridgeFailure('RUNTIME_REQUEST_TOO_LARGE')
            request=json.loads(raw)
            if not isinstance(request,dict) or set(request)!={'operation','fields'} or request['operation'] not in allowed:
                raise BridgeFailure('RUNTIME_REQUEST_INVALID')
            if args.installation.read_bytes()!=original:
                raise BridgeFailure('INSTALLATION_BINDING_CHANGED')
            if not store.is_file():raise BridgeFailure('STORE_MISSING')
            entry['verify_install'](root,root/'production_inventory.json',config['expected_build_sha256'])
            if callable(getattr(client,'verify_store',None)):
                client.verify_store()
            else:
                # Old pinned installations remain usable while an operator
                # stages an upgrade; their original per-open checks still run.
                client=type(client)(snapshot_root=config['snapshot_root'],store_path=str(store),
                                    expected_build_sha256=config['expected_build_sha256'])
            result=dispatch(client,request['operation'],request['fields'])
        except Exception as error:
            code=public_failure_code(error)
            if not isinstance(code,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}',code):code='RUNTIME_UNAVAILABLE'
            result={'schema_version':'task02-m2-runtime-response/1','command_id':None,'status':'FAIL','value':None,
                    'diagnostics':[{'severity':'ERROR','code':code,'path':'runtime','message':'Public runtime operation failed'}]}
        print(json.dumps(result,ensure_ascii=True,allow_nan=False),flush=True)
        if result.get('status')=='FAIL' and result['diagnostics'][0]['code'] in ('INSTALLATION_BINDING_CHANGED','RUNTIME_REQUEST_TOO_LARGE'):
            return 0
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--installation", required=True, type=Path)
    parser.add_argument("--operation", choices=["capabilities", "bootstrap_run", "bootstrap", "resolve", "submit", "compute", "compute_batch", "job_view", "job_forecast", "cancel", "recover", "accept", "advance", "apply_event", "read_notifications", "acknowledge", "validate_session", "backup", "inspect_sessions", "read_notifications_batch", "acknowledge_batch", "compare_profiles"], default="capabilities")
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    try:
        guard = runpy.run_path(str(Path(__file__).with_name('offline_guard.py')))
        if guard['install_from_environment']('sdk-bridge'):
            guard['protect_sdk_children'](args.installation)
        config = json.loads(args.installation.read_text(encoding="utf-8"))
        if config["schema_version"] != "saferoute-m3-server-installation/1":
            raise ValueError("Installation schema")
        root = Path(config["runtime_root"]).resolve(strict=True)
        entry = runpy.run_path(str(root / "runtime_entry.py"))
        entry["verify_install"](root, root / "production_inventory.json", config["expected_build_sha256"])
        # -I excludes the team tree/PYTHONPATH; only the verified installation is added.
        sys.path.insert(0, str(root))
        from optimization.runtime.sdk import RuntimeClient
        import optimization.runtime.sdk as sdk
        if Path(sdk.__file__).resolve() != root / "optimization/runtime/sdk.py":
            raise ValueError("SDK import origin")
        store = Path(config["authority_store_parent"]) / "backend_authority.sqlite"
        if args.operation not in ("capabilities", "bootstrap", "bootstrap_run") and not store.is_file():
            raise BridgeFailure("STORE_MISSING")
        client = RuntimeClient(snapshot_root=config["snapshot_root"], store_path=str(store),
                               expected_build_sha256=config["expected_build_sha256"])
        if args.serve:
            return serve(args, client, config, entry, root, store)
        fields = {} if args.operation == 'capabilities' else json.loads(sys.stdin.buffer.read(1048576))
        result = dispatch(client, args.operation, fields)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0
    except Exception as error:
        code = public_failure_code(error)
        if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
            print(json.dumps({"schema_version": "task02-m2-runtime-response/1", "command_id": None, "status": "FAIL", "value": None,
                "diagnostics": [{"severity": "ERROR", "code": code, "path": "runtime", "message": "Public runtime operation failed"}]}))
            return 0
        # Never return installation paths, stack traces, raw stores or credentials.
        print(json.dumps({"schema_version": "saferoute-m3-bridge-error/1", "code": "RUNTIME_VERIFICATION_FAILED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
