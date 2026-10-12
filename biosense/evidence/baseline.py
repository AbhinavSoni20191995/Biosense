"""The published way this cell is made, stage by stage, before this run changes it.

A protocol document carries a dose only for a parameter the project registered.
That rule is the reason nothing in this system invents a number, and it is not
negotiable. Its cost shows up in the plan: a project created from the quick
form has the vessel controls and no biology, so the expansion and
differentiation stages render as "no inducer set" — not because nothing is
known about them, but because nobody registered anywhere to put it. A reader
sees a protocol that starts at maturation and reasonably concludes the run only
looked there.

This is where that knowledge goes. The literature specialist writes what the
established route puts in the medium at each stage, and this checks it:

* **every stage of the project appears.** A stage the run found nothing for
  comes back `addressed: false` with the reason, never silently missing — the
  gap is the finding;
* a `reported` factor names a source and quotes the sentence. A dose with no
  source is refused whatever the provenance claims;
* `adapted` says which context it came from, because a dose moved from a flask
  to a stirred tank is a judgement and should read as one;
* `design_choice` carries a rationale and no citation, and is a person's to
  approve;
* `gap` carries no value at all and says what to search. A stage that needs a
  factor nobody could dose is more useful stated than left blank;
* nothing here is "optimal", "best" or "proven". The literature says what was
  done and what happened.

What this is **not** is a protocol. Nothing here is approved, nothing is a
protocol value, and registering a factor as a project parameter is still a
person's act. It is the published starting point, beside the run's own
recommendation, so that a scientist reading the plan can see the whole process
rather than the one stage this run had a hypothesis about.
"""
from __future__ import annotations

import re

from .. import contracts as K

ROLES = ('essential', 'supporting', 'optional')
PROVENANCE = ('reported', 'adapted', 'design_choice', 'gap')
# The literature says what was done and what happened. It does not establish
# that anything is the best way to do it, and a baseline that says so would
# turn a starting point into a recommendation nobody checked.
OVERCLAIM = re.compile(r'\b(optimal|optimis\w+|optimiz\w+|the best|best[- ]in[- ]class|'
                       r'ideal|guarantee\w*|proven to|definitive|gold[- ]standard)\b', re.I)
NOTE = ('The published route, stage by stage, as the sources state it. Not a protocol and not '
        'approved: a factor here carries a dose only where a source gives one, and becomes a '
        'protocol value only once somebody registers it as a parameter of the project and a '
        'named person approves the protocol.')
UNADDRESSED = ('This run established nothing for this stage. That is a gap in what was '
               'gathered, not evidence that the stage needs no factors.')

TEMPLATE = {
    'cell_type': 'iPSC-derived alveolar macrophage',
    'route': 'iPSC directed differentiation through hemogenic endothelium',
    'base_medium': 'serum-free myeloid base medium',
    'stages': [
        {'stage_id': 'expansion', 'label': 'Expansion',
         'purpose': 'Grow the iPSC aggregates to the size the next stage starts from.',
         'factors': [
             {'name': 'Y-27632 (ROCK inhibitor)', 'role': 'supporting', 'value': 10,
              'unit': 'uM', 'timing': 'first 24 h after seeding only',
              'provenance': 'reported',
              'why': 'Prevents dissociation-induced apoptosis while single cells re-aggregate.',
              'sources': [{'ref': 'Watanabe2007', 'quote': 'addition of Y-27632 markedly '
                           'diminished dissociation-induced apoptosis', 'locator': 'p. 682'}]},
         ]},
        {'stage_id': 'myeloid', 'label': 'Myeloid harvest',
         'purpose': 'Continuous release of myeloid progenitors from the factories.',
         'factors': [
             {'name': 'M-CSF', 'role': 'essential', 'value': 100, 'unit': 'ng/mL',
              'timing': 'from day 14 to harvest, every feed', 'provenance': 'reported',
              'why': 'Drives myeloid commitment and the monocyte output of the factories.',
              'sources': [{'ref': 'vanWilgenburg2013', 'quote': 'medium supplemented with '
                           '100 ng/ml M-CSF and 25 ng/ml IL-3', 'locator': 'Methods'}]},
             {'name': 'GM-CSF', 'role': 'essential', 'provenance': 'gap',
              'why': 'Alveolar specification needs it, and no source found here doses it for '
                     'a stirred tank at this stage.',
              'what_to_search': 'GM-CSF concentration iPSC alveolar macrophage suspension '
                                'differentiation'},
         ]},
    ],
    'limitations': ['No source found gives a stirred-tank dose for the maturation factors.'],
}


def _text(v, where, *, minimum=1, required=True):
    s = str(v or '').strip()
    if not s:
        if required:
            raise K.ContractError(f'{where} is required')
        return None
    if len(s) < minimum:
        raise K.ContractError(f'{where} is too short to be useful ({minimum}+ characters)')
    if OVERCLAIM.search(s):
        raise K.ContractError(
            f'{where} says {OVERCLAIM.search(s).group(0)!r}. This document records what the '
            f'sources state was done, not that it is the best way to do it; say what the '
            f'source reports and leave the judgement to the reader')
    return s


def _enum(v, allowed, where):
    s = str(v or '').strip()
    if s not in allowed:
        raise K.ContractError(f'{where} must be one of {", ".join(allowed)}; got {s!r}')
    return s


def _slug(name, used):
    base = re.sub(r'[^a-z0-9]+', '_', str(name or '').lower()).strip('_')[:48] or 'factor'
    if base[0].isdigit():
        base = f'f_{base}'[:48]
    pid, n = base, 2
    while pid in used:
        pid = f'{base}_{n}'[:48]
        n += 1
    used.add(pid)
    return pid


def _key(name):
    """One key per real thing, whatever it is called.

    'M-CSF', 'M CSF', 'mcsf_ng_ml' and 'M-CSF concentration' are one factor.
    Keyed apart, the plan offers to register a parameter the project already
    has, which is the most confusing thing it could offer.
    """
    base = re.sub(r'_(ng_ml|ug_ml|mg_ml|um|nm|mm|pct|percent)$', '',
                  str(name or '').strip().lower())
    base = re.split(r'\b(concentration|conc|dose|level|supplement(?:ation)?)\b', base)[0]
    return re.sub(r'[^a-z0-9]+', '', base)


def _project_factors(project):
    """{normalised name: parameter_id} for what the project already carries."""
    out = {}
    for pid in project.parameter_ids:
        out.setdefault(_key(pid), pid)
        label = getattr(project.parameter(pid), 'label', None)
        if label:
            out.setdefault(_key(label), pid)
    out.pop('', None)
    return out


def _factor(raw, where, used, known):
    name = _text(raw.get('name'), f'{where}.name', minimum=2)
    prov = _enum(raw.get('provenance'), PROVENANCE, f'{where}.provenance')
    role = _enum(raw.get('role'), ROLES, f'{where}.role')
    sources = []
    for j, s in enumerate(raw.get('sources') or []):
        w = f'{where}.sources[{j}]'
        sources.append({'ref': _text((s or {}).get('ref'), f'{w}.ref', minimum=2),
                        'quote': _text((s or {}).get('quote'), f'{w}.quote', minimum=3),
                        'locator': _text((s or {}).get('locator'), f'{w}.locator', required=False),
                        'context': _text((s or {}).get('context'), f'{w}.context', required=False)})
    value = raw.get('value')
    if value is not None and not isinstance(value, (int, float)):
        raise K.ContractError(f'{where}.value is a number or nothing; got {value!r}')

    if prov == 'reported' and not sources:
        raise K.ContractError(
            f'{where} ({name}) is reported with nothing cited. A reported factor names the '
            f'source and quotes the sentence it rests on, or it is a design_choice')
    if prov == 'adapted':
        if not sources:
            raise K.ContractError(f'{where} ({name}) is adapted from nothing cited')
        if not str(raw.get('adapted_from') or '').strip():
            raise K.ContractError(
                f'{where} ({name}) is adapted: say in adapted_from which context the value came '
                f'from, because moving a dose between systems is a judgement and should read '
                f'as one')
    if prov == 'gap':
        if value is not None or raw.get('range'):
            raise K.ContractError(
                f'{where} ({name}) is a gap and carries a value. A gap is what this run could '
                f'not establish; a number here would be the invention the gap exists to avoid')
        if not str(raw.get('what_to_search') or '').strip():
            raise K.ContractError(f'{where} ({name}) is a gap with no search to run next')
    if value is not None and prov in ('reported', 'adapted') and not sources:
        raise K.ContractError(f'{where} ({name}) gives a dose with no source')
    if value is not None and not str(raw.get('unit') or '').strip():
        raise K.ContractError(f'{where} ({name}) gives {value} with no unit')
    rng = raw.get('range')
    if rng is not None:
        if not isinstance(rng, dict) or {'lower', 'upper'} - set(rng):
            raise K.ContractError(f'{where}.range is {{"lower": .., "upper": ..}}')
        if rng['lower'] > rng['upper']:
            raise K.ContractError(f'{where}.range: lower is above upper')

    return {
        'factor_id': _slug(name, used), 'name': name, 'role': role,
        'value': float(value) if isinstance(value, (int, float)) else None,
        'unit': _text(raw.get('unit'), f'{where}.unit', required=False),
        'range': ({'lower': float(rng['lower']), 'upper': float(rng['upper'])} if rng else None),
        'timing': _text(raw.get('timing'), f'{where}.timing', required=False),
        'provenance': prov, 'sources': sources,
        'why': _text(raw.get('why'), f'{where}.why', minimum=10),
        'adapted_from': _text(raw.get('adapted_from'), f'{where}.adapted_from', required=False),
        'what_to_search': _text(raw.get('what_to_search'), f'{where}.what_to_search',
                                required=False),
        # Whether the project can hold this is a fact about the project, so it
        # is read from the project rather than taken from the draft.
        'in_project': known.get(_key(name)),
        'notes': _text(raw.get('notes'), f'{where}.notes', required=False),
    }


def build(draft, *, project, run_id=None, created_by=None):
    """A checked BaselineProtocol from an agent's draft, against a project's stages."""
    if not isinstance(draft, dict):
        raise K.ContractError('a baseline draft is a JSON object; see '
                              '`python -m biosense.evidence.cli template baseline`')
    stage_ids = {s['stage_id']: (s.get('label') or s['stage_id']) for s in project.stages}
    known = _project_factors(project)
    stages, seen = [], set()
    for i, st in enumerate(draft.get('stages') or []):
        w = f'stages[{i}]'
        sid = str((st or {}).get('stage_id') or '').strip()
        if sid not in stage_ids:
            raise K.ContractError(
                f'{w}.stage_id is {sid!r}; this project\'s stages are '
                f'{", ".join(stage_ids)}. A baseline is laid out on the process it is for')
        if sid in seen:
            raise K.ContractError(f'{w}: stage {sid!r} appears twice')
        seen.add(sid)
        used = set()
        factors, names = [], {}
        for j, f in enumerate(st.get('factors') or []):
            row = _factor(f or {}, f'{w}.factors[{j}]', used, known)
            k = _key(row['name'])
            if k in names:
                raise K.ContractError(
                    f'{w}: {row["name"]!r} and {names[k]!r} are the same factor in one stage; '
                    f'give it once, with the timing that distinguishes them')
            names[k] = row['name']
            factors.append(row)
        stages.append({
            'stage_id': sid, 'label': stage_ids[sid],
            'purpose': _text(st.get('purpose'), f'{w}.purpose', minimum=10),
            'addressed': True,
            'base_medium': _text(st.get('base_medium'), f'{w}.base_medium', required=False),
            'note': _text(st.get('note'), f'{w}.note', required=False),
            'factors': factors,
        })
    if not stages:
        raise K.ContractError('a baseline covers at least one stage of the process, or it is a '
                              'limitation rather than a document')

    # Every stage of the project appears. A stage left out of the draft is the
    # case this document exists for: the plan would otherwise show it empty
    # with nothing saying whether that is ignorance or a finding.
    for sid, label in stage_ids.items():
        if sid not in seen:
            stages.append({'stage_id': sid, 'label': label, 'purpose': UNADDRESSED,
                           'addressed': False, 'base_medium': None,
                           'note': 'Not addressed by this run.', 'factors': []})
    order = list(stage_ids)
    stages.sort(key=lambda s: order.index(s['stage_id']))

    allf = [f for s in stages for f in s['factors']]
    doc = {
        'kind': 'baseline_protocol', 'schema_version': K.PRODUCTION_VERSION,
        'project_id': project.project_id, 'run_id': run_id, 'created_at': K.now_iso(),
        'created_by': created_by,
        'cell_type': _text(draft.get('cell_type'), 'cell_type', minimum=2),
        'route': _text(draft.get('route'), 'route', required=False),
        'base_medium': _text(draft.get('base_medium'), 'base_medium', required=False),
        'stages': stages,
        'coverage': {
            'stages_total': len(stages),
            'stages_addressed': sum(1 for s in stages if s['addressed'] and s['factors']),
            'factors': len(allf),
            'reported': sum(1 for f in allf if f['provenance'] == 'reported'),
            'gaps': sum(1 for f in allf if f['provenance'] == 'gap'),
            'already_parameters': sum(1 for f in allf if f['in_project']),
        },
        'limitations': [_text(x, 'limitations', minimum=1)
                        for x in draft.get('limitations') or []],
        'note': NOTE,
    }
    K.require_valid('baseline_protocol', doc)
    return doc


def markdown(doc):
    """The baseline as a section of the written report."""
    if not doc or not doc.get('stages'):
        return []
    c = doc.get('coverage') or {}
    L = ['## The published route, stage by stage', '',
         f'{doc["cell_type"]}'
         + (f' — {doc["route"]}' if doc.get('route') else '') + '.', '', doc['note'], '',
         f'{c.get("stages_addressed", 0)} of {c.get("stages_total", 0)} stages have factors; '
         f'{c.get("reported", 0)} of {c.get("factors", 0)} carry a reported dose; '
         f'{c.get("gaps", 0)} '
         + ('is a gap.' if c.get('gaps') == 1 else 'are gaps.'), '']
    for s in doc['stages']:
        L += [f'### {s["label"]}', '', s['purpose'], '']
        if not s['factors']:
            L += ['Nothing established for this stage by this run.', '']
            continue
        for f in s['factors']:
            dose = (f'{f["value"]:g} {f["unit"]}' if f.get('value') is not None
                    else 'no dose established')
            when = f' · {f["timing"]}' if f.get('timing') else ''
            refs = ', '.join(x['ref'] for x in f.get('sources') or [])
            L.append(f'- **{f["name"]}** ({f["role"]}) — {dose}{when} · {f["provenance"]}'
                     + (f' [{refs}]' if refs else '') + f'. {f["why"]}'
                     + (f' Search: {f["what_to_search"]}' if f.get('what_to_search') else ''))
        L.append('')
    return L
