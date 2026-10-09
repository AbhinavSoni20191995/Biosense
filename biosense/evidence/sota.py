"""The recommended protocol against the state of the art.

A protocol means little in isolation: the question a scientist asks first is
"how does this compare to how the cell is already made?". This builds a checked
SotaComparison from an agent's draft — the leading published protocols for the
target cell, then the recommended protocol lined up against them.

The rules are the ones that keep the comparison honest, the same spirit as the
developmental map:

* every reference protocol cites its source and names its route (directed
  differentiation, forward programming, transdifferentiation, primary
  isolation);
* a difference carries an EXPECTED effect at most — a hypothesis with cited
  support and confidence capped at low (moderate only with a reference behind
  the direction) — never a measured improvement. This system's model is
  uncalibrated and claims no real gain;
* no "better", "best", "outperforms", "superior", "beats": the standing is
  matches / variant / departs, and a difference is what changed and what it is
  expected to do, not a verdict that it won.
"""
from __future__ import annotations

import re

from .. import contracts as K

ROUTES = ('directed_differentiation', 'forward_programming', 'transdifferentiation',
          'primary_isolation', 'other')
VERDICTS = ('same', 'differs', 'novel', 'not_comparable')
STANDINGS = ('matches_sota', 'variant_of_sota', 'departs_from_sota')
DIRECTIONS = ('increase', 'decrease', 'no_change', 'unknown')
# Claims the uncalibrated model and a literature comparison cannot support.
OVERCLAIM = re.compile(r'\b(better than|best|outperform\w*|superior|beats?|state[- ]of[- ]the[- ]'
                       r'art (?:result|performance)|proven|optimal|breakthrough|world[- ]class)\b',
                       re.I)

NOTE = ('The recommended protocol next to the leading published ways this cell is made. A '
        'difference carries only an expected effect — a hypothesis with its basis, confidence '
        'capped low — never a measured improvement: this system\'s model is uncalibrated and '
        'claims no real gain. The standing says matches, variant or departs, never better.')

TEMPLATE = {
    'cell_type': 'macrophage',
    'references': [
        {'ref_id': 'S1', 'citation': 'Author et al. 2023, Journal',
         'route': 'directed_differentiation',
         'summary': 'EB-based iPSC-to-macrophage via BMP4/VEGF then M-CSF/IL-3, continuous harvest.',
         'key_factors': ['BMP4', 'VEGF', 'M-CSF', 'IL-3'],
         'reported': {'yield': '~1e6 macrophages per cm2 per week over weeks',
                      'purity': '>90% CD14+/CD11b+', 'timeline': '~30 days to first harvest',
                      'scale': 'adherent EB', 'format': 'static'},
         'is_benchmark': True, 'refs': ['PMID:<id>']},
        {'ref_id': 'S2', 'citation': 'Author et al. 2022, Journal',
         'route': 'forward_programming',
         'summary': 'Transcription-factor forward programming (e.g. inducible lineage factors) '
                    'to macrophages, faster but with a construct.',
         'key_factors': ['inducible TFs'],
         'reported': {'yield': 'high', 'purity': '>95%', 'timeline': '~2 weeks'},
         'is_benchmark': False, 'refs': ['PMID:<id>']},
    ],
    'comparisons': [
        {'dimension': 'M-CSF dose at myeloid', 'stage_id': 'myeloid',
         'this_protocol': '50 ng/mL', 'state_of_the_art': 'S1 uses 100 ng/mL M-CSF',
         'ref_ids': ['S1'], 'verdict': 'differs', 'difference': 'half the M-CSF of the benchmark',
         'expected_effect': {'readout': 'macrophage yield', 'direction': 'decrease',
                             'confidence': 'low',
                             'basis': 'M-CSF is dose-limiting for CSF1R output in the cited range; '
                                      'a lower dose may lower yield unless the aggregate format '
                                      'compensates.',
                             'refs': ['PMID:<id>'], 'hypothesis_id': None}},
        {'dimension': 'culture format', 'stage_id': 'all',
         'this_protocol': 'stirred suspension aggregates',
         'state_of_the_art': 'S1 is adherent EB; S2 is 2D', 'ref_ids': ['S1', 'S2'],
         'verdict': 'novel', 'difference': 'suspension rather than adherent',
         'expected_effect': {'readout': 'scalability', 'direction': 'increase', 'confidence': 'low',
                             'basis': 'suspension removes the surface-area ceiling of adherent EB; '
                                      'a scale argument, not a yield-per-cell one.',
                             'refs': ['PMID:<id>'], 'hypothesis_id': None}},
    ],
    'standing': {'verdict': 'variant_of_sota', 'benchmark_ref_id': 'S1',
                 'summary': 'A suspension variant of the standard BMP4/VEGF -> M-CSF route, at a '
                            'lower M-CSF dose; same lineage logic, different format and dose.'},
    'limitations': ['No head-to-head study compares these routes at matched scale.'],
}


def _text(v, where, *, minimum=1):
    s = str(v or '').strip()
    if len(s) < minimum:
        raise K.ContractError(f'{where} is too short (need at least {minimum} characters)')
    if OVERCLAIM.search(s):
        raise K.ContractError(
            f'{where} says {OVERCLAIM.search(s).group(0)!r}. This system claims no measured gain: '
            f'state what differs and what it is EXPECTED to change (expected_effect), and let the '
            f'standing be matches / variant / departs')
    return s


def _refs(raw, where):
    refs = [str(r).strip() for r in ([raw] if isinstance(raw, str) else raw or []) if str(r).strip()]
    if not refs:
        raise K.ContractError(f'{where} cites no source; a reference protocol needs its citation ref')
    return refs


def _enum(v, allowed, where):
    s = str(v or '').strip().lower().replace(' ', '_').replace('-', '_')
    if s not in allowed:
        raise K.ContractError(f'{where} is {v!r}; use one of {", ".join(allowed)}')
    return s


def _reported(raw):
    raw = raw if isinstance(raw, dict) else {}
    out = {k: (str(raw[k]).strip() or None) if raw.get(k) is not None else None
           for k in ('yield', 'purity', 'timeline', 'scale', 'format')}
    return out


def _effect(raw, where, found_ref):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise K.ContractError(f'{where} is an object or null')
    asked = _enum(raw.get('confidence') or 'low', ('low', 'moderate', 'high'), f'{where}.confidence')
    refs = [str(r).strip() for r in raw.get('refs') or [] if str(r).strip()]
    # A direction worth moderate confidence needs a citation behind it; without
    # one it is mechanism alone, which this comparison caps at low.
    cap = 'moderate' if refs else 'low'
    conf = cap if {'low': 0, 'moderate': 1, 'high': 2}[asked] > {'low': 0, 'moderate': 1}[cap] \
        else asked
    return {
        'readout': _text(raw.get('readout'), f'{where}.readout'),
        'direction': _enum(raw.get('direction'), DIRECTIONS, f'{where}.direction'),
        'confidence': conf,
        'basis': _text(raw.get('basis'), f'{where}.basis', minimum=10),
        'refs': refs,
        'hypothesis_id': (str(raw.get('hypothesis_id')).strip()
                          if raw.get('hypothesis_id') else None),
    }


def build(draft, *, project, run_id=None, created_by=None):
    if not isinstance(draft, dict):
        raise K.ContractError('a state-of-the-art draft is a JSON object; see '
                              '`python -m biosense.evidence.cli template sota`')
    stages = {s['stage_id'] for s in project.stages} | {'all'}
    refs_out, ref_ids = [], set()
    for i, r in enumerate(draft.get('references') or []):
        w = f'references[{i}]'
        rid = str(r.get('ref_id') or f'S{i + 1}').strip()
        if rid in ref_ids:
            raise K.ContractError(f'{w}: ref_id {rid!r} is used twice')
        ref_ids.add(rid)
        refs_out.append({
            'ref_id': rid,
            'citation': _text(r.get('citation'), f'{w}.citation', minimum=4),
            'route': _enum(r.get('route'), ROUTES, f'{w}.route'),
            'summary': _text(r.get('summary'), f'{w}.summary', minimum=10),
            'key_factors': [str(k).strip() for k in r.get('key_factors') or [] if str(k).strip()],
            'reported': _reported(r.get('reported')),
            'is_benchmark': bool(r.get('is_benchmark')),
            'refs': _refs(r.get('refs'), f'{w} ({rid})'),
        })
    if not refs_out:
        raise K.ContractError('name at least one published protocol to compare against, or record '
                              'as a limitation that none was found for this cell and route')

    def _ref_ids(raw, where):
        ids = [str(x).strip() for x in raw or [] if str(x).strip()]
        bad = [x for x in ids if x not in ref_ids]
        if bad:
            raise K.ContractError(f'{where} names {bad[0]!r}, which is not a reference '
                                  f'(ref_ids are {", ".join(sorted(ref_ids))})')
        return ids

    comps = []
    for i, c in enumerate(draft.get('comparisons') or []):
        w = f'comparisons[{i}]'
        verdict = _enum(c.get('verdict'), VERDICTS, f'{w}.verdict')
        sid = c.get('stage_id')
        if sid and str(sid).strip() not in stages:
            raise K.ContractError(f'{w}.stage_id is {sid!r}; project stages are '
                                  f'{", ".join(sorted(stages))}')
        diff = c.get('difference')
        if verdict in ('differs', 'novel') and not (diff and str(diff).strip()):
            raise K.ContractError(f'{w} is "{verdict}" but states no difference; say what is '
                                  f'different in one line')
        comps.append({
            'dimension': _text(c.get('dimension'), f'{w}.dimension'),
            'stage_id': str(sid).strip() if sid else None,
            'this_protocol': _text(c.get('this_protocol'), f'{w}.this_protocol'),
            'state_of_the_art': _text(c.get('state_of_the_art'), f'{w}.state_of_the_art'),
            'ref_ids': _ref_ids(c.get('ref_ids'), f'{w}.ref_ids'),
            'verdict': verdict,
            'difference': str(diff).strip() if diff else None,
            'expected_effect': _effect(c.get('expected_effect'), f'{w}.expected_effect', ref_ids),
        })

    st = draft.get('standing') or {}
    bench = str(st.get('benchmark_ref_id') or '').strip() or None
    if bench and bench not in ref_ids:
        raise K.ContractError(f'standing.benchmark_ref_id {bench!r} is not a reference')
    if not bench:
        bench = next((r['ref_id'] for r in refs_out if r['is_benchmark']), None)
    standing = {
        'verdict': _enum(st.get('verdict'), STANDINGS, 'standing.verdict'),
        'benchmark_ref_id': bench,
        'summary': _text(st.get('summary'), 'standing.summary', minimum=10),
    }
    doc = {
        'kind': 'sota_comparison', 'schema_version': K.PRODUCTION_VERSION,
        'project_id': project.project_id, 'run_id': run_id, 'created_at': K.now_iso(),
        'created_by': created_by,
        'cell_type': _text(draft.get('cell_type'), 'cell_type', minimum=2),
        'references': refs_out, 'comparisons': comps, 'standing': standing,
        'limitations': [_text(x, 'limitations', minimum=1) for x in draft.get('limitations') or []],
        'note': NOTE,
    }
    K.require_valid('sota_comparison', doc)
    return doc
