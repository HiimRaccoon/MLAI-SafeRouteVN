import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest
from backend.services.offline_guard import loopback, protect_sdk_children, OfflineNetworkBlocked


@pytest.mark.parametrize('host', ['127.0.0.1', '127.44.2.3', '::1', '::ffff:127.0.0.1', 'localhost', 'LOCALHOST', b'127.0.0.1'])
def test_only_loopback_addresses_are_allowed(host):
    assert loopback(host)


@pytest.mark.parametrize('host', ['0.0.0.0', '::', '203.0.113.1', '2001:db8::1', 'example.org', '127.0.0.1.example.org', 'localhost.', '', None, 2130706433, b'\xff'])
def test_external_wildcard_or_ambiguous_addresses_are_blocked(host):
    assert not loopback(host)


def test_real_socket_policy_blocks_external_calls_and_allows_local_http(tmp_path):
    code = '''import json,socket,threading,urllib.request
from http.server import HTTPServer,BaseHTTPRequestHandler
from backend.services.offline_guard import install_from_environment,OfflineNetworkBlocked
assert install_from_environment('unit-process') is True
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'local-only')
 def log_message(self,*args): pass
server=HTTPServer(('127.0.0.1',0),Handler)
thread=threading.Thread(target=server.handle_request);thread.start()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
assert opener.open('http://127.0.0.1:'+str(server.server_port)).read()==b'local-only'
thread.join();server.server_close()
assert socket.getaddrinfo('localhost',80)
for operation in (lambda:socket.gethostbyname('offline-probe.invalid'),lambda:socket.socket().bind(('0.0.0.0',0)),lambda:socket.socket().connect_ex(('203.0.113.1',443)),lambda:socket.socket(socket.AF_INET,socket.SOCK_DGRAM).sendto(b'x',('203.0.113.1',443))):
 try: operation()
 except OfflineNetworkBlocked: continue
 raise AssertionError('external access escaped policy')
print('REAL_OFFLINE_SOCKET_POLICY_PASS')
'''
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=root,
        env={**os.environ, 'SAFEROUTE_OFFLINE_MODE': 'loopback-only', 'SAFEROUTE_OFFLINE_AUDIT_DIR': str(tmp_path)}, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode()
    assert b'REAL_OFFLINE_SOCKET_POLICY_PASS' in result.stdout
    rows = [json.loads(line) for file in tmp_path.glob('*.jsonl') for line in file.read_text().splitlines()]
    assert sum(row['outcome'] == 'BLOCKED' and row['self_probe'] for row in rows) == 4
    assert any(row['outcome'] == 'LOOPBACK_ALLOWED' and not row['self_probe'] for row in rows)
    assert sum(row['outcome'] == 'BLOCKED' and not row['self_probe'] for row in rows) == 4


def test_unknown_policy_fails_before_any_network(tmp_path):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, '-B', '-c', "from backend.services.offline_guard import install_from_environment; install_from_environment('invalid')"], cwd=root,
        env={**os.environ, 'SAFEROUTE_OFFLINE_MODE': 'invalid', 'SAFEROUTE_OFFLINE_AUDIT_DIR': str(tmp_path)}, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert b'Unsupported offline network policy' in result.stderr
    assert not list(tmp_path.glob('*.jsonl'))


def test_native_child_is_rewritten_to_isolated_guarded_entry(monkeypatch, tmp_path):
    config = tmp_path / 'installation.json'
    config.write_text('{}')
    original = Mock(return_value='native-child')
    monkeypatch.setattr(subprocess, 'Popen', original)
    protect_sdk_children(config)
    command = [sys.executable, '-m', 'optimization.runtime.worker_v2', '--input', 'input.json', '--output', 'out.json']
    assert subprocess.Popen(command, cwd=tmp_path) == 'native-child'
    argv = original.call_args.args[0]
    assert argv[:3] == [sys.executable, '-I', '-B']
    assert Path(argv[3]).name == 'offline_worker_entry.py'
    assert argv[4:6] == ['--installation', str(config.resolve())]
    assert argv[6:] == command[3:]


@pytest.mark.parametrize('command,kwargs', [(['curl', 'https://example.org'], {}), ([sys.executable, '-m', 'http.server'], {}), ([sys.executable, '-m', 'optimization.runtime.worker_v2'], {'shell': True}), ([sys.executable, '-m', 'optimization.runtime.worker_v2'], {'env': {}})])
def test_sdk_cannot_spawn_an_unguarded_command(monkeypatch, tmp_path, command, kwargs):
    config = tmp_path / 'installation.json'
    config.write_text('{}')
    original = Mock()
    monkeypatch.setattr(subprocess, 'Popen', original)
    protect_sdk_children(config)
    with pytest.raises(OfflineNetworkBlocked):
        subprocess.Popen(command, **kwargs)
    original.assert_not_called()
