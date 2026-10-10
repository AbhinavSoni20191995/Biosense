"""How close the cells this protocol makes should come to the real ones.

Yield and purity say how many cells came out and how clean the harvest is.
Neither says whether the thing in the vessel is the cell the person meant. That
question has a shape the field already uses — markers, global expression,
maturation state, ontogeny, function, epigenome, metabolism, purity, the
missing niche, and whether identity holds — and this builds that judgement into
a document a reader can check.

The rules are the ones that keep an expectation from reading as a result:

* the axes are a fixed list. An axis nobody addressed comes back `unknown`
  with a reason, because a fidelity report that silently omits maturation
  state is how an iPSC product gets called "the cell" when it is the fetal
  version of it;
* every axis says what development does, what the protocol does instead, and
  names the assay that would settle it — a judgement nobody can check is not
  worth recording;
* `high` similarity needs a citation: a source that measured this axis for a
  comparable protocol. Mechanism alone supports `moderate` at most;
* confidence is capped at moderate throughout. These cells do not exist yet;
* nothing may call the product identical to, equivalent to or
  indistinguishable from the primary cell.
"""
from __future__ import annotations

import re

from .. import contracts as K

# The fixed axes, in the order a reader should meet them: what it looks like,
# what it is, where it came from, what it does, then what the dish cannot give.
AXES = {
    'identity_markers': 'Identity markers',
    'transcriptome': 'Transcriptome-wide similarity',
    'maturation_state': 'Maturation state (developmental age)',
    'ontogeny_route': 'Ontogeny — how it got there',
    'function': 'Function',
    'epigenome': 'Epigenome and residual memory',
    'metabolism': 'Metabolic programme',
    'purity': 'Purity and off-target populations',
    'niche_context': 'Niche the vessel cannot provide',
    'stability': 'Stability once factors are withdrawn',
}
# Why each axis is asked at all, for the agent writing the draft and for the
# person reading an `unknown`.
AXIS_ASKS = {
    'identity_markers': 'Does it carry the surface and transcription-factor markers that define '
                        'the mature cell, and not those of its precursor?',
    'transcriptome': 'How close is genome-wide expression to the primary cell, where anyone has '
                     'compared them?',
    'maturation_state': 'Is the product the adult cell or the fetal version of it? This is where '
                        'iPSC-derived cells most often fall short, and it is rarely the axis a '
                        'protocol reports.',
    'ontogeny_route': 'Did it arrive through the same developmental intermediates? Two cells '
                      'with the same markers and different ontogeny are not the same cell.',
    'function': 'Does it do the job the cell exists to do, in an assay rather than by marker?',
    'epigenome': 'Are the lineage loci in the right state, and is there memory of the starting '
                 'cell left?',
    'metabolism': 'Does it run the mature cell\'s metabolic programme, or the glycolytic one of '
                  'a dividing progenitor?',
    'purity': 'What fraction of the harvest is the target, and what are the rest?',
    'niche_context': 'Which in vivo cues — neighbouring cells, matrix, mechanics, oxygen — does '
                     'this vessel not provide, and what do they set?',
    'stability': 'Does the identity hold once the factors come out, or does it drift?',
}
SIMILARITY = ('high', 'moderate', 'low', 'unknown')
MATCH = ('close', 'partial', 'distant', 'unknown')
CONFIDENCE = ('low', 'moderate')
# Claims no protocol report and no model can support about cells not yet made.
OVERCLAIM = re.compile(r'\b(identical|equivalent to (?:the )?(?:primary|adult|real|native)|'
                       r'indistinguishable|fully (?:mature|matured|functional)|'
                       r'bona fide|true (?:adult|primary)|perfect(?:ly)? match\w*)\b', re.I)

NOTE = ('How close the product is EXPECTED to come to the real cell, axis by axis: what '
        'development does, what this protocol does instead, and the assay that would settle it. '
        'Expectations about cells that have not been made yet — never a measurement, never a '
        'claim that the product is the primary cell.')

TEMPLATE = {
    'cell_type': 'macrophage',
    'in_vivo_reference': {
        'cell': 'yolk-sac-derived tissue-resident macrophage',
        'stage_of_life': 'embryonic/fetal', 'tissue': 'tissue-resident',
        'why_this_one': 'This protocol induces mesoderm and makes macrophages without a '
                        'monocyte intermediate, which is the primitive, yolk-sac-like programme '
                        'rather than the adult monocyte-derived one.',
        'refs': ['PMID:<id>'],
    },
    'criteria': [
        {'axis': 'identity_markers',
         'in_vivo': 'Tissue macrophages are CD45+/CD11b+/CD14+ with MAF/MAFB and lineage '
                    'transcription factors on.',
         'protocol': 'M-CSF/IL-3 myeloid stage to harvest; identity read by CD14/CD11b at '
                     'harvest.',
         'departure': None, 'expected_similarity': 'high', 'confidence': 'moderate',
         'reasons': ['Protocols of this route consistently report the defining surface panel at '
                     'the reported purities.'],
         'measured_by': 'flow cytometry for CD14/CD11b/CD45 and an off-target panel at harvest',
         'in_round_plan': True, 'refs': ['PMID:<id>']},
        {'axis': 'maturation_state',
         'in_vivo': 'Tissue macrophages acquire their mature programme over weeks in the tissue, '
                    'under niche signals this process does not supply.',
         'protocol': 'Harvest begins at the myeloid stage and continues; no separate maturation '
                     'phase with tissue cues.',
         'departure': 'The product is expected to sit at a fetal-like maturation state rather '
                      'than an adult tissue-resident one.',
         'expected_similarity': 'low', 'confidence': 'moderate',
         'reasons': ['iPSC-derived myeloid cells are repeatedly reported as fetal-like across '
                     'this route.',
                     'No stage of this protocol supplies the tissue-specific signals that drive '
                     'the final programme.'],
         'measured_by': 'expression of the maturation gene set against primary tissue '
                        'macrophages in a public dataset',
         'in_round_plan': False, 'refs': ['PMID:<id>']},
        {'axis': 'function',
         'in_vivo': 'Phagocytosis, cytokine response to stimulus, and migration.',
         'protocol': 'Not assayed by this protocol; identity is read by marker only.',
         'departure': 'Function is assumed from identity rather than measured.',
         'expected_similarity': 'unknown', 'confidence': 'low',
         'reasons': ['No source in this run reports a functional assay for a protocol at these '
                     'setpoints.'],
         'measured_by': 'a phagocytosis assay and an LPS cytokine response on the harvest',
         'in_round_plan': False, 'refs': []},
    ],
    'overall': {
        'expected_match': 'partial',
        'summary': 'The product is expected to carry the identity markers of the target cell at '
                   'the reported purity, at a fetal-like maturation state, with function not '
                   'established by this run.',
        'dominant_gap': 'maturation_state',
        'what_would_settle_it': 'a maturation gene set against primary cells, and a function '
                                'assay, on the first harvest',
    },
    'limitations': ['No source found compares this route head-to-head with primary cells on '
                    'genome-wide expression.'],
}


def _text(v, where, *, minimum=1, required=True):
    s = str(v or '').strip()
    if not s and not required:
        return None
    if len(s) < minimum:
        raise K.ContractError(f'{where} is too short (need at least {minimum} characters)')
    if OVERCLAIM.search(s):
        raise K.ContractError(
            f'{where} says {OVERCLAIM.search(s).group(0)!r}. These cells have not been made yet, '
            f'and no protocol report makes a product the primary cell: say which axis is '
            f'expected to be close, how close, and what would measure it')
    return s


def _enum(v, allowed, where):
    s = str(v or '').strip().lower().replace(' ', '_').replace('-', '_')
    if s not in allowed:
        raise K.ContractError(f'{where} is {v!r}; use one of {", ".join(allowed)}')
    return s


def build(draft, *, project, run_id=None, created_by=None, round_plan=None):
    """A checked CellFidelity from an agent's draft.

    *round_plan*, when given, is this run's plan: an axis whose measurement is
    in it is marked, so the report says which expectations this round turns
    into measurements and which stay expectations.
    """
    if not isinstance(draft, dict):
        raise K.ContractError('a fidelity draft is a JSON object; see '
                              '`python -m biosense.evidence.cli template fidelity`')
    ref = draft.get('in_vivo_reference') or {}
    reference = {
        'cell': _text(ref.get('cell'), 'in_vivo_reference.cell', minimum=3),
        'stage_of_life': _text(ref.get('stage_of_life'), 'in_vivo_reference.stage_of_life',
                               required=False),
        'tissue': _text(ref.get('tissue'), 'in_vivo_reference.tissue', required=False),
        'why_this_one': _text(ref.get('why_this_one'), 'in_vivo_reference.why_this_one',
                              minimum=10),
        'refs': [str(r).strip() for r in ref.get('refs') or [] if str(r).strip()],
    }

    readouts = set()
    for r in (round_plan or {}).get('readouts') or []:
        name = str((r or {}).get('name') or '').strip().lower()
        if name:
            readouts.add(name)

    crits, seen = [], {}
    for i, c in enumerate(draft.get('criteria') or []):
        w = f'criteria[{i}]'
        axis = _enum(c.get('axis'), AXES, f'{w}.axis')
        if axis in seen:
            raise K.ContractError(f'{w}: axis {axis!r} is judged twice')
        sim = _enum(c.get('expected_similarity'), SIMILARITY, f'{w}.expected_similarity')
        refs = [str(x).strip() for x in c.get('refs') or [] if str(x).strip()]
        if sim == 'high' and not refs:
            raise K.ContractError(
                f'{w} ({axis}) expects high similarity with nothing cited. High needs a source '
                f'that measured this axis for a comparable protocol; mechanism alone is '
                f'moderate at most')
        reasons = [_text(x, f'{w}.reasons[{j}]', minimum=5)
                   for j, x in enumerate(c.get('reasons') or [])]
        if not reasons:
            raise K.ContractError(f'{w} ({axis}) gives no reason for its judgement')
        measured = _text(c.get('measured_by'), f'{w}.measured_by', minimum=5)
        # The plan is the fact: an axis whose assay this round actually runs is
        # marked whatever the draft said, because the agent may not have checked.
        in_plan = bool(c.get('in_round_plan'))
        if readouts and any(tok in measured.lower() for tok in readouts):
            in_plan = True
        seen[axis] = True
        crits.append({
            'axis': axis, 'label': _text(c.get('label'), f'{w}.label', required=False)
                                   or AXES[axis],
            'in_vivo': _text(c.get('in_vivo'), f'{w}.in_vivo', minimum=10),
            'protocol': _text(c.get('protocol'), f'{w}.protocol', minimum=10),
            'departure': _text(c.get('departure'), f'{w}.departure', required=False),
            'expected_similarity': sim,
            'confidence': _enum(c.get('confidence') or 'low', CONFIDENCE, f'{w}.confidence'),
            'reasons': reasons, 'measured_by': measured,
            'in_round_plan': bool(in_plan), 'refs': refs,
        })
    if not crits:
        raise K.ContractError('judge at least one axis, or record as a limitation that this run '
                              'could not address fidelity at all')
    # An axis nobody addressed is unknown and says so, never missing: a report
    # that quietly omits maturation state is how a fetal product gets called
    # the adult cell.
    for axis in AXES:
        if axis not in seen:
            crits.append({
                'axis': axis, 'label': AXES[axis],
                'in_vivo': AXIS_ASKS[axis],
                'protocol': 'Not addressed by this run.',
                'departure': None, 'expected_similarity': 'unknown', 'confidence': 'low',
                'reasons': ['No evidence gathered in this run speaks to this axis.'],
                'measured_by': 'not established by this run', 'in_round_plan': False, 'refs': [],
            })
    crits.sort(key=lambda c: list(AXES).index(c['axis']))

    ov = draft.get('overall') or {}
    gap = _text(ov.get('dominant_gap'), 'overall.dominant_gap', required=False)
    if gap and gap not in AXES:
        raise K.ContractError(f'overall.dominant_gap is {gap!r}; name one of the axes: '
                              f'{", ".join(AXES)}')
    weak = [c['axis'] for c in crits if c['expected_similarity'] in ('low', 'unknown')]
    if weak and not gap:
        raise K.ContractError(
            f'{len(weak)} axis/axes are expected low or unknown, so name which one most limits '
            f'the match (overall.dominant_gap) — that is what the next round aims at')
    overall = {
        'expected_match': _enum(ov.get('expected_match'), MATCH, 'overall.expected_match'),
        'summary': _text(ov.get('summary'), 'overall.summary', minimum=10),
        'dominant_gap': gap,
        'what_would_settle_it': _text(ov.get('what_would_settle_it'),
                                      'overall.what_would_settle_it', required=False),
    }
    doc = {
        'kind': 'cell_fidelity', 'schema_version': K.PRODUCTION_VERSION,
        'project_id': project.project_id, 'run_id': run_id, 'created_at': K.now_iso(),
        'created_by': created_by,
        'cell_type': _text(draft.get('cell_type'), 'cell_type', minimum=2),
        'in_vivo_reference': reference, 'criteria': crits, 'overall': overall,
        'limitations': [_text(x, 'limitations', minimum=1) for x in draft.get('limitations') or []],
        'note': NOTE,
    }
    K.require_valid('cell_fidelity', doc)
    return doc
