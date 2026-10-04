"""Production-loop CLI for agents and operators (run from the repo root).

    .venv/bin/python -m biosense.production.cli <command> ...   (or: uv run --frozen python -m biosense.production.cli ...)

Every command prints a compact JSON summary and writes full artifacts to the given
paths. Exit code 0 = ok, 1 = contract error, 2 = blocked/invalid result reported.
"""
import argparse
import json
import sys
from pathlib import Path

from .. import contracts as K
from . import analysis as AN
from . import autonomy as AU
from . import orchestrator as OR
from . import protocol as PR
from .runs import validate_run


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _handoffs(paths):
    return [K.read_json(p) for p in paths] if paths else None


def _request(path):
    return K.require_valid('production_request', K.read_json(path)) if path else None


def cmd_validate_request(a):
    errs = K.schema_errors('production_request', K.read_json(a.request))
    _print({'valid': not errs, 'errors': errs})
    return 0 if not errs else 1


def cmd_validate_protocol(a):
    v = PR.validate_protocol(K.read_json(a.protocol), _handoffs(a.handoff), _request(a.request))
    _print(v)
    return {'approved': 0, 'needs_approval': 0, 'blocked': 2, 'invalid': 1}[v['status']]


def cmd_render_protocol(a):
    p = K.read_json(a.protocol)
    req = _request(a.request)
    v = PR.validate_protocol(p, _handoffs(a.handoff), req)
    out = Path(a.out_dir)
    K.write_json_atomic(out / 'protocol_validation.json', v)
    if v['status'] == 'invalid':
        _print({'status': 'invalid', 'errors': v['errors']})
        return 1
    K.write_text_atomic(out / 'run_sheet.md', PR.render_run_sheet(p, v, req))
    K.write_json_atomic(out / 'measurement_template.json', PR.measurement_template(p, v))
    _print({'status': v['status'], 'protocol_sha256': v['protocol_sha256'], 'run_sheet': str(out / 'run_sheet.md'),
            'measurement_template': str(out / 'measurement_template.json'),
            'gaps': [g['path'] for g in v['gaps']], 'design_choices_needing_approval': [d['path'] for d in v['design_choices']],
            'hypothesis_level_genotype_effects': v['hypothesis_effects'], 'warnings': v['warnings']})
    return 0 if v['status'] in ('approved', 'needs_approval') else 2


def cmd_approve_protocol(a):
    p = K.read_json(a.protocol)
    v = PR.validate_protocol(p, _handoffs(a.handoff), _request(a.request))
    approved = PR.approve_protocol(p, v, a.approved_by, a.note or '')
    K.write_json_atomic(a.out, approved, overwrite=False)
    _print({'out': a.out, 'approved_by': a.approved_by, 'protocol_sha256': v['protocol_sha256'],
            'acknowledged_design_choices': approved['approval']['acknowledged_design_choices']})
    return 0


def cmd_validate_run(a):
    r = validate_run(K.read_json(a.run), K.read_json(a.protocol))
    _print(r)
    return 0 if r['valid'] else 1


def cmd_simulate_standin(a):
    from standins import get_standin
    simulate_protocol = get_standin(a.standin).simulate_protocol
    p = K.read_json(a.protocol)
    v = PR.validate_protocol(p, None, None)
    if v['status'] == 'invalid':
        _print({'error': 'protocol invalid', 'errors': v['errors']})
        return 1
    truth = K.read_json(a.truth) if a.truth else None
    run = simulate_protocol(p, v['protocol_sha256'], mode=a.mode, seed=a.seed, scenario=a.scenario, truth=truth,
                            replicates=a.replicates, release_tests=True if a.release_tests else None, run_id=a.run_id)
    K.require_valid('bioreactor_run', run)
    K.write_json_atomic(a.out, run, overwrite=False)
    _print({'out': a.out, 'run_id': run['run_id'], 'mode': run['mode'], 'source': run['source'], 'notes': run['notes'],
            'arms': [{'arm_id': x['arm_id'], 'replicate': x['replicate'], 'harvest': x['harvest']} for x in run['arms']],
            'warning': 'SYNTHETIC STAND-IN: workflow demonstration, not biological evidence'})
    return 0


def cmd_analyze(a):
    rep = AN.analyze(K.read_json(a.run), K.read_json(a.protocol), _request(a.request))
    out = Path(a.out)
    K.write_json_atomic(out, rep, overwrite=False)
    K.write_text_atomic(out.with_suffix('.md'), AN.render_report(rep), overwrite=False)
    _print({'out': str(out), 'report': str(out.with_suffix('.md')), 'verdict': rep['verdict'],
            'data_integrity': rep['data_integrity']['status'],
            'arms': [{'arm_id': x['arm_id'], 'target': x['target']['status'], 'observed': x['target']['observed_mean'],
                      'required': x['target']['required'], 'qc': x['qc']['status']} for x in rep['arms']],
            'genotype_effects': [{'arm': c['arm_id'], 'effect': p['effect_id'], 'predicted': p['predicted_direction'],
                                  'verdict': p['verdict']} for c in rep['genotype_comparison'] for p in c['predicted_effects']],
            'diagnosis': [f'{h["hypothesis_id"]} [{h["category"]}] {h["statement"]}' for h in rep['diagnosis']]})
    return 0


def cmd_loop_init(a):
    s = OR.loop_init(_request(a.request), a.out)
    _print({'loop_dir': a.out, 'loop_id': s['loop_id'], 'max_iterations': s['max_iterations']})
    return 0


def cmd_decide(a):
    d, path, prompt = OR.decide(K.read_json(a.analysis), K.read_json(a.protocol), _request(a.request), a.loop_dir)
    _print({'decision': str(path), 'type': d['type'], 'protocol_worked': d['protocol_worked'], 'route_to': d['route_to'],
            'reason': d['reason'], 'operator_actions': d['operator_actions'],
            'revision_prompt': str(prompt) if prompt else None, 'remaining_iterations': d['remaining_iterations']})
    return 0


def cmd_loop_status(a):
    _print(OR.load_state(a.loop_dir))
    return 0


def cmd_report(a):
    """Write the reasoning report for one loop, or a comparative one for several.

    Agents may run this: it only reads what the loop already wrote, and it never
    reads a file whose name contains 'truth'.
    """
    from . import report as RP
    dirs = a.loop_dir
    if len(dirs) == 1:
        html, pdf = RP.write_report(dirs[0], a.out, a.pdf, a.title)
    else:
        html, pdf = RP.write_comparative_report(dirs, a.out, a.pdf, a.title)
    _print({'loops': [str(d) for d in dirs], 'html': str(html),
            'pdf': str(pdf) if pdf else None,
            'note': 'PDF needs a headless Chromium on PATH; the HTML prints to PDF from any '
                    'browser when none is present.'})
    return 0


def cmd_demo(a):
    """Fixed offline loop on the synthetic fixtures (no LLM): it0 -> revise -> it1 -> complete_qc -> release tests -> success."""
    from analysis_agent.protocol_runner import simulate_protocol
    ex = K.ROOT / 'examples' / 'production'
    req = _request(ex / 'request.monocyte_d25.json')
    truth = K.read_json(ex / 'standin_truth.synthetic.json')
    out = Path(a.out)
    OR.loop_init(req, out)
    trail = []
    for it, run_kw in ((0, {}), (1, {}), (1, {'release_tests': True})):
        p = K.read_json(ex / f'protocol.it{it}.synthetic.json')
        v = PR.validate_protocol(p, [K.read_json(ex / f'handoff.it{it}.synthetic.json')], req)
        p = PR.approve_protocol(p, v, 'DEMO (automatic approval of a synthetic fixture)')
        tag = f'it{it}' + ('-release' if run_kw else '')
        d = out / tag
        K.write_json_atomic(d / 'protocol.approved.json', p, overwrite=False)
        K.write_text_atomic(d / 'run_sheet.md', PR.render_run_sheet(p, PR.validate_protocol(p, None, req), req))
        run = simulate_protocol(p, v['protocol_sha256'], truth=truth, run_id=f'demo-{tag}', **run_kw)
        K.write_json_atomic(d / 'run.json', run, overwrite=False)
        rep = AN.analyze(run, p, req)
        K.write_json_atomic(d / 'analysis.json', rep, overwrite=False)
        K.write_text_atomic(d / 'analysis.md', AN.render_report(rep), overwrite=False)
        dec, path, prompt = OR.decide(rep, p, req, out)
        trail.append({'step': tag, 'verdict': rep['verdict']['status'], 'decision': dec['type'], 'route_to': dec['route_to'],
                      'revision_prompt': str(prompt) if prompt else None})
    _print({'loop_dir': str(out), 'trail': trail,
            'note': 'SYNTHETIC fixtures and stand-in; approvals are automatic only in this demo. The live loop needs a human approver.'})
    return 0 if trail[-1]['decision'] == 'protocol_succeeded' else 1


def cmd_autonomy(a):
    gates = AU.resolve(_request(a.request))
    _print({'gates': gates, 'summary': AU.describe(gates)})
    return 0


def cmd_advise(a):
    env = OR.allowed_actions(K.read_json(a.analysis), K.read_json(a.protocol), _request(a.request),
                             OR.load_state(a.loop_dir), [K.read_json(x) for x in (a.bioinfo or [])])
    _print(env)
    return 0


def cmd_propose_decision(a):
    report, protocol, request = K.read_json(a.analysis), K.read_json(a.protocol), _request(a.request)
    bio = [K.read_json(x) for x in (a.bioinfo or [])]
    decision = K.read_json(a.decision)
    if a.check_only:
        errors = OR.validate_decision(
            {**decision, 'schema_version': K.PRODUCTION_VERSION,
             'decision_id': 'decision-check', 'loop_id': OR.load_state(a.loop_dir)['loop_id'],
             'iteration': len(OR.load_state(a.loop_dir)['iterations']),
             'analysis_id': report['analysis_id'], 'protocol_id': protocol['protocol_id'],
             'run_id': report['run_id'], 'created_at': K.now_iso(), 'authored_by': 'orchestrator_agent',
             'advances_iteration': decision.get('advances_iteration', decision['type'] in OR.ADVANCING),
             'autonomy': {'mode': 'check', 'decision_approval_required': False}},
            report, protocol, request, OR.load_state(a.loop_dir), bio)
        _print({'valid': not errors, 'errors': errors})
        return 0 if not errors else 1
    d, path, prompt = OR.commit_decision(decision, report, protocol, request, a.loop_dir, bio)
    _print({'decision': str(path), 'type': d['type'], 'status': d['status'], 'authored_by': d['authored_by'],
            'advances_iteration': d['advances_iteration'], 'route_to': d['route_to'],
            'allowed_actions': d['allowed_actions'], 'policy_advice': d['policy_advice'],
            'revision_prompt': str(prompt) if prompt else None,
            'remaining_iterations': d['remaining_iterations'],
            'note': 'pending_human: no downstream step may act on this until approve-decision runs'
                    if d['status'] == 'pending_human' else 'committed'})
    return 0


def cmd_approve_decision(a):
    d = OR.approve_decision(a.loop_dir, a.decision_id, a.approved_by, a.note or '')
    _print({'decision_id': d['decision_id'], 'status': d['status'], 'approved_by': d['autonomy']['approved_by'],
            'type': d['type'], 'route_to': d['route_to']})
    return 0


def cmd_consult_raise(a):
    consult, path = OR.raise_consult(a.loop_dir, K.read_json(a.consult))
    _print({'consult': str(path), 'consult_id': consult['consult_id'], 'status': consult['status'],
            'blocking': consult['blocking'], 'question': consult['question'],
            'fallback': consult['fallback']})
    return 0


def cmd_consult_answer(a):
    c = OR.answer_consult(a.loop_dir, a.consult_id, a.answered_by, a.content, a.option,
                          a.evidence_status, a.citable_as, a.declined)
    _print({'consult_id': c['consult_id'], 'status': c['status'], 'answer': c['answer']})
    return 0


def cmd_consult_list(a):
    rows = OR.list_consults(OR.load_state(a.loop_dir), a.status)
    _print({'consults': rows, 'open': sum(1 for r in rows if r['status'] == 'open'),
            'blocking_open': [r['consult_id'] for r in rows if r['status'] == 'open' and r['blocking']]})
    return 0


def cmd_demo_cart(a):
    from .demo_cart import run_demo
    s = run_demo(a.out)
    _print(s)
    last = s['ledger'][-1]['type'] if s['ledger'] else None
    return 0 if last == 'protocol_succeeded' else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m biosense.production.cli')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('validate-request'); p.add_argument('--request', required=True); p.set_defaults(fn=cmd_validate_request)
    for name, fn in (('validate-protocol', cmd_validate_protocol), ('render-protocol', cmd_render_protocol),
                     ('approve-protocol', cmd_approve_protocol)):
        p = sub.add_parser(name)
        p.add_argument('--protocol', required=True)
        p.add_argument('--handoff', nargs='*', help='literature handoff(s) whose claim IDs the protocol cites')
        p.add_argument('--request')
        p.set_defaults(fn=fn)
        if name == 'render-protocol':
            p.add_argument('--out-dir', required=True)
        if name == 'approve-protocol':
            p.add_argument('--approved-by', required=True, help='name of the HUMAN reviewer; agents must never run this')
            p.add_argument('--note'); p.add_argument('--out', required=True)
    p = sub.add_parser('validate-run'); p.add_argument('--run', required=True); p.add_argument('--protocol', required=True)
    p.set_defaults(fn=cmd_validate_run)
    p = sub.add_parser('simulate-standin', help='SYNTHETIC stand-in for the wet lab (analysis_agent simulator)')
    p.add_argument('--protocol', required=True); p.add_argument('--out', required=True)
    p.add_argument('--mode', choices=['minimal', 'max'], default='minimal'); p.add_argument('--seed', type=int, default=5, help='stand-in line biology is sampled from the seed')
    # Choices come from the registry, so a stand-in added there is reachable here.
    # They were hard-coded, which silently hid ipsc_tcell from every agent that
    # drives the loop through this CLI.
    from standins import STANDINS
    p.add_argument('--standin', choices=sorted(set(STANDINS) | {'monocyte'}), default='monocyte',
                   help='which product the stand-in models; only monocyte supports max mode')
    p.add_argument('--scenario', default='clean',
                   help='monocyte: clean|clonal|sensor_fault|stain_fault; '
                        'tcell: clean|poor_viability|low_transduction; '
                        'ipsc_tcell: clean|poor_viability|weak_commitment')
    p.add_argument('--truth', help='hidden genotype truth file (agents must not read it)')
    p.add_argument('--replicates', type=int, default=1); p.add_argument('--release-tests', action='store_true')
    p.add_argument('--run-id'); p.set_defaults(fn=cmd_simulate_standin)
    p = sub.add_parser('analyze'); p.add_argument('--run', required=True); p.add_argument('--protocol', required=True)
    p.add_argument('--request', required=True); p.add_argument('--out', required=True); p.set_defaults(fn=cmd_analyze)
    p = sub.add_parser('loop-init'); p.add_argument('--request', required=True); p.add_argument('--out', required=True)
    p.set_defaults(fn=cmd_loop_init)
    p = sub.add_parser('decide'); p.add_argument('--analysis', required=True); p.add_argument('--protocol', required=True)
    p.add_argument('--request', required=True); p.add_argument('--loop-dir', required=True); p.set_defaults(fn=cmd_decide)
    p = sub.add_parser('loop-status'); p.add_argument('--loop-dir', required=True); p.set_defaults(fn=cmd_loop_status)
    p = sub.add_parser('report', help='reasoning report for a loop: every decision with its '
                                      'reasoning, the search moves, provenance and references')
    p.add_argument('--loop-dir', action='append', required=True,
                   help='loop directory; repeat it for a comparative report')
    p.add_argument('--out', required=True, help='output .html path')
    p.add_argument('--pdf', help='also print a PDF here (needs headless Chromium)')
    p.add_argument('--title'); p.set_defaults(fn=cmd_report)
    p = sub.add_parser('autonomy', help='show the effective human-in-the-loop gates for a request')
    p.add_argument('--request', required=True); p.set_defaults(fn=cmd_autonomy)
    p = sub.add_parser('advise', help='what the orchestrator may choose for this analysis, and what the policy advises')
    p.add_argument('--analysis', required=True); p.add_argument('--protocol', required=True)
    p.add_argument('--request', required=True); p.add_argument('--loop-dir', required=True)
    p.add_argument('--bioinfo', nargs='*'); p.set_defaults(fn=cmd_advise)
    p = sub.add_parser('propose-decision', help='commit an orchestrator-authored decision after validation')
    p.add_argument('--decision', required=True); p.add_argument('--analysis', required=True)
    p.add_argument('--protocol', required=True); p.add_argument('--request', required=True)
    p.add_argument('--loop-dir', required=True); p.add_argument('--bioinfo', nargs='*')
    p.add_argument('--check-only', action='store_true', help='validate without writing')
    p.set_defaults(fn=cmd_propose_decision)
    p = sub.add_parser('approve-decision', help='human release of a decision held by the autonomy mode')
    p.add_argument('--loop-dir', required=True); p.add_argument('--decision-id', required=True)
    p.add_argument('--approved-by', required=True); p.add_argument('--note')
    p.set_defaults(fn=cmd_approve_decision)
    p = sub.add_parser('consult-raise', help='record a question for a person that the machine cannot answer')
    p.add_argument('--loop-dir', required=True); p.add_argument('--consult', required=True)
    p.set_defaults(fn=cmd_consult_raise)
    p = sub.add_parser('consult-answer'); p.add_argument('--loop-dir', required=True)
    p.add_argument('--consult-id', required=True); p.add_argument('--answered-by', required=True)
    p.add_argument('--content', required=True); p.add_argument('--option')
    p.add_argument('--evidence-status', default='expert_judgement',
                   choices=['unpublished_in_house_result', 'published_but_unretrieved', 'expert_judgement', 'declined'])
    p.add_argument('--citable-as', default='design_choice', choices=['reported', 'adapted', 'design_choice'])
    p.add_argument('--declined', action='store_true'); p.set_defaults(fn=cmd_consult_answer)
    p = sub.add_parser('consult-list'); p.add_argument('--loop-dir', required=True)
    p.add_argument('--status', choices=['open', 'answered', 'declined', 'expired'])
    p.set_defaults(fn=cmd_consult_list)
    p = sub.add_parser('demo', help='offline end-to-end loop on the synthetic fixtures (CI smoke test)')
    p.add_argument('--out', required=True); p.set_defaults(fn=cmd_demo)
    p = sub.add_parser('demo-cart', help='offline agent-driven loop on the synthetic CAR-T fixtures '
                                         '(bioinformatics, a human consult and agent-authored decisions)')
    p.add_argument('--out', required=True); p.set_defaults(fn=cmd_demo_cart)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (K.ContractError, ValueError, KeyError, FileNotFoundError) as e:
        _print({'error': type(e).__name__, 'message': str(e)})
        return 1


if __name__ == '__main__':
    sys.exit(main())
