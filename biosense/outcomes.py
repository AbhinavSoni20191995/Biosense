"""Outcome metrics, constraint feasibility and matched comparisons.

All values come from numerical results. Undefined metrics stay null with a
flag; failed/blocked runs are accounted for but never ranked as successes.
"""
import statistics

from . import contracts as K
from .simulation import read_trajectory

METRIC_DEFS = {
    'total_viable_cells': ('cells', 'R + C + O at harvest', 'Does not identify useful target output'),
    'target_cell_yield': ('cells', 'C at harvest (end of schedule)', 'A modeled compartment count, not a measured marker count'),
    'target_purity': ('fraction', 'C / (R + C + O) at harvest', 'Not automatically measured marker positivity, subtype purity or potency'),
    'viability_proxy': ('fraction', '(R + C + O) / (R + C + O + D) at harvest', 'Toy metric; assumes dead cells remain in the accounting'),
    'target_yield_per_initial_cell': ('dimensionless', 'C at harvest / (R0 + C0 + O0)', 'Requires a nonzero starting population'),
    'time_to_target_h': ('h', 'First trajectory time at which the target and trajectory-evaluable constraints all hold', 'Censored at simulation end; never extrapolated'),
    'cells_entering_differentiation': ('cells', 'R + C + O at the end of ipsc_expansion', 'Diagnostic intermediate state'),
    'culture_time_h': ('h', 'Sum of stage durations', 'Computational schedule length, not lab time'),
    'resource_cost': (None, 'Supplied medium/reagent cost model', 'No cost model supplied: unknown'),
}
_TRAJECTORY_METRICS = {'target_cell_yield', 'target_purity', 'total_viable_cells', 'viability_proxy'}
_OPS = {'>=': lambda a, b: a >= b, '<=': lambda a, b: a <= b}


def _point_metrics(R, C, O, D):
    N = R + C + O
    return {'target_cell_yield': C, 'total_viable_cells': N,
            'target_purity': C / N if N > 0 else None,
            'viability_proxy': N / (N + D) if N + D > 0 else None}


def _metric(name, value, flag=None):
    unit, definition, limitation = METRIC_DEFS[name]
    m = {'value': value, 'unit': unit, 'definition': definition, 'limitation': limitation}
    if flag:
        m['flag'] = flag
    return m


def _conditions(objective):
    conds = [c for c in objective.get('constraints', []) if c['metric'] in _TRAJECTORY_METRICS]
    if objective.get('target'):
        conds = conds + [objective['target']]
    return conds


def compute_metrics(result, objective, rows=None):
    """Metrics for one result. Non-completed results get metrics=None."""
    base = {'run_id': result['run_id'], 'experiment_id': result['experiment_id'],
            'parameter_draw_id': result['parameter_draw_id'], 'status': result['status'],
            'stage_durations_h': {s['stage']: s['duration_h'] for s in result['stage_schedule']}}
    if result['status'] != 'completed':
        return {**base, 'metrics': None, 'reason': f'run {result["status"]}: ' + '; '.join(result.get('errors', [])[:3])}
    if rows is None:
        rows = read_trajectory(result['trajectory']['path'])
    end = result['endpoint_state']
    pm = _point_metrics(end['R'], end['C'], end['O'], end['D'])
    init = result['initial_state']
    n0 = init['R'] + init['C'] + init['O']
    exp_end = [r for r in rows if r['stage'] == 'ipsc_expansion']
    metrics = {
        'total_viable_cells': _metric('total_viable_cells', pm['total_viable_cells']),
        'target_cell_yield': _metric('target_cell_yield', pm['target_cell_yield']),
        'target_purity': _metric('target_purity', pm['target_purity'],
                                 None if pm['target_purity'] is not None else 'undefined: zero viable cells'),
        'viability_proxy': _metric('viability_proxy', pm['viability_proxy'],
                                   None if pm['viability_proxy'] is not None else 'undefined: zero total cells'),
        'target_yield_per_initial_cell': _metric('target_yield_per_initial_cell', end['C'] / n0 if n0 > 0 else None,
                                                 None if n0 > 0 else 'undefined: zero initial viable cells'),
        'cells_entering_differentiation': _metric('cells_entering_differentiation',
                                                  (exp_end[-1]['R'] + exp_end[-1]['C'] + exp_end[-1]['O']) if exp_end else None),
        'culture_time_h': _metric('culture_time_h', result['cost_accounting']['culture_time_h']),
        'resource_cost': _metric('resource_cost', None, 'unknown: no cost model supplied'),
    }
    conds = _conditions(objective)
    hit = None
    if objective.get('target'):
        for r in rows:
            p = _point_metrics(r['R'], r['C'], r['O'], r['D'])
            if all(p[c['metric']] is not None and _OPS[c['op']](p[c['metric']], c['value']) for c in conds):
                hit = r['t_h']
                break
        metrics['time_to_target_h'] = _metric('time_to_target_h', hit, None if hit is not None else 'censored: target not reached by simulation end')
    else:
        metrics['time_to_target_h'] = _metric('time_to_target_h', None, 'no target declared')
    return {**base, 'metrics': metrics}


def feasibility(record, objective):
    """Apply every constraint before ranking. Undefined metrics are violations."""
    if record['metrics'] is None:
        return {'feasible': False, 'violations': [{'constraint_id': None, 'reason': record['reason']}]}
    violations = []
    for c in objective.get('constraints', []):
        v = record['metrics'].get(c['metric'], {}).get('value')
        if v is None:
            violations.append({'constraint_id': c['id'], 'metric': c['metric'], 'reason': 'metric undefined'})
        elif not _OPS[c['op']](v, c['value']):
            violations.append({'constraint_id': c['id'], 'metric': c['metric'], 'value': v,
                               'threshold': c['value'], 'op': c['op'], 'reason': 'violated'})
    return {'feasible': not violations, 'violations': violations}


def _value(record, metric):
    return record['metrics'][metric]['value'] if record['metrics'] else None


def compare_results(records, baseline_run_id, objective):
    """Feasibility-first ranking, matched comparisons to baseline and a Pareto table."""
    key = objective['maximize']
    feas = {r['run_id']: feasibility(r, objective) for r in records}
    feasible = [r for r in records if feas[r['run_id']]['feasible'] and _value(r, key) is not None]
    ranking = sorted(feasible, key=lambda r: (-_value(r, key), r['run_id']))
    base = next((r for r in records if r['run_id'] == baseline_run_id), None)
    comparisons = []
    for r in records:
        if base is None or r['run_id'] == baseline_run_id:
            continue
        matched = (r['parameter_draw_id'] == base['parameter_draw_id']
                   and sum(r['stage_durations_h'].values()) == sum(base['stage_durations_h'].values()))
        entry = {'run_id': r['run_id'], 'baseline_run_id': baseline_run_id, 'matched_conditions': matched,
                 'matching': 'same scenario, initial cells, parameter draw and total culture time' if matched
                             else 'NOT matched: different draw or total time; comparison is not interpretable'}
        for m in ('target_cell_yield', 'target_purity', 'total_viable_cells'):
            a, b = _value(r, m), _value(base, m)
            if a is None or b is None:
                entry[m] = {'absolute_difference': None, 'ratio': None, 'flag': 'undefined: missing metric'}
                continue
            entry[m] = {'candidate': a, 'baseline': b, 'absolute_difference': a - b,
                        'ratio': a / b if b != 0 else None}
            if b == 0:
                entry[m]['flag'] = 'zero baseline: ratio undefined; absolute difference only'
        comparisons.append(entry)
    completed = [r for r in records if r['metrics'] and _value(r, 'target_purity') is not None]
    pareto = []
    for r in completed:
        y, p = _value(r, 'target_cell_yield'), _value(r, 'target_purity')
        dominated = any((_value(o, 'target_cell_yield') >= y and _value(o, 'target_purity') >= p)
                        and (_value(o, 'target_cell_yield') > y or _value(o, 'target_purity') > p) for o in completed)
        pareto.append({'run_id': r['run_id'], 'stage_durations_h': r['stage_durations_h'], 'target_cell_yield': y,
                       'target_purity': p, 'feasible': feas[r['run_id']]['feasible'], 'pareto_optimal': not dominated})
    if not any(r['metrics'] for r in records):
        status = 'no_valid_runs'
    elif ranking:
        status = 'feasible_found'
    else:
        status = 'infeasible'
    return {
        'status': status,
        'ranking_rule': f'Discard runs that are not completed or violate any constraint; then sort by {key} descending (ties by run_id). No weighted composite score.',
        'feasibility': feas,
        'ranking': [{'rank': i + 1, 'run_id': r['run_id'], key: _value(r, key),
                     'stage_durations_h': r['stage_durations_h']} for i, r in enumerate(ranking)],
        'best_run_id': ranking[0]['run_id'] if ranking else None,
        'paired_comparisons': comparisons,
        'pareto_table': sorted(pareto, key=lambda x: x['run_id']),
    }


def summarize_ensemble(paired, objective, spec):
    """paired: list of (candidate_record, baseline_record) on identical draws."""
    if not paired:
        return {'n_draws': 0, 'semantics': 'no ensemble evaluated'}
    for c, b in paired:
        if c['parameter_draw_id'] != b['parameter_draw_id']:
            raise K.ContractError('ensemble comparison requires identical draws')
    out = {'ensemble_id': spec['ensemble_id'], 'n_draws': len(paired), 'seed': spec['seed'],
           'parameters': spec['parameters'], 'correlation': spec.get('correlation', 'independent draws (modeling choice; correlations unknown)'),
           'semantics': ('Assumption-sensitivity interval over the declared (synthetic) ranges; '
                         'NOT a confidence interval or calibrated probability of success'),
           'paired_differences': {}}
    for m in ('target_cell_yield', 'target_purity'):
        diffs = [_value(c, m) - _value(b, m) for c, b in paired if _value(c, m) is not None and _value(b, m) is not None]
        out['paired_differences'][m] = ({'n': len(diffs), 'min': min(diffs), 'median': statistics.median(diffs),
                                         'max': max(diffs), 'fraction_positive': sum(d > 0 for d in diffs) / len(diffs)}
                                        if diffs else {'n': 0})
    out['fraction_feasible'] = {
        'candidate': sum(feasibility(c, objective)['feasible'] for c, _ in paired) / len(paired),
        'baseline': sum(feasibility(b, objective)['feasible'] for _, b in paired) / len(paired)}
    out['failed_draws'] = [c['run_id'] for c, b in paired if c['metrics'] is None] + \
                          [b['run_id'] for c, b in paired if b['metrics'] is None]
    return out


def write_outcome_report(evaluation):
    """Human-readable Markdown rendering of an OutcomeEvaluation (numbers copied, not re-derived)."""
    na = evaluation['next_action']
    lines = [f'# Outcome evaluation {evaluation["evaluation_id"]}', '',
             f'**Mode:** {evaluation["mode"]} — {evaluation["label"]}', '',
             f'Objective `{evaluation["objective_id"]}`: maximize `{evaluation["objective"]["maximize"]}`; '
             f'baseline run `{evaluation["baseline_run_id"]}`.', '',
             f'Comparison status: **{evaluation["comparison"]["status"]}**', '',
             '| run | expansion h | differentiation h | status | target yield (cells) | purity | feasible |',
             '|---|---|---|---|---|---|---|']
    feas = evaluation['comparison']['feasibility']
    for r in evaluation['run_metrics']:
        y = _value(r, 'target_cell_yield')
        p = _value(r, 'target_purity')
        d = r['stage_durations_h']
        lines.append(f'| {r["run_id"]} | {d.get("ipsc_expansion", "")} | {d.get("cardiac_differentiation", "")} | '
                     f'{r["status"]} | {"" if y is None else f"{y:.4g}"} | {"" if p is None else f"{p:.3f}"} | '
                     f'{feas[r["run_id"]]["feasible"]} |')
    lines += ['', '## Interpretation', '', evaluation['scientific_interpretation'], '',
              '## Next action', '', f'**{na["type"]}** — {na["reason"]}', '']
    if na.get('experiments'):
        for e in na['experiments']:
            lines.append(f'- `{e["experiment_id"]}`: {e["stage_durations_h"]}')
    if na.get('missing_parameter_ids'):
        lines.append('Missing parameters: ' + ', '.join(na['missing_parameter_ids']))
    lines += ['', 'Alternatives considered:'] + [f'- {a["type"]}: {a["reason"]}' for a in na['alternatives']]
    lines += ['', '## Limitations', ''] + [f'- {x}' for x in evaluation['claim_limitations']]
    return '\n'.join(lines) + '\n'
