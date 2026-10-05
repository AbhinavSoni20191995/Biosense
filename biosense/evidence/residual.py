"""Prediction residuals: what the model said, what was measured, and the gap.

This is where the loop closes. Everything upstream — the literature, the
analyses, the hypothesis, the simulator — produces a number that has not yet
been checked against anything. A residual is the first point at which the system
can be wrong in public.

Three rules hold it to that:

* **The prediction must exist before the measurement.** `commit()` records what
  was predicted, when, and a hash of the values; `residual()` refuses a
  commitment that was made after the run was measured. Without that rule the
  comparison is a model fitted to a result and then congratulated for matching
  it, which is the failure mode this whole contract exists to prevent.

* **A residual is never `measured`.** No instrument measured a gap: code
  subtracted a prediction from a reading. It takes the weaker of the two sides,
  so the distance between a simulated prediction and a real reading is
  `simulated` — which is the useful label, because what a reader needs to know
  about that number is that a model is standing on one end of it.

* **Agreement is not validation.** `counts_as_model_evidence` is false for a
  synthetic stand-in, for an unreplicated measurement, and for a parameter the
  model does not cover, and the reason travels with the residual rather than
  being left for a reader to work out. One run that matches is one run that
  matched; `summarise()` reports agreement without ever calling a model
  validated.
"""
from __future__ import annotations

from .. import contracts as K
from . import estimates as E

SOURCES = ('biosimulator', 'quantified_hypothesis', 'human_expectation')
MEASUREMENT_SOURCES = ('wet_lab', 'synthetic_standin')
#: Below this many independent units, a single reading cannot separate a model
#: being wrong from the run being noisy.
MIN_N_FOR_EVIDENCE = 2


def commit(*, source, readout, value, unit, model_id=None, simulator_coverage='modelled',
           scenario_ref=None, hypothesis_ref=None, interval_=None, committed_at=None, note=None):
    """Record a prediction, before the run that will test it.

    The hash covers the predicted value, its interval and the readout it is
    about. Editing any of them after the result is in produces a different hash,
    which is what makes a quietly-revised prediction visible instead of
    deniable.
    """
    if source not in SOURCES:
        raise K.ContractError(f'prediction source must be one of {SOURCES}, got {source!r}')
    if simulator_coverage not in ('modelled', 'not_modelled', 'no_simulator'):
        raise K.ContractError(f'unknown simulator_coverage {simulator_coverage!r}')
    if value is None or value.get('value') is None:
        raise K.ContractError(
            'a commitment needs a predicted value. A direction with no magnitude cannot be '
            'checked against a measurement, so record it as a hypothesis instead of as a '
            'prediction nobody can be wrong about.')
    if source == 'biosimulator' and simulator_coverage != 'modelled':
        raise K.ContractError(
            f'the simulator cannot predict a readout it does not cover (coverage: '
            f'{simulator_coverage}). A number for it would be invented, and a residual against '
            f'an invented number would then be reported as model performance.')
    payload = {'readout': readout, 'unit': unit, 'value': value.get('value'),
               'interval': interval_, 'source': source, 'model_id': model_id}
    return {'committed_at': committed_at or K.now_iso(), 'source': source,
            'model_id': model_id, 'simulator_coverage': simulator_coverage,
            'scenario_ref': scenario_ref, 'hypothesis_ref': hypothesis_ref,
            'values_sha256': K.sha256_obj(payload), 'note': note}


def measurement(*, run_id, source, measured_at=None, n=None, sd=None, arm_id=None,
                instrument=None, operator=None):
    if source not in MEASUREMENT_SOURCES:
        raise K.ContractError(f'measurement source must be one of {MEASUREMENT_SOURCES}, '
                              f'got {source!r}')
    return {'run_id': run_id, 'source': source, 'measured_at': measured_at or K.now_iso(),
            'n': n, 'sd': sd, 'arm_id': arm_id, 'instrument': instrument, 'operator': operator}


def _evidence_verdict(commitment, meas, interval_):
    """Whether this residual says anything about the model, and why."""
    if meas['source'] == 'synthetic_standin':
        return False, ('the measurement came from the synthetic stand-in, which is a workflow '
                       'demonstration and not biological evidence, so the gap says nothing '
                       'about whether the model describes a real process')
    if commitment['simulator_coverage'] != 'modelled':
        return False, (f'the readout is {commitment["simulator_coverage"]} by the simulator, so '
                       f'there is no model claim here to be right or wrong')
    if commitment['source'] == 'human_expectation':
        return False, ('the prediction was a human expectation, not a model output; the gap is '
                       'worth recording but it is not evidence about the simulator')
    n = meas.get('n')
    if n is None or n < MIN_N_FOR_EVIDENCE:
        return False, (f'the measurement has n={n}, so a gap cannot be told apart from run-to-run '
                       f'variation; at least {MIN_N_FOR_EVIDENCE} independent units are needed '
                       f'before a residual is about the model rather than about noise')
    if interval_ is None:
        return True, (f'a modelled readout measured in a real run with n={n}. No prediction '
                      f'interval was committed to, so the size of the gap is informative but '
                      f'nothing was agreed beforehand that it could fall outside')
    return True, (f'a modelled readout measured in a real run with n={n}, against an interval '
                  f'committed to before the run')


def residual(residual_id, readout, unit, *, commitment, measurement_, predicted, measured,
             interval_=None, higher_is_better=None, limitations=()):
    """measured - predicted, with the refusals that keep it honest."""
    if measured.get('estimate_type') != 'measured':
        raise K.ContractError(
            f'the measured side of a residual must be a measured value, got '
            f'{measured.get("estimate_type")!r}. Comparing a prediction against another '
            f'prediction is a model talking to itself.')
    if predicted['estimate_type'] not in ('simulated', 'predicted'):
        raise K.ContractError(
            f'the predicted side must be simulated or predicted, got '
            f'{predicted["estimate_type"]!r}')
    if commitment['committed_at'] > measurement_['measured_at']:
        raise K.ContractError(
            f'the prediction was committed at {commitment["committed_at"]} but the run was '
            f'measured at {measurement_["measured_at"]}. A prediction made after the result is '
            f'not a prediction, and a residual computed from one would report a model as '
            f'accurate for having been told the answer.')

    # predicted is the baseline and measured is the candidate, so the sign reads
    # the way a person expects: positive means the run came out above the model.
    est = E.estimate(readout, unit, predicted, measured,
                     label=f'{readout}: measured against predicted',
                     higher_is_better=higher_is_better, interval_=interval_,
                     limitations=list(limitations))
    counts, why = _evidence_verdict(commitment, measurement_, interval_)
    within = None
    if interval_ is not None and measured.get('value') is not None:
        within = bool(interval_['lower'] <= measured['value'] <= interval_['upper'])

    lims = list(limitations)
    if not counts:
        lims.append(why)
    if measurement_['source'] == 'synthetic_standin':
        lims.append('SYNTHETIC STAND-IN: not biological evidence')

    return K.require_valid('prediction_residual', {
        'schema_version': '0.1', 'residual_id': residual_id,
        'readout': readout, 'unit': unit,
        'commitment': commitment, 'measurement': measurement_,
        'predicted': predicted, 'measured': measured,
        'residual': est, 'interval': interval_, 'within_interval': within,
        'counts_as_model_evidence': counts, 'why_it_does_or_does_not': why,
        'limitations': lims, 'created_at': K.now_iso(),
    })


def summarise(residuals):
    """What a set of residuals supports, and what it does not.

    Deliberately does not return a pass or a score. Agreement across a handful of
    runs is a reason to keep using the model and look again; it is not a
    calibration, and the one phrase this must never produce is 'the model is
    validated'.
    """
    rs = list(residuals)
    if not rs:
        return {'n_residuals': 0, 'n_counting_as_evidence': 0, 'readouts': [],
                'within_interval': None, 'mean_absolute_residual': None,
                'statement': 'no prediction has been checked against a measurement yet',
                'limitations': ['the loop has not closed: nothing predicted has been measured']}
    counting = [r for r in rs if r['counts_as_model_evidence']]
    withs = [r['within_interval'] for r in counting if r['within_interval'] is not None]
    mags = [abs(r['residual']['absolute_change']) for r in counting
            if r['residual'].get('absolute_change') is not None]
    lims = sorted({lim for r in rs for lim in r['limitations']})
    if not counting:
        statement = (f'{len(rs)} residual(s) recorded, none of which says anything about the '
                     f'model yet')
    else:
        inside = f'{sum(withs)}/{len(withs)} inside the committed interval; ' if withs else ''
        statement = (f'{len(counting)} of {len(rs)} residual(s) bear on the model: {inside}'
                     f'mean absolute gap '
                     f'{sum(mags) / len(mags):.4g} across {len(set(r["readout"] for r in counting))} '
                     f'readout(s). Agreement over this few runs is a reason to keep using the '
                     f'model and check again, not a calibration.')
        lims.append('a handful of runs cannot establish that the model is right; this reports '
                    'agreement, never validation')
    return {
        'n_residuals': len(rs), 'n_counting_as_evidence': len(counting),
        'readouts': sorted({r['readout'] for r in rs}),
        'within_interval': (sum(withs), len(withs)) if withs else None,
        'mean_absolute_residual': (sum(mags) / len(mags)) if mags else None,
        'statement': statement, 'limitations': sorted(set(lims)),
    }


def render(res):
    """One readable block per residual."""
    r = res['residual']
    gap = r['absolute_change']
    sign = '+' if gap > 0 else ''
    out = [f'{res["readout"]}: predicted {res["predicted"]["value"]:g} {res["unit"]}, '
           f'measured {res["measured"]["value"]:g} {res["unit"]} '
           f'({sign}{gap:g} {r["change_unit"]}, {r["estimate_type"].upper()})']
    if res['within_interval'] is not None:
        iv = res['interval']
        out.append(f'  {"inside" if res["within_interval"] else "OUTSIDE"} the committed '
                   f'interval [{iv["lower"]:g}, {iv["upper"]:g}]')
    out.append(f'  model evidence: {"yes" if res["counts_as_model_evidence"] else "no"} — '
               f'{res["why_it_does_or_does_not"]}')
    return '\n'.join(out)
