"""A demo deployment with the agents' sandbox off, and the key kept out of reach.

Railway refuses the user namespaces bubblewrap needs, so for the hackathon demo
the operator turns the agents' OS sandbox off (BIOSENSE_AGENT_SANDBOX=off). That
is a decision, so it is reported everywhere a run can be started, and the one
secret worth stealing — the model key — is moved into a process the agents
cannot read. These tests pin both halves.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from biosense import contracts as K
from biosense.production import model_proxy as MP
from biosense.production import runtime as RT
from biosense.production import selfcheck as SC

ROOT = K.ROOT
REAL_KEY = 'the-real-key-only-the-proxy-holds'


class _Upstream(BaseHTTPRequestHandler):
    """Stands in for the model API: records headers, streams or not."""
    protocol_version = 'HTTP/1.1'
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        _Upstream.seen.append({k.lower(): v for k, v in self.headers.items()})
        if self.path.startswith('/v1/stream'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            for piece in (b'event: a\ndata: 1\n\n', b'event: b\ndata: 2\n\n'):
                self.wfile.write(b'%x\r\n%s\r\n' % (len(piece), piece))
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            return
        out = json.dumps({'echo': json.loads(body or b'{}')}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def _serve(handler):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class ModelProxyTests(unittest.TestCase):
    def setUp(self):
        _Upstream.seen = []
        self.up = _serve(_Upstream)
        upstream = f'http://127.0.0.1:{self.up.server_address[1]}'
        self.budget = MP.Budget(3)
        self.proxy = _serve(MP.make_handler(REAL_KEY, upstream, self.budget))
        self.base = f'http://127.0.0.1:{self.proxy.server_address[1]}'

    def tearDown(self):
        for s in (self.proxy, self.up):
            s.shutdown()
            s.server_close()

    def post(self, path, body, headers=None):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                   method='POST', headers={'Content-Type': 'application/json',
                                                           **(headers or {})})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_the_real_key_replaces_whatever_the_caller_sent(self):
        code, body = self.post('/v1/messages', {'q': 1}, {
            'x-api-key': 'held-by-the-biosense-model-proxy',
            'Authorization': 'Bearer something-an-agent-made-up',
            'anthropic-version': '2023-06-01'})
        self.assertEqual(200, code)
        self.assertEqual({'echo': {'q': 1}}, json.loads(body))
        seen = _Upstream.seen[-1]
        self.assertEqual(REAL_KEY, seen['x-api-key'])
        self.assertNotIn('authorization', seen)
        self.assertEqual('2023-06-01', seen['anthropic-version'])

    def test_a_streamed_response_arrives_whole_and_in_order(self):
        code, body = self.post('/v1/stream', {})
        self.assertEqual(200, code)
        self.assertEqual(b'event: a\ndata: 1\n\nevent: b\ndata: 2\n\n', body)

    def test_the_daily_ceiling_refuses_with_a_reason(self):
        for _ in range(3):
            self.assertEqual(200, self.post('/v1/messages', {})[0])
        code, body = self.post('/v1/messages', {})
        self.assertEqual(429, code)
        self.assertIn('ceiling', json.loads(body)['error']['message'])

    def test_it_listens_on_loopback_only_and_needs_a_key(self):
        with self.assertRaises(SystemExit):
            MP.main(['--host', '0.0.0.0'])
        with mock.patch.dict(os.environ, {MP.KEY_ENV: ''}), self.assertRaises(SystemExit):
            MP.main(['--port', '0'])


class SandboxOffStateTests(unittest.TestCase):
    def test_on_is_the_default_and_off_is_reported_with_where_the_key_is(self):
        self.assertEqual('on', RT.agent_sandbox_state({})['sandbox'])
        off = RT.agent_sandbox_state({'BIOSENSE_AGENT_SANDBOX': 'off',
                                      'BIOSENSE_MODEL_PROXY': 'on'})
        self.assertEqual(('off', 'proxy'), (off['sandbox'], off['model_key']))
        self.assertIn('Demo deployment', off['note'])
        bare = RT.agent_sandbox_state({'BIOSENSE_AGENT_SANDBOX': 'off'})
        self.assertEqual('exposed', bare['model_key'])
        # Anything but the exact word leaves it on.
        self.assertEqual('on', RT.agent_sandbox_state({'BIOSENSE_AGENT_SANDBOX': 'of'})['sandbox'])

    def test_a_deliberately_off_sandbox_is_not_refused(self):
        cfg = RT.from_env(env={'BIOSENSE_RUNTIME_MODE': 'local', 'BIOSENSE_HOSTED': '1'})
        env = {'BIOSENSE_SANDBOX_CHECK': '', 'BIOSENSE_AGENT_SANDBOX': 'off'}
        self.assertIsNone(RT.check_sandbox(cfg, env=env, trial=lambda: 'user_namespaces'))

    def test_the_self_check_says_it_and_fails_if_the_key_is_exposed(self):
        c = SC.Check(ROOT, ROOT, sandboxed=False, bind_proc=False, python='python')
        with mock.patch.dict(os.environ, {'BIOSENSE_AGENT_SANDBOX': 'off',
                                          'BIOSENSE_MODEL_PROXY': 'on'}):
            SC.check_sandbox(c, {'ok': False}, hosted=True)
        self.assertEqual('warn', c.rows[-1]['status'])
        self.assertIn('OFF by operator choice', c.rows[-1]['detail'])
        with mock.patch.dict(os.environ, {'BIOSENSE_AGENT_SANDBOX': 'off',
                                          'BIOSENSE_MODEL_PROXY': ''}):
            SC.check_sandbox(c, {'ok': False}, hosted=True)
        self.assertEqual('fail', c.rows[-1]['status'])


class BootScriptTests(unittest.TestCase):
    def test_the_entrypoint_gives_the_key_to_the_proxy_alone_then_drops_root(self):
        text = (ROOT / 'deploy' / 'entrypoint-ai.sh').read_text()
        self.assertIn('--reuid=keyholder', text)
        self.assertIn('biosense.production.model_proxy', text)
        self.assertIn("export ANTHROPIC_API_KEY='held-by-the-biosense-model-proxy'", text)
        self.assertIn('export ANTHROPIC_BASE_URL="http://127.0.0.1:${port}"', text)
        # The last thing it does is become biosense, with nothing kept.
        last = [ln for ln in text.splitlines() if ln.strip()][-2:]
        self.assertIn('exec setpriv --reuid=biosense', last[0])
        self.assertIn('--inh-caps=-all --no-new-privs', last[0])
        # The proxy is started only when the operator turned the sandbox off.
        self.assertRegex(text, r'BIOSENSE_AGENT_SANDBOX:-on\}" = \'off\'')
        docker = (ROOT / 'deploy' / 'Dockerfile.ai').read_text()
        self.assertIn('keyholder', docker)
        self.assertIn('CMD ["/app/deploy/entrypoint-ai.sh"]', docker)

    def test_sandbox_off_changes_exactly_the_sandbox_types_in_a_copy(self):
        boot = (ROOT / 'deploy' / 'start-ai.sh').read_text()
        expr = re.search(r"sed -i '([^']+)'", boot).group(1)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(ROOT / 'discovery_loop', tmp / 'discovery_loop')
        for cfg in (tmp / 'discovery_loop').rglob('config.yaml'):
            subprocess.run(['sed', '-i', expr, str(cfg)], check=True)
        changed = 0
        for cfg in (tmp / 'discovery_loop').rglob('config.yaml'):
            orig = (ROOT / cfg.relative_to(tmp)).read_text().splitlines()
            new = cfg.read_text().splitlines()
            diff = [(a, b) for a, b in zip(orig, new) if a != b]
            for a, b in diff:
                self.assertEqual(a.replace('auto', 'none'), b)
            changed += len(diff)
        self.assertEqual(6, changed, 'the orchestrator and its five specialists')
        # The image's own bundle is untouched.
        self.assertIn('type: auto', (ROOT / 'discovery_loop' / 'config.yaml').read_text())


if __name__ == '__main__':
    unittest.main()
