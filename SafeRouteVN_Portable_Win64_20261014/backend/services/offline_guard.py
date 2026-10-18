"""Opt-in loopback-only Python socket policy for an offline rehearsal.

This is an audited test/operations policy, not an OS security sandbox.
"""
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
from uuid import uuid4

MODE_ENV = 'SAFEROUTE_OFFLINE_MODE'
AUDIT_ENV = 'SAFEROUTE_OFFLINE_AUDIT_DIR'
_installed = False


class OfflineNetworkBlocked(PermissionError):
    pass


def loopback(host):
    if isinstance(host, bytes):
        try:
            host = host.decode('ascii')
        except UnicodeError:
            return False
    if not isinstance(host, str):
        return False
    if host.lower() == 'localhost':
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or (isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


def install_from_environment(role, *, deny_children=False):
    global _installed
    mode = os.getenv(MODE_ENV)
    if mode is None:
        return False
    if mode != 'loopback-only':
        raise ValueError('Unsupported offline network policy')
    if _installed:
        return True
    folder = Path(os.environ[AUDIT_ENV])
    if not folder.is_absolute():
        raise ValueError('Absolute private offline audit directory required')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{os.getpid()}_{uuid4().hex}.jsonl'
    stream = path.open('x', encoding='utf-8', buffering=1)

    def record(event, outcome, probe=False):
        # No addresses, command lines, environment, credentials or SDK fields.
        stream.write(json.dumps({'schema_version': 'saferoute-m3-offline-audit/1',
            'pid': os.getpid(), 'role': role, 'event': event, 'outcome': outcome,
            'self_probe': probe}) + '\n')

    probing = False

    def hook(event, args):
        allowed = None
        if event in ('socket.connect', 'socket.bind', 'socket.sendto', 'socket.sendmsg'):
            address = args[1]
            allowed = isinstance(address, tuple) and bool(address) and loopback(address[0])
        elif event in ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr'):
            allowed = loopback(args[0])
        elif event == 'socket.getnameinfo':
            address = args[0]
            allowed = isinstance(address, tuple) and bool(address) and loopback(address[0])
        elif event == 'socket.__new__':
            allowed = args[1] in (socket.AF_INET, socket.AF_INET6)
        elif deny_children and event in ('subprocess.Popen', 'os.system', 'os.exec', 'os.spawn', 'os.posix_spawn'):
            allowed = False
        if allowed is not None:
            if not allowed:
                record(event, 'BLOCKED', probing)
                raise OfflineNetworkBlocked('OFFLINE_EXTERNAL_ACCESS_BLOCKED')
            if event != 'socket.__new__':
                record(event, 'LOOPBACK_ALLOWED', probing)

    sys.addaudithook(hook)
    _installed = True
    record('policy', 'INSTALLED')
    # Denial is tested in every guarded process before application/SDK imports.
    probing = True
    probes = [lambda: socket.getaddrinfo('offline-probe.invalid', 443),
        lambda: socket.socket().connect(('203.0.113.1', 443)),
        lambda: socket.socket(socket.AF_INET6).connect(('2001:db8::1', 443)),
        lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'probe', ('203.0.113.1', 443))]
    try:
        for probe in probes:
            try:
                probe()
            except OfflineNetworkBlocked:
                continue
            raise RuntimeError('Offline policy denial self-test failed')
    finally:
        probing = False
    record('self_test', 'PASS')
    return True


def protect_sdk_children(installation):
    """Route the pinned SDK's one native worker through the guarded entry.

    The pinned SDK source is unchanged. Other executable/module launches from
    its bridge fail closed in offline mode.
    """
    import subprocess
    original = subprocess.Popen
    entry = Path(__file__).with_name('offline_worker_entry.py')
    installation = Path(installation).resolve(strict=True)

    def guarded(args, *positional, **kwargs):
        if (not isinstance(args, (list, tuple)) or len(args) < 3
                or Path(args[0]).resolve() != Path(sys.executable).resolve()
                or list(args[1:3]) != ['-m', 'optimization.runtime.worker_v2']
                or kwargs.get('shell', False) or kwargs.get('env') is not None):
            raise OfflineNetworkBlocked('OFFLINE_UNGUARDED_CHILD_BLOCKED')
        command = [sys.executable, '-I', '-B', str(entry), '--installation', str(installation), *args[3:]]
        return original(command, *positional, **kwargs)

    subprocess.Popen = guarded
