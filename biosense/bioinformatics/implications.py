"""From a measured difference to a process parameter, explicitly and auditably.

This is the step that makes the bioinformatics useful to a cell-production loop,
and it is also the step where a system most easily starts making things up. So
the mapping is a table a person can read and disagree with, not a judgement made
per result:

* **The parameter comes from what the dataset says it varied**, not from the
  readout that moved. A table whose declared perturbation is "M-CSF
  concentration" can implicate `mcsf_ng_ml`. The same readouts in a dataset that
  varied agitation implicate `agitation_rpm`. If the perturbation maps to no
  protocol parameter, nothing is suggested and the analysis says so.
* **The readout decides what KIND of claim is available** — identity, purity,
  viability, stress — which is what the implication is allowed to talk about.
* **A favourable comparison is not a recommendation.** `direction` records which
  way the measurement moved. Whether to move the parameter that way is the
  orchestrator's decision, taken against every other source of evidence, and the
  wording here is written to leave that decision where it belongs.

Confidence never exceeds `moderate` from a single dataset with one comparison,
whatever the p-value: one experiment, measured once, in someone else's hands and
possibly another cell type.
"""
from __future__ import annotations

import re

# Declared perturbation -> the protocol parameter it is about. Matching is on
# substrings of the lowercased declared perturbation, which is a field a person
# filled in, so the match is against their words and not against a guess.
PERTURBATION_PARAMETERS = (
    (('m-csf', 'mcsf', 'csf1', 'macrophage colony'), 'mcsf_ng_ml',
     'the dataset states that M-CSF concentration is what differs between its groups'),
    (('il-3', 'il3'), 'il3_ng_ml',
     'the dataset states that IL-3 concentration is what differs between its groups'),
    (('il-7', 'il7'), 'il7_ng_ml',
     'the dataset states that IL-7 concentration is what differs between its groups'),
    (('bmp4', 'bmp-4'), 'bmp4_ng_ml', 'the dataset states that BMP4 differs between its groups'),
    (('vegf',), 'vegf_ng_ml', 'the dataset states that VEGF differs between its groups'),
    (('agitation', 'rpm', 'shear', 'stirring'), 'agitation_rpm',
     'the dataset states that agitation is what differs between its groups'),
    (('dissolved oxygen', 'oxygen tension', 'hypoxia', ' do '), 'do_setpoint',
     'the dataset states that oxygen is what differs between its groups'),
    (('feed', 'medium exchange', 'glucose'), 'feed_fraction',
     'the dataset states that feeding is what differs between its groups'),
    (('seeding', 'seed density', 'inoculation'), 'seed_density',
     'the dataset states that seeding density is what differs between its groups'),
    (('timepoint', 'duration', 'day of', 'harvest day'), 'stage_duration_days',
     'the dataset states that timing is what differs between its groups'),
)

# What a readout is about. Used to word the implication, and to refuse to talk
# about purity on the basis of a viability column.
READOUT_KINDS = (
    # Yield is checked before viability on purpose: "viable_cells_e6" is a count
    # of cells, not a percentage of them, and classifying a yield as a viability
    # measure turns the product's headline number into a constraint on itself.
    (r'cells?_e\d|cell_count|cells_per|total_cells|yield|harvest|_e6|_e9|absolute',
     'yield', 'how many cells the process delivers'),
    (r'viab|live|7aad|propidium|pi_pct', 'viability',
     'how many cells are alive'),
    (r'cd14|cd16|cd11b|cd206|mrc1|csf1r|cd3|cd4|cd8|car[_ -]?pos|marker|identity',
     'identity', 'how many cells carry the identity marker'),
    (r'purity|frac|freq|percent|pct|%', 'population_frequency',
     'what fraction of the population a gate holds'),
    (r'exhaust|pd1|pdcd1|lag3|tim3|tox', 'exhaustion',
     'how far cells have gone down an exhaustion programme'),
    (r'stress|hspa|apopt|casp|ldh|dna damage', 'stress',
     'how much stress or death signalling is present'),
    (r'mfi|intensity', 'marker_intensity', 'how much marker each cell carries'),
)


def parameter_for_perturbation(perturbation):
    """(parameter, basis) for a declared perturbation, or (None, why not)."""
    p = f' {(perturbation or "").lower()} '
    for keys, param, basis in PERTURBATION_PARAMETERS:
        if any(k in p for k in keys):
            return param, basis
    return None, (
        f'the dataset declares its perturbation as {perturbation!r}, which this version does not '
        f'map to any protocol parameter. The comparison still stands as evidence; naming a '
        f'parameter for it would be inventing the link.')


def readout_kind(readout):
    r = (readout or '').lower()
    for pattern, kind, gloss in READOUT_KINDS:
        if re.search(pattern, r):
            return kind, gloss
    return 'unclassified', 'a measured quantity this version does not classify'


def confidence_for(rows, *, source_visibility, replicate_n, independent_units):
    """How much weight one dataset's comparison can carry.

    Caps rather than scores. A single dataset compared once is `moderate` at best
    whatever the p-value, because the quantity a p-value does not measure --
    whether this transfers to your cells, your vessel and your stage -- is the
    one that decides whether a parameter should move.
    """
    reasons = []
    strong = [r for r in rows if (r.get('q_value') is not None and r['q_value'] < 0.05)]
    if not strong:
        reasons.append('no readout survives multiplicity correction at q < 0.05')
        return 'low', reasons
    if independent_units is not None and independent_units < 3:
        reasons.append(f'only {independent_units} independent experimental units, so the spread '
                       f'is barely estimated')
        return 'low', reasons
    if replicate_n is not None and replicate_n < 3:
        reasons.append(f'{replicate_n} replicates per group')
        return 'low', reasons
    reasons.append(f'{len(strong)} readout(s) at q < 0.05 with consistent direction')
    reasons.append('capped at moderate: one dataset, one comparison, and transfer to this cell '
                   'type, stage and culture format is untested')
    if source_visibility == 'private':
        reasons.append('private in-house data: it can support or contradict a hypothesis, and it '
                       'can never become a literature citation')
    return 'moderate', reasons


def candidate_parameters(rows, manifest, *, confidence, arm_scope=None):
    """Candidate levers in the loop's existing vocabulary, or an empty list.

    The wording is deliberately not a recommendation: it states what was measured
    and that the parameter is worth testing. Nothing downstream reads this as an
    instruction, and `revise_protocol` still has to pass the envelope.
    """
    param, basis = parameter_for_perturbation(manifest.get('perturbation'))
    if param is None:
        return [], basis

    signif = [r for r in rows if r.get('q_value') is not None and r['q_value'] < 0.05
              and r.get('direction') in ('increase', 'decrease')]
    if not signif:
        return [], ('no readout moved beyond what multiplicity correction tolerates, so this '
                    'dataset implicates no direction for ' + param)

    # Identity and frequency readouts say whether the product improved. Viability
    # and stress readouts constrain how far a parameter may be pushed; they do not
    # by themselves argue for pushing it.
    gains = [r for r in signif if readout_kind(r['readout'])[0]
             in ('yield', 'identity', 'population_frequency', 'marker_intensity')]
    harms = [r for r in signif if readout_kind(r['readout'])[0] in ('viability', 'stress',
                                                                    'exhaustion')
             and r['direction'] == ('decrease' if readout_kind(r['readout'])[0] == 'viability'
                                    else 'increase')]
    if not gains:
        return [], (f'the readouts that moved describe '
                    f'{", ".join(sorted({readout_kind(r["readout"])[0] for r in signif}))} rather '
                    f'than the product itself, so they bound how far {param} may be pushed '
                    f'without arguing for pushing it')

    up = sum(1 for r in gains if r['direction'] == 'increase')
    down = len(gains) - up
    if up and down:
        direction = 'revisit'
        wording = (f'{up} yield/identity readout(s) rose and {down} fell in the group with '
                   f'more of this factor, so the dataset does not point one way')
    else:
        direction = 'increase' if up else 'decrease'
        wording = (f'{len(gains)} yield/identity readout(s) '
                   f'{"higher" if up else "lower"} in the group with more of this factor '
                   f'({", ".join(r["readout"] for r in gains[:4])})')

    caveat = ''
    if harms:
        caveat = (f' Counterweight: {", ".join(r["readout"] for r in harms)} moved unfavourably in '
                  f'the same comparison, so any move has to be read against that.')

    label = {'public_dataset': 'a public dataset',
             'private_user_dataset': 'private user-provided data',
             'derived_analysis': 'a derived analysis',
             'synthetic_fixture': 'a SYNTHETIC committed fixture, which is invented data and '
                                  'not a measurement of anything',
             }.get(manifest['evidence_class'], 'a dataset')

    return [{
        'parameter': param,
        'direction': direction,
        'arm_scope': arm_scope,
        'basis': (f'{wording}. Source: {label} ({manifest["dataset_id"]}), which {basis}. '
                  f'This is evidence that {param} is worth testing, not a value to adopt and not '
                  f'a conclusion that it must move.{caveat}'),
        'confidence': confidence,
        'suggested_range': None,
    }], None
