"""What somebody actually needs to run round 1.

An arms table is not a protocol. A scientist reading "A2: GM-CSF 50 ng/mL
replacing M-CSF" cannot start: which medium, made up how, which reagent from
whom, at what stock concentration, seeded at what density, fed how often, for
how long, and what are the two mistakes that ruin the run. Those are the
questions that stand between a recommendation and an experiment, and the run
has usually read the papers that answer them.

The rule this module exists to hold is about sourcing. A supplier and a
catalogue number are the easiest thing in this system to invent — they look
like facts, they are rarely checked before ordering, and a wrong one costs a
week. So they may be recorded **only** where a source states them, with the
source and the quoted sentence; anything else is `your_lab`, which says to use
your own qualified stock and names nobody. A catalogue number with no citation
is refused outright rather than softened with a caveat.

Everything else keeps the system's ordinary provenance: `reported` with its
source, `adapted` from a stated other context, `design_choice` with a reason,
`gap` where nobody knows. A do or a don't carries what goes wrong if it is
ignored, because a rule with no reason is folklore and gets dropped by the
first person in a hurry.

None of this is approved, and none of it is an instruction to an instrument.

    python -m biosense.evidence.cli template bench
    python -m biosense.evidence.cli bench --project P --draft D --out bench_setup.json
"""
from __future__ import annotations

import re

from .. import contracts as K

PROVENANCE = ('reported', 'adapted', 'design_choice', 'gap')
SOURCING = ('from_source', 'your_lab')
MAX_REAGENTS = 60
MAX_STEPS = 40
# A catalogue number is a string of digits and dashes that reads like a fact.
# Written without a source it is the most expensive kind of invention here.
CATALOGUE_RULE = ('A supplier or catalogue number may only be recorded where a source states '
                  'it, with the sentence quoted. Nobody checks a catalogue number before '
                  'ordering, and a wrong one costs a week: so one with nothing behind it is '
                  'refused rather than caveated. Use sourcing "your_lab" and say what to use '
                  'from your own stock.')
NOTE = ('What round 1 needs at the bench, from what this run read. Supplier and catalogue '
        'appear only where a source states them; everything else says to use your own '
        'qualified stock. Nothing here is approved: a named person approves the protocol '
        'before anything runs, and your lab\'s own SOPs and safety rules come first.')

TEMPLATE = {
    'culture_format': 'six_well',
    'vessel': '6-well plate, tissue-culture treated',
    'media': [
        {'stage_id': 'myeloid', 'base_medium': 'X-VIVO 15',
         'supplements': ['GlutaMAX 2 mM', 'beta-mercaptoethanol 50 uM'],
         'serum': 'serum-free',
         'prepare': 'Make up the full medium fresh each feed; hold the base at 4 degC for no '
                    'more than 2 weeks after opening.',
         'provenance': 'reported',
         'sources': [{'ref': 'vanWilgenburg2013', 'quote': 'X-VIVO 15 supplemented with '
                      'GlutaMAX and beta-mercaptoethanol', 'locator': 'Methods'}]},
    ],
    'reagents': [
        {'name': 'M-CSF', 'role': 'drives myeloid commitment and monocyte output',
         'stage_ids': ['myeloid'], 'working_concentration': '100 ng/mL',
         'sourcing': 'your_lab', 'provenance': 'reported',
         'stock': 'reconstitute to 100 ug/mL in sterile PBS with 0.1% BSA; aliquot so no vial '
                  'is thawed twice',
         'storage': '-80 degC; working aliquot at 4 degC for up to one week',
         'handling': 'carrier protein matters at this concentration: without BSA the cytokine '
                     'adsorbs to the plastic',
         'sources': [{'ref': 'vanWilgenburg2013', 'quote': 'medium supplemented with 100 ng/ml '
                      'M-CSF', 'locator': 'Methods'}],
         'note': 'No source read here names a supplier, so use your own qualified stock and '
                 'record the lot.'},
    ],
    'handling': [
        {'step': 'Seeding', 'stage_id': 'myeloid',
         'what': 'Seed the factories into 2 mL of complete medium per well, one well per arm '
                 'per replicate; record the actual count.',
         'when': 'day 0 of the stage', 'provenance': 'design_choice'},
        {'step': 'Feed', 'stage_id': 'myeloid',
         'what': 'Replace half the medium with fresh complete medium, leaving the aggregates '
                 'undisturbed.',
         'when': 'every 2-3 days', 'provenance': 'adapted',
         'sources': [{'ref': 'vanWilgenburg2013', 'quote': 'fed every 3-4 days'}]},
    ],
    'do': [
        {'rule': 'Randomise which wells get which arm, and keep the plate edges for medium '
                 'rather than for samples.',
         'why': 'Edge wells evaporate faster than the middle, so an arm sitting on the edge is '
                'confounded with its position.',
         'provenance': 'design_choice'},
    ],
    'dont': [
        {'rule': 'Do not re-freeze a thawed cytokine aliquot.',
         'why': 'Freeze-thaw loses activity, and an arm that silently got less factor reads as '
                'a smaller effect.',
         'provenance': 'design_choice'},
    ],
    'safety': ['Human iPSC-derived material: handle at the containment level your institution '
               'requires, which this does not decide.'],
    'consumables': ['6-well tissue-culture plates', 'serological pipettes',
                    'low-protein-binding tubes for cytokine aliquots'],
    'limitations': ['No source read here names a supplier for any factor.'],
}


def _text(v, where, *, minimum=1, required=True, limit=2000):
    s = str(v or '').strip()
    if not s:
        if required:
            raise K.ContractError(f'{where} is required')
        return None
    if len(s) < minimum:
        raise K.ContractError(f'{where} is too short to be useful ({minimum}+ characters)')
    if len(s) > limit:
        raise K.ContractError(f'{where} is {len(s)} characters; the limit is {limit}')
    return s


def _enum(v, allowed, where, *, default=None):
    s = str(v or default or '').strip()
    if s not in allowed:
        raise K.ContractError(f'{where} must be one of {", ".join(allowed)}; got {s!r}')
    return s


def _sources(raw, where):
    out = []
    for i, s in enumerate(raw or []):
        w = f'{where}.sources[{i}]'
        out.append({'ref': _text((s or {}).get('ref'), f'{w}.ref', minimum=2),
                    'quote': _text((s or {}).get('quote'), f'{w}.quote', minimum=3),
                    'locator': _text((s or {}).get('locator'), f'{w}.locator', required=False)})
    return out


def _rule(raw, where):
    return {'rule': _text((raw or {}).get('rule'), f'{where}.rule', minimum=5),
            'why': _text((raw or {}).get('why'), f'{where}.why', minimum=10),
            'provenance': _enum((raw or {}).get('provenance'), PROVENANCE, f'{where}.provenance',
                                default='design_choice'),
            'sources': _sources((raw or {}).get('sources'), where)}


def build(draft, *, project, run_id=None, created_by=None, round_number=None,
          culture_format=None):
    """A checked BenchSetup from an agent's draft, against a project's stages."""
    if not isinstance(draft, dict):
        raise K.ContractError('a bench setup is a JSON object; see '
                              '`python -m biosense.evidence.cli template bench`')
    stage_ids = {s['stage_id']: (s.get('label') or s['stage_id']) for s in project.stages}

    def stage(v, where, *, required=True):
        sid = str(v or '').strip()
        if not sid:
            if required:
                raise K.ContractError(f'{where}.stage_id is required')
            return None
        if sid not in stage_ids:
            raise K.ContractError(f'{where}.stage_id is {sid!r}; this project\'s stages are '
                                  f'{", ".join(stage_ids)}')
        return sid

    media, seen = [], set()
    for i, m in enumerate(draft.get('media') or []):
        w = f'media[{i}]'
        sid = stage(m.get('stage_id'), w)
        if sid in seen:
            raise K.ContractError(f'{w}: stage {sid!r} has two media; give one, with the '
                                  f'supplements that differ')
        seen.add(sid)
        prov = _enum(m.get('provenance'), PROVENANCE, f'{w}.provenance')
        srcs = _sources(m.get('sources'), w)
        if prov == 'reported' and not srcs:
            raise K.ContractError(f'{w}: a reported medium names the source and quotes it')
        media.append({
            'stage_id': sid, 'stage_label': stage_ids[sid],
            'base_medium': _text(m.get('base_medium'), f'{w}.base_medium', minimum=2),
            'supplements': [_text(x, f'{w}.supplements[{j}]', minimum=1)
                            for j, x in enumerate(m.get('supplements') or [])],
            'serum': _text(m.get('serum'), f'{w}.serum', required=False),
            'prepare': _text(m.get('prepare'), f'{w}.prepare', required=False),
            'provenance': prov, 'sources': srcs,
            'note': _text(m.get('note'), f'{w}.note', required=False)})

    reagents = []
    raw_reagents = draft.get('reagents') or []
    if len(raw_reagents) > MAX_REAGENTS:
        raise K.ContractError(f'{len(raw_reagents)} reagents; the limit is {MAX_REAGENTS}')
    for i, r in enumerate(raw_reagents):
        w = f'reagents[{i}]'
        srcs = _sources(r.get('sources'), w)
        sourcing = _enum(r.get('sourcing'), SOURCING, f'{w}.sourcing', default='your_lab')
        supplier = _text(r.get('supplier'), f'{w}.supplier', required=False)
        catalogue = _text(r.get('catalogue'), f'{w}.catalogue', required=False)
        if (supplier or catalogue) and sourcing != 'from_source':
            raise K.ContractError(
                f'{w} ({r.get("name")}) names a supplier or catalogue with sourcing '
                f'"your_lab". {CATALOGUE_RULE}')
        if (supplier or catalogue) and not srcs:
            raise K.ContractError(
                f'{w} ({r.get("name")}) gives a supplier or catalogue with nothing cited. '
                f'{CATALOGUE_RULE}')
        if sourcing == 'from_source' and not (supplier or catalogue):
            raise K.ContractError(
                f'{w} ({r.get("name")}) is sourced from a paper but names neither supplier '
                f'nor catalogue; use "your_lab" instead')
        prov = _enum(r.get('provenance'), PROVENANCE, f'{w}.provenance')
        if prov == 'reported' and not srcs:
            raise K.ContractError(f'{w}: a reported reagent names the source and quotes it')
        reagents.append({
            'name': _text(r.get('name'), f'{w}.name', minimum=2),
            'role': _text(r.get('role'), f'{w}.role', minimum=3),
            'stage_ids': [stage(x, f'{w}.stage_ids[{j}]')
                          for j, x in enumerate(r.get('stage_ids') or [])],
            'working_concentration': _text(r.get('working_concentration'),
                                           f'{w}.working_concentration', required=False),
            'supplier': supplier, 'catalogue': catalogue, 'sourcing': sourcing,
            'stock': _text(r.get('stock'), f'{w}.stock', required=False),
            'storage': _text(r.get('storage'), f'{w}.storage', required=False),
            'handling': _text(r.get('handling'), f'{w}.handling', required=False),
            'provenance': prov, 'sources': srcs,
            'note': _text(r.get('note'), f'{w}.note', required=False)})

    steps = []
    raw_steps = draft.get('handling') or []
    if not raw_steps:
        raise K.ContractError('say how the culture is actually run: at least a seeding step and '
                              'a feeding step, or this is an arms table again')
    if len(raw_steps) > MAX_STEPS:
        raise K.ContractError(f'{len(raw_steps)} handling steps; the limit is {MAX_STEPS}')
    for i, h in enumerate(raw_steps):
        w = f'handling[{i}]'
        steps.append({
            'step': _text(h.get('step'), f'{w}.step', minimum=2),
            'stage_id': stage(h.get('stage_id'), w, required=False),
            'what': _text(h.get('what'), f'{w}.what', minimum=10),
            'when': _text(h.get('when'), f'{w}.when', required=False),
            'duration': _text(h.get('duration'), f'{w}.duration', required=False),
            'provenance': _enum(h.get('provenance'), PROVENANCE, f'{w}.provenance',
                                default='design_choice'),
            'sources': _sources(h.get('sources'), w)})

    do = [_rule(x, f'do[{i}]') for i, x in enumerate(draft.get('do') or [])]
    dont = [_rule(x, f'dont[{i}]') for i, x in enumerate(draft.get('dont') or [])]
    if not (do or dont):
        raise K.ContractError('give at least one thing to do or avoid, with what goes wrong if '
                              'it is ignored; that is the part a protocol usually omits and a '
                              'person usually needs')

    doc = {
        'kind': 'bench_setup', 'schema_version': K.PRODUCTION_VERSION,
        'project_id': project.project_id, 'run_id': run_id,
        'round': int(round_number) if round_number else None,
        'created_at': K.now_iso(), 'created_by': created_by,
        'culture_format': (culture_format or draft.get('culture_format') or None),
        'vessel': _text(draft.get('vessel'), 'vessel', required=False),
        'media': media, 'reagents': reagents, 'handling': steps,
        'do': do, 'dont': dont,
        'safety': [_text(x, f'safety[{i}]', minimum=5)
                   for i, x in enumerate(draft.get('safety') or [])],
        'consumables': [_text(x, f'consumables[{i}]', minimum=2)
                        for i, x in enumerate(draft.get('consumables') or [])],
        'coverage': {
            'stages_total': len(stage_ids),
            'stages_with_medium': len(media),
            'reagents': len(reagents),
            'sourced': sum(1 for r in reagents if r['sourcing'] == 'from_source'),
            'gaps': sum(1 for r in reagents if r['provenance'] == 'gap')
                    + sum(1 for m in media if m['provenance'] == 'gap'),
        },
        'limitations': [_text(x, f'limitations[{i}]', minimum=1)
                        for i, x in enumerate(draft.get('limitations') or [])],
        'note': NOTE,
    }
    K.require_valid('bench_setup', doc)
    return doc


def markdown(doc):
    """The bench setup as a section of the written protocol."""
    if not doc:
        return []
    L = ['## At the bench', '', doc['note'], '']
    if doc.get('vessel'):
        L += [f'**Vessel** {doc["vessel"]}', '']
    if doc.get('media'):
        L += ['### Media', '']
        for m in doc['media']:
            supp = ('; '.join(m['supplements']) if m['supplements'] else 'no supplements listed')
            refs = ', '.join(s['ref'] for s in m['sources'])
            L.append(f'- **{m["stage_label"]}** — {m["base_medium"]}'
                     + (f' ({m["serum"]})' if m.get('serum') else '')
                     + f'; {supp} · {m["provenance"]}'
                     + (f' [{refs}]' if refs else ''))
            if m.get('prepare'):
                L.append(f'  - {m["prepare"]}')
        L.append('')
    if doc.get('reagents'):
        L += ['### Reagents', '']
        for r in doc['reagents']:
            where = (f'{r["supplier"] or ""} {r["catalogue"] or ""}'.strip()
                     if r['sourcing'] == 'from_source'
                     else 'your own qualified stock (no source names one)')
            L.append(f'- **{r["name"]}** — {r["role"]}'
                     + (f'; {r["working_concentration"]}' if r.get('working_concentration') else '')
                     + f' · {where}')
            for k, label in (('stock', 'Stock'), ('storage', 'Storage'),
                             ('handling', 'Handling')):
                if r.get(k):
                    L.append(f'  - {label}: {r[k]}')
        L.append('')
    if doc.get('handling'):
        L += ['### How it is run', '']
        for h in doc['handling']:
            L.append(f'- **{h["step"]}**'
                     + (f' ({h["when"]})' if h.get('when') else '') + f' — {h["what"]}')
        L.append('')
    for key, title in (('do', 'Do'), ('dont', 'Do not')):
        if doc.get(key):
            L += [f'### {title}', '']
            L += [f'- {r["rule"]} — {r["why"]}' for r in doc[key]]
            L.append('')
    if doc.get('safety'):
        L += ['### Safety', ''] + [f'- {x}' for x in doc['safety']] + ['']
    if doc.get('consumables'):
        L += ['### Consumables', ''] + [f'- {x}' for x in doc['consumables']] + ['']
    return L
