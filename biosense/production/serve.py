"""Read-only HTTP view of the loop artifacts under runs/.

    uv run --frozen python -m biosense.production.serve --runs runs --static webapp

The agents write JSON documents; this serves them so a browser can watch a loop
without touching the loop. Every handler is a GET, nothing here writes, and the
Omnigent session is not reachable from this process: a reader cannot approve a
protocol, commit a decision or answer a consult, because those are CLI acts with
a named human behind them.

Endpoints
    GET /api/loops              every loop directory, newest first
    GET /api/loops/<loop_id>    one loop: state, decisions, analyses, consults
    GET /healthz                liveness
    GET /<path>                 static files from --static, when given

Two files are never served: anything whose name contains ``truth`` (the
stand-in's hidden answers, which no agent and no dashboard may read) and
anything resolving outside the runs directory.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

LOOP_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
FORBIDDEN = 'truth'
MAX_BYTES = 8 * 1024 * 1024


def _is_forbidden(path):
    return FORBIDDEN in path.name.lower()


def _read_json(path):
    """The file as JSON, or None when it is absent, oversized, forbidden or malformed."""
    if _is_forbidden(path):
        return None
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def list_loops(runs_dir):
    """Every loop directory under *runs_dir*, newest first."""
    out = []
    for d in sorted(Path(runs_dir).iterdir()) if Path(runs_dir).is_dir() else []:
        if not d.is_dir() or not LOOP_ID.match(d.name):
            continue
        state = _read_json(d / 'loop_state.json')
        if state is None:
            continue
        iterations = state.get('iterations', [])
        out.append({
            'loop_id': state.get('loop_id', d.name),
            'dir': d.name,
            'request_id': state.get('request_id'),
            'created_at': state.get('created_at'),
            'max_iterations': state.get('max_iterations'),
            'iterations_used': sum(1 for i in iterations if i.get('advances_iteration')),
            'analyses': len(iterations),
            'verdicts': [i.get('verdict') for i in iterations],
            'autonomy_mode': (state.get('autonomy') or {}).get('mode'),
            'bioreactor_source': (state.get('autonomy') or {}).get('bioreactor_source'),
            'open_consults': sum(1 for c in state.get('consults', []) if c.get('status') == 'open'),
        })
    out.sort(key=lambda r: r.get('created_at') or '', reverse=True)
    return out


def load_loop(runs_dir, loop_id):
    """One loop as a single JSON bundle, or None when there is no such loop.

    Shapes match what the tracker page already ingests: ``state`` is
    loop_state.json, each decision is a decision-NN.json, each analysis is an
    itN/analysis.json. Nothing is reshaped or summarised here.
    """
    if not LOOP_ID.match(loop_id):
        return None
    root = Path(runs_dir).resolve()
    d = (root / loop_id).resolve()
    if not str(d).startswith(str(root) + '/') or not d.is_dir():
        return None
    state = _read_json(d / 'loop_state.json')
    if state is None:
        return None

    decisions = []
    for p in sorted((d / 'decisions').glob('decision-*.json')):
        j = _read_json(p)
        if j is not None:
            decisions.append(j)

    analyses, protocols = [], []
    for stage in sorted(x for x in d.iterdir() if x.is_dir() and x.name != 'decisions' and x.name != 'consults'):
        a = _read_json(stage / 'analysis.json')
        if a is not None:
            analyses.append({'stage': stage.name, 'analysis': a})
        p = _read_json(stage / 'protocol.approved.json')
        if p is not None:
            protocols.append({'stage': stage.name, 'protocol': p})

    # consult-NN.json and consult-NN.answered.json are the same consult at two
    # points in its life; the answered copy is the one worth showing.
    by_id = {}
    for p in sorted((d / 'consults').glob('*.json')):
        j = _read_json(p)
        if j is None:
            continue
        cid = j.get('consult_id', p.stem)
        if cid not in by_id or j.get('answer'):
            by_id[cid] = j
    consults = [by_id[k] for k in sorted(by_id)]

    return {'loop_id': state.get('loop_id', loop_id), 'dir': loop_id, 'state': state,
            'decisions': decisions, 'analyses': analyses, 'protocols': protocols,
            'consults': consults,
            'note': 'Read-only view of files on disk. Approving a protocol, committing a '
                    'decision and answering a consult remain CLI acts with a named person.'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'biosense-serve'
    runs_dir = Path('runs')
    static_dir = None

    def log_message(self, fmt, *args):  # quieter than the default stderr spam
        sys.stderr.write(f'{self.address_string()} {fmt % args}\n')

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, indent=2).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == '/healthz':
            return self._send(200, {'ok': True})
        if path == '/api/loops':
            return self._send(200, list_loops(self.runs_dir))
        m = re.fullmatch(r'/api/loops/([^/]+)', path)
        if m:
            bundle = load_loop(self.runs_dir, m.group(1))
            return self._send(200, bundle) if bundle else self._send(404, {'error': 'no such loop'})
        if path.startswith('/api/'):
            return self._send(404, {'error': 'no such endpoint'})
        return self._static(path)

    def _static(self, path):
        if self.static_dir is None:
            return self._send(404, {'error': 'no static directory configured'})
        root = Path(self.static_dir).resolve()
        target = (root / path.lstrip('/')).resolve()
        if target.is_dir():
            target = target / 'index.html'
        if not str(target).startswith(str(root)) or not target.is_file() or _is_forbidden(target):
            return self._send(404, {'error': 'not found'})
        types = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                 '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
                 '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2'}
        self._send(200, target.read_bytes(), types.get(target.suffix, 'application/octet-stream'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--runs', default='runs', help='directory holding loop directories')
    ap.add_argument('--static', default=None, help='directory of static files to serve at /')
    ap.add_argument('--host', default='127.0.0.1', help='interface to bind (0.0.0.0 in a container)')
    ap.add_argument('--port', type=int, default=8000)
    a = ap.parse_args(argv)
    Handler.runs_dir = Path(a.runs)
    Handler.static_dir = Path(a.static) if a.static else None
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f'serving {a.runs}/ read-only on http://{a.host}:{a.port}'
          + (f' (static: {a.static})' if a.static else ''), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
