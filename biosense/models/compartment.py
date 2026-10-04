"""compartment_growth_v1: a phenomenological population-count demonstration model.

NOT a validated cardiomyocyte differentiation mechanism. State (cell counts):

  R  remaining undifferentiated / differentiation-competent cells (a coarse
     compartment that hides intermediate developmental states)
  C  modeled target cardiomyocytes (not a subtype, maturity or marker claim)
  O  modeled off-target cells
  D  dead cells retained in the toy accounting (no removal or lysis)

With N = R + C + O and f(N) = max(0, 1 - N/K), per stage:

  dR/dt = [mu_R f(N) - delta_R - k_C - k_O] R
  dC/dt = k_C R + [mu_C f(N) - delta_C] C
  dO/dt = k_O R + [mu_O f(N) - delta_O] O
  dD/dt = delta_R R + delta_C C + delta_O O

A fifth bookkeeping state B = integral of births makes the conservation check
exact: (R + C + O + D)(t) - (R + C + O + D)(0) - B(t) = 0.
Time is in hours, rates in 1/h, K and states in cells.
"""
import math

MODEL_ID = 'compartment_growth_v1'
STATE = ('R', 'C', 'O', 'D')
STAGES = ('ipsc_expansion', 'cardiac_differentiation')
TIME_UNIT = 'h'
POPULATION_UNIT = 'cells'

_RATE_MEANING = {
    'mu_R': 'Per-capita growth rate of R at low density (logistic, suppressed as N approaches K)',
    'mu_C': 'Per-capita growth rate of modeled target cells C at low density',
    'mu_O': 'Per-capita growth rate of modeled off-target cells O at low density',
    'delta_R': 'Per-capita death rate of R (moves cells to D)',
    'delta_C': 'Per-capita death rate of C (moves cells to D)',
    'delta_O': 'Per-capita death rate of O (moves cells to D)',
    'k_C': 'Per-capita conversion rate R -> C (NOT identifiable from endpoint marker positivity)',
    'k_O': 'Per-capita conversion rate R -> O',
}
_POPULATION = {'R': 'R', 'C': 'C', 'O': 'O', 'D': 'D'}

# Rates consumed per stage. During starting-iPSC expansion k_C and k_O are
# structurally zero by model declaration; they are not inputs and must not be
# supplied. That is a modeling choice, not a measured property.
STAGE_RATES = {
    'ipsc_expansion': ('mu_R', 'delta_R', 'mu_C', 'delta_C', 'mu_O', 'delta_O'),
    'cardiac_differentiation': ('mu_R', 'delta_R', 'k_C', 'k_O', 'mu_C', 'delta_C', 'mu_O', 'delta_O'),
}
STRUCTURAL_ZEROS = {'ipsc_expansion': ('k_C', 'k_O')}

# Literature claim parameter names that may populate an input. Anything else
# is refused: e.g. an effective net growth rate is never silently mu_R.
_CLAIM_PARAMS = {
    'mu_R': {'ipsc_expansion': ['maximum_growth_rate', 'mu_R'], 'cardiac_differentiation': ['mu_R']},
    'k_C': {'cardiac_differentiation': ['differentiation_transition_rate', 'k_C']},
}


def _registry():
    inputs = {}
    for stage, rates in STAGE_RATES.items():
        for r in rates:
            pop = r.split('_')[1] if r.startswith(('mu_', 'delta_')) else 'R'
            inputs[f'{stage}.{r}'] = {
                'kind': 'rate', 'stage': stage, 'symbol': r, 'unit': '1/h', 'family': 'rate',
                'meaning': _RATE_MEANING[r], 'population': pop,
                'accepted_claim_parameters': _CLAIM_PARAMS.get(r, {}).get(stage, []),
                'min': 0.0,
            }
    inputs['global.K'] = {
        'kind': 'capacity', 'stage': 'all', 'symbol': 'K', 'unit': 'cells', 'family': 'count',
        'meaning': 'Modeled carrying capacity in viable cells (not automatically a maximum observed count)',
        'population': 'R+C+O', 'accepted_claim_parameters': [], 'min_exclusive': 0.0,
    }
    for s in STATE:
        inputs[f'initial.{s}'] = {
            'kind': 'initial', 'stage': 'ipsc_expansion', 'symbol': s + '0', 'unit': 'cells', 'family': 'count',
            'meaning': f'Cell count of compartment {s} at the start of ipsc_expansion',
            'population': s, 'accepted_claim_parameters': [], 'min': 0.0,
        }
    for stage in STAGES:
        inputs[f'schedule.{stage}.duration'] = {
            'kind': 'schedule', 'stage': stage, 'symbol': 'T_' + stage, 'unit': 'h', 'family': 'time',
            'meaning': f'Reference duration of {stage} measured from that stage start',
            'population': None, 'accepted_claim_parameters': ['duration'], 'min_exclusive': 0.0,
        }
    return inputs


INPUTS = _registry()


def contract():
    """Machine-readable model contract recorded in every scenario."""
    return {
        'model_id': MODEL_ID,
        'status': 'phenomenological demonstration; not biologically validated',
        'state': list(STATE),
        'time_unit': TIME_UNIT,
        'population_unit': POPULATION_UNIT,
        'stage_order': list(STAGES),
        'structural_zeros': {k: list(v) for k, v in STRUCTURAL_ZEROS.items()},
        'required_inputs': sorted(INPUTS),
        'equations': [
            'f(N) = max(0, 1 - N/K), N = R + C + O',
            'dR/dt = [mu_R f(N) - delta_R - k_C - k_O] R',
            'dC/dt = k_C R + [mu_C f(N) - delta_C] C',
            'dO/dt = k_O R + [mu_O f(N) - delta_O] O',
            'dD/dt = delta_R R + delta_C C + delta_O O',
        ],
        'not_modeled': ['reagent dose-response (e.g. CHIR99021)', 'Wnt signaling timing', 'metabolism / nutrients',
                        'oxygen transport', 'aggregate geometry', 'purification', 'recovery', 'marker expression',
                        'maturity / electrophysiology', 'dead-cell removal'],
    }


def rhs_factory(rates, K):
    """Return f(t, y) for state y = [R, C, O, D, B]."""
    mu_R, mu_C, mu_O = rates['mu_R'], rates['mu_C'], rates['mu_O']
    d_R, d_C, d_O = rates['delta_R'], rates['delta_C'], rates['delta_O']
    k_C, k_O = rates.get('k_C', 0.0), rates.get('k_O', 0.0)

    def rhs(_t, y):
        R, C, O = y[0], y[1], y[2]
        N = R + C + O
        f = max(0.0, 1.0 - N / K)
        bR, bC, bO = mu_R * f * R, mu_C * f * C, mu_O * f * O
        dR = bR - (d_R + k_C + k_O) * R
        dC = k_C * R + bC - d_C * C
        dO = k_O * R + bO - d_O * O
        dD = d_R * R + d_C * C + d_O * O
        return [dR, dC, dO, dD, bR + bC + bO]
    return rhs


def stage_rates(params, stage):
    """Collect the numeric rates a stage consumes; structural zeros are explicit."""
    rates = {r: float(params[f'{stage}.{r}']) for r in STAGE_RATES[stage]}
    for r in STRUCTURAL_ZEROS.get(stage, ()):
        rates[r] = 0.0
    for k, v in rates.items():
        if not math.isfinite(v) or v < 0:
            raise ValueError(f'{stage}.{k} must be finite and nonnegative')
    return rates
