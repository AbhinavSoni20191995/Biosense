"""Omnigent stream events, turned into the twelve things a scientist cares about.

An Omnigent session emits fifty-odd event types: token deltas, heartbeats,
presence, compaction notices, turn lifecycle. None of them is a scientific step.
A person who asked "is M-CSF limiting monocyte output?" wants to know whether the
literature has been read yet, not that a reasoning summary delta arrived.

So this module maps the stream onto twelve stages, and it does so from **typed
events and tool calls only**. It never reads the model's prose to decide what is
happening. A sentence saying "now I'll search the literature" is a sentence; a
`sys_session_send` to the literature agent is a fact, and only facts move a tick
from pending to done.

Two further rules, both of which exist because the alternative misleads:

* **Stages are monotone.** A late literature follow-up after the hypothesis is
  formed does not rewind the display to step three. The furthest stage reached
  is the one shown as current, and earlier ones stay done.
* **A stage that cannot be inferred stays pending.** There is no "probably
  running the analysis by now". If nothing in the stream says a step happened,
  the tick stays open and the technical view shows what did arrive.

Everything here is pure: events in, events out. Nothing performs IO, so the
whole map is testable against recorded fixtures with no server anywhere.
"""
from __future__ import annotations

import json
import re

# ── the stages, in order ────────────────────────────────────────────────
STAGES = (
    ('understanding_objective', 'Understanding the objective',
     'Reading the question, the project and the scope it has to stay inside.'),
    ('identifying_uncertainty', 'Identifying the main uncertainty',
     'Naming what is not known and what it would change.'),
    ('searching_literature', 'Searching the literature',
     'Looking for published claims that bear on the question, each with its source.'),
    ('searching_datasets', 'Searching the data',
     'Finding public datasets, and reading the private ones you provided.'),
    ('planning_analysis', 'Planning the analysis',
     'Writing an analysis that answers the named uncertainty — a plan without one is refused.'),
    ('running_analysis', 'Running the analysis',
     'Deterministic tools computing over the data itself. No number here is written by a model.'),
    ('synthesizing_evidence', 'Synthesising the evidence',
     'Weighing what each source says, and what it is not evidence for.'),
    ('building_hypothesis', 'Building the hypothesis',
     'A parameter, a direction, a quantified effect, and the evidence behind each number.'),
    ('testing_simulator', 'Testing it in the simulator',
     'Only for parameters this project\'s model actually has a term for.'),
    ('generating_report', 'Writing the report',
     'The summary, the limitations and the recommended next experiment.'),
    ('complete', 'Complete', 'The run finished.'),
    ('failed', 'Failed', 'The run stopped before it finished.'),
)
STAGE_IDS = tuple(s[0] for s in STAGES)
STAGE_LABEL = {s[0]: s[1] for s in STAGES}
STAGE_BLURB = {s[0]: s[2] for s in STAGES}
TERMINAL = ('complete', 'failed')
ORDER = {sid: i for i, sid in enumerate(STAGE_IDS)}

# ── tool call -> stage ──────────────────────────────────────────────────
# Matched against the tool's own name and the text of its arguments. Ordered:
# the first pattern that matches wins, so the more specific commands come first.
COMMAND_STAGES = (
    # The real commands are `analyse run` and `analyse plan`; `execute` is kept
    # because older briefs named it, and a recorded run must still map.
    (re.compile(r'bioinformatics\.cli\s+(analyse\s+run|execute)\b|analysis\s+execute\b'),
     'running_analysis'),
    (re.compile(r'bioinformatics\.cli\s+analyse\s+plan\b|analysis-plan'), 'planning_analysis'),
    (re.compile(r'bioinformatics\.cli\s+(annotate|datasets|search|plan|sets|tools)\b'),
     'searching_datasets'),
    (re.compile(r'production\.cli\s+report\b|reasoning_report'), 'generating_report'),
    (re.compile(r'simulate-standin|sim_mode|sim-compare|simulator'), 'testing_simulator'),
    (re.compile(r'propose-decision|quantified_hypothesis|hypothesis'), 'building_hypothesis'),
    (re.compile(r'production\.cli\s+advise|loop-status'), 'synthesizing_evidence'),
    (re.compile(r'production\.cli\s+loop-init|validate-request|autonomy'),
     'understanding_objective'),
    (re.compile(r'agent_tools\.py\s+(search|fetch|compile)|europepmc|europe_pmc'),
     'searching_literature'),
)
# A sub-agent dispatch names its specialist in the session title, by the
# orchestrator's own convention (`<agent>-it<N>`).
AGENT_STAGES = (
    (re.compile(r'\bliterature\b'), 'searching_literature'),
    (re.compile(r'\bbioinformatics\b'), 'searching_datasets'),
    (re.compile(r'\banalysis\b'), 'running_analysis'),
    (re.compile(r'\bbiosimulator\b'), 'testing_simulator'),
    (re.compile(r'\boutcome\b'), 'synthesizing_evidence'),
)
DISPATCH_TOOLS = ('sys_session_send', 'session_send', 'sys_subagent_send')

# Events that say only that the connection is alive. Dropped: they would fill
# the event budget and tell a reader nothing.
LIVENESS = ('response.heartbeat', 'session.heartbeat', 'session.presence',
            'session.changed_files.invalidated', 'session.terminal_activity',
            'injection.consumed')
# Streamed text, which is shown as progress but not kept one event per token.
DELTAS = ('response.output_text.delta', 'response.reasoning_text.delta',
          'response.reasoning_summary_text.delta', 'response.function_call_output.delta')


def initial_progress():
    """Every stage, all pending. What the interface renders before anything arrives."""
    return [{'stage': sid, 'label': STAGE_LABEL[sid], 'blurb': STAGE_BLURB[sid],
             'status': 'pending'} for sid in STAGE_IDS if sid not in TERMINAL]


def progress(reached, *, terminal=None):
    """The tick list: everything before the furthest stage done, that one current.

    *reached* is the set of stage ids seen so far. A terminal state marks every
    stage that was reached as done and says which way it ended.
    """
    seen = {s for s in reached if s in ORDER and s not in TERMINAL}
    furthest = max((ORDER[s] for s in seen), default=-1)
    rows = []
    for sid in STAGE_IDS:
        if sid in TERMINAL:
            continue
        i = ORDER[sid]
        if terminal == 'complete':
            status = 'done' if i <= furthest else 'skipped'
        elif i < furthest:
            status = 'done'
        elif i == furthest:
            status = 'done' if terminal else 'current'
        else:
            status = 'pending'
        rows.append({'stage': sid, 'label': STAGE_LABEL[sid], 'blurb': STAGE_BLURB[sid],
                     'status': status})
    return rows


def _text(value, limit=4000):
    if value is None:
        return ''
    if isinstance(value, str):
        return value[:limit]
    try:
        return json.dumps(value, default=str)[:limit]
    except (TypeError, ValueError):
        return str(value)[:limit]


def stage_for_tool(name, arguments):
    """The stage a tool call implies, or None when it implies nothing.

    None is a real answer here. A shell command that lists a directory says
    nothing about which scientific step is running, and guessing would move a
    tick on no evidence.
    """
    blob = f'{name or ""} {_text(arguments)}'
    if (name or '') in DISPATCH_TOOLS:
        for pattern, stage in AGENT_STAGES:
            if pattern.search(_text(arguments)):
                return stage
    for pattern, stage in COMMAND_STAGES:
        if pattern.search(blob):
            return stage
    return None


def stage_for_agent(name):
    """The stage a specialist's work implies, from its name alone, or None.

    Used for a sub-agent session seen directly on the server — the same mapping a
    dispatch to it already uses, so the two routes tick the same box.
    """
    for pattern, stage in AGENT_STAGES:
        if pattern.search(name or ''):
            return stage
    return None


def _as_dict(event):
    """An SDK event object or a plain dict, as a dict.

    The adapter hands us typed Pydantic models; the tests hand us dicts recorded
    from a real stream. Both have to work, and neither should need the SDK
    installed to be read.
    """
    if isinstance(event, dict):
        return event
    for attr in ('model_dump', 'dict'):
        fn = getattr(event, attr, None)
        if callable(fn):
            try:
                return fn()
            except TypeError:
                pass
    return {k: v for k, v in vars(event).items() if not k.startswith('_')}


def map_event(event):
    """One Omnigent event as a BioSense event, or None to drop it.

    The returned dict is what the browser receives: `kind` for the badge,
    `stage` when one is implied, `simple` for the plain-language view and
    `technical` for everything else. `simple` is None when a line has nothing to
    say to a non-specialist, rather than being padded with a restatement.
    """
    e = _as_dict(event)
    etype = e.get('type') or ''
    if etype in LIVENESS:
        return None
    if etype in DELTAS:
        text = e.get('delta') or e.get('text') or ''
        return None if not text else {
            'kind': 'delta', 'stage': None, 'simple': None, 'technical': _text(text, 400),
            'omnigent_type': etype}

    if etype == 'session.status':
        status = e.get('status')
        if status == 'failed':
            err = e.get('error') or {}
            return {'kind': 'error', 'stage': 'failed', 'omnigent_type': etype,
                    'simple': 'The run stopped before it finished.',
                    'technical': _text(err.get('message') or err) or 'session failed',
                    'error_code': err.get('code')}
        if status == 'waiting':
            return {'kind': 'waiting', 'stage': None, 'omnigent_type': etype,
                    'simple': 'Waiting for a specialist agent to come back.',
                    'technical': 'session.status=waiting'}
        return {'kind': 'status', 'stage': None, 'omnigent_type': etype, 'simple': None,
                'technical': f'session.status={status}'}

    if etype in ('session.created', 'response.created', 'response.queued'):
        return {'kind': 'accepted', 'stage': 'understanding_objective', 'omnigent_type': etype,
                'simple': 'BioSense sent your question to the discovery agents.',
                'technical': etype}
    if etype == 'response.in_progress':
        return {'kind': 'status', 'stage': 'understanding_objective', 'omnigent_type': etype,
                'simple': None, 'technical': etype}

    if etype == 'response.output_item.done':
        return _item_event(e.get('item') or {}, etype)

    if etype == 'response.policy_denied':
        return {'kind': 'refusal', 'stage': None, 'omnigent_type': etype,
                'simple': 'An action was refused because it fell outside what the evidence '
                          'allows. That is the system working, not a fault.',
                'technical': _text(e.get('reason') or e.get('policy') or e)}
    if etype == 'response.elicitation_request':
        return {'kind': 'needs_human', 'stage': None, 'omnigent_type': etype,
                'simple': 'The agents are asking a question only a person can answer.',
                'technical': _text(e.get('params') or e)}
    if etype == 'response.retry':
        err = e.get('error') or {}
        return {'kind': 'warning', 'stage': None, 'omnigent_type': etype,
                'simple': f'A step is being retried ({e.get("attempt")} of '
                          f'{e.get("max_attempts")}).',
                'technical': f'{e.get("source")}: {_text(err.get("message") or err)}'}
    if etype == 'response.error':
        err = e.get('error') or {}
        return {'kind': 'error', 'stage': None, 'omnigent_type': etype,
                'simple': 'A step failed.', 'error_source': e.get('source'),
                'error_code': err.get('code'),
                'technical': f'{e.get("source")}: {_text(err.get("message") or err)}'}
    if etype == 'response.completed':
        return {'kind': 'terminal', 'stage': 'complete', 'omnigent_type': etype,
                'simple': 'The discovery run finished.', 'technical': etype}
    if etype in ('response.failed', 'response.cancelled', 'response.incomplete'):
        resp = e.get('response') or {}
        err = resp.get('error') or e.get('error') or {}
        detail = resp.get('incomplete_details') or err
        return {'kind': 'terminal', 'stage': 'failed', 'omnigent_type': etype,
                'error_source': e.get('source'), 'error_code': err.get('code'),
                'simple': {'response.cancelled': 'The run was stopped.',
                           'response.incomplete': 'The run ended before it finished.'}
                .get(etype, 'The run failed.'),
                'technical': _text(detail) or etype}
    if etype.startswith('response.compaction'):
        return {'kind': 'note', 'stage': None, 'omnigent_type': etype, 'simple': None,
                'technical': 'the conversation history is being summarised'}
    if etype == 'session.interrupted':
        return {'kind': 'terminal', 'stage': 'failed', 'omnigent_type': etype,
                'simple': 'The run was interrupted.', 'technical': etype}
    # Anything else is recorded in the technical view rather than dropped: an
    # event this version does not know about is still something that happened.
    return {'kind': 'note', 'stage': None, 'omnigent_type': etype, 'simple': None,
            'technical': etype}


def _item_event(item, etype):
    """A completed conversation item: a tool call, a tool result, or a message."""
    kind = item.get('type')
    if kind == 'function_call':
        name = item.get('name')
        args = item.get('arguments')
        stage = stage_for_tool(name, args)
        return {'kind': 'tool', 'stage': stage, 'omnigent_type': etype, 'tool': name,
                'simple': STAGE_BLURB[stage] if stage else None,
                'technical': f'{name} {_text(args, 600)}'.strip()}
    if kind in ('function_call_output', 'native_tool'):
        return {'kind': 'tool_result', 'stage': None, 'omnigent_type': etype,
                'tool': item.get('name'), 'simple': None,
                'technical': _text(item.get('output') or item.get('result') or item, 800)}
    if kind == 'message':
        text = _message_text(item)
        if not text:
            return None
        return {'kind': 'message', 'stage': None, 'omnigent_type': etype,
                'role': item.get('role'),
                'simple': text if item.get('role') == 'assistant' else None,
                'technical': text}
    if kind == 'reasoning':
        return {'kind': 'reasoning', 'stage': None, 'omnigent_type': etype, 'simple': None,
                'technical': _text(item.get('summary') or item.get('content'), 800)}
    return {'kind': 'note', 'stage': None, 'omnigent_type': etype, 'simple': None,
            'technical': _text(kind)}


def _message_text(item):
    content = item.get('content')
    if isinstance(content, str):
        return content[:4000]
    parts = []
    for block in content or []:
        if isinstance(block, dict):
            parts.append(block.get('text') or '')
        elif isinstance(block, str):
            parts.append(block)
    return ' '.join(p for p in parts if p).strip()[:4000]


def stage_counts(reached, *, terminal=None):
    """How many of the workflow's stages are done, as a count and not a percentage.

    A count is a fact about the deterministic stage list. A percentage would
    imply the remaining work is proportional to the remaining stages, which is
    not true of a run that spends eleven minutes inside one analysis — and a
    progress bar that lies is worse than one that only says "still working".
    """
    rows = progress(reached, terminal=terminal)
    done = sum(1 for r in rows if r['status'] == 'done')
    return {'done': done, 'total': len(rows),
            'current': next((r['stage'] for r in rows if r['status'] == 'current'), None)}
