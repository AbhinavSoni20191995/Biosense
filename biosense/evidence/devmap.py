"""The process read against the embryo: the developmental-biology add-on.

A production protocol is development compressed into a dish. Reading it beside
the embryo asks questions a protocol search cannot: which signal the embryo has
at this point that the dish lacks, when a receptor is actually present, what
losing a regulator does, and which lever development suggests that nobody seems
to have put into a production protocol.

`build` turns an agent's draft into a checked DevelopmentalMap. The rules are
the ones a reader would otherwise have to trust:

* every developmental statement (a signal, a regulator, a perturbation) cites
  its source — an uncited one is a guess and belongs in an idea's rationale;
* stages are the project's own, so the map lines up with the protocol;
* an idea states the search behind its novelty, and its novelty is written by
  this code as the scope of that search ("not found in the N protocol searches
  listed"), never as "never tried" — no search can show that;
* an idea's confidence is capped: low when no protocol was found to use it
  (mechanism and analogy, no direct test), moderate at most when one was.

An idea enters a protocol only as a hypothesis (candidate, best guess at most
low) that a person approves; this document itself changes nothing.
"""
from __future__ import annotations

import re

from .. import contracts as K

ACTIONS = ('add', 'remove', 'retime', 'change_dose', 'sequence', 'inhibit')
STATES = ('on', 'off', 'rising', 'falling', 'pulse', 'gradient', 'inhibited')
PERTURBATIONS = ('knockout', 'knockdown', 'overexpression', 'mutation', 'inhibitor', 'agonist')
# Claims no literature search can support. Novelty is the scope of a search.
OVERCLAIM = re.compile(r'\b(never (?:been )?(?:tried|tested|used|done)|first[- ]ever|'
                       r'nobody has|no one has|unprecedented)\b', re.I)

NOTE = ('Each process stage beside its in vivo counterpart, from cited developmental '
        'evidence. Ideas are levers development suggests that the protocol searches listed '
        'did not find in use: hypotheses to test, at low confidence, never findings. Developmental '
        'evidence from another species or from the embryo is analogous to a production process, '
        'not a measurement of it.')

TEMPLATE = {
    'cell_type': 'rod photoreceptor',
    'species_basis': 'human fetal retina and mouse; human retinal organoids',
    'stages': [{
        'stage_id': 'differentiation',
        'in_vivo_counterpart': 'optic cup neural retina, photoreceptor precursor specification',
        'timing': 'human fetal weeks 8-12; mouse E12-P0',
        'signals': [{'name': 'retinoic acid', 'pathway': 'RA', 'state': 'gradient',
                     'window': 'high centrally during cone/rod specification, falling after',
                     'source': 'RPE and ventral retina', 'refs': ['PMID:<id>']}],
        'regulators': [{'gene': 'NRL', 'role': 'rod fate', 'refs': ['PMID:<id>']}],
        'perturbations': [{'gene': 'NRL', 'type': 'knockout', 'model': 'mouse',
                           'phenotype': 'rods become S-cone-like cells',
                           'implication': 'NRL timing gates the rod yield',
                           'refs': ['PMID:<id>']}],
        'protocol_vs_development': [
            'The protocol holds RA constant from day 20; in vivo RA falls after specification.'],
    }],
    'ideas': [{
        'idea_id': 'D1', 'lever': 'thyroid hormone (T3) pulse', 'stage_id': 'differentiation',
        'action': 'add',
        'rationale': 'T3 rises during cone maturation in vivo and TRbeta2 loss removes M cones; '
                     'the protocol has no thyroid signal at that window.',
        'developmental_refs': ['PMID:<id>'],
        'protocol_search': {'queries': ['retinal organoid T3 photoreceptor protocol'],
                            'found_in_protocols': False, 'closest_protocol_ref': None},
        'confidence': 'low',
        'test': 'two arms, with and without T3 from day 90, counting cone opsins at harvest',
        'hypothesis_id': None,
    }],
    'limitations': ['Developmental timing is from mouse where no human data was found.'],
}


def _refs(raw, where):
    refs = [str(r).strip() for r in ([raw] if isinstance(raw, str) else raw or []) if str(r).strip()]
    if not refs:
        raise K.ContractError(f'{where} cites no source. A developmental statement needs its '
                              f'reference; one without is a guess, which belongs in an idea\'s '
                              f'rationale')
    return refs


def _text(v, where, *, required=True):
    s = str(v or '').strip()
    if required and not s:
        raise K.ContractError(f'{where} is empty')
    if s and OVERCLAIM.search(s):
        raise K.ContractError(f'{where} says {OVERCLAIM.search(s).group(0)!r}. No search can show '
                              f'that; state what was searched (protocol_search) and BioSense '
                              f'writes the novelty as the scope of that search')
    return s or None


def _enum(v, allowed, where):
    s = str(v or '').strip().lower().replace(' ', '_').replace('-', '_')
    if s not in allowed:
        raise K.ContractError(f'{where} is {v!r}; use one of {", ".join(allowed)}')
    return s


def _stage(v, stages, where):
    sid = str(v or '').strip()
    if sid not in stages:
        raise K.ContractError(f'{where} names stage {sid!r}; this project\'s stages are '
                              f'{", ".join(sorted(stages - {"all"}))} (or "all")')
    return sid


def build(draft, *, project, run_id=None, created_by=None):
    if not isinstance(draft, dict):
        raise K.ContractError('a developmental map draft is a JSON object; see '
                              '`python -m biosense.evidence.cli template devmap`')
    stages = {s['stage_id'] for s in project.stages} | {'all'}
    out_stages = []
    for i, st in enumerate(draft.get('stages') or []):
        w = f'stages[{i}]'
        sid = _stage(st.get('stage_id'), stages, w)
        out_stages.append({
            'stage_id': sid,
            'in_vivo_counterpart': _text(st.get('in_vivo_counterpart'), f'{w}.in_vivo_counterpart'),
            'timing': _text(st.get('timing'), f'{w}.timing', required=False),
            'signals': [{
                'name': _text(s.get('name'), f'{w}.signals[{j}].name'),
                'pathway': _text(s.get('pathway'), f'{w}.signals[{j}].pathway', required=False),
                'state': _enum(s.get('state'), STATES, f'{w}.signals[{j}].state'),
                'window': _text(s.get('window'), f'{w}.signals[{j}].window', required=False),
                'source': _text(s.get('source'), f'{w}.signals[{j}].source', required=False),
                'refs': _refs(s.get('refs'), f'{w}.signals[{j}] ({s.get("name")})'),
            } for j, s in enumerate(st.get('signals') or [])],
            'regulators': [{
                'gene': _text(r.get('gene'), f'{w}.regulators[{j}].gene'),
                'role': _text(r.get('role'), f'{w}.regulators[{j}].role'),
                'refs': _refs(r.get('refs'), f'{w}.regulators[{j}] ({r.get("gene")})'),
            } for j, r in enumerate(st.get('regulators') or [])],
            'perturbations': [{
                'gene': _text(p.get('gene'), f'{w}.perturbations[{j}].gene'),
                'type': _enum(p.get('type'), PERTURBATIONS, f'{w}.perturbations[{j}].type'),
                'model': _text(p.get('model'), f'{w}.perturbations[{j}].model'),
                'phenotype': _text(p.get('phenotype'), f'{w}.perturbations[{j}].phenotype'),
                'implication': _text(p.get('implication'), f'{w}.perturbations[{j}].implication',
                                     required=False),
                'refs': _refs(p.get('refs'), f'{w}.perturbations[{j}] ({p.get("gene")})'),
            } for j, p in enumerate(st.get('perturbations') or [])],
            'protocol_vs_development': [
                _text(x, f'{w}.protocol_vs_development[{j}]')
                for j, x in enumerate(st.get('protocol_vs_development') or [])],
        })
    ideas, seen = [], set()
    for i, idea in enumerate(draft.get('ideas') or []):
        w = f'ideas[{i}]'
        iid = str(idea.get('idea_id') or f'D{i + 1}').strip()
        if iid in seen:
            raise K.ContractError(f'{w}: idea_id {iid!r} is used twice')
        seen.add(iid)
        search = idea.get('protocol_search') or {}
        queries = [str(q).strip() for q in search.get('queries') or [] if str(q).strip()]
        if not queries:
            raise K.ContractError(
                f'{w} ({idea.get("lever")}) states no protocol search. An idea\'s novelty is '
                f'what was searched and not found: list the queries in protocol_search.queries')
        found = bool(search.get('found_in_protocols'))
        closest = str(search.get('closest_protocol_ref') or '').strip() or None
        if found and not closest:
            raise K.ContractError(f'{w}: found_in_protocols is true, so name the protocol that '
                                  f'uses it (closest_protocol_ref)')
        novelty = (f'used before: {closest}' if found else
                   f'not found in the {len(queries)} protocol search'
                   f'{"es" if len(queries) != 1 else ""} listed'
                   + (f' (closest: {closest})' if closest else ''))
        cap = 'moderate' if found else 'low'
        asked = str(idea.get('confidence') or 'low').strip().lower()
        if asked not in ('low', 'moderate', 'high'):
            raise K.ContractError(f'{w}.confidence is {asked!r}; use low or moderate')
        conf, note = asked, None
        if {'low': 0, 'moderate': 1, 'high': 2}[asked] > {'low': 0, 'moderate': 1}[cap]:
            conf = cap
            note = (f'capped from {asked}: ' + (
                'an idea no protocol searched was found to use has no direct test, only '
                'mechanism and analogy' if not found else
                'a protocol precedent is not a test of this process'))
        ideas.append({
            'idea_id': iid,
            'lever': _text(idea.get('lever'), f'{w}.lever'),
            'stage_id': _stage(idea.get('stage_id'), stages, w),
            'action': _enum(idea.get('action'), ACTIONS, f'{w}.action'),
            'rationale': _text(idea.get('rationale'), f'{w}.rationale'),
            'developmental_refs': _refs(idea.get('developmental_refs'),
                                        f'{w} ({idea.get("lever")}).developmental_refs'),
            'protocol_search': {'queries': queries, 'found_in_protocols': found,
                                'closest_protocol_ref': closest},
            'novelty': novelty,
            'confidence': conf,
            'confidence_note': note,
            'test': _text(idea.get('test'), f'{w}.test', required=False),
            'hypothesis_id': (str(idea.get('hypothesis_id')).strip()
                              if idea.get('hypothesis_id') else None),
        })
    if not out_stages and not ideas:
        raise K.ContractError('the map has no stage and no idea; write what development says '
                              'about at least one stage, or record why nothing was found as a '
                              'limitation of the run')
    doc = {
        'kind': 'developmental_map', 'schema_version': K.PRODUCTION_VERSION,
        'project_id': project.project_id, 'run_id': run_id, 'created_at': K.now_iso(),
        'created_by': created_by,
        'cell_type': _text(draft.get('cell_type'), 'cell_type'),
        'species_basis': _text(draft.get('species_basis'), 'species_basis', required=False),
        'stages': out_stages, 'ideas': ideas,
        'limitations': [_text(x, 'limitations') for x in draft.get('limitations') or []],
        'note': NOTE,
    }
    K.require_valid('developmental_map', doc)
    return doc
