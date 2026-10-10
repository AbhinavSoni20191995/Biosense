"""Every condition this run surfaced, as options a person can design a run from.

A protocol can only carry a dose for a parameter the project has. That is the
right rule — it is what stops a plausible number appearing with nothing behind
it — but it leaves a person looking at an empty stage with no idea what the
run actually learned was available. This gathers all of it into one list:
the levers in the protocol, the ones hypotheses named, the ones development
suggests, the terms the run proposed, and the factors the leading published
protocols use.

Each option is sorted into one of three, by what stands behind it and nothing
else:

* `necessary` — this process does not run without it. Either the protocol
  already carries it from reported or adapted evidence, or the benchmark
  protocol for this route lists it among the factors it uses.
* `potential` — a real option here: a live hypothesis named it with cited
  evidence, another published protocol uses it, or development suggests it and
  a protocol was found using it.
* `experimental` — nobody searched was found to use it: a developmental idea
  with no protocol behind it, or a response this run proposed de novo.

A dose is shown only where a source gives one, with where it came from. An
option with no dose says so; it never gets a number to look complete.
"""
from __future__ import annotations

import re

from .. import contracts as K

CLASSES = ('necessary', 'potential', 'experimental')
WHY = {
    'necessary': 'The process does not run without it: the protocol carries it on reported or '
                 'adapted evidence, or the benchmark protocol for this route uses it.',
    'potential': 'A real option here: a hypothesis named it with cited evidence, a published '
                 'protocol uses it, or development suggests it and a protocol was found using it.',
    'experimental': 'No protocol searched was found using it: a developmental idea with nothing '
                    'behind it yet, or a response this run proposed de novo.',
}
NOTE = ('Conditions this run surfaced, sorted by what stands behind each. A dose appears only '
        'where a source gives one. Registering an option as a parameter of the project is what '
        'lets a protocol carry its dose; until then it is a condition to design a run around, '
        'not a value the protocol holds.')


def _key(label, parameter_id=None):
    """One key per real thing, whatever named it.

    The protocol knows M-CSF as `mcsf_ng_ml` and a cited protocol calls it
    "M-CSF"; keyed apart they appear as two options, one with a dose and one
    without. The name decides, with punctuation and the unit suffix dropped.
    """
    name = str(label or '').strip()
    if not name and parameter_id:
        name = re.sub(r'_(ng_ml|ug_ml|mg_ml|um|nm|mm|pct|percent|h|hr|hours|days?|c|rpm|'
                      r'e6_per_ml|fraction)$', '', str(parameter_id).strip().lower())
    return re.sub(r'[^a-z0-9]+', '', name.lower()) or str(parameter_id or name).lower()


def _opt(key, label, *, kind, stage=None, unit=None, value=None, value_from=None,
         source=None, detail=None, refs=(), registered=False, parameter_id=None,
         hypothesis_id=None):
    return {'key': key, 'label': label, 'class': kind, 'stage_id': stage, 'unit': unit,
            'value': value, 'value_from': value_from, 'source': source, 'detail': detail,
            'refs': [str(r) for r in refs if r], 'registered': bool(registered),
            'parameter_id': parameter_id, 'hypothesis_id': hypothesis_id}


def _is_factor(unit):
    from ..production.first_pass import is_factor
    return is_factor(unit)


def build(doc, *, devmap=None, sota=None):
    """The option catalogue for one protocol, with what each rests on."""
    out, seen = [], {}

    def add(o):
        # The strongest standing wins: a lever the protocol carries is not
        # downgraded because a developmental idea also mentions it.
        prev = seen.get(o['key'])
        if prev is None:
            seen[o['key']] = o
            out.append(o)
            return
        if CLASSES.index(o['class']) < CLASSES.index(prev['class']):
            prev.update({k: o[k] for k in ('class', 'source', 'detail')})
        for k in ('value', 'unit', 'stage_id', 'parameter_id', 'hypothesis_id'):
            if prev.get(k) is None and o.get(k) is not None:
                prev[k] = o[k]
                if k == 'value':
                    prev['value_from'] = o['value_from']
        prev['refs'] = sorted(set(prev['refs']) | set(o['refs']))

    labels = {s['stage_id']: s.get('label') or s['stage_id']
              for s in (doc.get('timeline') or {}).get('stages') or []}

    # 1. What the protocol already carries. A factor held on reported or
    #    adapted evidence is one the process needs; a design choice is a
    #    starting value somebody picked, which is an option, not a necessity.
    for st in doc.get('stages') or []:
        for q in st.get('parameters') or []:
            if not _is_factor(q.get('unit')):
                continue
            prov = q.get('provenance')
            kind = 'necessary' if prov in ('reported', 'adapted') else 'potential'
            add(_opt(_key(q['label'], q['parameter_id']), q['label'], kind=kind, stage=st['stage_id'],
                     unit=q.get('unit'), value=q.get('recommended_value'), value_from=prov,
                     source='in the protocol', registered=True, parameter_id=q['parameter_id'],
                     hypothesis_id=q.get('hypothesis_ref'),
                     detail=f'Carried by the protocol at {labels.get(st["stage_id"], st["stage_id"])}.'))

    # 2. Levers a live hypothesis named that the project does not have yet.
    ruled = {'contradicted', 'superseded', 'rejected'}
    ledger = {r['hypothesis_id']: r for r in doc.get('hypothesis_ledger') or []}
    for c in (doc.get('timeline') or {}).get('candidates') or []:
        row = ledger.get(c.get('hypothesis_id')) or {}
        if row.get('status') in ruled:
            continue
        from ..production.first_pass import split_label
        label, unit_in = split_label(c.get('label'))
        add(_opt(_key(label, c['parameter_id']), label, kind='potential', stage=c.get('stage_id'),
                 unit=c.get('unit') or unit_in, value=c.get('candidate_value'),
                 value_from='hypothesis' if c.get('candidate_value') is not None else None,
                 source=f'proposed by {c.get("hypothesis_id")}' if c.get('hypothesis_id')
                        else 'proposed by this run',
                 detail=c.get('statement'), parameter_id=c['parameter_id'],
                 hypothesis_id=c.get('hypothesis_id')))

    # 3. What the leading published protocols use. The benchmark's factors are
    #    what this route is made of; another route's are options to weigh.
    for ref in (sota or {}).get('references') or []:
        bench = ref.get('is_benchmark')
        for f in ref.get('key_factors') or []:
            add(_opt(_key(f), str(f).strip(),
                     kind='necessary' if bench else 'potential',
                     source=('used by the benchmark protocol' if bench
                             else f'used by {ref.get("citation")}'),
                     detail=ref.get('summary'), refs=ref.get('refs') or []))

    # 4. What development suggests. An idea no protocol searched was found to
    #    use is exactly what "experimental" means here.
    for idea in (devmap or {}).get('ideas') or []:
        found = ((idea.get('protocol_search') or {}).get('found_in_protocols'))
        add(_opt(_key(idea.get('lever') or idea.get('idea_id')),
                 idea.get('lever') or idea.get('idea_id'),
                 kind='potential' if found else 'experimental',
                 stage=idea.get('stage_id'),
                 source=f'development suggests it ({idea.get("novelty")})',
                 detail=idea.get('rationale'), refs=idea.get('developmental_refs') or [],
                 hypothesis_id=idea.get('hypothesis_id')))

    # 5. Responses this run proposed for levers the reactor has no term for.
    for t in doc.get('proposed_terms') or []:
        d = t.get('description') or {}
        add(_opt(_key(t.get('label'), t.get('parameter_id')), t.get('label'),
                 kind='experimental', stage=t.get('stage'), unit=t.get('unit'),
                 source='a response this run proposed (DE NOVO)',
                 detail=d.get('summary'), refs=d.get('references') or [],
                 parameter_id=t.get('parameter_id')))

    order = {c: i for i, c in enumerate(CLASSES)}
    out.sort(key=lambda o: (order[o['class']], not o['registered'], o['label'].lower()))
    groups = [{'class': c, 'why': WHY[c], 'options': [o for o in out if o['class'] == c]}
              for c in CLASSES]
    return {'groups': [g for g in groups if g['options']], 'count': len(out), 'note': NOTE}


def markdown(cat):
    """The catalogue as a section of the written report."""
    if not cat or not cat.get('count'):
        return []
    L = ['## Conditions this run surfaced', '', cat['note'], '']
    for g in cat['groups']:
        L += [f'**{g["class"].title()}** — {g["why"]}', '']
        for o in g['options']:
            dose = (f' at {K.fmt_number(o["value"]) if hasattr(K, "fmt_number") else o["value"]}'
                    f'{" " + o["unit"] if o.get("unit") else ""}'
                    if o.get('value') is not None else ' (no dose established)')
            where = f' — {o["stage_id"]}' if o.get('stage_id') else ''
            L.append(f'- {o["label"]}{dose}{where}; {o.get("source") or ""}'
                     + (f' [{", ".join(o["refs"])}]' if o.get('refs') else ''))
        L.append('')
    return L
