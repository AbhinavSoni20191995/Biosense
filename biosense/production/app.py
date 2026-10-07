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
    POST /api/discovery/<id>/extend   more time for a live run, within the deployment's bounds
    POST /api/discovery/<id>/pause    hold a live run now; the clock stops with it
    POST /api/discovery/<id>/continue resume a paused run with the time it had left, or
                                  start a fresh run seeded with an ended run's artifacts
    POST /api/discovery/<id>/follow-up   the next round: an ended run's artifacts, plus the
                                  results a person measured, read against its round plan
    POST /api/discovery/<id>/delete   move a finished run of yours to the server's trash
    POST /api/discovery/<id>/promote-reference   an admin adds a run's reference draft, named
    POST /api/projects/<id>/delete    move a project of yours (and optionally its runs) to the trash
    POST /api/projects/<id>/parameters/<pid>/model   state how a parameter acts (a declared
                                  response) so the simulator can play it as an assumed effect
    POST /api/sim/project-effects  the assumed effects a project's modelled parameters imply
    GET  /api/library             the cell production library: size, papers, values (?q= to search)
    POST /api/projects/<id>/parameters   register a lever a hypothesis named into your own copy
                                  of the project; with run_id, rebuild that run's protocol
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
import os
import re
import shutil
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
from . import activity as ACT
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
# The time limit is a bill cap, not a verdict: a run cut off mid-hypothesis
# throws away everything the specialists found. So the orchestrator is told
# WRAP_UP_S before the deadline to write what it has, and the person watching
# may extend the run, EXTENSION_S at a time and MAX_EXTENSIONS times — a
# bounded choice, never an automatic one.
WRAP_UP_S = 5 * 60
EXTENSION_S = 10 * 60
MAX_EXTENSIONS = 2
# At the time limit an operator's run is PAUSED, not stopped: the agents are
# interrupted (nothing is spent while waiting) and the person is asked to
# continue or finish. Unanswered for PAUSE_HOLD_S, it finishes with what the
# agents wrote. BIOSENSE_PAUSE_AT_LIMIT says whose runs: `admin` (default),
# `all`, or `none` (every run stops at the limit, the public-demo bill cap).
PAUSE_HOLD_S = int(os.environ.get('BIOSENSE_PAUSE_HOLD_S') or 30 * 60)
PAUSE_AT_LIMIT = (os.environ.get('BIOSENSE_PAUSE_AT_LIMIT') or 'admin').strip().lower()
# The literature agent's running notes, shown live. Bounded so a snapshot stays
# a snapshot; the whole file is on disk with the run.
INSIGHTS_MAX_CHARS = 12000
PAPERS_MAX = 40
# Why a run stopped short, in the words the person reading it needs. Both are
# recorded as `stopped`, never as a result: a run that was halted produced no
# answer, and saying otherwise is the one thing this product must never do.
STOP_MESSAGES = {
    'cancelled': 'You stopped this run. The agents were interrupted, so nothing here is a '
                 'finished answer.',
    'finished_at_pause': 'You finished this run at its pause. The agents were stopped, and what '
                         'they had written is shown as a partial result — not a finished answer.',
    'pause_unanswered': 'This run paused at its time limit and nobody chose to continue it, so it '
                        'was finished with what the agents had written. Nothing here is a '
                        'finished answer.',
    'timed_out': 'This run reached the deployment\'s time limit for a single real AI run and '
                 'was stopped. Nothing here is a finished answer. While a run is live, the '
                 'live panel offers more time before the limit falls.',
    'shutdown': 'The server shut down while this run was in flight.',
}
# After the agent stream ends, BioSense is still working: discovering the files
# the agents wrote, validating them, ingesting them and building the payload the
# page renders. A parent turn completing is not the run completing — during a
# recorded Codex run child agents kept writing for a further minute — so the run
# sits in FINALIZING until the directory stops changing and the ingestion is
# done. QUIET is how long nothing may change before the artifacts are called
# settled; MAX is how long that is waited for at all.
ARTIFACT_SCAN_S = 2.0        # how often a run looks at its own directory
ARTIFACT_QUIET_S = 6.0
ARTIFACT_SETTLE_MAX_S = 150.0
# How long a readiness probe is believed before the server asks the runtime
# again. A health page that re-probes on every request becomes its own load.
READY_CACHE_S = 10
DISCOVERY_ID = re.compile(r'^[0-9a-f]{8,32}$')
MAX_BODY_DISCOVERY = 256 * 1024     # a request carries a context and constraints
# A processed table, uploaded from the Data page as text inside JSON. Raw counts
# for a whole transcriptome are larger and belong on the command line.
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
UPLOAD_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$')
UPLOAD_TYPES = ('csv', 'tsv', 'txt')
STANDINS = ('ipsc_tcell', 'tcell', 'monocyte')
# Truth files for the stand-ins the app offers. The engine reads these; no
# handler ever serves one.
TRUTH_FOR = {
    'ipsc_tcell': K.ROOT / 'examples' / 'ipsc_tcell' / 'standin_truth.synthetic.json',
    'tcell': K.ROOT / 'examples' / 'cart' / 'standin_truth.synthetic.json',
}


def _slug(name):
    """A project id from a name, so nobody has to invent one to get started."""
    import unicodedata
    raw = unicodedata.normalize('NFKD', (name or '')).encode('ascii', 'ignore').decode()
    out = re.sub(r'[^a-z0-9]+', '_', raw.lower()).strip('_')
    out = re.sub(r'^[^a-z]+', '', out)[:64]
    if len(out) < 3:
        raise K.ContractError(
            'a project needs a name with at least three letters, or an explicit project_id')
    return out


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
        self.activity = ACT.Activity(started_at=self.started_at)
        self._scanned_at = 0.0
        # What the literature agent writes as it reads, shown to the person
        # while the run is still going: its running notes and the papers it
        # found. Read off disk when the file changes, never from the model.
        self.insights = None
        self.papers_found = []
        self.bioinformatics = None
        self.genotype_sim = None
        self.proposed_terms = None
        self.round_plan = None
        self.literature_stages = []
        self.reference_draft = None
        self._live_mtimes = {}
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


# The last system check this process ran, for the operator's page. One at a time:
# the live variant starts a real session and spends model credit.
SELFCHECK = {'state': 'idle', 'live': False, 'report': None, 'error': None,
             'started_at': None, 'finished_at': None}
SELFCHECK_LOCK = threading.Lock()


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
                 started_by_admin=False, projects_dir=None):
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
        # Where this run's project profile lives. A project somebody created is
        # in their workspace, not in the committed set, and a run that cannot
        # find its own project refuses for a reason that reads like a fault.
        self.projects_dir = projects_dir
        # The badge as this deployment says it: a hosted service's loopback
        # runtime is ONLINE to the person reading it, not LOCAL.
        self.runtime_label = runtime_label or RT.LABELS[runtime_mode]
        self.deadline = deadline
        # An operator's run, by default, pauses at its limit and asks; it is
        # never cut off unasked. Its extensions are not counted either.
        self.pauses = bool(deadline) and (
            PAUSE_AT_LIMIT == 'all' or (PAUSE_AT_LIMIT == 'admin' and bool(started_by_admin)))
        self.extensions_left = (None if self.pauses else MAX_EXTENSIONS) if deadline else 0
        self.paused = False
        self.paused_at = None
        self.pause_reason = None        # 'deadline': the clock ran out. 'person': they chose.
        self.pause_remaining_s = None   # a person pause gives its remaining time back
        self.wrap_up_sent = False
        self._inbox = []            # messages for the orchestrator, delivered by the adapter
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
        self.activity = ACT.Activity(started_at=self.started_at)
        self._scanned_at = 0.0
        # What the literature agent writes as it reads, shown to the person
        # while the run is still going: its running notes and the papers it
        # found. Read off disk when the file changes, never from the model.
        self.insights = None
        self.papers_found = []
        self.bioinformatics = None
        self.genotype_sim = None
        self.proposed_terms = None
        self.round_plan = None
        self.literature_stages = []
        self.reference_draft = None
        self._live_mtimes = {}
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)

    def add(self, event):
        with self.cv:
            new_stage = bool(event.get('stage')) and event['stage'] not in self.stages_reached
            if event.get('stage'):
                self.stages_reached.add(event['stage'])
            row = None
            now = time.time()
            if len(self.events) < MAX_EVENTS_PER_RUN:
                # Both clocks: `t` for a reader ("4.2s in"), `at` so a journal
                # replayed after a restart rebuilds the same activity state.
                row = dict(event, seq=len(self.events), at=now,
                           t=round(now - self.started_at, 2))
                self.events.append(row)
                self.activity.observe(row)
            self.cv.notify_all()
        # Journalled outside the lock: a slow disk must not stall the stream.
        if row is not None:
            RS.append_event(self.out_dir, row)
        self.scan_artifacts()
        if new_stage:
            self.persist()

    def scan_artifacts(self, *, force=False):
        """Notice the files the agents have written. Throttled; never fatal."""
        now = time.time()
        if not force and now - self._scanned_at < ARTIFACT_SCAN_S:
            return
        self._scanned_at = now
        try:
            names = [p.name for p in self.out_dir.iterdir() if p.is_file()]
        except OSError:
            return
        self.activity.observe_files(names, at=now)
        self._read_live_notes()

    def _read_live_notes(self):
        """The literature agent's insights.md and discover.json, as they grow."""
        self._read_analyses()
        self._read_genotype_sim()
        self._read_proposed_terms()
        self._read_round_plan()
        self._read_stage_shards()
        self._read_reference_draft()
        papers = ('literature/merged/discover.json'
                  if (self.out_dir / 'literature' / 'merged' / 'discover.json').is_file()
                  else 'literature/discover.json')
        for rel, reader in (('literature/insights.md', self._read_insights),
                            (papers, self._read_papers),
                            ('bioinformatics/insights.md', self._read_bio_notes)):
            path = self.out_dir / rel
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if self._live_mtimes.get(rel) == mtime:
                continue
            self._live_mtimes[rel] = mtime
            try:
                reader(path, mtime)
            except (OSError, ValueError):
                continue

    def _read_insights(self, path, mtime):
        text = path.read_text(encoding='utf-8', errors='replace')
        if len(text) > INSIGHTS_MAX_CHARS:
            text = '…' + text[-INSIGHTS_MAX_CHARS:]
        self.insights = {'text': text, 'updated_at': mtime,
                         'by': 'literature agent, as it read',
                         'note': 'Running notes written during the run by the literature agent, '
                                 'before the orchestrator weighed them. Leads, not findings.'}

    def _read_bio_notes(self, path, mtime):
        text = path.read_text(encoding='utf-8', errors='replace')
        if len(text) > INSIGHTS_MAX_CHARS:
            text = '…' + text[-INSIGHTS_MAX_CHARS:]
        bio = dict(self.bioinformatics or {})
        bio['notes'] = {'text': text, 'updated_at': mtime,
                        'by': 'bioinformatics agent, as it worked'}
        self.bioinformatics = bio

    def _read_analyses(self):
        """Planned and finished analyses, from the files the CLI wrote.

        Only what validates is shown: the plan's question, dataset, tool and the
        uncertainty it targets; the result's key findings, or its refusal.
        """
        plans, results, interps, agent_runs = [], [], [], []
        try:
            files = []
            # The run directory, and the analyst's and bioinformatics' folders.
            for base in (self.out_dir, self.out_dir / 'analyst', self.out_dir / 'bioinformatics'):
                if base.is_dir():
                    files += sorted(base.glob('analysis_plan*.json'))
                    files += sorted(base.glob('analysis_result*.json'))
                    files += sorted(base.glob('interpretation*.json'))
                    files += sorted(base.glob('*/agent_analysis.json'))
        except OSError:
            return
        sig = tuple((f.name, f.stat().st_mtime) for f in files if f.is_file())
        if self._live_mtimes.get('analyses') == sig:
            return
        self._live_mtimes['analyses'] = sig
        for f in files:
            try:
                doc = K.read_json(f)
            except (OSError, ValueError):
                continue
            if f.name.startswith('analysis_plan') and not K.schema_errors('analysis_plan', doc):
                tool = doc.get('tool') or {}
                plans.append({'plan_id': doc.get('plan_id'), 'question': doc.get('question'),
                              'datasets': doc.get('dataset_ids') or [],
                              'tool': tool.get('name') if isinstance(tool, dict) else tool,
                              'analysis_type': doc.get('analysis_type'),
                              'uncertainty': (doc.get('uncertainty_ref') or {}).get('statement'),
                              'why': doc.get('why_requested'),
                              'decision_relevance': doc.get('decision_relevance')})
            elif f.name.startswith('analysis_result') and not K.schema_errors('analysis_result', doc):
                results.append({'analysis_id': doc.get('analysis_id'),
                                'plan_ref': doc.get('plan_ref'),
                                'question': doc.get('question'),
                                'source': doc.get('source_evidence_class'),
                                'confidence': doc.get('confidence'),
                                'key_findings': (doc.get('key_findings') or [])[:5],
                                'limitations': (doc.get('limitations') or [])[:3]})
            elif f.name.startswith('interpretation') and \
                    not K.schema_errors('analysis_interpretation', doc):
                interps.append({k: doc.get(k) for k in (
                    'question', 'what_it_shows', 'meaning_for_process', 'transfer',
                    'confidence', 'confidence_claimed', 'confidence_capped_by',
                    'confidence_reason', 'confirm_with', 'recommendation', 'caveats')})
            elif f.name == 'agent_analysis.json' and not K.schema_errors('agent_analysis', doc):
                agent_runs.append({k: doc.get(k) for k in (
                    'analysis_id', 'question', 'succeeded', 'problem', 'method', 'findings',
                    'confidence')} | {'script': (doc.get('script') or {}).get('path')})
        bio = dict(self.bioinformatics or {})
        bio.update(plans=plans, results=results, interpretations=interps,
                   agent_analyses=agent_runs)
        self.bioinformatics = bio if (plans or results or interps or agent_runs
                                      or bio.get('notes')) else None

    def _read_reference_draft(self):
        """Process-reference entries this run collected, for an admin to review."""
        from ..evidence import process_reference as PREF
        f = self.out_dir / PREF.DRAFT_NAME
        try:
            mtime = f.stat().st_mtime
        except OSError:
            return
        if self._live_mtimes.get('reference_draft') == mtime:
            return
        self._live_mtimes['reference_draft'] = mtime
        try:
            doc = K.read_json(f)
        except (OSError, ValueError) as e:
            self.reference_draft = {'entries': [], 'errors': [f'unreadable: {type(e).__name__}']}
            return
        rows, errors = [], []
        for e in (doc or {}).get('entries') or []:
            if not isinstance(e, dict):
                continue
            rows.append({'entry_id': e.get('entry_id'), 'parameter_id': e.get('parameter_id'),
                         'typical': e.get('typical'), 'range': e.get('range'),
                         'unit': e.get('unit'), 'basis': e.get('basis'),
                         'confidence': e.get('confidence'),
                         'vessel': (e.get('context') or {}).get('vessel'),
                         'sources': [{'ref': x.get('ref'), 'quote': (x.get('quote') or '')[:400]}
                                     for x in (e.get('sources') or []) if isinstance(x, dict)][:4]})
            try:
                PREF.check_entry(dict(e, status='reviewed'))
            except (K.ContractError, KeyError, TypeError) as err:
                errors.append(str(err)[:200])
        self.reference_draft = {'entries': rows, 'errors': errors[:10]}

    def _read_stage_shards(self):
        """A by-stage search: each stage agent's notes and paper count, live."""
        lit = self.out_dir / 'literature'
        try:
            dirs = sorted(d for d in lit.iterdir()
                          if d.is_dir() and d.name not in ('merged', 'cache', 'sources'))
        except OSError:
            return
        sig = []
        for d in dirs:
            for f in ('insights.md', 'discover.json'):
                try:
                    sig.append((d.name, f, (d / f).stat().st_mtime))
                except OSError:
                    pass
        sig = tuple(sig)
        if self._live_mtimes.get('stages') == sig:
            return
        self._live_mtimes['stages'] = sig
        rows = []
        for d in dirs:
            row = {'stage': d.name, 'notes': None, 'papers': None, 'updated_at': None}
            notes = d / 'insights.md'
            if notes.is_file():
                text = notes.read_text(encoding='utf-8', errors='replace')
                row['notes'] = ('…' + text[-INSIGHTS_MAX_CHARS:]) if len(text) > INSIGHTS_MAX_CHARS else text
                row['updated_at'] = notes.stat().st_mtime
            disc = d / 'discover.json'
            if disc.is_file():
                try:
                    row['papers'] = len((K.read_json(disc) or {}).get('papers') or [])
                except (OSError, ValueError):
                    pass
            if row['notes'] is not None or row['papers'] is not None:
                rows.append(row)
        self.literature_stages = rows
        if rows and not (self.out_dir / 'literature' / 'merged' / 'discover.json').is_file():
            # Before the merge, the paper list is the union of what the stages saved.
            seen, union = set(), []
            for d in dirs:
                f = d / 'discover.json'
                if not f.is_file():
                    continue
                try:
                    for p in (K.read_json(f) or {}).get('papers') or []:
                        k = p.get('pmid') or p.get('pmcid') or p.get('doi') or p.get('id')
                        if k and k not in seen:
                            seen.add(k)
                            union.append(p)
                except (OSError, ValueError):
                    continue
            self._read_papers_from(union)

    def _read_genotype_sim(self):
        """A wild-type-against-edited-line run the agents asked the reactor for."""
        try:
            files = sorted(f for f in self.out_dir.glob('simulation*.json') if f.is_file())
        except OSError:
            return
        sig = tuple((f.name, f.stat().st_mtime) for f in files)
        if self._live_mtimes.get('genotype_sim') == sig:
            return
        self._live_mtimes['genotype_sim'] = sig
        for f in reversed(files):
            try:
                g = (K.read_json(f) or {}).get('genotype_simulation')
            except (OSError, ValueError):
                continue
            if isinstance(g, dict) and isinstance(g.get('curves'), dict):
                self.genotype_sim = {k: g.get(k) for k in (
                    'genotype', 'curves', 'deltas', 'verdict', 'note', 'assumption',
                    'stand_in', 'stand_in_note', 'used', 'not_represented', 'control_label')}
                return

    def _read_round_plan(self):
        """The round plan the run wrote: what it could not settle, as an experiment."""
        f = self.out_dir / 'round_plan.json'
        try:
            doc = K.read_json(f) if f.is_file() else None
        except (OSError, ValueError):
            doc = None
        self.round_plan = ({k: doc.get(k) for k in (
            'round', 'purpose', 'unknowns', 'arms', 'readouts', 'replicates',
            'decision_rules', 'limitations', 'status', 'commitment_sha256', 'created_at')}
            if isinstance(doc, dict) and doc.get('kind') == 'round_plan' else None)

    def _read_proposed_terms(self):
        """The response terms this run proposed for levers the base reactor lacks.

        Shown with what each says and what it cites, so a person can add one to
        their project. Read from the file the CLI validated; a term that failed
        validation never reaches that file.
        """
        f = self.out_dir / 'proposed_terms.json'
        try:
            doc = K.read_json(f) if f.is_file() else None
        except (OSError, ValueError):
            doc = None
        if not isinstance(doc, dict) or doc.get('kind') != 'proposed_terms':
            self.proposed_terms = None
            return
        self.proposed_terms = {
            'project_id': doc.get('project_id'), 'status': doc.get('status'),
            'note': doc.get('note'),
            'terms': [{k: t.get(k) for k in ('parameter_id', 'label', 'unit', 'stage',
                                             'minimum', 'maximum', 'description')}
                      for t in (doc.get('terms') or [])[:20] if isinstance(t, dict)]}

    def _read_papers(self, path, mtime):
        self._read_papers_from(K.read_json(path).get('papers') or [])

    def _read_papers_from(self, papers):
        rows = []
        for p in papers[:PAPERS_MAX]:
            rows.append({'id': p.get('pmcid') or (f'PMID:{p["pmid"]}' if p.get('pmid') else p.get('id')),
                         'pmcid': p.get('pmcid'), 'pmid': p.get('pmid'),
                         'title': (p.get('title') or '')[:200], 'year': p.get('pubYear'),
                         'open_access': p.get('isOpenAccess'),
                         'cited_by': p.get('citedByCount'),
                         'matched_queries': len(p.get('matched_queries') or []),
                         'found_by': p.get('found_by') or [],
                         'full_text_read': bool(p.get('source_file'))})
        self.papers_found = rows

    def finish(self, status, result=None, error=None, reason=None, next_step=None):
        with self.cv:
            self.status = status
            self.result = result
            self.error = error
            self.error_reason = reason
            self.next_step = next_step
            self.finished_at = time.time()
            self.activity.close('complete' if status == 'done'
                                else 'cancelled' if status == 'stopped' else 'failed',
                                at=self.finished_at)
            self.cv.notify_all()
        self.persist()

    def persist(self):
        """Write the snapshot beside the artifacts, so a reload can find it.

        Events are already journalled line by line; this is the header. Failure
        is survivable and silent on purpose: losing recoverability is bad, and
        killing a paid-for run because a disk was full would be worse.
        """
        self.scan_artifacts(force=True)
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
        if self.paused:
            if time.time() - self.paused_at > PAUSE_HOLD_S:
                self.cancelled = 'pause_unanswered'
                return True
            return False
        if self.deadline and time.time() > self.deadline:
            if self.pauses:
                self.paused, self.paused_at = True, time.time()
                self.pause_reason = 'deadline'
                self.add({'kind': 'note', 'stage': None,
                          'simple': 'Time limit reached: the run is paused. Continue it for '
                                    f'{EXTENSION_S // 60} more minutes, or finish it with what '
                                    f'the agents have written.',
                          'technical': f'paused at deadline; held up to {PAUSE_HOLD_S} s'})
                return False
            self.cancelled = 'timed_out'
            return True
        if self.deadline and not self.wrap_up_sent and time.time() >= self.deadline - WRAP_UP_S:
            self.wrap_up_sent = True
            self._tell(self._wrap_up_text(),
                       simple='Time is nearly up. The orchestrator was told to write the '
                              'hypothesis with what it already has.',
                       technical='wrap-up sent %d s before the deadline' % WRAP_UP_S)
        return False

    def _tell(self, text, *, simple, technical=None):
        with self.cv:
            self._inbox.append({'text': text, 'simple': simple, 'technical': technical})

    def is_paused(self):
        """Polled by the adapter: hold the agents while a person decides."""
        return self.paused

    def take_message(self):
        """The next message for the orchestrator, or None. Polled by the adapter."""
        with self.cv:
            return self._inbox.pop(0) if self._inbox else None

    def _minutes_left(self):
        return max(0, int(round((self.deadline - time.time()) / 60)))

    def _wrap_up_text(self):
        return (f'BioSense: about {self._minutes_left()} minutes remain before this run\'s time '
                f'limit stops it. Stop gathering now. With what you already have, write the '
                f'hypothesis file(s), the research context and the limitations into '
                f'`{self.out_dir.name}/` with the commands the brief names. Where the magnitude '
                f'is not established use direction_only or a labelled best guess; put every '
                f'open question and anything a specialist has not returned under '
                f'limitations. Do not wait for a specialist that is still working.')

    def extend(self):
        """More time, by the person's choice: EXTENSION_S, at most MAX_EXTENSIONS times.

        The orchestrator is told, so a wrap-up it was already given is lifted.
        Returns False when there is nothing to extend: no deadline, no
        extensions left, or a run that is no longer live.
        """
        with self.cv:
            if self.finished_at is not None or self.cancelled or not self.deadline:
                return False
            if self.extensions_left is not None and self.extensions_left <= 0:
                return False
            was_paused = self.paused
            # From a pause the clock restarts now; otherwise the time is added.
            self.deadline = (time.time() if was_paused else self.deadline) + EXTENSION_S
            if self.extensions_left is not None:
                self.extensions_left -= 1
            self.paused, self.paused_at = False, None
            self.pause_reason, self.pause_remaining_s = None, None
            self.wrap_up_sent = False
        left = '' if self.extensions_left is None else f' ({self.extensions_left} extension(s) left)'
        self.add({'kind': 'note', 'stage': None,
                  'simple': (f'Continued from the pause for {EXTENSION_S // 60} more minutes.'
                             if was_paused else
                             f'The run was given {EXTENSION_S // 60} more minutes.') + left,
                  'technical': f'deadline extended by {EXTENSION_S} s'
                               + (' (resumed from pause)' if was_paused else '')})
        if was_paused:
            text = (f'BioSense: the run was paused at its time limit, and the person chose to '
                    f'continue it for {EXTENSION_S // 60} more minutes. Your last turn and any '
                    f'specialist still working were interrupted. Pick up where you left off: '
                    f'read your inbox, send again (same title, so the thread continues) any '
                    f'specialist whose answer you still need, and finish the hypothesis, the '
                    f'design choices and the simulator comparison.')
        else:
            text = (f'BioSense: the person gave this run {EXTENSION_S // 60} more minutes; '
                    f'about {self._minutes_left()} remain. Finish the hypothesis properly, '
                    f'then the simulator comparison where the project models the parameter.')
        self._tell(text, simple='The orchestrator was told it has more time.',
                   technical='resume message sent' if was_paused else 'extension message sent')
        return True

    def pause(self):
        """Hold the run now, because the person asked to.

        The agents are held between stream events and the clock stops: whatever
        time the run had left is recorded and given back on continue, so a
        pause costs nothing. The hold is bounded by PAUSE_HOLD_S exactly like
        the pause at the limit — a paused run still occupies the runtime's
        one-at-a-time gate, so it cannot hold it for ever; unanswered, it
        finishes with what the agents have written, as a partial result.
        """
        if self.runtime_mode not in RT.REAL_MODES:
            # A synthetic run is over in seconds and holds no agents; there is
            # nothing to pause. Guarded here as well as in the snapshot flag, so
            # the state cannot be reached by any route.
            return False
        with self.cv:
            if self.finished_at is not None or self.cancelled or self.paused:
                return False
            self.paused, self.paused_at = True, time.time()
            self.pause_reason = 'person'
            if self.deadline:
                self.pause_remaining_s = max(0.0, self.deadline - time.time())
        self.add({'kind': 'note', 'stage': None,
                  'simple': f'Paused by you. The agents are held and the clock is stopped; '
                            f'continue within {PAUSE_HOLD_S // 60} minutes or the run '
                            f'finishes with what they have written.',
                  'technical': f'paused by request; held up to {PAUSE_HOLD_S} s'})
        return True

    def resume(self):
        """Continue a run the person paused, with the time it had left.

        Only a person pause resumes here: a pause at the limit is a question
        about buying more time, and `extend` is its answer. No extension is
        spent — the run simply gets back the minutes it had.
        """
        with self.cv:
            if self.finished_at is not None or self.cancelled or not self.paused:
                return False
            if self.pause_reason != 'person':
                return False
            if self.deadline:
                # Never resume into an instant timeout: a pause pressed with
                # seconds left still deserves a minute to wrap up.
                self.deadline = time.time() + max(self.pause_remaining_s or 0.0, 60.0)
            self.paused, self.paused_at = False, None
            self.pause_reason, self.pause_remaining_s = None, None
        self.add({'kind': 'note', 'stage': None,
                  'simple': 'Continued from your pause with the time it had left.',
                  'technical': 'resumed from a person pause; no extension spent'})
        left = f'; about {self._minutes_left()} minutes remain' if self.deadline else ''
        self._tell('BioSense: the person paused this run and has continued it'
                   f'{left}. Your last turn and any specialist still working were '
                   'interrupted. Pick up where you left off: read your inbox, send again '
                   '(same title, so the thread continues) any specialist whose answer you '
                   'still need, and carry on.',
                   simple='The orchestrator was told the run has continued.',
                   technical='resume message sent')
        return True

    @property
    def is_live(self):
        return self.finished_at is None

    def set_status(self, status, *, note=None):
        """Move a run to a non-terminal status and say so in its stream."""
        with self.cv:
            self.status = status
            self.cv.notify_all()
        if note:
            self.add({'kind': 'note', 'stage': None, 'simple': note, 'technical': status})
        else:
            self.persist()

    def _terminal(self):
        """How this run ended, or None while it is still going.

        One definition, used by both the tick list and the count beside it —
        they disagreed before, so a finished run showed a ticked stage next to
        "0 / 10".
        """
        return ('complete' if self.status == 'done'
                else 'failed' if self.status in ('error', 'refused', 'unavailable',
                                                 'stopped', 'interrupted')
                else None)

    def progress(self):
        return ST.progress(self.stages_reached, terminal=self._terminal())

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
                'engine': (self.session or {}).get('harness'),
                'model': (self.session or {}).get('llm_model'),
                'owner': self.owner,
                'credential_mode': self.credential_mode,
                'started_by_admin': self.started_by_admin,
                'error': self.error, 'error_reason': self.error_reason,
                'next_step': self.next_step,
                'result': self.result if include_result else None,
                'elapsed_s': round((self.finished_at or time.time()) - self.started_at, 1),
                'started_at': self.started_at,
                'finished_at': self.finished_at,
                'updated_at': time.time(),
                'last_activity_at': self.activity.last_at,
                'activity': self.activity.snapshot(),
                'insights': self.insights,
                'papers_found': list(self.papers_found),
                'bioinformatics': self.bioinformatics,
                'genotype_simulation': self.genotype_sim,
                'proposed_terms': self.proposed_terms,
                'literature_stages': list(self.literature_stages),
                'reference_draft': self.reference_draft,
                'stage_counts': ST.stage_counts(self.stages_reached,
                                                terminal=self._terminal()),
                'deadline_in_s': (round(self.deadline - time.time(), 1)
                                  if self.deadline and not self.finished_at else None),
                'cancellable': self.finished_at is None,
                'extendable': (self.finished_at is None and not self.cancelled
                               and bool(self.deadline)
                               and (self.extensions_left is None or self.extensions_left > 0)),
                'paused': self.paused,
                'pause_reason': self.pause_reason,
                'pause_hold_s': (round(PAUSE_HOLD_S - (time.time() - self.paused_at), 1)
                                 if self.paused else None),
                'pauses_at_limit': self.pauses,
                # A person can hold a live real run (the agents are polled
                # between events; a synthetic run is over in seconds), and can
                # continue an ended real run as a fresh run seeded with this
                # one's artifacts.
                'pausable': (self.finished_at is None and not self.cancelled
                             and not self.paused
                             and self.runtime_mode in RT.REAL_MODES),
                'continuable': (self.finished_at is not None
                                and self.status in ('stopped', 'interrupted', 'error')
                                and self.runtime_mode in RT.REAL_MODES),
                # Any ended real run can be followed up with results, finished or not:
                # the round plan is what was run, and the results are what came back.
                'followable': (self.finished_at is not None
                               and self.runtime_mode in RT.REAL_MODES),
                'round_plan': self.round_plan,
                'continued_from': self.request.get('continued_from'),
                'extensions_left': self.extensions_left,
                'extension_s': EXTENSION_S,
                'wrap_up_sent': self.wrap_up_sent,
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

    def start(self, request, *, identity=None, client=None, projects_dir=None,
              seed_from=None):
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
                    f'{MAX_CONCURRENT_REAL}. That run is not affected by this one being '
                    f'refused: it keeps going, and it is in your run list. Start this one '
                    f'when it finishes, or stop it first.')
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
                               started_by_admin=(role == AZ.ROLE_ADMIN),
                               projects_dir=projects_dir)
            self.runs[rid] = run
            self.order.append(rid)
            if target.is_real:
                self.active_real += 1
            else:
                self.active_synthetic += 1
        if seed_from:
            self._seed(out, seed_from)
        run.persist()
        t = threading.Thread(target=self._work, args=(run, target), daemon=True,
                             name=f'discovery-{rid[:6]}')
        t.start()
        return run

    @staticmethod
    def _seed(out_dir, prior_dir):
        """Copy an ended run's artifacts into a new run's directory, before it starts.

        A continued run begins with everything the old one wrote, so the agents
        read instead of redoing. The old run's own bookkeeping stays behind: its
        state header, its event journal and its request describe THAT run, and
        the new run writes its own. A file that cannot be copied is skipped —
        the brief tells the agents to read what is there, not what should be.
        """
        skip = {RS.STATE_NAME, RS.EVENTS_NAME, 'discovery_request.json'}
        src, dst = Path(prior_dir), Path(out_dir)
        if not src.is_dir():
            return
        dst.mkdir(parents=True, exist_ok=True)
        for p in src.iterdir():
            if p.name in skip or p.name.startswith('.'):
                continue
            try:
                if p.is_dir():
                    shutil.copytree(p, dst / p.name, dirs_exist_ok=True)
                elif p.is_file():
                    shutil.copy2(p, dst / p.name)
            except OSError:
                continue

    def _settle(self, run):
        """Wait for the run directory to stop changing, within a bound.

        The agent stream ending is not the work ending: a parent turn completes
        while child agents are still writing, and a run marked COMPLETE at that
        moment shows a result that is missing the files that were still landing.
        So the run reports FINALIZING and this waits for the directory to go
        quiet — bounded, because a wait with no end is its own failure.
        """
        if not run.out_dir.is_dir():
            return
        deadline = time.time() + ARTIFACT_SETTLE_MAX_S
        last_change, signature = time.time(), None
        while time.time() < deadline:
            try:
                now_sig = sorted((p.name, p.stat().st_mtime, p.stat().st_size)
                                 for p in run.out_dir.iterdir() if p.is_file())
            except OSError:
                return
            if now_sig != signature:
                signature, last_change = now_sig, time.time()
            elif time.time() - last_change >= ARTIFACT_QUIET_S:
                return
            if run.cancelled:
                return
            time.sleep(0.75)

    def _work(self, run, target):
        try:
            run.status = 'running'
            if target.is_real:
                res = DRUN.run_real(target, run.request, run.out_dir, on_event=run.add,
                                    on_session=lambda sess: setattr(run, 'session',
                                                                    sess.public()),
                                    should_stop=run.should_stop,
                                    inbox=run.take_message, paused=run.is_paused,
                                    projects_dir=run.projects_dir)
                summary = res['omnigent']
                run.session = summary['session']
                # AI execution is over; BioSense's is not. Everything from here
                # — settling, discovering, validating, ingesting, assembling —
                # is reported as FINALIZING RESULTS rather than as COMPLETE.
                run.set_status('finalizing',
                               note='The agents have finished. BioSense is collecting and '
                                    'validating what they wrote.')
                self._settle(run)
                final = DRUN.finish(run.request, run.out_dir, runtime_mode=run.runtime_mode,
                                    session=run.session, projects_dir=run.projects_dir)
                # A real run that wrote nothing is the most confusing outcome
                # this product can produce. It says what happened instead of
                # leaving an empty hypothesis panel.
                final['diagnosis'] = run.activity.diagnose(
                    artifacts_ingested=(final.get('bundle') or {}).get('artifacts_ingested', 0),
                    hypotheses=len((final.get('bundle') or {}).get('hypotheses') or []),
                    is_real=True)
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
                        # The provider's own words are kept: "rejected" and "never
                        # sent" have different fixes, and the code alone cannot
                        # tell them apart. The CLI never prints a credential.
                        run.finish('unavailable', final, str(RT.RuntimeUnavailable(
                            code, err[:600], hosted=self.cfg.hosted)), reason=code,
                            next_step=RT.next_step_for(code, hosted=self.cfg.hosted))
                        return
                    if 'safeguards flagged' in err or "can't respond to your last message" in err:
                        # Not a fault and not a finding: the model's safety
                        # classifiers stopped the session. The provider's own
                        # remedy is a different model, which is a setting.
                        run.finish('error', final, err, reason='model_safeguard',
                                   next_step='The model\'s safety classifiers stopped this '
                                             'session. Its own advice is to rephrase or change '
                                             'the model: set BIOSENSE_AGENT_MODEL (the hosted '
                                             'image uses claude-opus-5) and restart the service.')
                        return
                    run.finish('error', final, err, reason='run_failed')
                    return
            else:
                res = DRUN.run_synthetic(run.request, run.out_dir, on_event=run.add,
                                         projects_dir=run.projects_dir)
                run.benchmark = res['benchmark']
                run.set_status('finalizing')
                final = DRUN.finish(run.request, run.out_dir, runtime_mode=run.runtime_mode,
                                    benchmark=res['benchmark'],
                                    projects_dir=run.projects_dir)
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

    def delete(self, rid, *, owner=None, operator=False):
        """Move one finished run to the trash. Returns 'deleted', 'live' or None.

        Only its owner may, or an operator for a run that has no owner. A live
        run is refused: stop it first, so nothing is still writing into the
        directory being moved.
        """
        if not DISCOVERY_ID.match(rid or ''):
            return None
        with self.lock:
            run = self.runs.get(rid)
        if run is not None:
            if run.finished_at is None:
                return 'live'
            run_owner, out_dir = run.owner, run.out_dir
        else:
            d = RS.find(self.runs_dir, rid)
            if d is None:
                return None
            # On disk only: whatever its record says, nothing in this process is
            # writing to it (a "running" record here is an interrupted run).
            state = RS._read_state(RS.state_path(d)) or {}
            run_owner, out_dir = state.get('owner'), d
        if run_owner is not None and run_owner != owner:
            return None
        if run_owner is None and not operator:
            return None
        RS.trash(self.runs_dir, out_dir)
        with self.lock:
            self.runs.pop(rid, None)
            if rid in self.order:
                self.order.remove(rid)
        return 'deleted'

    def rebuild_protocol(self, rid, *, owner=None, projects_dir=None):
        """Assemble a finished run's result again, against the project as it is now.

        For a lever registered after the run: the hypothesis that named it can
        now be adopted. Nothing the agents wrote is changed; only the protocol
        and bundle are read again. Returns 'rebuilt', 'live', None (no such run
        of this owner's) or the reason it failed.
        """
        if not DISCOVERY_ID.match(rid or ''):
            return None
        with self.lock:
            run = self.runs.get(rid)
        if run is not None:
            if run.finished_at is None:
                return 'live'
            if run.owner is not None and run.owner != owner:
                return None
            out_dir, request, mode = run.out_dir, run.request, run.runtime_mode
            state = None
        else:
            d = RS.find(self.runs_dir, rid)
            if d is None:
                return None
            state = RS._read_state(RS.state_path(d)) or {}
            if state.get('owner') is not None and state.get('owner') != owner:
                return None
            if state.get('status') in RS.LIVE_STATUSES and not state.get('salvaged'):
                return 'live'
            out_dir, mode = d, state.get('runtime_mode')
            try:
                request = K.read_json(Path(d) / 'discovery_request.json')
            except (OSError, ValueError):
                return 'the run directory holds no request to rebuild from'
        try:
            final = DRUN.finish(request, out_dir, runtime_mode=mode, projects_dir=projects_dir,
                                session=None)
        except Exception as e:  # noqa: BLE001 - a failed rebuild keeps the old result
            return f'not rebuilt: {type(e).__name__}: {str(e)[:200]}'
        if run is not None:
            with run.cv:
                run.result = final
            run.persist()
        else:
            RS.write_state(out_dir, dict(state, result=final))
        return 'rebuilt'

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
        if run.paused and reason == 'cancelled':
            reason = 'finished_at_pause'
        if not run.stop(reason, STOP_MESSAGES[reason]):
            return None
        sess = (run.session or {}).get('omnigent_session_id')
        if sess and run.runtime_mode in RT.REAL_MODES:
            from . import omnigent_runtime as OMNI
            OMNI.interrupt(self.cfg.for_mode(run.runtime_mode), sess)
        return run

    def extend(self, rid, *, owner=None):
        """More time for a live run, by whoever may see it. None if there is none to give."""
        run = self.get(rid, owner=owner)
        if run is None:
            return None
        return run if run.extend() else False

    def pause(self, rid, *, owner=None):
        """Hold a live run now, by whoever may see it. None if there is none."""
        run = self.get(rid, owner=owner)
        if run is None:
            return None
        return run if run.pause() else False

    def resume(self, rid, *, owner=None):
        """Continue a person-paused run. None if unseen, False if not theirs to resume."""
        run = self.get(rid, owner=owner)
        if run is None:
            return None
        return run if run.resume() else False

    def listing(self, *, owner=None, project_id=None, limit=200):
        """Every run this caller may see: the live ones and the durable ones.

        One list, assembled once, so the Discovery page and the Runs page cannot
        disagree about what exists or what state it is in. A run held in memory
        wins over its own record on disk, because the record is written as it
        goes and the object is current.
        """
        with self.lock:
            live = [self.runs[r] for r in reversed(self.order) if r in self.runs]
        mine = [r for r in live
                if (owner is None or r.owner is None or r.owner == owner)
                and (not project_id or r.request.get('project_id') == project_id)]
        rows, seen = [], set()
        for run in mine:
            snap = run.snapshot(after=10 ** 9, include_result=False)
            row = RS.summarise(dict(snap, project_id=run.request.get('project_id'),
                                    objective=run.request.get('objective'),
                                    run_dir=run.out_dir.name,
                                    activity=snap.get('activity')),
                               live_ids={run.id})
            row['recovered'] = False
            found = ((run.result or {}).get('bundle') or {}).get('hypotheses') or []
            row['hypothesis'] = found[0].get('statement') if found else None
            row['hypothesis_count'] = len(found)
            row['has_protocol'] = bool((run.result or {}).get('protocol'))
            rows.append(row)
            seen.add(run.id)
        live_ids = {r.id for r in live}
        for row in RS.listing(self.runs_dir, owner=owner, project_id=project_id,
                              live_ids=live_ids, limit=limit):
            if row['run_id'] not in seen:
                rows.append(row)
        rows.sort(key=lambda r: r.get('started_at') or 0, reverse=True)
        return rows[:limit]

    def project_counts(self, *, owner=None):
        """Active and total runs per project, for the selector and the detail panel.

        So switching project can say what is still working in the one being left
        behind — which is the question somebody is actually asking when they
        hesitate before switching.
        """
        out = {}
        for row in self.listing(owner=owner):
            pid = row.get('project_id') or '—'
            slot = out.setdefault(pid, {'active': 0, 'total': 0, 'last_at': None})
            slot['total'] += 1
            if row['group'] == 'active':
                slot['active'] += 1
            started = row.get('started_at') or 0
            if started and (slot['last_at'] or 0) < started:
                slot['last_at'] = started
        return out

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
            m = re.fullmatch(r'/api/discovery/([^/]+)/pause', path)
            if m:
                return self._pause_discovery(m.group(1))
            m = re.fullmatch(r'/api/discovery/([^/]+)/continue', path)
            if m:
                return self._continue_discovery(m.group(1))
            m = re.fullmatch(r'/api/discovery/([^/]+)/follow-up', path)
            if m:
                return self._continue_ended(m.group(1), self._identity(),
                                            follow_up=self._body(MAX_BODY_DISCOVERY))
            m = re.fullmatch(r'/api/discovery/([^/]+)/extend', path)
            if m:
                return self._extend_discovery(m.group(1))
            m = re.fullmatch(r'/api/discovery/([^/]+)/promote-reference', path)
            if m:
                return self._promote_reference(m.group(1), self._body())
            m = re.fullmatch(r'/api/discovery/([^/]+)/delete', path)
            if m:
                return self._delete_discovery(m.group(1))
            m = re.fullmatch(r'/api/projects/([^/]+)/delete', path)
            if m:
                return self._delete_project(m.group(1), self._body())
            m = re.fullmatch(r'/api/projects/([^/]+)/parameters', path)
            if m:
                return self._add_parameter(m.group(1), self._body())
            m = re.fullmatch(r'/api/projects/([^/]+)/parameters/([^/]+)/model', path)
            if m:
                return self._model_parameter(m.group(1), m.group(2), self._body())
            m = re.fullmatch(r'/api/projects/([^/]+)/terms/adopt', path)
            if m:
                return self._adopt_term(m.group(1), self._body())
            if path == '/api/sim/project-effects':
                return self._project_effects(self._body())
            if path == '/api/benchmarks/run':
                return self._run_benchmark(self._body(MAX_BODY_DISCOVERY))
            if path == '/api/projects':
                return self._create_project(self._body(MAX_BODY_DISCOVERY))
            if path == '/api/auth/login':
                return self._login(self._body())
            if path == '/api/auth/logout':
                return self._logout()
            if path == '/api/admin/selfcheck':
                return self._start_selfcheck(self._body())
            if path == '/api/datasets/upload':
                return self._upload_dataset(self._body(MAX_UPLOAD_BYTES + 64 * 1024))
            # Simulator mode. A person turns the knobs, so there is no decision to
            # validate and no iteration to spend: these run the model and return
            # what it read. They are capped like any other work this server starts.
            if path == '/api/sim/run':
                with SIM_GATE:
                    return self._send(200, SM.simulate(self._body()))
            if path == '/api/sim/genotype':
                with SIM_GATE:
                    return self._send(200, SM.genotype_compare(self._body()))
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

    def _upload_dataset(self, body):
        """A processed table from the Data page, registered as a PRIVATE dataset.

        An operator's action: it writes to the volume and the agents read it.
        The file lands under the private root, never under a served directory,
        and the registry records who added it. The listing endpoint still never
        names a private dataset — the response gives its id, and the page keeps
        it for the person who uploaded it.
        """
        identity = self._require_operator('adding a dataset')
        from ..data import ingest as ING
        from ..data import roots as DROOTS
        did = str(body.get('dataset_id') or '').strip()
        if not UPLOAD_ID.match(did):
            return self._send(400, {'error': 'dataset_id: 3–64 letters, digits, dot, dash or '
                                             'underscore, starting with a letter or digit'})
        name = str(body.get('file_name') or '')
        ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
        if ext not in UPLOAD_TYPES:
            return self._send(400, {'error': 'upload a processed table: .csv, .tsv or .txt. '
                                             '.h5ad and peak files are registered from the '
                                             'command line (bioinformatics.cli datasets register).'})
        content = body.get('content')
        if not isinstance(content, str) or not content.strip():
            return self._send(400, {'error': 'the file is empty'})
        if len(content.encode('utf-8')) > MAX_UPLOAD_BYTES:
            return self._send(413, {'error': f'the table is larger than '
                                             f'{MAX_UPLOAD_BYTES // (1024 * 1024)} MB; register it '
                                             f'from the command line instead'})
        title = str(body.get('title') or did).strip()[:200]
        DROOTS.ensure_roots()
        folder = DROOTS.private_root() / 'uploads' / did
        if folder.exists():
            return self._send(409, {'error': f'a dataset {did!r} was already uploaded; choose '
                                             f'another id'})
        folder.mkdir(parents=True)
        path = folder / f'table.{"tsv" if ext == "txt" else ext}'
        path.write_text(content, encoding='utf-8')
        design = {}
        for key in ('condition_column', 'control', 'replicate_column', 'sample_id_column',
                    'batch_column', 'timepoint_column', 'donor_column'):
            v = str(body.get(key) or '').strip()
            if v:
                design[key] = v
        treats = [str(t).strip() for t in (body.get('treatments') or []) if str(t).strip()]
        if treats:
            design['treatments'] = treats
        try:
            m, _ = ING.ingest_local(
                path, dataset_id=did, title=title, modality=(body.get('modality') or None),
                organism=str(body.get('organism') or 'Homo sapiens'),
                cell_type=body.get('cell_type') or None,
                perturbation=body.get('perturbation') or None,
                experimental_design=design, description=body.get('description') or None,
                registered_by=identity.owner or 'local operator', visibility='private')
        except K.ContractError as e:
            shutil.rmtree(folder, ignore_errors=True)
            return self._send(400, {'error': str(e)})
        return self._send(200, {
            'dataset': DREG.summary(m),
            'note': 'Registered as PRIVATE: stored on this server\'s volume, never served or '
                    'listed over HTTP, never a literature citation. Name it in a run to have '
                    'the bioinformatics agent analyse it.'})

    def _require_operator(self, what):
        """An admin, or anyone on a single-user local instance. Never a visitor.

        A hosted deployment with no admin configured has nobody who may run it:
        the live check spends the deployment's model credit.
        """
        identity = self._identity()
        if self.policy.has_admins or self.runtime_cfg.hosted:
            self.policy.require_admin(identity, what)
        return identity

    def _start_selfcheck(self, body):
        self._require_operator('the system check')
        live = bool((body or {}).get('live'))
        with SELFCHECK_LOCK:
            if SELFCHECK['state'] == 'running':
                return self._send(409, {'error': 'a system check is already running',
                                        **SELFCHECK})
            SELFCHECK.update(state='running', live=live, started_at=time.time(),
                             report=None, error=None)
        cfg, runs_dir = self.runtime_cfg, self.discovery.runs_dir

        def work():
            from . import selfcheck as SC
            try:
                report, error = SC.run(live=live, runs_dir=runs_dir, cfg=cfg), None
            except Exception as e:  # noqa: BLE001 - reported, never raised into a thread
                report, error = None, f'{type(e).__name__}: {e}'
            with SELFCHECK_LOCK:
                SELFCHECK.update(state='done', report=report, error=error,
                                 finished_at=time.time())

        threading.Thread(target=work, name='biosense-selfcheck', daemon=True).start()
        with SELFCHECK_LOCK:
            return self._send(202, dict(SELFCHECK))

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
            title=body.get('title'), notes=body.get('notes'), effort=body.get('effort'),
            literature_mode=body.get('literature_mode'), purpose=body.get('purpose'),
            public_data=body.get('public_data'),
            requested_by=identity.owner if identity.authenticated else None,
            projects_dir=self._projects_dir(identity))

    def _projects_dir(self, identity):
        """Where this workspace's own projects live, when it has any."""
        d = WS.projects_dir(identity)
        return str(d) if d.is_dir() and any(d.glob('*.json')) else None

    def _start_discovery(self, body):
        req = self._discovery_request(body)
        if req.get('purpose') == 'landscape':
            # The library is shared by every run on this server, so only an
            # operator adds to it.
            self._require_operator('building the cell production library')
        try:
            identity = self._identity()
            run = self.discovery.start(req, identity=identity, client=self._client(),
                                       projects_dir=self._projects_dir(identity))
        except RT.RuntimeUnavailable as e:
            # 503, not 500: the request was fine, the runtime is not there. The
            # browser shows the reason and the command that fixes it, and it
            # never silently receives a synthetic run instead.
            return self._send(503, {'error': str(e), 'refused': True, 'reason': e.reason,
                                    'headline': e.headline, 'next_step': e.next_step,
                                    'runtime_mode': req['runtime_mode'],
                                    'runtime_label': RT.LABELS[req['runtime_mode']]})
        return self._send(202, run.snapshot())

    def _is_operator(self, identity):
        try:
            if self.policy.has_admins or self.runtime_cfg.hosted:
                self.policy.require_admin(identity, 'deleting a run nobody owns')
            return True
        except Exception:  # noqa: BLE001 - not an operator is an answer, not a fault
            return False

    def _library_summary(self, q=None):
        """The cell production library: its size, and its papers (newest first)."""
        from ..evidence import library as LIB
        lib = LIB.load()
        terms = [x for x in ((q or {}).get('q') or [''])[0].split() if x] if isinstance(q, dict) \
            else []
        papers = sorted(lib.get('papers') or [], key=lambda p: p.get('added_at') or '',
                        reverse=True)
        rows = [{'paper_id': p['paper_id'], 'title': p['title'], 'year': p.get('year'),
                 'system': p.get('system'), 'stages': p.get('stages'),
                 'summary': p.get('summary'), 'status': p.get('status'),
                 'values': p.get('values')[:12], 'n_values': len(p.get('values') or [])}
                for p in papers[:200]]
        out = {'stats': LIB.stats(lib), 'papers': rows,
               'can_build': self._is_operator(self._identity()),
               'note': 'Built by literature runs. Each value quotes its paper; a run re-reads '
                       'the paragraph before citing it.'}
        if terms:
            out['matches'] = LIB.search(terms, limit=40, lib=lib)
        return self._send(200, out)

    def _promote_reference(self, rid, body):
        """An admin promotes a run's process-reference draft, as its named reviewer."""
        identity = self._require_operator('promoting process-reference entries')
        from ..evidence import process_reference as PREF
        if not DISCOVERY_ID.match(rid or ''):
            return self._send(404, {'error': 'no such run'})
        run = self.discovery.get(rid, owner=identity.owner)
        d = run.out_dir if run is not None else RS.find(self.discovery.runs_dir, rid)
        if d is None:
            return self._send(404, {'error': 'no such run'})
        draft = Path(d) / PREF.DRAFT_NAME
        if not draft.is_file():
            return self._send(404, {'error': 'this run wrote no process-reference draft'})
        try:
            ids = PREF.promote(K.read_json(draft), reviewed_by=str(body.get('reviewed_by') or ''),
                               from_run=rid)
        except (K.ContractError, OSError, ValueError, KeyError, TypeError) as e:
            return self._send(400, {'error': f'not promoted: {e}'})
        return self._send(200, {'promoted': ids,
                                'note': 'Added to this server\'s process reference under your '
                                        'name. Every later run uses these as starting values, '
                                        'labelled with their source.'})

    def _delete_discovery(self, rid):
        """Move a finished run of the caller's to the trash."""
        identity = self._identity()
        r = self.discovery.delete(rid, owner=identity.owner,
                                  operator=self._is_operator(identity))
        if r == 'live':
            return self._send(409, {'error': 'this run is still going; stop it first'})
        if r is None:
            return self._send(404, {'error': 'no such run of yours'})
        return self._send(200, {'run_id': rid, 'deleted': True,
                                'note': 'Moved to the server\'s trash: no longer listed or '
                                        'served, and recoverable by whoever runs the server.'})

    def _add_parameter(self, project_id, body):
        """Register a lever a hypothesis named into the caller's own project.

        With a run id, that run's protocol is rebuilt against the project as it
        now is, so the hypothesis that named the lever can be adopted.
        """
        identity = self._identity()
        pid = PB.check_project_id(project_id)
        who = (getattr(identity, 'display', None) or identity.owner or 'local user')
        rid = (body.get('run_id') or '').strip() or None
        doc, param, how = PB.add_parameter(identity, pid, body, registered_by=str(who)[:80],
                                           from_run=rid)
        out = {'project_id': pid, 'parameter_id': param, 'how': how,
               'version': doc['version'],
               'note': (f'{param} is now a parameter of your project {pid} '
                        + ('(a canonical parameter it did not have)' if how == 'canonical'
                           else '(defined for this project; the canonical registry does not '
                                'have it)')
                        + '. The reactor has no term for it, so any simulated effect is an '
                          'assumption you set.')}
        if rid:
            rebuilt = self.discovery.rebuild_protocol(
                rid, owner=identity.owner, projects_dir=str(WS.projects_dir(identity)))
            out['protocol_rebuilt'] = rebuilt == 'rebuilt'
            if rebuilt != 'rebuilt':
                out['rebuild_note'] = {'live': 'the run is still going; its protocol is built '
                                               'when it finishes',
                                       None: 'no such finished run of yours'}.get(
                                           rebuilt, str(rebuilt))
        return self._send(200, out)

    def _model_parameter(self, project_id, parameter_id, body):
        """A person states how a parameter acts, so the simulator can play it."""
        identity = self._identity()
        who = str(getattr(identity, 'display', None) or identity.owner or 'local user')[:80]
        doc, pid = PB.model_parameter(identity, PB.check_project_id(project_id), parameter_id,
                                      body.get('response_model'), declared_by=who)
        from . import response_model as RM
        q = next(p for p in doc['parameters'] if p['parameter_id'] == pid)
        return self._send(200, {'project_id': project_id, 'parameter_id': pid,
                                'version': doc['version'],
                                'description': RM.describe(q['response_model']),
                                'note': 'Stored on your project as EXPERT-DECLARED: a stated '
                                        'response, not a fitted one. The Simulator plays it as '
                                        'an assumed effect in its stage.'})

    def _adopt_term(self, project_id, body):
        """Add a term a run proposed to the caller's own project, as proposed.

        The term is read from the run's own validated file — never from the
        request body — so what is added is exactly what the run proposed and
        cited. Only the run's owner may, and only into their own project.
        """
        identity = self._identity()
        rid, pid = str(body.get('run_id') or ''), str(body.get('parameter_id') or '')
        run = self.discovery.get(rid, owner=identity.owner)
        if run is not None:
            run_dir = run.out_dir
        else:
            d = RS.find(self.discovery.runs_dir, rid) if DISCOVERY_ID.match(rid) else None
            state = RS._read_state(RS.state_path(d)) if d else None
            if not state or (state.get('owner') is not None
                             and state.get('owner') != identity.owner):
                return self._send(404, {'error': 'no such discovery run'})
            run_dir = d
        try:
            doc = K.read_json(Path(run_dir) / 'proposed_terms.json')
        except (OSError, ValueError):
            return self._send(404, {'error': 'that run proposed no terms'})
        if doc.get('project_id') != project_id:
            return self._send(409, {'error': f'that run proposed terms for '
                                             f'{doc.get("project_id")}, not {project_id}'})
        draft = next((t for t, row in zip(doc.get('drafts') or [], doc.get('terms') or [])
                      if row.get('parameter_id') == pid), None)
        if draft is None:
            return self._send(404, {'error': f'that run proposed no term for {pid}'})
        who = str(getattr(identity, 'display', None) or identity.owner or 'local user')[:80]
        pdoc, pid = PB.adopt_term(identity, PB.check_project_id(project_id), draft,
                                  accepted_by=who, from_run=rid)
        from . import response_model as RM
        q = next(p for p in pdoc['parameters'] if p['parameter_id'] == pid)
        return self._send(200, {'project_id': project_id, 'parameter_id': pid,
                                'version': pdoc['version'],
                                'description': RM.describe(q['response_model']),
                                'note': 'Added to your project as DE NOVO: proposed by the run '
                                        'from cited claims, accepted by you, uncalibrated. Every '
                                        'later run and the Simulator play it in its stage.'})

    def _project_effects(self, body):
        """The assumed effects a project's modelled new parameters imply at given values."""
        identity = self._identity()
        project = PB.load_for(identity, PB.check_project_id(body.get('project_id') or ''))
        values = {}
        for k, v in (body.get('values') or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                values[str(k)] = float(v)
        return self._send(200, SM.effects_from_project(project, values))

    def _delete_project(self, project_id, body):
        """Move a workspace's own project — and, if asked, its runs — to the trash."""
        from .. import projects as PJ
        identity = self._identity()
        if not any(p.project_id == project_id for p in PB.workspace_projects(identity)):
            if any(p.project_id == project_id for p in PJ.load_all()):
                return self._send(403, {'error': 'this is a built-in project template; it is part '
                                                 'of the app and cannot be deleted'})
            return self._send(404, {'error': 'no such project in your workspace'})
        runs = self.discovery.listing(owner=identity.owner, project_id=project_id)
        if any(r.get('live') for r in runs):
            return self._send(409, {'error': 'a run in this project is still going; stop it first'})
        deleted, kept = [], []
        if body.get('delete_runs'):
            op = self._is_operator(identity)
            for r in runs:
                (deleted if self.discovery.delete(r['run_id'], owner=identity.owner,
                                                  operator=op) == 'deleted'
                 else kept).append(r['run_id'])
        PB.delete(identity, project_id)
        return self._send(200, {'project_id': project_id, 'deleted': True,
                                'runs_deleted': deleted, 'runs_kept': kept,
                                'note': 'Moved to the server\'s trash and recoverable by whoever '
                                        'runs the server.'})

    def _extend_discovery(self, rid):
        """Give a live run more time, within the bounds the deployment sets."""
        owner = self._identity().owner
        run = self.discovery.extend(rid, owner=owner)
        if run is None:
            return self._send(404, {'error': 'no such discovery run in this process'})
        if run is False:
            return self._send(409, {'error': 'this run cannot be extended: it has finished, '
                                             'has no time limit, or has used every extension'})
        return self._send(200, {'run_id': rid, 'deadline_in_s': round(run.deadline - time.time(), 1),
                                'extensions_left': run.extensions_left})

    def _pause_discovery(self, rid):
        """Hold a live run now. The agents stop between events and the clock stops."""
        owner = self._identity().owner
        run = self.discovery.pause(rid, owner=owner)
        if run is None:
            return self._send(404, {'error': 'no such discovery run in this process'})
        if run is False:
            return self._send(409, {'error': 'this run cannot pause: it has finished, was '
                                             'stopped, or is already paused'})
        return self._send(200, {'run_id': rid, 'paused': True, 'pause_hold_s': PAUSE_HOLD_S})

    def _continue_discovery(self, rid):
        """Continue a run, whatever state it is in.

        A person-paused run resumes with the time it had left. A run paused at
        its limit continues the way that pause asks — an extension. An ended
        run (stopped, interrupted, failed) becomes a fresh run seeded with
        everything the old one wrote.
        """
        identity = self._identity()
        run = self.discovery.get(rid, owner=identity.owner)
        if run is not None and run.finished_at is None:
            if run.paused and (run.resume() or run.extend()):
                return self._send(200, {
                    'run_id': rid, 'paused': run.paused,
                    'deadline_in_s': (round(run.deadline - time.time(), 1)
                                      if run.deadline else None)})
            return self._send(409, {'error': 'this run is live and not paused; there is '
                                             'nothing to continue'})
        return self._continue_ended(rid, identity)

    def _continue_ended(self, rid, identity, follow_up=None):
        """A fresh run that picks up where an ended one left off.

        The old run's artifacts are copied into the new run's directory and the
        brief tells the orchestrator to read them before searching anew. In
        every accounted way it is a new run — it spends budget, holds the
        one-at-a-time gate and needs the runtime — because what restarts is
        the agents, and the agents are the cost.

        With *follow_up* ({results, dataset_ids}) it is the next round instead:
        the earlier run finished, a person ran its round plan, and this run
        reads their results against the predictions and rules that run fixed.
        """
        owner = identity.owner
        old = self.discovery.get(rid, owner=owner)
        if old is not None:
            if old.finished_at is None:
                return self._send(409, {'error': 'that run is still going; a follow-up starts '
                                                 'from a run that has ended'})
            old_dir, old_status, old_owner = old.out_dir, old.status, old.owner
        else:
            d = RS.find(self.discovery.runs_dir, rid)
            if d is None:
                return self._send(404, {'error': 'no such discovery run'})
            try:
                state = K.read_json(d / RS.STATE_NAME)
            except (OSError, ValueError):
                return self._send(404, {'error': 'no such discovery run'})
            old_dir, old_status, old_owner = d, state.get('status'), state.get('owner')
        if old_owner is not None and old_owner != owner:
            return self._send(404, {'error': 'no such discovery run'})
        try:
            prior = K.read_json(Path(old_dir) / 'discovery_request.json')
        except (OSError, ValueError):
            return self._send(409, {'error': 'that run left no request on disk, so it '
                                             'cannot be continued; start a new run instead'})
        if prior.get('runtime_mode') not in RT.REAL_MODES:
            return self._send(409, {'error': 'only a real-AI run can be continued; a '
                                             'synthetic demonstration reruns in seconds'})
        if prior.get('purpose') == 'landscape':
            # The library is shared by every run on this server, so only an
            # operator continues a build of it — the same rule as starting one.
            self._require_operator('building the cell production library')
        # The continuation is the same validated request with a new identity
        # and the continuation named, so the brief can say what this run is.
        cont = {'run_id': rid, 'run_dir': Path(old_dir).name, 'status': old_status}
        datasets = list(prior.get('dataset_ids') or [])
        if follow_up is not None:
            results = str(follow_up.get('results') or '').strip()
            added = [str(d).strip() for d in follow_up.get('dataset_ids') or [] if str(d).strip()]
            if not results and not added:
                return self._send(400, {'error': 'a follow-up brings results: describe what was '
                                                 'measured, attach a results dataset, or both'})
            if len(results) > 4000:
                return self._send(400, {'error': 'results are limited to 4000 characters; attach '
                                                 'the full table as a dataset instead'})
            before = (prior.get('continued_from') or {})
            cont.update(kind='follow_up', results=results or None,
                        round=(int(before.get('round') or 1) + 1
                               if before.get('kind') == 'follow_up' else 2),
                        reported_by=(str(getattr(identity, 'display', None) or owner)[:200]
                                     if identity.authenticated else None))
            datasets += [d for d in added if d not in datasets]
            if len(datasets) > DISC.MAX_DATASETS:
                return self._send(400, {'error': f'{len(datasets)} datasets; the limit is '
                                                 f'{DISC.MAX_DATASETS}'})
        elif (prior.get('continued_from') or {}).get('kind') == 'follow_up':
            # An interrupted round keeps its round number and the results it was
            # given; the earlier rounds' files came with the directory.
            before = prior['continued_from']
            cont.update(kind='follow_up', round=before.get('round'),
                        results=before.get('results'), reported_by=before.get('reported_by'))
        else:
            cont['kind'] = 'continue'
        req = dict(prior,
                   request_id=f'disc-{uuid.uuid4().hex[:12]}',
                   created_at=K.now_iso(),
                   requested_by=owner if identity.authenticated else None,
                   dataset_ids=datasets,
                   continued_from={k: v for k, v in cont.items() if v is not None})
        try:
            K.require_valid('discovery_request', req)
        except K.ContractError as e:
            return self._send(409, {'error': f'the stored request no longer validates, so '
                                             f'this run cannot be continued: {e}'})
        try:
            new = self.discovery.start(req, identity=identity, client=self._client(),
                                       projects_dir=self._projects_dir(identity),
                                       seed_from=old_dir)
        except RT.RuntimeUnavailable as e:
            return self._send(503, {'error': str(e), 'refused': True, 'reason': e.reason,
                                    'headline': e.headline, 'next_step': e.next_step,
                                    'runtime_mode': req['runtime_mode'],
                                    'runtime_label': RT.LABELS[req['runtime_mode']]})
        except K.ContractError as e:
            return self._send(409, {'error': str(e)})
        return self._send(202, new.snapshot())

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
            identity = self._identity()
            run = self.discovery.start(req, identity=identity, client=self._client(),
                                       projects_dir=self._projects_dir(identity))
        except RT.RuntimeUnavailable as e:
            return self._send(503, {'error': str(e), 'refused': True, 'reason': e.reason,
                                    'headline': e.headline, 'next_step': e.next_step})
        return self._send(202, run.snapshot())

    def _create_project(self, body):
        """Create a durable project, and say which one is now selected.

        Three ways in, one outcome: a validated ProjectProfile written into this
        account's workspace, which survives a refresh, a restart and a redeploy
        because it is a file and not a page's state.
        """
        identity = self._identity()
        template = (body.get('template_of') or '').strip()
        if body.get('quick') or (not template and not body.get('stages')):
            doc = PB.quick_build(
                project_id=body.get('project_id') or _slug(body.get('name')),
                name=body.get('name'), species=body.get('species'),
                starting_cell=body.get('starting_cell'), target_cell=body.get('target_cell'),
                cell_state=body.get('cell_state'),
                process_context=body.get('process_context'),
                goal=body.get('goal'), description=body.get('description'),
                parameters=body.get('parameters'))
            PB.save(identity, doc, overwrite=bool(body.get('overwrite')))
            from .. import projects as PJ
            return self._send(201, {'project': PJ.Project(doc).summary(),
                                    'project_id': doc['project_id'],
                                    'selected': doc['project_id'],
                                    'owner': self._identity_payload(identity)})
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
                                'project_id': doc['project_id'],
                                'selected': doc['project_id'],
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
        if path == '/api/admin/selfcheck':
            try:
                self._require_operator('the system check')
            except AZ.Forbidden as e:
                return self._send(403, {'error': str(e), 'refused': True, 'reason': 'forbidden'})
            with SELFCHECK_LOCK:
                return self._send(200, dict(SELFCHECK))
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
        if path == '/api/library':
            return self._library_summary(q)
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
            counts = self.discovery.project_counts(owner=identity.owner)
            projects = PB.available_for(identity)
            for row in projects:
                row['runs'] = counts.get(row['project_id'], {'active': 0, 'total': 0,
                                                             'last_at': None})
            return self._send(200, {
                'projects': projects,
                'owner': self._identity_payload(identity),
                'note': 'Projects you create are stored in your workspace under the private '
                        'data root. They are never written into the repository and never '
                        'served as files.'})
        m = re.fullmatch(r'/api/projects/([^/]+)', path)
        if m:
            return self._project_detail(m.group(1))
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
            owner = self._identity().owner
            pid = (q.get('project_id') or [None])[0]
            rows = self.discovery.listing(owner=owner, project_id=pid)
            groups = {}
            for row in rows:
                groups.setdefault(row['group'], []).append(row['run_id'])
            return self._send(200, {
                'runs': rows,
                'counts': {g: len(ids) for g, ids in groups.items()},
                'active': len(groups.get('active', [])),
                'projects': self.discovery.project_counts(owner=owner),
                'note': 'Every run you have started, live or finished, from this process and '
                        'from its records on disk. A run keeps going whether or not this page '
                        'is open: closing the browser observes less, it does not stop work.'})
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
        out['agent_sandbox'] = RT.agent_sandbox_state()
        out['identity'] = self._identity_payload()
        out['limits'] = self.limits.describe()
        out['usage'] = self.discovery.ledger.state()
        out['note'] = (
            'A synthetic run and a real one are different claims about the same question. '
            'BioSense never substitutes one for the other: if a real runtime is unavailable '
            'the run fails with the reason and the command that fixes it.')
        return self._send(200, out)

    def _project_detail(self, pid):
        """One project as a workspace: its biology, its knobs, and its work.

        Deliberately a view and not a management screen. What a scientist asks of
        a project is "what is this process, what can it predict, what is running,
        and what did I learn" — so that is what it answers, from the same
        authoritative run list the Runs page reads.
        """
        identity = self._identity()
        try:
            project = PB.load_for(identity, pid)
        except K.ContractError as e:
            return self._send(404, {'error': str(e), 'project_id': pid})
        runs = self.discovery.listing(owner=identity.owner, project_id=project.project_id)
        hypotheses = []
        for row in runs:
            if row.get('hypothesis'):
                hypotheses.append({'run_id': row['run_id'], 'statement': row['hypothesis'],
                                   'at': row.get('finished_at') or row.get('started_at'),
                                   'runtime_mode': row.get('runtime_mode')})
        summary = project.summary()
        summary['modelled_parameter_ids'] = sorted(project.modelled_ids())
        summary['has_simulator'] = bool(summary.get('simulator', {}).get('model_id'))
        return self._send(200, {
            'project': summary,
            'owned': any(pr.project_id == project.project_id
                         for pr in PB.workspace_projects(identity)),
            'runs': runs,
            'active_runs': [r for r in runs if r['group'] == 'active'],
            'recent_runs': [r for r in runs if r['group'] != 'active'][:10],
            'hypotheses': hypotheses[:10],
            'benchmarks': [r['run_id'] for r in runs if r.get('benchmark')],
            'datasets': list((runs[0].get('dataset_ids') if runs else None) or []),
            'note': 'Everything this project has: its process, its parameters, what is running '
                    'now and what it has produced. Runs belong to the project and to you.'})

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
    # A run in flight when the last process stopped (a redeploy, a crash) is
    # finished from what its agents had written, and kept, labelled partial.
    from . import salvage as SV
    SV.salvage_in_background(runs, log=lambda m: print(f'[salvage] {m}', flush=True))
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
