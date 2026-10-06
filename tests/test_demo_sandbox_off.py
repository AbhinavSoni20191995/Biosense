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
        if self.path.startswith('/v1/deny'):
            out = json.dumps({'type': 'error', 'error': {
                'type': 'authentication_error', 'message': 'invalid bearer token'}}).encode()
            self.send_response(401)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
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

    def test_a_refusal_reaches_the_caller_intact_and_is_logged_without_headers(self):
        import io
        err = io.StringIO()
        with mock.patch.object(MP.sys, 'stderr', err):
            code, body = self.post('/v1/deny', {})
        self.assertEqual(401, code)
        self.assertEqual('invalid bearer token', json.loads(body)['error']['message'])
        self.assertIn('upstream 401: authentication_error: invalid bearer token', err.getvalue())
        self.assertNotIn(REAL_KEY, err.getvalue())

    def test_claude_codes_reachability_check_is_forwarded(self):
        """Claude Code sends HEAD /api/hello first; the proxy answered 501."""
        _Upstream.do_HEAD = lambda h: (h.send_response(200),
                                       h.send_header('Content-Length', '0'), h.end_headers())
        self.addCleanup(delattr, _Upstream, 'do_HEAD')
        r = urllib.request.Request(self.base + '/api/hello', method='HEAD')
        with urllib.request.urlopen(r, timeout=10) as resp:
            self.assertEqual(200, resp.status)

    def test_a_credential_pasted_with_whitespace_is_trimmed(self):
        """Claude Code trims its credential; a value with a trailing newline
        worked directly and was refused through the proxy."""
        _Upstream.seen = []
        up = _serve(_Upstream)
        proxy = _serve(MP.make_handler(f'  {REAL_KEY}\n', f'http://127.0.0.1:{up.server_address[1]}',
                                       MP.Budget(0), 'oauth'))
        try:
            r = urllib.request.Request(f'http://127.0.0.1:{proxy.server_address[1]}/v1/m',
                                       data=b'{}', method='POST')
            urllib.request.urlopen(r, timeout=10).read()
        finally:
            for srv in (proxy, up):
                srv.shutdown()
                srv.server_close()
        self.assertEqual(f'Bearer {REAL_KEY}', _Upstream.seen[-1]['authorization'])

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


class ProxyNotRunningTests(unittest.TestCase):
    """When the credential cannot be separated, the card says why and the fix."""

    def state(self, why):
        return RT.agent_sandbox_state({'BIOSENSE_AGENT_SANDBOX': 'off',
                                       'BIOSENSE_MODEL_PROXY_WHY': why})

    def test_each_reason_has_its_own_words_and_fix(self):
        s = self.state('not_root:1000')
        self.assertEqual('exposed', s['model_key'])
        self.assertIn('user 1000', s['note'])
        self.assertIn('RAILWAY_RUN_UID=0', s['note'])
        self.assertIn('start command', self.state('entrypoint_skipped')['note'])
        none = self.state('no_credential')
        self.assertEqual('none', none['model_key'])
        self.assertIn('[model-proxy]', self.state('proxy_failed')['note'])

    def test_a_login_token_is_attached_as_a_bearer_token(self):
        _Upstream.seen = []
        up = _serve(_Upstream)
        proxy = _serve(MP.make_handler(REAL_KEY, f'http://127.0.0.1:{up.server_address[1]}',
                                       MP.Budget(0), 'oauth'))
        try:
            r = urllib.request.Request(
                f'http://127.0.0.1:{proxy.server_address[1]}/v1/messages', data=b'{}',
                method='POST', headers={'Authorization': 'Bearer placeholder',
                                        'x-api-key': 'placeholder'})
            urllib.request.urlopen(r, timeout=10).read()
        finally:
            for srv in (proxy, up):
                srv.shutdown()
                srv.server_close()
        seen = _Upstream.seen[-1]
        self.assertEqual(f'Bearer {REAL_KEY}', seen['authorization'])
        self.assertNotIn('x-api-key', seen)


class SandboxOffStateTests(unittest.TestCase):
    def test_on_is_the_default_and_off_is_reported_with_where_the_key_is(self):
        self.assertEqual('on', RT.agent_sandbox_state({})['sandbox'])
        off = RT.agent_sandbox_state({'BIOSENSE_AGENT_SANDBOX': 'off',
                                      'BIOSENSE_MODEL_PROXY': 'on'})
        self.assertEqual(('off', 'proxy'), (off['sandbox'], off['model_key']))
        self.assertIn('Demo deployment', off['note'])
        bare = RT.agent_sandbox_state({'BIOSENSE_AGENT_SANDBOX': 'off'})
        self.assertEqual('exposed', bare['model_key'])
        self.assertEqual('unknown', bare['proxy_why'])
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
        self.assertIn("PLACEHOLDER='held-by-the-biosense-model-proxy'", text)
        self.assertIn('export ANTHROPIC_API_KEY="$PLACEHOLDER"', text)
        self.assertIn('export CLAUDE_CODE_OAUTH_TOKEN="$PLACEHOLDER"', text)
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

    def test_everything_already_on_the_volume_is_handed_to_the_app_user(self):
        """A root-owned folder inside the volume refused the app's writes:
        "Permission denied" creating a project on Railway."""
        text = (ROOT / 'deploy' / 'entrypoint-ai.sh').read_text()
        self.assertRegex(text, r'find "\$APP_ROOT/data" -xdev .*! -user biosense')
        self.assertIn('-exec chown -h biosense:biosense {} +', text)
        # ...and that happens before the drop to the app user.
        self.assertLess(text.index('find "$APP_ROOT/data"'),
                        text.index('exec setpriv --reuid=biosense'))

    def _off_copy(self):
        """The bundle as start-ai.sh rewrites it, using its own sed expressions."""
        boot = (ROOT / 'deploy' / 'start-ai.sh').read_text()
        exprs = re.findall(r"-e '([^']+)'", boot)
        self.assertEqual(2, len(exprs), 'the sandbox type and the network line')
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(ROOT / 'discovery_loop', tmp / 'discovery_loop')
        for cfg in (tmp / 'discovery_loop').rglob('config.yaml'):
            cmd = ['sed', '-i']
            for e in exprs:
                cmd += ['-e', e]
            subprocess.run(cmd + [str(cfg)], check=True)
        return tmp

    def test_sandbox_off_changes_only_the_sandbox_lines_in_a_copy(self):
        tmp = self._off_copy()
        changed = 0
        for cfg in (tmp / 'discovery_loop').rglob('config.yaml'):
            orig = (ROOT / cfg.relative_to(tmp)).read_text().splitlines()
            new = cfg.read_text().splitlines()
            for a, b in [(a, b) for a, b in zip(orig, new) if a != b]:
                self.assertIn((a.strip(), b.strip()), (('type: auto', 'type: none'),
                                                       ('allow_network: false',
                                                        'allow_network: true')))
                changed += 1
        # six sandbox types, five network lines (literature already had network)
        self.assertEqual(11, changed)
        # The image's own bundle is untouched.
        self.assertIn('type: auto', (ROOT / 'discovery_loop' / 'config.yaml').read_text())

    def test_every_agent_in_the_off_copy_passes_omnigents_own_sandbox_check(self):
        """`type: none` with `allow_network: false` makes Omnigent refuse every
        command ("sandbox type 'none' cannot restrict network") — the run that
        asked both specialists and could not execute `echo hi`."""
        try:
            from omnigent.inner.sandbox import resolve_sandbox
            from omnigent.spec import parser
        except ImportError:
            self.skipTest('Omnigent is not installed')
        root = self._off_copy() / 'discovery_loop'
        specs = [parser.parse(root)] + [parser.parse(d) for d in sorted((root / 'agents').iterdir())]
        for spec in specs:
            policy = resolve_sandbox(spec.os_env, ROOT)
            self.assertFalse(policy.active)
        # And the system check, pointed at that registered copy, passes it —
        # while the copy the old boot script made is caught.
        c = SC.Check(ROOT, ROOT, sandboxed=False, bind_proc=False, python='python')
        with mock.patch.dict(os.environ, {'BIOSENSE_AGENT_BUNDLE': str(root)}):
            SC.check_bundle(c)
        self.assertEqual('ok', c.rows[-1]['status'], c.rows[-1]['detail'])
        for cfg in root.rglob('config.yaml'):
            cfg.write_text(cfg.read_text().replace('allow_network: true',
                                                   'allow_network: false'))
        with mock.patch.dict(os.environ, {'BIOSENSE_AGENT_BUNDLE': str(root)}):
            SC.check_bundle(c)
        self.assertEqual('fail', c.rows[-1]['status'])
        self.assertIn('cannot restrict network', c.rows[-1]['detail'])


if __name__ == '__main__':
    unittest.main()
