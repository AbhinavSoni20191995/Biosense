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
    d = Path(loop_dir) / f'it{iteration}'
    K.write_json_atomic(d / 'bioinfo.json', bio, overwrite=False)
    K.write_text_atomic(d / 'bioinfo.md', BT.render_report(bio))
    return bio, d / 'bioinfo.json'


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
    summary = {'loop_id': out.name, 'loop_dir': str(out), 'request_id': request['request_id'],
               'iterations': [], 'terminal': None, 'best': {}, 'refusals': [], 'consults': [],
               'standin': standin}

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
                    'reason': f'Before spending an iteration I want to know which protocol parameters '
                              f'losing {", ".join(genes)} implicates, and which questions this machine '
                              f'cannot settle at all. Annotation is offline and costs no iteration.',
                    'reasoning': f'The analysis gives a per-arm result but not a cause. A shared change '
                                 f'would address whatever both arms have in common and tell me nothing '
                                 f'about the engineered arm specifically. Asking the annotation tools '
                                 f'first is free, cannot spend an iteration, and either returns levers '
                                 f'scoped to the gene or returns nothing - and nothing is itself worth '
                                 f'knowing before I revise.',
                    'considered': [{'type': 'revise_protocol',
                                    'why_not': 'Revising now would spend an iteration on a shared guess '
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
