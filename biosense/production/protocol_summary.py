"""One recommended protocol, and the ledger of every idea that did not make it.

A discovery run forms several hypotheses and some of them are wrong. Reading
eight cards and working out which survived is not what a scientist wants at the
end of a run; one protocol is. This assembles that page.

Everything in it comes from artifacts that already passed their own contracts —
the project profile, the hypotheses, the candidate parameters, the simulator
comparison. Nothing is written by a model and nothing is inferred from prose.

Three rules, because a protocol is the artifact most likely to be acted on:

* **It is a proposal.** `status` is a const in the contract, the document says
  `PROPOSED — NOT APPROVED`, and it names the step that would approve it:
  `approve-protocol --approved-by "<a person>"`, which is a CLI act with a human
  behind it. Nothing that renders this approves anything.
* **A discarded hypothesis stays on the page.** Showing only the winner hides
  that three alternatives were considered and ruled out, which is the part a
  reviewer most needs. The ledger is part of the protocol, not an appendix.
* **A gap is not a value.** A parameter with no evidence appears as `GAP` and
  sets `blocks_wet_lab`. It is never filled with a plausible number, and the
  summary says in words that it stops the run.
"""
from __future__ import annotations

import uuid

from .. import contracts as K
from .. import parameters as PR
from .. import projects as PJ
from ..evidence import limitations as LIM
from ..evidence import process_reference as PREF
from . import first_pass as FP
from ..evidence import options as OPT
from . import response_model as RM
from . import runtime as RT

APPROVAL_HOW = ('A person approves a protocol by name, from the command line: '
                'python -m biosense.production.cli approve-protocol '
                '--protocol <file> --approved-by "<their name>". No part of the web '
                'application can do it on their behalf.')
# Hypothesis statuses whose parameter change is allowed into the protocol. A
# contradicted or superseded hypothesis is shown in the ledger and not applied.
ADOPTABLE = ('supported', 'proposed')

# How firm a recommended value is. Mirrors the vocabulary a ProductionProtocol
# already uses, so one word means one thing across the whole system.
PROVENANCE_BY_EVIDENCE = {
    'published_literature': 'reported',
    'public_dataset': 'adapted',
    'private_user_dataset': 'adapted',
    'derived_analysis': 'adapted',
    'real_measurement': 'reported',
    'simulation': 'design_choice',
    'synthetic_fixture': 'design_choice',
    'expert_knowledge': 'design_choice',
}


def _provenance_for(hyp):
    """How firm this hypothesis's value is, from the evidence actually behind it.

    The strongest supporting class wins, and expert knowledge never promotes a
    value past `design_choice` however confident it was: it may narrow a range,
    it may not supply a cited number.
    """
    rank = {'reported': 0, 'adapted': 1, 'design_choice': 2, 'gap': 3}
    best = 'gap'
    for row in hyp.get('evidence') or []:
        if row.get('stance') != 'supportive':
            continue
        p = PROVENANCE_BY_EVIDENCE.get(row.get('evidence_class'), 'design_choice')
        if rank[p] < rank[best]:
            best = p
    return best


def _estimate_type_for(hyp):
    """The weakest estimate type among this hypothesis's expected effects.

    Weakest, because a recommendation is only as direct as the least direct
    number supporting it, and a card that showed MEASURED beside a value whose
    only magnitude came from a simulation would say something untrue.
    """
    order = ('judgement', 'target', 'predicted', 'simulated', 'derived', 'measured')
    seen = {e.get('estimate_type') for e in (hyp.get('expected_effects') or [])}
    for t in order:
        if t in seen:
            return t
    return None


def _ledger(hypotheses, adopted_ids, known=None):
    rows = []
    for h in hypotheses:
        hid = h.get('hypothesis_id')
        status = h.get('status') or 'proposed'
        adopted = hid in adopted_ids
        if adopted:
            reason = 'Its parameter change is in the protocol above.'
        elif status == 'contradicted':
            reason = ('Not adopted: the evidence collected in this run contradicts it. '
                      + (h.get('superseded_reason') or '')).strip()
        elif status == 'superseded':
            reason = ('Not adopted: replaced by a later hypothesis. '
                      + (h.get('superseded_reason') or '')).strip()
        elif status == 'rejected':
            reason = ('Not adopted: rejected. ' + (h.get('superseded_reason') or '')).strip()
        elif known is not None and (PR.resolve((h.get('parameter') or {}).get('parameter_id'),
                                               required=False) or '') not in known:
            reason = ('Not adopted: its lever is not a parameter of this project yet. '
                      'Register it as a candidate parameter (with a unit, bounds and a stage) '
                      'and the protocol can carry it.')
        elif (h.get('parameter') or {}).get('candidate_value') is None:
            reason = ('Not adopted: it names a direction but no value to set, so there is '
                      'nothing to put in a protocol yet.')
        else:
            reason = ('Not adopted: another hypothesis moves the same parameter and carries '
                      'stronger evidence.')
        par = h.get('parameter') or {}
        pid = PR.resolve(par.get('parameter_id'), required=False) or par.get('parameter_id')
        rows.append({
            'hypothesis_id': hid, 'statement': h.get('statement') or '',
            'lever': {'parameter_id': par.get('parameter_id'),
                      'label': (par.get('proposed_label') or par.get('label')
                                or (parameter_label(pid) if pid else None)),
                      'stage': par.get('stage'), 'direction': par.get('direction'),
                      'candidate_value': par.get('candidate_value'), 'unit': par.get('unit'),
                      'registered': known is None or (pid or '') in known},
            'status': status, 'adopted': adopted, 'reason': reason,
            'confidence': h.get('confidence'),
            'parameter_id': (h.get('parameter') or {}).get('parameter_id'),
            'supersedes': h.get('supersedes'),
            'superseded_reason': h.get('superseded_reason'),
            'evidence_sources': sorted({e.get('evidence_class') for e in
                                        (h.get('evidence') or []) if e.get('evidence_class')}),
        })
    return rows


def _pick(hypotheses):
    """Which hypotheses become protocol values, one per parameter.

    Strength first, then confidence. A parameter moved by two hypotheses takes
    the better-evidenced one; the other stays in the ledger saying why it lost,
    which is more useful than silently dropping it.
    """
    strength = {'supported': 0, 'proposed': 1}
    conf = {'high': 0, 'moderate': 1, 'low': 2}
    by_param = {}
    for h in hypotheses:
        if (h.get('status') or 'proposed') not in ADOPTABLE:
            continue
        p = h.get('parameter') or {}
        pid, value = p.get('parameter_id'), p.get('candidate_value')
        if not pid or value is None:
            continue
        # A lever registered after the hypothesis was written is found by the
        # name the hypothesis used for it.
        pid = PR.resolve(pid, required=False) or pid
        key = (strength.get(h.get('status') or 'proposed', 9),
               conf.get(h.get('confidence'), 9))
        if pid not in by_param or key < by_param[pid][0]:
            by_param[pid] = (key, h)
    return {pid: h for pid, (_, h) in by_param.items()}


def build(*, project, objective, hypotheses, runtime_mode, control=None, simulator=None,
          research_context=None, uncertainty=None, next_experiment=None, limitations=(),
          privacy=None, provenance=None, run_id=None, request_id=None, title=None,
          evidence_status=None, design_choices=None, round_plan=None, simulation_ran=None,
          proposed_terms=None):
    """Assemble a ProtocolSummary. Validated before it is returned.

    *project* is a loaded `Project`; *hypotheses* are QuantifiedHypothesis
    documents as written by the run.
    """
    if not isinstance(project, PJ.Project):
        raise K.ContractError('a protocol summary is built against a loaded Project')
    mode = RT.normalise_mode(runtime_mode)
    # Only a lever the project has can be in its protocol; one it does not have
    # stays in the ledger as not adopted, and on the timeline as a candidate.
    chosen = {pid: h for pid, h in _pick(hypotheses or []).items()
              if pid in set(project.parameter_ids)}
    adopted_ids = {h['hypothesis_id'] for h in chosen.values()}
    control = dict(control or {})

    stages, gaps, choice_rows = [], [], []
    stage_rows = list(project.stages) + [{'stage_id': 'all', 'label': 'Whole process'}]
    for s in stage_rows:
        sid = s['stage_id']
        params = []
        for pid in sorted(project.parameter_ids):
            q = project.parameter(pid)
            if q.stage != sid:
                continue
            h = chosen.get(pid)
            current = _current_value(project, pid, q, control)
            recommended = (h.get('parameter') or {}).get('candidate_value') if h else current
            if h:
                prov = _provenance_for(h)
                est = _estimate_type_for(h)
                reason = h.get('statement')
                sources = sorted({e.get('evidence_class') for e in (h.get('evidence') or [])
                                  if e.get('evidence_class')})
                href = h.get('hypothesis_id')
            else:
                # Nothing proposed a change. The value stays what the process
                # already uses; where there is none, a reasoned starting value
                # may have been proposed, and only where neither exists is it a
                # gap. A setpoint nobody will ever measure for this exact vessel
                # is routinely derived from adjacent practice: refusing to name
                # one leaves the work undone rather than making it safer.
                prov = 'design_choice' if current is not None else 'gap'
                est, reason, sources, href = None, None, [], None
            dc = (design_choices or {}).get(pid)
            if not dc and not h and current is None:
                # Nothing in the run set it: the process reference may. A
                # physical setpoint shared across suspension cultures is filled
                # from it, labelled with its basis, rather than left a GAP.
                dc = PREF.as_design_choice(pid, project)
            if dc and not h and current is None:
                prov, recommended = 'design_choice', dc['value']
                reason = dc['rationale']
                sources = ['design_choice']
                choice_rows.append(dc)
            if prov == 'gap':
                gaps.append({'parameter_id': pid,
                             'why': f'{q.label} has no recorded value for this process, no '
                                    f'hypothesis set one, and no starting value was proposed '
                                    f'for it. A reasoned starting value with its basis would '
                                    f'unblock it; so would a measurement.'})
            params.append({
                'parameter_id': pid, 'label': q.label, 'unit': q.unit,
                'control_value': current, 'recommended_value': recommended,
                'changed': bool(h) and recommended != current,
                'direction': (h.get('parameter') or {}).get('direction') if h else None,
                'suggested_range': ({'minimum': q.minimum, 'maximum': q.maximum}
                                    if q.minimum is not None or q.maximum is not None else None),
                'provenance': prov, 'estimate_type': est,
                'design_choice': ({'confidence': dc['confidence'],
                                   'reference_entry': dc.get('reference_entry'),
                                   'derived_from': dc['derived_from'],
                                   'would_settle_it': dc.get('would_settle_it'),
                                   'risk_if_wrong': dc.get('risk_if_wrong'),
                                   'context_note': dc.get('context_note')}
                                  if dc and prov == 'design_choice' and not h else None),
                'simulator_coverage': q.simulator_coverage,
                'reason': reason, 'evidence_sources': sources, 'hypothesis_ref': href,
                'constraint': ({'source': q.bound_origin['source'],
                                'basis': q.bound_origin.get('basis')}
                               if q.bound_origin else None),
            })
        if params:
            stages.append({'stage_id': sid, 'label': s.get('label') or sid,
                           'goal': s.get('wants'), 'parameters': params})

    lead = _lead(chosen, hypotheses or [])
    limits = list(limitations or [])
    if any(q.simulator_coverage in PJ.UNCALIBRATED
           for q in (project.parameter(p) for p in project.parameter_ids)):
        limits.append(
            'Some values rest on a response that was proposed rather than fitted to data. '
            'Those predictions show what the proposal implies and are not calibrated.')
    if gaps:
        limits.append('This protocol has gaps. It cannot be run until each one has evidence '
                      'or a named design choice.')
    from_ref = sorted(c['parameter_id'] for c in choice_rows if c.get('reference_entry'))
    if from_ref:
        limits.append(f'{", ".join(from_ref)} filled from the process reference: established '
                      f'suspension-culture setpoints, not values measured for this cell type. '
                      f'Each row names its entry and basis.')
    if choice_rows:
        low = [c['parameter_id'] for c in choice_rows if c['confidence'] == 'low']
        limits.append(
            f'{len(choice_rows)} setpoint(s) are reasoned starting values, not measurements: '
            f'{", ".join(sorted(c["parameter_id"] for c in choice_rows))}. Each carries what it '
            f'was derived from and what would settle it; a person approves them before the wet '
            f'lab.' + (f' Lowest confidence: {", ".join(sorted(low))}.' if low else ''))
    if mode == 'synthetic_demo':
        limits.append('Produced by the synthetic demonstration path. It shows what the system '
                      'can express and measures no real cell.')

    doc = {
        'schema_version': K.PRODUCTION_VERSION,
        'summary_id': f'proto-{uuid.uuid4().hex[:12]}',
        'created_at': K.now_iso(),
        'run_id': run_id, 'request_id': request_id,
        'status': 'proposed_not_approved',
        'approval': {'required': True, 'approved_by': None, 'how': APPROVAL_HOW},
        'title': title or f'Recommended protocol — {project.name}',
        'objective': objective,
        'runtime_mode': mode,
        'evidence_status': evidence_status,
        'project': {'project_id': project.project_id, 'name': project.name,
                    'version': project.version, 'simulator': project.simulator},
        'research_context': research_context,
        'uncertainty': uncertainty,
        'stages': stages,
        'hypothesis_ledger': _ledger(hypotheses or [], adopted_ids, set(project.parameter_ids)),
        'timeline': timeline(project, hypotheses or []),
        'expected_effects': (lead.get('expected_effects') or []) if lead else [],
        'trade_offs': (lead.get('trade_offs') or []) if lead else [],
        'readouts': list(project.readouts),
        'simulator': simulator,
        'next_experiment': next_experiment or ((lead or {}).get('next_experiment')),
        'round_plan': round_plan,
        'simulation_ran': simulation_ran,
        'proposed_terms': proposed_terms or None,
        'gaps': gaps,
        'design_choices': choice_rows,
        'blocks_wet_lab': bool(gaps),
        'privacy': privacy,
        'limitations': sorted(set(limits)),
        'limitation_groups': LIM.digest(limits),
        'provenance': provenance,
    }
    K.require_valid('protocol_summary', doc)
    return doc


def timeline(project, hypotheses=()):
    """The process laid out in days, for drawing the protocol as a production chain.

    Stage lengths are the project's own defaults, never a recommendation: no
    hypothesis or design choice here changes how long a stage runs unless it
    is a parameter. `candidates` are levers a hypothesis named that the project
    does not have, drawn as dashed rows so they are seen and not mistaken for
    protocol values.
    """
    stages, day = [], 0.0
    for s in project.stages:
        d = s.get('default_days')
        stages.append({'stage_id': s['stage_id'], 'label': s.get('label') or s['stage_id'],
                       'goal': s.get('wants'), 'days': d, 'day_start': day,
                       'min_days': s.get('min_days'), 'max_days': s.get('max_days')})
        day += d or 0.0
    known = set(project.parameter_ids)
    seen, candidates = set(), []
    for h in hypotheses:
        p = h.get('parameter') or {}
        pid = p.get('parameter_id')
        if not pid or pid in seen or (PR.resolve(pid, required=False) or pid) in known:
            continue
        seen.add(pid)
        stage = p.get('stage') if p.get('stage') in {s['stage_id'] for s in stages} else None
        candidates.append({'parameter_id': pid,
                           'label': p.get('proposed_label') or p.get('label') or pid,
                           'unit': p.get('unit'), 'stage_id': stage,
                           'direction': p.get('direction'),
                           'candidate_value': p.get('candidate_value'),
                           'hypothesis_id': h.get('hypothesis_id'),
                           'confidence': h.get('confidence'),
                           'statement': h.get('statement')})
    return {'basis': 'stage lengths are the project defaults (default_days)',
            'total_days': day, 'stages': stages, 'candidates': candidates}


def _current_value(project, pid, q, control):
    """What the process uses today for this parameter.

    Mostly the parameter's own default. Stage duration is the exception: a
    project records it per stage, not on the parameter, so reading the parameter
    alone reported a gap for something the profile states plainly — and a false
    "this blocks the wet lab" is how a real one stops being believed.
    """
    if pid in control:
        return control[pid]
    if q.default_value is not None:
        return q.default_value
    if pid == 'stage_duration_days':
        days = [s.get('default_days') for s in project.stages
                if s.get('default_days') is not None]
        if days:
            return sum(days)
    return None


def _lead(chosen, hypotheses):
    """The hypothesis whose effects head the summary: the best-evidenced adopted one."""
    if not chosen:
        return None
    conf = {'high': 0, 'moderate': 1, 'low': 2}
    return sorted(chosen.values(), key=lambda h: conf.get(h.get('confidence'), 9))[0]


def from_benchmark(result, *, project=None, runtime_mode='synthetic_demo', run_id=None,
                   request_id=None, projects_dir=None):
    """A ProtocolSummary from a BenchmarkResult, which already holds the whole flow."""
    K.require_valid('benchmark_result', result)
    project = project or PJ.load(result['project']['project_id'], projects_dir)
    sim = result.get('simulator') or {}
    control = {}
    for row in (sim.get('handoff') or {}).get('applied') or []:
        if row.get('current_value') is not None:
            control[row['parameter_id']] = row['current_value']
    return build(
        project=project, objective=result['objective'],
        hypotheses=result.get('hypotheses') or [], runtime_mode=runtime_mode,
        control=control, simulator=sim,
        research_context=result.get('research_context'),
        uncertainty=(result.get('uncertainties') or [None])[0],
        next_experiment=result.get('next_experiment'),
        limitations=result.get('limitations') or [],
        privacy=result.get('privacy'), provenance=result.get('provenance'),
        run_id=run_id or result.get('run_id'), request_id=request_id,
        title=result.get('title'),
        evidence_status=(project.simulator or {}).get('evidence_status'))


def from_bundle(bundle, *, project, objective, runtime_mode, run_id=None, request_id=None,
                research_context=None, simulator=None, privacy=None, design_choices=None,
                extra_limitations=(), round_plan=None, simulation_ran=None,
                proposed_terms=None):
    """A ProtocolSummary from an ingested run directory."""
    hyps = []
    for card in bundle.get('hypotheses') or []:
        # ingest returns display cards; the ledger and the picker want the
        # contract fields, which the card preserves under the same names.
        hyps.append({
            'hypothesis_id': card['hypothesis_id'], 'statement': card['statement'],
            'status': card['status'], 'confidence': card.get('confidence'),
            'supersedes': card.get('supersedes'),
            'superseded_reason': card.get('superseded_reason'),
            'parameter': card['parameter'], 'evidence': card.get('evidence') or [],
            'expected_effects': card.get('effects') or [],
            'trade_offs': card.get('trade_offs') or [],
            'next_experiment': card.get('next_experiment') or {},
        })
    return build(
        project=project, objective=objective, hypotheses=hyps, runtime_mode=runtime_mode,
        simulator=simulator, research_context=research_context or bundle.get('research_context'),
        run_id=run_id, request_id=request_id, privacy=privacy,
        limitations=sorted({x for c in (bundle.get('hypotheses') or [])
                            for x in (c.get('limitations') or [])} | set(extra_limitations or ())),
        design_choices=design_choices, round_plan=round_plan, simulation_ran=simulation_ran,
        proposed_terms=proposed_terms)


# ── rendering ───────────────────────────────────────────────────────────
def _fmt(v):
    if v is None:
        return '—'
    if isinstance(v, float):
        return f'{v:g}'
    return str(v)


TAG = {'reported': 'R', 'adapted': 'A', 'design_choice': 'D', 'gap': 'GAP'}


def markdown(doc):
    """The summary as Markdown, for the export action and the terminal.

    Decision first — what the run recommends, what would be compared stage by
    stage and what the simulator played — then the protocol and the next round,
    and everything that justifies it in a supplement (`protocol_report`).
    """
    from . import protocol_report as PRP
    L = [f'# {doc["title"]}', '',
         f'**{RT.LABELS[doc["runtime_mode"]]}** · **PROPOSED — NOT APPROVED**', '',
         f'> {doc["approval"]["how"]}', '',
         f'**Objective.** {doc["objective"]}', '']
    if doc.get('uncertainty'):
        L += [f'**Decision-blocking uncertainty.** {doc["uncertainty"].get("statement", "")}', '']
    if doc['blocks_wet_lab']:
        L += ['> **This protocol has gaps and cannot be run.** Each one needs evidence or a '
              'named design choice.', '']
    L += PRP.glance(doc)
    L += FP.markdown(FP.plan(doc))
    L += PRP.hypotheses_glance(doc)
    L += PRP.comparison(doc, _is_factor)
    L += PRP.protocol(doc, _timeline_markdown(doc), _effect_line)
    L += PRP.next_round(doc)
    if doc.get('gaps'):
        L += ['## Gaps — these block the wet lab', '']
        L += [f'- `{g["parameter_id"]}`: {g["why"]}' for g in doc['gaps']] + ['']
    L += ['---', '', '# Supplementary', '',
          'Everything behind the recommendation: every hypothesis in full, including the ones '
          'not adopted and why, the reasoned starting values, and the limitations.', '']
    L += PRP.supplement(doc)
    return '\n'.join(L)


def _confidence_of(p, ledger):
    if p['provenance'] == 'gap':
        return 'none'
    if p.get('design_choice'):
        return p['design_choice']['confidence']
    if p.get('hypothesis_ref'):
        return ledger.get(p['hypothesis_ref']) or '—'
    return 'in use'


def _timeline_markdown(doc):
    """The production chain, one row per stage: its days, factors and setpoints."""
    tl = doc.get('timeline') or {}
    if not tl.get('stages'):
        return []
    ledger = {r['hypothesis_id']: r.get('confidence') for r in doc['hypothesis_ledger']}
    by_stage = {st['stage_id']: st['parameters'] for st in doc['stages']}

    def cell(rows):
        return '<br>'.join(f'{p["label"]} {_fmt(p["recommended_value"])}'
                           f'{" " + p["unit"] if p.get("unit") else ""} '
                           f'[{TAG[p["provenance"]]}, {_confidence_of(p, ledger)}]'
                           for p in rows) or '—'
    L = ['### Production timeline', '',
         f'Stage lengths are the project defaults; the whole process runs '
         f'{_fmt(tl["total_days"])} days. Each value shows [provenance, confidence]; '
         f'"in use" is the value the process already runs at.', '',
         '| Stage | Days | Factors | Setpoints |', '|---|---|---|---|']
    for s in tl['stages']:
        rows = by_stage.get(s['stage_id'], [])
        factors = [p for p in rows if _is_factor(p)]
        others = [p for p in rows if not _is_factor(p)]
        span = (f'd{_fmt(s["day_start"])}–{_fmt(s["day_start"] + (s["days"] or 0))}'
                if s.get('days') is not None else '—')
        L.append(f'| {s["label"]} | {span} | {cell(factors)} | {cell(others)} |')
    whole = by_stage.get('all', [])
    if whole:
        L.append(f'| Whole process | all | — | {cell(whole)} |')
    L.append('')
    if tl.get('candidates'):
        L += ['Levers a hypothesis named that this project does not have yet (not in the '
              'protocol until registered):', '']
        L += [f'- {c["label"]}' + (f' ({c["unit"]})' if c.get('unit') else '')
              + (f', proposed {_fmt(c["candidate_value"])}' if c.get('candidate_value') is not None
                 else f', {c.get("direction") or "direction only"}')
              + (f' — {c["hypothesis_id"]}' if c.get('hypothesis_id') else '')
              for c in tl['candidates']] + ['']
    return L


MOLAR = ('pM', 'nM', 'uM', 'µM', 'mM')


def _is_factor(p):
    """A concentration: a factor the cells are exposed to over a window, not a setpoint.

    A cell density is per mL too, and is a setpoint; molar units are case-
    sensitive because µm is a size.
    """
    u = (p.get('unit') or '').strip()
    return u in MOLAR or (u.lower().endswith(('/ml', '/l')) and 'cell' not in u.lower())


def _effect_line(e):
    label = e.get('label') or e.get('metric')
    et = (e.get('estimate_type') or '').upper()
    if not e.get('magnitude_estimated', True):
        return f'{label}: direction {e.get("direction", "unknown")}, magnitude not estimated [{et}]'
    if e.get('estimate_type') == 'judgement':
        # A best guess has no baseline or candidate: it is a range of change
        # with a confidence, and is stated as exactly that.
        iv, j = e.get('interval') or {}, e.get('judgement') or {}
        return (f'{label}: {e.get("direction") or "unknown"}, best guess '
                f'{_fmt(iv.get("lower"))}–{_fmt(iv.get("upper"))} {e.get("change_unit") or ""}'
                + (f' (central {_fmt(e["absolute_change"])})'
                   if e.get('absolute_change') is not None else '')
                + f', {j.get("confidence") or "unstated"} confidence [{et}]')
    sign = '+' if (e.get('absolute_change') or 0) > 0 else ''
    rel = (f' ({"+" if e.get("relative_change_pct", 0) > 0 else ""}'
           f'{e["relative_change_pct"]}%)' if e.get('relative_change_pct') is not None else '')
    basis = ''
    if e.get('model_basis'):
        basis = (' — through a response nobody fitted ('
                 + ', '.join(RM.BADGE.get(b, b) for b in e['model_basis']) + ')')
    return (f'{label}: {_fmt((e.get("baseline") or {}).get("value"))} → '
            f'{_fmt((e.get("candidate") or {}).get("value"))} '
            f'{sign}{_fmt(e.get("absolute_change"))} {e.get("change_unit") or ""}'
            f'{rel} [{et}]{basis}')


def display(doc, *, devmap=None, sota=None):
    """The payload the interface renders: the document plus a few derived counts."""
    changed = [p for st in doc['stages'] for p in st['parameters'] if p['changed']]
    ledger = doc['hypothesis_ledger']
    labels = {s['stage_id']: s.get('label') or s['stage_id']
              for s in (doc.get('timeline') or {}).get('stages') or []}
    return {
        **doc,
        'summary_counts': {
            'parameters_total': sum(len(st['parameters']) for st in doc['stages']),
            'parameters_changed': len(changed),
            'hypotheses_total': len(ledger),
            'hypotheses_adopted': sum(1 for r in ledger if r['adopted']),
            'hypotheses_discarded': sum(1 for r in ledger if not r['adopted']),
            'gaps': len(doc.get('gaps') or []),
        },
        'changed_parameters': changed,
        'badge': RT.LABELS[doc['runtime_mode']],
        # The same protocol laid out stage by stage for the bench, and each
        # hypothesis as the one action it asks for.
        'first_pass': FP.plan(doc),
        # Every condition this run surfaced, sorted by what stands behind it,
        # so a person designing a run can see the options rather than an
        # empty stage.
        'options': OPT.build(doc, devmap=devmap, sota=sota),
        'actions': {r['hypothesis_id']: FP.lever_action(r.get('lever'), labels)
                    for r in ledger},
    }


def parameter_label(pid):
    """A canonical parameter's display label, for a caller that has only the id."""
    p = PR.resolve(pid, required=False)
    return PR.BY_ID[p].label if p else pid
