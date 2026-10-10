"""Which way of making the cell this run pursues: the route selection.

The specialist literature mode works in steps. First the run learns how the
cell develops and every known way of producing it — not only from iPSC. Then,
before any parameter is researched, it chooses the route whose parameters are
worth researching for this need. This builds that choice from the
orchestrator's draft and checks it the way a reviewer would:

* the need is stated as criteria, each naming where it came from (the
  objective, a constraint, the project's system) — a criterion the request
  never stated is a design choice and is labelled as one;
* every candidate route cites its sources, names its starting material and
  maturity, and is scored against every criterion (a missing score is
  `unknown`, never silently skipped);
* a route that fails a hard criterion cannot be chosen;
* the chosen route is one of the candidates, and every other candidate is in
  `alternatives` with a reason — a route dropped without a word is the one a
  reader cannot check;
* when the chosen route does not fit the project's own system, the document
  says what the project would need; the protocol still runs on the project's
  parameters and the change is a person's to make;
* the words are "chosen because", never "best" or "superior": a literature
  comparison cannot prove one route beats another for this process.
"""
from __future__ import annotations

import re

from .. import contracts as K

KINDS = ('directed_differentiation', 'forward_programming', 'transdifferentiation',
         'primary_expansion', 'cell_line', 'organoid_derived', 'other')
MATURITY = ('established', 'emerging', 'exploratory')
FIT = ('meets', 'partial', 'fails', 'unknown')
# Whether the need, as written, lets the run reach what the person is after.
VERDICTS = ('well_matched', 'constrained', 'suboptimal')
OVERCLAIM = re.compile(r'\b(best|superior|outperform\w*|proven|optimal|beats?|'
                       r'the only (?:viable|possible) (?:route|way|option))\b', re.I)

NOTE = ('The route this run researched, chosen against the need stated as criteria, with every '
        'candidate and why it was or was not chosen. A reasoned choice a person can overturn, '
        'not a proof: the literature can say what each route reports, never that one beats '
        'another for this process.')

TEMPLATE = {
    'cell_type': 'macrophage',
    'need': {
        'summary': 'Suspension-scale production of macrophages from the lab\'s iPSC line.',
        'criteria': [
            {'criterion_id': 'C1', 'criterion': 'scales in stirred suspension',
             'source': 'project system: stirred suspension aggregates', 'must': True},
            {'criterion_id': 'C2', 'criterion': 'no integrating construct',
             'source': 'constraint: research-grade, no transgene', 'must': True},
            {'criterion_id': 'C3', 'criterion': 'high purity at harvest',
             'source': 'objective', 'must': False},
            {'criterion_id': 'C4', 'criterion': 'short time to first harvest',
             'source': 'design choice: not stated in the request', 'must': False},
        ],
    },
    'candidates': [
        {'route_id': 'R1', 'label': 'iPSC directed differentiation via mesoderm (EB/aggregate)',
         'kind': 'directed_differentiation', 'starting_material': 'iPSC', 'maturity': 'established',
         'summary': 'BMP4/VEGF mesoderm induction then M-CSF/IL-3 myeloid harvest; adherent EB '
                    'or suspension aggregates.',
         'reported': {'yield': 'continuous harvest over weeks', 'purity': '>90% CD14+',
                      'timeline': '~25-30 days to first harvest', 'format': 'EB / aggregate'},
         'fit': [{'criterion_id': 'C1', 'verdict': 'meets', 'note': 'aggregate formats reported'},
                 {'criterion_id': 'C2', 'verdict': 'meets'},
                 {'criterion_id': 'C3', 'verdict': 'meets'},
                 {'criterion_id': 'C4', 'verdict': 'partial', 'note': 'weeks, not days'}],
         'limits': ['slow first harvest'], 'refs': ['PMID:<id>']},
        {'route_id': 'R2', 'label': 'Transcription-factor forward programming',
         'kind': 'forward_programming', 'starting_material': 'iPSC with inducible construct',
         'maturity': 'emerging',
         'summary': 'Inducible lineage factors drive macrophage identity in about two weeks.',
         'reported': {'purity': '>95%', 'timeline': '~10-14 days'},
         'fit': [{'criterion_id': 'C1', 'verdict': 'unknown'},
                 {'criterion_id': 'C2', 'verdict': 'fails', 'note': 'needs an integrating construct'},
                 {'criterion_id': 'C3', 'verdict': 'meets'},
                 {'criterion_id': 'C4', 'verdict': 'meets'}],
         'refs': ['PMID:<id>']},
        {'route_id': 'R3', 'label': 'Primary monocyte-derived macrophages',
         'kind': 'primary_expansion', 'starting_material': 'peripheral blood monocytes',
         'maturity': 'established',
         'summary': 'CD14+ monocytes matured with M-CSF; the adult reference, not a renewable source.',
         'reported': {'purity': '>95%', 'timeline': '~7 days'},
         'fit': [{'criterion_id': 'C1', 'verdict': 'fails', 'note': 'does not expand'},
                 {'criterion_id': 'C2', 'verdict': 'meets'},
                 {'criterion_id': 'C3', 'verdict': 'meets'},
                 {'criterion_id': 'C4', 'verdict': 'meets'}],
         'refs': ['PMID:<id>']},
    ],
    'chosen': {'route_id': 'R1',
               'why': 'The only candidate meeting both hard criteria (suspension scale, no '
                      'construct); purity reported comparable to the others.',
               'fits_project': True, 'project_note': None},
    'alternatives': [
        {'route_id': 'R2', 'why_not': 'fails the no-construct constraint', 'worth_a_parallel_arm': False},
        {'route_id': 'R3', 'why_not': 'not a renewable, scalable source', 'worth_a_parallel_arm': False},
    ],
    'need_assessment': {
        'verdict': 'constrained',
        'summary': 'The no-construct constraint rules out forward programming, which reports a '
                   'shorter timeline and higher purity. The chosen route is the closest fit to '
                   'the need as written, and this run optimises inside it.',
        'limiting_criteria': ['C2'],
        'trade_offs': [{'criterion_id': 'C2', 'route_id': 'R2',
                        'would_gain': 'first harvest in about two weeks rather than about four, '
                                      'at a reported purity above 95%',
                        'would_cost': 'an integrating construct in the line, and the regulatory '
                                      'and characterisation work that comes with it',
                        'refs': ['PMID:<id>']}],
    },
    'headroom': {
        'summary': 'Within the aggregate route, the myeloid stage is where the reported spread '
                   'between protocols is widest.',
        'levers': [{'lever': 'M-CSF concentration at the myeloid stage',
                    'parameter_id': 'mcsf_ng_ml', 'stage_id': 'myeloid',
                    'why': 'reported working values span a wide range and the sources disagree '
                           'on where output saturates',
                    'bounded_by': 'the project maximum for this parameter',
                    'refs': ['PMID:<id>']},
                   {'lever': 'aggregate size at induction', 'stage_id': 'mesoderm',
                    'why': 'sets how evenly the induction signal reaches the cells',
                    'bounded_by': 'the vessel: this project has no aggregate-size parameter yet'}],
    },
    'review': {'reviewed_by': 'analyst', 'note': 'fit scores checked against the landscape reply',
               'disagreements': []},
    'limitations': ['Time-to-harvest preference is a design choice, not stated in the request.'],
}


def _text(v, where, *, minimum=1, required=True):
    s = str(v or '').strip()
    if not s and not required:
        return None
    if len(s) < minimum:
        raise K.ContractError(f'{where} is too short (need at least {minimum} characters)')
    if OVERCLAIM.search(s):
        raise K.ContractError(
            f'{where} says {OVERCLAIM.search(s).group(0)!r}. A literature comparison cannot prove '
            f'one route beats another for this process: say which criteria it meets and why it '
            f'was chosen')
    return s


def _enum(v, allowed, where):
    s = str(v or '').strip().lower().replace(' ', '_').replace('-', '_')
    if s not in allowed:
        raise K.ContractError(f'{where} is {v!r}; use one of {", ".join(allowed)}')
    return s


def _refs(raw, where):
    refs = [str(r).strip() for r in ([raw] if isinstance(raw, str) else raw or []) if str(r).strip()]
    if not refs:
        raise K.ContractError(f'{where} cites no source; a route needs the paper(s) that report it')
    return refs


def build(draft, *, project, run_id=None, created_by=None):
    if not isinstance(draft, dict):
        raise K.ContractError('a route selection draft is a JSON object; see '
                              '`python -m biosense.evidence.cli template route-select`')
    stages = {st['stage_id'] for st in project.stages} | {'all'}
    need = draft.get('need') or {}
    crits, cids = [], set()
    for i, c in enumerate(need.get('criteria') or []):
        w = f'need.criteria[{i}]'
        cid = str(c.get('criterion_id') or f'C{i + 1}').strip()
        if cid in cids:
            raise K.ContractError(f'{w}: criterion_id {cid!r} is used twice')
        cids.add(cid)
        crits.append({'criterion_id': cid,
                      'criterion': _text(c.get('criterion'), f'{w}.criterion', minimum=3),
                      'source': _text(c.get('source'), f'{w}.source', minimum=3),
                      'must': bool(c.get('must'))})
    if not crits:
        raise K.ContractError('the need has no criteria; state what the route has to satisfy, '
                              'each with where it came from (the objective, a constraint, the '
                              'project\'s system, or a design choice)')
    must_ids = {c['criterion_id'] for c in crits if c['must']}

    cands, route_ids = [], set()
    for i, r in enumerate(draft.get('candidates') or []):
        w = f'candidates[{i}]'
        rid = str(r.get('route_id') or f'R{i + 1}').strip()
        if rid in route_ids:
            raise K.ContractError(f'{w}: route_id {rid!r} is used twice')
        route_ids.add(rid)
        fit, scored = [], {}
        for j, f in enumerate(r.get('fit') or []):
            fcid = str(f.get('criterion_id') or '').strip()
            if fcid not in cids:
                raise K.ContractError(f'{w}.fit[{j}] scores {fcid!r}, which is not a criterion')
            scored[fcid] = _enum(f.get('verdict'), FIT, f'{w}.fit[{j}].verdict')
            fit.append({'criterion_id': fcid, 'verdict': scored[fcid],
                        'note': _text(f.get('note'), f'{w}.fit[{j}].note', required=False)})
        # Every criterion gets a score; one left out is unknown, never skipped.
        for cid in cids:
            if cid not in scored:
                fit.append({'criterion_id': cid, 'verdict': 'unknown', 'note': None})
                scored[cid] = 'unknown'
        rep = r.get('reported') if isinstance(r.get('reported'), dict) else {}
        cands.append({
            'route_id': rid, 'label': _text(r.get('label'), f'{w}.label', minimum=2),
            'kind': _enum(r.get('kind'), KINDS, f'{w}.kind'),
            'starting_material': _text(r.get('starting_material'), f'{w}.starting_material'),
            'maturity': _enum(r.get('maturity'), MATURITY, f'{w}.maturity'),
            'summary': _text(r.get('summary'), f'{w}.summary', minimum=10),
            'reported': {k: (str(rep[k]).strip() or None) if rep.get(k) is not None else None
                         for k in ('yield', 'purity', 'timeline', 'scale', 'format')},
            'fit': fit,
            'limits': [_text(x, f'{w}.limits', minimum=1) for x in r.get('limits') or []],
            'refs': _refs(r.get('refs'), f'{w} ({rid})'),
            '_fails_must': {cid for cid in must_ids if scored.get(cid) == 'fails'},
        })
    if not cands:
        raise K.ContractError('name at least one candidate route, or record as a limitation that '
                              'the landscape search found none for this cell')

    ch = draft.get('chosen') or {}
    chosen_id = str(ch.get('route_id') or '').strip()
    chosen = next((c for c in cands if c['route_id'] == chosen_id), None)
    if chosen is None:
        raise K.ContractError(f'chosen.route_id {chosen_id!r} is not one of the candidates '
                              f'({", ".join(sorted(route_ids))})')
    if chosen['_fails_must']:
        bad = ', '.join(sorted(chosen['_fails_must']))
        raise K.ContractError(f'the chosen route fails hard criteria ({bad}); a route that fails a '
                              f'"must" criterion cannot be chosen — choose another or relax the '
                              f'criterion and say why')
    chosen_doc = {'route_id': chosen_id, 'why': _text(ch.get('why'), 'chosen.why', minimum=10),
                  'fits_project': bool(ch.get('fits_project')),
                  'project_note': _text(ch.get('project_note'), 'chosen.project_note',
                                        required=False)}
    if not chosen_doc['fits_project'] and not chosen_doc['project_note']:
        raise K.ContractError('the chosen route does not fit the project, so say what the project '
                              'would need (chosen.project_note): the protocol runs on the '
                              'project\'s parameters and a person decides any change')

    alts, alt_ids = [], set()
    for i, a in enumerate(draft.get('alternatives') or []):
        rid = str(a.get('route_id') or '').strip()
        if rid not in route_ids:
            raise K.ContractError(f'alternatives[{i}] names {rid!r}, which is not a candidate')
        if rid == chosen_id:
            raise K.ContractError('the chosen route is not its own alternative')
        alt_ids.add(rid)
        alts.append({'route_id': rid,
                     'why_not': _text(a.get('why_not'), f'alternatives[{i}].why_not', minimum=5),
                     'worth_a_parallel_arm': bool(a.get('worth_a_parallel_arm'))})
    missing = route_ids - alt_ids - {chosen_id}
    if missing:
        raise K.ContractError(f'every candidate but the chosen one is an alternative with a '
                              f'reason; missing: {", ".join(sorted(missing))}')

    # Does the need, as the person wrote it, let this run reach what they are
    # after? A request can be consistent and still box the run into a route the
    # evidence reports worse numbers for. Saying so is the job; so is still
    # optimising inside what was asked, which is what the protocol does anyway.
    na = draft.get('need_assessment') or {}
    verdict = _enum(na.get('verdict') or 'well_matched', VERDICTS, 'need_assessment.verdict')
    limiting = []
    for cid in na.get('limiting_criteria') or []:
        cid = str(cid).strip()
        if cid not in cids:
            raise K.ContractError(f'need_assessment.limiting_criteria names {cid!r}, which is '
                                  f'not a criterion')
        limiting.append(cid)
    trades = []
    for i, t in enumerate(na.get('trade_offs') or []):
        w = f'need_assessment.trade_offs[{i}]'
        cid, rid = str(t.get('criterion_id') or '').strip(), str(t.get('route_id') or '').strip()
        if cid not in cids:
            raise K.ContractError(f'{w}.criterion_id {cid!r} is not a criterion')
        if rid not in route_ids:
            raise K.ContractError(f'{w}.route_id {rid!r} is not a candidate route')
        trades.append({'criterion_id': cid, 'route_id': rid,
                       'would_gain': _text(t.get('would_gain'), f'{w}.would_gain', minimum=5),
                       'would_cost': _text(t.get('would_cost'), f'{w}.would_cost', minimum=5),
                       'refs': [str(r).strip() for r in t.get('refs') or [] if str(r).strip()]})
    if verdict in ('constrained', 'suboptimal') and not trades:
        raise K.ContractError(
            f'need_assessment.verdict is {verdict!r}, so name what relaxing a criterion would '
            f'open up and what it would cost (trade_offs). Telling a person their request is '
            f'worse without saying what better looks like, and its price, is not something they '
            f'can act on')
    assessment = {'verdict': verdict,
                  'summary': _text(na.get('summary') or
                                   'The need as stated and what the evidence reports agree; no '
                                   'criterion rules out a route with stronger reported numbers.',
                                   'need_assessment.summary', minimum=10),
                  'limiting_criteria': limiting, 'trade_offs': trades}

    # Inside the chosen route and this project's vessel, where the gain still is.
    headroom = None
    hr = draft.get('headroom')
    if isinstance(hr, dict):
        levers = []
        for i, lv in enumerate(hr.get('levers') or []):
            w = f'headroom.levers[{i}]'
            sid = str(lv.get('stage_id') or '').strip() or None
            if sid and sid not in stages:
                raise K.ContractError(f'{w}.stage_id is {sid!r}; project stages are '
                                      f'{", ".join(sorted(stages))}')
            levers.append({'lever': _text(lv.get('lever'), f'{w}.lever'),
                           'parameter_id': str(lv.get('parameter_id')).strip()
                                           if lv.get('parameter_id') else None,
                           'stage_id': sid,
                           'why': _text(lv.get('why'), f'{w}.why', minimum=5),
                           'bounded_by': _text(lv.get('bounded_by'), f'{w}.bounded_by',
                                               required=False),
                           'refs': [str(r).strip() for r in lv.get('refs') or [] if str(r).strip()]})
        headroom = {'summary': _text(hr.get('summary'), 'headroom.summary', required=False),
                    'levers': levers}

    review = None
    rv = draft.get('review')
    if isinstance(rv, dict):
        review = {'reviewed_by': _text(rv.get('reviewed_by'), 'review.reviewed_by', required=False),
                  'note': _text(rv.get('note'), 'review.note', required=False),
                  'disagreements': [_text(x, 'review.disagreements', minimum=1)
                                    for x in rv.get('disagreements') or []]}

    for c in cands:
        c.pop('_fails_must', None)
    doc = {
        'kind': 'route_selection', 'schema_version': K.PRODUCTION_VERSION,
        'project_id': project.project_id, 'run_id': run_id, 'created_at': K.now_iso(),
        'created_by': created_by,
        'cell_type': _text(draft.get('cell_type'), 'cell_type', minimum=2),
        'need': {'summary': _text(need.get('summary'), 'need.summary', required=False),
                 'criteria': crits},
        'candidates': cands, 'chosen': chosen_doc, 'need_assessment': assessment,
        'headroom': headroom, 'alternatives': alts, 'review': review,
        'limitations': [_text(x, 'limitations', minimum=1) for x in draft.get('limitations') or []],
        'note': NOTE,
    }
    K.require_valid('route_selection', doc)
    return doc
