"""Hold the model key in a process the agents cannot read, and forward to the API.

Used only when a deployment has turned the agents' OS sandbox off (Railway, whose
containers refuse the user namespaces bubblewrap needs). Without the sandbox an
agent's shell command runs as the same user as everything else and could print
the environment — so the real `ANTHROPIC_API_KEY` must not be in it.

This process runs as a **different user** (`keyholder`), started by the image's
entrypoint before it drops to the `biosense` user for everything else. It is the
only process that holds the key. The runtime gets `ANTHROPIC_BASE_URL` pointing
here and a placeholder key; every request is forwarded to the API with the
placeholder replaced. An agent can still *use* the model through this address —
it is the model the agents already have — but it cannot read the key, so it
cannot take it anywhere. A daily request ceiling bounds what a runaway loop can
spend.

Standard library only, so it starts before, and independently of, everything else.

    python -m biosense.production.model_proxy --port 6790
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

KEY_ENV = 'ANTHROPIC_API_KEY'
UPSTREAM = 'https://api.anthropic.com'
DEFAULT_DAILY_REQUESTS = 5000
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te',
       'trailers', 'transfer-encoding', 'upgrade', 'host', 'content-length'}
# Never forwarded from the caller: the credential is the proxy's to set.
CREDENTIAL_HEADERS = {'x-api-key', 'authorization'}
TIMEOUT_S = 600


class Budget:
    """Requests per UTC day. A ceiling on a loop, not a pricing model."""

    def __init__(self, limit):
        self.limit, self.day, self.used = limit, None, 0
        self.lock = threading.Lock()

    def take(self):
        with self.lock:
            today = time.strftime('%Y-%m-%d', time.gmtime())
            if today != self.day:
                self.day, self.used = today, 0
            if self.limit and self.used >= self.limit:
                return False
            self.used += 1
            return True


def make_handler(key, upstream, budget):
    up = urlsplit(upstream)
    conn_cls = http.client.HTTPSConnection if up.scheme == 'https' else http.client.HTTPConnection

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):  # one line per request, never a header
            sys.stderr.write(f'[model-proxy] {self.command} {self.path.split("?")[0]} '
                             f'{args[1] if len(args) > 1 else ""}\n')

        def _refuse(self, code, message):
            body = json.dumps({'type': 'error', 'error': {
                'type': 'biosense_model_proxy', 'message': message}}).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _forward(self):
            if self.path == '/healthz':
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not budget.take():
                return self._refuse(429, 'the daily model request ceiling for this demo '
                                         'deployment is reached; it resets at 00:00 UTC')
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in HOP and k.lower() not in CREDENTIAL_HEADERS}
            headers['x-api-key'] = key
            headers['Host'] = up.netloc
            conn = conn_cls(up.hostname, up.port, timeout=TIMEOUT_S)
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
            except OSError as e:
                conn.close()
                return self._refuse(502, f'the model API could not be reached: '
                                         f'{type(e).__name__}')
            try:
                self.send_response(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                length = resp.getheader('Content-Length')
                if length is not None:
                    self.send_header('Content-Length', length)
                    self.end_headers()
                    while True:
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                else:
                    # Streamed (server-sent events): pass each piece on as it
                    # arrives, so the agent sees the model's output live.
                    self.send_header('Transfer-Encoding', 'chunked')
                    self.end_headers()
                    while True:
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(b'%x\r\n%s\r\n' % (len(chunk), chunk))
                        self.wfile.flush()
                    self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                conn.close()

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _forward

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=6790)
    ap.add_argument('--upstream', default=os.environ.get('BIOSENSE_MODEL_PROXY_UPSTREAM',
                                                         UPSTREAM))
    a = ap.parse_args(argv)
    if a.host not in ('127.0.0.1', 'localhost', '::1'):
        sys.exit('the model proxy listens on loopback only')
    key = os.environ.pop(KEY_ENV, '')
    if not key:
        sys.exit(f'{KEY_ENV} is not set for the model proxy; nothing to hold')
    limit = int(os.environ.get('BIOSENSE_MODEL_PROXY_DAILY_REQUESTS') or DEFAULT_DAILY_REQUESTS)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(key, a.upstream, Budget(limit)))
    srv.daemon_threads = True
    sys.stderr.write(f'[model-proxy] holding the model key; forwarding {a.host}:{a.port} '
                     f'-> {a.upstream}; ceiling {limit} requests/day\n')
    srv.serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
