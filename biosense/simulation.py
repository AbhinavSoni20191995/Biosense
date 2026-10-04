"""Scenario/experiment validation and deterministic staged ODE execution."""
import copy
import csv
import io
import math
from pathlib import Path

import numpy as np
from scipy.integrate import solve_ivp

from agent_tools import convert_unit

from . import contracts as K
from .models import get_model

SOLVER_DEFAULT = {'method': 'LSODA', 'rtol': 1e-8, 'atol': 1e-6, 'output_interval_h': 1.0}
SOLVER_TIGHT = {'method': 'LSODA', 'rtol': 1e-11, 'atol': 1e-9, 'output_interval_h': 1.0}
CONSERVATION_REL_TOL = 1e-6
CONVERGENCE_REL_TOL = 1e-5


def _param_map(scenario):
    return {p['id']: p for p in scenario.get('parameters', [])}


def validate_scenario(scenario):
    """Return {'status': runnable|blocked|invalid, 'errors': [...], 'gaps': [...], 'warnings': [...]}."""
    errors = list(K.schema_errors('scenario', scenario))
    warnings = []
    if errors:
        return {'status': 'invalid', 'errors': errors, 'gaps': [], 'warnings': warnings}
    try:
        model = get_model(scenario['model_id'])
    except ValueError as e:
        return {'status': 'invalid', 'errors': [str(e)], 'gaps': [], 'warnings': warnings}
    gaps = list(scenario.get('gaps', []))
    errors += scenario.get('errors', [])
    params = _param_map(scenario)
    if len(params) != len(scenario['parameters']):
        errors.append('duplicate parameter IDs')
    for pid, p in params.items():
        spec = model.INPUTS.get(pid)
        if spec is None:
            errors.append(f'unknown parameter {pid!r} is refused')
            continue
        v = p['value']
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            errors.append(f'{pid}: value must be a finite number')
            continue
        if p['unit'] != spec['unit']:
            errors.append(f'{pid}: unit {p["unit"]!r} must be normalized to {spec["unit"]!r}')
        if 'min' in spec and v < spec['min']:
            errors.append(f'{pid}: {v} below minimum {spec["min"]}')
        if 'min_exclusive' in spec and v <= spec['min_exclusive']:
            errors.append(f'{pid}: {v} must exceed {spec["min_exclusive"]}')
        pt = p['provenance_type']
        if pt == 'synthetic_assumption' and scenario['mode'] == 'evidence_based':
            errors.append(f'{pid}: synthetic assumption in evidence_based scenario')
        if pt == 'reported' and (not p['source_claim_ids'] or p['assumption_ids']):
            errors.append(f'{pid}: reported input must cite claim IDs only')
        if pt == 'synthetic_assumption' and (p['source_claim_ids'] or not p['assumption_ids']):
            errors.append(f'{pid}: synthetic assumption must cite assumption IDs and no evidence claims')
        if pt == 'derived' and not p.get('transformation'):
            errors.append(f'{pid}: derived input lacks transformation/parents')
        if p.get('depends_on_synthetic') and scenario['mode'] == 'evidence_based':
            errors.append(f'{pid}: depends on synthetic values in evidence_based scenario')
    missing = sorted(set(model.INPUTS) - set(params))
    if missing and not gaps:
        errors.append(f'missing required inputs without gap records: {missing}')
    if not missing and not gaps:
        n0 = sum(params[f'initial.{s}']['value'] for s in ('R', 'C', 'O'))
        if n0 <= 0:
            errors.append('initial viable population R0+C0+O0 must be positive')
        if n0 > params['global.K']['value']:
            warnings.append('initial viable population exceeds capacity K; growth starts fully suppressed')
        for row in scenario['stage_schedule']:
            if row['duration'] != params[row['input_id']]['value']:
                errors.append(f'stage_schedule {row["stage"]} disagrees with {row["input_id"]}')
        for s, v in scenario['initial_state'].items():
            if v != params[f'initial.{s}']['value']:
                errors.append(f'initial_state.{s} disagrees with parameter manifest')
    if [r['stage'] for r in scenario['stage_schedule']] != list(model.STAGES):
        errors.append(f'stage_schedule must follow model stage order {list(model.STAGES)}')
    for u in scenario.get('unsupported_controls', []):
        if u.get('conflict_status') == 'unresolved':
            warnings.append(f'unsupported control {u["claim_id"]} ({u["parameter"]}) has an unresolved source '
                            'conflict; it is not consumed by the model and no reagent response is simulated')
    if errors:
        status = 'invalid'
    elif gaps or missing or not scenario['validation']['numerically_complete']:
        status = 'blocked'
    else:
        status = 'runnable'
    return {'status': status, 'errors': errors, 'gaps': gaps, 'warnings': warnings}


def validate_experiment(scenario, experiment, *, max_total_h=None, min_stage_h=None):
    errors = list(K.schema_errors('experiment', experiment))
    if errors:
        return errors
    model = get_model(scenario['model_id'])
    if experiment['scenario_id'] != scenario['scenario_id']:
        errors.append('experiment scenario_id does not match scenario')
    durs = experiment['stage_durations_h']
    if set(durs) != set(model.STAGES):
        errors.append(f'stage_durations_h must define exactly {list(model.STAGES)}')
    for st, d in durs.items():
        if not isinstance(d, (int, float)) or isinstance(d, bool) or not math.isfinite(d) or d <= 0:
            errors.append(f'{st}: duration must be a positive finite number of hours')
        elif min_stage_h and d < min_stage_h.get(st, 0):
            errors.append(f'{st}: {d} h below minimum {min_stage_h[st]} h')
    total = sum(d for d in durs.values() if isinstance(d, (int, float)))
    if max_total_h is not None and total > max_total_h + 1e-9:
        errors.append(f'total culture time {total} h exceeds budget {max_total_h} h')
    params = _param_map(scenario)
    for pid, v in experiment.get('parameter_overrides', {}).items():
        if pid not in params:
            errors.append(f'override of unknown parameter {pid!r} refused')
        elif pid.startswith('schedule.'):
            errors.append(f'{pid}: schedules are set by stage_durations_h, not overrides')
        elif not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            errors.append(f'{pid}: override must be finite and nonnegative')
    if experiment.get('parameter_overrides') and experiment.get('parameter_draw_id', 'nominal') == 'nominal':
        errors.append('parameter overrides require a non-nominal parameter_draw_id')
    return errors


def _effective_params(scenario, experiment):
    values = {pid: p['value'] for pid, p in _param_map(scenario).items()}
    values.update(experiment.get('parameter_overrides', {}))
    return values


def _evidence_refs(scenario):
    claims = sorted({c for p in scenario['parameters'] for c in p['source_claim_ids']})
    assumptions = sorted({a for p in scenario['parameters'] for a in p['assumption_ids']})
    return {'claim_ids': claims, 'assumption_ids': assumptions}


def _base_result(scenario, experiment, run_id, solver):
    return {
        'schema_version': K.RESULT_VERSION,
        'run_id': run_id,
        'scenario_id': scenario.get('scenario_id'),
        'experiment_id': experiment.get('experiment_id'),
        'mode': scenario.get('mode'),
        'label': scenario.get('label'),
        'status': None,
        'created_at': K.now_iso(),
        'model_id': scenario.get('model_id'),
        'parameter_draw_id': experiment.get('parameter_draw_id', 'nominal'),
        'stage_schedule': [{'stage': s, 'duration_h': d} for s, d in experiment.get('stage_durations_h', {}).items()],
        'hashes': {
            'scenario_sha256': K.sha256_obj(scenario),
            'experiment_sha256': K.sha256_obj(experiment),
            'code_sha256': K.code_sha256(),
            'code_revision': K.code_revision(),
            'solver_sha256': K.sha256_obj(solver),
            'upstream': scenario.get('upstream'),
        },
        'solver': {**solver},
        'evidence_refs': _evidence_refs(scenario) if scenario.get('parameters') else {'claim_ids': [], 'assumption_ids': []},
        'unsupported_controls': [u['claim_id'] for u in scenario.get('unsupported_controls', [])],
        'cost_accounting': {
            'computational_runs': 1,
            'culture_time_h': sum(experiment.get('stage_durations_h', {}).values()),
            'medium_reagent_cost': None,
            'cost_note': 'No medium/reagent cost model supplied; currency cost is unknown.',
        },
        'warnings': [],
        'errors': [],
        'parameter_manifest': [],
        'event_log': [],
        'solver_checks': None,
        'trajectory': None,
        'endpoint_state': None,
    }


def run_simulation(scenario, experiment, *, run_id='run-adhoc', solver=None, max_total_h=None, min_stage_h=None):
    """Execute one experiment. Returns (result, trajectory_rows).

    Blocked/invalid inputs and solver failures return a result without metrics
    or an endpoint state; nothing is clipped to hide a broken model.
    """
    solver = {**SOLVER_DEFAULT, **(solver or {})}
    result = _base_result(scenario, experiment, run_id, solver)
    check = validate_scenario(scenario)
    result['warnings'] += check['warnings']
    if check['status'] != 'runnable':
        result['status'] = 'blocked'
        result['errors'] = check['errors'] + [f'gap {g["gap_id"]}: {g["model_input"]} ({g["reason"]})' for g in check['gaps']]
        result['blocked_gap_ids'] = [g['gap_id'] for g in check['gaps']]
        return result, []
    exp_errors = validate_experiment(scenario, experiment, max_total_h=max_total_h, min_stage_h=min_stage_h)
    if exp_errors:
        result['status'] = 'blocked'
        result['errors'] = exp_errors
        return result, []

    model = get_model(scenario['model_id'])
    values = _effective_params(scenario, experiment)
    overridden = experiment.get('parameter_overrides', {})
    for p in scenario['parameters']:
        if p['id'].startswith('schedule.'):
            continue
        result['parameter_manifest'].append({
            'id': p['id'], 'value': values[p['id']], 'unit': p['unit'], 'provenance_type': p['provenance_type'],
            'source_claim_ids': p['source_claim_ids'], 'assumption_ids': p['assumption_ids'],
            'overridden_by_draw': result['parameter_draw_id'] if p['id'] in overridden else None})

    cap = float(values['global.K'])
    y = np.array([values[f'initial.{s}'] for s in model.STATE] + [0.0], dtype=float)
    y0_total = float(y[:4].sum())
    neg_tol = 10 * solver['atol'] + 1e-9 * max(cap, y0_total)
    t0 = 0.0
    rows, nfev, max_correction, max_residual = [], 0, 0.0, 0.0
    stage_diag = []
    for stage in model.STAGES:
        dur = float(experiment['stage_durations_h'][stage])
        rates = model.stage_rates(values, stage)
        t1 = t0 + dur
        n_out = max(2, int(round(dur / solver['output_interval_h'])) + 1)
        t_eval = np.linspace(t0, t1, n_out)
        result['event_log'].append({'t_h': t0, 'event': 'stage_start', 'stage': stage,
                                    'state_before': dict(zip(model.STATE, y[:4].tolist())),
                                    'state_after': dict(zip(model.STATE, y[:4].tolist())), 'state_changed': False,
                                    'note': 'No automatic conversion at the boundary; state carried forward'})
        try:
            sol = solve_ivp(model.rhs_factory(rates, cap), (t0, t1), y, method=solver['method'],
                            rtol=solver['rtol'], atol=solver['atol'], t_eval=t_eval)
        except Exception as e:  # solver internals raised; record as failure
            result['status'] = 'failed'
            result['errors'].append(f'{stage}: solver raised {type(e).__name__}: {e}')
            return result, []
        nfev += int(sol.nfev)
        diag = {'stage': stage, 'success': bool(sol.success), 'message': sol.message, 'nfev': int(sol.nfev),
                'njev': int(getattr(sol, 'njev', 0) or 0), 't_end_reached': float(sol.t[-1]) if sol.t.size else None}
        stage_diag.append(diag)
        if not sol.success or sol.t.size == 0 or not math.isclose(sol.t[-1], t1, rel_tol=1e-12, abs_tol=1e-9):
            result['status'] = 'failed'
            result['errors'].append(f'{stage}: solver did not complete the requested interval ({sol.message})')
            break
        Y = sol.y
        if not np.all(np.isfinite(Y)):
            result['status'] = 'failed'
            result['errors'].append(f'{stage}: nonfinite state')
            break
        min_state = float(Y[:4].min())
        if min_state < -neg_tol:
            result['status'] = 'failed'
            result['errors'].append(f'{stage}: materially negative state {min_state:.3g} cells (tolerance {neg_tol:.3g})')
            break
        if min_state < 0:
            max_correction = max(max_correction, -min_state)
            Y[:4] = np.maximum(Y[:4], 0.0)
        residual = np.abs(Y[:4].sum(axis=0) - y0_total - Y[4]) / max(1.0, float(np.max(Y[:4].sum(axis=0))))
        max_residual = max(max_residual, float(residual.max()))
        for i, t in enumerate(sol.t):
            if rows and i == 0:
                continue  # stage-start sample duplicates previous stage end (state unchanged)
            R, C, O, D = (float(v) for v in Y[:4, i])
            rows.append({'t_h': float(t), 'stage': stage, 'R': R, 'C': C, 'O': O, 'D': D})
        y = Y[:, -1].copy()
        result['event_log'].append({'t_h': t1, 'event': 'stage_end', 'stage': stage,
                                    'state_before': dict(zip(model.STATE, y[:4].tolist())),
                                    'state_after': dict(zip(model.STATE, y[:4].tolist())), 'state_changed': False})
        t0 = t1
    result['solver_checks'] = {
        'stages': stage_diag, 'total_nfev': nfev,
        'negative_tolerance_cells': neg_tol, 'max_negative_correction_cells': max_correction,
        'conservation_relative_residual_max': max_residual, 'conservation_tolerance': CONSERVATION_REL_TOL,
        'conservation_ok': max_residual <= CONSERVATION_REL_TOL,
    }
    if result['status'] is None and max_residual > CONSERVATION_REL_TOL:
        result['status'] = 'failed'
        result['errors'].append(f'conservation residual {max_residual:.3g} exceeds tolerance')
    if result['status'] is None:
        result['status'] = 'completed'
        result['endpoint_state'] = dict(zip(model.STATE, y[:4].tolist()))
        result['initial_state'] = {s: float(values[f'initial.{s}']) for s in model.STATE}
        result['trajectory'] = {'columns': {'t_h': 'h', 'stage': 'text', 'R': 'cells', 'C': 'cells',
                                            'O': 'cells', 'D': 'cells'},
                                'n_rows': len(rows), 'path': None,
                                'note': 'R, C, O, D are modeled latent compartments, not measurements'}
    else:
        rows = []
    return result, rows


def trajectory_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=['t_h', 'stage', 'R', 'C', 'O', 'D'], lineterminator='\n')
    w.writeheader()
    for r in rows:
        w.writerow({k: (repr(v) if isinstance(v, float) else v) for k, v in r.items()})
    return buf.getvalue()


def read_trajectory(path):
    with open(path, newline='') as f:
        return [{'t_h': float(r['t_h']), 'stage': r['stage'], **{k: float(r[k]) for k in 'RCOD'}}
                for r in csv.DictReader(f)]


def save_run(result, rows, out_dir):
    """Persist an immutable run: result.json (+ trajectory.csv when completed)."""
    out = Path(out_dir)
    result = copy.deepcopy(result)
    if rows:
        K.write_text_atomic(out / 'trajectory.csv', trajectory_csv(rows), overwrite=False)
        result['trajectory']['path'] = str((out / 'trajectory.csv').as_posix())
        result['trajectory']['sha256'] = K.sha256_file(out / 'trajectory.csv')
    K.require_valid('result', result)
    K.write_json_atomic(out / 'result.json', result, overwrite=False)
    return result


def generate_draws(scenario, spec):
    """Deterministic, seeded parameter draws for a declared uncertainty spec."""
    params = _param_map(scenario)
    if not isinstance(spec.get('seed'), int) or spec.get('n_draws', 0) < 1:
        raise K.ContractError('uncertainty spec needs an integer seed and n_draws >= 1')
    if scenario['mode'] == 'synthetic_demo' and 'synthetic' not in spec.get('label', '').lower():
        raise K.ContractError('ranges in a synthetic_demo ensemble must be labelled synthetic')
    rng = np.random.default_rng(spec['seed'])
    names = sorted(spec['parameters'])
    for pid in names:
        if pid not in params or pid.startswith('schedule.'):
            raise K.ContractError(f'uncertain parameter {pid!r} is not a scenario rate/state input')
    draws = []
    for i in range(spec['n_draws']):
        overrides = {}
        for pid in names:
            d = spec['parameters'][pid]
            lo = convert_unit(d['low'], d['unit'], params[pid]['unit']) if params[pid]['unit'] != 'cells' else d['low']
            hi = convert_unit(d['high'], d['unit'], params[pid]['unit']) if params[pid]['unit'] != 'cells' else d['high']
            if not 0 <= lo <= hi:
                raise K.ContractError(f'{pid}: need 0 <= low <= high')
            if d['distribution'] == 'uniform':
                overrides[pid] = float(rng.uniform(lo, hi))
            elif d['distribution'] == 'loguniform' and lo > 0:
                overrides[pid] = float(math.exp(rng.uniform(math.log(lo), math.log(hi))))
            else:
                raise K.ContractError(f'{pid}: unsupported distribution {d["distribution"]!r}')
        draws.append({'draw_id': f'{spec["ensemble_id"]}-d{i:03d}', 'overrides': overrides})
    return draws


def run_ensemble(scenario, experiment, spec, *, run_id_prefix='ens', **kw):
    """Run one experiment over every declared draw (same draws for every experiment)."""
    out = []
    for d in generate_draws(scenario, spec):
        exp = {**copy.deepcopy(experiment), 'parameter_draw_id': d['draw_id'], 'parameter_overrides': d['overrides']}
        out.append(run_simulation(scenario, exp, run_id=f'{run_id_prefix}-{d["draw_id"]}', **kw))
    return out


def convergence_check(scenario, experiment):
    """Numerical verification outside any campaign budget: tighten tolerances."""
    a, _ = run_simulation(scenario, experiment, run_id='verify-default', solver=SOLVER_DEFAULT)
    b, _ = run_simulation(scenario, experiment, run_id='verify-tight', solver=SOLVER_TIGHT)
    if a['status'] != 'completed' or b['status'] != 'completed':
        return {'passed': False, 'reason': 'a verification run did not complete', 'statuses': [a['status'], b['status']]}
    diffs = {s: abs(a['endpoint_state'][s] - b['endpoint_state'][s]) / max(1.0, abs(b['endpoint_state'][s]))
             for s in a['endpoint_state']}
    worst = max(diffs.values())
    return {'passed': worst <= CONVERGENCE_REL_TOL, 'max_relative_endpoint_difference': worst,
            'per_state': diffs, 'tolerance': CONVERGENCE_REL_TOL,
            'label': 'numerical verification check; not counted in the scientific campaign budget'}
