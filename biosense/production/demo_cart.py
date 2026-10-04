"""Offline walkthrough of the agent-driven loop on the synthetic CAR-T fixtures.

No model runs here. The decision documents are written out in full and pushed
through `commit_decision`, which is the same validation the live orchestrator
agent must pass. So this demo proves the contracts, the allowed-action envelope,
the consult mechanism and the ledger — not that an LLM reasons well.

Where a decision's `reasoning` appears below, read it as a scripted stand-in for
what the orchestrator agent would write after reading the analysis.
"""
import json
from pathlib import Path

from .. import contracts as K
from ..bioinformatics import tools as BT
from . import analysis as AN
from . import autonomy as AU
from . import orchestrator as OR
from . import protocol as PR

EX = K.ROOT / 'examples' / 'cart'
GENE = 'EXH1'


def _approve(it, request, out_dir):
    p = K.read_json(EX / f'protocol.it{it}.synthetic.json')
    h = [K.read_json(EX / f'handoff.it{it}.synthetic.json')]
    v = PR.validate_protocol(p, h, request)
    if v['status'] != 'needs_approval':
        raise K.ContractError(f'protocol it{it} is {v["status"]}: {v["errors"][:4] or v["gaps"][:4]}')
    approved = PR.approve_protocol(p, v, 'DEMO reviewer (scripted; a real run needs a real person)')
    K.write_json_atomic(out_dir / 'protocol.approved.json', approved, overwrite=False)
    K.write_text_atomic(out_dir / 'run_sheet.md',
                        PR.render_run_sheet(approved, PR.validate_protocol(approved, h, request), request))
    return approved


def _run_and_analyse(approved, request, out_dir, tag, release_tests=False):
    from standins import tcell
    truth = K.read_json(EX / 'standin_truth.synthetic.json')
    run = tcell.simulate_protocol(approved, PR.protocol_sha256(approved), truth=truth,
                                  release_tests=release_tests, run_id=f'demo-cart-{tag}')
    K.require_valid('bioreactor_run', run)
    K.write_json_atomic(out_dir / 'run.json', run, overwrite=False)
    report = AN.analyze(run, approved, request)
    K.write_json_atomic(out_dir / 'analysis.json', report, overwrite=False)
    K.write_text_atomic(out_dir / 'analysis.md', AN.render_report(report))
    return run, report


def _arm_line(report):
    return '; '.join(f'{a["arm_id"]} {a["target"]["observed_mean"]} vs {a["target"]["required"]} '
                     f'({a["target"]["status"]})' for a in report['arms'])


def _brief(report, approved, request, iteration, extra_levers=(), keep_extra=(), queries=()):
    b = OR.build_revision_brief(report, approved, request, iteration)
    b['levers'] = list(b['levers']) + list(extra_levers)
    b['keep_fixed'] = list(b['keep_fixed']) + list(keep_extra)
    b['suggested_queries'] = list(dict.fromkeys(list(b['suggested_queries']) + list(queries)))
    return b


def run_demo(out_dir):
    out = Path(out_dir)
    request = K.require_valid('production_request', K.read_json(EX / 'request.cart_d10.json'))
    gates = AU.resolve(request)
    OR.loop_init(request, out)
    trail = [{'step': 'loop-init', 'autonomy': AU.describe(gates)}]

    # ── iteration 0 ──────────────────────────────────────────
    d0 = out / 'it0'
    approved0 = _approve(0, request, d0)
    _, rep0 = _run_and_analyse(approved0, request, d0, 'it0')
    env = OR.allowed_actions(rep0, approved0, request, OR.load_state(out))
    trail.append({'step': 'it0 analysis', 'verdict': rep0['verdict']['status'], 'arms': _arm_line(rep0),
                  'allowed': env['allowed'], 'policy_advice': env['policy_advice']['type']})

    # information action 1: ask the bioinformatics tools about the knockout
    bio = BT.annotate([GENE], 'knockout', request['product']['target_cell'],
                      knowledge_sets=gates['knowledge_sets'], loop_id=out.name, iteration=0,
                      question='Which protocol parameters does losing this gene implicate?',
                      asked_by='orchestrator_agent')
    bio['protocol_cross_check'] = BT.cross_check(bio, approved0)
    bio_path = d0 / 'bioinfo.json'
    K.write_json_atomic(bio_path, bio, overwrite=False)
    K.write_text_atomic(d0 / 'bioinfo.md', BT.render_report(bio))
    levers = bio['lever_suggestions']
    dec, p1, _ = OR.commit_decision({
        'type': 'request_bioinformatics', 'route_to': 'bioinformatics', 'protocol_worked': 'undetermined',
        'reason': f'Both arms missed the target and the knockout is far below the control, so before changing '
                  f'anything shared I want to know which parameters losing {GENE} implicates.',
        'reasoning': f'The analysis shows a shared shortfall plus a much larger one in the knockout arm. A shared '
                     f'cytokine change would address the first but tells me nothing about the second, and I have '
                     f'budget for one information action before revising. Annotation is free and offline, so it '
                     f'costs nothing to ask first.',
        'considered': [{'type': 'revise_protocol', 'why_not': 'Revising now would spend an iteration on the shared '
                                                              'shortfall only, leaving the knockout gap unexplained.'}],
        'evidence': [rep0['verdict']['summary']],
        'hypotheses': [{'hypothesis_id': 'OH1',
                        'statement': f'The knockout needs different activation or cytokine conditions than the '
                                     f'control, rather than simply growing more slowly.',
                        'basis': [f'{GENE}_KO per-input output is far below WT under an identical schedule',
                                  'analysis hypothesis H01 (genotype_specific)'],
                        'would_be_tested_by': 'A per-arm adjustment that changes only the implicated parameters',
                        'status': 'open'}],
        'instruction': {'to': 'bioinformatics', 'ask': f'Annotated perturbation effects for {GENE} knockout in '
                                                       f'{request["product"]["target_cell"]}, and which protocol '
                                                       f'parameters they implicate', 'genes': [GENE]},
        'tool_calls': [{'tool': 'bioinformatics.annotate', 'purpose': 'perturbation effects and implicated levers',
                        'result_ref': str(bio_path),
                        'summary': f'{len(levers)} levers implicated: ' +
                                   ', '.join(f'{l["parameter"]} ({l["direction"]})' for l in levers)}],
    }, rep0, approved0, request, out)
    trail.append({'step': 'it0 decision 1', 'type': dec['type'], 'status': dec['status'],
                  'advances': dec['advances_iteration'], 'levers': [l['parameter'] for l in levers]})

    # information action 2: one of the implicated questions cannot be settled in this machine
    cand = BT.consult_candidates(bio)[0]
    consult, cpath = OR.raise_consult(out, {
        'consult_id': 'consult-00', 'iteration': 0, 'analysis_id': rep0['analysis_id'],
        'raised_by': 'orchestrator_agent', 'urgency': 'normal',
        'question': f'Do you have an in-house read on whether the {GENE} knockout under-performs because of '
                    f'activation-driven exhaustion, or because the 48 h stimulation over-shoots for this line? '
                    f'A number or a sentence is enough.',
        'why_it_matters': 'The two explanations imply opposite changes: shorten the stimulation, or support the '
                          'cells harder through it. Guessing wastes an iteration.',
        'hypothesis_ids': ['OH1'],
        'experiment_requested': {'assay': cand['assay'], 'feasible_in_this_machine': False,
                                 'why_not_feasible': cand['why_not_feasible'],
                                 'what_it_would_resolve': cand['what_it_would_resolve'],
                                 'rough_effort': 'One offline panel on banked material'},
        'expected_gain': {'statement': 'An answer picks the lever directly instead of testing both across two '
                                       'iterations.',
                          'affects': ['activation duration', 'IL-15 dose'],
                          'confidence_without_answer': 'low'},
        'blocking': False,
        'fallback': {'action': 'Apply the annotated levers together on the knockout arm only and read the result.',
                     'consequence': 'If it works we will not know which of the two changes carried it, so the '
                                    'protocol keeps a change that may be unnecessary.'},
        'options': [{'label': 'Exhaustion-driven', 'implication': 'Shorten the stimulation first.'},
                    {'label': 'Over-stimulation for this line', 'implication': 'Shorten and raise IL-15 together.'},
                    {'label': 'No in-house data', 'implication': 'Take the fallback.'}],
    })
    dec, _, _ = OR.commit_decision({
        'type': 'consult_human', 'route_to': 'human', 'protocol_worked': 'undetermined',
        'reason': 'One question that would pick the lever cleanly needs an assay this machine cannot run.',
        'reasoning': 'The annotation implicates two parameters at once and I cannot separate them from the '
                     'measurements I have. Asking is cheap, the consult is non-blocking, and the fallback is '
                     'written down, so the loop keeps moving either way.',
        'considered': [{'type': 'revise_protocol', 'why_not': 'Still possible, and it is the fallback if nobody answers.'}],
        'evidence': [f'untestable here: {cand["assay"]}'],
        'consult_ids': [consult['consult_id']],
        'instruction': {'to': 'human', 'ask': consult['question']},
    }, rep0, approved0, request, out)
    trail.append({'step': 'it0 decision 2', 'type': dec['type'], 'consult': consult['consult_id'],
                  'blocking': consult['blocking'], 'consult_path': str(cpath)})

    answered = OR.answer_consult(
        out, 'consult-00', 'DEMO scientist (scripted)',
        'Our banked material suggests the stimulation simply over-shoots for this line; we see no exhaustion '
        'signature at 24 h. Treat it as over-stimulation.',
        chosen_option='Over-stimulation for this line',
        evidence_status='unpublished_in_house_result', citable_as='design_choice')
    trail.append({'step': 'it0 consult answered', 'by': answered['answer']['answered_by'],
                  'citable_as': answered['answer']['citable_as'],
                  'chosen_option': answered['answer']['chosen_option']})

    # now revise: shared cytokine change this round, knockout levers held for the next
    brief = _brief(rep0, approved0, request, 0,
                   extra_levers=[{'hypothesis_id': 'OH1', 'step_id': 'S03', 'stage_id': 't_cell_activation',
                                  'parameter': 'activation duration (knockout arm only)', 'current': None,
                                  'direction_hint': 'shorten',
                                  'basis': f'{GENE} annotation plus the consult answer: the stimulation '
                                           f'over-shoots for this line'},
                                 {'hypothesis_id': 'OH1', 'step_id': None, 'stage_id': 't_cell_activation',
                                  'parameter': 'IL-15 dose (knockout arm only)', 'current': None,
                                  'direction_hint': 'increase',
                                  'basis': f'{GENE} annotation, confidence synthetic_fixture'}],
                   keep_extra=['Both arms must keep an identical schedule except where a per-arm adjustment is '
                               'justified by a cited genotype effect.'],
                   queries=[f'"{GENE}" AND (knockout OR deficient) AND ("T cell" OR expansion OR exhaustion)'])
    dec, _, prompt = OR.commit_decision({
        'type': 'revise_protocol', 'route_to': 'literature', 'protocol_worked': 'no',
        'reason': brief['why_it_did_not_work'],
        'reasoning': 'Two separable problems: a shared shortfall both arms show, and a knockout-specific gap. The '
                     'shared one has cited evidence available, so I take it first and keep the arms comparable. '
                     'The knockout levers go into the brief so the next round can act on them with evidence '
                     'rather than on the annotation alone.',
        'considered': [{'type': 'stop_budget', 'why_not': 'Three iterations remain and the first change is evidenced.'},
                       {'type': 'consult_human', 'why_not': 'Already raised and answered this round.'}],
        'evidence': [rep0['verdict']['summary'], f'bioinformatics levers: '
                     + ', '.join(l['parameter'] for l in levers)],
        'consult_ids': ['consult-00'],
        'hypotheses': [{'hypothesis_id': 'OH1', 'statement': 'The knockout needs its own activation length and '
                                                             'IL-15 dose.',
                        'basis': [f'{GENE} annotation', 'consult-00 answer', 'analysis H01'],
                        'would_be_tested_by': 'A knockout-only adjustment in a later iteration',
                        'status': 'open'}],
        'revision_brief': brief,
        'tool_calls': [{'tool': 'bioinformatics.cross_check', 'purpose': 'is either lever already differentiated '
                                                                        'between the arms?',
                        'result_ref': str(bio_path),
                        'summary': 'No: the knockout arm runs an identical schedule to the control.'}],
    }, rep0, approved0, request, out)
    trail.append({'step': 'it0 decision 3', 'type': dec['type'], 'status': dec['status'],
                  'advances': dec['advances_iteration'], 'prompt': str(prompt)})

    # ── iterations 1 and 2 ───────────────────────────────────
    for it, reasoning, extra in (
        (1, 'The shared cytokine change moved the control over the target, which confirms the shortfall was '
            'cytokine-limited. The knockout did not follow, so the remaining gap is genotype-specific and the '
            'levers from the annotation and the consult are now the obvious next change.',
         {'levers': [{'hypothesis_id': 'OH1', 'step_id': 'S03', 'stage_id': 't_cell_activation',
                      'parameter': 'activation duration (knockout arm only)', 'current': None,
                      'direction_hint': 'shorten', 'basis': 'consult-00 answer plus the annotation'},
                     {'hypothesis_id': 'OH1', 'step_id': 'S06', 'stage_id': 't_cell_activation',
                      'parameter': 'IL-15 dose (knockout arm only)', 'current': None,
                      'direction_hint': 'increase', 'basis': f'{GENE} annotation'}]}),
    ):
        dpath = out / f'it{it}'
        approved = _approve(it, request, dpath)
        _, rep = _run_and_analyse(approved, request, dpath, f'it{it}')
        brief = _brief(rep, approved, request, it, extra_levers=extra['levers'],
                       keep_extra=['The control arm met the target; do not change its schedule.'])
        dec, _, prompt = OR.commit_decision({
            'type': 'revise_protocol', 'route_to': 'literature', 'protocol_worked': 'no',
            'reason': brief['why_it_did_not_work'], 'reasoning': reasoning,
            'considered': [{'type': 'protocol_succeeded', 'why_not': 'The target applies to both arms and the '
                                                                     'knockout did not reach it.'}],
            'evidence': [rep['verdict']['summary']],
            'consult_ids': ['consult-00'],
            'hypotheses': [{'hypothesis_id': 'OH1', 'statement': 'The knockout needs its own activation length '
                                                                 'and IL-15 dose.',
                            'basis': [f'{GENE}_KO stayed below target while WT crossed it on the same schedule'],
                            'would_be_tested_by': 'Knockout-only adjustments in the next protocol',
                            'status': 'supported'}],
            'revision_brief': brief,
        }, rep, approved, request, out)
        trail.append({'step': f'it{it} analysis', 'verdict': rep['verdict']['status'], 'arms': _arm_line(rep),
                      'decision': dec['type'], 'prompt': str(prompt)})

    d2 = out / 'it2'
    approved2 = _approve(2, request, d2)
    _, rep2 = _run_and_analyse(approved2, request, d2, 'it2')
    dec, _, _ = OR.commit_decision({
        'type': 'complete_qc', 'route_to': 'operator', 'protocol_worked': 'undetermined',
        'reason': 'Both arms met the target, but release tests were never run, so success cannot be declared.',
        'reasoning': 'The knockout-only adjustments closed the gap and the predicted effect now scores as '
                     'consistent. What stands between this and a result is the QC record, not the recipe, so the '
                     'right move is to close the record on this protocol rather than change it.',
        'considered': [{'type': 'protocol_succeeded', 'why_not': 'Sterility, mycoplasma and endotoxin are untested; '
                                                                 'an untested criterion is never a pass.'},
                       {'type': 'revise_protocol', 'why_not': 'Nothing in the result asks for a protocol change.'}],
        'evidence': [rep2['verdict']['summary']],
        'instruction': {'to': 'operator', 'ask': 'Re-run this protocol with sterility, mycoplasma and endotoxin '
                                                 'testing, and report vector copy number.'},
        'hypotheses': [{'hypothesis_id': 'OH1', 'statement': 'The knockout needs its own activation length and '
                                                             'IL-15 dose.',
                        'basis': [f'both arms met the target once the knockout got its own conditions',
                                  'predicted effect E1 scored consistent'],
                        'status': 'supported'}],
    }, rep2, approved2, request, out)
    trail.append({'step': 'it2 analysis', 'verdict': rep2['verdict']['status'], 'arms': _arm_line(rep2),
                  'decision': dec['type']})

    # ── release run on the same protocol ─────────────────────
    d3 = out / 'it2-release'
    d3.mkdir(parents=True, exist_ok=True)
    _, rep3 = _run_and_analyse(approved2, request, d3, 'it2-release', release_tests=True)
    dec, _, _ = OR.commit_decision({
        'type': 'protocol_succeeded', 'route_to': 'human', 'protocol_worked': 'yes',
        'reason': 'Both arms met the target and every release criterion passed.',
        'reasoning': 'Target and QC are both satisfied on the same protocol, so the loop has done its job. This is '
                     'not a released process: one replicate per arm, a synthetic stand-in reactor, and QA sign-off '
                     'and confirmation runs are all still outstanding.',
        'considered': [{'type': 'revise_protocol', 'why_not': 'There is no failed criterion left to chase, and '
                                                              'further optimisation is a new question with a new target.'}],
        'evidence': [rep3['verdict']['summary']],
    }, rep3, approved2, request, out)
    trail.append({'step': 'release run', 'verdict': rep3['verdict']['status'], 'arms': _arm_line(rep3),
                  'decision': dec['type'], 'protocol_worked': dec['protocol_worked']})

    state = OR.load_state(out)
    summary = {
        'loop_dir': str(out),
        'autonomy': AU.describe(gates),
        'trail': trail,
        'ledger': [{'iteration': i['iteration'], 'type': i['decision_type'], 'advances': i['advances_iteration'],
                    'status': i['status'], 'verdict': i['verdict']} for i in state['iterations']],
        'iterations_used': OR.iterations_used(state),
        'max_iterations': state['max_iterations'],
        'consults': state['consults'],
        'note': 'SYNTHETIC fixtures and a synthetic stand-in reactor. The decision documents are scripted stands-in '
                'for the orchestrator agent, pushed through the same validation the live agent must pass. '
                'Nothing here is biological evidence.',
    }
    K.write_json_atomic(out / 'DEMO_SUMMARY.json', summary)
    return summary
