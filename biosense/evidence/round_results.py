"""What came back from a round at the bench, tied to the plan it ran.

A round plan names its arms, its readouts and its replicates before any result
exists. The results are entered against exactly those names — by a scientist
today, by a connected bioreactor later, or both — so nothing can be recorded
that the plan did not ask for, and every value says where it came from.

Every comparison against the control is computed here, deterministically; an
agent reads `compare` and decides which of the plan's rules applies, quoting
these numbers, never its own arithmetic.

    python -m biosense.evidence.cli round-compare --plan round_plan.json \
        --results round_results.json --out round_comparison.json
"""
from __future__ import annotations

import math
from pathlib import Path

from .. import contracts as K

NAME = 'round_results.json'
SOURCES = ('scientist', 'instrument')
MAX_ENTRIES = 5000


def load(run_dir):
    """The results recorded for this run's round plan, or None."""
    try:
        doc = K.read_json(Path(run_dir) / NAME)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get('kind') == 'round_results' else None


def _plan(run_dir):
    try:
        plan = K.read_json(Path(run_dir) / 'round_plan.json')
    except (OSError, ValueError):
        plan = None
    if not isinstance(plan, dict) or plan.get('kind') != 'round_plan':
        raise K.ContractError('this run wrote no round plan, so there is nothing to enter '
                              'results against')
    return plan


def add(run_dir, entries, *, source='scientist', entered_by=None, run_id=None):
    """Record results against the run's round plan. Returns the whole record.

    Each entry names an arm, a replicate and a readout the plan has, and a
    finite number. A value entered again for the same arm, replicate, readout
    and source replaces the earlier one, and the earlier one is kept in
    `replaced` so a correction is visible rather than silent.
    """
    if source not in SOURCES:
        raise K.ContractError(f'source must be one of {SOURCES}')
    plan = _plan(run_dir)
    arms = {a['arm_id'] for a in plan.get('arms') or []}
    readouts = {r['name']: r.get('unit') for r in plan.get('readouts') or []}
    reps = int(plan.get('replicates') or 1)
    if not isinstance(entries, list) or not entries:
        raise K.ContractError('no results to record')
    rows = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise K.ContractError(f'entry {i} is not an object')
        arm, readout = str(e.get('arm_id') or ''), str(e.get('readout') or '')
        if arm not in arms:
            raise K.ContractError(f'entry {i}: arm {arm!r} is not in the round plan '
                                  f'({", ".join(sorted(arms))})')
        if readout not in readouts:
            raise K.ContractError(f'entry {i}: readout {readout!r} is not in the round plan '
                                  f'({", ".join(readouts)})')
        try:
            rep = int(e.get('replicate'))
            value = float(e.get('value'))
        except (TypeError, ValueError):
            raise K.ContractError(f'entry {i}: replicate is a whole number and value a '
                                  f'number') from None
        if not 1 <= rep <= reps:
            raise K.ContractError(f'entry {i}: replicate {rep} is outside 1..{reps}, the '
                                  f'replicates the plan set')
        if not math.isfinite(value):
            raise K.ContractError(f'entry {i}: value must be finite')
        rows.append({'arm_id': arm, 'replicate': rep, 'readout': readout, 'value': value,
                     'unit': readouts[readout], 'source': source,
                     'entered_by': entered_by, 'entered_at': K.now_iso(),
                     'note': (str(e.get('note') or '').strip()[:300] or None)})
    doc = load(run_dir) or {
        'kind': 'round_results', 'run_id': run_id, 'round': plan.get('round') or 1,
        'plan_commitment_sha256': plan.get('commitment_sha256'), 'entries': [],
        'replaced': [],
        'note': ('Measurements of this process, entered against the round plan. Scientist '
                 'entries are a person\'s report; instrument entries come from a connected '
                 'bioreactor. Neither is a cited value.')}
    if doc.get('plan_commitment_sha256') != plan.get('commitment_sha256'):
        raise K.ContractError('the round plan changed after results were recorded against it; '
                              'results stay tied to the plan they ran')
    key = lambda r: (r['arm_id'], r['replicate'], r['readout'], r['source'])
    new = {key(r): r for r in rows}
    kept = []
    for r in doc['entries']:
        if key(r) in new:
            doc['replaced'].append(dict(r, replaced_at=K.now_iso()))
        else:
            kept.append(r)
    doc['entries'] = (kept + rows)[:MAX_ENTRIES]
    K.write_json_atomic(Path(run_dir) / NAME, doc)
    return doc


def summary(run_dir):
    """What the run page shows: counts per source, and each arm's mean per readout."""
    doc = load(run_dir)
    if not doc:
        return None
    by = {}
    for r in doc['entries']:
        by.setdefault((r['arm_id'], r['readout']), []).append(r['value'])
    return {
        'round': doc.get('round'), 'entries': doc['entries'],
        'count': len(doc['entries']),
        'by_source': {s: sum(1 for r in doc['entries'] if r['source'] == s) for s in SOURCES},
        'means': [{'arm_id': a, 'readout': ro, 'n': len(v), 'mean': sum(v) / len(v)}
                  for (a, ro), v in sorted(by.items())],
        'corrections': len(doc.get('replaced') or []),
    }


def compare(plan, results):
    """Each arm against the control, per readout: the numbers a decision rule reads."""
    from ..bioinformatics.toolkit import statistics as S
    if (results.get('plan_commitment_sha256') and plan.get('commitment_sha256')
            and results['plan_commitment_sha256'] != plan['commitment_sha256']):
        raise K.ContractError('these results were recorded against a different round plan')
    control = next((a['arm_id'] for a in plan.get('arms') or [] if a.get('control')), None)
    if control is None:
        raise K.ContractError('the round plan has no control arm')
    values = {}
    for r in results.get('entries') or []:
        values.setdefault((r['arm_id'], r['readout']), []).append(r['value'])
    rows = []
    for ro in plan.get('readouts') or []:
        a = values.get((control, ro['name']), [])
        for arm in plan.get('arms') or []:
            if arm['arm_id'] == control:
                continue
            b = values.get((arm['arm_id'], ro['name']), [])
            if not a or not b:
                rows.append({'readout': ro['name'], 'arm_id': arm['arm_id'], 'unit': ro.get('unit'),
                             'note': 'not measured in this arm or in the control yet'})
                continue
            row = S.compare_groups(ro['name'], a, b, group_a=control, group_b=arm['arm_id'],
                                   effect_type='difference',
                                   independence_note=f'{len(a)} vs {len(b)} replicate(s)')
            row.update(arm_id=arm['arm_id'], unit=ro.get('unit'))
            rows.append(row)
    tested = [r for r in rows if r.get('p_value') is not None]
    if tested:
        S.apply_fdr(tested)
    return {'kind': 'round_comparison', 'round': plan.get('round'),
            'plan_commitment_sha256': plan.get('commitment_sha256'), 'control': control,
            'rows': rows, 'decision_rules': plan.get('decision_rules') or [],
            'sources': sorted({r['source'] for r in results.get('entries') or []}),
            'software': S.SOFTWARE, 'created_at': K.now_iso(),
            'note': ('Every number here is computed from the recorded results. Which decision '
                     'rule applies is read from these rows; the rules were fixed before the '
                     'results existed.')}
