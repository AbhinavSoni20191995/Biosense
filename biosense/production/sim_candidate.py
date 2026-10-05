"""Candidate parameters -> the project's simulator -> a quantified prediction.

This is the join Phase 1 could not make. A candidate arrived as `mcsf_ng_ml`,
the simulator had a knob called `mcsf`, and nothing connected them. Now the
ProjectProfile holds the mapping, so the handoff is:

    candidate (canonical parameter_id)
      -> project.parameter(pid).simulator_mapping      # or no mapping at all
      -> setpoint override on the CONTROL condition
      -> sim_mode.compare(control, candidate)
      -> Estimates, every one labelled `simulated`

Three refusals matter more than the arithmetic:

* **A parameter the project does not expose is not silently dropped.** It comes
  back in `skipped` with the reason, so a benchmark can assert that BioSense
  said so rather than quietly shrinking the candidate set.
* **A `not_modelled` parameter produces no prediction.** It is still applied to
  nothing and reported as present-but-unpredictable, because the honest answer
  to "what will temperature do?" from a model with no temperature term is "this
  model cannot say".
* **A project with no simulator returns no prediction at all.** It does not
  borrow another project's model. The CAR-T profile exercises this path.

Readouts come from the project too: `simulator_metric` on each readout says
where to find it in a simulator result, so a CAR-T project asking for
CAR-positive fraction gets `None` rather than a monocyte gate percentage.
"""
from __future__ import annotations

from .. import contracts as K
from .. import parameters as PR
from .. import projects as PJ
from ..evidence import estimates as E
from . import sim_mode as SM

# Metrics the simulator signature exposes, and the unit each carries. The
# signature is a bare dict of numbers; without this table a comparison cannot
# say "percentage points" rather than "+3".
METRIC_UNITS = {
    'harvest_per_input_ipsc': 'cells/input_cell',
    'harvest_total_e6_per_ml': '1e6 cells/mL',
    'cumulative_differentiation_efficiency': 'fraction',
    'final_viability_pct': '%',
    'peak_vcd_e6_per_ml': '1e6 cells/mL',
    'mean_score': 'score',
    'monocyte_gate_pct': '%',
    'agg_diameter_final_um': 'um',
}
HIGHER_IS_BETTER = {
    'harvest_per_input_ipsc': True, 'harvest_total_e6_per_ml': True,
    'cumulative_differentiation_efficiency': True, 'final_viability_pct': True,
    'peak_vcd_e6_per_ml': True, 'mean_score': True, 'monocyte_gate_pct': True,
    'agg_diameter_final_um': None,
}


def enrich_signature(result):
    """Add the readouts the signature does not carry but the frames do.

    The phenotype readout is the one a macrophage project cares most about and
    the one `sim_mode` never rolled up: CD14 percentage lives in the frames.
    Computed here rather than invented — if no frame reports it, it is absent.
    """
    sig = dict(result['signature'])
    frames = result['frames']
    gated = [f.get('imp_frac_monocyte_cluster') for f in frames
             if f.get('imp_frac_monocyte_cluster')]
    panel = [f.get('cd14_pct') for f in frames if f.get('cd14_pct') is not None]
    if panel:
        sig['monocyte_gate_pct'] = round(float(panel[-1]), 3)
    elif gated:
        sig['monocyte_gate_pct'] = round(float(gated[-1]) * 100.0, 3)
    if frames:
        sig['agg_diameter_final_um'] = frames[-1].get('agg_diameter_mean_um')
    return sig


def plan_handoff(project, candidates):
    """Which candidates can reach this project's simulator, and why the rest cannot.

    Returns {applied, skipped, coverage}. Nothing is dropped silently: every
    candidate appears in exactly one of the two lists with a stated reason.
    """
    if not isinstance(project, PJ.Project):
        raise K.ContractError('plan_handoff needs a loaded Project')
    applied, skipped = [], []
    has_model = project.simulator['status'] == 'current'
    for c in candidates:
        pid = PR.resolve(c['parameter'] if 'parameter' in c else c['parameter_id'])
        cov = project.coverage(pid)
        row = {'parameter_id': pid, 'direction': c.get('direction'),
               'simulator_coverage': cov,
               'label': PR.BY_ID[pid].label, 'unit': PR.BY_ID[pid].unit}
        if not has_model:
            skipped.append({**row, 'reason':
                            f'{project.project_id} has no mechanistic model '
                            f'({project.simulator.get("not_modelled_note") or "status: "
                              + project.simulator["status"]}). No prediction is produced, and '
                            f'none is borrowed from another project.'})
            continue
        if cov == 'not_in_project':
            skipped.append({**row, 'reason':
                            f'{project.project_id} does not expose {pid}; it is a canonical '
                            f'parameter but not a knob of this process.'})
            continue
        if cov != 'modelled':
            pp = project.parameter(pid)
            skipped.append({**row, 'reason':
                            f'{pid} is a real design variable in {project.project_id} but its '
                            f'simulator has no term for it, so no prediction is produced. '
                            f'{pp.notes or ""}'.strip()})
            continue
        pp = project.parameter(pid)
        applied.append({**row, 'knob': pp.simulator_mapping,
                        'current_value': pp.default_value,
                        'minimum': pp.minimum, 'maximum': pp.maximum})
    return {'applied': applied, 'skipped': skipped,
            'coverage': {'modelled': len(applied), 'not_modelled': len(skipped),
                         'has_model': has_model,
                         'model_id': project.simulator.get('model_id'),
                         'model_version': project.simulator.get('model_version')}}


def candidate_setpoints(project, applied, values):
    """Build the candidate condition's setpoint overrides, clamped to the project."""
    out = {}
    clamped = []
    for row in applied:
        pid = row['parameter_id']
        if pid not in values:
            continue
        pp = project.parameter(pid)
        v = float(values[pid])
        lo = pp.minimum if pp.minimum is not None else v
        hi = pp.maximum if pp.maximum is not None else v
        c = min(max(v, lo), hi)
        if abs(c - v) > 1e-12:
            clamped.append({'parameter_id': pid, 'asked': v, 'used': c,
                            'range': [pp.minimum, pp.maximum],
                            'why': (pp.bound_origin or {}).get('basis',
                                                               "this project's range")})
        out[row['knob']] = c
    return out, clamped


def compare_conditions(project, *, control=None, candidate_values=None, candidates=(),
                       readouts=None, seed=7, challenge='none', label_a='control',
                       label_b='candidate'):
    """Run CONTROL and CANDIDATE through the project's simulator and quantify the gap.

    Returns a dict whose `effects` are Estimates with `estimate_type: simulated`
    throughout, plus the coverage report. Where the project has no model, there
    are no effects and `prediction` says why.
    """
    if not isinstance(project, PJ.Project):
        raise K.ContractError('compare_conditions needs a loaded Project')
    hand = plan_handoff(project, candidates or [])
    base_setpoints = dict(control or {})

    if not hand['coverage']['has_model']:
        return {'project_id': project.project_id, 'project_version': project.version,
                'model': project.simulator, 'handoff': hand, 'effects': [],
                'control': None, 'candidate': None, 'clamped': [],
                'prediction': 'none',
                'prediction_note':
                    f'{project.name} has no mechanistic model, so no control-versus-candidate '
                    f'prediction exists. The candidate parameters remain valid evidence-driven '
                    f'design variables and the next step is a real experiment, not a simulation.',
                'evidence_status': None}

    cand_setpoints, clamped = candidate_setpoints(
        project, hand['applied'], candidate_values or {})
    if not cand_setpoints:
        return {'project_id': project.project_id, 'project_version': project.version,
                'model': project.simulator, 'handoff': hand, 'effects': [],
                'control': None, 'candidate': None, 'clamped': clamped,
                'prediction': 'none',
                'prediction_note':
                    'No candidate parameter reached the simulator, so there is nothing to '
                    'compare. Every candidate and the reason it was not applied is in `handoff`.',
                'evidence_status': project.simulator.get('evidence_status')}

    a_cond = {'setpoints': base_setpoints, 'challenge': challenge, 'seed': seed, 'label': label_a}
    b_cond = {'setpoints': {**base_setpoints, **cand_setpoints}, 'challenge': challenge,
              'seed': seed, 'label': label_b}
    a, b = SM.simulate(a_cond), SM.simulate(b_cond)
    sig_a, sig_b = enrich_signature(a), enrich_signature(b)

    wanted = readouts or [r['readout_id'] for r in project.readouts if r.get('simulator_metric')]
    effects = []
    for rid in wanted:
        r = project.readout(rid)
        metric = r.get('simulator_metric')
        if not metric or metric not in sig_a or metric not in sig_b:
            continue
        if sig_a[metric] is None or sig_b[metric] is None:
            continue
        unit = r['unit'] or METRIC_UNITS.get(metric, '')
        e = E.estimate(
            rid, unit,
            E.value(sig_a[metric], 'simulated', source_ref=f'{project.simulator["model_id"]}:control'),
            E.value(sig_b[metric], 'simulated', source_ref=f'{project.simulator["model_id"]}:candidate'),
            label=r['label'],
            higher_is_better=r.get('higher_is_better', HIGHER_IS_BETTER.get(metric)),
            limitations=[f'Simulated by {project.simulator["model_id"]} '
                         f'v{project.simulator.get("model_version")}. '
                         f'{project.simulator.get("evidence_status") or ""}'.strip(),
                         'A mechanistic stand-in, not a validated digital twin. No number here '
                         'is a measurement of any real cell.'])
        effects.append(e)

    return {
        'project_id': project.project_id, 'project_version': project.version,
        'model': project.simulator, 'handoff': hand,
        'control': {'setpoints': a['setpoints'], 'signature': sig_a, 'label': label_a},
        'candidate': {'setpoints': b['setpoints'], 'signature': sig_b, 'label': label_b,
                      'changed': [{'parameter_id': row['parameter_id'], 'knob': row['knob'],
                                   'from': a['setpoints'][row['knob']],
                                   'to': b['setpoints'][row['knob']],
                                   'unit': row['unit'], 'label': row['label']}
                                  for row in hand['applied'] if row['knob'] in cand_setpoints]},
        'clamped': clamped,
        'effects': effects,
        'prediction': 'simulated',
        'prediction_note': 'Every number in `effects` was produced by the project\'s mechanistic '
                           'stand-in and is labelled SIMULATED. It is not a measurement and not '
                           'a promise about a real culture.',
        'evidence_status': project.simulator.get('evidence_status'),
        'seed': seed, 'challenge': challenge,
    }


def render(cmp_):
    """The control-vs-candidate table, as lines a person can read."""
    L = [f'Project: {cmp_["project_id"]} v{cmp_["project_version"]}']
    m = cmp_['model']
    L.append(f'Simulator: {m.get("model_id") or "none"} '
             f'({m.get("status")})' + (f' — {m.get("evidence_status")}'
                                       if m.get('evidence_status') else ''))
    for row in cmp_['handoff']['applied']:
        L.append(f'  MODELLED     {row["parameter_id"]} ({row["label"]}) -> knob {row["knob"]}')
    for row in cmp_['handoff']['skipped']:
        L.append(f'  NOT MODELLED {row["parameter_id"]} ({row["label"]}): {row["reason"]}')
    if cmp_['prediction'] == 'none':
        L.append(f'  {cmp_["prediction_note"]}')
        return '\n'.join(L)
    for c in cmp_['candidate']['changed']:
        L.append(f'  {c["label"]}: {c["from"]:g} -> {c["to"]:g} {c["unit"]}')
    L.append('')
    for e in cmp_['effects']:
        L.append('  ' + E.render(e))
    return '\n'.join(L)
