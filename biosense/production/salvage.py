"""Keep what a run had written when the server restarted under it.

A redeploy kills the container, and with it the Omnigent server and every
agent in flight. The run's record then said `interrupted` and showed nothing —
while the agents' files sat in the run directory on the volume: a literature
digest, the insights written as it read, sometimes a hypothesis already
validated. Twenty minutes of a real run, lost to the deploy that came after it.

On startup, each run whose record still says it is running, and that this new
process is not running, is finished from its directory: the same ingestion
that ends a run normally reads what is there, validates it, and attaches it as
the result. The status stays `interrupted` and says so — a partial answer is
labelled as what it is, never as a finished one — but nothing the agents
wrote is thrown away. A run is salvaged once; the mark is in its record.

The agents themselves cannot be resumed: they were processes in the container
that was replaced. Not deploying while a run is in flight is still the only
way to let one finish.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from .. import contracts as K
from . import run_store as RS

NOTE = ('The server restarted while this run was in flight, so its agents were stopped. '
        'What they had already written is shown below as a partial result: it was read '
        'from the run directory and validated, and nothing here is a finished answer.')
NEXT = ('Start the run again for a complete answer. To let a run finish, avoid deploying '
        'while one is in flight.')


class _Owner:
    def __init__(self, owner):
        self.owner = owner


def _projects_dir_for(owner):
    if not owner:
        return None
    from .. import workspace as WS
    try:
        d = WS.projects_dir(_Owner(owner))
    except Exception:  # noqa: BLE001 - an unreadable workspace means the committed set
        return None
    return str(d) if d.is_dir() and any(d.glob('*.json')) else None


def salvage_one(out_dir, state):
    """Finish one interrupted run from its directory. Returns the new state."""
    from . import discovery_runner as DRUN
    out = Path(out_dir)
    final, why = None, None
    req_path = out / 'discovery_request.json'
    if req_path.is_file():
        try:
            request = K.read_json(req_path)
            final = DRUN.finish(request, out, runtime_mode=state.get('runtime_mode'),
                                projects_dir=_projects_dir_for(state.get('owner')),
                                session=None)
        except Exception as e:  # noqa: BLE001 - a salvage that fails keeps the record as it was
            why = f'{type(e).__name__}: {str(e)[:300]}'
    else:
        why = 'the run directory holds no request, so there was nothing to finish from'
    new = dict(state)
    new.update(status='interrupted', error=NOTE, error_reason='interrupted', next_step=NEXT,
               result=final, salvaged=True, salvage_error=why,
               finished_at=state.get('finished_at') or state.get('written_at') or time.time())
    new.pop('written_at', None)
    RS.write_state(out, new)
    return new


def salvage(runs_dir, *, live_ids=()):
    """Every interrupted run under *runs_dir*, finished from its files. Returns their ids."""
    root = Path(runs_dir)
    done = []
    if not root.is_dir():
        return done
    for p in sorted(root.glob(f'*/{RS.STATE_NAME}')):
        state = RS._read_state(p)
        if not state or state.get('salvaged'):
            continue
        if state.get('status') not in RS.LIVE_STATUSES or state.get('run_id') in live_ids:
            continue
        try:
            salvage_one(p.parent, state)
            done.append(state['run_id'])
        except Exception:  # noqa: BLE001 - one bad record must not stop the rest
            continue
    return done


def salvage_in_background(runs_dir, *, log=print):
    """At startup, without holding the server up while files are read."""
    def work():
        ids = salvage(runs_dir)
        if ids:
            log(f'salvaged {len(ids)} run(s) interrupted by the last restart: {", ".join(ids)}')
    t = threading.Thread(target=work, name='salvage', daemon=True)
    t.start()
    return t
