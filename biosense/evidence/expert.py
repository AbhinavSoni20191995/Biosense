"""Expert and internal knowledge as a first-class private evidence class.

"We have previously seen reduced viability above 120 rpm" is real evidence. It
is also not a publication, not a measurement BioSense can inspect, and not a
verified causal relationship — and the whole value of recording it as structure
rather than prose is that those distinctions survive.

What it may do:
  * raise or lower the priority of a hypothesis;
  * narrow a parameter search range, with the narrowing attributed to it;
  * change how a result is interpreted;
  * contradict public evidence, visibly.

What it may never do:
  * become a literature citation (`citable` is a const false, and a claim needs a
    source, a paragraph and a verbatim quote that a recollection cannot supply);
  * supply a protocol value (`may_set_protocol_value` is const false — it says
    which parameter to investigate and over what range; the value stays a design
    choice until evidence supports it);
  * leave the machine (`visibility` is const private).

`knowledge_type` keeps the distinction the user asked for: a
`prior_internal_experiment` had data behind it, an `expert_judgement` did not,
and a `process_constraint` is not a belief about biology at all.
"""
from __future__ import annotations

from pathlib import Path

from .. import contracts as K
from .. import parameters as PR
from ..data import roots as DR

TYPES = ('unpublished_observation', 'prior_internal_experiment', 'expert_judgement',
         'process_constraint', 'known_failure', 'other')
CLAIMS = ('avoid_above', 'avoid_below', 'prefer_range', 'no_effect_seen', 'effect_seen',
          'operational_limit')
STORE = 'expert_knowledge'

# How much weight a type can carry on its own. An opinion is not an experiment.
MAX_CONFIDENCE = {'expert_judgement': 'moderate', 'unpublished_observation': 'moderate',
                  'other': 'moderate'}
_RANK = {'low': 0, 'moderate': 1, 'high': 2}


def knowledge(knowledge_id, statement, knowledge_type, *, source='user',
              confidence='moderate', scope=None, parameter_claims=(),
              supporting_dataset_ids=(), notes=None):
    if knowledge_type not in TYPES:
        raise K.ContractError(f'knowledge_type must be one of {TYPES}')
    if confidence not in ('low', 'moderate', 'high'):
        raise K.ContractError('confidence must be low, moderate or high')
    cap = MAX_CONFIDENCE.get(knowledge_type)
    if cap and _RANK[confidence] > _RANK[cap]:
        raise K.ContractError(
            f'{knowledge_type!r} cannot carry confidence {confidence!r} (cap: {cap}). '
            f'An observation or a judgement with no data behind it does not reach the '
            f'confidence of a recorded experiment; record it as '
            f'prior_internal_experiment if data exists.')

    claims = []
    for c in parameter_claims:
        pid = PR.resolve(c['parameter_id'])
        if c['claim'] not in CLAIMS:
            raise K.ContractError(f'parameter claim must be one of {CLAIMS}')
        p = PR.BY_ID[pid]
        unit = c.get('unit') or p.unit
        if unit != p.unit:
            raise K.ContractError(
                f'{pid} is measured in {p.unit}, not {unit}. A unit mismatch here would '
                f'narrow a search range by the wrong amount.')
        if c['claim'] in ('avoid_above', 'avoid_below') and c.get('value') is None:
            raise K.ContractError(f'{c["claim"]!r} needs a value')
        if c['claim'] == 'prefer_range' and (c.get('lower') is None or c.get('upper') is None):
            raise K.ContractError('prefer_range needs both lower and upper')
        claims.append({'parameter_id': pid, 'claim': c['claim'], 'value': c.get('value'),
                       'lower': c.get('lower'), 'upper': c.get('upper'), 'unit': unit,
                       'consequence': c.get('consequence')})

    k = {
        'schema_version': K.PRODUCTION_VERSION, 'knowledge_id': knowledge_id,
        'statement': statement, 'knowledge_type': knowledge_type, 'source': source,
        'visibility': 'private', 'evidence_class': 'expert_knowledge',
        'confidence': confidence, 'created_at': K.now_iso(),
        'scope': scope, 'parameter_claims': claims,
        'supporting_dataset_ids': list(supporting_dataset_ids),
        'citable': False, 'may_set_protocol_value': False, 'notes': notes,
    }
    K.require_valid('expert_knowledge', k)
    return k


# ── storage: private root only ───────────────────────────────────────────
def store_dir(root=None):
    return Path(root or DR.private_root()) / STORE


def save(k, *, root=None, overwrite=False):
    """Write to the private root. Expert knowledge never lands anywhere servable."""
    d = store_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    if not DR.is_private_path(d):
        raise K.ContractError(
            f'refusing to store expert knowledge at {d}: it is outside the private data root. '
            f'What somebody tells BioSense about their own process stays on their machine.')
    path = d / f'{k["knowledge_id"]}.json'
    K.write_json_atomic(path, k, overwrite=overwrite)
    return path


def load_all(*, root=None, project_id=None):
    d = store_dir(root)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob('*.json')):
        try:
            k = K.read_json(p)
        except ValueError:
            continue
        if project_id and (k.get('scope') or {}).get('project_id') not in (None, project_id):
            continue
        out.append(k)
    return out


# ── what it is allowed to do ─────────────────────────────────────────────
def narrow_range(parameter_id, lower, upper, knowledge_items):
    """Apply expert claims to a search range.

    Returns (lower, upper, notes). This is the one place expert knowledge
    changes a number, and every change is attributed to the knowledge_id that
    caused it — a range quietly clipped by a recollection would be indis-
    tinguishable from a range derived from evidence.
    """
    pid = PR.resolve(parameter_id)
    lo, hi, notes = float(lower), float(upper), []
    for k in knowledge_items:
        for c in k.get('parameter_claims', []):
            if c['parameter_id'] != pid:
                continue
            if c['claim'] == 'avoid_above' and c['value'] is not None and c['value'] < hi:
                notes.append(f'{k["knowledge_id"]} caps this at {c["value"]:g} {c["unit"]}: '
                             f'{c.get("consequence") or k["statement"]}')
                hi = min(hi, float(c['value']))
            elif c['claim'] == 'avoid_below' and c['value'] is not None and c['value'] > lo:
                notes.append(f'{k["knowledge_id"]} floors this at {c["value"]:g} {c["unit"]}: '
                             f'{c.get("consequence") or k["statement"]}')
                lo = max(lo, float(c['value']))
            elif c['claim'] == 'prefer_range':
                nlo, nhi = max(lo, float(c['lower'])), min(hi, float(c['upper']))
                if (nlo, nhi) != (lo, hi) and nlo < nhi:
                    notes.append(f'{k["knowledge_id"]} prefers {c["lower"]:g}-{c["upper"]:g} '
                                 f'{c["unit"]}: {c.get("consequence") or k["statement"]}')
                    lo, hi = nlo, nhi
            elif c['claim'] == 'operational_limit' and c['value'] is not None:
                notes.append(f'{k["knowledge_id"]} records an operational limit at '
                             f'{c["value"]:g} {c["unit"]}')
                hi = min(hi, float(c['value']))
    if lo >= hi:
        notes.append(f'internal knowledge narrowed this range to nothing ({lo:g}-{hi:g}); the '
                     f'original range is kept and the conflict is reported rather than resolved')
        return float(lower), float(upper), notes
    return lo, hi, notes


def as_evidence(k):
    """The evidence row a hypothesis carries for this knowledge."""
    return {
        'evidence_class': 'expert_knowledge',
        'stance': 'contradicting' if k['knowledge_type'] == 'known_failure' else 'supportive',
        'strength': {'prior_internal_experiment': 'moderate'}.get(k['knowledge_type'], 'weak'),
        'summary': k['statement'],
        'ref': k['knowledge_id'],
        'visibility': 'private',
        'context_match': 'not_assessed',
        'context_mismatch_note': None,
    }
