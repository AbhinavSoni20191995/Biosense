"""Shared synthetic fixtures for package tests (no network, no model credentials)."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / 'examples'


def load(name):
    return json.loads((EX / name).read_text())


def handoff():
    return json.loads((ROOT / 'example.handoff.json').read_text())


def scenario(**values):
    """The committed synthetic scenario with selected parameter values replaced."""
    sc = copy.deepcopy(load('scenario.synthetic.json'))
    for p in sc['parameters']:
        if p['id'] in values:
            p['value'] = float(values[p['id']])
    for s in sc['initial_state']:
        sc['initial_state'][s] = next(p['value'] for p in sc['parameters'] if p['id'] == f'initial.{s}')
    for row in sc['stage_schedule']:
        row['duration'] = next(p['value'] for p in sc['parameters'] if p['id'] == row['input_id'])
    return sc


ALL_RATES = [f'{st}.{r}' for st, rs in {
    'ipsc_expansion': ('mu_R', 'delta_R', 'mu_C', 'delta_C', 'mu_O', 'delta_O'),
    'cardiac_differentiation': ('mu_R', 'delta_R', 'k_C', 'k_O', 'mu_C', 'delta_C', 'mu_O', 'delta_O')}.items() for r in rs]


def zero_rates(**nonzero):
    vals = {r: 0.0 for r in ALL_RATES}
    vals.update(nonzero)
    return scenario(**vals)


def experiment(sc, exp_h=120.0, diff_h=216.0, **kw):
    e = {'schema_version': '1.0', 'experiment_id': f'exp-test-{exp_h:g}', 'scenario_id': sc['scenario_id'],
         'label': 'test', 'role': 'candidate',
         'stage_durations_h': {'ipsc_expansion': float(exp_h), 'cardiac_differentiation': float(diff_h)},
         'harvest': 'end_of_schedule', 'parameter_draw_id': 'nominal', 'parameter_overrides': {}}
    e.update(kw)
    return e
