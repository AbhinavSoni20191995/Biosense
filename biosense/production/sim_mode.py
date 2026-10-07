"""Simulator mode: turn the knobs yourself and watch the culture respond.

    POST /api/sim/run       one condition
    POST /api/sim/compare   two conditions, side by side

This is the sandbox beside the loop, not a part of it. The loop asks an agent to
choose a move and then refuses the moves the verdict does not permit; here a
*person* chooses the move and nothing is refused, because nothing is being
decided. The two share one bioreactor model (`analysis_agent.simulator`, the
iPSC -> monocyte process) so that what you learn by hand is about the same
object the loop optimises.

Three properties this module exists to hold:

* **Hidden state stays hidden.** `RunRecord.ground_truth` carries the line's
  true mu_max, death rate and clonal fraction. `_frames` reads observation
  channels only and this module never returns the truth block, so a reading in
  simulator mode costs exactly what it costs the loop: you infer the culture
  from its instruments. The challenge toggles below are the one exception, and
  they are inputs the person sets, not answers the model hands back.
* **The condition read is computed, never narrated.** `read_condition` is
  arithmetic over observables with stated thresholds. No model writes it, and it
  claims nothing beyond what the channel it names actually says.
* **A sandbox result is not evidence.** Every payload carries
  `evidence_status: "synthetic_demonstration"` and `bioreactor_source:
  "synthetic_standin"`, and `seed_brief` marks every setpoint a person carries
  into a loop run as `design_choice` with the sandbox as its origin. The toy
  model rewards the levers it was built to reward; a real culture may not.
"""
from __future__ import annotations

import math
from dataclasses import asdict

from .. import contracts as K

MAX_SEED = 2 ** 31 - 1
MAX_DAYS = 45.0

# ── the knobs ───────────────────────────────────────────────────────────
# One source of truth: the interface is generated from this, so a slider can
# never offer a value the model will not accept. `stage` says where in the
# process the knob bites, which is the thing that makes the sandbox teach
# something -- BMP4 does nothing to the expansion stage no matter how far you
# push it.
KNOBS = (
    dict(id='seed_density', label='Seed density', unit='1e6 cells/mL', stage='expansion',
         min=0.1, max=2.0, step=0.05, default=0.5,
         note='Divides the yield-per-input-iPSC. A denser seed hits the vessel '
              'ceiling sooner.'),
    dict(id='agitation_rpm', label='Agitation', unit='rpm', stage='all',
         min=20.0, max=140.0, step=1.0, default=60.0,
         note='The central trade-off: shear breaks aggregates down to the size a '
              'stage wants, and the same shear kills cells.'),
    dict(id='do_setpoint', label='Dissolved oxygen', unit='fraction', stage='all',
         min=0.05, max=0.40, step=0.01, default=0.20,
         note='Each stage has its own optimum, and oxygen also sets how deep into '
              'an aggregate the viable shell reaches.'),
    dict(id='feed_fraction', label='Feed exchange', unit='fraction of volume', stage='all',
         min=0.1, max=0.9, step=0.05, default=0.5,
         note='Replaces glucose and dilutes lactate and ammonia together.'),
    dict(id='feed_interval_h', label='Feed interval', unit='h', stage='all',
         min=12.0, max=72.0, step=2.0, default=24.0,
         note='How long waste accumulates between exchanges.'),
    dict(id='rocki_hours', label='ROCK inhibitor', unit='h', stage='expansion',
         min=0.0, max=48.0, step=2.0, default=24.0,
         note='Single-cell survival through seeding. Only the first hours matter.'),
    dict(id='bmp4', label='BMP4', unit='ng/mL', stage='mesoderm',
         min=0.0, max=80.0, step=1.0, default=25.0,
         note='Mesoderm specification. Off-optimum costs transition efficiency, '
              'not growth.'),
    dict(id='vegf', label='VEGF', unit='ng/mL', stage='mesoderm',
         min=0.0, max=150.0, step=2.0, default=50.0,
         note='Mesoderm specification, alongside BMP4.'),
    dict(id='mcsf', label='M-CSF', unit='ng/mL', stage='myeloid',
         min=0.0, max=150.0, step=2.0, default=50.0,
         note='Myeloid commitment. The dominant lever on the harvest stream.'),
    dict(id='il3', label='IL-3', unit='ng/mL', stage='myeloid',
         min=0.0, max=80.0, step=1.0, default=25.0,
         note='Myeloid commitment, alongside M-CSF.'),
)
KNOB_BY_ID = {k['id']: k for k in KNOBS}

STAGES = (
    dict(id='expansion', label='Expansion', default_days=4.0, min=2.0, max=10.0,
         wants='aggregates near 150 um, DO 0.20'),
    dict(id='mesoderm', label='Mesoderm', default_days=3.0, min=2.0, max=8.0,
         wants='aggregates near 200 um, DO 0.10'),
    dict(id='hemato', label='Hemogenic', default_days=4.0, min=2.0, max=10.0,
         wants='aggregates near 300 um, DO 0.12'),
    dict(id='myeloid', label='Myeloid harvest', default_days=14.0, min=4.0, max=24.0,
         wants='aggregates near 400 um, DO 0.18'),
)
STAGE_BY_ID = {s['id']: s for s in STAGES}

# ── challenge toggles ───────────────────────────────────────────────────
# Hidden state in the loop; a deliberate input here. Playing with these is the
# point: a fouling capacitance probe and a culture that is genuinely thinning
# look identical in one channel and nothing like each other across four.
CHALLENGES = (
    dict(id='none', label='Wild type, healthy instruments',
         note='The baseline. Every channel means what it says.'),
    dict(id='variant', label='Culture-adapted clone seeded (2%)',
         note='A 20q11.21-gain-type clone: lower death, slightly faster growth, '
              'badly impaired differentiation. It takes over across passages, so '
              'the culture looks better and yields less.'),
    dict(id='fault_capacitance', label='Capacitance probe fouling',
         note='Instrument, not biology. Biomass reads low while the cell count and '
              'the oxygen uptake rate disagree with it.'),
    dict(id='fault_ph', label='pH patch drift',
         note='Instrument, not biology. The pH offset moves with no matching change '
              'in lactate.'),
    dict(id='fault_staining', label='Antibody lot / no-wash failure',
         note='Instrument, not biology. Every fluorescence channel drops together '
              'and the label-free impedance channels do not.'),
)
CHALLENGE_IDS = tuple(c['id'] for c in CHALLENGES)
CHALLENGE_LABEL = {c['id']: c['label'] for c in CHALLENGES}

# ── thresholds for the condition read ───────────────────────────────────
# Stated here rather than buried in the renderer, because the read is only
# worth anything if you can see what it was asked.
# An engineered line, as the person (or a hypothesis) assumes it behaves: its
# effect on growth (mu_max) and on differentiation efficiency, as ratios to wild
# type. The stand-in models an edit exactly this way; the numbers are an
# ASSUMPTION a person sets, never something the model knows about a gene.
GENOTYPE_RANGE = (0.3, 2.0)


ASSUMED_KINDS = ('edit', 'factor')


def parse_genotype(raw):
    """{label, growth_ratio, diff_ratio[, kind, stage]} -> a clamped assumption, or None.

    `kind` is `edit` (an engineered line: acts through the whole process) or
    `factor` (something added to the medium: acts only in the stage named).
    Either way the effect is the same two ratios a person sets — the reactor has
    no term for the gene or the factor itself.
    """
    if raw in (None, {}, ''):
        return None
    if not isinstance(raw, dict):
        raise K.ContractError('genotype is {"label", "growth_ratio", "diff_ratio"}')
    kind = str(raw.get('kind') or 'edit').strip().lower()
    if kind not in ASSUMED_KINDS:
        raise K.ContractError(f'kind must be one of {ASSUMED_KINDS}; got {kind!r}')
    default = 'added factor' if kind == 'factor' else 'edited line'
    label = str(raw.get('label') or default).strip()[:40] or default
    stage = raw.get('stage')
    stage = None if stage in (None, '', 'all') else str(stage).strip()
    if stage is not None and stage not in STAGE_BY_ID:
        raise K.ContractError(f'the reactor\'s stages are {", ".join(STAGE_BY_ID)} (or "all"); '
                              f'got {stage!r}')
    out = {'label': label, 'kind': kind, 'stage': stage}
    for key in ('growth_ratio', 'diff_ratio'):
        try:
            v = float(raw.get(key, 1.0))
        except (TypeError, ValueError):
            raise K.ContractError(f'genotype.{key} must be a number') from None
        if not math.isfinite(v):
            raise K.ContractError(f'genotype.{key} must be finite')
        out[key] = min(GENOTYPE_RANGE[1], max(GENOTYPE_RANGE[0], v))
    return out


LIMITS = dict(
    viability_good=88.0, viability_poor=75.0,
    lactate_high=22.0, lactate_severe=30.0,
    ammonia_high=3.0, ammonia_severe=4.5,
    glucose_low=2.0, glucose_exhausted=0.5, vcd_collapsed=0.05,
    oversize_frac=0.35, oversize_severe=0.6,
    do_low=0.08, do_high=0.34,
)


def _clamp_knob(knob, value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise K.ContractError(f'{knob["id"]}: {value!r} is not a number')
    if not math.isfinite(v):
        raise K.ContractError(f'{knob["id"]}: {value!r} is not a finite number')
    return min(max(v, knob['min']), knob['max'])


def parse_condition(payload):
    """A client payload -> (Setpoints, stage_days, meta). Clamps; refuses junk.

    Clamping rather than refusing an out-of-range number is deliberate: a slider
    cannot send one, so an out-of-range value means a hand-written request, and
    the honest answer is to run the nearest condition the model is valid over and
    say so in `clamped`.
    """
    from analysis_agent.simulator import Setpoints

    payload = payload or {}
    if not isinstance(payload, dict):
        raise K.ContractError('send a JSON object')
    sent = payload.get('setpoints') or {}
    if not isinstance(sent, dict):
        raise K.ContractError('setpoints must be a JSON object')
    unknown = sorted(set(sent) - set(KNOB_BY_ID))
    if unknown:
        raise K.ContractError(
            f'simulator mode does not expose {", ".join(unknown)}. '
            f'Available knobs: {", ".join(KNOB_BY_ID)}.')

    values, clamped = {}, []
    for kid, knob in KNOB_BY_ID.items():
        if kid in sent:
            v = _clamp_knob(knob, sent[kid])
            if abs(v - float(sent[kid])) > 1e-9:
                clamped.append(dict(knob=kid, sent=float(sent[kid]), used=v,
                                    range=[knob['min'], knob['max']]))
            values[kid] = v
        else:
            values[kid] = knob['default']

    sent_days = payload.get('stage_days') or {}
    if not isinstance(sent_days, dict):
        raise K.ContractError('stage_days must be a JSON object')
    unknown = sorted(set(sent_days) - set(STAGE_BY_ID))
    if unknown:
        raise K.ContractError(f'unknown stage(s): {", ".join(unknown)}')
    stage_days = {}
    for sid, stage in STAGE_BY_ID.items():
        d = sent_days.get(sid, stage['default_days'])
        d = _clamp_knob(dict(id=f'{sid} days', min=stage['min'], max=stage['max']), d)
        stage_days[sid] = d
    total = sum(stage_days.values())
    if total > MAX_DAYS:
        raise K.ContractError(
            f'the stages add up to {total:.0f} days and the cap is {MAX_DAYS:.0f}')

    challenge = payload.get('challenge') or 'none'
    if challenge not in CHALLENGE_IDS:
        raise K.ContractError(f'unknown challenge {challenge!r}; '
                              f'one of {", ".join(CHALLENGE_IDS)}')
    try:
        seed = int(payload.get('seed', 7))
    except (TypeError, ValueError):
        raise K.ContractError('seed must be an integer')
    seed = abs(seed) % MAX_SEED

    sp = Setpoints(**values)
    meta = dict(challenge=challenge, seed=seed, clamped=clamped,
                total_days=round(total, 2), genotype=parse_genotype(payload.get('genotype')))
    return sp, stage_days, meta


def _reactor(meta):
    """A reactor with the challenge applied. The line is fixed, not sampled, so
    two conditions differ by what the person changed and nothing else."""
    from analysis_agent.simulator import (Bioreactor, LineState, SensorState,
                                          introduce_sensor_fault, introduce_variant)

    line, sensors = LineState(line_id='SIM'), SensorState()
    g = meta.get('genotype')
    if g:
        line.genotype_mu_ratio = g['growth_ratio']
        line.genotype_diff_ratio = g['diff_ratio']
        if g.get('stage'):
            line.genotype_stages = (g['stage'],)
    ch = meta['challenge']
    if ch == 'variant':
        introduce_variant(line, 0.02)
    elif ch.startswith('fault_'):
        introduce_sensor_fault(sensors, ch.split('_', 1)[1], 0.18)
    return Bioreactor(line, seed=meta['seed'], sensors=sensors)


# Channels the sandbox shows. Everything here is an instrument reading, which is
# why the hidden-truth block never has to be filtered twice.
FRAME_CHANNELS = (
    'vcd_e6_per_ml', 'viability_pct', 'glucose_mM', 'lactate_mM', 'ammonia_mM',
    'ldh_u_per_l', 'ph', 'do_measured', 'oxygen_uptake_rate', 'capacitance_pf_cm',
    'agitation_rpm',
    'agg_diameter_mean_um', 'agg_diameter_sd_um', 'agg_frac_over_300um',
    'agg_count_per_ml', 'harvest_cum_e6_per_ml', 'harvest_cells_e6_per_ml_day',
    'imp_frac_monocyte_cluster', 'imp_viability_pct', 'imp_opacity',
)


def _frames(record):
    out = []
    for o in record.observations:
        f = {'day': o['day'], 'stage': o['stage']}
        for c in FRAME_CHANNELS:
            f[c] = o.get(c, 0.0)
        mc = o.get('minicyto')
        if mc:
            f['cd14_pct'] = mc.get('CD14_PE_pct')
            f['tra_1_60_pct'] = mc.get('TRA_1_60_APC_pct')
        f['read'] = read_condition(f)
        out.append(f)
    return out


def read_condition(frame):
    """Classify one day from its instruments alone.

    Returns {state, score, flags}: `state` in good / strained / failing,
    `score` 0-100 for the vessel's fill colour, `flags` the named channel and
    the number that triggered each. A flag says what a channel reads, not what
    is wrong with the culture -- a fouling probe and a dying culture are both
    entitled to raise one, and telling them apart is the exercise.
    """
    L, flags, penalty = LIMITS, [], 0.0

    def flag(sev, channel, text, excess=0.0):
        """`excess` is how far past the threshold the channel sits, 0-1.

        A flat penalty per flag was wrong: a culture at 5% viability raised the
        same 26 points as one at 74%, so a dead vessel read "strained" while
        every metabolite looked calm -- because nothing was left to consume
        them. The breach now scales with its own size, so a channel that is far
        past its limit can carry the read on its own.
        """
        nonlocal penalty
        base = 26.0 if sev == 'bad' else 11.0
        penalty += base + (62.0 if sev == 'bad' else 14.0) * max(0.0, min(1.0, excess))
        flags.append(dict(severity=sev, channel=channel, detail=text,
                          excess=round(max(0.0, min(1.0, excess)), 3)))

    v = frame.get('viability_pct') or 0.0
    if v < L['viability_poor']:
        flag('bad', 'viability_pct',
             f'viability {v:.0f}% is below {L["viability_poor"]:.0f}%',
             (L['viability_poor'] - v) / L['viability_poor'])
    elif v < L['viability_good']:
        flag('warn', 'viability_pct', f'viability {v:.0f}% is under {L["viability_good"]:.0f}%',
             (L['viability_good'] - v) / (L['viability_good'] - L['viability_poor']))

    # A collapsed culture reads calm on every metabolite channel, because there
    # is nothing left to consume them. Density is what catches it.
    x = frame.get('vcd_e6_per_ml')
    if x is not None and x < L['vcd_collapsed']:
        flag('bad', 'vcd_e6_per_ml',
             f'viable cell density {x:.2f}e6/mL: the culture is effectively gone', 1.0)

    lac = frame.get('lactate_mM') or 0.0
    if lac > L['lactate_severe']:
        flag('bad', 'lactate_mM', f'lactate {lac:.0f} mM is past the half-inhibition point',
             (lac - L['lactate_severe']) / L['lactate_severe'])
    elif lac > L['lactate_high']:
        flag('warn', 'lactate_mM', f'lactate {lac:.0f} mM is inhibitory',
             (lac - L['lactate_high']) / (L['lactate_severe'] - L['lactate_high']))

    amm = frame.get('ammonia_mM') or 0.0
    if amm > L['ammonia_severe']:
        flag('bad', 'ammonia_mM', f'ammonia {amm:.1f} mM is past half-inhibition',
             (amm - L['ammonia_severe']) / L['ammonia_severe'])
    elif amm > L['ammonia_high']:
        flag('warn', 'ammonia_mM', f'ammonia {amm:.1f} mM is accumulating',
             (amm - L['ammonia_high']) / (L['ammonia_severe'] - L['ammonia_high']))

    glc = frame.get('glucose_mM') or 0.0
    if glc < L['glucose_exhausted']:
        flag('bad', 'glucose_mM', f'glucose {glc:.1f} mM: effectively exhausted',
             (L['glucose_exhausted'] - glc) / L['glucose_exhausted'])
    elif glc < L['glucose_low']:
        flag('warn', 'glucose_mM', f'glucose {glc:.1f} mM is near the Monod constant',
             (L['glucose_low'] - glc) / (L['glucose_low'] - L['glucose_exhausted']))

    over = frame.get('agg_frac_over_300um') or 0.0
    if over > L['oversize_severe']:
        flag('bad', 'agg_frac_over_300um',
             f'{over * 100:.0f}% of aggregates are over 300 um, so cores go hypoxic',
             (over - L['oversize_severe']) / (1 - L['oversize_severe']))
    elif over > L['oversize_frac']:
        flag('warn', 'agg_frac_over_300um',
             f'{over * 100:.0f}% of aggregates are over 300 um',
             (over - L['oversize_frac']) / (L['oversize_severe'] - L['oversize_frac']))

    do = frame.get('do_measured') or 0.0
    if do < L['do_low']:
        flag('bad', 'do_measured', f'DO reads {do:.2f}: oxygen transfer is not keeping up',
             (L['do_low'] - do) / L['do_low'])
    elif do > L['do_high']:
        flag('warn', 'do_measured', f'DO reads {do:.2f}, above every stage optimum',
             (do - L['do_high']) / (0.45 - L['do_high']))

    score = max(0.0, min(100.0, 100.0 - penalty))
    state = 'good' if score >= 80 else 'strained' if score >= 50 else 'failing'
    return dict(state=state, score=round(score, 1), flags=flags)


def _signature(frames, outcome):
    """One line per condition, computed: what a person should read off the run."""
    if not frames:
        return {}
    worst = min(frames, key=lambda f: f['read']['score'])
    peak = max(frames, key=lambda f: f['vcd_e6_per_ml'])
    return dict(
        harvest_per_input_ipsc=outcome['harvest_yield_per_input_ipsc'],
        harvest_total_e6_per_ml=outcome['harvest_total_e6_per_ml'],
        cumulative_differentiation_efficiency=outcome[
            'cumulative_differentiation_efficiency'],
        final_viability_pct=outcome['final_viability_pct'],
        peak_vcd_e6_per_ml=peak['vcd_e6_per_ml'], peak_vcd_day=peak['day'],
        worst_day=worst['day'], worst_state=worst['read']['state'],
        worst_score=worst['read']['score'],
        days_failing=sum(1 for f in frames if f['read']['state'] == 'failing'),
        days_strained=sum(1 for f in frames if f['read']['state'] == 'strained'),
        mean_score=round(sum(f['read']['score'] for f in frames) / len(frames), 1),
    )


def simulate(payload):
    """Run one condition and return everything the interface draws."""
    sp, stage_days, meta = parse_condition(payload)
    rec = _reactor(meta).run(sp, run_id='sim', stage_days=stage_days,
                             minicyto_every_days=2)
    frames = _frames(rec)
    return dict(
        evidence_status='synthetic_demonstration',
        bioreactor_source='synthetic_standin',
        process='ipsc_to_monocyte',
        label=str(payload.get('label') or 'condition')[:60],
        setpoints=asdict(sp), stage_days=stage_days,
        challenge=meta['challenge'], challenge_label=CHALLENGE_LABEL[meta['challenge']],
        genotype=meta.get('genotype'),
        seed=meta['seed'], clamped=meta['clamped'],
        total_days=meta['total_days'],
        frames=frames, outcome=rec.outcome, signature=_signature(frames, rec.outcome),
        assay_cost=rec.total_assay_cost,
        note='A sandbox reading from a phenomenological model. Not a measurement '
             'of any real cell, and not evidence for any protocol value.',
    )


def compare(payload):
    """Two conditions and the differences between them, computed here.

    Both run on the same line and the same seed unless the caller changes them,
    so a difference is attributable to the setpoints that differ -- which is the
    one thing a single-replicate comparison can honestly support.
    """
    payload = payload or {}
    conds = payload.get('conditions')
    if not isinstance(conds, list) or len(conds) != 2:
        raise K.ContractError('send {"conditions": [a, b]} with exactly two conditions')
    a, b = (simulate(c) for c in conds)

    changed = [dict(knob=k, label=KNOB_BY_ID[k]['label'], unit=KNOB_BY_ID[k]['unit'],
                    a=a['setpoints'][k], b=b['setpoints'][k])
               for k in KNOB_BY_ID
               if abs(a['setpoints'][k] - b['setpoints'][k]) > 1e-9]
    days_changed = [dict(stage=s, a=a['stage_days'][s], b=b['stage_days'][s])
                    for s in STAGE_BY_ID
                    if abs(a['stage_days'][s] - b['stage_days'][s]) > 1e-9]

    deltas = {}
    for key in ('harvest_per_input_ipsc', 'harvest_total_e6_per_ml',
                'cumulative_differentiation_efficiency', 'final_viability_pct',
                'peak_vcd_e6_per_ml', 'mean_score'):
        av, bv = a['signature'].get(key), b['signature'].get(key)
        if av is None or bv is None:
            continue
        # A percentage against a baseline that has collapsed is arithmetic
        # without meaning -- 5% viability to 75% is not "a 1400% improvement",
        # it is one dead culture and one living one. Suppress it rather than
        # print a number that invites the wrong sentence.
        meaningful = av and abs(av) >= 0.05 * abs(bv)
        deltas[key] = dict(a=av, b=bv, delta=round(bv - av, 4),
                           pct=(round((bv - av) / av * 100, 1) if meaningful else None),
                           pct_withheld=(None if meaningful else
                                         'the A baseline is too small for a percentage'))

    if not changed and not days_changed and a['challenge'] == b['challenge']:
        verdict = ('Both conditions are identical, so any difference is the noise '
                   'model alone.' if a['seed'] != b['seed'] else
                   'Both conditions are identical.')
    else:
        h = deltas.get('harvest_per_input_ipsc', {})
        d = h.get('delta') or 0.0
        moved = ', '.join(c['label'] for c in changed) or 'the stage durations'
        verdict = (f'B {"out-yields" if d > 0 else "under-yields"} A by '
                   f'{abs(d):.3f} monocytes per input iPSC. Changed: {moved}. '
                   f'One replicate each, so the direction is all this supports.')
    return dict(evidence_status='synthetic_demonstration', a=a, b=b,
                changed=changed, stage_days_changed=days_changed, deltas=deltas,
                verdict=verdict,
                note='Single replicate per condition. Directional only, and from a '
                     'model built to reward these levers.')


CURVE_CHANNELS = ('vcd_e6_per_ml', 'viability_pct', 'harvest_cum_e6_per_ml',
                  'imp_frac_monocyte_cluster', 'lactate_mM')


def genotype_compare(payload):
    """Wild type against an engineered line, same setpoints, same seed.

    The one difference between the two runs is the assumed effect of the edit,
    so every difference in the curves is that assumption played through the
    reactor — which is exactly what it can show, and all it can show. A gene's
    real effect is a measurement; these ratios are a person's or a hypothesis's
    guess at it.
    """
    payload = dict(payload or {})
    g = parse_genotype(payload.get('genotype'))
    if not g:
        raise K.ContractError('name the engineered line: {"genotype": {"label", '
                              '"growth_ratio", "diff_ratio"}}')
    factor = g['kind'] == 'factor'
    control = 'without it' if factor else 'wild type'
    window = (f' during {g["stage"]}' if g.get('stage') else
              (' throughout' if factor else ''))
    base = {k: v for k, v in payload.items() if k != 'genotype'}
    wt = dict(base, label=control)
    ed = dict(base, label=g['label'], genotype=g)
    cmp = compare({'conditions': [wt, ed]})
    a, b = cmp['a'], cmp['b']
    curves = {'days': [f['day'] for f in a['frames']],
              'wild_type': {c: [f.get(c) for f in a['frames']] for c in CURVE_CHANNELS},
              'edited': {c: [f.get(c) for f in b['frames']] for c in CURVE_CHANNELS}}
    h = cmp['deltas'].get('harvest_per_input_ipsc') or {}
    pk = cmp['deltas'].get('peak_vcd_e6_per_ml') or {}
    who = f'with {g["label"]}' if factor else g['label']
    verdict = (f'Under the assumed effect{window} (growth ×{g["growth_ratio"]:g}, '
               f'differentiation ×{g["diff_ratio"]:g}), the run {who} '
               f'{"out-yields" if (h.get("delta") or 0) > 0 else "under-yields"} {control} by '
               f'{abs(h.get("delta") or 0):.3f} cells per input iPSC'
               + (f'; peak density {pk["b"]:.2f} ({who}) against {pk["a"]:.2f} ({control}) '
                  f'1e6/mL' if pk else '')
               + '. One replicate each: the direction of an assumption, not a prediction '
                 + ('about the factor.' if factor else 'about the gene.'))
    note = ((f'{g["label"]} is modelled as an assumed change in growth rate and '
             f'differentiation efficiency{window}, nothing else: the reactor has no term for '
             f'it. The numbers are a sandbox reading from an uncalibrated model, never '
             f'evidence about the factor.') if factor else
            'The edit is modelled as an assumed change in growth rate and differentiation '
            'efficiency, nothing else. The numbers are a sandbox reading from an uncalibrated '
            'model, never evidence about the gene.')
    return dict(evidence_status='synthetic_demonstration', process='ipsc_to_monocyte',
                genotype=g, setpoints=a['setpoints'], stage_days=a['stage_days'],
                seed=a['seed'], curves=curves, deltas=cmp['deltas'],
                outcome={'wild_type': a['outcome'], 'edited': b['outcome']},
                signature={'wild_type': a['signature'], 'edited': b['signature']},
                verdict=verdict, control_label=control, note=note)


def seed_brief(result):
    """What a sandbox condition is allowed to become on the way into a loop run.

    Not a protocol. Every quantity leaves here as a `design_choice` whose origin
    is a person turning a knob against a synthetic model, which is exactly the
    provenance class the production loop blocks the wet lab on. The loop still
    has to earn each value; this only says where the search starts.
    """
    sp = result['setpoints']
    return dict(
        origin='simulator_sandbox', evidence_status='synthetic_demonstration',
        process='ipsc_to_monocyte', challenge=result['challenge'],
        seed=result['seed'], stage_days=result['stage_days'],
        quantities=[dict(knob=k, label=KNOB_BY_ID[k]['label'], value=sp[k],
                         unit=KNOB_BY_ID[k]['unit'], stage=KNOB_BY_ID[k]['stage'],
                         provenance='design_choice',
                         basis='chosen by hand in simulator mode against a synthetic '
                               'stand-in; no literature value supports it')
                    for k in KNOB_BY_ID],
        signature=result['signature'],
        prompt=('Optimise an iPSC to monocyte process. Start from the condition I '
                'explored in simulator mode: '
                + ', '.join(f'{KNOB_BY_ID[k]["label"]} {sp[k]:g} '
                            f'{KNOB_BY_ID[k]["unit"]}' for k in KNOB_BY_ID)
                + '. Those are design choices from a synthetic sandbox, not cited '
                  'values, so treat them as a starting point to be tested.'),
        caveat='Carrying these into a loop run carries their provenance with them. '
               'A design_choice quantity blocks the wet lab until a named reviewer '
               'accepts it.',
    )


def config():
    """Everything the interface needs to build itself."""
    return dict(
        process='ipsc_to_monocyte',
        process_label='iPSC → monocyte, four stages',
        # The project profile whose simulator this is. Stated rather than left to
        # be inferred from the knob names, so an interface can tell which project
        # this page serves instead of guessing and quietly guessing wrong.
        model_id='ipsc_monocyte_v1',
        knobs=[dict(k) for k in KNOBS],
        stages=[dict(s) for s in STAGES],
        challenges=[dict(c) for c in CHALLENGES],
        genotype_range=list(GENOTYPE_RANGE),
        limits=dict(LIMITS), max_days=MAX_DAYS,
        channels=list(FRAME_CHANNELS),
        evidence_status='synthetic_demonstration',
        refuses=[
            'returning the stand-in line\'s hidden truth (mu_max, death rate, '
            'clonal fraction) in any payload',
            'treating a sandbox reading as evidence: every quantity leaves as a '
            'design_choice',
            'any process other than the iPSC -> monocyte stand-in',
        ],
        not_modelled=[
            'T-lineage differentiation: the T-cell stand-ins the loop uses are '
            'phenomenological and have no DO, pH, shear or aggregate physics, so '
            'they cannot be driven from these knobs',
            'medium composition beyond glucose, lactate and ammonia',
            'a real vessel of any make',
        ],
    )
