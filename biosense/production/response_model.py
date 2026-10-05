"""Evaluating a proposed response term: the arithmetic is ours, the constants are not.

A project may carry a parameter the calibrated model has no equation for, with a
`response_model` saying how somebody thinks it behaves — a bell with an optimum,
a saturating curve with a half-maximum, a line with a slope, a threshold with a
step. Two kinds of somebody:

    de_novo_ai         an agent proposed the shape and the constants from cited
                       evidence, and nothing was fitted to data
    expert_declared    a person stated them from their own experience, which is
                       expert knowledge: private, not citable, never a reported
                       value

This module computes what such a term says. What it will not do is pretend the
result is the same kind of number as the calibrated model's output.

Three properties, each the reason a line of code here exists:

* **Deterministic code evaluates; a model never does.** The shapes are four
  closed-form functions. An agent chooses `bell` and supplies `optimum` and
  `tolerance`; it does not supply arithmetic, and it cannot make the term do
  anything the four shapes cannot.
* **A proposed term cannot overwhelm the calibrated biology.** `max_effect` is
  bounded by the contract and the computed multiplier is clamped, so a term with
  an enthusiastic constant moves a readout by the amount it declared and no more.
  A prediction that a cytokine multiplies yield by forty is not a prediction.
* **Every number it produces says where it came from.** `evaluate` returns the
  origin and an `evidence_status` sentence, and the caller is expected to carry
  both onto the screen. A de-novo number that looks like a calibrated one is the
  failure this whole design is arranged to prevent.
"""
from __future__ import annotations

import math

from .. import contracts as K

# What a term may multiply. Each maps onto something the calibrated model
# already computes, so a proposed term adjusts a quantity that exists rather
# than inventing a new output nobody can check.
TARGET_METRICS = {
    'growth': ('peak_vcd_e6_per_ml', 'harvest_per_input_ipsc', 'harvest_total_e6_per_ml'),
    'death': ('final_viability_pct',),
    'viability': ('final_viability_pct',),
    'transition_efficiency': ('cumulative_differentiation_efficiency',
                              'harvest_per_input_ipsc', 'harvest_total_e6_per_ml',
                              'monocyte_gate_pct'),
    'harvest': ('harvest_per_input_ipsc', 'harvest_total_e6_per_ml'),
}
# A term whose target is `death` improves the readout by reducing it, so its
# multiplier is inverted before it reaches a viability metric.
INVERTED = ('death',)
# The hard clamp on any single term, whatever it declared. Belt and braces: the
# contract bounds max_effect, and this bounds the product anyway.
MULTIPLIER_FLOOR, MULTIPLIER_CEILING = 0.2, 5.0

EVIDENCE_STATUS = {
    'ai_proposed':
        'de novo: an AI proposed this response from cited evidence and it was fitted to no '
        'data. The number is what that proposal implies, not a measurement and not a '
        'calibrated prediction.',
    'expert_declared':
        'expert-declared: a person supplied this response from their own experience. It is '
        'expert knowledge — private, not citable, and a protocol value resting on it is a '
        'design choice rather than a reported one.',
}
BADGE = {'ai_proposed': 'DE NOVO · UNCALIBRATED', 'expert_declared': 'EXPERT-DECLARED'}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def shape_value(rm, x):
    """The term's response at *x*, as a number between 0 and 1 (or above for linear).

    0 means "this parameter contributes nothing here", 1 means "as good as this
    term gets". The effect size is applied separately, so the shape and the
    magnitude stay independently reviewable.
    """
    if x is None:
        return None
    x = float(x)
    shape = rm['shape']
    if shape == 'bell':
        opt, tol = float(rm['optimum']), float(rm['tolerance'])
        return math.exp(-0.5 * ((x - opt) / tol) ** 2)
    if shape == 'saturating':
        half = float(rm['half_max'])
        if half <= 0:
            raise K.ContractError('a saturating response needs a positive half_max')
        return x / (half + x) if x >= 0 else 0.0
    if shape == 'threshold':
        return 1.0 if x >= float(rm['threshold']) else 0.0
    if shape == 'linear':
        return float(rm['slope']) * x
    raise K.ContractError(f'unknown response shape {shape!r}')


def multiplier(rm, control_value, candidate_value):
    """How much this term says the target changes from control to candidate.

    A ratio, not an absolute: the calibrated model already produced the control
    number, and this says what the proposed term would do to it. Returns None
    when either value is missing, because a comparison needs both.
    """
    a, b = shape_value(rm, control_value), shape_value(rm, candidate_value)
    if a is None or b is None:
        return None
    eff = float(rm['max_effect'])
    # response 0..1 scaled onto 1 .. 1+max_effect, so "no response" means "no
    # change from whatever the calibrated model already said".
    fa, fb = 1.0 + eff * a, 1.0 + eff * b
    if fa == 0:
        return None
    m = fb / fa
    if rm['target'] in INVERTED:
        m = 1.0 / m if m else None
    return None if m is None else _clamp(m, MULTIPLIER_FLOOR, MULTIPLIER_CEILING)


def describe(rm):
    """The term in one line a person can argue with, plus its badge."""
    shape = rm['shape']
    if shape == 'bell':
        body = f'peaks at {rm["optimum"]:g}, falling away over ±{rm["tolerance"]:g}'
    elif shape == 'saturating':
        body = f'rises to a plateau, half its effect at {rm["half_max"]:g}'
    elif shape == 'threshold':
        body = f'no effect below {rm["threshold"]:g}, full effect above it'
    else:
        body = f'proportional, {rm["slope"]:g} per unit'
    direction = 'raising' if float(rm['max_effect']) >= 0 else 'lowering'
    return {
        'shape': shape,
        'target': rm['target'],
        'summary': f'{body}; at best {direction} {rm["target"].replace("_", " ")} by '
                   f'{abs(float(rm["max_effect"])) * 100:.0f}%',
        'origin': rm['origin'],
        'badge': BADGE[rm['origin']],
        'basis': rm['basis'],
        'references': rm.get('references') or [],
        'evidence_status': rm.get('evidence_status') or EVIDENCE_STATUS[rm['origin']],
        'calibrated': False,
    }


def apply_to_signature(signature, terms):
    """Apply proposed terms to a simulator signature, returning the adjusted copy.

    *terms* is a list of {'parameter_id', 'response_model', 'multiplier'}. Each
    multiplies the metrics its target names; a metric no term touches is
    unchanged. The returned `adjusted` dict says which metric each term moved,
    so the interface can show the calibrated number and the proposed one side by
    side rather than silently replacing one with the other.
    """
    out = dict(signature)
    moved = {}
    for t in terms:
        m = t.get('multiplier')
        if m is None:
            continue
        for metric in TARGET_METRICS.get(t['response_model']['target'], ()):
            if metric not in out or out[metric] is None:
                continue
            before = float(out[metric])
            out[metric] = before * m
            moved.setdefault(metric, []).append(
                {'parameter_id': t['parameter_id'], 'multiplier': round(m, 6),
                 'from': before, 'to': out[metric], 'origin': t['response_model']['origin']})
    return {'signature': out, 'adjusted': moved}


def plan_terms(project, control, candidate_values):
    """The proposed terms this comparison would fire, with their multipliers.

    Reads coverage from the project rather than from the candidate, so a
    candidate cannot assert that a parameter is predictable. A parameter whose
    value does not change contributes a multiplier of 1 and is reported as
    unchanged rather than dropped — a reader should be able to see that the term
    exists and did nothing.
    """
    from .. import projects as PJ
    terms = []
    for pid, value in (candidate_values or {}).items():
        if not project.has(pid):
            continue
        q = project.parameter(pid)
        if q.simulator_coverage not in PJ.UNCALIBRATED or not q.response_model:
            continue
        current = (control or {}).get(pid, q.default_value)
        m = multiplier(q.response_model, current, value)
        terms.append({
            'parameter_id': pid, 'label': q.label, 'unit': q.unit,
            'stage': q.stage, 'coverage': q.simulator_coverage,
            'control_value': current, 'candidate_value': value,
            'response_model': q.response_model, 'multiplier': m,
            'unchanged': m is not None and abs(m - 1.0) < 1e-9,
            'description': describe(q.response_model),
        })
    return terms
