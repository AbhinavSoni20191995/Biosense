"""Synthetic stand-in for a CAR-T style process: activation, transduction, expansion.

NOT a validated model of T-cell biology and not usable for manufacturing. It is a
phenomenological stand-in whose job is to make the loop's logic testable: the
numbers it returns depend on the protocol's own literature-derived values
(activation duration, cytokine doses, vector timing, seeding density, feeding,
harvest day) and on each arm's hidden genotype, so a wrong recipe produces a
measurable shortfall and a knockout can need different conditions than wild type.

State per step (2 h), in a stirred or rocking bag of declared working volume:

  N     viable T cells per mL
  F     fraction of viable cells carrying the transgene (CAR+)
  E     exhaustion index in [0, 1) - a lumped stand-in for activation-driven
        loss of proliferative capacity, not a measured phenotype
  v     viability fraction

  activation A   = bell(activation_hours, line.activation_optimum_h)
  support    S   = weighted cytokine support, each factor a bell around the
                   line's own optimum, so IL-2 alone supports less expansion
                   than an IL-7 + IL-15 combination
  mu             = mu_max * A * S * (1 - E) * (1 - N/N_max) * genotype_mu_ratio
  dE/dt          = k_E * A_pressure * (1 - 0.35 * il7_fraction_of_S)
                   * genotype_exhaustion_ratio
  death          = death_base + 0.9 * E^2 + feeding shortfall
  F              is set at transduction from vector dose and the day it is added
                   relative to activation, then drifts with differential growth

Everything above is a modelling choice made for this stand-in.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

STAGE_ROLES = ('activation', 'transduction', 'expansion')
CYTOKINES = {
    'il-2': 'il2', 'il2': 'il2', 'interleukin-2': 'il2',
    'il-7': 'il7', 'il7': 'il7', 'interleukin-7': 'il7',
    'il-15': 'il15', 'il15': 'il15', 'interleukin-15': 'il15',
}
ACTIVATORS = {'anti-cd3/cd28', 'anti-cd3/anti-cd28', 'cd3/cd28 beads', 'ok432', 'activation reagent',
              'anti-cd3', 'tcr stimulus'}
VECTORS = {'lentiviral vector', 'lentivirus', 'retroviral vector', 'car vector', 'vector'}
CYTOKINE_UNITS = {'ng/mL': 1.0, 'ug/mL': 1000.0, 'IU/mL': 1.0}
N_MAX = 4.0e6          # cells/mL density ceiling of the bag (modelling choice)
MU_CAP = 0.038         # 1/h at full activation and full cytokine support
K_EXH = 0.0026         # per hour exhaustion accrual at full activation pressure
# Cytokine support weights. Chosen so that IL-7 + IL-15 together give roughly the
# 1.8-fold total expansion over IL-2 alone that this fixture's synthetic source
# reports (c12). They are a modelling choice, not a measured potency ranking.
SUPPORT_WEIGHTS = {'il2': 0.78, 'il7': 0.504, 'il15': 0.396}
SCENARIOS = ('clean', 'poor_viability', 'low_transduction')


@dataclass
class TCellLine:
    """Hidden per-line biology. Agents never see any of this."""
    line_id: str = 'D01'
    mu_max: float = MU_CAP
    death_base: float = 0.0016
    activation_optimum_h: float = 48.0
    activation_tol_h: float = 26.0
    cytokine_optima: Dict[str, float] = field(default_factory=lambda: {'il2': 100.0, 'il7': 10.0, 'il15': 5.0})
    cytokine_tol: Dict[str, float] = field(default_factory=lambda: {'il2': 70.0, 'il7': 7.0, 'il15': 6.0})
    transduction_max: float = 0.46
    vector_optimum_day_after_activation: float = 2.0
    # engineered-genotype knobs; defaults are wild type
    genotype_mu_ratio: float = 1.0
    genotype_exhaustion_ratio: float = 1.0

    @staticmethod
    def sample(rng: np.random.Generator, line_id: str) -> 'TCellLine':
        """Donor-to-donor variation, which is why a single replicate is directional only."""
        L = TCellLine(line_id=line_id)
        L.mu_max = MU_CAP * float(rng.normal(1.0, 0.06))
        L.death_base = 0.0016 * float(rng.normal(1.0, 0.12))
        L.transduction_max = 0.46 * float(rng.normal(1.0, 0.07))
        return L


def _bell(x: float, opt: float, tol: float) -> float:
    return float(math.exp(-0.5 * ((x - opt) / max(tol, 1e-6)) ** 2))


def _num(q):
    if not isinstance(q, dict) or q.get('provenance') == 'gap':
        return None
    v = q.get('value')
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _cytokine_dose(q, notes, step_id, name):
    v = _num(q)
    if v is None:
        notes.append(f'{step_id}: {name} has no value; treated as absent')
        return 0.0
    unit = q.get('unit')
    if unit not in CYTOKINE_UNITS:
        notes.append(f'{step_id}: {name} unit {unit!r} not understood by the stand-in; treated as absent')
        return 0.0
    return v * (CYTOKINE_UNITS[unit] if unit != 'IU/mL' else 1.0)


def arm_steps(protocol: Dict, arm_id: str) -> List[Dict]:
    """Base steps with this arm's adjustments applied."""
    adj: Dict[str, list] = {}
    for a in protocol.get('arm_adjustments', []):
        if a['arm_id'] == arm_id:
            adj.setdefault(a['step_id'], []).append(a)
    out = []
    for st in protocol['stages']:
        for step in st['steps']:
            s = copy.deepcopy(step)
            s['stage_id'] = st['stage_id']
            s['stage_start'] = st['start_day']
            s['stage_end'] = st['end_day']
            dropped = False
            for a in adj.get(step['step_id'], []):
                if a.get('omit'):
                    dropped = True
                if 'quantity' in a:
                    s['quantity'] = a['quantity']
                if a.get('day_shift'):
                    s['day'] += a['day_shift']
                    if s.get('end_day') is not None:
                        s['end_day'] += a['day_shift']
            if not dropped:
                out.append(s)
    return out


def protocol_to_inputs(protocol: Dict, arm_id: str):
    """Map one arm's protocol onto the stand-in's inputs.

    Returns (inputs, notes). Raises ValueError when the protocol cannot be mapped.
    """
    stages = protocol['stages']
    if len(stages) != 3:
        raise ValueError(f'the tcell stand-in models exactly 3 stages {STAGE_ROLES}; protocol has {len(stages)}')
    notes: List[str] = []
    cs = protocol['culture_system']
    volume = _num(cs['working_volume'])
    if volume is None or cs['working_volume']['unit'] != 'mL':
        raise ValueError('the tcell stand-in needs culture_system.working_volume in mL')

    inp = {'volume_ml': volume, 'seed_per_ml': None, 'activation_h': None, 'activation_present': False,
           'vector_day': None, 'vector_dose': None, 'harvest_day': None,
           'cytokines': {'il2': [], 'il7': [], 'il15': []}, 'feed_fraction': 0.5}

    ff = cs['parameters'].get('feed_fraction')
    if ff is not None:
        v = _num(ff)
        if v is None or ff['unit'] not in ('fraction', '%'):
            notes.append('feed_fraction has no usable value; stand-in default used')
        else:
            inp['feed_fraction'] = v * (0.01 if ff['unit'] == '%' else 1.0)
    for name in cs['parameters']:
        if name not in ('feed_fraction', 'temperature', 'dissolved_oxygen', 'agitation_rpm', 'rocking_rate'):
            notes.append(f'culture parameter {name!r} not simulated')

    for s in arm_steps(protocol, arm_id):
        q, sid = s.get('quantity'), s['step_id']
        factor = (s.get('factor') or '').strip().lower()
        if s['action'] == 'seed':
            v = _num(q)
            if v is None or q['unit'] != 'cells/mL':
                notes.append(f'{sid}: seeding density missing or not in cells/mL; stand-in default used')
                inp['seed_per_ml'] = 1.0e6
            else:
                inp['seed_per_ml'] = v
        elif s['action'] == 'add_factor' and factor in CYTOKINES:
            key = CYTOKINES[factor]
            dose = _cytokine_dose(q, notes, sid, factor)
            end = s['end_day'] if s.get('end_day') is not None else s['stage_end']
            inp['cytokines'][key].append((s['day'], end, dose))
        elif s['action'] == 'add_factor' and factor in ACTIVATORS:
            inp['activation_present'] = True
            end = s['end_day'] if s.get('end_day') is not None else s['stage_end']
            inp['activation_h'] = (end - s['day']) * 24.0
        elif s['action'] == 'add_factor' and factor in VECTORS:
            inp['vector_day'] = s['day']
            inp['vector_dose'] = _num(q)
            if inp['vector_dose'] is None:
                notes.append(f'{sid}: vector dose missing; nominal dose assumed')
        elif s['action'] == 'remove_factor' and factor in ACTIVATORS:
            inp['activation_h'] = (s['day'] - stages[0]['start_day']) * 24.0
        elif s['action'] == 'harvest':
            inp['harvest_day'] = s['day']
        elif s['action'] not in ('sample', 'feed', 'medium_exchange', 'start_harvest', 'set_parameter'):
            notes.append(f'step {sid} ({s["action"]}) not simulated')

    if inp['seed_per_ml'] is None:
        raise ValueError('protocol has no seed step the stand-in can read')
    if inp['harvest_day'] is None:
        inp['harvest_day'] = stages[-1]['end_day']
    if not inp['activation_present']:
        notes.append('no activation reagent found; activation strength falls back to the stage length')
        inp['activation_h'] = (stages[0]['end_day'] - stages[0]['start_day']) * 24.0
    if inp['vector_day'] is None:
        notes.append('no vector step found; transduction is modelled as absent (CAR+ fraction 0)')
    return inp, sorted(set(notes))


def _dose_at(windows, day):
    return max((d for (a, b, d) in windows if a - 1e-9 <= day <= b + 1e-9), default=0.0)


def _support(line: TCellLine, inp, day):
    """Weighted cytokine support. IL-7 + IL-15 together exceed what IL-2 alone gives."""
    w = SUPPORT_WEIGHTS
    parts = {}
    for k, weight in w.items():
        dose = _dose_at(inp['cytokines'][k], day)
        parts[k] = weight * _bell(dose, line.cytokine_optima[k], line.cytokine_tol[k]) if dose > 0 else 0.0
    total = min(1.12, sum(parts.values()))
    il7_share = parts['il7'] / total if total > 0 else 0.0
    return total, il7_share


def simulate_arm(protocol: Dict, arm_id: str, line: TCellLine, rng: np.random.Generator,
                 scenario: str = 'clean'):
    inp, notes = protocol_to_inputs(protocol, arm_id)
    dt = 2.0
    D = inp['seed_per_ml']          # viable cells per mL
    V = inp['volume_ml']            # culture volume, which grows with feeding
    V0 = V
    F, E, viab = 0.0, 0.0, 0.96
    A = _bell(inp['activation_h'], line.activation_optimum_h, line.activation_tol_h)
    # Longer stimulation drives exhaustion whether or not it helps proliferation.
    a_pressure = min(1.4, inp['activation_h'] / 48.0)
    expansion_start = protocol['stages'][-1]['start_day']
    transduced = False
    series = []
    day = 0.0
    steps = int(round(inp['harvest_day'] * 24.0 / dt))
    for _ in range(steps):
        S, il7_share = _support(line, inp, day)
        feed_ok = min(1.0, inp['feed_fraction'] / 0.4)
        mu = (line.mu_max * A * S * (1.0 - E) * max(0.0, 1.0 - D / N_MAX)
              * line.genotype_mu_ratio * feed_ok)
        dE = (K_EXH * a_pressure * (1.0 - 0.35 * il7_share) * line.genotype_exhaustion_ratio)
        E = min(0.92, E + dE * dt / 24.0)
        death = line.death_base + 0.9 * E * E + 0.004 * max(0.0, 0.4 - inp['feed_fraction'])
        if scenario == 'poor_viability':
            death *= 2.4
        D = max(D + (mu - death) * D * dt, 1.0)
        target_v = float(np.clip(1.0 - 22.0 * death, 0.05, 0.985))
        viab = float(np.clip(viab + 0.07 * (target_v - viab) * dt, 0.05, 0.985))
        if not transduced and inp['vector_day'] is not None and day >= inp['vector_day'] - 1e-9:
            timing = _bell(inp['vector_day'], line.vector_optimum_day_after_activation, 1.3)
            dose_f = 1.0 if inp['vector_dose'] is None else _bell(inp['vector_dose'], 1.0, 0.9)
            F = line.transduction_max * timing * max(0.35, A) * dose_f
            if scenario == 'low_transduction':
                F *= 0.45
            transduced = True
        day += dt / 24.0
        # Fed-batch volume expansion: once the culture approaches its density
        # ceiling it is split into more medium, so total yield keeps rising while
        # density does not. This is what makes feed_fraction a real lever.
        if day >= expansion_start and abs(day - round(day)) < dt / 48.0 and D > 0.55 * N_MAX:
            f = 1.0 + inp['feed_fraction']
            V *= f
            D /= f
        if abs(day - round(day)) < dt / 48.0:
            series.append({'day': round(day, 2), 'D_per_ml': D, 'V_ml': V, 'viability': viab, 'car_fraction': F})
    total = D * V
    return {
        'notes': notes,
        'volume_ml': V,
        'initial_volume_ml': V0,
        'input_cells': round(inp['seed_per_ml'] * V0, 0),
        'harvest_day': inp['harvest_day'],
        'viable_cells_total': round(total, 0),
        'final_density_per_ml': round(D, 0),
        'viability_pct': round(viab * 100.0 * float(rng.normal(1.0, 0.012)), 2),
        'car_pct': round(min(99.0, F * 100.0 * float(rng.normal(1.0, 0.04))), 2),
        'fold_expansion': round(total / (inp['seed_per_ml'] * V0), 2),
        'exhaustion_index': round(E, 3),
        'series': series,
    }


def _apply_truth(line: TCellLine, arm: Dict, truth: Optional[Dict]) -> None:
    t = (truth or {}).get('arms', {}).get(arm['arm_id'])
    if not t:
        return
    for k in ('genotype_mu_ratio', 'genotype_exhaustion_ratio', 'activation_optimum_h',
              'activation_tol_h', 'transduction_max'):
        if k in t:
            setattr(line, k, float(t[k]))
    line.cytokine_optima.update({k: float(v) for k, v in t.get('cytokine_optima', {}).items()})
    line.cytokine_tol.update({k: float(v) for k, v in t.get('cytokine_tol', {}).items()})


def simulate_protocol(protocol: Dict, protocol_sha256: str, mode: str = 'minimal', seed: int = 7,
                      scenario: str = 'clean', truth: Optional[Dict] = None, replicates: int = 1,
                      release_tests: Optional[bool] = None, run_id: Optional[str] = None) -> Dict:
    """Run every genotype arm of a CAR-T style protocol. Minimal measurement mode only."""
    if mode != 'minimal':
        raise ValueError('the tcell stand-in reports minimal mode only; the integrated-machine '
                         'max mode is modelled for the iPSC -> monocyte process (analysis_agent)')
    if scenario not in SCENARIOS:
        raise ValueError(f'scenario must be one of {SCENARIOS}')
    release_tests = bool(release_tests)
    rng = np.random.default_rng(seed)
    base = TCellLine.sample(rng, 'D01')
    arms, notes_all = [], set()
    for ai, arm in enumerate(protocol['genotype_arms']):
        for rep in range(1, replicates + 1):
            line = copy.deepcopy(base)
            line.line_id = arm.get('line_id') or f'D01-{arm["arm_id"]}'
            _apply_truth(line, arm, truth)
            r = simulate_arm(protocol, arm['arm_id'], line,
                             np.random.default_rng(seed * 1000 + ai * 10 + rep), scenario)
            notes_all.update(r['notes'])
            rec = {
                'arm_id': arm['arm_id'], 'replicate': rep, 'line_id': line.line_id, 'passage_number': None,
                'input_cells': r['input_cells'], 'deviations': [],
                'timepoints': [{'day': s['day'],
                                'viable_cells_total': round(s['D_per_ml'] * s['V_ml'], 0),
                                'viability_pct': round(s['viability'] * 100, 2)}
                               for s in r['series'] if float(s['day']).is_integer() and int(s['day']) % 2 == 0],
                'harvest': {
                    'day': r['harvest_day'],
                    'viable_cells_total': r['viable_cells_total'],
                    'viability_pct': r['viability_pct'],
                    'target_marker_pct': r['car_pct'],
                    'marker_method': 'flow cytometry, CAR detection reagent (synthetic stand-in)',
                    'residual_pluripotency_pct': None,
                    'residual_pluripotency_marker': None,
                    'harvest_onset_day': None,
                },
            }
            if release_tests:
                rec['qc_tests'] = {'sterility': 'no_growth', 'mycoplasma': 'not_detected',
                                   'endotoxin_EU_per_mL': 0.08, 'karyotype': 'normal',
                                   'karyotype_detail': 'vector copy number 2.1 per cell (synthetic stand-in)'}
            arms.append(rec)
    return {
        'schema_version': '2.0',
        'run_id': run_id or f'standin-{protocol["protocol_id"]}-{scenario}-s{seed}',
        'protocol_id': protocol['protocol_id'],
        'protocol_sha256': protocol_sha256,
        'mode': 'minimal',
        'source': 'synthetic_standin',
        'label': f'SYNTHETIC CAR-T STAND-IN ({scenario}); workflow demonstration, not biological evidence',
        'operator': None,
        'started_at': None,
        'notes': '; '.join(sorted(notes_all)) or 'all protocol elements mapped to the stand-in',
        'arms': arms,
    }
