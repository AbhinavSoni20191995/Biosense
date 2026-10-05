"""Deterministic two-group statistics. scipy does the arithmetic; nothing here guesses.

Everything an analysis reports about a difference comes through this module, so
the rules live in one place:

* **n is a count of independent observations.** Four wells from one donor are not
  four donors, and `independence_note` says which one was counted.
* **A test is chosen by the data, not by the answer.** Fewer than the minimum
  replicates for a rank test and the rank test is not run; the choice and its
  reason travel with the row.
* **Multiplicity is corrected across the readouts in a result**, by
  Benjamini-Hochberg, and the q-value is reported beside the p-value rather than
  instead of it.
* **A p-value is not a direction and not a decision.** `direction` is read off
  the means; whether it matters is for the orchestrator.

No model computes any of this, and none of it reads a dataset's hidden anything:
the inputs are two arrays.
"""
from __future__ import annotations

import numpy as np
from scipy import stats as sps

MIN_N = 2                 # below this nothing can be compared at all
MIN_N_RANK = 4            # below this a rank test has no resolution worth reporting
BOOTSTRAP_DRAWS = 4000
SOFTWARE = f'scipy {sps.__name__ and __import__("scipy").__version__}, numpy {np.__version__}'


def benjamini_hochberg(pvalues):
    """BH-FDR q-values, order preserved. None passes through as None."""
    idx = [i for i, p in enumerate(pvalues) if p is not None and np.isfinite(p)]
    out = [None] * len(pvalues)
    if not idx:
        return out
    p = np.asarray([pvalues[i] for i in idx], dtype=float)
    m = p.size
    order = np.argsort(p)
    ranked = p[order]
    q = ranked * m / np.arange(1, m + 1)
    q = np.minimum.accumulate(q[::-1])[::-1]       # enforce monotonicity
    q = np.clip(q, 0.0, 1.0)
    for pos, o in enumerate(order):
        out[idx[o]] = float(q[pos])
    return out


def _bootstrap_ci(a, b, draws=BOOTSTRAP_DRAWS, seed=0, alpha=0.05):
    """Percentile CI on the difference of means. Seeded, so a result reproduces."""
    if a.size < MIN_N or b.size < MIN_N:
        return None, None, None
    rng = np.random.default_rng(seed)
    diffs = (rng.choice(b, (draws, b.size), replace=True).mean(axis=1)
             - rng.choice(a, (draws, a.size), replace=True).mean(axis=1))
    lo, hi = np.percentile(diffs, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi), f'percentile bootstrap, {draws} resamples, seed {seed}'


def compare_groups(readout, a, b, *, group_a='control', group_b='treatment', paired=False,
                   effect_type='difference', seed=0, independence_note=None):
    """Compare two groups on one readout. Returns one statistics row.

    `effect_type` 'difference' reports b - a in the readout's own units;
    'log2_fold_change' reports log2(mean_b / mean_a) and needs positive means.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    row = {'readout': readout, 'group_a': group_a, 'group_b': group_b,
           'n_a': int(a.size), 'n_b': int(b.size), 'n': int(a.size + b.size),
           'mean_a': None, 'mean_b': None, 'sd_a': None, 'sd_b': None,
           'effect': None, 'effect_type': effect_type, 'ci_low': None, 'ci_high': None,
           'ci_method': None, 'p_value': None, 'q_value': None, 'test': 'none',
           'direction': 'unknown', 'note': None}

    if a.size < MIN_N or b.size < MIN_N:
        row['note'] = (f'{group_a} n={a.size}, {group_b} n={b.size}: fewer than {MIN_N} '
                       f'independent observations in a group, so nothing is compared. '
                       f'A single observation has no spread to compare against.')
        return row
    if paired and a.size != b.size:
        row['note'] = (f'a paired comparison needs equal group sizes; got {a.size} and {b.size}. '
                       f'Run it unpaired or fix the pairing column.')
        return row

    row['mean_a'], row['mean_b'] = float(a.mean()), float(b.mean())
    row['sd_a'] = float(a.std(ddof=1)) if a.size > 1 else None
    row['sd_b'] = float(b.std(ddof=1)) if b.size > 1 else None

    if effect_type == 'log2_fold_change':
        if row['mean_a'] <= 0 or row['mean_b'] <= 0:
            row['note'] = ('a log2 fold change needs positive means in both groups; reporting the '
                           'difference instead would silently change what the number means')
            row['effect_type'] = 'undefined'
        else:
            row['effect'] = float(np.log2(row['mean_b'] / row['mean_a']))
    else:
        row['effect'] = row['mean_b'] - row['mean_a']
        row['ci_low'], row['ci_high'], row['ci_method'] = _bootstrap_ci(a, b, seed=seed)

    # Both groups constant: a test would divide by zero and a p-value would be
    # meaningless, so it is not produced.
    def add_note(text):
        row['note'] = f'{row["note"]}; {text}' if row['note'] else text

    if np.allclose(a, a[0]) and np.allclose(b, b[0]):
        row['test'] = 'none'
        add_note('both groups are constant; no test has anything to work with')
    elif paired:
        try:
            if a.size >= MIN_N_RANK:
                res = sps.wilcoxon(b, a)
                row['test'] = 'wilcoxon signed-rank (paired)'
            else:
                res = sps.ttest_rel(b, a)
                row['test'] = f"paired t-test (n={a.size} < {MIN_N_RANK}, rank test has no resolution)"
            row['p_value'] = float(res.pvalue)
        except ValueError as e:
            add_note(f'paired test not run: {e}')
    else:
        res = sps.ttest_ind(b, a, equal_var=False)
        row['test'] = "Welch's t-test"
        row['p_value'] = float(res.pvalue)
        if a.size >= MIN_N_RANK and b.size >= MIN_N_RANK:
            u = sps.mannwhitneyu(b, a, alternative='two-sided')
            row['test'] = "Welch's t-test (Mann-Whitney U reported alongside)"
            add_note(f'Mann-Whitney U p={float(u.pvalue):.4g}')
        else:
            add_note(f'no rank test: it needs at least {MIN_N_RANK} per group and this has '
                     f'{a.size} and {b.size}')

    if row['effect'] is not None:
        row['direction'] = ('increase' if row['effect'] > 0 else
                            'decrease' if row['effect'] < 0 else 'no_change')
    if independence_note:
        row['note'] = f'{row["note"] + "; " if row["note"] else ""}{independence_note}'
    return row


def fisher_counts(readout, a_pos, a_n, b_pos, b_n, *, group_a='control', group_b='treatment'):
    """Fisher exact on counts, for a proportion measured as events rather than a percentage."""
    table = [[b_pos, b_n - b_pos], [a_pos, a_n - a_pos]]
    if min(min(r) for r in table) < 0:
        raise ValueError('a positive count cannot exceed its total')
    odds, p = sps.fisher_exact(table, alternative='two-sided')
    pa = a_pos / a_n if a_n else None
    pb = b_pos / b_n if b_n else None
    return {'readout': readout, 'test': 'Fisher exact', 'group_a': group_a, 'group_b': group_b,
            'mean_a': pa, 'mean_b': pb, 'sd_a': None, 'sd_b': None,
            'n_a': int(a_n), 'n_b': int(b_n), 'n': int(a_n + b_n),
            'effect': (pb - pa) if (pa is not None and pb is not None) else None,
            'effect_type': 'difference_in_proportion',
            'ci_low': None, 'ci_high': None, 'ci_method': None,
            'p_value': float(p), 'q_value': None,
            'direction': ('increase' if pb > pa else 'decrease' if pb < pa else 'no_change'),
            'note': f'odds ratio {float(odds):.3g}'}


def spearman(readout, x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    if x.size < 3 or x.size != y.size:
        return {'readout': readout, 'test': 'spearman', 'effect': None,
                'effect_type': 'rho', 'p_value': None, 'q_value': None, 'n': int(x.size),
                'direction': 'unknown', 'group_a': None, 'group_b': None,
                'mean_a': None, 'mean_b': None, 'sd_a': None, 'sd_b': None,
                'n_a': None, 'n_b': None, 'ci_low': None, 'ci_high': None, 'ci_method': None,
                'note': 'a rank correlation needs at least 3 paired observations'}
    res = sps.spearmanr(x, y)
    rho = float(res.statistic)
    return {'readout': readout, 'test': 'spearman rank correlation', 'effect': rho,
            'effect_type': 'rho', 'p_value': float(res.pvalue), 'q_value': None,
            'n': int(x.size), 'group_a': None, 'group_b': None,
            'mean_a': None, 'mean_b': None, 'sd_a': None, 'sd_b': None,
            'n_a': None, 'n_b': None, 'ci_low': None, 'ci_high': None, 'ci_method': None,
            'direction': 'increase' if rho > 0 else 'decrease' if rho < 0 else 'no_change',
            'note': 'correlation is not a mechanism and not a direction to move a parameter in'}


def apply_fdr(rows):
    """Fill q_value across a set of rows, in place, and return them."""
    qs = benjamini_hochberg([r.get('p_value') for r in rows])
    for r, q in zip(rows, qs):
        r['q_value'] = q
    return rows
