"""The prompt-driven web app: type a question, watch the loop, read the report.

    uv run --frozen python -m biosense.production.app --runs runs --static webapp

This is a separate server from `biosense.production.serve` on purpose. That one
is strictly read-only and safe to leave on a public URL. This one starts work, so
it has a different threat model and says so here rather than quietly widening the
other.

What a visitor can do: type a prompt, start a loop against the **synthetic
stand-in** reactor, watch it stream, and read the reasoning report it produces.

What a visitor cannot do, enforced in code and not by convention:

* run anything against a real bioreactor. `engine.preflight` refuses any request
  whose `bioreactor_source` is not `synthetic_standin`, and `prompt.parse` forces
  that field, so there is no input that reaches a wet-lab path;
* approve a protocol as a named person. The approver string every app run records
  says in words that no human approved it;
* reach a private dataset. `/api/datasets` reads the public roots only, the
  static handler refuses any path inside the private data root, and `main`
  refuses a runs or static directory that overlaps it;
* read the stand-in's hidden truth. Simulator mode runs the same reactor model
  by hand and returns instrument channels only -- never the line's true growth
  rate, death rate or clonal fraction;
* read the stand-in's hidden truth. No handler serves a file whose name contains
  `truth`, which is the same rule `serve.py` applies;
* spend model credits. Nothing on this path calls an LLM;
* start unbounded work. Concurrency, queue depth, prompt length and runs per
  client are all capped below, and a finished run's events are dropped after
  `RUN_TTL_S`.

Endpoints

    GET  /healthz                 liveness
    GET  /api/config              limits, the stand-ins on offer, what is refused
    POST /api/parse               prompt -> request plus what was assumed, runs nothing
    POST /api/runs                prompt -> start a loop, returns a run id
    GET  /api/runs                recent runs in this process
    GET  /api/runs/<id>           one run: status, events so far, summary
    GET  /api/runs/<id>/events    Server-Sent Events, live, from `?after=<n>`
    GET  /api/runs/<id>/report    the reasoning report as HTML
    GET  /api/datasets            registered PUBLIC datasets only; private ones are
                                  never listed here and never served
    GET  /api/analysis-tools      the tool registry: what can run, over what, and
                                  what is declared but not implemented
    GET  /api/sim/config          simulator mode: the knobs, stages and limits
    POST /api/sim/run             simulator mode: one condition, no loop, no decision
    POST /api/sim/compare         simulator mode: two conditions side by side
    POST /api/sim/brief           a sandbox condition as a carry-into-a-loop brief
    GET  /api/loops               every loop on disk (same shape as serve.py)
    GET  /api/loops/<id>          one loop on disk
    GET  /<path>                  static files from --static

Runs live in this process's memory and their artifacts on disk under --runs. A
restart loses the event streams and keeps the artifacts, which is the right way
round: the files are the durable record.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import contracts as K
from . import engine as EN
from . import prompt as PR
from . import report as RP
from . import sim_mode as SM
from ..data import registry as DREG
from .serve import LOOP_ID, list_loops, load_loop, _is_forbidden

MAX_PROMPT = 2000
MAX_CONCURRENT = 2            # loops running at once in this process
MAX_RUNS_TRACKED = 40         # event streams kept in memory
RUN_TTL_S = 60 * 60           # a finished run's events are dropped after this
MAX_EVENTS_PER_RUN = 4000
POLL_SLEEP_S = 0.25
SSE_IDLE_PING_S = 15
RUN_ID = re.compile(r'^[0-9a-f]{8,32}$')
MAX_BODY = MAX_PROMPT * 8       # a compare payload carries two full conditions
MAX_CONCURRENT_SIM = 4          # simulator-mode requests served at once
# A simulator-mode request is CPU-bound and about a tenth of a second, so it is
# bounded by a semaphore rather than the registry: it starts no loop, writes
# nothing to disk and holds no state between calls.
SIM_GATE = threading.BoundedSemaphore(MAX_CONCURRENT_SIM)
STANDINS = ('ipsc_tcell', 'tcell', 'monocyte')
# Truth files for the stand-ins the app offers. The engine reads these; no
# handler ever serves one.
TRUTH_FOR = {
    'ipsc_tcell': K.ROOT / 'examples' / 'ipsc_tcell' / 'standin_truth.synthetic.json',
    'tcell': K.ROOT / 'examples' / 'cart' / 'standin_truth.synthetic.json',
}


class Run:
    """One loop in flight, plus its event log."""

    def __init__(self, run_id, prompt_text, request, provenance, loop_dir, standin):
        self.id = run_id
        self.prompt = prompt_text
        self.request = request
        self.provenance = provenance
        self.loop_dir = Path(loop_dir)
        self.standin = standin
        self.events = []
        self.status = 'queued'
        self.summary = None
        self.error = None
        self.started_at = time.time()
        self.finished_at = None
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)

    def add(self, event):
        with self.cv:
            if len(self.events) < MAX_EVENTS_PER_RUN:
                self.events.append(dict(event, seq=len(self.events), t=round(
                    time.time() - self.started_at, 2)))
            self.cv.notify_all()

    def finish(self, status, summary=None, error=None):
        with self.cv:
            self.status = status
            self.summary = summary
            self.error = error
            self.finished_at = time.time()
            self.cv.notify_all()

    def snapshot(self, after=-1):
        with self.lock:
            return {
                'run_id': self.id, 'status': self.status, 'prompt': self.prompt,
                'loop_dir': self.loop_dir.name, 'standin': self.standin,
                'request_id': self.request['request_id'],
                'question': self.request['question'],
                'arms': [a['arm_id'] for a in self.request['genotype_arms']],
                'target': f'{self.request["desired_output"]["metric"]} >= '
                          f'{self.request["desired_output"]["value"]} '
                          f'{self.request["desired_output"]["unit"]} at day '
                          f'{self.request["desired_output"]["at_day"]}',
                'assumed': self.provenance['assumed'],
                'read_from_prompt': self.provenance['read_from_prompt'],
                'events': [e for e in self.events if e['seq'] > after],
                'event_count': len(self.events),
                'summary': self.summary, 'error': self.error,
                'elapsed_s': round((self.finished_at or time.time()) - self.started_at, 1),
            }


class Registry:
    def __init__(self, runs_dir):
        self.runs_dir = Path(runs_dir)
        self.runs = {}
        self.order = []
        self.lock = threading.Lock()
        self.active = 0

    def _evict(self):
        now = time.time()
        for rid in list(self.order):
            r = self.runs.get(rid)
            if r is None:
                self.order.remove(rid)
            elif r.finished_at and now - r.finished_at > RUN_TTL_S:
                with r.lock:
                    r.events = []
                self.runs.pop(rid, None)
                self.order.remove(rid)
        while len(self.order) > MAX_RUNS_TRACKED:
            rid = self.order.pop(0)
            self.runs.pop(rid, None)

    def start(self, prompt_text):
        request, provenance = PR.parse(prompt_text)
        standin = provenance['standin']
        with self.lock:
            self._evict()
            if self.active >= MAX_CONCURRENT:
                raise K.ContractError(
                    f'{self.active} loops are already running and the limit is {MAX_CONCURRENT}. '
                    f'Wait for one to finish, or watch it at /api/runs.')
            rid = uuid.uuid4().hex[:16]
            stamp = time.strftime('%Y%m%d-%H%M%S')
            loop_dir = self.runs_dir / f'app-{stamp}-{rid[:6]}'
            run = Run(rid, prompt_text, request, provenance, loop_dir, standin)
            self.runs[rid] = run
            self.order.append(rid)
            self.active += 1
        t = threading.Thread(target=self._work, args=(run,), daemon=True,
                             name=f'loop-{rid[:6]}')
        t.start()
        return run

    def _work(self, run):
        try:
            run.status = 'running'
            run.add({'kind': 'accepted', 'request_id': run.request['request_id'],
                     'loop_dir': run.loop_dir.name, 'standin': run.standin,
                     'assumed': len(run.provenance['assumed']),
                     'note': 'Synthetic stand-in reactor. Nothing here is biological evidence.'})
            truth_path = TRUTH_FOR.get(run.standin)
            truth = K.read_json(truth_path) if truth_path and truth_path.exists() else None
            if truth is None:
                run.add({'kind': 'note', 'detail':
                         f'No hidden-truth file for the {run.standin} stand-in, so every arm runs '
                         f'the same baseline biology and a genotype comparison cannot separate '
                         f'them. The loop still demonstrates the workflow.'})
            summary = EN.safe_run_loop(run.request, run.loop_dir, standin=run.standin,
                                       truth=truth, on_event=run.add)
            report_html = run.loop_dir / 'reasoning_report.html'
            try:
                RP.write_report(run.loop_dir, report_html,
                                title=f'Reasoning report: {run.loop_dir.name}')
                run.add({'kind': 'report', 'path': report_html.name,
                         'url': f'/api/runs/{run.id}/report'})
            except Exception as e:  # noqa: BLE001 - a failed report must not lose the run
                run.add({'kind': 'error', 'step': 'report', 'detail': f'{type(e).__name__}: {e}'})
            run.finish('done' if summary.get('terminal') not in ('error',) else 'error', summary)
        except K.ContractError as e:
            run.add({'kind': 'refusal', 'step': 'start', 'detail': str(e)})
            run.finish('refused', None, str(e))
        except Exception as e:  # noqa: BLE001
            run.add({'kind': 'error', 'step': 'worker', 'detail': f'{type(e).__name__}: {e}'})
            run.finish('error', None, f'{type(e).__name__}: {e}')
        finally:
            with self.lock:
                self.active = max(0, self.active - 1)

    def get(self, rid):
        return self.runs.get(rid) if RUN_ID.match(rid or '') else None

    def recent(self):
        with self.lock:
            return [self.runs[r].snapshot(after=10 ** 9) for r in reversed(self.order)
                    if r in self.runs]


class Handler(BaseHTTPRequestHandler):
    server_version = 'biosense-app'
    protocol_version = 'HTTP/1.1'
    registry = None
    runs_dir = Path('runs')
    static_dir = None

    def log_message(self, fmt, *args):
        sys.stderr.write(f'{self.address_string()} {fmt % args}\n')

    def _send(self, code, body, ctype='application/json; charset=utf-8', extra=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, indent=2, default=str).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            return {}

    def _prompt(self):
        p = (self._body().get('prompt') or '').strip()
        if not p:
            raise K.ContractError('send {"prompt": "..."} as JSON')
        if len(p) > MAX_PROMPT:
            raise K.ContractError(f'the prompt is {len(p)} characters; the limit is {MAX_PROMPT}')
        return p

    # ── POST ────────────────────────────────────────────────
    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == '/api/parse':
                request, prov = PR.parse(self._prompt())
                return self._send(200, {'request': request, 'provenance': prov,
                                        'summary_text': PR.render(prov)})
            if path == '/api/runs':
                run = self.registry.start(self._prompt())
                return self._send(202, run.snapshot())
            # Simulator mode. A person turns the knobs, so there is no decision to
            # validate and no iteration to spend: these run the model and return
            # what it read. They are capped like any other work this server starts.
            if path == '/api/sim/run':
                with SIM_GATE:
                    return self._send(200, SM.simulate(self._body()))
            if path == '/api/sim/compare':
                with SIM_GATE:
                    return self._send(200, SM.compare(self._body()))
            if path == '/api/sim/brief':
                with SIM_GATE:
                    return self._send(200, SM.seed_brief(SM.simulate(self._body())))
            return self._send(404, {'error': 'no such endpoint'})
        except K.ContractError as e:
            return self._send(400, {'error': str(e), 'refused': True})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {'error': f'{type(e).__name__}: {e}'})

    # ── GET ─────────────────────────────────────────────────
    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)
        if path == '/healthz':
            return self._send(200, {'ok': True, 'active': self.registry.active})
        if path == '/api/config':
            return self._send(200, {
                'max_prompt_chars': MAX_PROMPT, 'max_concurrent': MAX_CONCURRENT,
                'standins': list(STANDINS),
                'max_concurrent_sim': MAX_CONCURRENT_SIM,
                'simulator_mode': '/api/sim/config',
                'standins_with_hidden_truth': sorted(k for k, v in TRUTH_FOR.items()
                                                     if v.exists()),
                'bioreactor_source': 'synthetic_standin',
                'refuses': [
                    'any request whose bioreactor_source is not synthetic_standin',
                    'self-approving a protocol when the gates require a named human',
                    'serving any file whose name contains "truth"',
                    'serving or listing any dataset a person registered privately',
                    'calling a model: nothing on this path spends credits',
                ],
                'note': 'This server starts work. Its runs are synthetic-stand-in only and no '
                        'number it produces is biological evidence.'})
        if path == '/api/datasets':
            # include_private=False is applied to the ROOTS that are read, not to
            # the rows that come back, so a bug in a row filter cannot leak one.
            return self._send(200, {
                'datasets': [DREG.summary(m) for m in
                             DREG.list_datasets(include_private=False)],
                'note': 'Public and fixture datasets only. Datasets a person registered '
                        'privately are never listed or served by this process; they are '
                        'visible to the CLI on the machine that holds them.',
                'private_listed': False})
        if path == '/api/analysis-tools':
            from ..bioinformatics import registry as TREG
            from ..bioinformatics import external as EXT
            d = TREG.describe()
            d['external_adapters'] = EXT.describe()
            d['note'] = ('Implemented tools run in process on numpy and scipy. Planned entries '
                         'are declared so the shape is visible; calling one is refused.')
            return self._send(200, d)
        if path == '/api/sim/config':
            return self._send(200, SM.config())
        if path == '/api/runs':
            return self._send(200, self.registry.recent())
        m = re.fullmatch(r'/api/runs/([^/]+)', path)
        if m:
            run = self.registry.get(m.group(1))
            if not run:
                return self._send(404, {'error': 'no such run in this process'})
            after = int((q.get('after') or ['-1'])[0] or -1)
            return self._send(200, run.snapshot(after=after))
        m = re.fullmatch(r'/api/runs/([^/]+)/events', path)
        if m:
            return self._sse(m.group(1), int((q.get('after') or ['-1'])[0] or -1))
        m = re.fullmatch(r'/api/runs/([^/]+)/report', path)
        if m:
            run = self.registry.get(m.group(1))
            if not run:
                return self._send(404, {'error': 'no such run in this process'})
            p = run.loop_dir / 'reasoning_report.html'
            if not p.is_file():
                return self._send(404, {'error': 'the report is not written yet'})
            return self._send(200, p.read_bytes(), 'text/html; charset=utf-8')
        if path == '/api/loops':
            return self._send(200, list_loops(self.runs_dir))
        m = re.fullmatch(r'/api/loops/([^/]+)', path)
        if m:
            b = load_loop(self.runs_dir, m.group(1))
            return self._send(200, b) if b else self._send(404, {'error': 'no such loop'})
        m = re.fullmatch(r'/api/loops/([^/]+)/report', path)
        if m and LOOP_ID.match(m.group(1)):
            root = Path(self.runs_dir).resolve()
            p = (root / m.group(1) / 'reasoning_report.html').resolve()
            if str(p).startswith(str(root) + '/') and p.is_file():
                return self._send(200, p.read_bytes(), 'text/html; charset=utf-8')
            return self._send(404, {'error': 'no report for that loop'})
        if path.startswith('/api/'):
            return self._send(404, {'error': 'no such endpoint'})
        return self._static(path)

    def _sse(self, rid, after):
        run = self.registry.get(rid)
        if not run:
            return self._send(404, {'error': 'no such run in this process'})
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        sent = after
        last_ping = time.time()
        try:
            while True:
                with run.cv:
                    pending = [e for e in run.events if e['seq'] > sent]
                    done = run.finished_at is not None
                    if not pending and not done:
                        run.cv.wait(timeout=POLL_SLEEP_S)
                        pending = [e for e in run.events if e['seq'] > sent]
                        done = run.finished_at is not None
                for e in pending:
                    self.wfile.write(f'id: {e["seq"]}\ndata: {json.dumps(e, default=str)}\n\n'
                                     .encode())
                    sent = e['seq']
                if pending:
                    self.wfile.flush()
                    last_ping = time.time()
                elif time.time() - last_ping > SSE_IDLE_PING_S:
                    self.wfile.write(b': keep-alive\n\n')
                    self.wfile.flush()
                    last_ping = time.time()
                if done and not [e for e in run.events if e['seq'] > sent]:
                    final = json.dumps({'kind': 'closed', 'status': run.status,
                                        'summary': run.summary, 'error': run.error},
                                       default=str)
                    self.wfile.write(f'event: closed\ndata: {final}\n\n'.encode())
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _static(self, path):
        if self.static_dir is None:
            return self._send(404, {'error': 'no static directory configured'})
        root = Path(self.static_dir).resolve()
        target = (root / path.lstrip('/')).resolve()
        if target.is_dir():
            # This server's landing page is the prompt console. The tracker stays
            # at /index.html, which is also what serve.py puts at its own root.
            target = target / ('console.html' if (target / 'console.html').is_file()
                               else 'index.html')
        if not str(target).startswith(str(root)) or not target.is_file() or _is_forbidden(target):
            return self._send(404, {'error': 'not found'})
        from ..data import roots as DR
        if DR.is_private_path(target):
            return self._send(404, {'error': 'not found'})
        types = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                 '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
                 '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2',
                 '.pdf': 'application/pdf'}
        return self._send(200, target.read_bytes(),
                          types.get(target.suffix, 'application/octet-stream'))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--runs', default='runs', help='where loop directories are written')
    ap.add_argument('--static', default='webapp', help='directory of static files served at /')
    ap.add_argument('--host', default='127.0.0.1', help='interface to bind (0.0.0.0 in a container)')
    ap.add_argument('--port', type=int, default=8000)
    a = ap.parse_args(argv)
    from ..data import roots as DR
    DR.assert_disjoint(a.runs)
    if a.static:
        DR.assert_disjoint(a.static)
    runs = Path(a.runs)
    runs.mkdir(parents=True, exist_ok=True)
    Handler.runs_dir = runs
    Handler.static_dir = Path(a.static) if a.static else None
    Handler.registry = Registry(runs)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f'BioSense app on http://{a.host}:{a.port}  (runs: {a.runs}, static: {a.static})\n'
          f'synthetic stand-in only; nothing here calls a model or reaches a bioreactor',
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
