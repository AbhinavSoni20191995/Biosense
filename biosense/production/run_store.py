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


def _read_state(path):
    try:
        d = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get('run_id') else None


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
    live = snap.get('status') in ('queued', 'running')
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
    snap.pop('written_at', None)
    return snap
