"""Analysis of a bioreactor run against the request's target and the QC profile.

Order of reasoning (each later step is only trusted if the earlier ones hold):
  1. Release-safety tests (sterility, mycoplasma, endotoxin, karyotype) and, in max
     mode, clonal-drift evidence. A failure here is a SAFETY_HOLD, not a recipe problem.
  2. Data integrity (max mode, analysis_agent tools): staining failure, sensor drift,
     incoherent channels, open mass balance -> DATA_UNRELIABLE. Never redesign a
     protocol on untrustworthy numbers.
  3. Desired output per arm (number of target cells at the target day).
  4. QC profile per arm (viability, identity/purity, residual pluripotency, ...).
  5. Genotype comparison: each engineered arm vs the wild-type control, and each
     predicted genotype effect tested as consistent / contradicted / inconclusive.
  6. Diagnosis: rule-based hypotheses that name the protocol steps (levers) the
     literature agent should revisit. Hypotheses, not causal proof.
"""
import math
import statistics

import numpy as np

from .. import contracts as K
from . import qc as QC
from .protocol import protocol_sha256
from .runs import validate_run

NUMERIC = ('viable_cells_total', 'viability_pct', 'target_marker_pct', 'target_cells_total',
           'target_cells_per_input_cell', 'residual_pluripotency_pct', 'harvest_day', 'harvest_onset_day',
           'endotoxin_EU_per_mL', 'growth_rate_per_h', 'doubling_time_h', 'lactate_per_glucose',
           'harvest_cell_diam_mean_um')
CATEGORICAL_BAD = {'sterility': 'growth_detected', 'mycoplasma': 'detected', 'karyotype': 'abnormal'}
COMPARE = ('target_cells_per_input_cell', 'target_cells_total', 'target_marker_pct', 'viability_pct',
           'residual_pluripotency_pct', 'viable_cells_total', 'harvest_onset_day', 'growth_rate_per_h',
           'doubling_time_h', 'lactate_per_glucose')
DIRECTION_SIGN = {'increase': 1, 'later': 1, 'decrease': -1, 'earlier': -1, 'no_change': 0}
_TOOL_ERRORS = (KeyError, ValueError, TypeError, IndexError, ZeroDivisionError, np.linalg.LinAlgError)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def _sd(xs):
    xs = [x for x in xs if x is not None]
    return statistics.stdev(xs) if len(xs) > 1 else None


def _r(x, nd=4):
    return None if x is None else float(f'{x:.{nd}g}')


# ── max mode: analysis_agent integrated-machine checks ─────────────────────

def max_mode_checks(arm_rec):
    """Run the analysis_agent tools on one replicate. Missing channels -> not_assessable, never guessed."""
    from analysis_agent import analysis_tools as AT
    atrun = {'run_id': f'{arm_rec["arm_id"]}-r{arm_rec["replicate"]}', 'observations': arm_rec['observations'],
             'outcome': arm_rec.get('outcome') or {}, 'setpoints': arm_rec.get('setpoints') or {},
             'passage_number': arm_rec.get('passage_number')}
    history = arm_rec.get('history') or []
    res, na = {}, {}

    def tool(name, fn, *args):
        try:
            with np.errstate(all='ignore'):
                res[name] = fn(*args)
        except _TOOL_ERRORS as e:
            na[name] = f'{type(e).__name__}: {e}'
            res[name] = None

    tool('kinetics', AT.derive_kinetics, atrun)
    kin = res['kinetics']
    tool('purity', AT.fuse_purity, atrun, kin) if kin else na.setdefault('purity', 'needs kinetics')
    tool('staining', AT.check_staining_integrity, atrun)
    tool('coherence', AT.check_measurement_coherence, atrun)
    tool('mass_balance', AT.reconcile_mass_balance, atrun, kin) if kin else na.setdefault('mass_balance', 'needs kinetics')
    tool('sensor_health', AT.check_sensor_health, atrun, history)
    if atrun['setpoints'] and all(h.get('setpoints') for h in history):
        tool('attribution', AT.attribute_cause, atrun, history + [atrun])
    else:
        na['attribution'] = 'executed setpoints missing for this run or its history'
        res['attribution'] = None
    purity = res.get('purity') or {}
    cqa_like = {'purity': purity if purity.get('available') else {},
                'offline_confirmation_required': bool(purity.get('extrapolating'))}
    if res['attribution']:
        tool('recommended_device_action', AT.recommend_next_action, res['attribution'], cqa_like, 100.0)
    obs = arm_rec['observations']
    tail = [o['harvest_cell_diam_mean_um'] for o in obs if o.get('harvest_cell_diam_mean_um', 0) > 0][-3:]
    glc = [o['glucose_mM'] for o in obs if o.get('glucose_mM') is not None]
    res['derived'] = {
        'harvest_cell_diam_mean_um': round(_mean(tail), 2) if tail else None,
        'harvest_onset_day': next((o['day'] for o in obs if o.get('harvest_cells_e6_per_ml_day', 0) > 0), None),
        'min_glucose_mM': min(glc) if glc else None,
    }
    suspects = []
    st = res.get('staining') or {}
    if st.get('staining_failure_suspected'):
        suspects.append(f'staining failure: fluorescence viability {st["fluorescence_viability_pct"]}% vs label-free '
                        f'{st["impedance_viability_pct"]}% ({st["device"]}); antigen readouts from this panel are void')
    sh = res.get('sensor_health') or {}
    for f in sh.get('flags', []):
        suspects.append(f'sensor drift: {f["ratio"]} z={f["z"]}, shift {f["relative_shift"]:+.1%} ({f["device"]})')
    co = res.get('coherence') or {}
    if co.get('measurement_artifact_suspected'):
        bad = [c['pair'] for c in co['checks'] if not c['coherent']]
        suspects.append(f'measurement artifact: incoherent channel pairs {bad}')
    mb = res.get('mass_balance') or {}
    if mb and not mb.get('balance_closed', True):
        suspects.append('mass balance open: ' + '; '.join(mb['open_reasons']))
    att = res.get('attribution') or {}
    clonal = att.get('clonal') or {}
    res['integrity_suspects'] = suspects
    res['clonal_verdict'] = clonal.get('verdict', 'NOT_TESTABLE' if not att else 'NO_EVIDENCE')
    res['clonal_weight'] = (att.get('weights') or {}).get('clonal_genetic_drift')
    res['not_assessable'] = na
    return res


# ── per-replicate and per-arm metrics ──────────────────────────────────────

def replicate_metrics(rec, mode):
    h = rec['harvest']
    target = h['viable_cells_total'] * h['target_marker_pct'] / 100.0
    m = {'viable_cells_total': h['viable_cells_total'], 'viability_pct': h['viability_pct'],
         'target_marker_pct': h['target_marker_pct'], 'target_cells_total': target,
         'target_cells_per_input_cell': target / rec['input_cells'],
         'residual_pluripotency_pct': h.get('residual_pluripotency_pct'), 'harvest_day': h['day'],
         'harvest_onset_day': h.get('harvest_onset_day')}
    tests = rec.get('qc_tests') or {}
    for k in ('sterility', 'mycoplasma', 'karyotype', 'endotoxin_EU_per_mL'):
        m[k] = tests.get(k)
    mx = None
    if mode == 'max':
        mx = max_mode_checks(rec)
        kin = mx.get('kinetics') or {}
        m['growth_rate_per_h'] = kin.get('mu_h')
        m['doubling_time_h'] = kin.get('doubling_time_h')
        m['lactate_per_glucose'] = kin.get('y_lac_per_glc')
        m['harvest_cell_diam_mean_um'] = mx['derived']['harvest_cell_diam_mean_um']
        if m['harvest_onset_day'] is None:
            m['harvest_onset_day'] = mx['derived']['harvest_onset_day']
    return m, mx


def _aggregate(reps):
    agg = {}
    for k in NUMERIC:
        vals = [r[k] for r in reps if r.get(k) is not None]
        agg[k] = {'mean': _r(_mean(vals), 6), 'sd': _r(_sd(vals)), 'n': len(vals), 'values': [_r(v, 6) for v in vals]}
    for k, bad in CATEGORICAL_BAD.items():
        vals = [r[k] for r in reps if r.get(k) is not None]
        agg[k] = {'worst': (bad if bad in vals else vals[0]) if vals else None, 'n': len(vals), 'values': vals}
    return agg


def _target(agg, request):
    d = request['desired_output']
    m, hd = agg[d['metric']], agg['harvest_day']['mean']
    out = {'metric': d['metric'], 'required': d['value'], 'unit': d['unit'], 'at_day': d['at_day'],
           'observed_mean': m['mean'], 'observed_values': m['values'], 'harvest_day': hd}
    if hd is None or abs(hd - d['at_day']) > d['day_tolerance']:
        out.update(status='NOT_COMPARABLE', shortfall_fraction=None,
                   note=f'harvested day {hd}, target is day {d["at_day"]} +/- {d["day_tolerance"]}')
    else:
        met = m['mean'] >= d['value']
        out.update(status='MET' if met else 'NOT_MET', shortfall_fraction=_r(1 - m['mean'] / d['value']))
        if len(m['values']) > 1 and met != all(v >= d['value'] for v in m['values']):
            out['note'] = f'replicates {m["values"]} straddle the target'
    return out


def _qc(agg, profile, mode, intervals):
    values = {k: v['mean'] for k, v in agg.items() if 'mean' in v}
    values.update({k: agg[k]['worst'] for k in CATEGORICAL_BAD})
    endo = agg['endotoxin_EU_per_mL']['values']
    values['endotoxin_EU_per_mL'] = max(endo) if endo else None  # worst replicate
    reps = {k: v['values'] for k, v in agg.items() if 'values' in v and 'mean' in v}
    rows = QC.score(profile, mode, values, reps, intervals)
    return {'status': QC.overall(rows), 'criteria': rows}


# ── genotype comparison ────────────────────────────────────────────────────

def _direction(metric, a_vals, r_vals):
    ma, mr = _mean(a_vals), _mean(r_vals)
    if ma is None or mr is None:
        return None
    diff, na, nr = ma - mr, len(a_vals), len(r_vals)
    if na >= 2 and nr >= 2:
        se = math.sqrt(statistics.variance(a_vals) / na + statistics.variance(r_vals) / nr)
        band, basis = 2 * se, f'replicates (n={na} vs {nr}); change must exceed 2 standard errors'
    elif metric == 'harvest_onset_day':
        band, basis = 1.0, 'single replicate; timing change must exceed 1 day (daily sampling)'
    else:
        band, basis = 0.10 * abs(mr), 'single replicate; change must exceed 10% of the control'
    sign = 0 if abs(diff) <= band else (1 if diff > 0 else -1)
    strength = 'replicated' if na >= 3 and nr >= 3 else 'directional_single_run' if min(na, nr) == 1 else 'low_n'
    return {'arm_mean': _r(ma, 6), 'reference_mean': _r(mr, 6), 'absolute_difference': _r(diff, 6),
            'ratio': _r(ma / mr) if mr else None, 'observed_sign': sign, 'band': _r(band), 'basis': basis,
            'strength': strength}


def compare_genotypes(protocol, arm_reps):
    arms = {a['arm_id']: a for a in protocol['genotype_arms']}
    refs = [a for a in arms if arms[a]['genotype'] == 'wild_type' and a in arm_reps]
    if not refs:
        return []
    ref = refs[0]
    out = []
    for aid, reps in arm_reps.items():
        if aid == ref:
            continue
        diffs = {}
        for m in COMPARE:
            t = _direction(m, [r[m] for r in reps if r.get(m) is not None],
                           [r[m] for r in arm_reps[ref] if r.get(m) is not None])
            if t:
                diffs[m] = t
        preds = []
        for e in protocol['genotype_effects']:
            if e['arm_id'] != aid:
                continue
            t = diffs.get(e['readout_metric'])
            p = {'effect_id': e['effect_id'], 'gene': e['gene'], 'affected_quantity': e['affected_quantity'],
                 'readout_metric': e['readout_metric'], 'predicted_direction': e['predicted_direction'],
                 'evidence_level': e['evidence_level'], 'claim_ids': e['claim_ids']}
            if e['predicted_direction'] == 'unknown':
                p.update(verdict='untestable', reason='no direction was predicted')
            elif t is None:
                p.update(verdict='not_measured', reason=f'{e["readout_metric"]} not reported for both arms')
            else:
                want, got = DIRECTION_SIGN[e['predicted_direction']], t['observed_sign']
                if got == want:
                    verdict = 'consistent'
                elif got == 0:
                    verdict = 'inconclusive'
                else:
                    verdict = 'contradicted'
                p.update(verdict=verdict, observed=t, strength=t['strength'],
                         reason=f'observed {e["readout_metric"]} {t["arm_mean"]} vs WT {t["reference_mean"]} ({t["basis"]})')
                mag = e.get('predicted_magnitude')
                if mag and mag.get('unit') == 'fold' and isinstance(mag.get('value'), (int, float)) and t['ratio']:
                    p['predicted_fold'] = mag['value']
                    p['observed_fold'] = t['ratio']
            preds.append(p)
        out.append({'arm_id': aid, 'reference_arm_id': ref, 'genotype': arms[aid]['genotype'],
                    'gene': arms[aid].get('gene'), 'differences': diffs, 'predicted_effects': preds})
    return out


# ── diagnosis ──────────────────────────────────────────────────────────────

def _lever(step, stage_id, parameter, direction, basis):
    q = (step or {}).get('quantity')
    cur = None if q is None else {'value': q['value'], 'unit': q['unit'], 'provenance': q['provenance']}
    return {'step_id': (step or {}).get('step_id'), 'stage_id': stage_id, 'parameter': parameter,
            'current': cur, 'direction_hint': direction, 'basis': basis}


def _protocol_map(protocol):
    stages = protocol['stages']
    exp = next((s for s in stages if any(x['action'] == 'seed' for x in s['steps'])), stages[0])
    diff = [s for s in stages if s is not exp]
    harvest = next((s for s in reversed(stages) if any(x['action'] in ('harvest', 'start_harvest') for x in s['steps'])), stages[-1])
    return exp, diff, harvest


def diagnose(protocol, request, arms_out, comparison, max_by_arm, mode):
    hyps = []
    exp, diff, harvest = _protocol_map(protocol)
    seed = next((x for x in exp['steps'] if x['action'] == 'seed'), None)
    factor_steps = [(s, x) for s in diff for x in s['steps'] if x['action'] == 'add_factor']
    params = protocol['culture_system']['parameters']

    def add(cat, statement, arms, stage_ids, levers, obs, failed=(), conf='moderate', blocks=False):
        hyps.append({'hypothesis_id': f'H{len(hyps) + 1:02d}', 'category': cat, 'statement': statement,
                     'arms': sorted(arms), 'stage_ids': stage_ids, 'failed_criteria': list(failed), 'levers': levers,
                     'supporting_observations': obs, 'confidence': conf, 'blocks_protocol_revision': blocks})

    # 1-2: safety, genetics, data integrity (block redesign)
    for a in arms_out:
        bad = [c for c in a['qc']['criteria'] if c['status'] == 'FAIL' and c['severity'] == 'safety']
        safety = [c for c in bad if c['category'] == 'safety']
        if safety:
            add('safety', 'Release-safety test failed: a contamination/aseptic-processing problem, not a protocol-design problem.',
                [a['arm_id']], [], [], [f'{c["id"]}: observed {c["observed"]!r}, required {c["op"]} {c["value"]!r}' for c in safety],
                [c['id'] for c in safety], 'high', True)
        gen = [c for c in bad if c['category'] == 'genetic_stability']
        if gen:
            add('genetic_stability', 'Karyotype abnormal: the line itself has changed; process results from this line are confounded.',
                [a['arm_id']], [], [], [f'{c["id"]}: {c["observed"]!r}' for c in gen], [c['id'] for c in gen], 'high', True)
    for aid, mxs in max_by_arm.items():
        for mx in mxs:
            if mx['integrity_suspects']:
                add('measurement', 'Measurements are not trustworthy; fix the instrument/assay and re-measure before changing the protocol.',
                    [aid], [], [], mx['integrity_suspects'], [], 'high', True)
            if mx['clonal_verdict'] in ('LIKELY_CLONAL', 'POSSIBLE_CLONAL'):
                cl = (mx.get('attribution') or {}).get('clonal', {})
                add('genetic_stability', f'Clonal/genetic drift ({mx["clonal_verdict"]}): growth improves while product degrades with passage.',
                    [aid], [], [], [f'growth vs passage rho {cl.get("growth_vs_passage_rho")}',
                                    f'differentiation vs passage rho {cl.get("differentiation_vs_passage_rho")}',
                                    f'clonal drift score {cl.get("clonal_drift_score")}'],
                    [], 'high' if mx['clonal_verdict'] == 'LIKELY_CLONAL' else 'moderate',
                    mx['clonal_verdict'] == 'LIKELY_CLONAL')

    # 3-6: process hypotheses per arm
    for a in arms_out:
        aid, m, crit = a['arm_id'], a['metrics'], {c['id']: c for c in a['qc']['criteria']}
        kin = (max_by_arm.get(aid) or [{}])[0].get('kinetics') or {}
        mxd = (max_by_arm.get(aid) or [{}])[0].get('derived') or {}
        tgt = a['target']
        if tgt['status'] == 'NOT_COMPARABLE':
            hstep = next((x for x in harvest['steps'] if x['action'] == 'harvest'), None)
            add('schedule', 'Harvest day does not match the target day, so the run cannot answer the question.',
                [aid], [harvest['stage_id']],
                [_lever(hstep, harvest['stage_id'], 'harvest day', 'later' if tgt['harvest_day'] < tgt['at_day'] else 'earlier',
                        f'target day {tgt["at_day"]}')], [tgt['note']], [f'target:{aid}'], 'high')
        if crit.get('qc-residual-pluripotency', {}).get('status') == 'FAIL':
            first = diff[0] if diff else exp
            levers = [_lever(None, exp['stage_id'], 'expansion -> induction transition day', 'revisit',
                             'residual pluripotent cells persist to harvest')]
            levers += [_lever(x, s['stage_id'], f'{x["factor"]} dose/window', 'revisit', 'exit from pluripotency incomplete')
                       for s, x in factor_steps if s is first]
            add('residual_pluripotency', 'Exit from pluripotency is incomplete: undifferentiated cells persist to harvest.',
                [aid], [first['stage_id']], levers,
                [f'residual pluripotency {m["residual_pluripotency_pct"]["mean"]}% vs limit {crit["qc-residual-pluripotency"]["value"]}%'],
                ['qc-residual-pluripotency'])
        purity_fail = crit.get('qc-identity-purity', {}).get('status') in ('FAIL', 'MARGINAL')
        if purity_fail:
            obs = [f'{request["product"]["target_marker"]} {m["target_marker_pct"]["mean"]}% vs limit {crit["qc-identity-purity"]["value"]}%']
            if kin.get('frac_time_over_300um') is not None:
                obs.append(f'aggregates >300 um on {kin["frac_time_over_300um"]:.0%} of days; max diameter {kin["diameter_max_um"]} um (M3b)')
            add('differentiation', 'Differentiation toward the target is inefficient: factor doses, exposure windows or stage lengths are off for this line.',
                [aid], [s['stage_id'] for s in diff],
                [_lever(x, s['stage_id'], f'{x["factor"]} dose/window', 'revisit', 'identity/purity below specification')
                 for s, x in factor_steps] + [_lever(None, s['stage_id'], 'stage duration', 'revisit', 'stage-length sensitivity') for s in diff],
                obs, ['qc-identity-purity'], 'moderate' if mode == 'max' else 'low')
        if crit.get('qc-viability', {}).get('status') in ('FAIL', 'MARGINAL'):
            obs = [f'viability {m["viability_pct"]["mean"]}% vs limit {crit["qc-viability"]["value"]}%']
            levers = []
            if kin.get('frac_time_over_300um', 0) > 0.2:
                obs.append(f'aggregates >300 um on {kin["frac_time_over_300um"]:.0%} of days (necrotic-core risk, M3b)')
                levers.append(_lever({'quantity': params.get('agitation_rpm'), 'step_id': None}, None, 'agitation_rpm', 'increase',
                                     'larger aggregates than the oxygen-penetration depth'))
            if (kin.get('min_ph') or 7.4) < 6.9 or (kin.get('y_lac_per_glc') or 0) > 2.0 or (mxd.get('min_glucose_mM') or 99) < 1.0:
                obs.append(f'min pH {kin.get("min_ph")}, Y_lac/glc {kin.get("y_lac_per_glc")}, min glucose {mxd.get("min_glucose_mM")} mM (M1/M3a)')
                levers.append(_lever({'quantity': params.get('feed_fraction'), 'step_id': None}, None, 'feed_fraction', 'increase',
                                     'acidification / nutrient depletion'))
            if not levers:
                levers = [_lever({'quantity': params.get(k), 'step_id': None}, None, k, 'revisit', 'viability below specification')
                          for k in ('agitation_rpm', 'feed_fraction', 'dissolved_oxygen') if k in params]
            add('survival', 'Cell survival is limiting (shear, aggregate size, feeding or oxygen).', [aid],
                [s['stage_id'] for s in protocol['stages']], levers, obs, ['qc-viability'], 'moderate' if len(obs) > 1 else 'low')
        # Quantity-limited: the target would still be missed even at the purity limit.
        lim = crit.get('qc-identity-purity', {}).get('value')
        at_limit = None
        if tgt['status'] == 'NOT_MET' and purity_fail and isinstance(lim, (int, float)):
            marker = m['target_marker_pct']['mean'] or 0
            at_limit = tgt['observed_mean'] * (max(marker, lim) / marker) if marker else None
        quantity_limited = tgt['status'] == 'NOT_MET' and (not purity_fail or (at_limit is not None and at_limit < tgt['required']))
        if quantity_limited:
            obs = [f'{tgt["metric"]} {tgt["observed_mean"]} vs required {tgt["required"]} (shortfall {tgt["shortfall_fraction"]:.0%})'
                   + (' with purity in specification' if not purity_fail else
                      f'; even at the purity limit ({lim}%) output would be {at_limit:.3g}, still short')]
            if kin:
                obs.append(f'growth rate {kin.get("mu_h")} 1/h (doubling {kin.get("doubling_time_h")} h), fold expansion {kin.get("fold_expansion")}')
                if kin.get('harvest_rate_slope') is not None and kin['harvest_rate_slope'] < 0:
                    obs.append(f'harvest release declining (slope {kin["harvest_rate_slope"]}/day, M4)')
            levers = [_lever(seed, exp['stage_id'], 'seeding density', 'revisit', 'more input or better expansion raises output at fixed purity'),
                      _lever(None, exp['stage_id'], 'expansion duration', 'extend', 'more cells entering differentiation'),
                      _lever(None, harvest['stage_id'], 'production/harvest stage duration', 'revisit', 'output accumulates during this stage')]
            levers += [_lever(x, s['stage_id'], f'{x["factor"]} dose/window', 'revisit', 'drives output in the production stage')
                       for s, x in factor_steps if s is harvest]
            add('expansion', 'Output is quantity-limited: too few cells reach harvest, independent of how pure they are.',
                [aid], [exp['stage_id'], harvest['stage_id']], levers, obs, [f'target:{aid}'], 'moderate' if kin else 'low')

    # genotype-specific
    wt_ok = {a['arm_id'] for a in arms_out if a['target']['status'] == 'MET'}
    for c in comparison:
        aid = c['arm_id']
        arm_out = next(a for a in arms_out if a['arm_id'] == aid)
        contra = [p for p in c['predicted_effects'] if p['verdict'] == 'contradicted']
        adj = [x for x in protocol['arm_adjustments'] if x['arm_id'] == aid]
        steps = {x['step_id']: (s, x) for s in protocol['stages'] for x in s['steps']}
        levers = [_lever(steps[x['step_id']][1], steps[x['step_id']][0]['stage_id'],
                         f'{aid} adjustment {x["adjustment_id"]}', 'revisit', x['rationale']) for x in adj if x['step_id'] in steps]
        if contra:
            add('genotype_specific', f'Predicted {c["gene"]} effects were contradicted by the {aid} vs {c["reference_arm_id"]} comparison; '
                'the genotype-specific adjustments rest on a wrong expectation.', [aid],
                sorted({p.get('stage_id') for p in protocol['genotype_effects'] if p['arm_id'] == aid and p.get('stage_id')}),
                levers, [f'{p["effect_id"]}: predicted {p["predicted_direction"]}, {p["reason"]}' for p in contra],
                [p['effect_id'] for p in contra], 'moderate' if all(p['strength'] == 'replicated' for p in contra) else 'low')
        metric = request['desired_output']['metric']
        worse = (c['differences'].get(metric) or {}).get('observed_sign') == -1
        if worse and arm_out['target']['status'] == 'NOT_MET' and c['reference_arm_id'] not in wt_ok:
            d = c['differences'][metric]
            add('genotype_specific', f'{aid} ({c["gene"]} {c["genotype"]}) produces less than {c["reference_arm_id"]} under the same protocol: '
                'beyond the shared shortfall, the edit changes what this arm needs.', [aid], [s['stage_id'] for s in diff],
                levers or [_lever(x, s['stage_id'], f'{x["factor"]} dose/window for {aid}', 'revisit', 'genotype-dependent requirement')
                           for s, x in factor_steps],
                [f'{metric}: {aid} {d["arm_mean"]} vs {c["reference_arm_id"]} {d["reference_mean"]} (ratio {d["ratio"]}; {d["basis"]})'],
                [f'target:{aid}'], 'moderate' if d['strength'] == 'replicated' else 'low')
        if c['reference_arm_id'] in wt_ok and arm_out['target']['status'] == 'NOT_MET':
            d = c['differences'].get(request['desired_output']['metric'], {})
            add('genotype_specific', f'The protocol works for {c["reference_arm_id"]} but not for {aid} ({c["gene"]} {c["genotype"]}): '
                'the edit changes what this arm needs.', [aid], [s['stage_id'] for s in diff],
                levers or [_lever(x, s['stage_id'], f'{x["factor"]} dose/window for {aid}', 'revisit', 'genotype-dependent requirement')
                           for s, x in factor_steps],
                [f'{request["desired_output"]["metric"]}: {aid} {d.get("arm_mean")} vs {c["reference_arm_id"]} {d.get("reference_mean")}'],
                [f'target:{aid}'], 'moderate')
    return _merge(hyps)


def _merge(hyps):
    """One hypothesis per (category, statement): pool arms, evidence, levers and failed criteria."""
    merged = {}
    for h in hyps:
        key = (h['category'], h['statement'])
        if key not in merged:
            merged[key] = dict(h, arms=list(h['arms']), supporting_observations=list(h['supporting_observations']),
                               levers=list(h['levers']), failed_criteria=list(h['failed_criteria']), stage_ids=list(h['stage_ids']))
            continue
        m = merged[key]
        for k in ('arms', 'supporting_observations', 'failed_criteria', 'stage_ids'):
            m[k] += [x for x in h[k] if x not in m[k]]
        seen = {(lv['step_id'], lv['parameter']) for lv in m['levers']}
        m['levers'] += [lv for lv in h['levers'] if (lv['step_id'], lv['parameter']) not in seen]
        order = ('low', 'moderate', 'high')
        m['confidence'] = max(m['confidence'], h['confidence'], key=order.index)
        m['blocks_protocol_revision'] = m['blocks_protocol_revision'] or h['blocks_protocol_revision']
    out = sorted(merged.values(), key=lambda h: (not h['blocks_protocol_revision'], ('high', 'moderate', 'low').index(h['confidence'])))
    for i, h in enumerate(out, 1):
        h['hypothesis_id'] = f'H{i:02d}'
        h['arms'] = sorted(h['arms'])
    return out


# ── report ─────────────────────────────────────────────────────────────────

def analyze(run, protocol, request):
    """Return an AnalysisReport 2.0 dict (validated)."""
    K.require_valid('production_request', request)
    check = validate_run(run, protocol)
    if not check['valid']:
        raise K.ContractError('run invalid: ' + '; '.join(check['errors'][:10]))
    mode = run['mode']
    profile = QC.load_profile(request['qc_profile'], request.get('qc_overrides'))
    arm_reps, max_by_arm, rep_order = {}, {}, []
    for rec in run['arms']:
        m, mx = replicate_metrics(rec, mode)
        arm_reps.setdefault(rec['arm_id'], []).append(m)
        if mx is not None:
            max_by_arm.setdefault(rec['arm_id'], []).append(mx)
        if rec['arm_id'] not in rep_order:
            rep_order.append(rec['arm_id'])
    arms_meta = {a['arm_id']: a for a in protocol['genotype_arms']}
    arms_out = []
    for aid in rep_order:
        agg = _aggregate(arm_reps[aid])
        intervals = {}
        mxs = max_by_arm.get(aid, [])
        if len(mxs) == 1 and (mxs[0].get('purity') or {}).get('available'):
            intervals['target_marker_pct'] = mxs[0]['purity']['ci95']
        arms_out.append({'arm_id': aid, 'genotype': arms_meta[aid]['genotype'], 'gene': arms_meta[aid].get('gene'),
                         'n_replicates': len(arm_reps[aid]), 'metrics': agg, 'target': _target(agg, request),
                         'qc': _qc(agg, profile, mode, intervals),
                         'max_mode': [_max_summary(mx) for mx in mxs] or None})
    comparison = compare_genotypes(protocol, arm_reps)
    hyps = diagnose(protocol, request, arms_out, comparison, max_by_arm, mode)

    suspects = [s for mxs in max_by_arm.values() for mx in mxs for s in mx['integrity_suspects']]
    integrity = {'status': ('SUSPECT' if suspects else 'OK') if mode == 'max' else 'NOT_ASSESSED',
                 'checks': suspects if mode == 'max' else
                 ['minimal mode: no redundant channels, so instrument faults and measurement artifacts cannot be detected']}
    applies = request['desired_output'].get('applies_to_arms') or [a['arm_id'] for a in arms_out]
    target_arms = [a for a in arms_out if a['arm_id'] in applies]
    met = [a['arm_id'] for a in target_arms if a['target']['status'] == 'MET']
    failed = [a['arm_id'] for a in target_arms if a['target']['status'] != 'MET']
    qcs = [a['qc']['status'] for a in target_arms]
    qc_status = 'FAIL' if 'FAIL' in qcs else 'INCOMPLETE' if 'INCOMPLETE' in qcs else 'MARGINAL' if 'MARGINAL' in qcs else 'PASS'
    blocking = [h for h in hyps if h['blocks_protocol_revision']]
    if any(h['category'] in ('safety', 'genetic_stability') for h in blocking):
        status = 'SAFETY_HOLD'
    elif integrity['status'] == 'SUSPECT':
        status = 'DATA_UNRELIABLE'
    elif not failed and qc_status == 'PASS':
        status = 'SUCCESS'
    elif not failed and qc_status in ('INCOMPLETE', 'MARGINAL'):
        status = 'TARGET_MET_QC_INCOMPLETE'
    else:
        status = 'FAILED'
    d = request['desired_output']
    summary = (f'{status}: target {d["metric"]} >= {d["value"]:g} {d["unit"]} at day {d["at_day"]:g} met by {met or "no arm"}'
               f'{"; not met by " + str(failed) if failed else ""}; QC {qc_status}.')
    if blocking:
        summary += ' Blocking: ' + '; '.join(h['statement'] for h in blocking[:2])
    lim = ['Diagnosis hypotheses are rule-based pointers to protocol levers for the literature agent, not causal proof.',
           f'Target marker % ({request["product"]["target_marker"]}) is identity/purity, not potency or maturity.',
           f'QC profile {profile["profile_id"]}: limits marked demo_default or user_required must be confirmed by your QA team.']
    if any(a['n_replicates'] < 3 for a in arms_out):
        lim.append('Fewer than 3 replicates in at least one arm: genotype differences are directional only.')
    if run['source'] == 'synthetic_standin':
        lim.insert(0, 'SYNTHETIC STAND-IN DATA: the simulator is not a validated digital twin; this report demonstrates the workflow, not biology.')
    if mode == 'minimal':
        lim.append('Minimal mode cannot separate instrument faults, artifacts or clonal drift from process effects.')
    else:
        lim.append('Max-mode purity is a fusion of a label-free estimate and a sparse antigen panel; release needs a fresh direct panel.')
    report = {
        'schema_version': K.PRODUCTION_VERSION,
        'analysis_id': f'analysis-{run["run_id"]}',
        'run_id': run['run_id'], 'protocol_id': protocol['protocol_id'], 'request_id': request['request_id'],
        'mode': mode, 'source': run['source'], 'created_at': K.now_iso(),
        'qc_profile': {'profile_id': profile['profile_id'], 'chain': profile['chain']},
        'hashes': {'run_sha256': K.sha256_obj(run), 'protocol_sha256': protocol_sha256(protocol),
                   'request_sha256': K.sha256_obj(request), 'code_sha256': K.code_sha256(),
                   'run_validation_warnings': check['warnings']},
        'verdict': {'status': status, 'summary': summary, 'target_met_arms': met, 'target_failed_arms': failed,
                    'qc_status': qc_status},
        'arms': arms_out, 'genotype_comparison': comparison, 'diagnosis': hyps,
        'data_integrity': integrity,
        'max_mode': {'device_actions': {aid: [mx.get('recommended_device_action') for mx in mxs] for aid, mxs in max_by_arm.items()}}
        if mode == 'max' else None,
        'limitations': lim,
    }
    K.require_valid('analysis_report', report)
    return report


def _max_summary(mx):
    p = mx.get('purity') or {}
    att = mx.get('attribution') or {}
    return {'kinetics': mx.get('kinetics'),
            'fused_purity': {k: p.get(k) for k in ('purity_estimate_pct', 'ci95', 'sources', 'direct_measurement_age_h',
                                                     'extrapolating', 'calibration_offset_pct')} if p.get('available') else None,
            'staining': mx.get('staining'), 'sensor_flags': (mx.get('sensor_health') or {}).get('flags'),
            'sensor_testable': (mx.get('sensor_health') or {}).get('testable'),
            'coherence_incoherent': (mx.get('coherence') or {}).get('incoherent_count'),
            'mass_balance_closed': (mx.get('mass_balance') or {}).get('balance_closed'),
            'attribution_weights': att.get('weights'), 'top_cause': att.get('top_cause'),
            'clonal_verdict': mx['clonal_verdict'], 'recommended_device_action': mx.get('recommended_device_action'),
            'integrity_suspects': mx['integrity_suspects'], 'not_assessable': mx['not_assessable'],
            'derived': mx['derived']}


def render_report(report):
    """Short Markdown summary for humans."""
    v = report['verdict']
    L = [f'# Analysis {report["analysis_id"]}', '', f'**{v["status"]}** — {v["summary"]}', '',
         f'Mode: {report["mode"]}; source: {report["source"]}; data integrity: {report["data_integrity"]["status"]}', '',
         '| Arm | n | Target cells/input | Target cells | Marker % | Viability % | Target | QC |', '|---|---|---|---|---|---|---|---|']
    for a in report['arms']:
        m = a['metrics']
        L.append(f'| {a["arm_id"]} | {a["n_replicates"]} | {m["target_cells_per_input_cell"]["mean"]} | {m["target_cells_total"]["mean"]:.4g} | '
                 f'{m["target_marker_pct"]["mean"]} | {m["viability_pct"]["mean"]} | {a["target"]["status"]} | {a["qc"]["status"]} |')
    L += ['', '## QC criteria not passing', '']
    open_ = [(a, c) for a in report['arms'] for c in a['qc']['criteria'] if c['status'] not in ('PASS', 'NOT_APPLICABLE')]
    if not open_:
        L.append('- none')
    for a, c in open_:
        L.append(f'- {a["arm_id"]} {c["id"]}: {c["status"]} (observed {c["observed"]!r}, limit {c["op"]} {c["value"]!r}; {c["value_status"]})')
    if report['genotype_comparison']:
        L += ['', '## Genotype comparison', '']
        for c in report['genotype_comparison']:
            for p in c['predicted_effects']:
                L.append(f'- {c["arm_id"]} vs {c["reference_arm_id"]} {p["effect_id"]} ({p["gene"]}, {p["affected_quantity"]}): '
                         f'predicted {p["predicted_direction"]} -> **{p["verdict"]}** — {p.get("reason", "")}')
    L += ['', '## Diagnosis (hypotheses)', '']
    for h in report['diagnosis']:
        L.append(f'- {h["hypothesis_id"]} [{h["category"]}, {h["confidence"]}{", BLOCKS REVISION" if h["blocks_protocol_revision"] else ""}] '
                 f'{h["statement"]} Evidence: {"; ".join(h["supporting_observations"])}')
    L += ['', '## Limitations', ''] + [f'- {x}' for x in report['limitations']] + ['']
    return '\n'.join(L)
