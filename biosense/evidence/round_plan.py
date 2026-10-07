"""The next round at the bench: what a run could not settle, turned into an experiment.

A discovery run ends with things no source, dataset or simulation could settle
— a dose nobody reports for this line, a maturation window, the size of a
term's effect. Those unknowns are not a failure of the run: they are what the
first round in the bioreactor is for. A round plan says, per unknown, which
arms would settle it, what to measure, and what the next run does with each
outcome — written before any result exists, so nothing can be fitted to it
afterwards.

What it is not: an approved protocol, or an instruction to any instrument.
Every arm value is a design choice a person reviews; a wet-lab run needs a
named human approver, and nothing here actuates anything.

    python -m biosense.evidence.cli template round-plan
    python -m biosense.evidence.cli round-plan --project P --draft D --out round_plan.json
"""
from __future__ import annotations

import hashlib
import json

from .. import contracts as K
from .. import parameters as PR

MAX_ARMS = 12
MAX_UNKNOWNS = 12

TEMPLATE = {
    'round': 1,
    'purpose': 'Settle the GM-CSF maturation dose and window before optimising yield.',
    'unknowns': [{
        'id': 'U1',
        'question': 'Does GM-CSF during maturation raise the CD206+/CD163+ fraction, and from '
                    'what dose?',
        'why_unresolved': 'No source reports a dose-response for iPSC-derived monocytes; the '
                          'only public series lacks a GM-CSF-alone arm.',
        'levers': ['gmcsf_maturation_ng_ml'],
    }],
    'arms': [
        {'arm_id': 'A0', 'label': 'control: current process', 'control': True, 'setpoints': {}},
        {'arm_id': 'A1', 'label': 'GM-CSF 10 ng/mL in maturation',
         'setpoints': {'gmcsf_maturation_ng_ml': 10},
         'basis': 'low end of the range two cited protocols use'},
        {'arm_id': 'A2', 'label': 'GM-CSF 50 ng/mL in maturation',
         'setpoints': {'gmcsf_maturation_ng_ml': 50},
         'basis': 'the plateau the proposed term predicts'},
    ],
    'readouts': [
        {'name': 'CD206+ fraction', 'unit': '%', 'when': 'end of maturation, by flow'},
        {'name': 'viable cells per input iPSC', 'unit': 'cells/cell', 'when': 'harvest'},
    ],
    'replicates': 3,
    'decision_rules': [
        {'unknown': 'U1', 'if': 'A2 raises CD206+ by 10 points or more over A0 at equal yield',
         'then': 'carry 50 ng/mL forward and test the window (days) next round'},
        {'unknown': 'U1', 'if': 'no arm moves CD206+ beyond replicate spread',
         'then': 'drop GM-CSF as a lever; mark the proposed term contradicted'},
    ],
    'note': ('One plan per run. Every arm keeps the control alongside it. Values are design '
             'choices with a basis, never a cited value. Targets and decision rules are fixed '
             'now, before any result exists.'),
}


def _text(v, field, limit, minimum=1):
    s = str(v or '').strip()
    if len(s) < minimum:
        raise K.ContractError(f'{field} is required')
    if len(s) > limit:
        raise K.ContractError(f'{field} is {len(s)} characters; the limit is {limit}')
    return s


def build(draft, project, *, run_id=None, created_by=None, extra_levers=(),
          assembled_by='orchestrator'):
    """A validated round plan, or a refusal naming what is wrong.

    *extra_levers* are parameters the run proposed terms for: they may be set in
    an arm before the project has them, because testing them is the point.
    """
    if not isinstance(draft, dict):
        raise K.ContractError('a round plan is an object; see `template round-plan`')
    unknowns = draft.get('unknowns') or []
    if not unknowns:
        raise K.ContractError('a round plan settles at least one unknown; name it')
    if len(unknowns) > MAX_UNKNOWNS:
        raise K.ContractError(f'{len(unknowns)} unknowns; the limit is {MAX_UNKNOWNS}')
    extra = set(extra_levers or ())

    def lever(name, where):
        pid = PR.resolve(name, required=False) or name
        if project.has(pid) or pid in extra:
            return pid
        raise K.ContractError(
            f'{where}: {name!r} is not a parameter of {project.project_id} or a lever this run '
            f'proposed a term for; register it, or propose its term first')

    out_unknowns, ids = [], set()
    for i, u in enumerate(unknowns):
        uid = _text(u.get('id') or f'U{i + 1}', f'unknowns[{i}].id', 16)
        if uid in ids:
            raise K.ContractError(f'unknown {uid} appears twice')
        ids.add(uid)
        out_unknowns.append({
            'id': uid,
            'question': _text(u.get('question'), f'{uid}.question', 400, 10),
            'why_unresolved': _text(u.get('why_unresolved'), f'{uid}.why_unresolved', 600, 10),
            'levers': [lever(x, uid) for x in u.get('levers') or []]})

    arms = draft.get('arms') or []
    if len(arms) < 2:
        raise K.ContractError('a round needs a control arm and at least one arm that changes '
                              'something')
    if len(arms) > MAX_ARMS:
        raise K.ContractError(f'{len(arms)} arms; the limit is {MAX_ARMS}')
    out_arms, arm_ids = [], set()
    for i, a in enumerate(arms):
        aid = _text(a.get('arm_id') or f'A{i}', f'arms[{i}].arm_id', 16)
        if aid in arm_ids:
            raise K.ContractError(f'arm {aid} appears twice')
        arm_ids.add(aid)
        setpoints = {}
        for name, value in (a.get('setpoints') or {}).items():
            pid = lever(name, aid)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise K.ContractError(f'{aid}.{pid} must be a number')
            if project.has(pid):
                q = project.parameter(pid)
                if (q.minimum is not None and value < q.minimum) or (
                        q.maximum is not None and value > q.maximum):
                    raise K.ContractError(
                        f'{aid}.{pid} = {value} is outside the project\'s range '
                        f'[{q.minimum}, {q.maximum}]')
            setpoints[pid] = value
        control = bool(a.get('control'))
        if not control and not setpoints:
            raise K.ContractError(f'{aid} changes nothing; only the control arm may')
        out_arms.append({'arm_id': aid, 'label': _text(a.get('label'), f'{aid}.label', 120),
                         'control': control, 'setpoints': setpoints,
                         # every value an arm sets is chosen, never cited
                         'provenance': 'design_choice',
                         'basis': (None if control else
                                   _text(a.get('basis'), f'{aid}.basis', 300, 5))})
    if sum(a['control'] for a in out_arms) != 1:
        raise K.ContractError('exactly one arm is the control: every change is read against it')

    readouts = []
    for i, r in enumerate(draft.get('readouts') or []):
        readouts.append({'name': _text(r.get('name'), f'readouts[{i}].name', 120),
                         'unit': _text(r.get('unit'), f'readouts[{i}].unit', 40),
                         'when': _text(r.get('when'), f'readouts[{i}].when', 160)})
    if not readouts:
        raise K.ContractError('name what to measure: a round with no readout settles nothing')

    try:
        replicates = int(draft.get('replicates'))
    except (TypeError, ValueError):
        raise K.ContractError('replicates is a whole number of runs per arm') from None
    if replicates < 1:
        raise K.ContractError('replicates must be at least 1')

    rules = []
    for i, r in enumerate(draft.get('decision_rules') or []):
        uid = str(r.get('unknown') or '')
        if uid not in ids:
            raise K.ContractError(f'decision_rules[{i}] names unknown {uid!r}, which the plan '
                                  f'does not have')
        rules.append({'unknown': uid, 'if': _text(r.get('if'), f'rule {i}.if', 300, 5),
                      'then': _text(r.get('then'), f'rule {i}.then', 300, 5)})
    unruled = sorted(ids - {r['unknown'] for r in rules})
    if unruled:
        raise K.ContractError(f'say what the next run does with each outcome: no decision rule '
                              f'for {", ".join(unruled)}')

    plan = {
        'kind': 'round_plan', 'schema_version': K.PRODUCTION_VERSION,
        'round': int(draft.get('round') or 1),
        'project_id': project.project_id, 'project_version': project.version,
        'run_id': run_id, 'created_by': created_by, 'created_at': K.now_iso(),
        'purpose': _text(draft.get('purpose'), 'purpose', 400, 10),
        'unknowns': out_unknowns, 'arms': out_arms, 'readouts': readouts,
        'replicates': replicates, 'decision_rules': rules,
        'limitations': ([f'{replicates} replicate per arm: any difference is directional only']
                        if replicates < 2 else []),
        'status': ('PROPOSED. A person reviews the arms, approves a protocol with a named '
                   'approver, and runs it; nothing here actuates anything.'),
        'assembled_by': assembled_by,
    }
    # The rules are fixed before any result exists. The digest is what a follow-up
    # checks, so a rule quietly rewritten after the results arrive is visible.
    plan['commitment_sha256'] = hashlib.sha256(json.dumps(
        {k: plan[k] for k in ('unknowns', 'arms', 'readouts', 'replicates', 'decision_rules')},
        sort_keys=True).encode()).hexdigest()
    return plan


def from_hypotheses(project, hypotheses, *, run_id=None, replicates=3):
    """A round plan assembled by BioSense when the agents wrote none, or None.

    Mechanical, and labelled so: one control, and one arm per hypothesis that
    names a numeric value for its lever, setting only that lever. The unknown
    each arm settles is that hypothesis; the readouts are the project's own;
    the rule is the same for every arm — carry it forward if the readout moves
    the hypothesised way beyond replicate spread, mark it contradicted if not.
    Nothing here decides what is interesting: that was the hypotheses' job.
    """
    readouts = [{'name': r.get('label') or r['readout_id'], 'unit': r.get('unit') or 'unit',
                 'when': r.get('instrument') or 'at harvest'}
                for r in (project.readouts or [])[:6]]
    arms, unknowns, rules, extra = [], [], [], []
    for h in hypotheses or []:
        par = h.get('parameter') or {}
        value, pid = par.get('candidate_value'), par.get('parameter_id')
        if (h.get('status') or 'proposed') not in ('proposed', 'supported') or not pid \
                or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        rid = PR.resolve(pid, required=False) or pid
        if project.has(rid):
            q = project.parameter(rid)
            if (q.minimum is not None and value < q.minimum) or (
                    q.maximum is not None and value > q.maximum):
                continue
        else:
            extra.append(rid)
        i = len(arms) + 1
        uid, label = f'U{i}', par.get('proposed_label') or par.get('label') or rid
        statement = (h.get('statement') or '').strip() or f'Does changing {label} help?'
        unknowns.append({'id': uid, 'question': statement[:400].ljust(10, '.'),
                         'why_unresolved': ('its effect here has not been measured; the run '
                                            'proposed it as hypothesis '
                                            f'{h.get("hypothesis_id")}'),
                         'levers': [rid]})
        arms.append({'arm_id': f'A{i}', 'label': f'{label} {value}{" " + par["unit"] if par.get("unit") else ""}',
                     'setpoints': {rid: value},
                     'basis': f'the value hypothesis {h.get("hypothesis_id")} proposes'})
        direction = par.get('direction') or 'the hypothesised direction'
        rules += [{'unknown': uid, 'if': f'A{i} moves the readouts in {direction} beyond '
                                         f'replicate spread against A0',
                   'then': f'carry {label} forward and refine its value next round'},
                  {'unknown': uid, 'if': f'A{i} does not differ from A0 beyond replicate spread',
                   'then': f'mark hypothesis {h.get("hypothesis_id")} contradicted at this value'}]
        if len(arms) >= MAX_ARMS - 1:
            break
    if not arms or not readouts:
        return None
    draft = {'round': 1, 'purpose': ('Test each lever the run proposed against the current '
                                     'process, one lever per arm.'),
             'unknowns': unknowns,
             'arms': [{'arm_id': 'A0', 'label': 'control: current process', 'control': True,
                       'setpoints': {}}] + arms,
             'readouts': readouts, 'replicates': replicates, 'decision_rules': rules}
    plan = build(draft, project, run_id=run_id, created_by='biosense',
                 extra_levers=extra, assembled_by='biosense')
    plan['status'] = ('ASSEMBLED BY BIOSENSE from the hypotheses, because the run wrote no round '
                      'plan: one arm per proposed lever at its proposed value, against the '
                      'current process. ' + plan['status'])
    return plan
