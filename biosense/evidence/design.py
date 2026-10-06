"""Reasoned starting values for the setpoints no source states for this process.

A process cannot run until every physical setpoint has a number, and the
literature almost never reports the one for *this* vessel, this density, this
line. The run that prompted this left seven of them as GAP — agitation,
dissolved oxygen, feed fraction and interval, seed density, temperature — each
with "nothing in this run proposed one". That is true and useless: those
numbers are routinely derived from adjacent practice, and refusing to propose
one is not caution, it is leaving the work undone.

So there is a third option between a cited value and a blocking gap, and it is
the one a ProductionProtocol already names: `design_choice`. A number reasoned
from something stated — a related cell type, another format, a named practice —
carrying what it was derived from, how sure its author is, what would settle
it, and what goes wrong if it is wrong. It never becomes evidence, it is never
reported as measured, and a human approves it before the wet lab.

A value that must not be guessed stays a gap. The difference is judgement, and
it is recorded either way.
"""
from __future__ import annotations

from .. import contracts as K
from .. import parameters as PR
from .. import projects as PJ

CONFIDENCE = ('low', 'moderate', 'high')


def _number(value):
    if isinstance(value, str):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        raise K.ContractError(f'a design choice needs a number or a stated setting; got {value!r}') from None


def choice(parameter_id, value, *, rationale, derived_from, confidence, project,
           unit=None, range_=None, context_note=None, would_settle_it=None,
           risk_if_wrong=None):
    """One choice, checked against the project it is for.

    The parameter has to be one this project actually exposes: a number for a
    knob the process does not have is not a design choice, it is a typo that
    would reach a protocol.
    """
    if confidence not in CONFIDENCE:
        raise K.ContractError(f'confidence must be one of {CONFIDENCE}; got {confidence!r}')
    if not (rationale or '').strip():
        raise K.ContractError('a design choice needs its rationale: the reasoning from what is '
                              'known to the number proposed')
    refs = [str(r).strip() for r in (derived_from or []) if str(r).strip()]
    if not refs:
        raise K.ContractError(
            'a design choice needs derived_from: what it was reasoned from — a claim id, a '
            'PMID, a related cell type or format, or a named practice. If nothing can be '
            'named, this is a gap, not a choice.')
    pid = PR.resolve(parameter_id)
    if pid not in project.parameter_ids:
        raise K.ContractError(
            f'{project.project_id} does not expose {pid}, so no value for it can enter its '
            f'protocol. Its parameters are: {", ".join(sorted(project.parameter_ids))}')
    q = project.parameter(pid)
    v = _number(value)
    lims = []
    if isinstance(v, float):
        if q.minimum is not None and v < q.minimum:
            lims.append(f'below the project minimum {q.minimum}')
        if q.maximum is not None and v > q.maximum:
            lims.append(f'above the project maximum {q.maximum}')
    if lims:
        raise K.ContractError(f'{pid} = {v} is ' + ' and '.join(lims) +
                              '; a design choice may not leave the project\'s own bounds')
    rng = None
    if range_:
        lo, hi = float(range_['lower']), float(range_['upper'])
        rng = {'lower': min(lo, hi), 'upper': max(lo, hi)}
    return {'parameter_id': pid, 'label': q.label, 'unit': unit or q.unit, 'value': v,
            'range': rng, 'rationale': rationale.strip(), 'derived_from': refs,
            'confidence': confidence, 'context_note': context_note,
            'would_settle_it': would_settle_it, 'risk_if_wrong': risk_if_wrong}


def build(draft, *, project, run_id=None, created_by='orchestrator'):
    """A validated DesignChoices document from an agent's draft."""
    if not isinstance(draft, dict):
        raise K.ContractError('the draft must be a JSON object with a "choices" list')
    rows = draft.get('choices')
    if not isinstance(rows, list) or not rows:
        raise K.ContractError('"choices" must be a non-empty list')
    seen, out = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise K.ContractError(f'each choice is an object; got {row!r}')
        c = choice(row.get('parameter_id'), row.get('value'), project=project,
                   rationale=row.get('rationale'), derived_from=row.get('derived_from'),
                   confidence=row.get('confidence'), unit=row.get('unit'),
                   range_=row.get('range'), context_note=row.get('context_note'),
                   would_settle_it=row.get('would_settle_it'),
                   risk_if_wrong=row.get('risk_if_wrong'))
        if c['parameter_id'] in seen:
            raise K.ContractError(f'{c["parameter_id"]} is given twice; one choice per parameter')
        seen.add(c['parameter_id'])
        out.append(c)
    doc = {'schema_version': K.PRODUCTION_VERSION, 'created_at': K.now_iso(),
           'created_by': created_by, 'project_id': project.project_id, 'run_id': run_id,
           'choices': out}
    K.require_valid('design_choices', doc)
    return doc


def by_parameter(doc):
    """{parameter_id: choice} from a validated document, or {} from nothing."""
    return {c['parameter_id']: c for c in ((doc or {}).get('choices') or [])}
