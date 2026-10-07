"""Does a named set of genes move together between two conditions?

The screening comparison answers "which genes moved most", genome-wide. A
question about a module — "does this cue bundle shift the alveolar identity
genes (PPARG, ABCG1, MRC1, MARCO, SIGLEC1)?" — is a different question, and a
top-genes list does not answer it: the module's genes may move together
modestly and never reach the top of a genome-wide ranking. This scores the set.

Per sample, each gene in the set is standardised across the compared samples
(so a highly expressed gene does not outweigh the rest) and the sample's score
is the mean over the genes present. The two conditions' scores are compared
with the same Welch test the other tools use; each member gene is reported
beside it with BH-FDR across the set, so a score carried by one gene is
visible as such.

What it is not: a gene-set enrichment against a background (no permutation of
genes, no ranking), and not a statement about the set's biology. The set is
the planner's, named in the plan and recorded with the result.
"""
from __future__ import annotations

import numpy as np

from .... import contracts as K
from ...toolkit import statistics as S
from . import basic_de as DE

NAME = 'bulk.gene_set_score'
VERSION = '1.0.0'
MODALITIES = ('bulk_rna', 'generic_table')
REQUIRED_METADATA = ('condition_column', 'control', 'treatments')
ANALYSIS_TYPES = ('gene_set_score',)
MIN_GENES = 2
MAX_GENES = 200


def run(table, manifest, plan):
    design = manifest['experimental_design']
    comp = plan.get('comparison') or {}
    cond = comp.get('group_column') or design.get('condition_column')
    control = comp.get('control') or design.get('control')
    treatment = comp.get('treatment') or (design.get('treatments') or [None])[0]
    if not (cond and control and treatment):
        raise K.ContractError('a gene-set score needs a condition column, a control level and a '
                              'treatment level; they are not inferred')
    wanted = [str(g).strip() for g in comp.get('readouts') or [] if str(g).strip()]
    if len(wanted) < MIN_GENES:
        raise K.ContractError(f'name the gene set in the plan (--readouts GENE GENE ...), at '
                              f'least {MIN_GENES} genes; it is the planner\'s, never guessed')
    if len(wanted) > MAX_GENES:
        raise K.ContractError(f'{len(wanted)} genes; a set is at most {MAX_GENES}. A larger '
                              f'question is a genome-wide comparison')

    feature = (DE._named(table, comp.get('feature_column'))
               or DE._pick(table, DE.FEATURE_COLUMNS, 'feature'))
    value = (DE._named(table, comp.get('value_column'))
             or DE._pick(table, DE.VALUE_COLUMNS, 'expression value'))
    vals = table.numeric_column(value)
    if np.all(vals >= 0) and np.all(np.isclose(vals, np.round(vals))) and vals.max() > 1000:
        raise K.ContractError(f'column {value!r} looks like raw integer counts; score normalised '
                              f'log expression, not counts')

    sample_col = design.get('sample_id_column') or 'sample_id'
    if sample_col not in table.columns:
        raise K.ContractError(f'a per-sample score needs the sample column {sample_col!r}; the '
                              f'table has {", ".join(table.columns)}')
    upper = {g.upper(): g for g in wanted}
    per_gene = {}           # gene -> {sample: value}
    group_of = {}           # sample -> condition
    for i, r in enumerate(table.rows):
        g = str(r[feature]).upper()
        if g not in upper or r[cond] not in (control, treatment):
            continue
        per_gene.setdefault(upper[g], {})[r[sample_col]] = vals[i]
        group_of[r[sample_col]] = r[cond]
    present = [g for g in wanted if g in per_gene]
    missing = [g for g in wanted if g not in per_gene]
    if len(present) < MIN_GENES:
        raise K.ContractError(
            f'only {len(present)} of the {len(wanted)} genes are in this table '
            f'({", ".join(present) or "none"}); missing: {", ".join(missing)}. A set score '
            f'needs at least {MIN_GENES}. Check the identifiers (symbols, not Ensembl ids).')

    samples = sorted(s for s in group_of if all(s in per_gene[g] for g in present))
    z = {}
    for g in present:
        x = np.array([per_gene[g][s] for s in samples], dtype=float)
        sd = x.std(ddof=1) if len(x) > 1 else 0.0
        z[g] = (x - x.mean()) / sd if sd > 0 else np.zeros_like(x)
    score = np.mean(np.vstack([z[g] for g in present]), axis=0)
    a = [score[i] for i, s in enumerate(samples) if group_of[s] == control]
    b = [score[i] for i, s in enumerate(samples) if group_of[s] == treatment]
    if len(a) < S.MIN_N or len(b) < S.MIN_N:
        raise K.ContractError(f'{control}: {len(a)} and {treatment}: {len(b)} samples carry '
                              f'every gene of the set; a comparison needs {S.MIN_N} per group')
    label = str(comp.get('set_name') or plan.get('question') or 'gene set')[:80]
    head = S.compare_groups(f'gene set score ({len(present)} genes)', a, b, group_a=control,
                            group_b=treatment, effect_type='difference',
                            independence_note=f'{len(a)} vs {len(b)} samples')
    head['effect_type'] = 'difference_in_mean_gene_z_score'
    members = []
    for g in present:
        ga = [per_gene[g][s] for s in samples if group_of[s] == control]
        gb = [per_gene[g][s] for s in samples if group_of[s] == treatment]
        members.append(S.compare_groups(g, ga, gb, group_a=control, group_b=treatment,
                                        effect_type='difference'))
    S.apply_fdr(members)
    for m in members:
        m['effect_type'] = 'difference_in_log2_expression'
    up = sum(1 for m in members if (m.get('effect') or 0) > 0)
    checks = [
        {'check': 'genes found', 'status': 'PASS' if not missing else 'WARN',
         'detail': f'{len(present)} of {len(wanted)} in the table'
                   + (f'; missing: {", ".join(missing)}' if missing else '')},
        {'check': 'agreement within the set', 'status': 'PASS',
         'detail': f'{up} of {len(present)} genes higher in {treatment}: a score carried by one '
                   f'or two genes is visible in the per-gene rows'},
        {'check': 'group sizes', 'status': 'PASS' if min(len(a), len(b)) >= 3 else 'WARN',
         'detail': f'{control}: {len(a)}; {treatment}: {len(b)}'},
        {'check': 'method', 'status': 'WARN',
         'detail': 'mean of per-gene z-scores per sample, Welch test between conditions. Not a '
                   'background-controlled enrichment test.'},
    ]
    return {
        'statistics': [head] + members,
        'quality_control': {'checks': checks, 'passed': True},
        'comparison': {'group_column': cond, 'control': control, 'treatment': treatment,
                       'paired': False, 'feature_column': feature, 'value_column': value,
                       'gene_set': {'name': label, 'requested': wanted, 'present': present,
                                    'missing': missing}},
        'software': S.SOFTWARE,
        'independent_units_min': min(len(a), len(b)),
        'replicate_n_min': min(len(a), len(b)),
    }
