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
    artifacts    which files the agents have actually written, read from the
                 run directory — never guessed from the text of a tool call
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

import json
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
# Files BioSense writes itself. An agent's output is everything else, and the
# distinction is what tells "the agents produced nothing" from "a run happened".
OURS = ('discovery_request.json', 'brief.md', 'app_run.json', 'events.jsonl',
        'protocol_summary.json', 'protocol_summary.md')
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


_EXIT_RE = re.compile(r'"exit_code":\s*(-?\d+)')


def refusal_in(technical):
    """The refusal a tool result carries, as readable text, or None.

    Only an explicit refusal counts: a BioSense tool answering
    {"refused": true, "reason": ...} or {"error": ...}, or a command that
    exited non-zero. Matching words was wrong — the tool registry *describes*
    what it refuses ("Raw counts are refused"), so every listing of it was
    shown as a limitation, twice, as raw JSON.
    """
    text = (technical or '').strip()
    if not text:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if isinstance(doc, dict) and 'stdout' in doc:
        out, err = (doc.get('stdout') or '').strip(), (doc.get('stderr') or '').strip()
        inner = _json_or_none(out)
        if isinstance(inner, dict) and (inner.get('refused') or inner.get('error')):
            return _short(str(inner.get('reason') or inner.get('message')
                              or inner.get('error')), 300)
        code = doc.get('exit_code')
        if isinstance(code, int) and code not in (0, 2):
            # 2 is the documented "found nothing" (an unannotated gene): an
            # answer, not a failure.
            return _short((err or out)[-300:] or f'the command exited {code}', 300)
        return None
    if isinstance(doc, dict) and (doc.get('refused') or doc.get('error')):
        return _short(str(doc.get('reason') or doc.get('message') or doc.get('error')), 300)
    if text.startswith('{'):
        # A shell result cut short for display: trust only an explicit marker.
        m = _EXIT_RE.search(text)
        if m and int(m.group(1)) not in (0, 2):
            return f'a command exited {m.group(1)}'
        return None
    # Plain text from a tool that answers in words: a short refusal sentence.
    if len(text) <= 400 and REFUSAL_RE.search(text):
        return _short(text, 300)
    return None


def _json_or_none(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def agent_named(text):
    """The specialist a piece of text names, or None. One rule for every caller."""
    for name, rx in AGENT_RE.items():
        if rx.search(text or ''):
            return name
    return None


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
        self.turns = 0              # orchestrator turns that have ended
        self.last_said = None       # the orchestrator's latest message, verbatim but short
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
        # A specialist's own session reports as itself; everything else is the
        # orchestrator, which is the one working whenever nothing else is.
        who = event.get('agent')
        self._touch(who if who in self.agents else 'orchestrator', at)
        if kind == 'accepted':
            self._note(at, 'started', event.get('simple') or 'Run started.')
        if event.get('omnigent_type') == 'turn.completed':
            self.turns += 1
        if kind == 'message' and event.get('role') == 'assistant' and who is None:
            # What it said, not what it thought: reasoning is never kept here.
            self.last_said = _short(event.get('technical'), 800) or self.last_said
            self._note(at, 'said', event.get('technical'))
        if kind == 'tool':
            self._on_tool(event, at, text)
        elif kind == 'tool_result':
            self._on_result(event, at, text)
        elif kind in ('refusal', 'warning'):
            self._limit(at, event.get('simple') or event.get('technical'), event.get('tool'))
        elif kind == 'subagent':
            self._on_subagent(event, at)
        elif kind == 'needs_human':
            self._note(at, 'needs_human', event.get('simple') or 'A question needs a person.')
        elif kind in ('terminal', 'error'):
            self._finish(at, event)
        stage = event.get('stage')
        if stage and stage not in ST.TERMINAL:
            self._note(at, 'stage', ST.STAGE_LABEL.get(stage, stage), stage=stage)

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
            # The dispatch tool's result is the hand-over being accepted, not the
            # specialist's answer: the orchestrator is asynchronous, sends, and
            # ends its turn. Marking the agent finished here showed every
            # specialist "done" seconds after it was asked. It stays working
            # until its own session says otherwise, or the run ends.
            name = self._agent_in(text) or (self._pending.pop() if self._pending else None)
            if name and name in self.agents:
                self.agents[name]['last_activity_at'] = at
                self._note(at, 'agent', f'{AGENT_LABEL[name]} agent received its task',
                           agent=name)
        reason = refusal_in(event.get('technical'))
        if reason:
            self._limit(at, reason, event.get('tool'))

    def _on_subagent(self, event, at):
        """A specialist's session, as the server reports it.

        The parent's stream never carries a child's work, so this — polled from
        the session tree — is what says a specialist is working or has finished.
        """
        name = event.get('agent')
        if name not in self.agents or name == 'orchestrator':
            if event.get('simple'):
                self._note(at, 'agent', event['simple'])
            return
        row = self.agents[name]
        row['child_session_id'] = event.get('child_session_id') or row['child_session_id']
        row['last_activity_at'] = at
        state = event.get('state')
        if state == 'started':
            row['started_at'] = row['started_at'] or at
            row['status'] = RUNNING
            row['finished_at'] = None
            row['task'] = _short(event.get('task')) or row['task']
            self._note(at, 'agent', f'{AGENT_LABEL[name]} agent is working', agent=name)
        elif state in ('finished', 'failed'):
            row['status'] = COMPLETE if state == 'finished' else FAILED
            row['finished_at'] = at
            self._note(at, 'agent', f'{AGENT_LABEL[name]} agent '
                       + ('finished' if state == 'finished' else 'stopped with an error'),
                       agent=name)
            if state == 'failed':
                # Its evidence is missing from the result, and why is the first
                # thing a reader needs: a network refusal, a safeguard, a crash.
                said = (event.get('technical') or '').split(': ', 1)
                # A sandbox or SDK error carries its own remediation at the
                # end; cut at 300 characters it lost exactly that part.
                self._limit(at, f'{AGENT_LABEL[name]} agent failed'
                                + (f': {said[1]}' if len(said) > 1 else
                                   ' without saying why'), None, cap=700)

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

    def close(self, outcome, at=None):
        """The run is over: nobody is still working, whatever the stream last said.

        A run can end without a terminal event on the stream — the session goes
        idle, it is stopped, the deadline passes — and the agent panel then said
        "Orchestrator: Running" beside a run marked COMPLETE. *outcome* is
        complete, failed or cancelled.
        """
        at = time.time() if at is None else at
        state = {'complete': COMPLETE, 'cancelled': CANCELLED}.get(outcome, FAILED)
        for row in self.agents.values():
            if row['status'] == RUNNING:
                row['status'] = state
                row['finished_at'] = at

    def _is_dispatch(self, tool):
        """Whether this tool call is the orchestrator handing work to a specialist."""
        return tool in ST.DISPATCH_TOOLS or tool in AGENT_RE

    def _agent_in(self, text):
        return agent_named(text)

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

    def _limit(self, at, text, tool=None, cap=300):
        if not text:
            return
        text = _short(text, cap)
        if any(row['text'] == text for row in self.limitations):
            return          # the same refusal twice is one limitation
        row = {'at': at, 'rel_s': round(at - self.started_at, 1),
               'text': text, 'tool': tool}
        self.limitations.append(row)
        del self.limitations[:-MAX_LIMITATIONS]
        self._note(at, 'limitation', text)

    def observe_files(self, names, at=None):
        """Record the files that now exist in the run directory.

        Artifacts used to be detected by matching filenames in the text of tool
        calls, which reported `analysis_plan.json written` because the brief
        *asks* for a file by that name — the page showed artifacts a run had
        never produced. A file is an artifact when it is on disk.
        """
        at = time.time() if at is None else at
        for name in sorted(names or ()):
            if name in self._seen_artifacts or name in OURS:
                continue
            self._seen_artifacts.add(name)
            self.artifacts.append({'name': name, 'at': at,
                                   'rel_s': round(at - self.started_at, 1),
                                   'by': 'agents'})
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

    def diagnose(self, *, artifacts_ingested=0, hypotheses=0, is_real=True):
        """Why a run produced nothing, in facts rather than an empty panel.

        A real run that writes no artifact is the single most confusing outcome
        this product has: the page said "no hypothesis", which reads as a
        scientific conclusion when it is usually a mechanical one. This says what
        the agents actually did, and leaves the reader able to tell "the model
        gave up" from "nothing was ever asked of a specialist".
        """
        if not is_real or hypotheses or artifacts_ingested > 1:
            return None
        dispatched = [AGENT_LABEL[n] for n in AGENTS
                      if n != 'orchestrator' and self.agents[n]['status'] != QUEUED]
        wrote = [a['name'] for a in self.artifacts]
        if not dispatched:
            headline = ('The orchestrator ended without asking any specialist agent for '
                        'anything.')
            why = ('No dispatch to the literature, bioinformatics or analysis agents was '
                   'observed, so no evidence was gathered and there was nothing to form a '
                   'hypothesis from.')
            if self.last_said:
                why += (' Its last message is below — when it is a question, the '
                        'orchestrator was waiting for a person, and nobody can answer '
                        'inside a web run.')
        elif not wrote:
            headline = (f'{", ".join(dispatched)} ran, and no artifact was written to the '
                        f'run directory.')
            why = ('The specialists were asked and produced nothing BioSense could read. A '
                   'file written outside the run directory is invisible here, and so is one '
                   'that failed its contract.')
        else:
            headline = 'The agents wrote files, and none of them carried a hypothesis.'
            why = f'Files written: {", ".join(wrote)}.'
        return {
            'headline': headline,
            'why': why,
            'turns': self.turns,
            'orchestrator_said': self.last_said,
            'agents_dispatched': dispatched,
            'files_written': wrote,
            'limitations': [limit['text'] for limit in self.limitations[-5:]],
            'next_steps': [
                'Check the run directory: the agents are given one canonical path, and a file '
                'written anywhere else is not read.',
                'Check that the runtime has model credentials and that its sandbox permits '
                'writing to the run directory.',
                'Read the Activity timeline: it shows every tool call that was observed.',
            ],
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
