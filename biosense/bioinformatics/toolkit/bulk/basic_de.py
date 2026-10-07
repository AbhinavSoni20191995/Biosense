"""A minimal bulk-expression comparison over a long-format table.

Deliberately small. It exists so that modality dispatch is real rather than a
single hard-coded cytometry path, and so the shape of a differential-expression
result is fixed before DESeq2 or edgeR arrive behind the external-tool adapter.

What it is: per-gene Welch comparison of a normalised expression column between
two conditions, with BH-FDR across genes.

What it is **not**: a replacement for DESeq2 or edgeR. It does no count-level
dispersion modelling, no library-size normalisation and no shrinkage. On raw
counts it would be wrong, so it refuses a column that looks like raw counts and
says which tool to run instead.
"""
from __future__ import annotations

import numpy as np

from .... import contracts as K
from ...toolkit import statistics as S

NAME = 'bulk.expression_comparison'
VERSION = '1.0.0'
MODALITIES = ('bulk_rna', 'generic_table')
REQUIRED_METADATA = ('condition_column', 'control', 'treatments')
ANALYSIS_TYPES = ('bulk_expression_comparison',)

FEATURE_COLUMNS = ('gene', 'feature', 'symbol', 'gene_symbol', 'transcript')
VALUE_COLUMNS = ('expression_log2', 'log2_expression', 'logcpm', 'log_cpm', 'normalized',
                 'normalised', 'expression', 'value', 'tpm', 'fpkm')


def _pick(table, candidates, what):
    low = {c.lower(): c for c in table.columns}
    for c in candidates:
        if c in low:
            return low[c]
    raise K.ContractError(
        f'no {what} column found. Looked for {", ".join(candidates)}; the table has '
        f'{", ".join(table.columns)}. Name it in the plan rather than letting this guess.')


def run(table, manifest, plan):
    design = manifest['experimental_design']
    comp = plan.get('comparison') or {}
    cond = comp.get('group_column') or design.get('condition_column')
    control = comp.get('control') or design.get('control')
    treatment = comp.get('treatment') or (design.get('treatments') or [None])[0]
    if not (cond and control and treatment):
        raise K.ContractError('a bulk comparison needs a condition column, a control level and a '
                              'treatment level; they are not inferred')

    feature = _pick(table, FEATURE_COLUMNS, 'feature')
    value = _pick(table, VALUE_COLUMNS, 'expression value')
    vals = table.numeric_column(value)

    # Raw counts through a Welch test is the classic wrong answer, so it is refused
    # rather than computed and caveated.
    if np.all(vals >= 0) and np.all(np.isclose(vals, np.round(vals))) and vals.max() > 1000:
        raise K.ContractError(
            f'column {value!r} looks like raw integer counts (max {vals.max():.0f}). This tool '
            f'compares normalised expression; raw counts need dispersion modelling. Normalise '
            f'first, or run DESeq2/edgeR through the external-tool adapter and register the '
            f'result as a derived dataset.')

    features = table.levels(feature)
    if len(features) > 5000:
        raise K.ContractError(f'{len(features)} features is beyond what this minimal tool is for; '
                              f'use the external-tool adapter')

    # One pass to group rows by gene: scanning the whole table once per gene
    # made a few-thousand-gene public series take minutes.
    by_gene = {}
    for i, r in enumerate(table.rows):
        by_gene.setdefault(r[feature], []).append(i)
    rows = []
    for g in features:
        idx = by_gene.get(g, [])
        a = [vals[i] for i in idx if table.rows[i][cond] == control]
        b = [vals[i] for i in idx if table.rows[i][cond] == treatment]
        if len(a) < S.MIN_N or len(b) < S.MIN_N:
            continue
        rows.append(S.compare_groups(g, a, b, group_a=control, group_b=treatment,
                                     effect_type='difference',
                                     independence_note=f'{len(a)} vs {len(b)} samples per group'))
    if not rows:
        raise K.ContractError('no feature has at least two samples in both groups')
    S.apply_fdr(rows)
    for r in rows:
        r['effect_type'] = 'difference_in_log2_expression'

    n_a = len({table.rows[i][design.get('replicate_column') or cond]
               for i in table.where(cond, control)})
    n_b = len({table.rows[i][design.get('replicate_column') or cond]
               for i in table.where(cond, treatment)})
    checks = [
        {'check': 'value column', 'status': 'PASS',
         'detail': f'{value!r} read as normalised expression on a log scale'},
        {'check': 'group sizes', 'status': 'PASS' if min(n_a, n_b) >= 3 else 'WARN',
         'detail': f'{control}: {n_a} sample(s); {treatment}: {n_b}'},
        {'check': 'method', 'status': 'WARN',
         'detail': 'per-feature Welch t-test with BH-FDR. No dispersion modelling, no shrinkage: '
                   'this is a screening comparison, not a DESeq2/edgeR result.'},
    ]
    return {
        'statistics': rows,
        'quality_control': {'checks': checks, 'passed': True},
        'comparison': {'group_column': cond, 'control': control, 'treatment': treatment,
                       'paired': False, 'feature_column': feature, 'value_column': value,
                       'features_tested': len(rows)},
        'software': S.SOFTWARE,
        'independent_units_min': min(n_a, n_b),
        'replicate_n_min': min(n_a, n_b),
    }
