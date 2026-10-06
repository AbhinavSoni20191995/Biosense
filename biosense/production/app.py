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

    GET  /healthz                 liveness: the web process answers
    GET  /readyz                  readiness: whether the runtime this deployment offers
                                  can actually run, in named parts, with no secrets
    GET  /api/config              limits, the stand-ins on offer, what is refused
    POST /api/parse               prompt -> request plus what was assumed, runs nothing
    POST /api/runs                prompt -> start a loop, returns a run id
    GET  /api/runs                recent runs in this process
    GET  /api/runs/<id>           one run: status, events so far, summary
    GET  /api/runs/<id>/events    Server-Sent Events, live, from `?after=<n>`
    GET  /api/runs/<id>/report    the reasoning report as HTML
    POST /api/discovery/<id>/cancel   stop a run in flight, and its Omnigent session
    GET  /api/datasets            registered PUBLIC datasets only; private ones are
                                  never listed here and never served
    GET  /api/analysis-tools      the tool registry: what can run, over what, and
                                  what is declared but not implemented
    GET  /api/hypotheses          quantified hypotheses from built benchmark bundles
    GET  /api/projects            project profiles: which knobs each process has,
                                  with per-project bounds and simulator coverage
    GET  /api/sim/config          simulator mode: the knobs, stages and limits
    POST /api/sim/run             simulator mode: one condition, no loop, no decision
    POST /api/sim/compare         simulator mode: two conditions side by side
    POST /api/sim/brief           a sandbox condition as a carry-into-a-loop brief
    GET  /api/loops               every loop on disk (same shape as serve.py)
    GET  /api/loops/<id>          one loop on disk
    GET  /<path>                  static files from --static

Runs live in this process's memory and their artifacts on disk under --runs.
Every discovery run also journals its own snapshot and events beside those
artifacts (`run_store.py`), so a reloaded browser, or a replaced process, can get
a run back from its id alone — and a run that was in flight when the process went
away comes back as `interrupted` rather than as finished or running. The files are
the durable record; this memory is a view of them.
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
from .. import glossary as GL
from .. import workspace as WS
from . import authz as AZ
from . import budget as BU
from . import discovery as DISC
from . import discovery_runner as DRUN
from . import engine as EN
from . import health as HL
from . import ingest as IN
from . import project_builder as PB
from . import prompt as PR
from . import protocol_summary as PSUM
from . import report as RP
from . import run_store as RS
from . import runtime as RT
from . import sim_mode as SM
from . import stages as ST
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
# A real-AI run costs money and lasts minutes rather than a second, so it gets a
# tighter cap than the synthetic loop and its own queue.
MAX_CONCURRENT_REAL = 1
# Why a run stopped short, in the words the person reading it needs. Both are
# recorded as `stopped`, never as a result: a run that was halted produced no
# answer, and saying otherwise is the one thing this product must never do.
STOP_MESSAGES = {
    'cancelled': 'You stopped this run. The agents were interrupted, so nothing here is a '
                 'finished answer.',
    'timed_out': 'This run reached the deployment\'s time limit for a single real AI run and '
                 'was stopped. Nothing here is a finished answer.',
    'shutdown': 'The server shut down while this run was in flight.',
}
# How long a readiness probe is believed before the server asks the runtime
# again. A health page that re-probes on every request becomes its own load.
READY_CACHE_S = 10
DISCOVERY_ID = re.compile(r'^[0-9a-f]{8,32}$')
MAX_BODY_DISCOVERY = 256 * 1024     # a request carries a context and constraints
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


class DiscoveryRun:
    """One discovery run in flight: its request, its events, and what it produced.

    Deliberately a sibling of `Run` rather than a subclass. The synthetic
    prompt loop and a structured discovery run have different inputs, different
    runtimes and different artifacts, and the one thing that must never happen is
    for one to be presented as the other. Keeping the objects apart is the
    cheapest way to keep that true.
    """

    def __init__(self, run_id, request, out_dir, runtime_mode, *, runtime_label=None,
                 deadline=None, owner=None, credential_mode='platform_demo',
                 started_by_admin=False):
        self.id = run_id
        self.request = request
        self.out_dir = Path(out_dir)
        self.runtime_mode = runtime_mode
        # Who this run belongs to. Every route that can show a run checks it
        # against the caller, so a guessed id reaches nothing: a run id is not a
        # capability. Role is deliberately not part of that check — an operator
        # account is exempt from spending caps, not from other people's privacy.
        self.owner = owner
        self.credential_mode = credential_mode
        self.started_by_admin = bool(started_by_admin)
        # The badge as this deployment says it: a hosted service's loopback
        # runtime is ONLINE to the person reading it, not LOCAL.
        self.runtime_label = runtime_label or RT.LABELS[runtime_mode]
        self.deadline = deadline
        self.cancelled = None       # the reason it was stopped, once it is
        self.events = []
        self.stages_reached = set()
        self.status = 'queued'
        self.result = None
        self.error = None
        self.error_reason = None
        self.next_step = None
        self.session = None
        self.benchmark = None
        self.started_at = time.time()
        self.finished_at = None
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)

    def add(self, event):
        with self.cv:
            new_stage = bool(event.get('stage')) and event['stage'] not in self.stages_reached
            if event.get('stage'):
                self.stages_reached.add(event['stage'])
            row = None
            if len(self.events) < MAX_EVENTS_PER_RUN:
                row = dict(event, seq=len(self.events),
                           t=round(time.time() - self.started_at, 2))
                self.events.append(row)
            self.cv.notify_all()
        # Journalled outside the lock: a slow disk must not stall the stream.
        if row is not None:
            RS.append_event(self.out_dir, row)
        if new_stage:
            self.persist()

    def finish(self, status, result=None, error=None, reason=None, next_step=None):
        with self.cv:
            self.status = status
            self.result = result
            self.error = error
            self.error_reason = reason
            self.next_step = next_step
            self.finished_at = time.time()
            self.cv.notify_all()
        self.persist()

    def persist(self):
        """Write the snapshot beside the artifacts, so a reload can find it.

        Events are already journalled line by line; this is the header. Failure
        is survivable and silent on purpose: losing recoverability is bad, and
        killing a paid-for run because a disk was full would be worse.
        """
        snap = self.snapshot(after=-1)
        snap['events'] = []            # the journal holds those
        RS.write_state(self.out_dir, snap)

    def stop(self, reason, message):
        """Ask this run to stop. The worker notices between events."""
        with self.cv:
            if self.finished_at is not None:
                return False
            self.cancelled = reason
            self.cv.notify_all()
        self.add({'kind': 'note', 'stage': None, 'simple': message, 'technical': reason})
        return True

    def should_stop(self):
        """Polled by the adapter between stream events.

        Two reasons a run stops without finishing, and they are recorded
        differently because they mean different things: somebody pressed stop,
        or the deployment's wall-clock budget ran out.
        """
        if self.cancelled:
            return True
        if self.deadline and time.time() > self.deadline:
            self.cancelled = 'timed_out'
            return True
        return False

    def progress(self):
        terminal = ('complete' if self.status == 'done'
                    else 'failed' if self.status in ('error', 'refused', 'unavailable',
                                                     'stopped', 'interrupted')
                    else None)
        return ST.progress(self.stages_reached, terminal=terminal)

    def snapshot(self, after=-1, *, include_result=True):
        with self.lock:
            return {
                'run_id': self.id, 'status': self.status,
                'runtime_mode': self.runtime_mode,
                'runtime_label': self.runtime_label,
                'is_real': self.runtime_mode in RT.REAL_MODES,
                'request_id': self.request['request_id'],
                'project_id': self.request['project_id'],
                'objective': self.request['objective'],
                'run_dir': self.out_dir.name,
                'progress': self.progress(),
                'events': [e for e in self.events if e['seq'] > after],
                'event_count': len(self.events),
                'omnigent_session_id': (self.session or {}).get('omnigent_session_id'),
                'owner': self.owner,
                'credential_mode': self.credential_mode,
                'started_by_admin': self.started_by_admin,
                'error': self.error, 'error_reason': self.error_reason,
                'next_step': self.next_step,
                'result': self.result if include_result else None,
                'elapsed_s': round((self.finished_at or time.time()) - self.started_at, 1),
                'started_at': self.started_at,
                'deadline_in_s': (round(self.deadline - time.time(), 1)
                                  if self.deadline and not self.finished_at else None),
                'cancellable': self.finished_at is None,
                'recovered': False,
            }


class DiscoveryRegistry:
    """Discovery runs in this process, and the caps that bound them."""

    def __init__(self, runs_dir, runtime_config, limits=None, policy=None):
        self.runs_dir = Path(runs_dir)
        self.cfg = runtime_config
        self.limits = limits or BU.Limits()
        self.policy = policy or AZ.Policy()
        self.ledger = BU.Ledger(self.limits)
        self.runs = {}
        self.order = []
        self.lock = threading.Lock()
        self.active_real = 0
        self.active_synthetic = 0

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
            self.runs.pop(self.order.pop(0), None)

    def start(self, request, *, identity=None, client=None):
        mode = request['runtime_mode']
        target = self.cfg.for_mode(mode)
        if mode not in self.cfg.allowed:
            raise RT.RuntimeUnavailable('not_configured', hosted=self.cfg.hosted)
        role = self.policy.role_for(identity)
        credential_mode = self.policy.credential_mode(identity)
        if target.is_real:
            # Checked before anything is written, so a run that cannot possibly
            # work never appears in the list as if it might.
            from . import omnigent_runtime as OMNI
            OMNI.ensure_available(target)
            # And then the demo budget, which refuses by name and never
            # downgrades the request into a synthetic run. An operator account is
            # exempt here and nowhere else: the concurrency gate below, the
            # deadline, and every size limit apply to them unchanged.
            self.ledger.authorise(client or 'unknown', is_real=True, role=role)
        with self.lock:
            self._evict()
            if target.is_real and self.active_real >= MAX_CONCURRENT_REAL:
                raise K.ContractError(
                    f'a real-AI discovery run is already in flight and the limit is '
                    f'{MAX_CONCURRENT_REAL}. Wait for it, or watch it in the run list.')
            if not target.is_real and self.active_synthetic >= MAX_CONCURRENT:
                raise K.ContractError(
                    f'{self.active_synthetic} runs are already going and the limit is '
                    f'{MAX_CONCURRENT}.')
            rid = uuid.uuid4().hex[:16]
            stamp = time.strftime('%Y%m%d-%H%M%S')
            prefix = 'ai' if target.is_real else 'demo'
            out = self.runs_dir / f'{prefix}-{stamp}-{rid[:6]}'
            run = DiscoveryRun(rid, request, out, mode,
                               runtime_label=self.cfg.label_for(mode),
                               deadline=(self.limits.deadline_from(time.time())
                                         if target.is_real else None),
                               owner=getattr(identity, 'owner', None),
                               credential_mode=credential_mode,
                               started_by_admin=(role == AZ.ROLE_ADMIN))
            self.runs[rid] = run
            self.order.append(rid)
            if target.is_real:
                self.active_real += 1
            else:
                self.active_synthetic += 1
        run.persist()
        t = threading.Thread(target=self._work, args=(run, target), daemon=True,
                             name=f'discovery-{rid[:6]}')
        t.start()
        return run

    def _work(self, run, target):
        try:
            run.status = 'running'
            if target.is_real:
                res = DRUN.run_real(target, run.request, run.out_dir, on_event=run.add,
                                    on_session=lambda sess: setattr(run, 'session',
                                                                    sess.public()),
                                    should_stop=run.should_stop)
                summary = res['omnigent']
                run.session = summary['session']
                final = DRUN.finish(run.request, run.out_dir, runtime_mode=run.runtime_mode,
                                    session=run.session)
                if run.cancelled:
                    # Stopped, not finished. Whatever the agents wrote is kept
                    # and reported as what it is; no partial run is a result.
                    run.finish('stopped', final, STOP_MESSAGES[run.cancelled],
                               reason=run.cancelled,
                               next_step='Start the run again when you want the whole answer.')
                    return
                if summary.get('terminal') == 'failed':
                    # A provider 401 arrives as an ordinary task error. It is not
                    # an ordinary task error: it is the one failure here that has
                    # a specific remedy, and saying "the run failed" instead
                    # sends the reader looking at their question.
                    from . import omnigent_runtime as OMNI
                    err = summary.get('error') or 'the run failed'
                    code = OMNI.model_auth_reason(err)
                    if code:
                        run.finish('unavailable', final, str(RT.RuntimeUnavailable(
                            code, hosted=self.cfg.hosted)), reason=code,
                            next_step=RT.next_step_for(code, hosted=self.cfg.hosted))
                        return
                    run.finish('error', final, err, reason='run_failed')
                    return
            else:
                res = DRUN.run_synthetic(run.request, run.out_dir, on_event=run.add)
                run.benchmark = res['benchmark']
                final = DRUN.finish(run.request, run.out_dir, runtime_mode=run.runtime_mode,
                                    benchmark=res['benchmark'])
            run.finish('done', final)
        except RT.RuntimeUnavailable as e:
            # Never a fallback. The run ends saying which of the seven things is
            # missing and what fixes it.
            run.add({'kind': 'error', 'stage': 'failed', 'simple': e.headline,
                     'technical': f'{e.reason}: {e.detail or ""}'.strip()})
            run.finish('unavailable', None, str(e), reason=e.reason, next_step=e.next_step)
        except K.ContractError as e:
            run.add({'kind': 'refusal', 'stage': 'failed', 'simple': str(e),
                     'technical': str(e)})
            run.finish('refused', None, str(e), reason='refused')
        except Exception as e:  # noqa: BLE001
            run.add({'kind': 'error', 'stage': 'failed',
                     'simple': 'The run hit an unexpected error.',
                     'technical': f'{type(e).__name__}: {e}'})
            run.finish('error', None, f'{type(e).__name__}: {e}', reason='internal_error')
        finally:
            with self.lock:
                if target.is_real:
                    self.active_real = max(0, self.active_real - 1)
                else:
                    self.active_synthetic = max(0, self.active_synthetic - 1)

    def get(self, rid, *, owner=None):
        """A run in this process, if the caller owns it.

        *owner* is required of every route a browser can reach. The default of
        None is for internal callers only and is not reachable from HTTP.
        """
        if not DISCOVERY_ID.match(rid or ''):
            return None
        run = self.runs.get(rid)
        if run is None:
            return None
        if owner is not None and run.owner is not None and run.owner != owner:
            return None
        return run

    def restore(self, rid, *, after=-1, owner=None):
        """A run this process no longer holds, read back from its own directory.

        This is what makes a browser refresh survivable: the run id is enough,
        the journal is beside the artifacts, and a run that was in flight when
        the process went away is reported as interrupted rather than as either
        finished or running.
        """
        if not DISCOVERY_ID.match(rid or ''):
            return None
        found = RS.restore(self.runs_dir, rid, after=after)
        if found is None:
            return None
        if owner is not None and found.get('owner') and found['owner'] != owner:
            # Not "forbidden": a run somebody else owns is, to this caller,
            # a run that does not exist. Saying otherwise confirms the id.
            return None
        return found

    def cancel(self, rid, *, reason='cancelled', owner=None):
        """Stop a run in flight. Returns the run, or None if there is nothing to stop.

        A real run is money per minute, so stopping one has to be available to
        whoever started it without waiting for a timeout. The Omnigent session is
        interrupted as well as the local loop, because stopping the stream alone
        would leave the agents working and the bill running.
        """
        run = self.get(rid, owner=owner)
        if run is None or run.finished_at is not None:
            return None
        if not run.stop(reason, STOP_MESSAGES[reason]):
            return None
        sess = (run.session or {}).get('omnigent_session_id')
        if sess and run.runtime_mode in RT.REAL_MODES:
            from . import omnigent_runtime as OMNI
            OMNI.interrupt(self.cfg.for_mode(run.runtime_mode), sess)
        return run

    def recent(self, *, owner=None):
        """Runs this caller may see. Never everybody's.

        An owner of None is an internal caller; every HTTP route passes one, so
        a visitor's list holds their own work and nothing else.
        """
        with self.lock:
            rows = [self.runs[r] for r in reversed(self.order) if r in self.runs]
        return [r.snapshot(after=10 ** 9, include_result=False) for r in rows
                if owner is None or r.owner is None or r.owner == owner]


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
    discovery = None
    runs_dir = Path('runs')
    static_dir = None
    runtime_cfg = None
    sessions = None
    default_identity = None
    limits = BU.Limits()
    policy = AZ.Policy()
    _ready = {'at': 0.0, 'body': None, 'code': 503}
    _ready_lock = threading.Lock()

    def log_message(self, fmt, *args):
        sys.stderr.write(f'{self.address_string()} {fmt % args}\n')

    def handle_one_request(self):
        """Reset per-request state before the handler runs.

        A handler instance serves every request on a keep-alive connection, so
        anything cached on `self` outlives the request that made it. Two things
        here must not: the resolved identity (recomputing it mid-request would
        mint a second anonymous workspace, and the run would be owned by a
        cookie the browser never kept) and the queued Set-Cookie headers (which
        would otherwise be re-sent on every later request).
        """
        self._identity_cache = None
        self._cookies = []
        return super().handle_one_request()

    def _set_cookie(self, cookie):
        """Queue a Set-Cookie for whatever this handler answers with.

        Used by `_identity` so an unsigned-in browser is given its own workspace
        on the first request that needs one, without every handler having to know
        about it.
        """
        pending = getattr(self, '_cookies', None)
        if pending is None:
            pending = self._cookies = []
        pending.append(cookie)

    def _send(self, code, body, ctype='application/json; charset=utf-8', extra=()):
        extra = list(extra) + [('Set-Cookie', c) for c in getattr(self, '_cookies', [])]
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

    def _body(self, limit=MAX_BODY):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0:
            return {}
        if n > limit:
            raise K.ContractError(f'the request body is {n} bytes; the limit is {limit}')
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
            if path == '/api/discovery':
                return self._start_discovery(self._body(MAX_BODY_DISCOVERY))
            if path == '/api/discovery/preview':
                req = self._discovery_request(self._body(MAX_BODY_DISCOVERY))
                return self._send(200, {'request': req,
                                        'summary_text': DISC.summarise(req),
                                        'privacy': DISC.privacy(req)})
            m = re.fullmatch(r'/api/discovery/([^/]+)/benchmark', path)
            if m:
                return self._make_benchmark(m.group(1))
            m = re.fullmatch(r'/api/discovery/([^/]+)/cancel', path)
            if m:
                return self._cancel_discovery(m.group(1))
            if path == '/api/benchmarks/run':
                return self._run_benchmark(self._body(MAX_BODY_DISCOVERY))
            if path == '/api/projects':
                return self._create_project(self._body(MAX_BODY_DISCOVERY))
            if path == '/api/auth/login':
                return self._login(self._body())
            if path == '/api/auth/logout':
                return self._logout()
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
        except AZ.Forbidden as e:
            return self._send(403, {'error': str(e), 'refused': True, 'reason': 'forbidden',
                                    'required_role': e.required_role,
                                    'headline': 'Not allowed for this account'})
        except BU.BudgetExceeded as e:
            # 429, and the limit is named: a cap that refuses without saying
            # which cap or when to come back reads like a fault.
            extra = [('Retry-After', str(e.retry_after_s))] if e.retry_after_s else []
            return self._send(429, {'error': str(e), 'refused': True, 'reason': 'budget',
                                    'limit': e.limit, 'retry_after_s': e.retry_after_s,
                                    'headline': 'Run limit reached',
                                    'limits': self.limits.describe()}, extra=extra)
        except K.ContractError as e:
            return self._send(400, {'error': str(e), 'refused': True})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {'error': f'{type(e).__name__}: {e}'})

    def _client(self):
        """The counting bucket for rate limits. Not an identity, never logged."""
        return BU.client_key(self.client_address[0] if self.client_address else None,
                             self.headers.get('X-Forwarded-For'))

    # ── identity ────────────────────────────────────────────
    def _cookie(self, name):
        for part in (self.headers.get('Cookie') or '').split(';'):
            key, _, value = part.strip().partition('=')
            if key == name:
                return value
        return None

    def _identity(self):
        """Who this request is acting as.

        A signed-in session when there is one. Otherwise, on a deployment that
        serves strangers, this browser's own unnamed workspace — because one
        shared workspace on a public URL means every visitor can list every
        other visitor's runs and read their objectives. The anonymous cookie is
        an opaque random value and the workspace id is its hash, so the thing
        that grants access is never the thing written into a record.

        On a single-user instance (no hosted posture configured) the local
        workspace stays exactly as it was: one workspace, private because the
        machine is.
        """
        cached = getattr(self, '_identity_cache', None)
        if cached is not None:
            return cached
        found = self._resolve_identity()
        self._identity_cache = found
        return found

    def _resolve_identity(self):
        sid = self._cookie(WS.SESSION_COOKIE)
        if sid:
            found = self.sessions.get(sid)
            if found is not None:
                return found
        if not self.runtime_cfg.hosted:
            return self.default_identity
        value = self._cookie(WS.ANON_COOKIE)
        if not value:
            value = WS.new_anon_cookie()
            secure = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
            self._set_cookie(f'{WS.ANON_COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax'
                             f'; Max-Age={WS.ANON_TTL_S}{secure}')
        return WS.anon_identity(value)

    def _role(self, identity=None):
        return self.policy.role_for(identity if identity is not None else self._identity())

    def _identity_payload(self, identity=None):
        """The safe shape of "who am I", for /api/identity and the pages.

        Carries a display name, whether the caller is authenticated, and the
        role this deployment grants them. It carries no token, no provider key,
        no session id and no filesystem path — and the role is computed here,
        from configuration, never read from anything the caller sent.
        """
        identity = identity or self._identity()
        role = self._role(identity)
        out = identity.public(role=role)
        out['display_name'] = identity.display
        out['role_label'] = AZ.LABELS[role]
        out['is_admin'] = role == AZ.ROLE_ADMIN
        out['credential_mode'] = self.policy.credential_mode(identity)
        out['allowance'] = self.discovery.ledger.allowance(self._client(), role=role)
        out['sign_in_available'] = bool(self.policy.auth_server or self.runtime_cfg.is_real)
        return out

    def _login(self, body):
        asked = (body.get('server') or '').strip()
        trusted = self.policy.auth_server
        if trusted:
            # One issuer, chosen by the deployment. Before this, the server URL
            # came from the request body, so a caller could sign in against an
            # Omnigent server they ran themselves and be whoever they liked —
            # including an id this deployment lists as an administrator.
            if asked and asked.rstrip('/') != trusted:
                raise WS.AuthError(
                    'this deployment signs people in through one configured accounts server, '
                    'and that is not it.', reason='untrusted_issuer', status=400)
            server = trusted
        else:
            server = asked or (self.runtime_cfg.server if self.runtime_cfg.is_real else '')
        if not server:
            raise WS.AuthError(
                'this instance has no Omnigent server configured, so there is no account to '
                'sign in to. It runs one local workspace.',
                reason='no_server_configured', status=400)
        identity = WS.sign_in(server, body.get('username'), body.get('password'))
        sid = self.sessions.create(identity)
        # HttpOnly so a script on the page cannot read it; SameSite=Lax so it is
        # not sent from another site. Secure only on https, or a local http
        # instance could never sign in at all.
        secure = '; Secure' if (self.headers.get('X-Forwarded-Proto') == 'https') else ''
        cookie = (f'{WS.SESSION_COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax'
                  f'; Max-Age={WS.SESSION_TTL_S}{secure}')
        return self._send(200, {'identity': self._identity_payload(identity)},
                          extra=[('Set-Cookie', cookie)])

    def _logout(self):
        raw = self.headers.get('Cookie') or ''
        for part in raw.split(';'):
            name, _, value = part.strip().partition('=')
            if name == WS.SESSION_COOKIE:
                self.sessions.drop(value)
        cookie = f'{WS.SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0'
        return self._send(200, {'identity': self._identity_payload()},
                          extra=[('Set-Cookie', cookie)])

    # ── discovery ───────────────────────────────────────────
    def _discovery_request(self, body):
        """Build and validate the structured request. User text stays a value."""
        identity = self._identity()
        project_id = (body.get('project_id') or '').strip()
        if not project_id:
            raise K.ContractError('choose a project: it decides which parameters exist')
        # Resolve against this workspace first, so a person's own project wins
        # over a template of the same name.
        PB.load_for(identity, project_id)
        return DISC.build(
            project_id=project_id, objective=body.get('objective'),
            runtime_mode=body.get('runtime_mode') or self.runtime_cfg.mode,
            research_context=body.get('research_context'),
            dataset_ids=body.get('dataset_ids') or [],
            expert_knowledge_ids=body.get('expert_knowledge_ids') or [],
            process_constraints=body.get('process_constraints'),
            uncertainty=body.get('uncertainty'), control=body.get('control'),
            candidate_values=body.get('candidate_values'),
            title=body.get('title'), notes=body.get('notes'),
            requested_by=identity.owner if identity.authenticated else None,
            projects_dir=self._projects_dir(identity))

    def _projects_dir(self, identity):
        """Where this workspace's own projects live, when it has any."""
        d = WS.projects_dir(identity)
        return str(d) if d.is_dir() and any(d.glob('*.json')) else None

    def _start_discovery(self, body):
        req = self._discovery_request(body)
        try:
            run = self.discovery.start(req, identity=self._identity(),
                                       client=self._client())
        except RT.RuntimeUnavailable as e:
            # 503, not 500: the request was fine, the runtime is not there. The
            # browser shows the reason and the command that fixes it, and it
            # never silently receives a synthetic run instead.
            return self._send(503, {'error': str(e), 'refused': True, 'reason': e.reason,
                                    'headline': e.headline, 'next_step': e.next_step,
                                    'runtime_mode': req['runtime_mode'],
                                    'runtime_label': RT.LABELS[req['runtime_mode']]})
        return self._send(202, run.snapshot())

    def _cancel_discovery(self, rid):
        """Stop a run in flight.

        Deliberately unauthenticated on an instance that has no accounts, for the
        same reason the run is visible by id: whoever holds the id is whoever
        started it. On a real run this also interrupts the Omnigent session,
        because the cost is in the agents, not in the stream.
        """
        owner = self._identity().owner
        run = self.discovery.cancel(rid, owner=owner)
        if run is None:
            live = self.discovery.get(rid, owner=owner)
            if live is not None:
                return self._send(409, {'error': 'that run has already finished',
                                        'status': live.status})
            return self._send(404, {'error': 'no such discovery run in this process'})
        return self._send(202, {'run_id': run.id, 'status': 'stopping',
                                'note': STOP_MESSAGES['cancelled']})

    def _make_benchmark(self, rid):
        from ..benchmark import from_run as FR
        run = self.discovery.get(rid, owner=self._identity().owner)
        if not run:
            return self._send(404, {'error': 'no such discovery run in this process'})
        if run.status != 'done' or not run.result:
            return self._send(409, {'error': 'that run has not finished, so there is nothing to '
                                             'build a benchmark from'})
        result = FR.build(request=run.request, bundle=run.result['bundle'],
                          out_dir_name=run.out_dir.name, runtime_mode=run.runtime_mode,
                          session=run.session, benchmark=run.benchmark)
        # Beside the run that produced it, never into the repository's committed
        # public set: those are the project's own demonstrations, and a visitor's
        # run is not one of them. A private lineage still refuses to be written
        # anywhere public, which `FR.write` decides, not this handler.
        out = (run.out_dir / 'benchmark') if result['privacy']['safe_to_publish'] else None
        written = FR.write(result, out=out)
        return self._send(201, {'benchmark': FR.display(result), 'written': written})

    def _run_benchmark(self, body):
        """Run a published benchmark configuration, under either runtime."""
        from ..benchmark import cli as BCLI
        bid = (body.get('benchmark_id') or '').strip()
        path = BCLI.CONFIG_DIR / f'{bid}.json'
        if not LOOP_ID.match(bid or '') or not path.is_file():
            return self._send(404, {'error': f'no benchmark configuration {bid!r}'})
        cfg = K.read_json(path)
        req = DISC.from_benchmark_config(
            cfg, runtime_mode=body.get('runtime_mode') or 'synthetic_demo')
        try:
            run = self.discovery.start(req, identity=self._identity(),
                                       client=self._client())
        except RT.RuntimeUnavailable as e:
            return self._send(503, {'error': str(e), 'refused': True, 'reason': e.reason,
                                    'headline': e.headline, 'next_step': e.next_step})
        return self._send(202, run.snapshot())

    def _create_project(self, body):
        identity = self._identity()
        template = (body.get('template_of') or '').strip()
        if template:
            doc = PB.from_template(template, project_id=body.get('project_id'),
                                   name=body.get('name'),
                                   biological_system=body.get('biological_system'),
                                   description=body.get('description'))
        else:
            doc = PB.build(project_id=body.get('project_id'), name=body.get('name'),
                           biological_system=body.get('biological_system') or {},
                           stages=body.get('stages') or [],
                           parameters=body.get('parameters') or [],
                           readouts=body.get('readouts'),
                           simulator=body.get('simulator'),
                           description=body.get('description'),
                           limitations=body.get('limitations'))
        PB.save(identity, doc, overwrite=bool(body.get('overwrite')))
        from .. import projects as PJ
        return self._send(201, {'project': PJ.Project(doc).summary(),
                                'owner': self._identity_payload(identity)})

    # ── GET ─────────────────────────────────────────────────
    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)
        if path == '/healthz':
            return self._send(200, {'ok': True, 'active': self.registry.active})
        if path == '/readyz':
            return self._readyz()
        if path == '/api/config':
            return self._send(200, {
                'max_prompt_chars': MAX_PROMPT, 'max_concurrent': MAX_CONCURRENT,
                'standins': list(STANDINS),
                'max_concurrent_sim': MAX_CONCURRENT_SIM,
                'simulator_mode': '/api/sim/config',
                'standins_with_hidden_truth': sorted(k for k, v in TRUTH_FOR.items()
                                                     if v.exists()),
                'bioreactor_source': 'synthetic_standin',
                'limits': self.limits.describe(),
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
        if path == '/api/projects':
            # What drives the simulator panel. Served per project rather than as
            # one global knob list, because a project exposes the knobs its own
            # process actually has: offering a CAR-T project an M-CSF slider
            # because macrophages have one would invite a setpoint nobody can run.
            from .. import projects as PJ
            out = []
            for pr in PJ.load_all():
                d = pr.summary()
                d['modelled_parameter_ids'] = sorted(pr.modelled_ids())
                d['has_simulator'] = bool(d.get('simulator', {}).get('model_id'))
                out.append(d)
            return self._send(200, {
                'projects': out,
                'note': 'Each project exposes only its own parameters, with its own bounds and '
                        'the origin of any bound narrower than the global one. A parameter the '
                        "project's model has no term for is marked not_modelled: it can still "
                        'be set in the lab, but no prediction is produced for it.'})
        if path == '/api/hypotheses':
            # Read from built benchmark bundles on disk. Every row carries where
            # it came from and whether its inputs were synthetic, because a
            # hypothesis card is exactly the place a demonstration figure would
            # otherwise be mistaken for a finding.
            from ..benchmark import cli as BCLI
            rows = []
            for d in sorted(BCLI.PUBLIC_DIR.glob('*/benchmark.json')):
                try:
                    b = K.read_json(d)
                except (ValueError, OSError):
                    continue
                for h in b.get('hypotheses') or []:
                    rows.append({'hypothesis': h, 'source': f'benchmark:{b["benchmark_id"]}',
                                 'inputs': b.get('inputs_kind') or 'synthetic_demo',
                                 'bundle': str(d.parent.relative_to(K.ROOT))})
            return self._send(200, {
                'hypotheses': rows,
                'note': 'Hypotheses from benchmark bundles built on this machine. The public '
                        'benchmark runs on invented fixtures: its numbers demonstrate what the '
                        'system can express, and none of them measures any real cell.'})
        if path == '/api/sim/config':
            return self._send(200, SM.config())
        if path == '/api/glossary':
            # Served rather than documented: a reader who does not know what the
            # word beside a number means is reading decoration, and nobody goes
            # to a repository to find out.
            return self._send(200, GL.describe())
        if path == '/api/runtime':
            return self._runtime_state(q)
        if path == '/api/auth/me':
            return self._send(200, {'identity': self._identity_payload()})
        if path == '/api/workspace/projects':
            identity = self._identity()
            return self._send(200, {
                'projects': PB.available_for(identity),
                'owner': self._identity_payload(identity),
                'note': 'Projects you create are stored in your workspace under the private '
                        'data root. They are never written into the repository and never '
                        'served as files.'})
        if path == '/api/project-catalogue':
            return self._send(200, PB.catalogue())
        if path == '/api/project-templates':
            return self._send(200, {'templates': PB.templates()})
        if path == '/api/benchmarks':
            return self._send(200, self._benchmarks())
        m = re.fullmatch(r'/api/benchmarks/([^/]+)', path)
        if m:
            return self._benchmark(m.group(1))
        if path == '/api/discovery':
            return self._send(200, self.discovery.recent(owner=self._identity().owner))
        m = re.fullmatch(r'/api/discovery/([^/]+)', path)
        if m:
            after = int((q.get('after') or ['-1'])[0] or -1)
            owner = self._identity().owner
            run = self.discovery.get(m.group(1), owner=owner)
            if run:
                return self._send(200, run.snapshot(after=after))
            # Not in memory: the browser was reloaded, or this process was
            # replaced. The run wrote its own record beside its artifacts, so the
            # id is enough to get it back — and a run that was interrupted says so
            # rather than being reported as finished.
            found = self.discovery.restore(m.group(1), after=after, owner=owner)
            if found:
                return self._send(200, found)
            return self._send(404, {'error': 'no record of that discovery run',
                                    'run_id': m.group(1)})
        m = re.fullmatch(r'/api/discovery/([^/]+)/events', path)
        if m:
            return self._sse_discovery(m.group(1), int((q.get('after') or ['-1'])[0] or -1))
        if path == '/api/identity':
            return self._send(200, self._identity_payload())
        m = re.fullmatch(r'/api/discovery/([^/]+)/protocol', path)
        if m:
            owner = self._identity().owner
            run = self.discovery.get(m.group(1), owner=owner)
            # A finished run's protocol is in the record it wrote, so exporting
            # it works after a reload or a restart, not only while the process
            # that produced it is still alive.
            result = (run.result if run
                      else (self.discovery.restore(m.group(1), owner=owner) or {}).get('result'))
            protocol = (result or {}).get('protocol')
            if not protocol:
                return self._send(404, {'error': 'no protocol summary for that run'})
            fmt = (q.get('format') or ['json'])[0]
            if fmt == 'md':
                doc = {k: v for k, v in protocol.items()
                       if k not in ('summary_counts', 'changed_parameters', 'badge')}
                return self._send(200, PSUM.markdown(doc), 'text/markdown; charset=utf-8')
            return self._send(200, protocol)
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

    def _readyz(self):
        """Is this deployment actually able to run what it offers?

        The judgement lives in `health.readiness`, which is where it can be
        tested; this method is the cache and the response. The probe is cached
        briefly so a monitor cannot turn this page into load on the runtime.
        """
        now = time.time()
        with self._ready_lock:
            cached = Handler._ready
            if cached['body'] is not None and now - cached['at'] < READY_CACHE_S:
                return self._send(cached['code'], dict(cached['body'], cached=True))
        code, body = HL.readiness(self.runtime_cfg, limits=self.limits,
                                  usage=self.discovery.ledger.state(),
                                  runs_writable=self._runs_writable())
        with self._ready_lock:
            Handler._ready = {'at': now, 'body': body, 'code': code}
        return self._send(code, body)

    def _runs_writable(self):
        """Whether artifacts can actually be written. A real run needs this."""
        try:
            probe = Path(self.runs_dir) / '.write-probe'
            probe.write_text('ok', encoding='utf-8')
            probe.unlink()
            return True
        except OSError:
            return False

    def _runtime_state(self, q):
        """What runtimes this deployment offers, and whether each can run now.

        `probe=1` actually contacts the configured server. Without it the answer
        is configuration only, so loading the page costs no network call.
        """
        cfg = self.runtime_cfg
        out = cfg.public()
        out['default_mode'] = cfg.mode
        if (q.get('probe') or ['0'])[0] in ('1', 'true', 'yes'):
            checks = {}
            for mode in RT.MODES:
                try:
                    checks[mode] = RT.available(cfg, mode)
                except K.ContractError as e:
                    checks[mode] = RT.reason('probe_failed', str(e))
            out['availability'] = checks
        out['identity'] = self._identity_payload()
        out['limits'] = self.limits.describe()
        out['usage'] = self.discovery.ledger.state()
        out['note'] = (
            'A synthetic run and a real one are different claims about the same question. '
            'BioSense never substitutes one for the other: if a real runtime is unavailable '
            'the run fails with the reason and the command that fixes it.')
        return self._send(200, out)

    def _benchmarks(self):
        """Every benchmark this instance can show or run, without a clone."""
        from ..benchmark import cli as BCLI
        built = []
        for path in sorted(BCLI.PUBLIC_DIR.glob('*/benchmark.json')):
            try:
                b = K.read_json(path)
            except (ValueError, OSError):
                continue
            built.append({
                'benchmark_id': b['benchmark_id'], 'title': b.get('title'),
                'objective': b['objective'], 'created_at': b['created_at'],
                'mode': b['mode'], 'project': b['project']['project_id'],
                'passed': b['scorecard']['passed'], 'failed': b['scorecard']['failed'],
                'privacy': ('PUBLIC-SAFE' if b['privacy'].get('safe_to_publish')
                            else 'PRIVATE — DO NOT PUBLISH'),
                'inputs_kind': b.get('inputs_kind') or 'synthetic_demo',
            })
        configs = []
        for path in sorted(BCLI.CONFIG_DIR.glob('*.json')):
            try:
                c = K.read_json(path)
            except (ValueError, OSError):
                continue
            configs.append({'benchmark_id': c['benchmark_id'], 'title': c['title'],
                            'objective': c['objective'], 'project_id': c['project_id'],
                            'description': c.get('description'),
                            'datasets': c.get('datasets') or [],
                            'export_policy': c['export_policy']})
        return {'built': built, 'configs': configs,
                'runtimes': [{'mode': m, 'label': self.runtime_cfg.label_for(m),
                              'offered': m in self.runtime_cfg.allowed} for m in RT.MODES],
                'note': 'A benchmark measures system capability, not biological truth. You can '
                        'read the ones already built and run a configuration again under either '
                        'runtime, without cloning anything.'}

    def _benchmark(self, bid):
        from ..benchmark import cli as BCLI
        from ..benchmark import from_run as FR
        if not LOOP_ID.match(bid or ''):
            return self._send(404, {'error': 'no such benchmark'})
        path = (BCLI.PUBLIC_DIR / bid / 'benchmark.json')
        if not path.is_file():
            return self._send(404, {'error': f'no built benchmark {bid!r}'})
        try:
            return self._send(200, FR.display(K.read_json(path)))
        except (ValueError, OSError) as e:
            return self._send(500, {'error': f'that benchmark could not be read: {e}'})

    def _sse_discovery(self, rid, after):
        run = self.discovery.get(rid, owner=self._identity().owner)
        if not run:
            return self._send(404, {'error': 'no such discovery run in this process'})
        return self._stream(run, after)

    def _sse(self, rid, after):
        run = self.registry.get(rid)
        if not run:
            return self._send(404, {'error': 'no such run in this process'})
        return self._stream(run, after)

    def _stream(self, run, after):
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
                                        'summary': getattr(run, 'summary', None),
                                        'result': getattr(run, 'result', None),
                                        'reason': getattr(run, 'error_reason', None),
                                        'next_step': getattr(run, 'next_step', None),
                                        'error': run.error},
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
    # Read once, at startup: a misconfiguration should stop the server with a
    # reason rather than let it start and refuse every run.
    cfg = RT.from_env(runs_dir=runs)
    limits = BU.from_env()
    policy = AZ.from_env()
    Handler.runs_dir = runs
    Handler.static_dir = Path(a.static) if a.static else None
    Handler.registry = Registry(runs)
    Handler.runtime_cfg = cfg
    Handler.limits = limits
    Handler.policy = policy
    Handler.discovery = DiscoveryRegistry(runs, cfg, limits, policy)
    Handler.sessions = WS.SessionStore()
    Handler.default_identity = WS.identity_from_env()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    offered = ', '.join(cfg.label_for(m) for m in cfg.allowed)
    print(f'BioSense app on http://{a.host}:{a.port}  (runs: {a.runs}, static: {a.static})\n'
          f'runtimes offered: {offered}\n'
          + (f'real AI via {cfg.server_display} as {cfg.agent}'
             + ('  (token configured)' if cfg.token else '  (no token: loopback single-user)')
             if cfg.is_real else
             'synthetic stand-in only; nothing on this path calls a model')
          + (f'\ncaps: {limits.describe()}' if limits.any_cap else ''),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
