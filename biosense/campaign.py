"""Budgeted computational campaign: ledger, proposals, evaluation and next action.

The campaign writer is the only component that assigns run IDs and decrements
the budget. Evaluations are read-only with respect to results and are written
as new immutable files. The next-test policy is deterministic and cites runs.
"""
import copy
import math
from pathlib import Path

from . import contracts as K
from . import outcomes as OC
from . import simulation as SIM
from .adapters.literature import literature_parameter_id, suggested_query
from .models import get_model

STATE_FILE = 'campaign_state.json'


class BudgetExhausted(K.ContractError):
    pass


# ── ledger I/O ──────────────────────────────────────────────

def _load(campaign_dir):
    return K.read_json(Path(campaign_dir) / STATE_FILE)


def _save(campaign_dir, state):
    K.write_json_atomic(Path(campaign_dir) / STATE_FILE, state)


def _inputs(campaign_dir):
    d = Path(campaign_dir) / 'inputs'
    return {k: K.read_json(d / f'{k}.json') for k in ('scenario', 'objective', 'baseline')} | (
        {'uncertainty': K.read_json(d / 'uncertainty.json')} if (d / 'uncertainty.json').exists() else {})


def _resolve(path, base):
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    return Path(base) / p


def init_campaign(config, campaign_dir, config_dir='.'):
    """Snapshot inputs, validate the scenario and create the ledger."""
    campaign_dir = Path(campaign_dir)
    if (campaign_dir / STATE_FILE).exists():
        raise K.ContractError(f'campaign already exists at {campaign_dir}; use a new --out directory')
    scenario = K.read_json(_resolve(config['scenario'], config_dir))
    objective = K.require_valid('objective', K.read_json(_resolve(config['objective'], config_dir)))
    baseline = K.require_valid('experiment', K.read_json(_resolve(config['baseline_experiment'], config_dir)))
    check = SIM.validate_scenario(scenario)
    if check['status'] == 'invalid':
        raise K.ContractError('scenario invalid: ' + '; '.join(check['errors'][:10]))
    design = config['design']
    if design.get('dimension') != 'stage_allocation':
        raise K.ContractError('only the stage_allocation design dimension is implemented')
    if objective.get('total_culture_time_h') != design['total_culture_time_h']:
        raise K.ContractError('objective and design must declare the same total culture time')
    for k, obj in (('scenario', scenario), ('objective', objective), ('baseline', baseline)):
        K.write_json_atomic(campaign_dir / 'inputs' / f'{k}.json', obj, overwrite=False)
    if config.get('uncertainty'):
        unc = K.read_json(_resolve(config['uncertainty'], config_dir))
        if check['status'] == 'runnable':
            SIM.generate_draws(scenario, unc)  # validate early
        K.write_json_atomic(campaign_dir / 'inputs' / 'uncertainty.json', unc, overwrite=False)
    state = {
        'schema_version': K.CAMPAIGN_VERSION,
        'campaign_id': config['campaign_id'],
        'created_at': K.now_iso(),
        'mode': scenario['mode'],
        'label': scenario['label'],
        'scenario_id': scenario['scenario_id'],
        'scenario_sha256': K.sha256_obj(scenario),
        'scenario_status': check['status'],
        'scenario_gaps': check['gaps'],
        'objective_id': objective['objective_id'],
        'objective_sha256': K.sha256_obj(objective),
        'design': design,
        'budget': {'max_runs': config['budget']['max_runs'], 'runs_used': 0,
                   'runs_remaining': config['budget']['max_runs'],
                   'max_rounds': config['budget']['max_rounds'], 'rounds_completed': 0},
        'diagnostic_simulations': 0,
        'runs': [],
        'proposals': [],
        'evaluations': [],
        'stopped': None,
    }
    _save(campaign_dir, state)
    return state


# ── experiment construction ─────────────────────────────────

def _bounds(design):
    total = design['total_culture_time_h']
    lo = design['min_stage_h']['ipsc_expansion']
    hi = total - design['min_stage_h']['cardiac_differentiation']
    return lo, hi, total


def allocation_experiment(scenario, design, expansion_h, role='candidate'):
    _, _, total = _bounds(design)
    e = float(expansion_h)
    return {'schema_version': K.EXPERIMENT_VERSION,
            'experiment_id': f'exp-alloc-E{e:g}h',
            'scenario_id': scenario['scenario_id'],
            'label': f'{e:g} h expansion + {total - e:g} h differentiation (fixed total {total:g} h)',
            'role': role,
            'stage_durations_h': {'ipsc_expansion': e, 'cardiac_differentiation': float(total - e)},
            'harvest': 'end_of_schedule',
            'parameter_draw_id': 'nominal',
            'parameter_overrides': {}}


def _nominal_runs(state):
    return [r for r in state['runs'] if r['parameter_draw_id'] == 'nominal']


def _tried_expansion(state):
    return sorted({r['stage_durations_h']['ipsc_expansion'] for r in _nominal_runs(state)})


def propose_experiments(campaign_dir):
    """Return validated, not-yet-run experiments for the next round (>= 2 when budget allows)."""
    state = _load(campaign_dir)
    inp = _inputs(campaign_dir)
    scenario, design = inp['scenario'], state['design']
    if state['scenario_status'] != 'runnable':
        return {'proposal_id': None, 'experiments': [], 'blocked': True,
                'reason': 'scenario blocked by gaps; request evidence before proposing experiments',
                'gap_ids': [g['gap_id'] for g in state['scenario_gaps']]}
    if state['stopped']:
        return {'proposal_id': None, 'experiments': [], 'blocked': True, 'reason': f'campaign stopped: {state["stopped"]["type"]}'}
    if not state['runs']:
        lo, hi, _ = _bounds(design)
        candidates = [inp['baseline']] + [allocation_experiment(scenario, design, e) for e in design['initial_expansion_h']]
        criteria = ('Round 0: the literature-reference allocation as baseline plus candidates that move time toward '
                    'expansion and toward differentiation; identical initial cells, nominal draw and total time.')
    else:
        last = state['evaluations'][-1] if state['evaluations'] else None
        if not last or last['next_action_type'] != 'run_experiment':
            return {'proposal_id': None, 'experiments': [], 'blocked': True,
                    'reason': 'no pending run_experiment action; evaluate first'}
        candidates = K.read_json(last['path'])['next_action']['experiments']
        criteria = f'Experiments specified by next action of {last["evaluation_id"]}.'
    ran = {r['experiment_sha256'] for r in state['runs']}
    valid, rejected = [], []
    for e in candidates:
        errs = SIM.validate_experiment(scenario, e, max_total_h=design['total_culture_time_h'],
                                       min_stage_h=design['min_stage_h'])
        if errs:
            rejected.append({'experiment_id': e.get('experiment_id'), 'errors': errs})
        elif K.sha256_obj(e) in ran:
            rejected.append({'experiment_id': e['experiment_id'], 'errors': ['already run in this campaign']})
        else:
            valid.append(e)
    remaining = state['budget']['runs_remaining']
    chosen = valid[:remaining]
    proposal = {'proposal_id': f'prop-{len(state["proposals"]):02d}', 'round': state['budget']['rounds_completed'],
                'experiments': chosen, 'not_chosen': [e['experiment_id'] for e in valid[remaining:]],
                'rejected': rejected, 'selection_criteria': criteria,
                'budget_note': f'{len(chosen)} of {len(valid)} valid experiments fit the remaining budget ({remaining} runs)'}
    state['proposals'].append({k: proposal[k] for k in ('proposal_id', 'round', 'selection_criteria', 'budget_note')}
                              | {'experiment_ids': [e['experiment_id'] for e in chosen]})
    _save(campaign_dir, state)
    return proposal


def run_experiment(campaign_dir, experiment):
    """Budget-checked execution. Failed runs still consume budget."""
    campaign_dir = Path(campaign_dir)
    state = _load(campaign_dir)
    inp = _inputs(campaign_dir)
    if state['stopped']:
        raise K.ContractError(f'campaign stopped ({state["stopped"]["type"]}); no further runs')
    if state['budget']['runs_remaining'] <= 0:
        raise BudgetExhausted('computational run budget exhausted')
    run_id = f'run-{len(state["runs"]) + 1:04d}'
    record = {'run_id': run_id, 'experiment_id': experiment.get('experiment_id'),
              'experiment_sha256': K.sha256_obj(experiment), 'scenario_sha256': state['scenario_sha256'],
              'code_sha256': K.code_sha256(), 'code_revision': K.code_revision(),
              'stage_durations_h': experiment.get('stage_durations_h'),
              'parameter_draw_id': experiment.get('parameter_draw_id', 'nominal'),
              'registered_at': K.now_iso(), 'status': 'pending', 'result_path': None}
    state['runs'].append(record)
    state['budget']['runs_used'] += 1
    state['budget']['runs_remaining'] -= 1
    _save(campaign_dir, state)  # pre-execution record: hashes and spec before running
    K.write_json_atomic(campaign_dir / 'runs' / run_id / 'experiment.json', experiment, overwrite=False)
    result, rows = SIM.run_simulation(inp['scenario'], experiment, run_id=run_id,
                                      max_total_h=state['design']['total_culture_time_h'],
                                      min_stage_h=state['design']['min_stage_h'])
    result['campaign_id'] = state['campaign_id']
    saved = SIM.save_run(result, rows, campaign_dir / 'runs' / run_id)
    state = _load(campaign_dir)
    rec = next(r for r in state['runs'] if r['run_id'] == run_id)
    rec['status'] = saved['status']
    rec['result_path'] = str((campaign_dir / 'runs' / run_id / 'result.json').as_posix())
    _save(campaign_dir, state)
    return saved


# ── evaluation ──────────────────────────────────────────────

def _records(state, objective):
    out = []
    for r in state['runs']:
        if r['result_path']:
            out.append(OC.compute_metrics(K.read_json(r['result_path']), objective))
    return out


def sensitivity(scenario, experiment, objective, rel_step=0.1):
    """One-at-a-time central-difference elasticities of yield and purity at one allocation.

    Diagnostic simulations; reported separately from the scientific run budget.
    """
    base, rows = SIM.run_simulation(scenario, experiment, run_id='diag-base')
    if base['status'] != 'completed':
        return {'n_simulations': 1, 'entries': [], 'note': 'base diagnostic run failed'}
    b = OC.compute_metrics(base, objective, rows)['metrics']
    entries, n = [], 1
    for p in scenario['parameters']:
        if p['id'].startswith(('schedule.', 'initial.C', 'initial.O', 'initial.D')) or p['value'] == 0:
            continue
        vals = {}
        for sign in (+1, -1):
            exp = {**copy.deepcopy(experiment), 'parameter_draw_id': f'sens-{p["id"]}-{sign:+d}',
                   'parameter_overrides': {p['id']: p['value'] * (1 + sign * rel_step)}}
            res, rws = SIM.run_simulation(scenario, exp, run_id='diag')
            n += 1
            vals[sign] = OC.compute_metrics(res, objective, rws)['metrics'] if res['status'] == 'completed' else None
        e = {'parameter_id': p['id'], 'provenance_type': p['provenance_type'],
             'assumption_ids': p['assumption_ids'], 'source_claim_ids': p['source_claim_ids']}
        for m in ('target_cell_yield', 'target_purity'):
            bv = b[m]['value']
            if vals[1] is None or vals[-1] is None or not bv:
                e[f'elasticity_{m}'] = None
            else:
                e[f'elasticity_{m}'] = ((vals[1][m]['value'] - vals[-1][m]['value']) / bv) / (2 * rel_step)
        entries.append(e)
    entries.sort(key=lambda e: -max(abs(e['elasticity_target_cell_yield'] or 0), abs(e['elasticity_target_purity'] or 0)))
    return {'n_simulations': n, 'relative_step': rel_step, 'at_experiment_id': experiment['experiment_id'],
            'entries': entries,
            'semantics': 'Local one-at-a-time elasticity d ln(metric)/d ln(parameter) at the nominal values; not global sensitivity'}


def _round_to(x, res):
    return round(x / res) * res


def recommend_next_experiment(state, scenario, objective, comparison, records, sens):
    """Deterministic next-test policy. Every branch cites runs and offers an alternative."""
    design = state['design']
    budget = state['budget']
    lo, hi, total = _bounds(design)
    res = design['resolution_h']
    model = get_model(scenario['model_id'])
    nominal = {r['run_id']: r for r in records if r['parameter_draw_id'] == 'nominal'}
    run_ids = sorted(nominal)
    synthetic = scenario.get('synthetic_inputs', [])
    remaining = {'runs': budget['runs_remaining'], 'rounds': budget['max_rounds'] - budget['rounds_completed'] - 1}
    drivers = [e['parameter_id'] for e in (sens or {}).get('entries', [])[:3]]

    def evidence_request(reason, inputs, basis, alternatives):
        specs = model.INPUTS
        return {'type': 'request_evidence', 'reason': reason,
                'missing_parameter_ids': sorted({literature_parameter_id(i) for i in inputs}),
                'model_inputs': sorted(inputs),
                'context': {k: scenario['context'].get(k) for k in ('species', 'cell_origin', 'target_cell', 'target_subtype', 'cell_line', 'culture_format')},
                'suggested_queries': sorted({suggested_query(specs[i]) for i in inputs}),
                'numerical_basis': basis, 'supporting_run_ids': run_ids, 'alternatives': alternatives,
                'remaining_budget': remaining}

    if state['scenario_status'] != 'runnable':
        gaps = state['scenario_gaps']
        return evidence_request(
            f'Scenario {scenario["scenario_id"]} is blocked: {len(gaps)} required model inputs are missing or invalid.',
            [g['model_input'] for g in gaps], {'gap_ids': [g['gap_id'] for g in gaps]},
            [{'type': 'request_calibration', 'reason': 'Fit the missing rates to measured trajectories instead of searching literature.'},
             {'type': 'run_experiment', 'reason': 'Only possible in a separately declared synthetic_demo scenario with an explicit assumptions file.'}])
    failed = [rid for rid, r in nominal.items() if r['status'] == 'failed']
    if failed:
        return {'type': 'stop_invalid_model', 'reason': f'Solver/accounting failure in nominal runs {failed}; results cannot be ranked.',
                'numerical_basis': {'failed_run_ids': failed}, 'supporting_run_ids': failed, 'remaining_budget': remaining,
                'alternatives': [{'type': 'request_calibration', 'reason': 'Revise model inputs after inspecting solver diagnostics.'}]}
    best = comparison['best_run_id']
    target = objective.get('target')
    if best and target:
        v = nominal[best]['metrics'][target['metric']]['value']
        if v is not None and OC._OPS[target['op']](v, target['value']):
            return {'type': 'stop_target', 'reason': f'{best} meets target {target["metric"]} {target["op"]} {target["value"]:g} ({v:.4g}) while satisfying all constraints.',
                    'numerical_basis': {'best_run_id': best, target['metric']: v}, 'supporting_run_ids': run_ids,
                    'remaining_budget': remaining,
                    'alternatives': [{'type': 'run_experiment', 'reason': 'Continue refining allocation for higher yield within remaining budget.'},
                                     {'type': 'request_calibration', 'reason': f'Toy-model result; calibrate driving inputs {drivers} before any real-world claim.'}]}
    exhausted = budget['runs_remaining'] <= 0 or remaining['rounds'] <= 0

    def stop_budget():
        return {'type': 'stop_budget', 'reason': f'Budget exhausted ({budget["runs_used"]}/{budget["max_runs"]} runs, {budget["rounds_completed"] + 1}/{budget["max_rounds"]} rounds) before the question was resolved.',
                'numerical_basis': {'best_run_id': best, 'comparison_status': comparison['status']},
                'supporting_run_ids': run_ids, 'remaining_budget': remaining,
                'alternatives': [{'type': 'request_calibration', 'reason': 'Use further effort on calibration data rather than more toy-model runs.'}]}

    tried = sorted({r['stage_durations_h']['ipsc_expansion'] for r in nominal.values()})
    if best:
        e_best = nominal[best]['stage_durations_h']['ipsc_expansion']
        below = [e for e in tried if e < e_best]
        above = [e for e in tried if e > e_best]
        new = []
        for nb in ([below[-1]] if below else [lo]) + ([above[0]] if above else [hi]):
            mid = _round_to((e_best + nb) / 2, res)
            if lo <= mid <= hi and mid not in tried and mid != e_best and mid not in new:
                new.append(mid)
        basis = {'best_run_id': best, 'best_expansion_h': e_best, 'tried_expansion_h': tried,
                 'bracket_h': [below[-1] if below else lo, above[0] if above else hi], 'resolution_h': res,
                 'active_constraints': _active(nominal[best], objective)}
        if not new:
            return {'type': 'request_calibration',
                    'reason': (f'The yield-maximizing feasible allocation is resolved to the {res:g} h design resolution '
                               f'around {e_best:g} h expansion ({best}). More toy-model runs cannot change this conclusion; '
                               f'it is driven by {drivers}, which need calibration against measured cell-count histories.'),
                    'calibration_request': {'model_inputs': drivers,
                                            'measurements_needed': 'Viable cell counts over time per stage and target-cell fraction over differentiation time, same cell line/format'},
                    'numerical_basis': basis, 'supporting_run_ids': run_ids, 'remaining_budget': remaining,
                    'alternatives': [{'type': 'request_evidence', 'reason': f'Search literature for {drivers} instead of measuring.'},
                                     {'type': 'stop_target', 'reason': 'Accept the toy-model allocation as a demonstration result only.'}]}
        if exhausted:
            return stop_budget()
        new = new[:budget['runs_remaining']]
        exps = [allocation_experiment(scenario, design, e) for e in new]
        return {'type': 'run_experiment',
                'reason': (f'Refine the feasible high-yield region: best so far {best} at {e_best:g} h expansion; '
                           f'test midpoints {new} h inside the bracket.'),
                'experiments': exps,
                'expected_learning': f'Narrows the location of the yield optimum from bracket {basis["bracket_h"]} h toward the {res:g} h resolution.',
                'cost_estimate': {'computational_runs': len(exps), 'culture_time_h_per_run': total, 'lab_cost': None},
                'numerical_basis': basis, 'supporting_run_ids': run_ids, 'remaining_budget': remaining,
                'alternatives': [{'type': 'request_evidence', 'reason': f'Conclusion depends on synthetic inputs {[d for d in drivers if d in synthetic]}; obtain evidence first.'}]}

    # Nothing feasible: investigate the active constraint by shifting time toward differentiation.
    viol = {}
    for rid in run_ids:
        for v in comparison['feasibility'][rid]['violations']:
            viol.setdefault(v.get('metric'), []).append(rid)
    basis = {'comparison_status': comparison['status'], 'violated_metrics': viol, 'tried_expansion_h': tried}
    if exhausted:
        return stop_budget()
    if 'target_purity' in viol:
        e_min = tried[0] if tried else hi
        cand = _round_to((lo + e_min) / 2, res)
        if lo <= cand < e_min and cand not in tried:
            exps = [allocation_experiment(scenario, design, cand)]
            return {'type': 'run_experiment',
                    'reason': f'All tested allocations violate the purity constraint; probe more differentiation time ({cand:g} h expansion).',
                    'experiments': exps, 'expected_learning': 'Whether any allocation in the domain can satisfy the purity constraint.',
                    'cost_estimate': {'computational_runs': 1, 'culture_time_h_per_run': total, 'lab_cost': None},
                    'numerical_basis': basis, 'supporting_run_ids': run_ids, 'remaining_budget': remaining,
                    'alternatives': [{'type': 'request_evidence', 'reason': 'Search for differentiation transition rates that determine purity.'}]}
    purity_inputs = [i for i in ('cardiac_differentiation.k_C', 'cardiac_differentiation.k_O',
                                 'cardiac_differentiation.mu_O', 'cardiac_differentiation.mu_R') if i in model.INPUTS]
    return evidence_request(
        'No allocation in the declared domain satisfies the constraints under current inputs; thresholds are not lowered. '
        'The purity-determining differentiation rates need evidence.',
        purity_inputs, basis,
        [{'type': 'request_calibration', 'reason': 'Measure target-cell fraction over differentiation time.'},
         {'type': 'stop_budget', 'reason': 'Report infeasibility and stop.'}])


def _active(record, objective, rel=0.02):
    act = []
    for c in objective.get('constraints', []):
        v = record['metrics'].get(c['metric'], {}).get('value')
        if v is not None and c['value'] and abs(v - c['value']) <= rel * abs(c['value']):
            act.append(c['id'])
    return act


def _interpretation(state, scenario, comparison, records, sens, ens):
    n = len(records)
    done = [r for r in records if r['metrics']]
    lines = [f'{n} runs evaluated ({len(done)} completed, {n - len(done)} blocked/failed); comparison status: {comparison["status"]}.']
    if comparison['best_run_id']:
        b = next(r for r in records if r['run_id'] == comparison['best_run_id'])
        m = b['metrics']
        lines.append(f'Best feasible run {b["run_id"]} ({b["stage_durations_h"]}) gives modeled target yield '
                     f'{m["target_cell_yield"]["value"]:.4g} cells at purity {m["target_purity"]["value"]:.3f}.')
    for c in comparison['paired_comparisons']:
        if c['run_id'] == comparison['best_run_id'] and c['matched_conditions']:
            d = c['target_cell_yield']
            if d.get('absolute_difference') is not None:
                lines.append(f'Relative to baseline {c["baseline_run_id"]}: {d["absolute_difference"]:+.4g} cells '
                             f'(ratio {"undefined" if d["ratio"] is None else f"{d["ratio"]:.3f}"}).')
    if sens and sens.get('entries'):
        top = sens['entries'][:3]
        lines.append('Conclusion is most sensitive to: ' + ', '.join(
            f'{e["parameter_id"]} [{e["provenance_type"]}] (yield elasticity {e["elasticity_target_cell_yield"]:.2f}, purity elasticity {e["elasticity_target_purity"]:.2f})'
            for e in top if e['elasticity_target_cell_yield'] is not None and e['elasticity_target_purity'] is not None) + '.')
    if ens and ens.get('n_draws'):
        py = ens['paired_differences']['target_cell_yield']
        if py.get('n'):
            lines.append(f'Across {ens["n_draws"]} paired synthetic draws the best-vs-baseline yield difference ranged '
                         f'{py["min"]:+.3g} to {py["max"]:+.3g} cells (positive in {py["fraction_positive"]:.0%}); '
                         'this is an assumption-sensitivity interval, not a confidence interval.')
    if scenario['mode'] == 'synthetic_demo':
        lines.append(f'These numbers come from synthetic assumptions ({len(scenario.get("synthetic_inputs", []))} inputs); '
                     'they demonstrate the workflow and the toy model, not real cardiomyocyte production.')
    if scenario.get('unsupported_controls'):
        lines.append('No reagent response was investigated; stored as unsupported controls: ' + ', '.join(
            sorted({u['parameter'] for u in scenario['unsupported_controls']})) + '.')
    return ' '.join(lines)


def evaluate_campaign(campaign_dir):
    """Write an immutable OutcomeEvaluation with exactly one typed next action."""
    campaign_dir = Path(campaign_dir)
    state = _load(campaign_dir)
    inp = _inputs(campaign_dir)
    scenario, objective = inp['scenario'], inp['objective']
    records = _records(state, objective)
    pending = [r['run_id'] for r in state['runs'] if r['status'] == 'pending']
    if pending:
        raise K.ContractError(f'runs still pending: {pending}')
    baseline_id = next((r['run_id'] for r in state['runs'] if r['experiment_id'] == inp['baseline']['experiment_id']
                        and r['parameter_draw_id'] == 'nominal'), None)
    comparison = OC.compare_results(records, baseline_id, objective) if records else {
        'status': 'no_valid_runs', 'ranking_rule': 'n/a', 'feasibility': {}, 'ranking': [], 'best_run_id': None,
        'paired_comparisons': [], 'pareto_table': []}
    sens, ens = None, None
    best_id = comparison['best_run_id']
    if best_id and state['scenario_status'] == 'runnable':
        best_exp = K.read_json(campaign_dir / 'runs' / best_id / 'experiment.json')
        sens = sensitivity(scenario, best_exp, objective)
        state['diagnostic_simulations'] += sens['n_simulations']
        if 'uncertainty' in inp and baseline_id:
            base_exp = K.read_json(campaign_dir / 'runs' / baseline_id / 'experiment.json')
            pairs = []
            for (cr, crow), (br, brow) in zip(SIM.run_ensemble(scenario, best_exp, inp['uncertainty'], run_id_prefix='diag-cand'),
                                              SIM.run_ensemble(scenario, base_exp, inp['uncertainty'], run_id_prefix='diag-base')):
                pairs.append((OC.compute_metrics(cr, objective, crow), OC.compute_metrics(br, objective, brow)))
            ens = OC.summarize_ensemble(pairs, objective, inp['uncertainty'])
            state['diagnostic_simulations'] += 2 * len(pairs)
    action = recommend_next_experiment(state, scenario, objective, comparison, records, sens)
    action['evidence_refs'] = {'claim_ids': sorted({c for p in scenario['parameters'] for c in p['source_claim_ids']}),
                               'assumption_ids': sorted({a for p in scenario['parameters'] for a in p['assumption_ids']})}
    K.require_valid('next_action', action)
    eid = f'eval-{len(state["evaluations"]):02d}'
    evaluation = {
        'schema_version': K.EVALUATION_VERSION,
        'evaluation_id': eid,
        'campaign_id': state['campaign_id'],
        'created_at': K.now_iso(),
        'mode': scenario['mode'],
        'label': scenario['label'],
        'scenario_id': scenario['scenario_id'],
        'objective_id': objective['objective_id'],
        'objective': objective,
        'constraint_ids': [c['id'] for c in objective.get('constraints', [])],
        'result_ids': [r['run_id'] for r in records],
        'baseline_run_id': baseline_id,
        'result_sha256': {r['run_id']: K.sha256_file(r['result_path']) for r in state['runs'] if r['result_path']},
        'run_metrics': records,
        'comparison': comparison,
        'sensitivity': sens,
        'uncertainty': ens or {'n_draws': 0, 'semantics': 'No ensemble declared: deterministic nominal runs only; no uncertainty quantified.'},
        'scientific_interpretation': _interpretation(state, scenario, comparison, records, sens, ens),
        'claim_limitations': [
            'compartment_growth_v1 is an uncalibrated phenomenological model; numerical correctness is not biological validity.',
            'Target purity is a modeled compartment fraction, not marker positivity, subtype purity, maturity or potency.',
            'Reagent settings (e.g. CHIR99021) are unsupported controls; no dose-response was simulated.',
            'An improvement over one baseline in a toy model is not evidence of accelerated discovery or better manufacturing.',
        ] + (['All kinetic rates are synthetic assumptions in this campaign.'] if scenario['mode'] == 'synthetic_demo' else []),
        'budget': {**state['budget'], 'diagnostic_simulations': state['diagnostic_simulations']},
        'next_action': action,
    }
    K.require_valid('evaluation', evaluation)
    path = campaign_dir / 'evaluations' / f'{eid}.json'
    K.write_json_atomic(path, evaluation, overwrite=False)
    K.write_text_atomic(campaign_dir / 'evaluations' / f'{eid}.md', OC.write_outcome_report(evaluation), overwrite=False)
    state = _load(campaign_dir)
    state['diagnostic_simulations'] = evaluation['budget']['diagnostic_simulations']
    state['budget']['rounds_completed'] += 1
    state['evaluations'].append({'evaluation_id': eid, 'path': str(path.as_posix()), 'next_action_type': action['type']})
    if action['type'].startswith('stop_'):
        state['stopped'] = {'type': action['type'], 'at': K.now_iso(), 'evaluation_id': eid}
    _save(campaign_dir, state)
    return evaluation


def check_action(campaign_dir, action):
    """Coordinator-side validation of a next action before dispatch."""
    errors = K.schema_errors('next_action', action)
    if errors or action['type'] != 'run_experiment':
        return errors
    state = _load(campaign_dir)
    inp = _inputs(campaign_dir)
    if len(action['experiments']) > state['budget']['runs_remaining']:
        errors.append('more experiments than remaining run budget')
    for e in action['experiments']:
        errors += [f'{e.get("experiment_id")}: {x}' for x in SIM.validate_experiment(
            inp['scenario'], e, max_total_h=state['design']['total_culture_time_h'], min_stage_h=state['design']['min_stage_h'])]
    return errors


def run_campaign(config, campaign_dir, config_dir='.', max_iterations=20):
    """Fixed pipeline for tests/offline demo. The live demo uses agents for each step."""
    state = init_campaign(config, campaign_dir, config_dir)
    for _ in range(max_iterations):
        prop = propose_experiments(campaign_dir)
        for e in prop['experiments']:
            run_experiment(campaign_dir, e)
        ev = evaluate_campaign(campaign_dir)
        if ev['next_action']['type'] != 'run_experiment':
            break
    return _load(campaign_dir)
