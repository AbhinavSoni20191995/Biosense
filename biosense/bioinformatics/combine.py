"""One readout across several series: a random-effects pooled estimate.

Three public series that each show a gene modestly higher under a cue say more
together than any one of them — and say less, if they disagree. This pools the
same readout (a gene, or a gene-set score) across analysis results that were
each run with the tools here, without touching the data again:

* each series' difference becomes a standardised mean difference (Hedges' g),
  because series differ in platform, normalisation and scale, so raw log2
  differences are not comparable;
* the g values are pooled by DerSimonian-Laird random effects, which widens
  the interval when the series disagree, and the disagreement is reported
  (tau², I², Cochran's Q) rather than averaged away;
* the pooled confidence can be no higher than the best contributing series,
  and is lowered when the series disagree. Evidence from mouse or zebrafish
  stays mouse or zebrafish evidence however many series there are; a pooled
  estimate over the best-evidenced series alone is reported beside it.

What it is not: a re-analysis of the raw data, a batch correction, or a way to
make a weak series strong. Each series' contrast is stated, and pooling series
whose contrasts differ is the planner's claim, recorded with the result.

    python -m biosense.bioinformatics.cli analyse combine \
        --results r1.json r2.json r3.json --readouts PPARG "gene set score" --out pooled.json
"""
from __future__ import annotations

import math

from scipy import stats as sps

from .. import contracts as K

LEVELS = ('low', 'moderate', 'high')


def hedges_g(row):
    """(g, variance) from one statistics row's group means, SDs and sizes, or None."""
    try:
        na, nb = int(row['n_a']), int(row['n_b'])
        ma, mb = float(row['mean_a']), float(row['mean_b'])
        sa, sb = float(row['sd_a']), float(row['sd_b'])
    except (KeyError, TypeError, ValueError):
        return None
    if na < 2 or nb < 2:
        return None
    sp = math.sqrt(((na - 1) * sa ** 2 + (nb - 1) * sb ** 2) / (na + nb - 2))
    if sp <= 0:
        return None
    j = 1 - 3 / (4 * (na + nb) - 9)
    g = j * (mb - ma) / sp
    return g, (na + nb) / (na * nb) + g ** 2 / (2 * (na + nb))


def pool(studies):
    """DerSimonian-Laird over [(g, var), ...] -> the pooled estimate and heterogeneity."""
    k = len(studies)
    w = [1 / v for _, v in studies]
    fixed = sum(wi * g for (g, _), wi in zip(studies, w)) / sum(w)
    q = sum(wi * (g - fixed) ** 2 for (g, _), wi in zip(studies, w))
    c = sum(w) - sum(wi ** 2 for wi in w) / sum(w)
    tau2 = max(0.0, (q - (k - 1)) / c) if c > 0 else 0.0
    ws = [1 / (v + tau2) for _, v in studies]
    est = sum(wi * g for (g, _), wi in zip(studies, ws)) / sum(ws)
    se = math.sqrt(1 / sum(ws))
    z = est / se
    return {'pooled_g': est, 'se': se, 'ci_low': est - 1.96 * se, 'ci_high': est + 1.96 * se,
            'p_value': float(2 * sps.norm.sf(abs(z))), 'tau2': tau2, 'q': q,
            'q_p_value': float(sps.chi2.sf(q, k - 1)) if k > 1 else None,
            'i2': max(0.0, (q - (k - 1)) / q) if q > 0 else 0.0,
            'weights': [wi / sum(ws) for wi in ws]}


def _match(row, wanted):
    name = str(row.get('readout') or '')
    return (name.upper() == wanted.upper()
            or (wanted.lower() == 'gene set score' and name.lower().startswith('gene set score')))


def combine(results, readouts, *, created_by=None):
    """Pool each named readout across the given analysis results."""
    if len(results) < 2:
        raise K.ContractError('pooling needs at least two analysis results')
    if not readouts:
        raise K.ContractError('name the readout(s) to pool: a gene symbol, or "gene set score"')
    rows, skipped = [], []
    for wanted in readouts:
        per = []
        for res in results:
            row = next((r for r in res.get('statistics') or [] if _match(r, wanted)), None)
            gv = hedges_g(row) if row else None
            if gv is None:
                skipped.append({'readout': wanted, 'analysis_id': res.get('analysis_id'),
                                'why': ('not in this result' if row is None else
                                        'no group means, SDs and sizes of at least 2')})
                continue
            comp = res.get('comparison') or {}
            per.append({'analysis_id': res.get('analysis_id'),
                        'datasets': [d.get('dataset_id') for d in res.get('datasets') or []],
                        'contrast': f'{comp.get("treatment")} vs {comp.get("control")}',
                        'confidence': res.get('confidence'),
                        'evidence_class': res.get('source_evidence_class'),
                        'g': gv[0], 'variance': gv[1], 'n_a': row['n_a'], 'n_b': row['n_b']})
        if len(per) < 2:
            continue
        pooled = pool([(s['g'], s['variance']) for s in per])
        for s, w in zip(per, pooled.pop('weights')):
            s['weight'] = w
        levels = [LEVELS.index(s['confidence']) for s in per if s['confidence'] in LEVELS]
        best = LEVELS[max(levels)] if levels else 'low'
        conf = best if pooled['i2'] < 0.5 else LEVELS[max(0, LEVELS.index(best) - 1)]
        top = [(s['g'], s['variance']) for s in per if s['confidence'] == best]
        rows.append({'readout': wanted, 'k': len(per), **pooled,
                     'direction': ('increase' if pooled['ci_low'] > 0 else
                                   'decrease' if pooled['ci_high'] < 0 else 'unknown'),
                     'confidence': conf,
                     'confidence_basis': (f'no higher than the best contributing series ({best})'
                                          + ('; lowered: the series disagree (I² ≥ 50%)'
                                             if pooled['i2'] >= 0.5 else '')),
                     'best_evidence_only': (dict(pool(top), k=len(top), level=best)
                                            if 2 <= len(top) < len(per) else None),
                     'studies': per})
    if not rows:
        raise K.ContractError('no readout is present, with group statistics, in at least two of '
                              'these results: ' + '; '.join(
                                  f'{s["readout"]} in {s["analysis_id"]}: {s["why"]}'
                                  for s in skipped[:10]))
    for r in rows:
        r.get('best_evidence_only') and r['best_evidence_only'].pop('weights', None)
    contrasts = sorted({s['contrast'] for r in rows for s in r['studies']})
    return {'kind': 'combined_analysis', 'created_at': K.now_iso(), 'created_by': created_by,
            'method': 'Hedges g per series; DerSimonian-Laird random-effects pooling',
            'evidence_class': 'derived_analysis', 'rows': rows, 'skipped': skipped,
            'contrasts': contrasts,
            'limitations': (['The series compare different contrasts (' + '; '.join(contrasts)
                             + '): pooling them is a claim that they ask the same question.']
                            if len(contrasts) > 1 else [])
            + ['A pooled estimate is no stronger than the evidence class of its series, and '
               'is not a measurement of this process.'],
            'note': ('Pooled from analysis results, not from the data: each series keeps its own '
                     'analysis, and its weight is shown.')}
