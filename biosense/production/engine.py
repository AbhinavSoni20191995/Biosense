"""Drive a whole production loop from a request, emitting events as it goes.

This is the headless engine behind the web app and the CLI demo. It runs the
same steps a live Omnigent session would drive, through the same validation:
design or revise a protocol, validate it, approve it where the gates allow,
run the stand-in reactor, analyse, ask the bioinformatics tools, raise a consult
when one is warranted, and commit a decision inside the allowed-action envelope.

What is and is not a model here, stated once:

* The protocol designer (`design.py`) and reviser (`revise.py`) are
  deterministic. No LLM is involved on this path.
* The decisions are authored by the deterministic policy, so every decision
  document carries `authored_by: "policy"`. The reasoning text in them is the
  policy's, not a model's. A live Omnigent run replaces exactly this part and
  writes `authored_by: "orchestrator_agent"` instead. The reasoning report says
  which it was reading, every time.
* Every verdict, metric, QC call and refusal comes from the same tools the live
  agent must pass. Those are not replaced on either path.

Two safety rules this module enforces and will not run past:

* It refuses any request whose `bioreactor_source` is not `synthetic_standin`.
  An unattended loop must not produce a run sheet that someone could take to a
  bench as if it had been approved.
* It refuses to self-approve a protocol when the resolved gates require a named
  human. It stops and reports instead, which is the correct outcome rather than
  a failure to work around.
"""
from __future__ import annotations

import traceback
from pathlib import Path

from .. import contracts as K
from ..bioinformatics import tools as BT
from . import analysis as AN
from . import autonomy as AU
from . import design as DS
from . import orchestrator as OR
from . import protocol as PR
from . import revise as RV

MAX_STEPS = 40          # hard stop, so a loop cannot spin
AUTO_APPROVER = ('automated stand-in run (no human approved this; no wet-lab run is authorised '
                 'by this protocol)')


class EngineRefusal(K.ContractError):
    """The engine declined to run. Not a bug: a gate said no."""


def _noop(event):
    pass


def _emit(on_event, kind, **payload):
    ev = dict(payload, kind=kind)
    on_event(ev)
    return ev


def preflight(request):
    """Resolve the gates and refuse anything an unattended loop must not do."""
    gates = AU.resolve(request)
    if gates['bioreactor_source'] != 'synthetic_standin':
        raise EngineRefusal(
            'this engine runs against the synthetic stand-in reactor only. The request asks for '
            f'{gates["bioreactor_source"]!r}, which needs a named human to approve every protocol and a '
            'real operator to run it. Use the CLI, with a person, for that.')
    if gates['protocol_approval_required']:
        raise EngineRefusal(
            'the resolved gates require a named human to approve each protocol '
            f'(autonomy mode {gates["mode"]!r}). Set human_in_the_loop.mode to "autonomous" for an '
            'unattended stand-in run, or drive the loop from the CLI so a person can approve.')
    return gates


def _bioinfo(request, gates, protocol, loop_dir, iteration, genes):
    """Ask the annotation tools about the engineered genes. Offline, deterministic."""
    bio = BT.annotate(genes, 'knockout', request['product']['target_cell'],
                      knowledge_sets=gates['knowledge_sets'], loop_id=Path(loop_dir).name,
                      iteration=iteration,
                      question='Which protocol parameters does losing this gene implicate, and what '
                               'cannot be settled in this machine at all?',
                      asked_by='engine (deterministic policy path)')
    bio['protocol_cross_check'] = BT.cross_check(bio, protocol)
    K.require_valid('bioinformatics_report', bio)   # validate what is written, not what was built
    d = Path(loop_dir) / f'it{iteration}'
    K.write_json_atomic(d / 'bioinfo.json', bio, overwrite=False)
    K.write_text_atomic(d / 'bioinfo.md', BT.render_report(bio))
    return bio, d / 'bioinfo.json'


def _dataset_analysis(request, gates, report, loop_dir, iteration):
    """Analyse a registered dataset, but only against an uncertainty the loop has.

    Off unless `bioinformatics.datasets` is set, so an existing loop behaves
    exactly as before. When it is on, the step is still conditional on three
    things in order, and returns None rather than inventing any of them:

      1. the analysis raised a hypothesis that does not block revision --
         an analysis with nothing open has no uncertainty to resolve;
      2. a registered dataset is relevant to it, found through the same search
         adapter a person would use;
      3. the datasets the gates permit. Private data is read only when the
         request asked for it.

    Returns (plan, result, paths) or None.
    """
    from ..bioinformatics import execute as EX
    from ..bioinformatics import plan as PLAN
    from ..bioinformatics import registry as TREG
    from ..data import sources as DSRC

    open_h = [h for h in report.get('diagnosis', []) if not h['blocks_protocol_revision']]
    if not open_h:
        return None
    h = open_h[0]

    terms = ' '.join([h['statement']] + [lv['parameter'] for lv in h.get('levers', [])]
                     + [request['product']['target_cell']])
    res = DSRC.get('local').search(terms, include_private=gates['private_data_allowed'])
    if not res.candidates:
        return None

    for cand in res.candidates:
        tools = TREG.for_analysis('population_comparison', cand.modality) or \
            TREG.for_analysis('bulk_expression_comparison', cand.modality)
        if not tools:
            continue
        spec = tools[0]
        atype = ('population_comparison' if 'population_comparison' in spec.analysis_types
                 else 'bulk_expression_comparison')
        try:
            p = PLAN.plan(
                plan_id=f'plan-it{iteration}-{cand.accession}',
                question=f'Does {cand.title} speak to: {h["statement"]}',
                uncertainty_ref=PLAN.uncertainty_from_hypothesis(
                    h, source_artifact=str(Path(loop_dir) / f'it{iteration}' / 'analysis.json'),
                    raised_by='analysis_agent'),
                why_requested=(f'The analysis raised {h["hypothesis_id"]} and the loop holds no '
                               f'measurement that bears on it. The lookup spends no iteration.'),
                dataset_ids=[cand.accession], analysis_type=atype, tool=spec.name,
                decision_relevance=(f'Whether the next revision should move '
                                    f'{", ".join(lv["parameter"] for lv in h.get("levers", [])) or "a parameter"}'),
                parameters_that_may_change=[lv['parameter'] for lv in h.get('levers', [])],
                created_by='engine (deterministic policy path)',
                loop_id=Path(loop_dir).name, iteration=iteration,
                analysis_report=report)
            d = Path(loop_dir) / f'it{iteration}'
            K.write_json_atomic(d / 'analysis_plan.json', p, overwrite=False)
            r = EX.execute(p, executed_by='engine (deterministic policy path)',
                           analysis_id=f'dsanalysis-it{iteration}-{cand.accession}',
                           out=d / 'dataset_analysis.json')
            K.write_text_atomic(d / 'dataset_analysis.md', EX.render(r))
            return p, r, (d / 'analysis_plan.json', d / 'dataset_analysis.json')
        except K.ContractError:
            # A dataset that cannot support this plan is skipped, not forced.
            continue
    return None


def run_loop(request, loop_dir, standin='ipsc_tcell', truth=None, on_event=None, seed=11,
             scenario='clean', release_tests=True, max_steps=MAX_STEPS):
    """Run the loop to a terminal decision. Returns a summary dict.

    `on_event` is called with a dict for every step, so a caller can stream. The
    engine never raises for an ordinary refusal inside the loop: a refusal is an
    event and, where it is terminal, the summary says so.
    """
    on_event = on_event or _noop
    out = Path(loop_dir)
    request = K.require_valid('production_request', request)
    gates = preflight(request)
    from standins import get_standin
    reactor = get_standin(standin)

    OR.loop_init(request, out)
    _emit(on_event, 'loop_init', loop_dir=str(out), loop_id=out.name,
          autonomy=AU.describe(gates), request_id=request['request_id'],
          question=request['question'],
          target=f'{request["desired_output"]["metric"]} >= {request["desired_output"]["value"]} '
                 f'{request["desired_output"]["unit"]} at day {request["desired_output"]["at_day"]}',
          arms=[a['arm_id'] for a in request['genotype_arms']],
          note='Synthetic stand-in reactor. Workflow demonstration, not biological evidence.')

    handoff = DS.build_handoff(request)
    K.write_json_atomic(out / 'handoff.json', handoff, overwrite=False)
    _emit(on_event, 'evidence', path=str(out / 'handoff.json'),
          claims=len(handoff['claims']), sources=len(handoff['source_manifest']),
          readiness=handoff['readiness_reason'],
          citations=[s.get('citation') for s in handoff['source_manifest']])

    genes = sorted({a['gene'] for a in request['genotype_arms'] if a.get('gene')})
    protocol = DS.build_protocol(request)
    search = RV.new_state()
    last_report, last_iteration = None, 0
    iteration, steps, bio_paths, bio_reports, levers = 0, 0, [], [], []
    ds_done, ds_results = False, []
    summary = {'loop_id': out.name, 'loop_dir': str(out), 'request_id': request['request_id'],
               'iterations': [], 'terminal': None, 'best': {}, 'refusals': [], 'consults': [],
               'standin': standin, 'dataset_analyses': []}

    while steps < max_steps:
        steps += 1
        stage_dir = out / f'it{iteration}'
        v = PR.validate_protocol(protocol, [handoff], request)
        _emit(on_event, 'protocol', iteration=iteration, protocol_id=protocol['protocol_id'],
              status=v['status'], design_choices=len(v['design_choices']), gaps=len(v['gaps']),
              errors=v['errors'][:6], title=protocol['title'],
              changes=[c['change'] for c in protocol.get('changes_from_parent', [])
                       if '[tried ' in c['change']][:6])
        if v['status'] != 'needs_approval':
            summary['refusals'].append({'step': 'validate_protocol', 'status': v['status'],
                                        'errors': v['errors'][:6],
                                        'gaps': [g['path'] for g in v['gaps']][:6]})
            _emit(on_event, 'refusal', step='validate_protocol', status=v['status'],
                  detail=v['errors'][:4] or [g['path'] for g in v['gaps']][:4],
                  note='The protocol cannot be approved in this state. The loop stops here rather '
                       'than running something invalid.')
            summary['terminal'] = 'protocol_invalid'
            break

        approved = PR.approve_protocol(protocol, v, AUTO_APPROVER)
        K.write_json_atomic(stage_dir / 'protocol.approved.json', approved, overwrite=False)
        K.write_text_atomic(stage_dir / 'run_sheet.md',
                            PR.render_run_sheet(approved, PR.validate_protocol(approved, [handoff], request),
                                                request))
        _emit(on_event, 'approved', iteration=iteration, protocol_id=approved['protocol_id'],
              approved_by=AUTO_APPROVER, acknowledged=len(approved['approval']['acknowledged_design_choices']),
              run_sheet=str(stage_dir / 'run_sheet.md'))

        run = reactor.simulate_protocol(approved, PR.protocol_sha256(approved), truth=truth, seed=seed,
                                        scenario=scenario, release_tests=release_tests,
                                        run_id=f'{out.name}-it{iteration}')
        K.require_valid('bioreactor_run', run)
        K.write_json_atomic(stage_dir / 'run.json', run, overwrite=False)
        _emit(on_event, 'run', iteration=iteration, run_id=run['run_id'], source=run['source'],
              label=run['label'], notes=run['notes'])

        report = AN.analyze(run, approved, request)
        K.write_json_atomic(stage_dir / 'analysis.json', report, overwrite=False)
        K.write_text_atomic(stage_dir / 'analysis.md', AN.render_report(report))
        arms = [{'arm_id': a['arm_id'], 'metric': a['target']['metric'],
                 'observed': a['target']['observed_mean'], 'required': a['target']['required'],
                 'status': a['target']['status'],
                 'qc_fail': [c['id'] for c in a['qc']['criteria'] if c['status'] == 'FAIL'],
                 'qc_open': [c['id'] for c in a['qc']['criteria']
                             if c['status'] in ('NOT_TESTED', 'SPEC_MISSING', 'MARGINAL')]}
                for a in report['arms']]
        _emit(on_event, 'analysis', iteration=iteration, analysis_id=report['analysis_id'],
              verdict=report['verdict']['status'], summary=report['verdict']['summary'], arms=arms,
              diagnosis=[{'id': h['hypothesis_id'], 'category': h['category'],
                          'confidence': h['confidence'], 'statement': h['statement']}
                         for h in report['diagnosis'][:6]],
              comparison=[{'arm_id': c['arm_id'], 'gene': c.get('gene'),
                           'reference': c['reference_arm_id'],
                           'effects': [{'effect_id': p['effect_id'], 'predicted': p['predicted_direction'],
                                        'verdict': p['verdict'], 'strength': p.get('strength')}
                                       for p in c['predicted_effects']]}
                          for c in report['genotype_comparison']])
        # The search records this iteration itself, inside plan_changes, so that it
        # scores against the previous best before raising it. Recording here would
        # let an arm be compared against its own new reading.
        last_report, last_iteration = report, iteration
        for a in arms:
            best = summary['best'].get(a['arm_id'])
            if best is None or (a['observed'] or 0) > (best['observed'] or 0):
                summary['best'][a['arm_id']] = dict(a, iteration=iteration,
                                                    protocol_id=approved['protocol_id'])

        env = OR.allowed_actions(report, approved, request, OR.load_state(out), bio_reports)
        _emit(on_event, 'envelope', iteration=iteration, allowed=env['allowed'],
              refused=env.get('refused'), policy_advice=env['policy_advice']['type'])

        # One information action per iteration when a gene arm is in play and we
        # have not asked yet. Annotation is offline and free, so asking before
        # spending an iteration is the cheaper order.
        if (genes and gates['bioinformatics_allowed'] and not bio_paths
                and 'request_bioinformatics' in env['allowed']):
            bio, bio_path = _bioinfo(request, gates, approved, out, iteration, genes)
            bio_paths.append(str(bio_path))
            bio_reports.append(bio)
            levers = bio['lever_suggestions']
            _emit(on_event, 'bioinformatics', iteration=iteration, genes=genes,
                  path=str(bio_path), found=[g['symbol'] for g in bio['genes'] if g['found']],
                  not_found=[g['symbol'] for g in bio['genes'] if not g['found']],
                  levers=[{'parameter': l['parameter'], 'direction': l['direction'],
                           'confidence': l['confidence'], 'basis': l['basis']} for l in levers],
                  untestable=bio['untestable_here'],
                  effects=[{'symbol': g['symbol'],
                            'effects': [{'direction': e['direction'], 'readout': e['readout_metric'],
                                         'confidence': e['confidence'], 'source': e['source'],
                                         'process': e['affected_process'], 'conditions': e.get('conditions')}
                                        for e in g['effects']]}
                           for g in bio['genes'] if g['found']])
            try:
                dec, path, _ = OR.commit_decision({
                    'type': 'request_bioinformatics', 'route_to': 'bioinformatics',
                    'protocol_worked': 'undetermined',
                    'reason': f'An annotation lookup for {", ".join(genes)} runs before the first '
                              f'revision: it is offline, spends no iteration, and may scope the '
                              f'change to one arm.',
                    'reasoning': f'Deterministic policy, not a model. The engine runs one annotation '
                                 f'lookup per loop whenever the request declares a gene arm and the '
                                 f'envelope permits an information action. The rule exists because '
                                 f'an analysis reports a per-arm result but not a cause: a shared '
                                 f'change addresses what the arms have in common and leaves the '
                                 f'engineered arm unexplained, while annotation costs nothing and '
                                 f'cannot spend an iteration. A lookup that returns nothing is also '
                                 f'informative, so the rule does not depend on finding an entry.',
                    'considered': [{'type': 'revise_protocol',
                                    'why_not': 'Allowed here, but the rule gathers first: revising '
                                               'now would spend an iteration on a shared change '
                                               'while the engineered arm stayed unexplained.'}],
                    'evidence': [report['verdict']['summary']],
                    'hypotheses': [{'hypothesis_id': 'OH1',
                                    'statement': f'The engineered arm needs different conditions from the '
                                                 f'control rather than simply growing more slowly under '
                                                 f'the same ones.',
                                    'basis': [f'{a["arm_id"]} observed {a["observed"]} vs required '
                                              f'{a["required"]} under an identical schedule'
                                              for a in arms],
                                    'would_be_tested_by': 'A per-arm adjustment that changes only the '
                                                          'implicated parameters',
                                    'status': 'open'}],
                    'instruction': {'to': 'bioinformatics',
                                    'ask': f'Annotated perturbation effects for {", ".join(genes)} and the '
                                           f'protocol parameters they implicate',
                                    'genes': genes},
                    'tool_calls': [{'tool': 'bioinformatics.annotate',
                                    'purpose': 'perturbation effects and implicated levers',
                                    'result_ref': str(bio_path),
                                    'summary': f'{len(levers)} levers implicated: '
                                               + (', '.join(f'{l["parameter"]} ({l["direction"]}, '
                                                            f'{l["confidence"]})' for l in levers)
                                                  or 'none')}],
                }, report, approved, request, out, bio_reports, author='policy')
                _emit(on_event, 'decision', iteration=iteration, decision_id=dec['decision_id'],
                      type=dec['type'], status=dec['status'], authored_by=dec['authored_by'],
                      advances=dec['advances_iteration'], reason=dec['reason'],
                      reasoning=dec.get('reasoning'), path=str(path))
            except K.ContractError as e:
                summary['refusals'].append({'step': 'commit_decision(request_bioinformatics)',
                                            'error': str(e)})
                _emit(on_event, 'refusal', step='commit_decision(request_bioinformatics)', detail=str(e),
                      note='The envelope refused this decision. That is the system working.')

        # Dataset-backed analysis, when the request enabled it and the analysis
        # left an uncertainty open. It reuses `request_bioinformatics` rather than
        # adding a decision type: an analysis is information-gathering, it must
        # not spend an iteration, and the envelope already gets that right.
        if (gates['datasets_allowed'] and not ds_done
                and 'request_bioinformatics' in env['allowed']):
            found = _dataset_analysis(request, gates, report, out, iteration)
            if found:
                dplan, dres, (plan_path, res_path) = found
                ds_done = True
                ds_results.append(dres)
                _emit(on_event, 'dataset_analysis', iteration=iteration,
                      analysis_id=dres['analysis_id'],
                      uncertainty=dres['uncertainty_ref']['ref'],
                      datasets=[d['dataset_id'] for d in dres['datasets']],
                      evidence_class=dres['evidence_class'],
                      source_evidence_class=dres['source_evidence_class'],
                      source_visibility=dres['source_visibility'],
                      confidence=dres['confidence'],
                      findings=[f['finding'] for f in dres['key_findings']][:4],
                      levers=dres['candidate_process_parameters'],
                      path=str(res_path),
                      note='Evidence for the orchestrator. It changes nothing by itself.')
                try:
                    dec, dpath, _ = OR.commit_decision({
                        'type': 'request_bioinformatics', 'route_to': 'bioinformatics',
                        'protocol_worked': 'undetermined',
                        'reason': f'{dplan["uncertainty_ref"]["ref"]} is open and a registered '
                                  f'dataset bears on it. Analysing it spends no iteration.',
                        'reasoning': (
                            f'Deterministic policy, not a model. The engine asks for a dataset '
                            f'analysis only when the analysis report leaves a hypothesis open '
                            f'that does not block revision, a registered dataset is relevant to '
                            f'it, and the request enabled datasets. The rule exists because the '
                            f'alternative is revising against a lever nothing has measured: an '
                            f'analysis that resolves {dplan["uncertainty_ref"]["ref"]} either '
                            f'supports that lever or removes it from consideration, and either '
                            f'outcome is worth more than a blind move. The result is evidence; '
                            f'it does not change a parameter.'),
                        'considered': [{'type': 'revise_protocol',
                                        'why_not': 'Allowed here, but it would spend an iteration '
                                                   'moving a lever no measurement in this loop '
                                                   'speaks to.'}],
                        'evidence': [report['verdict']['summary']]
                                    + [f['finding'] for f in dres['key_findings']][:3],
                        'hypotheses': [{
                            'hypothesis_id': dplan['uncertainty_ref']['ref'],
                            'statement': dplan['uncertainty_ref']['statement'],
                            'basis': [f'{f["finding"]} ({f["basis"]})'
                                      for f in dres['key_findings']][:4]
                                     or ['no readout survived multiplicity correction'],
                            'would_be_tested_by': 'A protocol revision that moves the implicated '
                                                  'parameter against a wild-type control',
                            'status': ('supported' if dres['candidate_process_parameters']
                                       else 'open')}],
                        'instruction': {
                            'to': 'bioinformatics',
                            'ask': dplan['question'],
                            'analysis': {
                                'uncertainty_ref': dplan['uncertainty_ref'],
                                'dataset_ids': dplan['dataset_ids'],
                                'plan': dplan,
                                'parameter_decision': dplan['decision_relevance'],
                            }},
                        'tool_calls': [{
                            'tool': dres['method']['tool'],
                            'purpose': f'resolve {dplan["uncertainty_ref"]["ref"]}',
                            'result_ref': str(res_path),
                            'summary': (
                                f'{dres["evidence_class"]} over a '
                                f'{dres["source_evidence_class"]} source '
                                f'({dres["source_visibility"]}); confidence '
                                f'{dres["confidence"]}; candidates: '
                                + (', '.join(f'{c["parameter"]} ({c["direction"]})'
                                             for c in dres['candidate_process_parameters'])
                                   or 'none'))}],
                    }, report, approved, request, out, bio_reports, author='policy')
                    _emit(on_event, 'decision', iteration=iteration,
                          decision_id=dec['decision_id'], type=dec['type'],
                          status=dec['status'], authored_by=dec['authored_by'],
                          advances=dec['advances_iteration'], reason=dec['reason'],
                          reasoning=dec.get('reasoning'), path=str(dpath))
                except K.ContractError as e:
                    summary['refusals'].append(
                        {'step': 'commit_decision(dataset_analysis)', 'error': str(e)})
                    _emit(on_event, 'refusal', step='commit_decision(dataset_analysis)',
                          detail=str(e),
                          note='The envelope refused this decision. That is the system working.')

        # The policy decides what happens next, inside the same envelope and
        # through the same commit path a reasoning agent would use.
        try:
            doc = OR.policy_decision_document(report, approved, request, OR.load_state(out),
                                              bio_reports)
            decision, dpath, prompt = OR.commit_decision(doc, report, approved, request, out,
                                                         bio_reports, author='policy')
        except K.ContractError as e:
            summary['refusals'].append({'step': 'decide', 'error': str(e)})
            _emit(on_event, 'refusal', step='decide', detail=str(e))
            summary['terminal'] = 'decide_refused'
            break
        _emit(on_event, 'decision', iteration=iteration, decision_id=decision['decision_id'],
              type=decision['type'], status=decision['status'], authored_by=decision['authored_by'],
              advances=decision['advances_iteration'], reason=decision['reason'],
              reasoning=decision.get('reasoning'), allowed=decision['allowed_actions'],
              path=str(dpath))
        summary['iterations'].append({'iteration': iteration, 'verdict': report['verdict']['status'],
                                      'decision': decision['type'], 'arms': arms,
                                      'protocol_id': approved['protocol_id'],
                                      'advances': decision['advances_iteration']})

        if decision['type'] != 'revise_protocol':
            summary['terminal'] = decision['type']
            _emit(on_event, 'terminal', iteration=iteration, type=decision['type'],
                  reason=decision['reason'], verdict=report['verdict']['status'])
            break

        brief = decision['revision_brief']
        _emit(on_event, 'brief', iteration=iteration, brief_id=brief['brief_id'],
              why=brief['why_it_did_not_work'],
              failed=[f'{f["criterion"]} {f["status"]}' for f in brief['failed_criteria']],
              keep_fixed=brief['keep_fixed'],
              levers=[{'parameter': l['parameter'], 'direction': l.get('direction_hint'),
                       'step_id': l.get('step_id'), 'basis': l.get('basis')}
                      for l in brief['levers']])
        try:
            child, plan, search = RV.revise(protocol, report, request, brief, levers,
                                            iteration=iteration + 1, state=search)
            K.write_json_atomic(out / 'search_state.json', search)
        except K.ContractError as e:
            summary['refusals'].append({'step': 'revise', 'error': str(e)})
            _emit(on_event, 'refusal', step='revise', detail=str(e),
                  note='The lever search had nothing left it could move. The loop stops rather than '
                       'emitting an identical protocol.')
            summary['terminal'] = 'search_exhausted'
            break
        _emit(on_event, 'revision', iteration=iteration + 1, protocol_id=child['protocol_id'],
              moves=[{'factor': m['factor'], 'step_id': m['step_id'], 'arm_id': m['arm_id'],
                      'from': m['from'], 'to': m['to'], 'unit': m['unit'],
                      'direction': m['direction'], 'origin': m['origin'],
                      'confidence': m.get('confidence'), 'parameter': m['parameter'],
                      'basis': m['basis']} for m in plan['moves']],
              scored=[{'factor': s['factor'], 'arm_id': s['arm_id'], 'from': s['from'],
                       'to': s['to'], 'outcome': s['outcome'], 'deltas': s['deltas']}
                      for s in plan['scored']],
              reverts=[{'factor': r['factor'], 'arm_id': r['arm_id'], 'to': r['to'],
                        'revert_to': r['revert_to'], 'why': r['why']} for r in plan['reverts']],
              conflicts=plan['conflicts'], findings=plan['findings'],
              unmapped=plan['unmapped'], failing=plan['failing_arms'], passing=plan['passing_arms'])
        protocol, iteration = child, iteration + 1

    # The final iteration never reaches plan_changes, so record it here for the
    # search history the report reads. record_observation is idempotent.
    if last_report is not None:
        RV.record_observation(search, last_iteration, last_report)
    K.write_json_atomic(out / 'search_state.json', search)
    summary['search'] = {'observations': search['observations'], 'best': search['best'],
                         'findings': search['findings']}

    state = OR.load_state(out)
    summary['consults'] = state.get('consults', [])
    summary['iterations_used'] = OR.iterations_used(state)
    summary['max_iterations'] = state['max_iterations']
    summary['steps'] = steps
    if summary['terminal'] is None:
        summary['terminal'] = 'max_steps'
        _emit(on_event, 'refusal', step='engine', detail=f'stopped after {steps} steps',
              note='A hard step cap, not a conclusion.')
    _emit(on_event, 'done', **{k: summary[k] for k in
                               ('loop_id', 'terminal', 'best', 'iterations_used', 'max_iterations')},
          refusals=summary['refusals'])
    K.write_json_atomic(out / 'ENGINE_SUMMARY.json', dict(
        summary,
        note='Deterministic engine run: the protocol designer, the reviser and the decisions are all '
             'deterministic code, and the reactor is a synthetic stand-in. No LLM reasoned here and no '
             'number is biological evidence.'))
    summary['dataset_analyses'] = [
        {'analysis_id': r['analysis_id'], 'uncertainty': r['uncertainty_ref']['ref'],
         'evidence_class': r['evidence_class'],
         'source_evidence_class': r['source_evidence_class'],
         'source_visibility': r['source_visibility'], 'confidence': r['confidence'],
         'datasets': [d['dataset_id'] for d in r['datasets']],
         'candidate_process_parameters': r['candidate_process_parameters']}
        for r in ds_results]
    return summary


def safe_run_loop(*args, **kwargs):
    """run_loop, with any unexpected exception turned into a final event.

    The web app needs a background thread that cannot die silently. An
    EngineRefusal is a refusal event; anything else is a bug and is reported as
    one rather than being swallowed.
    """
    on_event = kwargs.get('on_event') or _noop
    try:
        return run_loop(*args, **kwargs)
    except EngineRefusal as e:
        _emit(on_event, 'refusal', step='preflight', detail=str(e),
              note='The engine declined to start. This is a gate, not a bug.')
        _emit(on_event, 'done', terminal='refused', best={}, refusals=[{'step': 'preflight',
                                                                       'error': str(e)}])
        return {'terminal': 'refused', 'refusals': [{'step': 'preflight', 'error': str(e)}]}
    except Exception as e:  # noqa: BLE001 - a background thread must report, not vanish
        _emit(on_event, 'error', step='engine', detail=f'{type(e).__name__}: {e}',
              traceback=traceback.format_exc()[-2000:])
        _emit(on_event, 'done', terminal='error', best={},
              refusals=[{'step': 'engine', 'error': f'{type(e).__name__}: {e}'}])
        return {'terminal': 'error', 'refusals': [{'step': 'engine', 'error': str(e)}]}
