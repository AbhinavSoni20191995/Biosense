"""Building Estimates: the arithmetic, and the refusals.

Four rules, each of which exists because the alternative would mislead someone:

* **`estimate_type` is the weakest link.** An effect computed from measured data
  is `derived`, not `measured`; a difference between two simulated values is
  `simulated`. A caller cannot label a simulated difference `measured` by
  passing the wrong argument, because `combine_type` decides it from the inputs.

* **Percentages subtract, they do not divide.** 42% -> 68% is *+26 percentage
  points*, and calling it "+62%" is a different and much larger-sounding claim
  about a different quantity. When both sides are percentages the change unit is
  `percentage_points` and the relative figure is reported separately, clearly
  labelled.

* **A ratio against a baseline near zero is arithmetic without meaning.** It is
  withheld with a reason rather than printed.

* **No magnitude is a state, not an absence.** `direction_only()` produces a
  valid Estimate with `magnitude_estimated: false` and a required reason, so
  "direction predicted: increase; magnitude: not yet estimated" is representable
  and testable rather than being a missing field somebody fills in later.
"""
from __future__ import annotations

import math

from .. import contracts as K

TYPES = ('measured', 'derived', 'simulated', 'predicted', 'target', 'judgement')
# Weakest-link order: a result is only as direct as its least direct input.
_STRENGTH = {'measured': 0, 'derived': 1, 'simulated': 2, 'predicted': 3, 'target': 4,
             'judgement': 5}
# A best guess says how sure its author is, in the hypothesis' own words, and
# the evidence it points at has to be able to carry that.
CONFIDENCE = ('low', 'moderate', 'high')
_MIN_REFS = {'low': 0, 'moderate': 1, 'high': 2}

PERCENT_UNITS = ('%', 'percent', 'pct', 'percentage')
PP = 'percentage_points'
# Below this fraction of the candidate, a baseline makes a ratio meaningless.
RELATIVE_FLOOR = 0.05


def value(v, estimate_type, *, source_ref=None, n=None, sd=None, note=None):
    if estimate_type not in TYPES:
        raise K.ContractError(f'estimate_type must be one of {TYPES}, got {estimate_type!r}')
    if v is not None and not math.isfinite(float(v)):
        raise K.ContractError('an estimate value must be a finite number or null')
    return {'value': None if v is None else float(v), 'estimate_type': estimate_type,
            'source_ref': source_ref, 'n': n, 'sd': sd, 'note': note}


def change_type(*types):
    """What a CHANGE computed from these inputs is.

    The weakest link, with one extra rule: a difference between two measured
    values is `derived`, never `measured`. No instrument measured the
    difference — code subtracted two readings, and a reader who sees MEASURED
    beside "+26 percentage points" will believe something stronger than is true.
    Two simulated values stay `simulated`, because there the thing worth knowing
    is that a model produced them at all.
    """
    weakest = combine_type(*types)
    return 'derived' if weakest == 'measured' else weakest


def combine_type(*types):
    """The weakest of several estimate types."""
    present = [t for t in types if t]
    if not present:
        raise K.ContractError('combine_type needs at least one estimate type')
    for t in present:
        if t not in TYPES:
            raise K.ContractError(f'unknown estimate_type {t!r}')
    return max(present, key=lambda t: _STRENGTH[t])


def is_percent(unit):
    return str(unit or '').strip().lower() in PERCENT_UNITS


def interval(lower, upper, kind, *, level=None, method=None):
    if lower is None or upper is None:
        return None
    if lower > upper:
        lower, upper = upper, lower
    if kind not in ('confidence_interval', 'prediction_interval', 'range', 'search_range',
                    'best_guess_range'):
        raise K.ContractError(f'unknown interval type {kind!r}')
    return {'lower': float(lower), 'upper': float(upper), 'type': kind,
            'level': level, 'method': method}


def estimate(metric, unit, baseline, candidate, *, label=None, higher_is_better=None,
             interval_=None, evidence_refs=(), limitations=(), fold_change_ok=False):
    """A full Estimate with the arithmetic computed and the refusals applied."""
    if baseline is None or candidate is None:
        raise K.ContractError('an estimate needs both a baseline and a candidate value; use '
                              'direction_only() when the magnitude cannot be computed')
    a, b = baseline.get('value'), candidate.get('value')
    etype = change_type(baseline['estimate_type'], candidate['estimate_type'])

    if a is None or b is None:
        return direction_only(
            metric, unit, 'unknown', etype,
            reason='one side of the comparison has no value',
            label=label, baseline=baseline, candidate=candidate,
            higher_is_better=higher_is_better, evidence_refs=evidence_refs,
            limitations=limitations)

    percent = is_percent(unit)
    absolute = b - a
    change_unit = PP if percent else unit

    # A ratio needs a baseline that is actually there. 0.1% -> 5% is not a
    # "4900% improvement" in any sense a reader will take correctly.
    rel, rel_withheld = None, None
    if a == 0:
        rel_withheld = 'the baseline is zero, so a relative change is undefined'
    elif abs(a) < RELATIVE_FLOOR * max(abs(b), 1e-12):
        rel_withheld = (f'the baseline ({a:g}) is too small relative to the candidate ({b:g}) '
                        f'for a percentage to carry meaning')
    else:
        rel = round(absolute / abs(a) * 100.0, 2)

    fold = None
    if fold_change_ok and a > 0 and b > 0:
        fold = round(b / a, 4)
    elif fold_change_ok:
        limitations = list(limitations) + [
            'a fold change needs both values positive; it was not computed']

    direction = 'increase' if absolute > 0 else 'decrease' if absolute < 0 else 'no_change'
    favourable = None
    if higher_is_better is not None and direction != 'no_change':
        favourable = (direction == 'increase') == bool(higher_is_better)

    out = {
        'metric': metric, 'label': label, 'unit': unit, 'estimate_type': etype,
        'baseline': baseline, 'candidate': candidate,
        'magnitude_estimated': True, 'withheld_reason': None, 'direction': direction,
        'absolute_change': round(absolute, 6), 'change_unit': change_unit,
        'relative_change_pct': rel, 'relative_withheld_reason': rel_withheld,
        'fold_change': fold, 'interval': interval_,
        'higher_is_better': higher_is_better, 'favourable': favourable,
        'evidence_refs': list(evidence_refs), 'limitations': list(limitations),
    }
    K.require_valid('estimate', out)
    return out


def direction_only(metric, unit, direction, estimate_type, *, reason, label=None,
                   baseline=None, candidate=None, higher_is_better=None,
                   evidence_refs=(), limitations=()):
    """A known direction with no computable magnitude.

    The correct output when evidence says "this goes up" and nothing says by how
    much. Invented precision is the alternative, and it is worse.
    """
    if not (reason or '').strip():
        raise K.ContractError('withholding a magnitude requires a reason')
    if direction not in ('increase', 'decrease', 'no_change', 'unknown'):
        raise K.ContractError(f'unknown direction {direction!r}')
    favourable = None
    if higher_is_better is not None and direction in ('increase', 'decrease'):
        favourable = (direction == 'increase') == bool(higher_is_better)
    out = {
        'metric': metric, 'label': label, 'unit': unit, 'estimate_type': estimate_type,
        'baseline': baseline, 'candidate': candidate,
        'magnitude_estimated': False, 'withheld_reason': reason, 'direction': direction,
        'absolute_change': None, 'change_unit': None,
        'relative_change_pct': None, 'relative_withheld_reason': None,
        'fold_change': None, 'interval': None,
        'higher_is_better': higher_is_better, 'favourable': favourable,
        'evidence_refs': list(evidence_refs), 'limitations': list(limitations),
    }
    K.require_valid('estimate', out)
    return out


def best_guess(metric, unit, *, low, high, confidence, rationale, central=None,
               direction=None, would_change_it=None, label=None, higher_is_better=None,
               evidence_refs=(), limitations=()):
    """A labelled best guess at a change: a range, a confidence, and the reasoning.

    For when the evidence points somewhere but does not measure the size —
    the person still needs a starting point, and "not established" alone gives
    them none. It is typed `judgement`, the weakest estimate type there is, so
    it never makes a hypothesis QUANTIFIED, never combines into anything
    stronger, and reads as a guess wherever it is shown. The range is the
    claim; a central value is optional and never computed by averaging.
    """
    if confidence not in CONFIDENCE:
        raise K.ContractError(f'a best guess needs a confidence of {CONFIDENCE}, got {confidence!r}')
    if not (rationale or '').strip():
        raise K.ContractError('a best guess needs its rationale: which evidence, which context '
                              'differences, and what was missing')
    refs = [r for r in evidence_refs or () if str(r).strip()]
    if len(refs) < _MIN_REFS[confidence]:
        raise K.ContractError(
            f'a {confidence}-confidence best guess needs at least {_MIN_REFS[confidence]} '
            f'evidence reference(s); it has {len(refs)}. Lower the confidence or cite the sources.')
    for v in (low, high, central):
        if v is not None and not math.isfinite(float(v)):
            raise K.ContractError('a best guess is finite numbers')
    if low is None or high is None:
        raise K.ContractError('a best guess is a range: give low and high')
    low, high = sorted((float(low), float(high)))
    if central is not None and not low <= float(central) <= high:
        raise K.ContractError(f'the central guess {central} lies outside its own range {low}..{high}')
    if direction is None:
        direction = 'increase' if low > 0 else 'decrease' if high < 0 else 'unknown'
    if direction not in ('increase', 'decrease', 'no_change', 'unknown'):
        raise K.ContractError(f'unknown direction {direction!r}')
    favourable = None
    if higher_is_better is not None and direction in ('increase', 'decrease'):
        favourable = (direction == 'increase') == bool(higher_is_better)
    out = {
        'metric': metric, 'label': label, 'unit': unit, 'estimate_type': 'judgement',
        'baseline': None, 'candidate': None,
        'magnitude_estimated': True, 'withheld_reason': None, 'direction': direction,
        'absolute_change': None if central is None else float(central),
        'change_unit': PP if is_percent(unit) else unit,
        'relative_change_pct': None, 'relative_withheld_reason': None,
        'fold_change': None,
        'interval': interval(low, high, 'best_guess_range', method='judgement'),
        'higher_is_better': higher_is_better, 'favourable': favourable,
        'judgement': {'confidence': confidence, 'rationale': rationale.strip(),
                      'would_change_it': (would_change_it or '').strip() or None},
        'evidence_refs': refs,
        'limitations': list(limitations) + [
            'A best guess (JUDGEMENT): reasoned from the cited evidence, not measured or '
            'computed. It is a starting point to test, and a person approves it before any '
            'protocol uses it.'],
    }
    K.require_valid('estimate', out)
    return out


def from_statistics_row(row, metric_unit, *, source_ref, higher_is_better=None, label=None):
    """An AnalysisResult statistics row -> an Estimate.

    The group means are **measured** (an instrument produced them); the effect
    between them is **derived** (code computed it). That is exactly why
    estimate_type sits on each number rather than on the row.
    """
    ci = interval(row.get('ci_low'), row.get('ci_high'), 'confidence_interval',
                  level=0.95, method=row.get('ci_method'))
    base = value(row.get('mean_a'), 'measured', source_ref=source_ref,
                 n=row.get('n_a'), sd=row.get('sd_a'))
    cand = value(row.get('mean_b'), 'measured', source_ref=source_ref,
                 n=row.get('n_b'), sd=row.get('sd_b'))
    if base['value'] is None or cand['value'] is None:
        return direction_only(row['readout'], metric_unit, row.get('direction', 'unknown'),
                              'derived',
                              reason=row.get('note') or 'the comparison produced no group means',
                              label=label, higher_is_better=higher_is_better,
                              evidence_refs=[source_ref])
    e = estimate(row['readout'], metric_unit, base, cand, label=label,
                 higher_is_better=higher_is_better, interval_=ci,
                 evidence_refs=[source_ref])
    lims = list(e['limitations'])
    if row.get('q_value') is not None:
        lims.append(f'{row["test"]}, BH-q = {row["q_value"]:.3g}')
    if row.get('q_value') is not None and row['q_value'] >= 0.05:
        lims.append('this readout does not survive multiplicity correction at q < 0.05')
    e['limitations'] = lims
    K.require_valid('estimate', e)
    return e


def render(e, *, nd=3):
    """One line a person can read, with the label the number has earned."""
    tag = e['estimate_type'].upper()
    if e['estimate_type'] == 'judgement':
        iv, j = e.get('interval') or {}, e.get('judgement') or {}
        unit = 'percentage points' if e.get('change_unit') == PP else e['unit']
        mid = '' if e['absolute_change'] is None else f', central {e["absolute_change"]:+.4g}'
        return (f'{e["label"] or e["metric"]}: best guess {iv.get("lower"):+.4g} to '
                f'{iv.get("upper"):+.4g} {unit}{mid} [{tag} · {j.get("confidence", "?")} confidence]')
    if not e['magnitude_estimated']:
        return (f'{e["label"] or e["metric"]}: direction {e["direction"]}, '
                f'magnitude not yet estimated ({e["withheld_reason"]}) [{tag}]')
    a = e['baseline']['value'] if e.get('baseline') else None
    b = e['candidate']['value'] if e.get('candidate') else None
    unit = '' if is_percent(e['unit']) else f' {e["unit"]}'
    head = f'{e["label"] or e["metric"]}: '
    if a is not None and b is not None:
        head += f'{_n(a, nd)}{"%" if is_percent(e["unit"]) else unit} → ' \
                f'{_n(b, nd)}{"%" if is_percent(e["unit"]) else unit}'
    chg = e['absolute_change']
    if e['change_unit'] == PP:
        head += f'  {chg:+.4g} percentage points'
    else:
        head += f'  {chg:+.4g}{unit}'
    if e['relative_change_pct'] is not None:
        head += f' ({e["relative_change_pct"]:+.4g}%)'
    return f'{head} [{tag}]'


def _n(v, nd=3):
    return f'{v:.{nd}g}'
