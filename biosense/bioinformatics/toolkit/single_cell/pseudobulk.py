"""Single-cell comparison by pseudobulk, which is the conservative choice.

The decision this tool exists to avoid: cell-level differential expression.
Treating 120 cells from one donor as 120 independent observations makes every
p-value tiny for a reason that has nothing to do with biology — the replication
unit is the sample, not the cell. So the matrix is collapsed to one profile per
sample first, `n` counts samples, and the row says so.

What it does NOT do, by design: recluster, re-annotate, or run UMAP. Those are
exactly the steps that should not run unless they resolve the named uncertainty,
and a tool that offers them by default invites running them by default.

Needs `biosense[singlecell]`; without it, `run` refuses with the install line
rather than quietly analysing something else.
"""
from __future__ import annotations

import numpy as np

from .... import contracts as K
from ....data.readers import h5ad as H5
from ...toolkit import statistics as S

NAME = 'single_cell.pseudobulk_comparison'
VERSION = '1.0.0'
MODALITIES = ('single_cell_rna',)
REQUIRED_METADATA = ('condition_column', 'control', 'treatments', 'sample_id_column')
ANALYSIS_TYPES = ('pseudobulk_comparison', 'cell_composition_comparison')
EXTERNAL_DEPENDENCY = 'singlecell'

# Conservative, decision-oriented signatures. Each is a small, named gene set a
# reader can check, not a learned signature: the point is to score a culture's
# state, not to discover one.
SIGNATURES = {
    'proliferation': ('MKI67', 'TOP2A', 'CCNB1', 'PCNA', 'CDK1'),
    'stress_apoptosis': ('HSPA1A', 'HSPA1B', 'CASP3', 'DDIT3', 'JUN', 'FOS'),
    'myeloid_identity': ('CD14', 'ITGAM', 'CSF1R', 'SPI1', 'MRC1'),
    'exhaustion': ('PDCD1', 'LAG3', 'HAVCR2', 'TOX', 'TIGIT'),
    'memory': ('CCR7', 'SELL', 'TCF7', 'IL7R'),
}


def _file(manifest):
    for f in manifest['files']:
        if f['file_type'] == 'h5ad':
            from ....data import manifest as MF
            return MF.resolve_path(f['path'])
    raise K.ContractError(f'{manifest["dataset_id"]} carries no .h5ad file')


def run(table, manifest, plan):
    """`table` is unused: a single-cell dataset is read from its .h5ad directly."""
    H5.require()
    design = manifest['experimental_design']
    comp = plan.get('comparison') or {}
    cond = comp.get('group_column') or design.get('condition_column')
    control = comp.get('control') or design.get('control')
    treatment = comp.get('treatment') or (design.get('treatments') or [None])[0]
    sample_col = design.get('sample_id_column')
    if not (cond and control and treatment and sample_col):
        raise K.ContractError(
            'a pseudobulk comparison needs a condition column, a control level, a treatment '
            'level and a sample id column. The sample column decides what counts as an '
            'independent observation, so it is asked for rather than assumed.')

    path = _file(manifest)
    ad = H5.read(path, backed=False)
    try:
        obs = ad.obs
        for col in (cond, sample_col):
            if col not in obs.columns:
                raise K.ContractError(f'{col!r} is not an obs column. Present: '
                                      f'{", ".join(obs.columns)}')
        sample_cond = {}
        for s, c in zip(obs[sample_col].astype(str), obs[cond].astype(str)):
            if sample_cond.setdefault(s, c) != c:
                raise K.ContractError(
                    f'sample {s!r} appears under more than one condition; the sample column '
                    f'cannot be the unit of replication if it spans groups')
        a_samples = [s for s, c in sample_cond.items() if c == control]
        b_samples = [s for s, c in sample_cond.items() if c == treatment]
        if len(a_samples) < 2 or len(b_samples) < 2:
            raise K.ContractError(
                f'pseudobulk needs at least two samples per group; found {len(a_samples)} '
                f'{control!r} and {len(b_samples)} {treatment!r}. More cells do not help: the '
                f'replication unit is the sample.')

        wanted = comp.get('readouts') or None
        pb = H5.pseudobulk(path, sample_column=sample_col, genes=wanted)
        pos = {s: i for i, s in enumerate(pb['samples'])}
        M, genes = pb['matrix'], pb['genes']
        indep = (f'n counted as {len(a_samples)} and {len(b_samples)} samples; '
                 f'{ad.n_obs} cells collapsed to one profile per sample first, because the '
                 f'replication unit is the sample and not the cell')

        rows = []
        ai = [pos[s] for s in a_samples]
        bi = [pos[s] for s in b_samples]
        for j, g in enumerate(genes):
            rows.append(S.compare_groups(g, M[ai, j], M[bi, j], group_a=control,
                                         group_b=treatment, effect_type='difference',
                                         independence_note=indep))
        # Signature scores: the mean of a named gene set per sample, compared the
        # same way. Reported alongside the genes, never instead of them.
        upper = {g.upper(): j for j, g in enumerate(genes)}
        for sig, members in SIGNATURES.items():
            idx = [upper[m] for m in members if m in upper]
            if len(idx) < 3:
                continue
            a_vals = M[np.ix_(ai, idx)].mean(axis=1)
            b_vals = M[np.ix_(bi, idx)].mean(axis=1)
            rows.append(S.compare_groups(
                f'score:{sig}', a_vals, b_vals, group_a=control, group_b=treatment,
                independence_note=f'{indep}; score over {len(idx)} of '
                                  f'{len(members)} genes in the set'))
        S.apply_fdr(rows)

        comp_counts = {}
        if 'cell_type' in obs.columns:
            for s in pb['samples']:
                m = obs[sample_col].astype(str) == s
                vc = obs.loc[m, 'cell_type'].astype(str).value_counts()
                comp_counts[s] = {k: int(v) for k, v in vc.items()}

        checks = [
            {'check': 'replication unit', 'status': 'PASS',
             'detail': indep},
            {'check': 'group sizes',
             'status': 'PASS' if min(len(a_samples), len(b_samples)) >= 3 else 'WARN',
             'detail': f'{control}: {len(a_samples)} sample(s); {treatment}: {len(b_samples)}'},
            {'check': 'cells per sample', 'status': 'PASS',
             'detail': '; '.join(f'{s}: {n}' for s, n in
                                 list(pb['cells_per_sample'].items())[:6])},
            {'check': 'clustering', 'status': 'PASS',
             'detail': 'no reclustering, re-annotation or UMAP was run. Those steps change what '
                       'the data says and are only run when they resolve the named uncertainty.'},
            {'check': 'multiplicity', 'status': 'PASS',
             'detail': f'Benjamini-Hochberg across {len(rows)} features and scores'},
        ]
        return {
            'statistics': rows,
            'quality_control': {'checks': checks, 'passed': True},
            'comparison': {'group_column': cond, 'control': control, 'treatment': treatment,
                           'paired': False, 'sample_column': sample_col,
                           'samples_a': a_samples, 'samples_b': b_samples,
                           'cells_total': int(ad.n_obs),
                           'cells_per_sample': pb['cells_per_sample'],
                           'composition': comp_counts,
                           'independence_note': indep},
            'software': f'{S.SOFTWARE}, anndata',
            'independent_units_min': min(len(a_samples), len(b_samples)),
            'replicate_n_min': min(len(a_samples), len(b_samples)),
        }
    finally:
        if getattr(ad, 'isbacked', False):
            ad.file.close()
