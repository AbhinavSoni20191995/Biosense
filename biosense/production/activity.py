"""What is actually happening right now, derived from typed events alone.

A real run spends minutes inside sub-agents. During a recorded Codex run the
agents worked for a quarter of an hour while the page showed a stage list that
barely moved — everything was happening and nothing was visible. This module is
the answer to "what is it doing?", and it answers it the same way `stages.py`
answers "how far has it got": from typed events and tool calls, never from the
model's prose.

What it tracks:

    agents       one row per specialist: queued, running, complete or failed,
                 the task it was given, when it started and when it last did
                 something
    timeline     a short, readable history of what happened and when
    limitations  tool refusals and analysis limitations, kept where a reader can
                 see them instead of disappearing into a log
    artifacts    which canonical files the run has produced so far
    last_at      when anything last happened, which is what tells somebody
                 watching that a quiet run is alive rather than wedged

Three rules it keeps:

**No invented progress.** An agent is `running` because a dispatch to it was
observed, and `complete` because its result came back. Nothing is marked done
because enough time passed, and no percentage is computed from anything.

**No hidden reasoning.** Reasoning summaries and token deltas move `last_at` and
nothing else: they are evidence that the system is alive, and they are not shown
as scientific content.

**Rebuildable.** `from_events` reconstructs the whole state from the event
journal, so a restarted process shows the same thing a live one does rather than
an empty panel beside a run that is plainly working.
"""
from __future__ import annotations

import re
import time

from . import stages as ST

# The specialists the orchestrator dispatches to, in the order a run uses them.
# `orchestrator` is first because it is the one that is working whenever nothing
# else is.
AGENTS = ('orchestrator', 'literature', 'bioinformatics', 'analysis', 'biosimulator', 'outcome')
AGENT_LABEL = {
    'orchestrator': 'Orchestrator', 'literature': 'Literature',
    'bioinformatics': 'Bioinformatics', 'analysis': 'Analysis',
    'biosimulator': 'BioSimulator', 'outcome': 'Outcome',
}
QUEUED, RUNNING, COMPLETE, FAILED, CANCELLED = (
    'queued', 'running', 'complete', 'failed', 'cancelled')

# A dispatch names its specialist in the arguments; the same names the stage map
# already keys on, so the two cannot drift.
AGENT_RE = {name: re.compile(rf'\b{name}\b', re.I) for name in AGENTS if name != 'orchestrator'}
ARTIFACT_RE = re.compile(
    r'\b(analysis_plan|analysis_result|quantified_hypothesis|research_context|'
    r'protocol_summary|benchmark|reasoning_report|summary)\.(json|md|html)\b')
# What a refusal looks like when a tool declines rather than fails. These are
# scientific content — "the metadata join is unsupported" is a finding about the
# data — so they are surfaced, not swallowed.
REFUSAL_RE = re.compile(
    r'\b(refus\w+|not supported|unsupported|cannot be joined|too large|no exact|'
    r'unavailable|not modelled|not registered|declined|requires an uncertainty)\b', re.I)

MAX_TIMELINE = 300
MAX_LIMITATIONS = 60
MAX_ARTIFACTS = 40
MAX_TASK_CHARS = 140


def _short(text, limit=MAX_TASK_CHARS):
    s = ' '.join((text or '').split())
    return s[:limit] + ('…' if len(s) > limit else '')


class Activity:
    """The live picture of one run. Fed events; never performs IO."""

    def __init__(self, started_at=None):
        self.started_at = time.time() if started_at is None else started_at
        self.last_at = self.started_at
        self.agents = {name: {'agent': name, 'label': AGENT_LABEL[name],
                              'status': QUEUED, 'task': None, 'child_session_id': None,
                              'started_at': None, 'last_activity_at': None,
                              'finished_at': None}
                       for name in AGENTS}
        self.timeline = []
        self.limitations = []
        self.artifacts = []
        self._pending = []          # agents dispatched and not yet returned, newest last
        self._seen_artifacts = set()

    # ── feeding ────────────────────────────────────────────────────────
    def observe(self, event):
        """Record one mapped BioSense event. Safe to call with anything."""
        if not isinstance(event, dict):
            return
        at = event.get('at') or time.time()
        self.last_at = max(self.last_at, at)
        kind = event.get('kind')
        text = f"{event.get('tool') or ''} {event.get('technical') or ''}"
        if kind in ('delta', 'reasoning'):
            # Alive, and nothing a reader should be shown as a finding.
            return
        self._touch('orchestrator', at)
        if kind == 'accepted':
            self._note(at, 'started', event.get('simple') or 'Run started.')
        if kind == 'tool':
            self._on_tool(event, at, text)
        elif kind == 'tool_result':
            self._on_result(event, at, text)
        elif kind in ('refusal', 'warning'):
            self._limit(at, event.get('simple') or event.get('technical'), event.get('tool'))
        elif kind == 'needs_human':
            self._note(at, 'needs_human', event.get('simple') or 'A question needs a person.')
        elif kind in ('terminal', 'error'):
            self._finish(at, event)
        stage = event.get('stage')
        if stage and stage not in ST.TERMINAL:
            self._note(at, 'stage', ST.STAGE_LABEL.get(stage, stage), stage=stage)
        self._artifacts(at, text)

    def _on_tool(self, event, at, text):
        tool = event.get('tool') or ''
        # A dispatch is the only thing that starts a specialist. A bash line that
        # happens to contain the word "analysis" is prose about an agent, not an
        # agent — and treating it as one would show work that is not happening.
        if self._is_dispatch(tool):
            name = self._agent_in(text)
            if name:
                row = self.agents[name]
                if row['status'] != RUNNING:
                    row['started_at'] = row['started_at'] or at
                row['status'] = RUNNING
                row['task'] = _short(event.get('technical')) or row['task']
                row['last_activity_at'] = at
                self._pending.append(name)
                self._note(at, 'agent', f'{AGENT_LABEL[name]} agent started', agent=name)
                return
        self._note(at, 'tool', _short(f'{tool} {event.get("simple") or ""}'.strip()),
                   tool=tool or None)

    def _on_result(self, event, at, text):
        if self._is_dispatch(event.get('tool') or ''):
            name = self._agent_in(text) or (self._pending.pop() if self._pending else None)
            if name and name in self.agents:
                row = self.agents[name]
                row['status'] = COMPLETE
                row['finished_at'] = at
                row['last_activity_at'] = at
                self._note(at, 'agent', f'{AGENT_LABEL[name]} agent finished', agent=name)
        if REFUSAL_RE.search(event.get('technical') or ''):
            self._limit(at, event.get('technical'), event.get('tool'))

    def _finish(self, at, event):
        stage = event.get('stage')
        failed = stage == 'failed' or event.get('kind') == 'error'
        for row in self.agents.values():
            if row['status'] == RUNNING:
                row['status'] = FAILED if failed else COMPLETE
                row['finished_at'] = at
        self.agents['orchestrator']['status'] = FAILED if failed else COMPLETE
        self.agents['orchestrator']['finished_at'] = at
        self._note(at, 'error' if failed else 'complete',
                   event.get('simple') or ('The run failed.' if failed else 'Agent work done.'))

    def _is_dispatch(self, tool):
        """Whether this tool call is the orchestrator handing work to a specialist."""
        return tool in ST.DISPATCH_TOOLS or tool in AGENT_RE

    def _agent_in(self, text):
        for name, rx in AGENT_RE.items():
            if rx.search(text or ''):
                return name
        return None

    def _touch(self, name, at):
        row = self.agents.get(name)
        if row is None:
            return
        if row['status'] == QUEUED:
            row['status'] = RUNNING
            row['started_at'] = row['started_at'] or at
        if row['status'] == RUNNING:
            row['last_activity_at'] = at

    def _note(self, at, kind, text, **extra):
        if not text:
            return
        row = {'at': at, 'rel_s': round(at - self.started_at, 1), 'kind': kind,
               'text': _short(text, 180)}
        row.update({k: v for k, v in extra.items() if v})
        if self.timeline and self.timeline[-1].get('text') == row['text'] \
                and self.timeline[-1].get('kind') == kind:
            return                      # a repeat says nothing new
        self.timeline.append(row)
        del self.timeline[:-MAX_TIMELINE]

    def _limit(self, at, text, tool=None):
        if not text:
            return
        row = {'at': at, 'rel_s': round(at - self.started_at, 1),
               'text': _short(text, 300), 'tool': tool}
        self.limitations.append(row)
        del self.limitations[:-MAX_LIMITATIONS]
        self._note(at, 'limitation', text)

    def _artifacts(self, at, text):
        for m in ARTIFACT_RE.finditer(text or ''):
            name = m.group(0)
            if name in self._seen_artifacts:
                continue
            self._seen_artifacts.add(name)
            self.artifacts.append({'name': name, 'at': at,
                                   'rel_s': round(at - self.started_at, 1)})
            del self.artifacts[:-MAX_ARTIFACTS]
            self._note(at, 'artifact', f'{name} written')

    # ── reading ────────────────────────────────────────────────────────
    def snapshot(self, *, now=None):
        now = now or time.time()
        return {
            'agents': [dict(self.agents[n]) for n in AGENTS],
            'timeline': list(self.timeline),
            'limitations': list(self.limitations),
            'artifacts': list(self.artifacts),
            'last_activity_at': self.last_at,
            'idle_s': round(max(0.0, now - self.last_at), 1),
            'active_agents': [AGENT_LABEL[n] for n in AGENTS
                              if self.agents[n]['status'] == RUNNING and n != 'orchestrator'],
        }

    @classmethod
    def from_events(cls, events, *, started_at=None):
        """Rebuild from a journal, so a restarted process shows what a live one does."""
        rows = list(events or [])
        base = started_at
        if base is None and rows:
            base = (rows[0].get('at')
                    or (time.time() - (rows[-1].get('t') or 0)))
        self = cls(started_at=base)
        for e in rows:
            if 'at' not in e and 't' in e and base is not None:
                e = dict(e, at=base + (e.get('t') or 0))
            self.observe(e)
        return self
