"""Command-line tools for tests and Omnigent agents (run from the repo root).

    .venv/bin/python -m biosense.cli <command> ...      (or: uv run --frozen python -m biosense.cli ...)

Every command prints a compact JSON summary to stdout and writes full
artifacts to the paths given. Exit code 0 = ok, 1 = contract/budget error,
2 = blocked or invalid input reported (artifact still written where possible).
"""
import argparse
import json
import sys
from pathlib import Path

from . import campaign as CAM
from . import contracts as K
from . import outcomes as OC
from . import simulation as SIM
from .adapters.literature import assemble_scenario


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_assemble(a):
    scenario = assemble_scenario(K.read_json(a.handoff), K.read_json(a.selection),
                                 K.read_json(a.assumptions) if a.assumptions else None)
    K.require_valid('scenario', scenario)
    K.write_json_atomic(a.out, scenario)
    _print({'out': a.out, 'scenario_id': scenario['scenario_id'], 'mode': scenario['mode'],
            'status': scenario['validation']['status'], 'n_parameters': len(scenario['parameters']),
            'gaps': [f'{g["gap_id"]} {g["model_input"]}: {g["reason"]}' + (f' ({g["detail"]})' if g.get('detail') else '')
                     for g in scenario['gaps']],
            'errors': scenario['errors'],
            'unsupported_controls': [f'{u["claim_id"]} {u["parameter"]}={u["value"]} {u["unit"]} conflict={u["conflict_status"]}'
                                     for u in scenario['unsupported_controls']],
            'synthetic_inputs': len(scenario['synthetic_inputs'])})
    return 0 if scenario['validation']['status'] == 'runnable' else 2


def cmd_validate(a):
    r = SIM.validate_scenario(K.read_json(a.scenario))
    _print(r)
    return {'runnable': 0, 'blocked': 2, 'invalid': 1}[r['status']]


def cmd_simulate(a):
    scenario, exp = K.read_json(a.scenario), K.read_json(a.experiment)
    result, rows = SIM.run_simulation(scenario, exp, run_id=a.run_id)
    saved = SIM.save_run(result, rows, a.out)
    _print({'out': a.out, 'run_id': saved['run_id'], 'status': saved['status'], 'endpoint_state': saved['endpoint_state'],
            'errors': saved['errors'], 'warnings': saved['warnings'][:5],
            'note': 'Ad hoc run outside any campaign budget ledger'})
    return 0 if saved['status'] == 'completed' else 2


def cmd_evaluate(a):
    objective = K.require_valid('objective', K.read_json(a.objective))
    records = [OC.compute_metrics(K.read_json(p), objective) for p in a.result]
    cmp = OC.compare_results(records, a.baseline, objective)
    out = {'objective_id': objective['objective_id'], 'run_metrics': records, 'comparison': cmp,
           'note': 'Ad hoc comparison; campaign-evaluate produces the typed next action'}
    K.write_json_atomic(a.out, out)
    _print({'out': a.out, 'status': cmp['status'], 'best_run_id': cmp['best_run_id'], 'ranking': cmp['ranking']})
    return 0


def cmd_convergence(a):
    r = SIM.convergence_check(K.read_json(a.scenario), K.read_json(a.experiment))
    _print(r)
    return 0 if r['passed'] else 1


def cmd_campaign_init(a):
    cfg = K.read_json(a.config)
    state = CAM.init_campaign(cfg, a.out, config_dir=Path(a.config).parent)
    _print({'campaign_dir': a.out, 'campaign_id': state['campaign_id'], 'mode': state['mode'],
            'scenario_status': state['scenario_status'], 'budget': state['budget'],
            'gaps': [g['model_input'] for g in state['scenario_gaps']]})
    return 0


def cmd_campaign_propose(a):
    prop = CAM.propose_experiments(a.campaign)
    if prop.get('proposal_id'):
        K.write_json_atomic(Path(a.campaign) / 'proposals' / f'{prop["proposal_id"]}.json', prop, overwrite=False)
        prop['path'] = str(Path(a.campaign) / 'proposals' / f'{prop["proposal_id"]}.json')
    _print({k: v for k, v in prop.items() if k != 'experiments'} |
           {'experiments': [{'experiment_id': e['experiment_id'], 'stage_durations_h': e['stage_durations_h'],
                             'role': e['role']} for e in prop['experiments']]})
    return 0 if prop['experiments'] else 2


def cmd_campaign_run(a):
    if a.proposal:
        exps = K.read_json(Path(a.campaign) / 'proposals' / f'{a.proposal}.json')['experiments']
    else:
        exps = [K.read_json(a.experiment)]
    out = []
    for e in exps:
        r = CAM.run_experiment(a.campaign, e)
        out.append({'run_id': r['run_id'], 'experiment_id': r['experiment_id'], 'status': r['status'],
                    'endpoint_state': r['endpoint_state'], 'errors': r['errors']})
    state = CAM._load(a.campaign)
    _print({'runs': out, 'budget': state['budget']})
    return 0


def cmd_campaign_evaluate(a):
    ev = CAM.evaluate_campaign(a.campaign)
    na = ev['next_action']
    path = Path(a.campaign) / 'evaluations' / f'{ev["evaluation_id"]}.json'
    _print({'evaluation': str(path), 'report': str(path.with_suffix('.md')),
            'comparison_status': ev['comparison']['status'], 'best_run_id': ev['comparison']['best_run_id'],
            'ranking': ev['comparison']['ranking'],
            'interpretation': ev['scientific_interpretation'],
            'next_action': {k: na[k] for k in na if k not in ('experiments', 'evidence_refs')} |
                           ({'experiments': [e['stage_durations_h'] for e in na['experiments']]} if 'experiments' in na else {}),
            'budget': ev['budget']})
    return 0


def cmd_check_action(a):
    obj = K.read_json(a.action)
    action = obj.get('next_action', obj)
    errors = CAM.check_action(a.campaign, action)
    _print({'type': action.get('type'), 'valid': not errors, 'errors': errors})
    return 0 if not errors else 1


def cmd_campaign_status(a):
    s = CAM._load(a.campaign)
    _print({k: s[k] for k in ('campaign_id', 'mode', 'scenario_status', 'budget', 'diagnostic_simulations', 'stopped')} |
           {'runs': [{'run_id': r['run_id'], 'experiment_id': r['experiment_id'], 'status': r['status']} for r in s['runs']],
            'evaluations': s['evaluations']})
    return 0


def cmd_campaign_auto(a):
    cfg = K.read_json(a.config)
    s = CAM.run_campaign(cfg, a.out, config_dir=Path(a.config).parent)
    _print({'campaign_dir': a.out, 'runs': [(r['run_id'], r['stage_durations_h'], r['status']) for r in s['runs']],
            'evaluations': s['evaluations'], 'budget': s['budget'], 'stopped': s['stopped'],
            'note': 'Fixed offline pipeline; the live demo has Omnigent agents make each step'})
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m biosense.cli')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('assemble-scenario', help='literature handoff + selection (+ assumptions) -> scenario')
    p.add_argument('--handoff', required=True); p.add_argument('--selection', required=True)
    p.add_argument('--assumptions'); p.add_argument('--out', required=True); p.set_defaults(fn=cmd_assemble)
    p = sub.add_parser('validate-scenario'); p.add_argument('--scenario', required=True); p.set_defaults(fn=cmd_validate)
    p = sub.add_parser('simulate', help='one ad hoc run outside a campaign')
    p.add_argument('--scenario', required=True); p.add_argument('--experiment', required=True)
    p.add_argument('--out', required=True); p.add_argument('--run-id', default='run-adhoc'); p.set_defaults(fn=cmd_simulate)
    p = sub.add_parser('evaluate', help='ad hoc metrics/comparison of saved results')
    p.add_argument('--result', nargs='+', required=True); p.add_argument('--baseline', required=True)
    p.add_argument('--objective', required=True); p.add_argument('--out', required=True); p.set_defaults(fn=cmd_evaluate)
    p = sub.add_parser('verify-convergence'); p.add_argument('--scenario', required=True)
    p.add_argument('--experiment', required=True); p.set_defaults(fn=cmd_convergence)
    p = sub.add_parser('campaign', help='fixed offline pipeline: init, propose, run, evaluate until a non-run action')
    p.add_argument('--config', required=True); p.add_argument('--out', required=True); p.set_defaults(fn=cmd_campaign_auto)
    p = sub.add_parser('campaign-init'); p.add_argument('--config', required=True); p.add_argument('--out', required=True)
    p.set_defaults(fn=cmd_campaign_init)
    p = sub.add_parser('campaign-propose'); p.add_argument('--campaign', required=True); p.set_defaults(fn=cmd_campaign_propose)
    p = sub.add_parser('campaign-run'); p.add_argument('--campaign', required=True)
    g = p.add_mutually_exclusive_group(required=True); g.add_argument('--proposal'); g.add_argument('--experiment')
    p.set_defaults(fn=cmd_campaign_run)
    p = sub.add_parser('campaign-evaluate'); p.add_argument('--campaign', required=True); p.set_defaults(fn=cmd_campaign_evaluate)
    p = sub.add_parser('campaign-status'); p.add_argument('--campaign', required=True); p.set_defaults(fn=cmd_campaign_status)
    p = sub.add_parser('check-action', help='validate a next action (or evaluation file) before dispatch')
    p.add_argument('--campaign', required=True); p.add_argument('--action', required=True); p.set_defaults(fn=cmd_check_action)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (K.ContractError, ValueError, KeyError, FileNotFoundError) as e:
        _print({'error': type(e).__name__, 'message': str(e)})
        return 1


if __name__ == '__main__':
    sys.exit(main())
