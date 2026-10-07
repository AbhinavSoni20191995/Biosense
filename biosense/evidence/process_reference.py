"""Established suspension-culture setpoints, shared across target cell types.

Temperature, dissolved oxygen, seeding density, feed exchange and agitation in
stirred culture of human iPSC and their derivatives are largely the same
whatever the cells become, and a protocol must not leave them blank because no
paper about this exact cell states them. This module holds those values with
their basis, and fills a protocol's physical-setpoint gaps from them — as a
design choice a person approves, never as a reported value.

Two homes, read together:

* the committed file (`process_reference/process_reference.json`) — conventions
  only, such as 37 degC, which need no citation;
* the server's private data root (`process_reference.json` there) — cited
  entries collected by a "Build the process reference" run, each with its
  source and the quoted passage, and promoted by a named reviewer. They live on
  the volume, not in the repository, because they are this deployment's
  curation.

Rules this module enforces, because each failure would mislead:

* a `cited` entry has at least one source with a quote; a `convention` has none;
* an entry's `typical` is one source's value or the convention, never an average;
* agitation in rpm is vessel-specific — an entry naming no vessel fills a gap
  only at low confidence, and says why;
* an unreviewed entry fills a gap only at low confidence, and says so.

    python -m biosense.evidence.process_reference show
    python -m biosense.evidence.process_reference template
    python -m biosense.evidence.process_reference promote --draft <file> --reviewed-by "<name>"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import contracts as K

COMMITTED = K.ROOT / 'process_reference' / 'process_reference.json'
PRIVATE_NAME = 'process_reference.json'
DRAFT_NAME = 'process_reference.draft.json'
ORDER = {'low': 0, 'moderate': 1, 'high': 2}


def private_path():
    from ..data import roots as DR
    return DR.private_root() / PRIVATE_NAME


def _read(path):
    try:
        doc = K.read_json(path)
    except (OSError, ValueError):
        return []
    if K.schema_errors('process_reference', doc):
        return []
    return list(doc.get('entries') or [])


def load():
    """Every entry: the committed conventions, then this server's curated ones.

    A curated entry with the same id replaces the committed one.
    """
    by_id = {e['entry_id']: e for e in _read(COMMITTED)}
    for e in _read(private_path()):
        by_id[e['entry_id']] = e
    return list(by_id.values())


def check_entry(e):
    """Refusals the schema cannot express."""
    if e['basis'] == 'cited' and not e.get('sources'):
        raise K.ContractError(f'{e["entry_id"]}: a cited entry needs at least one source with '
                              f'the quoted passage it rests on')
    if e['basis'] == 'convention' and e.get('sources'):
        raise K.ContractError(f'{e["entry_id"]}: a convention carries no source; if a paper is '
                              f'the basis, mark it cited')
    if e.get('typical') is None and not e.get('range'):
        raise K.ContractError(f'{e["entry_id"]}: give the typical value a source uses, a range, '
                              f'or both')
    rng = e.get('range')
    if rng and rng['lower'] > rng['upper']:
        raise K.ContractError(f'{e["entry_id"]}: range lower is above upper')
    if rng and e.get('typical') is not None and not rng['lower'] <= e['typical'] <= rng['upper']:
        raise K.ContractError(f'{e["entry_id"]}: the typical value lies outside its own range')
    return e


def entry_for(parameter_id, project=None):
    """The best entry for a parameter in this project, with its effective confidence.

    Returns (entry, confidence, caveats) or None. A vessel-specific entry wins
    over a general one when the project names the same vessel; reviewed beats
    unreviewed; then the stated confidence.
    """
    vessel = ((getattr(project, 'doc', None) or {}).get('biological_system') or {}).get('vessel') \
        if project is not None else None
    best = None
    for e in load():
        if e['parameter_id'] != parameter_id:
            continue
        if e.get('typical') is None:
            continue    # a range alone is not a value to set; it stays a gap with the range shown
        conf, caveats = e['confidence'], []
        if e['status'] != 'reviewed':
            conf = 'low'
            caveats.append('from an unreviewed reference entry')
        ev = (e.get('context') or {}).get('vessel')
        if parameter_id == 'agitation_rpm':
            if not ev:
                conf = 'low'
                caveats.append('rpm is vessel-specific and this entry names no vessel')
            elif vessel and ev.lower() != str(vessel).lower():
                conf = 'low'
                caveats.append(f'set in a {ev}, not this project\'s {vessel}')
        key = (e['status'] == 'reviewed', ORDER[conf],
               bool(vessel and ev and ev.lower() == str(vessel).lower()))
        if best is None or key > best[0]:
            best = (key, e, conf, caveats)
    return None if best is None else best[1:]


def as_design_choice(parameter_id, project=None):
    """A reference entry in the shape a protocol's design choice takes, or None."""
    found = entry_for(parameter_id, project)
    if not found:
        return None
    e, conf, caveats = found
    refs = ([s['ref'] for s in e.get('sources') or []]
            or [f'convention: {e.get("notes") or e["entry_id"]}'])
    ctx = e.get('context') or {}
    where = ', '.join(x for x in (ctx.get('cells'), ctx.get('format'), ctx.get('vessel')) if x)
    return {
        'parameter_id': parameter_id, 'label': None, 'unit': e['unit'],
        'value': e['typical'], 'range': e.get('range'),
        'rationale': (f'From the process reference ({e["entry_id"]}, {e["basis"]}'
                      + (f'; {where}' if where else '') + ')'
                      + (f'. Caveat: {"; ".join(caveats)}' if caveats else '')),
        'derived_from': [f'process_reference:{e["entry_id"]}'] + refs,
        'confidence': conf,
        'context_note': where or None,
        'would_settle_it': e.get('notes'),
        'risk_if_wrong': None,
        'reference_entry': e['entry_id'],
    }


def promote(draft, *, reviewed_by, from_run=None, now=None):
    """Check a draft's entries and merge them into this server's reference.

    The reviewer is a named person; their name is recorded on every entry they
    promote. Returns the ids promoted. An entry that fails a rule is refused
    with its reason, and nothing is written.
    """
    if not (reviewed_by or '').strip():
        raise K.ContractError('promoting entries needs the name of the person reviewing them')
    entries = (draft or {}).get('entries') or []
    if not entries:
        raise K.ContractError('the draft has no entries')
    stamped = []
    for e in entries:
        e = dict(e, status='reviewed', reviewed_by=reviewed_by.strip(),
                 reviewed_at=now or K.now_iso(), from_run=from_run or e.get('from_run'))
        e.setdefault('context', None)
        stamped.append(e)
    doc = {'schema_version': K.PRODUCTION_VERSION, 'updated_at': K.now_iso(),
           'note': 'Curated on this server: cited entries promoted by a named reviewer.',
           'entries': stamped}
    errors = K.schema_errors('process_reference', doc)
    if errors:
        raise K.ContractError('the draft does not fit the process reference: '
                              + '; '.join(errors[:5]))
    for e in stamped:
        check_entry(e)
    path = private_path()
    existing = {e['entry_id']: e for e in _read(path)}
    for e in stamped:
        existing[e['entry_id']] = e
    path.parent.mkdir(parents=True, exist_ok=True)
    K.write_json_atomic(path, dict(doc, entries=list(existing.values())))
    return [e['entry_id'] for e in stamped]


TEMPLATE = {
    'schema_version': '2.0', 'updated_at': None,
    'note': 'Draft from a run. Each cited entry quotes its source; a convention has none.',
    'entries': [
        {'entry_id': 'agitation.ipsc-aggregates-spinner', 'parameter_id': 'agitation_rpm',
         'unit': 'rpm', 'typical': None, 'range': None,
         'context': {'cells': 'hiPSC aggregates', 'format': 'stirred suspension',
                     'vessel': 'spinner flask <volume>', 'stage': 'expansion'},
         'basis': 'cited',
         'sources': [{'ref': 'PMID:<id>', 'quote': '<the sentence stating the value>',
                      'locator': 'Methods, paragraph p0012'}],
         'confidence': 'moderate', 'status': 'unreviewed', 'reviewed_by': None,
         'reviewed_at': None, 'from_run': None,
         'notes': 'What would settle it here: an agitation series measuring aggregate size.'},
    ],
}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('show', help='every entry in force, with its basis')
    sub.add_parser('template', help='the shape of a draft')
    p = sub.add_parser('promote', help='check a draft and add it to this server\'s reference')
    p.add_argument('--draft', required=True)
    p.add_argument('--reviewed-by', required=True)
    p.add_argument('--from-run')
    a = ap.parse_args(argv)
    if a.cmd == 'template':
        print(json.dumps(TEMPLATE, indent=2))
        return 0
    if a.cmd == 'show':
        rows = [{'entry_id': e['entry_id'], 'parameter_id': e['parameter_id'],
                 'typical': e.get('typical'), 'unit': e['unit'], 'range': e.get('range'),
                 'basis': e['basis'], 'status': e['status'], 'confidence': e['confidence'],
                 'vessel': (e.get('context') or {}).get('vessel')} for e in load()]
        print(json.dumps({'entries': rows, 'private_file': str(private_path())}, indent=1))
        return 0
    try:
        ids = promote(K.read_json(a.draft), reviewed_by=a.reviewed_by, from_run=a.from_run)
    except (K.ContractError, OSError, ValueError) as e:
        print(json.dumps({'refused': True, 'reason': str(e)}))
        return 1
    print(json.dumps({'promoted': ids, 'file': str(private_path())}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
