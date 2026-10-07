"""A discovery run's own record on disk, so a browser refresh is not a loss.

Runs live in the app process's memory, which was fine while a run was a minute of
deterministic code. A real AI run is minutes long and costs money, and the person
watching it will close the tab, lose a network, or reload the page. If the only
copy of "what happened" is a Python object and an SSE stream, all three of those
throw the run away while it is still being paid for.

So every discovery run writes two files beside its artifacts:

    app_run.json    the snapshot: status, progress, session ids, the result
    events.jsonl    one JSON object per line, appended as events arrive

Both are written by the run itself, in the run's own directory, next to the
artifacts the agents produce. There is no index and no database: a run id is
found by reading the state files, which is a few dozen small reads on a service
of this size and cannot drift out of step with the directories it describes.

Two properties this module exists to keep true:

**A restored run never pretends to be live.** If the state on disk says `running`
and the process holding it is gone, `restore` returns status `interrupted` and
says the server restarted. It does not report a finished run, and it does not
report a running one that nothing is running.

**The record is the artifacts.** This file is a convenience over them, never a
substitute: a restored snapshot carries the result that was computed from the
artifacts at the time, and the artifacts stay the durable truth.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .. import contracts as K

STATE_NAME = 'app_run.json'
EVENTS_NAME = 'events.jsonl'
MAX_JOURNAL_BYTES = 8 * 1024 * 1024     # a run that writes more than this is a bug
# How long a non-terminal state file is believed before it is called interrupted.
# Longer than any permitted run timeout, so a live run is never mislabelled.
STALE_AFTER_S = 6 * 60 * 60


def state_path(out_dir):
    return Path(out_dir) / STATE_NAME


def events_path(out_dir):
    return Path(out_dir) / EVENTS_NAME


def write_state(out_dir, snapshot):
    """Record the run's current snapshot. Atomic, so a crash leaves it readable."""
    out = Path(out_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
        K.write_json_atomic(state_path(out), dict(snapshot, written_at=time.time()))
        return True
    except (OSError, TypeError, ValueError):
        # A run must not die because its journal could not be written. The
        # in-memory run and the artifacts both still work; only recovery is lost.
        return False


def append_event(out_dir, event):
    """Append one event to the journal. Best effort, and bounded."""
    p = events_path(out_dir)
    try:
        if p.exists() and p.stat().st_size > MAX_JOURNAL_BYTES:
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(event, default=str) + '\n')
        return True
    except (OSError, TypeError, ValueError):
        return False


def read_events(out_dir, after=-1):
    """Events from the journal with seq greater than *after*."""
    p = events_path(out_dir)
    if not p.is_file():
        return []
    rows = []
    try:
        with p.open(encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get('seq', -1) > after:
                    rows.append(e)
    except OSError:
        return rows
    return rows


# Statuses a run can hold. `finalizing` is not an afterthought: it is the window
# where AI execution is done and BioSense is still collecting, validating and
# assembling, and a run shown as COMPLETE during it is showing a result that is
# missing files still landing.
LIVE_STATUSES = ('queued', 'running', 'finalizing')
DONE_STATUSES = ('done',)
FAILED_STATUSES = ('error', 'refused', 'unavailable', 'interrupted')
STOPPED_STATUSES = ('stopped',)


def group_for(status):
    """Which column of the Runs page a status belongs in."""
    if status in LIVE_STATUSES:
        return 'active'
    if status in DONE_STATUSES:
        return 'complete'
    if status in STOPPED_STATUSES:
        return 'cancelled'
    return 'failed'


def _read_state(path):
    try:
        d = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get('run_id') else None


def summarise(state, *, live_ids=()):
    """One run as a row in a list: enough to judge it, not enough to render it.

    Deliberately small and deliberately the same shape wherever a run is listed,
    so the Discovery page and the Runs page cannot disagree about what a run is.
    The full scientific state is read through the run itself.
    """
    status = state.get('status')
    if status in LIVE_STATUSES and state.get('run_id') not in live_ids:
        # On disk it says running; nothing is running it. Saying so is the only
        # honest answer — it is neither finished nor in flight.
        status = 'interrupted'
    activity = state.get('activity') or {}
    result = state.get('result') or {}
    protocol = result.get('protocol') or {}
    bundle = result.get('bundle') or {}
    hypotheses = bundle.get('hypotheses') or []
    return {
        'run_id': state.get('run_id'),
        'project_id': state.get('project_id'),
        'owner': state.get('owner'),
        'objective': state.get('objective'),
        'status': status,
        'group': group_for(status),
        'live': status in LIVE_STATUSES,
        'runtime_mode': state.get('runtime_mode'),
        'runtime_label': state.get('runtime_label'),
        'is_real': state.get('is_real'),
        'engine': state.get('engine'),
        'model': state.get('model'),
        'credential_mode': state.get('credential_mode'),
        'started_by_admin': state.get('started_by_admin'),
        'started_at': state.get('started_at'),
        'finished_at': state.get('finished_at'),
        'last_activity_at': activity.get('last_activity_at') or state.get('written_at'),
        'elapsed_s': state.get('elapsed_s'),
        'stage_counts': state.get('stage_counts'),
        'current_stage': (state.get('stage_counts') or {}).get('current'),
        'active_agents': activity.get('active_agents') or [],
        'event_count': state.get('event_count'),
        'hypothesis': (hypotheses[0].get('statement') if hypotheses else None),
        'hypothesis_count': len(hypotheses),
        'has_protocol': bool(protocol),
        'benchmark': bool(result.get('benchmark')),
        'run_dir': state.get('run_dir'),
        'recovered': True,
    }


def listing(runs_dir, *, owner=None, project_id=None, live_ids=(), limit=200):
    """Every run on disk this caller may see, newest first.

    The durable half of the run list. A process holds its own runs in memory and
    forgets them on restart; the directory holds all of them, which is what makes
    history survive a redeploy. Ownership is applied here rather than by the
    caller, so there is one place it can be got wrong.
    """
    root = Path(runs_dir)
    if not root.is_dir():
        return []
    dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime if d.exists() else 0, reverse=True)
    rows = []
    for d in dirs[:limit * 2]:
        state = _read_state(state_path(d))
        if state is None:
            continue
        if owner is not None and state.get('owner') and state['owner'] != owner:
            continue
        if project_id and state.get('project_id') != project_id:
            continue
        _settle_counts(state)
        rows.append(summarise(state, live_ids=live_ids))
        if len(rows) >= limit:
            break
    rows.sort(key=lambda r: r.get('started_at') or 0, reverse=True)
    return rows


TRASH = '.trash'


def trash(runs_dir, run_dir):
    """Move a run directory into the runs root's trash. Returns where it went.

    Not an unlink: a run is a scientist's record, and "delete" pressed by
    mistake should be recoverable by whoever runs the server. The trash is a
    dot-folder one level down, so no listing (`*/app_run.json`) and no lookup
    ever sees it, and it is never served.
    """
    src = Path(run_dir).resolve()
    root = Path(runs_dir).resolve()
    if src.parent != root:
        raise ValueError(f'{src} is not a run directory directly under {root}')
    dst_dir = root / TRASH
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f'{src.name}-{time.strftime("%Y%m%d-%H%M%S")}'
    src.rename(dst)
    return dst


def find(runs_dir, run_id):
    """The directory holding *run_id*, or None.

    Named directories are checked first: a run directory ends in the first six
    characters of its id, so the usual case is one `glob` and one read.
    """
    root = Path(runs_dir)
    if not run_id or not root.is_dir():
        return None
    candidates = list(root.glob(f'*-{run_id[:6]}')) if len(run_id) >= 6 else []
    for d in candidates:
        s = _read_state(state_path(d))
        if s and s['run_id'] == run_id:
            return d
    for p in sorted(root.glob(f'*/{STATE_NAME}')):
        s = _read_state(p)
        if s and s['run_id'] == run_id:
            return p.parent
    return None


def _settle_counts(snap):
    """Make an old record's clock and stage count agree with what it says.

    Records written before `finished_at` was part of the snapshot have no end
    time, so a viewer counted their elapsed time up to "now" for ever: a thirty
    second run read back the next morning said fourteen hours. The last time the
    record was written is the latest moment it can have finished, so that is
    used, and the elapsed figure is frozen at it. The stage count is recomputed
    from the tick list beside it, because older code computed the two apart and
    they disagreed ("0 / 10" next to a ticked stage).
    """
    if snap.get('status') not in LIVE_STATUSES:
        if not snap.get('finished_at'):
            snap['finished_at'] = snap.get('written_at') or snap.get('updated_at')
        if snap.get('finished_at') and snap.get('started_at'):
            snap['elapsed_s'] = round(max(0.0, snap['finished_at'] - snap['started_at']), 1)
    rows = snap.get('progress')
    if isinstance(rows, list) and rows:
        counts = dict(snap.get('stage_counts') or {})
        counts['done'] = sum(1 for r in rows if isinstance(r, dict) and r.get('status') == 'done')
        counts['total'] = len(rows)
        counts['current'] = next((r.get('stage') for r in rows
                                  if isinstance(r, dict) and r.get('status') == 'current'), None)
        snap['stage_counts'] = counts


def restore(runs_dir, run_id, *, after=-1):
    """A snapshot for *run_id* read back from disk, or None if there is no record.

    The shape matches `DiscoveryRun.snapshot` so the interface renders a restored
    run with the code it already has. Two fields are added: `recovered`, so the
    interface can say where this came from, and `interrupted`, when the state
    says running and nothing is running it.
    """
    d = find(runs_dir, run_id)
    if d is None:
        return None
    snap = _read_state(state_path(d))
    if snap is None:
        return None
    snap['events'] = read_events(d, after=after)
    snap['event_count'] = len(read_events(d, after=-1))
    snap['recovered'] = True
    live = snap.get('status') in LIVE_STATUSES
    if live:
        age = time.time() - (snap.get('written_at') or 0)
        snap['interrupted'] = True
        snap['status'] = 'interrupted'
        snap['error'] = ('The server restarted while this run was in flight, so its stream '
                         'ended. Whatever the agents had written is still in the run '
                         'directory; nothing here is a result that was not produced.')
        snap['error_reason'] = 'interrupted'
        snap['next_step'] = ('Start the run again. It was stopped, not completed, and no '
                             'partial answer is reported as a finding.'
                             if age > STALE_AFTER_S else
                             'Start the run again, or reload in a moment in case this process '
                             'is still coming up.')
    # A real run that ended (stopped, interrupted, failed) can continue as a
    # fresh run seeded with its artifacts. Set from the resolved status and the
    # record's own runtime, because the stored flag was written while the run
    # was live and so is always false. A restored run is never itself live, so
    # it can never be paused or pausable.
    has_request = (Path(d) / 'discovery_request.json').is_file()
    snap['continuable'] = bool(snap.get('is_real')
                               and snap.get('status') in ('stopped', 'interrupted', 'error')
                               and has_request)
    # Any ended real run can come back with results as its next round.
    snap['followable'] = bool(snap.get('is_real') and has_request)
    snap['pausable'] = False
    _settle_counts(snap)
    snap.pop('written_at', None)
    return snap
