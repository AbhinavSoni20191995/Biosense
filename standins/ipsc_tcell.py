"""Synthetic stand-in for an iPSC -> T-lineage process: differentiation then expansion.

NOT a validated model of T-cell development and not usable for manufacturing. It
is a phenomenological stand-in whose only job is to make the loop's logic
testable: the numbers it returns depend on the protocol's own values (Notch
ligand presentation, stage-specific factor doses, TCR agonist strength, stage
timing, seeding density, feeding, harvest day) and on each arm's hidden
genotype, so a wrong recipe produces a measurable shortfall and a knockout can
need different conditions than wild type.

Five stages, in this order:

  mesoderm              iPSC aggregates commit toward mesoderm
  hemogenic_endothelium  endothelial-to-haematopoietic transition
  t_commitment          Notch-driven commitment to the T lineage
  maturation            TCR/CD3 agonist drives maturation
  expansion             homeostatic cytokines expand the committed pool

State per step (2 h), in a vessel of declared working volume:

  U   residual undifferentiated iPSC per mL - the tumorigenicity QC readout
  P   progenitor cells per mL (mesoderm through pro-T, one lumped pool)
  T   T-lineage committed cells per mL
  X   differentiation index in [0, 1) - a lumped stand-in for how far the
      committed pool has run toward a terminal effector state. It is NOT a
      measured phenotype and nothing in the loop can observe it directly.
  v   viability fraction

  notch     = bell(dll4_dose, line.dll4_optimum)          Notch drive
  support_s = weighted stage-appropriate factor support, each factor a bell
              around the line's own optimum
  tcr       = bell(agonist_dose, line.tcr_optimum)        maturation drive
  commit    = k_commit * notch * support_s * genotype_commitment_ratio
  mu_T      = mu_cap * support_s * (1 - X) * (1 - N/N_max) * genotype_mu_ratio
  dX/dt     = k_dif * (tcr_pressure + 0.3 * effector_push)
              * (1 - 0.4 * il7_share) * genotype_differentiation_ratio

The key coupling is `mu_T` falling as `X` rises: a population that has run
further toward a terminal state sustains less expansion late in the window. That
is what lets a genotype which differentiates faster look similar early and fall
behind late. Every coefficient above is a modelling choice made for this
stand-in, not a measured parameter, and the hidden per-arm genotype knobs are
invented. A loop that "discovers" one of them has demonstrated the workflow and
nothing about biology.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

STAGE_ROLES = ('mesoderm', 'hemogenic_endothelium', 't_commitment', 'maturation', 'expansion')

# Factor name -> internal key. Anything not listed here is reported as not simulated
# rather than silently ignored, so a protocol cannot quietly do nothing.
FACTORS = {
    'bmp4': 'bmp4', 'bmp-4': 'bmp4',
    'vegf': 'vegf', 'vegf-a': 'vegf', 'vegfa': 'vegf',
    'fgf2': 'fgf2', 'bfgf': 'fgf2', 'fgf-2': 'fgf2',
    'chir99021': 'wnt', 'chir': 'wnt',
    'scf': 'scf', 'kit ligand': 'scf', 'kitl': 'scf',
    'flt3l': 'flt3l', 'flt3-l': 'flt3l', 'flt3 ligand': 'flt3l',
    'il-7': 'il7', 'il7': 'il7', 'interleukin-7': 'il7',
    'il-15': 'il15', 'il15': 'il15', 'interleukin-15': 'il15',
    'il-2': 'il2', 'il2': 'il2', 'interleukin-2': 'il2',
    'tpo': 'tpo', 'thrombopoietin': 'tpo',
    'sb431542': 'tgfbi',
}
NOTCH_LIGANDS = {'dll4', 'dll-4', 'delta-like 4', 'dll4/vcam1', 'dll1', 'notch ligand', 'dll4 + vcam1'}
TCR_AGONISTS = {'anti-cd3', 'anti-cd3/cd28', 'anti-cd3/anti-cd28', 'cd3/cd28 beads', 'tcr stimulus',
                'tcr agonist', 'anti-cd3e', 'okt3'}
# Dose units the stand-in understands, normalised to ng/mL for protein factors.
DOSE_UNITS = {'ng/mL': 1.0, 'ug/mL': 1000.0, 'IU/mL': 1.0, 'uM': 1.0, 'nM': 0.001, 'ug/cm2': 1.0}

N_MAX = 3.2e6          # cells/mL density ceiling of the vessel (modelling choice)
MU_CAP = 0.012         # 1/h expansion ceiling of the committed pool (~58 h doubling)
K_COMMIT = 0.055       # per hour ceiling on progenitor -> T-lineage commitment
K_MESO = 0.070         # per hour ceiling on iPSC -> progenitor commitment
K_DIF = 0.0021         # per hour differentiation-index accrual at full TCR pressure
# The vessel is finite: fed-batch splitting stops at this multiple of the starting
# working volume, after which density caps and total yield stops rising. Without
# this the stand-in would report an unbounded harvest.
MAX_VOLUME_FACTOR = 10.0
# Residual undifferentiated cells are lost slowly once their medium is gone, not
# cleared. Keeping this small is what leaves a measurable residual-pluripotency
# readout at harvest, which is the safety QC this product is judged on.
K_UNDIFF_LOSS = 0.0002  # per hour
# Stage-appropriate factor support weights. Chosen so that no single factor can
# carry a stage on its own and so the combinations the literature treats as
# standard score highest. A modelling choice, not a measured potency ranking.
SUPPORT_WEIGHTS = {
    'mesoderm': {'bmp4': 0.42, 'vegf': 0.30, 'fgf2': 0.18, 'wnt': 0.16},
    'hemogenic_endothelium': {'vegf': 0.40, 'scf': 0.28, 'fgf2': 0.20, 'tgfbi': 0.12},
    't_commitment': {'scf': 0.30, 'flt3l': 0.28, 'il7': 0.34, 'tpo': 0.10},
    'maturation': {'il7': 0.62, 'scf': 0.18, 'flt3l': 0.14},
    'expansion': {'il7': 0.46, 'il15': 0.38, 'il2': 0.30},
}
SCENARIOS = ('clean', 'poor_viability', 'weak_commitment')


@dataclass
class IPSCTLine:
    """Hidden per-line biology. Agents never see any of this."""
    line_id: str = 'iPSC-01'
    mu_max: float = MU_CAP
    death_base: float = 0.0018
    # Notch drive: DLL4 surface density the line commits best at (ng/mL equivalent).
    dll4_optimum: float = 500.0
    dll4_tol: float = 420.0
    # TCR agonist strength the line matures best at, as ng/mL equivalent.
    tcr_optimum: float = 50.0
    tcr_tol: float = 38.0
    factor_optima: Dict[str, float] = field(default_factory=lambda: {
        'bmp4': 20.0, 'vegf': 50.0, 'fgf2': 20.0, 'wnt': 4.0, 'scf': 50.0,
        'flt3l': 10.0, 'il7': 10.0, 'il15': 10.0, 'il2': 50.0, 'tpo': 20.0, 'tgfbi': 5.0})
    factor_tol: Dict[str, float] = field(default_factory=lambda: {
        'bmp4': 16.0, 'vegf': 40.0, 'fgf2': 18.0, 'wnt': 3.2, 'scf': 42.0,
        'flt3l': 9.0, 'il7': 8.0, 'il15': 9.0, 'il2': 45.0, 'tpo': 20.0, 'tgfbi': 5.0})
    # Fraction of the starting pool that never leaves the undifferentiated state.
    refractory_fraction: float = 0.055
    # engineered-genotype knobs; defaults are wild type
    genotype_mu_ratio: float = 1.0
    genotype_commitment_ratio: float = 1.0
    genotype_differentiation_ratio: float = 1.0

    @staticmethod
    def sample(rng: np.random.Generator, line_id: str) -> 'IPSCTLine':
        """Line-to-line variation, which is why a single replicate is directional only."""
        L = IPSCTLine(line_id=line_id)
        L.mu_max = MU_CAP * float(rng.normal(1.0, 0.055))
        L.death_base = 0.0018 * float(rng.normal(1.0, 0.11))
        L.refractory_fraction = float(np.clip(0.055 * rng.normal(1.0, 0.18), 0.005, 0.2))
        return L


def _bell(x: float, opt: float, tol: float) -> float:
    return float(math.exp(-0.5 * ((x - opt) / max(tol, 1e-6)) ** 2))


def _num(q):
    if not isinstance(q, dict) or q.get('provenance') == 'gap':
        return None
    v = q.get('value')
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _dose(q, notes, step_id, name):
    """A factor dose normalised to the stand-in's internal scale, or 0.0 when unusable."""
    v = _num(q)
    if v is None:
        notes.append(f'{step_id}: {name} has no value; treated as absent')
        return 0.0
    unit = q.get('unit')
    if unit not in DOSE_UNITS:
        notes.append(f'{step_id}: {name} unit {unit!r} not understood by the stand-in; treated as absent')
        return 0.0
    return v * DOSE_UNITS[unit]


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


def arm_stages(protocol: Dict, arm_id: str) -> List[Dict]:
    """Stage windows with this arm's stage_shift adjustments applied.

    An arm_adjustment whose step_id names a stage_id and which carries a
    day_shift moves that stage's end, and every later stage with it. That is how
    an arm gets a shorter or longer expansion than the control.
    """
    stages = [{'stage_id': st['stage_id'], 'start_day': st['start_day'], 'end_day': st['end_day']}
              for st in protocol['stages']]
    by_id = {st['stage_id']: i for i, st in enumerate(stages)}
    for a in protocol.get('arm_adjustments', []):
        if a['arm_id'] != arm_id or not a.get('day_shift'):
            continue
        i = by_id.get(a['step_id'])
        if i is None:
            continue
        shift = float(a['day_shift'])
        stages[i]['end_day'] += shift
        for j in range(i + 1, len(stages)):
            stages[j]['start_day'] += shift
            stages[j]['end_day'] += shift
    return stages


def protocol_to_inputs(protocol: Dict, arm_id: str):
    """Map one arm's protocol onto the stand-in's inputs.

    Returns (inputs, notes). Raises ValueError when the protocol cannot be mapped.
    """
    stages = arm_stages(protocol, arm_id)
    if len(stages) != 5:
        raise ValueError(f'the ipsc_tcell stand-in models exactly 5 stages {STAGE_ROLES}; '
                         f'protocol has {len(stages)}')
    notes: List[str] = []
    cs = protocol['culture_system']
    volume = _num(cs['working_volume'])
    if volume is None or cs['working_volume']['unit'] != 'mL':
        raise ValueError('the ipsc_tcell stand-in needs culture_system.working_volume in mL')

    # One exposure-window list per internal factor key, so a factor that is never
    # added simply has an empty window rather than a missing one.
    inp = {'volume_ml': volume, 'seed_per_ml': None, 'harvest_day': None, 'feed_fraction': 0.5,
           'stages': stages, 'notch': [], 'tcr': [],
           'windows': {k: [] for k in ('bmp4', 'vegf', 'fgf2', 'wnt', 'scf', 'flt3l',
                                       'il7', 'il15', 'il2', 'tpo', 'tgfbi')}}

    ff = cs['parameters'].get('feed_fraction')
    if ff is not None:
        v = _num(ff)
        if v is None or ff['unit'] not in ('fraction', '%'):
            notes.append('feed_fraction has no usable value; stand-in default used')
        else:
            inp['feed_fraction'] = v * (0.01 if ff['unit'] == '%' else 1.0)
    for name in cs['parameters']:
        if name not in ('feed_fraction', 'temperature', 'dissolved_oxygen', 'agitation_rpm',
                        'rocking_rate', 'aggregate_diameter'):
            notes.append(f'culture parameter {name!r} not simulated')

    for s in arm_steps(protocol, arm_id):
        q, sid = s.get('quantity'), s['step_id']
        factor = (s.get('factor') or '').strip().lower()
        end = s['end_day'] if s.get('end_day') is not None else s['stage_end']
        if s['action'] == 'seed':
            v = _num(q)
            if v is None or q['unit'] != 'cells/mL':
                notes.append(f'{sid}: seeding density missing or not in cells/mL; stand-in default used')
                inp['seed_per_ml'] = 2.0e5
            else:
                inp['seed_per_ml'] = v
        elif s['action'] == 'add_factor' and factor in NOTCH_LIGANDS:
            inp['notch'].append((s['day'], end, _dose(q, notes, sid, factor)))
        elif s['action'] == 'add_factor' and factor in TCR_AGONISTS:
            inp['tcr'].append((s['day'], end, _dose(q, notes, sid, factor)))
        elif s['action'] == 'add_factor' and factor in FACTORS:
            inp['windows'][FACTORS[factor]].append((s['day'], end, _dose(q, notes, sid, factor)))
        elif s['action'] == 'remove_factor' and factor in TCR_AGONISTS:
            inp['tcr'] = [(a, min(b, s['day']), d) for (a, b, d) in inp['tcr']]
        elif s['action'] == 'remove_factor' and factor in NOTCH_LIGANDS:
            inp['notch'] = [(a, min(b, s['day']), d) for (a, b, d) in inp['notch']]
        elif s['action'] == 'remove_factor' and factor in FACTORS:
            k = FACTORS[factor]
            inp['windows'][k] = [(a, min(b, s['day']), d) for (a, b, d) in inp['windows'][k]]
        elif s['action'] == 'harvest':
            inp['harvest_day'] = s['day']
        elif s['action'] == 'add_factor':
            notes.append(f'{sid}: factor {factor!r} not simulated by this stand-in')
        elif s['action'] not in ('sample', 'feed', 'medium_exchange', 'start_harvest',
                                 'set_parameter', 'passage'):
            notes.append(f'step {sid} ({s["action"]}) not simulated')

    if inp['seed_per_ml'] is None:
        raise ValueError('protocol has no seed step the stand-in can read')
    if inp['harvest_day'] is None:
        inp['harvest_day'] = stages[-1]['end_day']
    if not inp['notch']:
        notes.append('no Notch ligand step found; T-lineage commitment is modelled as near-absent')
    if not inp['tcr']:
        notes.append('no TCR agonist step found; maturation drive falls back to a weak default')
    return inp, sorted(set(notes))


def _dose_at(windows, day):
    return max((d for (a, b, d) in windows if a - 1e-9 <= day <= b + 1e-9), default=0.0)


def _stage_at(stages, day):
    for st in stages:
        if st['start_day'] - 1e-9 <= day <= st['end_day'] + 1e-9:
            return st['stage_id']
    return stages[-1]['stage_id'] if day > stages[-1]['end_day'] else stages[0]['stage_id']


def _support(line: IPSCTLine, inp, day, role):
    """Weighted factor support for the stage *role*, plus IL-7's share of it."""
    weights = SUPPORT_WEIGHTS.get(role, SUPPORT_WEIGHTS['expansion'])
    parts = {}
    for k, w in weights.items():
        dose = _dose_at(inp['windows'][k], day)
        parts[k] = w * _bell(dose, line.factor_optima[k], line.factor_tol[k]) if dose > 0 else 0.0
    total = min(1.10, sum(parts.values()))
    il7_share = parts.get('il7', 0.0) / total if total > 0 else 0.0
    return total, il7_share


def simulate_arm(protocol: Dict, arm_id: str, line: IPSCTLine, rng: np.random.Generator,
                 scenario: str = 'clean'):
    inp, notes = protocol_to_inputs(protocol, arm_id)
    dt = 2.0
    stages = inp['stages']
    roles = [st['stage_id'] for st in stages]
    V = inp['volume_ml']
    V0 = V
    # The starting population is iPSC. A fixed fraction never leaves that state.
    U = inp['seed_per_ml']
    P = 0.0
    T = 0.0
    X = 0.0
    viab = 0.96
    refractory = U * line.refractory_fraction
    series = []
    day = 0.0
    steps = int(round(inp['harvest_day'] * 24.0 / dt))
    for _ in range(steps):
        role = _stage_at(stages, day)
        S, il7_share = _support(line, inp, day, role)
        notch = _bell(_dose_at(inp['notch'], day), line.dll4_optimum, line.dll4_tol) \
            if _dose_at(inp['notch'], day) > 0 else 0.0
        tcr_dose = _dose_at(inp['tcr'], day)
        tcr = _bell(tcr_dose, line.tcr_optimum, line.tcr_tol) if tcr_dose > 0 else 0.0
        tcr_pressure = min(1.5, tcr_dose / max(line.tcr_optimum, 1e-6)) if tcr_dose > 0 else 0.0
        feed_ok = min(1.0, inp['feed_fraction'] / 0.4)
        N = U + P + T
        crowd = max(0.0, 1.0 - N / N_MAX)

        # iPSC -> progenitor, only while the early stages are running.
        meso = K_MESO * S * crowd if role in ('mesoderm', 'hemogenic_endothelium') else 0.0
        leaving = min(max(U - refractory, 0.0), meso * max(U - refractory, 0.0) * dt)
        U -= leaving
        P += leaving
        # Progenitor -> T lineage, Notch-gated.
        commit = (K_COMMIT * notch * S * line.genotype_commitment_ratio
                  if role in ('t_commitment', 'maturation') else 0.0)
        if scenario == 'weak_commitment':
            commit *= 0.4
        moved = min(P, commit * P * dt)
        P -= moved
        T += moved

        # Differentiation index: TCR pressure pushes the committed pool toward a
        # terminal state, IL-7 support partly offsets it, and the genotype scales it.
        effector_push = 1.0 if role == 'expansion' else 0.0
        dX = (K_DIF * (tcr_pressure + 0.3 * effector_push) * (1.0 - 0.4 * il7_share)
              * line.genotype_differentiation_ratio)
        X = min(0.94, X + dX * dt / 24.0)

        # Expansion of the committed pool; progenitors expand more slowly.
        mu_T = line.mu_max * S * (1.0 - X) * crowd * line.genotype_mu_ratio * feed_ok
        if role == 'maturation':
            mu_T *= 0.55 + 0.45 * tcr
        mu_P = 0.45 * line.mu_max * S * crowd * feed_ok if role != 'expansion' else 0.0
        death = line.death_base + 0.85 * X * X + 0.004 * max(0.0, 0.4 - inp['feed_fraction'])
        if scenario == 'poor_viability':
            death *= 2.3
        # Undifferentiated cells are slowly lost once their medium is gone.
        U = max(U * (1.0 - K_UNDIFF_LOSS * dt) if role != 'mesoderm' else U, 0.0)
        T = max(T + (mu_T - death) * T * dt, 0.0)
        P = max(P + (mu_P - death) * P * dt, 0.0)
        target_v = float(np.clip(1.0 - 20.0 * death, 0.05, 0.985))
        viab = float(np.clip(viab + 0.07 * (target_v - viab) * dt, 0.05, 0.985))

        day += dt / 24.0
        # Fed-batch volume expansion once the culture approaches its ceiling, so
        # total yield keeps rising while density does not.
        if (abs(day - round(day)) < dt / 48.0 and (U + P + T) > 0.55 * N_MAX
                and V < V0 * MAX_VOLUME_FACTOR):
            f = min(1.0 + inp['feed_fraction'], V0 * MAX_VOLUME_FACTOR / V)
            V *= f
            U, P, T = U / f, P / f, T / f
            refractory /= f
        if abs(day - round(day)) < dt / 48.0:
            series.append({'day': round(day, 2), 'U': U, 'P': P, 'T': T, 'V_ml': V,
                           'viability': viab, 'dif_index': X, 'stage': role})

    N = U + P + T
    total = N * V
    t_pct = (T / N * 100.0) if N > 0 else 0.0
    u_pct = (U / N * 100.0) if N > 0 else 0.0
    input_cells = round(inp['seed_per_ml'] * V0, 0)
    return {
        'notes': notes,
        'roles': roles,
        'volume_ml': V,
        'initial_volume_ml': V0,
        'input_cells': input_cells,
        'harvest_day': inp['harvest_day'],
        'viable_cells_total': round(total, 0),
        'final_density_per_ml': round(N, 0),
        'viability_pct': round(viab * 100.0 * float(rng.normal(1.0, 0.012)), 2),
        'target_marker_pct': round(float(np.clip(t_pct * float(rng.normal(1.0, 0.035)), 0.0, 99.0)), 2),
        'residual_pluripotency_pct': round(float(np.clip(u_pct * float(rng.normal(1.0, 0.09)), 0.0, 100.0)), 3),
        'fold_expansion': round(total / max(input_cells, 1.0), 2),
        'dif_index': round(X, 3),
        'series': series,
    }


def _apply_truth(line: IPSCTLine, arm: Dict, truth: Optional[Dict]) -> None:
    t = (truth or {}).get('arms', {}).get(arm['arm_id'])
    if not t:
        return
    for k in ('genotype_mu_ratio', 'genotype_commitment_ratio', 'genotype_differentiation_ratio',
              'dll4_optimum', 'dll4_tol', 'tcr_optimum', 'tcr_tol', 'refractory_fraction',
              'mu_max', 'death_base'):
        if k in t:
            setattr(line, k, float(t[k]))
    line.factor_optima.update({k: float(v) for k, v in t.get('factor_optima', {}).items()})
    line.factor_tol.update({k: float(v) for k, v in t.get('factor_tol', {}).items()})


def simulate_protocol(protocol: Dict, protocol_sha256: str, mode: str = 'minimal', seed: int = 11,
                      scenario: str = 'clean', truth: Optional[Dict] = None, replicates: int = 1,
                      release_tests: Optional[bool] = None, run_id: Optional[str] = None) -> Dict:
    """Run every genotype arm of an iPSC -> T-lineage protocol. Minimal mode only."""
    if mode != 'minimal':
        raise ValueError('the ipsc_tcell stand-in reports minimal mode only; the integrated-machine '
                         'max mode is modelled for the iPSC -> monocyte process (analysis_agent)')
    if scenario not in SCENARIOS:
        raise ValueError(f'scenario must be one of {SCENARIOS}')
    release_tests = bool(release_tests)
    rng = np.random.default_rng(seed)
    base = IPSCTLine.sample(rng, 'iPSC-01')
    arms, notes_all = [], set()
    for ai, arm in enumerate(protocol['genotype_arms']):
        for rep in range(1, replicates + 1):
            line = copy.deepcopy(base)
            line.line_id = arm.get('line_id') or f'iPSC-01-{arm["arm_id"]}'
            _apply_truth(line, arm, truth)
            r = simulate_arm(protocol, arm['arm_id'], line,
                             np.random.default_rng(seed * 1000 + ai * 10 + rep), scenario)
            notes_all.update(r['notes'])
            rec = {
                'arm_id': arm['arm_id'], 'replicate': rep, 'line_id': line.line_id, 'passage_number': None,
                'input_cells': r['input_cells'], 'deviations': [],
                'timepoints': [{'day': s['day'],
                                'viable_cells_total': round((s['U'] + s['P'] + s['T']) * s['V_ml'], 0),
                                'viability_pct': round(s['viability'] * 100, 2)}
                               for s in r['series']
                               if float(s['day']).is_integer() and int(s['day']) % 5 == 0],
                'harvest': {
                    'day': r['harvest_day'],
                    'viable_cells_total': r['viable_cells_total'],
                    'viability_pct': r['viability_pct'],
                    'target_marker_pct': r['target_marker_pct'],
                    'marker_method': 'flow cytometry, T-lineage identity marker (synthetic stand-in)',
                    'residual_pluripotency_pct': r['residual_pluripotency_pct'],
                    'residual_pluripotency_marker': 'TRA-1-60 (synthetic stand-in)',
                    'harvest_onset_day': None,
                },
            }
            if release_tests:
                rec['qc_tests'] = {'sterility': 'no_growth', 'mycoplasma': 'not_detected',
                                   'endotoxin_EU_per_mL': 0.07, 'karyotype': 'normal',
                                   'karyotype_detail': 'G-banding 20 metaphases, no clonal abnormality '
                                                       '(synthetic stand-in)'}
            arms.append(rec)
    return {
        'schema_version': '2.0',
        'run_id': run_id or f'standin-{protocol["protocol_id"]}-{scenario}-s{seed}',
        'protocol_id': protocol['protocol_id'],
        'protocol_sha256': protocol_sha256,
        'mode': 'minimal',
        'source': 'synthetic_standin',
        'label': f'SYNTHETIC iPSC -> T-LINEAGE STAND-IN ({scenario}); workflow demonstration, '
                 f'not biological evidence',
        'operator': None,
        'started_at': None,
        'notes': '; '.join(sorted(notes_all)) or 'all protocol elements mapped to the stand-in',
        'arms': arms,
    }
