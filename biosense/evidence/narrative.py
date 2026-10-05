"""Plain language, generated from structured facts and validated against them.

Two views, never one. The technical trace stays exactly as it is; this adds a
parallel reading for a scientist who should not have to read JSON to follow what
BioSense is doing.

The mechanism is the point. Every object that can be narrated first produces a
**fact set** — `(fact_id, label, value, unit, estimate_type, source_ref)` — and
the prose is rendered from that. Deterministic templates do it by default, so
this works offline, in CI, and with no model at all.

A model MAY rewrite the prose, and that is where `validate()` earns its place:
every number in the rewritten text must appear in the fact set, and any claim of
statistical significance must map to a recorded q-value. Prose that fails is
rejected and the template output is used instead. So an LLM can make the
language better and cannot make the content up.

`UNITS_IN_PROSE` exists because "+26" and "26%" and "26 percentage points" are
three different claims, and the first is never acceptable.
"""
from __future__ import annotations

import re

from .. import contracts as K
from . import estimates as E

# Numerals that are structural rather than claims: a count of datasets, a step
# number, a year. Checked against the fact set like anything else, but these
# patterns let a renderer emit them without registering each one.
_NUM = re.compile(r'(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')
SIGNIFICANCE_WORDS = ('significant', 'significantly', 'p <', 'p<', 'q <', 'q<')


class Fact:
    """One structured thing prose is allowed to say."""

    __slots__ = ('fact_id', 'label', 'value', 'unit', 'estimate_type', 'source_ref', 'text')

    def __init__(self, fact_id, label, value=None, *, unit=None, estimate_type=None,
                 source_ref=None, text=None):
        self.fact_id = fact_id
        self.label = label
        self.value = value
        self.unit = unit
        self.estimate_type = estimate_type
        self.source_ref = source_ref
        self.text = text

    def numerals(self):
        if self.value is None:
            return set()
        try:
            return {_canon(float(self.value))}
        except (TypeError, ValueError):
            return set()

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def _canon(x):
    """A number as a comparable string, so 26.0 and 26 and 26.00 match."""
    return f'{float(x):.6g}'


def _tolerance(raw):
    """How far a written numeral may legitimately sit from the fact behind it.

    Exact rounding semantics: a numeral written to two decimal places stands for
    any value within half of the last place, so "41.18" covers [41.175, 41.185)
    and "41" covers [40.5, 41.5). Significant-figure tolerance was tried first
    and was far too loose -- it let "40" match 41.18 by rounding to one figure,
    which is precisely the invented number this guard exists to catch.
    """
    s = raw.lstrip('+-').lower()
    exp = 0
    if 'e' in s:
        s, _, e = s.partition('e')
        exp = int(e)
    decimals = len(s.split('.')[1]) if '.' in s else 0
    return 0.5 * (10.0 ** (-decimals)) * (10.0 ** exp)


def _matches(raw, values):
    """Does a written numeral correspond to one of *values*?

    Exact first, then within the rounding tolerance the numeral itself claims.
    A text that says 41.18 where the fact is 41.181666 is rounding; a text that
    says 40 is inventing. The guard has to allow the first and catch the second,
    or every renderer would have to emit full float precision to stay honest.
    """
    try:
        x = float(raw)
    except ValueError:
        return False
    if _canon(x) in values or _canon(abs(x)) in values:
        return True
    tol = _tolerance(raw)
    for v in values:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if abs(fv - x) <= tol or abs(abs(fv) - abs(x)) <= tol:
            return True
    return False


def facts_from_estimate(e, prefix='est'):
    """Every number an Estimate licenses prose to mention."""
    out = []
    mid = e['metric']
    for side in ('baseline', 'candidate'):
        v = (e.get(side) or {}).get('value')
        if v is not None:
            out.append(Fact(f'{prefix}.{mid}.{side}', f'{e.get("label") or mid} {side}', v,
                            unit=e['unit'], estimate_type=e[side]['estimate_type'],
                            source_ref=e[side].get('source_ref')))
    if e['magnitude_estimated']:
        if e['absolute_change'] is not None:
            out.append(Fact(f'{prefix}.{mid}.absolute', f'{mid} absolute change',
                            e['absolute_change'], unit=e['change_unit'],
                            estimate_type=e['estimate_type']))
        if e['relative_change_pct'] is not None:
            out.append(Fact(f'{prefix}.{mid}.relative', f'{mid} relative change',
                            e['relative_change_pct'], unit='%',
                            estimate_type=e['estimate_type']))
        if e['fold_change'] is not None:
            out.append(Fact(f'{prefix}.{mid}.fold', f'{mid} fold change', e['fold_change'],
                            unit='fold', estimate_type=e['estimate_type']))
    iv = e.get('interval')
    if iv:
        out.append(Fact(f'{prefix}.{mid}.ci_low', f'{mid} interval lower', iv['lower'],
                        unit=e['unit'], estimate_type=e['estimate_type']))
        out.append(Fact(f'{prefix}.{mid}.ci_high', f'{mid} interval upper', iv['upper'],
                        unit=e['unit'], estimate_type=e['estimate_type']))
    for side in ('baseline', 'candidate'):
        n = (e.get(side) or {}).get('n')
        if n is not None:
            out.append(Fact(f'{prefix}.{mid}.n_{side}', f'{mid} n ({side})', n))
    # A q-value, where the estimate carries one, is what licenses prose to use
    # the word "significant" at all.
    for lim in e.get('limitations') or ():
        m = re.search(r'BH-q = ([0-9.eE+-]+)', lim)
        if m:
            out.append(Fact(f'{prefix}.{mid}.q_value', f'{mid} BH-q', float(m.group(1))))
    return out


def facts_from_hypothesis(h):
    out = [Fact('hyp.id', 'hypothesis id', text=h['hypothesis_id']),
           Fact('hyp.statement', 'statement', text=h['statement']),
           Fact('hyp.confidence', 'confidence', text=h['confidence']),
           Fact('hyp.parameter', 'parameter', text=h['parameter']['parameter_id']),
           Fact('hyp.direction', 'direction', text=h['parameter']['direction']),
           Fact('hyp.coverage', 'simulator coverage',
                text=h['parameter']['simulator_coverage'])]
    p = h['parameter']
    for key in ('current_value', 'candidate_value'):
        if p.get(key) is not None:
            out.append(Fact(f'hyp.{key}', f'{p["parameter_id"]} {key.replace("_", " ")}',
                            p[key], unit=p.get('unit')))
    if p.get('search_range'):
        out.append(Fact('hyp.range_low', 'search range lower', p['search_range']['lower'],
                        unit=p.get('unit')))
        out.append(Fact('hyp.range_high', 'search range upper', p['search_range']['upper'],
                        unit=p.get('unit')))
    for e in h['expected_effects']:
        out += facts_from_estimate(e, prefix='hyp.effect')
    out.append(Fact('hyp.evidence_count', 'evidence sources', len(h['evidence'])))
    return out


def validate(text, facts, *, allow=()):
    """Every number in *text* must come from a fact. Returns a list of problems.

    This is what lets an LLM improve the prose without being able to invent
    content. `allow` carries numerals a renderer legitimately produced that are
    not biological claims — a list length, a step index.
    """
    known = set()
    for f in facts:
        known |= f.numerals()
    known |= {_canon(a) for a in allow}
    problems = []
    for m in _NUM.finditer(text or ''):
        raw = m.group(0)
        if _matches(raw, known):
            continue
        problems.append(f'{raw!r} appears in the text but in no structured fact')
    low = (text or '').lower()
    if any(w in low for w in SIGNIFICANCE_WORDS):
        # Only an actual p- or q-value licenses a significance claim. An earlier
        # version accepted any fact whose id mentioned a relative change, which
        # let "the difference was significant" ride on "+61.9%" -- a statement
        # about effect size standing in for a statement about evidence.
        if not any(f.fact_id.endswith(('.q_value', '.p_value')) for f in facts):
            problems.append('the text claims statistical significance but no q-value or p-value '
                            'is among the facts')
    return problems


def accept(text, facts, fallback, *, allow=()):
    """Use *text* only if it survives validation; otherwise use *fallback*.

    The one place an LLM rewrite can enter. A rejection is not an error: the
    deterministic sentence is already correct, and the reason is returned so a
    reader can see that a rewrite was declined.
    """
    problems = validate(text, facts, allow=allow)
    if problems:
        return fallback, problems
    return text, []


# ── deterministic renderers ──────────────────────────────────────────────
def say_estimate(e):
    """One sentence about one Estimate, in words rather than symbols."""
    label = e.get('label') or e['metric'].replace('_', ' ')
    tag = {'measured': 'measured', 'derived': 'calculated from measurements',
           'simulated': 'predicted by the simulator', 'predicted': 'forecast by a model',
           'target': 'the target you set'}[e['estimate_type']]
    if not e['magnitude_estimated']:
        return (f'{label}: the direction expected is {e["direction"]}, but the magnitude is not '
                f'yet estimated ({e["withheld_reason"]}).')
    a = (e.get('baseline') or {}).get('value')
    b = (e.get('candidate') or {}).get('value')
    pct = E.is_percent(e['unit'])
    unit = '%' if pct else f' {e["unit"]}'
    chg = e['absolute_change']
    change = (f'{chg:+.4g} percentage points' if e['change_unit'] == E.PP
              else f'{chg:+.4g}{unit}')
    rel = f' ({e["relative_change_pct"]:+.4g}%)' if e['relative_change_pct'] is not None else ''
    head = f'{label} goes from {a:.4g}{unit} to {b:.4g}{unit}' if a is not None and b is not None \
        else f'{label} changes by'
    return f'{head}, a change of {change}{rel} — {tag}.'


def say_hypothesis(h):
    """A short paragraph a scientist can read instead of the JSON."""
    p = h['parameter']
    lines = []
    move = {'increase': 'raising', 'decrease': 'lowering', 'extend': 'extending',
            'shorten': 'shortening', 'earlier': 'bringing forward', 'later': 'delaying',
            'revisit': 'revisiting'}[p['direction']]
    if p.get('current_value') is not None and p.get('candidate_value') is not None:
        lines.append(f'BioSense is asking whether {move} {p["label"]} from '
                     f'{p["current_value"]:.4g} to {p["candidate_value"]:.4g} {p["unit"]} '
                     f'would improve the process.')
    else:
        lines.append(f'BioSense is asking whether {move} {p["label"]} would improve the process.')
    lines.append(h['statement'])
    for e in h['expected_effects']:
        lines.append(say_estimate(e))
    cov = p['simulator_coverage']
    if cov != 'modelled':
        lines.append(f'The simulator for this project has no term for {p["label"]}, so no '
                     f'predicted improvement is produced for it. {p.get("coverage_note") or ""}'
                     .strip())
    classes = sorted({e['evidence_class'].replace('_', ' ') for e in h['evidence']
                      if e['stance'] == 'supportive'})
    if classes:
        lines.append('This rests on ' + ', '.join(classes) + '.')
    against = [e for e in h['evidence'] if e['stance'] == 'contradicting']
    if against:
        lines.append('Against it: ' + '; '.join(e['summary'] for e in against))
    lines.append(f'Confidence is {h["confidence"]}. '
                 f'{h["next_experiment"]["summary"]}')
    lines.append('This is evidence for a decision. It does not change the protocol by itself.')
    return ' '.join(x for x in lines if x)


def steps(items):
    """A numbered process story. Each entry is (text, facts)."""
    out = []
    for i, (text, facts) in enumerate(items, 1):
        problems = validate(text, facts, allow=(i,))
        out.append({'step': i, 'text': text, 'ok': not problems, 'problems': problems,
                    'facts': [f.fact_id for f in facts]})
    return out
