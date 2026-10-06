"""Building a QuantifiedHypothesis from evidence that already exists.

A hypothesis here is a parameter change, several Estimates of what it is
expected to do, and the evidence broken out by class. Three things it enforces:

* **Several outcomes, always.** Cell production is not a single-number problem.
  A contract with one "improvement" field would let a candidate that raises
  yield 18% and costs 1.5 points of viability be reported as an improvement, and
  the orchestrator would never see the cost. `trade_offs()` reads the effects and
  names the gains and the costs separately.

* **Coverage before prediction.** The parameter carries the project's
  `simulator_coverage`. A `not_modelled` parameter still produces a hypothesis
  with measured and derived effects; it simply has no simulated one, and the
  coverage note says why.

* **Evidence stays broken out.** `confidence` is a single word, but it never
  replaces the evidence list: a reader can always see that the private FACS data
  was strong, the public RNA supportive, and the real experiment untested.
"""
from __future__ import annotations

from .. import contracts as K
from .. import parameters as PR
from .. import projects as PJ
from . import estimates as E

_RANK = {'low': 0, 'moderate': 1, 'high': 2}
_STRENGTH = {'weak': 0, 'moderate': 1, 'strong': 2, 'not_assessed': 0}


def evidence_row(evidence_class, stance, summary, *, ref=None, strength='not_assessed',
                 visibility='public', context_match='not_assessed', context_note=None):
    if evidence_class not in K.EVIDENCE_CLASSES:
        raise K.ContractError(f'unknown evidence_class {evidence_class!r}')
    return {'evidence_class': evidence_class, 'stance': stance, 'strength': strength,
            'summary': summary, 'ref': ref, 'visibility': visibility,
            'context_match': context_match, 'context_mismatch_note': context_note}


def trade_offs(effects):
    """Name the gains and the costs, from the Estimates themselves."""
    gains = [e for e in effects if e.get('favourable') is True]
    costs = [e for e in effects if e.get('favourable') is False]
    if not costs:
        summary = ('No measured or predicted cost among the outcomes considered. That is not the '
                   'same as no cost: only the outcomes listed here were examined.')
    elif not gains:
        summary = 'Every outcome that moved, moved unfavourably.'
    else:
        summary = (f'{len(gains)} outcome(s) improve and {len(costs)} worsen. '
                   f'This is a trade-off, not an improvement.')
    return [{'summary': summary,
             'gains': [E.render(e) for e in gains],
             'costs': [E.render(e) for e in costs]}]


def confidence_from(evidence, effects):
    """A single word, plus the reasons behind it. The evidence list stays visible.

    Capped at moderate unless a real measurement supports it: everything else is
    literature, somebody else's data, a model, or a recollection.
    """
    reasons = []
    supportive = [e for e in evidence if e['stance'] == 'supportive']
    contradicting = [e for e in evidence if e['stance'] == 'contradicting']
    classes = {e['evidence_class'] for e in supportive}
    measured = any(e['evidence_class'] == 'real_measurement' for e in supportive)
    strong = [e for e in supportive if e['strength'] == 'strong']

    if contradicting:
        reasons.append(f'{len(contradicting)} source(s) contradict this '
                       f'({", ".join(sorted({e["evidence_class"] for e in contradicting}))})')
    if not supportive:
        return 'low', reasons + ['no source supports this hypothesis']
    reasons.append(f'supported by {len(supportive)} source(s) across '
                   f'{len(classes)} evidence class(es)')

    level = 'low'
    if len(classes) >= 2 and strong:
        level = 'moderate'
    elif len(classes) >= 2 or strong:
        level = 'moderate' if len(supportive) >= 2 else 'low'
    if measured and len(classes) >= 2 and len(strong) >= 2:
        level = 'high'
    if contradicting:
        level = 'low' if level == 'moderate' else level
    if not measured and level == 'high':
        level = 'moderate'
    if level != 'high':
        reasons.append('capped below high: no real experimental measurement of this process '
                       'supports it yet' if not measured else
                       'capped: the supporting evidence is not yet strong across classes')
    if any(e['context_match'] == 'context_mismatch' for e in supportive):
        reasons.append('some supporting evidence is outside the requested research context and '
                       'is counted at reduced relevance')
    return level, reasons


def claim_level_of(effects, coverage):
    """What kind of claim these effects add up to. Computed, never asserted.

    The distinction the whole policy turns on:

      candidate   a plausible relationship worth testing. A direction, and
                  possibly no magnitude at all. This is a real scientific output
                  — most useful hypotheses start here — and it is NOT a failed
                  quantified one.
      quantified  at least one effect carries a magnitude from measurement or
                  derivation.
      simulated   an effect came from a model that covers the parameter.

    A system that may only speak when it can quantify will stay silent exactly
    when a scientist most needs a lead.
    """
    kinds = {e.get('estimate_type') for e in effects or []}
    sized = [e for e in effects or [] if e.get('magnitude_estimated')]
    if kinds & {'simulated'} and coverage == 'modelled':
        return 'simulated', None
    if any(e.get('estimate_type') in ('measured', 'derived', 'predicted') for e in sized):
        return 'quantified', None
    if any(e.get('estimate_type') == 'judgement' for e in sized):
        return 'candidate', ('the magnitudes here are labelled best guesses, not measurements, '
                             'so the claim stays a candidate to test')
    reasons = sorted({e.get('withheld_reason') for e in (effects or [])
                      if e.get('withheld_reason')})
    return 'candidate', (reasons[0] if reasons else
                         'no effect carries a magnitude, so the direction is the claim')


def hypothesis(hypothesis_id, statement, *, project, uncertainty_ref, parameter_id, direction,
               effects, evidence, next_experiment, current_value=None, candidate_value=None,
               search_range=None, research_context=None, limitations=(), created_by='orchestrator',
               loop_id=None, iteration=None, status='proposed', supersedes=None,
               superseded_reason=None, confidence=None, stage=None,
               parameter_label=None):
    """Assemble and validate. `project` is a loaded Project."""
    if not isinstance(project, PJ.Project):
        raise K.ContractError('hypothesis() needs a loaded Project, so that the parameter and '
                              'its simulator coverage come from the system being optimised')
    # An unregistered parameter is a vocabulary gap, not a scientific one. The
    # hypothesis is kept, labelled, and barred from a protocol until the
    # parameter is mapped — because discarding a plausible lever to protect the
    # registry throws away the science to keep the filing tidy.
    registered, proposed_label, note = True, None, None
    try:
        pid = PR.resolve(parameter_id)
        canonical = PR.BY_ID[pid]
    except K.ContractError:
        registered = False
        pid = str(parameter_id or '').strip() or 'unregistered_parameter'
        proposed_label = parameter_label or pid.replace('_', ' ')
        canonical = None
    coverage = project.coverage(pid) if registered else 'not_in_project'
    if not registered:
        note = (f'CANDIDATE PARAMETER — NOT YET REGISTERED: {proposed_label!r} is not in the '
                f'canonical parameter registry, so BioSense has no units, bounds or simulator '
                f'term for it. The hypothesis stands and can be tested; no protocol may adopt '
                f'this value until the parameter is added and mapped.')
    if registered and coverage == 'not_in_project':
        note = (f'{pid} is a canonical parameter but {project.project_id} does not expose it, so '
                f'this project can neither set nor predict it.')
    elif coverage == 'not_modelled':
        note = (f'{project.project_id} exposes {pid} as a real design variable, but its '
                f'simulator has no term for it, so no prediction is produced for it.')
    elif coverage == 'no_simulator':
        note = (f'{project.project_id} has no mechanistic model at all, so no prediction is '
                f'produced. Evidence and analysis are unaffected.')

    if not effects:
        raise K.ContractError(
            'a hypothesis needs at least one expected effect, even if its magnitude is '
            'withheld. `estimates.direction_only(...)` is the right shape when the direction '
            'is supportable and the size is not: a null magnitude with a stated reason is a '
            'valid scientific claim, and an invented number is not.')
    for e in effects:
        K.require_valid('estimate', e)
    if coverage != 'modelled' and any(e['estimate_type'] in ('simulated', 'predicted')
                                      for e in effects):
        raise K.ContractError(
            f'this hypothesis carries a simulated or predicted effect, but {pid} has coverage '
            f'{coverage!r} in {project.project_id}. A prediction for a parameter the model does '
            f'not cover would be invented.')

    level, reasons = (confidence, []) if confidence else confidence_from(evidence, effects)
    # A best guess cannot be surer than the evidence behind the whole hypothesis.
    order = {'low': 0, 'moderate': 1, 'high': 2}
    for e in effects:
        j = e.get('judgement')
        if j and level in order and order[j['confidence']] > order[level]:
            e['limitations'] = list(e.get('limitations') or []) + [
                f'confidence lowered from {j["confidence"]} to {level}: the evidence behind '
                f'this hypothesis supports no more']
            e['judgement'] = {**j, 'confidence': level}
    level_name, level_why = claim_level_of(effects, coverage)
    pp = (project.parameter(pid)
          if registered and coverage in ('modelled', 'not_modelled', 'no_simulator') else None)

    h = {
        'schema_version': K.PRODUCTION_VERSION, 'hypothesis_id': hypothesis_id,
        'created_at': K.now_iso(), 'created_by': created_by,
        'loop_id': loop_id, 'iteration': iteration, 'statement': statement,
        'project_id': project.project_id, 'project_version': project.version,
        'research_context': research_context,
        'uncertainty_ref': dict(uncertainty_ref),
        'parameter': {
            'parameter_id': pid,
            'label': (pp.label if pp else (canonical.label if canonical else proposed_label)),
            'unit': canonical.unit if canonical else None,
            'stage': stage or (pp.stage if pp else None),
            'direction': direction,
            'current_value': current_value, 'candidate_value': candidate_value,
            'search_range': search_range,
            'simulator_coverage': coverage, 'coverage_note': note,
            'registered': registered, 'proposed_label': proposed_label,
        },
        'claim_level': level_name, 'claim_level_reason': level_why,
        'expected_effects': list(effects),
        'trade_offs': trade_offs(effects),
        'evidence': list(evidence),
        'confidence': level, 'confidence_basis': reasons,
        'status': status, 'supersedes': supersedes, 'superseded_reason': superseded_reason,
        'next_experiment': next_experiment,
        'may_change_protocol': False,
        'limitations': sorted(set(list(limitations) + ([note] if note else []))),
    }
    K.require_valid('quantified_hypothesis', h)
    return h


def evidence_table(h):
    """Rows for the UI and the report: class, stance, strength, visibility."""
    order = {'published_literature': 0, 'public_dataset': 1, 'private_user_dataset': 2,
             'derived_analysis': 3, 'expert_knowledge': 4, 'simulation': 5,
             'real_measurement': 6, 'synthetic_fixture': 7}
    return sorted(h['evidence'], key=lambda e: order.get(e['evidence_class'], 9))
