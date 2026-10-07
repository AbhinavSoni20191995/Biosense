"""Identity and purity per sample from single cells, by a stated marker rule.

The question this tool answers: what fraction of the product is on-identity, in
each sample, and does a condition change it? For example, the share of
alveolar-like macrophages (PPARG, MARCO, MRC1, SIGLEC1, ABCG1) in an
iPSC-derived culture.

How it answers, all of it written into the result:

* **A marker rule, not a clustering.** A cell is ON-identity when at least
  `min_markers` of the named markers are detected in it (value above
  `detect_threshold`), and none of the optional `negative_markers` is. The rule
  is applied cell by cell to the values in X as stored, and the result states it
  in words.
* **The sample is the unit of replication.** Cells are counted per sample and
  collapsed to one fraction per sample before anything is compared, so `n` counts
  samples. A thousand cells from one well are one observation of that well.
* **Markers are named by the person.** The marker set comes from the plan
  (`--readouts`); nothing here proposes one. A marker absent from the matrix is
  listed, never substituted.

What it is NOT: an annotation, a cell-type call, or a clustering. It does not
recluster, re-annotate, score against a reference, or run UMAP, and a fraction
it reports is the fraction that passes this rule, which is only as good as the
markers chosen. Detection depends on sequencing depth and dropout, so a fraction
is comparable between conditions processed alike, not an absolute purity.

Needs `biosense[singlecell]`; without it, `run` refuses with the install line.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import sparse as sp

from .... import contracts as K
from ....data.readers import h5ad as H5
from ...toolkit import options as OPT
from ...toolkit import statistics as S

NAME = 'single_cell.identity_purity'
VERSION = '1.0.0'
MODALITIES = ('single_cell_rna',)
REQUIRED_METADATA = ('condition_column', 'control', 'treatments', 'sample_id_column')
ANALYSIS_TYPES = ('identity_purity',)
EXTERNAL_DEPENDENCY = 'singlecell'

AUTO = 'auto'
DEFAULTS = {
    'detect_threshold': 0,      # a marker counts as detected when its value is > this
    'min_markers': AUTO,        # auto = ceil(half the markers present)
    'negative_markers': '',     # comma-separated; any one detected puts a cell OFF-identity
}
MIN_MARKERS, MAX_MARKERS = 2, 50
MIN_CELLS_PER_SAMPLE = 50       # below this a per-sample fraction is noisy
MIN_SAMPLES_SOLID = 3           # below this per group the spread is barely estimated
ROW_CHUNK = 20000               # cells read at a time from a row-major matrix

# var columns that commonly hold gene symbols when var_names are ids. Checked
# only after var_names, and only to find a marker the person named.
SYMBOL_COLUMNS = ('gene_symbols', 'gene_symbol', 'feature_name', 'gene_name', 'gene_names',
                  'symbol', 'symbols')


def _file(manifest):
    for f in manifest['files']:
        if f['file_type'] == 'h5ad':
            from ....data import manifest as MF
            return MF.resolve_path(f['path'])
    raise K.ContractError(f'{manifest["dataset_id"]} carries no .h5ad file')


def _symbols(values):
    """Readouts or a comma list -> unique stripped symbols, first spelling kept."""
    out, seen = [], set()
    for v in values or []:
        for s in str(v).split(','):
            s = s.strip()
            if s and s.upper() not in seen:
                seen.add(s.upper())
                out.append(s)
    return out


def _options(plan):
    opts = OPT.take(plan, DEFAULTS)
    thr = opts['detect_threshold']
    if isinstance(thr, bool) or not isinstance(thr, (int, float)) or not math.isfinite(thr) \
            or thr < 0:
        raise K.ContractError(
            f'detect_threshold must be a number >= 0 (a marker is detected when its value is '
            f'above it); got {thr!r}. Scaled or centred values, where a negative number is '
            f'possible, are not a detection scale: use counts or normalised expression.')
    mm = opts['min_markers']
    if mm != AUTO and (isinstance(mm, bool) or not isinstance(mm, int)):
        raise K.ContractError(f'min_markers must be a whole number of markers or {AUTO!r} '
                              f'(ceil of half the markers present); got {mm!r}')
    neg = opts['negative_markers']
    if not isinstance(neg, str):
        raise K.ContractError(f'negative_markers is a comma-separated list of symbols, e.g. '
                              f'negative_markers=GENE1,GENE2; got {neg!r}')
    return float(thr), mm, _symbols([neg])


def _lookup(ad):
    """symbol (upper) -> {var index: where it matched}, var_names first."""
    names = [str(v) for v in ad.var_names]
    by_name = {}
    for i, v in enumerate(names):
        by_name.setdefault(v.upper(), []).append(i)
    by_col = []
    for col in SYMBOL_COLUMNS:
        if col in ad.var.columns:
            d = {}
            for i, v in enumerate(ad.var[col].astype(str)):
                d.setdefault(v.upper(), []).append(i)
            by_col.append((col, d))
    return names, by_name, by_col


def _match(symbols, names, by_name, by_col, *, role):
    """Each named symbol -> one var index, or listed as missing. Ambiguity refuses."""
    found, missing = [], []
    for s in symbols:
        u = s.upper()
        hits, where = by_name.get(u), 'var_names'
        if not hits:
            for col, d in by_col:
                if d.get(u):
                    hits, where = d[u], f'var[{col!r}]'
                    break
        if not hits:
            missing.append(s)
            continue
        if len(set(hits)) > 1:
            raise K.ContractError(
                f'{role} {s!r} matches {len(set(hits))} features in {where} '
                f'({", ".join(names[i] for i in hits[:6])}). Choosing one, or pooling them, '
                f'would change what the rule counts; name the feature you mean by its var name.')
        found.append({'marker': s, 'var_name': names[hits[0]], 'matched_on': where,
                      'index': hits[0]})
    return found, missing


def _columns(X, idx, n_obs):
    """Dense (n_cells x len(idx)) float array of just these columns, read once.

    A column-major sparse matrix is sliced by column directly. A row-major or
    dense one is read in row chunks so that only the marker columns of each chunk
    are kept, never the whole matrix.
    """
    if not idx:
        return np.zeros((n_obs, 0))
    order = sorted(set(idx))
    back = [order.index(i) for i in idx]

    def dense(block):
        return block.toarray() if sp.issparse(block) else np.asarray(block)

    if getattr(X, 'format', None) == 'csc':
        return dense(X[:, order]).astype(float)[:, back]
    parts = []
    for a in range(0, n_obs, ROW_CHUNK):
        block = X[a:min(a + ROW_CHUNK, n_obs)]
        if sp.issparse(block):               # sparse rows: keep only the marker columns
            parts.append(block[:, order].toarray())
        else:
            parts.append(np.asarray(block)[:, order])
    return np.vstack(parts).astype(float)[:, back]


def run(table, manifest, plan):
    """`table` is unused: a single-cell dataset is read from its .h5ad directly."""
    H5.require()
    design = manifest['experimental_design']
    comp = plan.get('comparison') or {}
    cond = comp.get('group_column') or design.get('condition_column')
    control = comp.get('control') or design.get('control')
    treatments = design.get('treatments') or []
    treatment = comp.get('treatment') or (treatments or [None])[0]
    sample_col = design.get('sample_id_column')
    if not (cond and control and treatment and sample_col):
        raise K.ContractError(
            'an identity/purity comparison needs a condition column, a control level, a '
            'treatment level and a sample id column. The sample column decides what counts as '
            'an independent observation, so it is asked for rather than assumed.')

    markers = _symbols(comp.get('readouts'))
    if not markers:
        raise K.ContractError(
            'name the identity markers in the plan (--readouts GENE1 GENE2 ...), e.g. the '
            'genes that define the product. This tool applies a marker rule you state; it does '
            'not choose markers, cluster, or annotate.')
    if not MIN_MARKERS <= len(markers) <= MAX_MARKERS:
        raise K.ContractError(
            f'an identity rule takes {MIN_MARKERS} to {MAX_MARKERS} markers; {len(markers)} '
            f'were named. One marker is a gate, not an identity; a long list is a signature '
            f'score, which this tool does not compute.')
    thr, min_opt, negatives = _options(plan)
    overlap = sorted({m.upper() for m in markers} & {n.upper() for n in negatives})
    if overlap:
        raise K.ContractError(f'{", ".join(overlap)} named as both an identity marker and a '
                              f'negative marker; a cell cannot be required to have and to lack it')
    if len(negatives) > MAX_MARKERS:
        raise K.ContractError(f'at most {MAX_MARKERS} negative markers; got {len(negatives)}')

    path = _file(manifest)
    ad = H5.read(path, backed=True)
    try:
        obs = ad.obs
        for col in (cond, sample_col):
            if col not in obs.columns:
                raise K.ContractError(f'{col!r} is not an obs column. Present: '
                                      f'{", ".join(obs.columns)}')
        cell_sample = obs[sample_col].astype(str).to_numpy()
        cell_cond = obs[cond].astype(str).to_numpy()
        sample_cond = {}
        for s, c in zip(cell_sample, cell_cond):
            if sample_cond.setdefault(s, c) != c:
                raise K.ContractError(
                    f'sample {s!r} appears under more than one condition; the sample column '
                    f'cannot be the unit of replication if it spans groups')
        a_samples = [s for s, c in sample_cond.items() if c == control]
        b_samples = [s for s, c in sample_cond.items() if c == treatment]
        other = sorted(s for s, c in sample_cond.items() if c not in (control, treatment))
        if len(a_samples) < S.MIN_N or len(b_samples) < S.MIN_N:
            raise K.ContractError(
                f'an identity/purity comparison needs at least two samples per group; found '
                f'{len(a_samples)} {control!r} and {len(b_samples)} {treatment!r}. More cells do '
                f'not help: the replication unit is the sample.')

        names, by_name, by_col = _lookup(ad)
        found, missing = _match(markers, names, by_name, by_col, role='marker')
        if len(found) < MIN_MARKERS:
            raise K.ContractError(
                f'only {len(found)} of the {len(markers)} named markers are in this matrix '
                f'(missing: {", ".join(missing)}); a rule needs at least {MIN_MARKERS}. Checked '
                f'var_names and the symbol column(s) '
                f'{", ".join(c for c, _ in by_col) or "(none present)"}.')
        neg_found, neg_missing = _match(negatives, names, by_name, by_col,
                                        role='negative marker')
        m = len(found)
        k = math.ceil(m / 2) if min_opt == AUTO else min_opt
        if not 1 <= k <= m:
            raise K.ContractError(f'min_markers must be between 1 and the {m} markers present '
                                  f'in the matrix; got {k}')

        # One read of the matrix, marker columns only.
        cols = _columns(ad.X, [f['index'] for f in found] + [f['index'] for f in neg_found],
                        int(ad.n_obs))
    finally:
        if getattr(ad, 'isbacked', False):
            ad.file.close()

    pos = cols[:, :m] > thr                                  # cells x markers
    hits = pos.sum(axis=1)
    passes_positive = hits >= k
    negative = (cols[:, m:] > thr).any(axis=1) if neg_found else np.zeros(len(hits), bool)
    on = passes_positive & ~negative

    per_sample = {}
    for s in list(a_samples) + list(b_samples):
        mask = cell_sample == s
        n = int(mask.sum())
        n_on = int(on[mask].sum())
        per_sample[s] = {
            'condition': sample_cond[s], 'n_cells': n, 'n_on_identity': n_on,
            'fraction_on_identity': n_on / n,
            'n_excluded_by_negative_marker': int((passes_positive & negative)[mask].sum()),
            'marker_detection_rate': {f['marker']: float(pos[mask, j].mean())
                                      for j, f in enumerate(found)},
            'negative_marker_detection_rate': {
                f['marker']: float((cols[mask, m + j] > thr).mean())
                for j, f in enumerate(neg_found)},
        }

    cells_used = sum(v['n_cells'] for v in per_sample.values())
    indep = (f'n counted as {len(a_samples)} and {len(b_samples)} samples; {cells_used} cells '
             f'were collapsed to one fraction per sample first, because the replication unit '
             f'is the sample and not the cell')
    rule = (f'a cell is on-identity when at least {k} of {m} markers '
            f'({", ".join(f["marker"] for f in found)}) have a value above {thr:g} in X'
            + (f', and none of {", ".join(f["marker"] for f in neg_found)} does'
               if neg_found else ''))
    label = (f'on-identity fraction ({k} of {m} markers'
             + (f'; none of {", ".join(f["marker"] for f in neg_found)})' if neg_found else ')'))

    def frac(samples, key=None, j=None):
        if key is None:
            return [per_sample[s]['fraction_on_identity'] for s in samples]
        return [per_sample[s][key][j] for s in samples]

    main = S.compare_groups(label, frac(a_samples), frac(b_samples), group_a=control,
                            group_b=treatment, effect_type='difference_in_fraction',
                            independence_note=f'{indep}; rule: {rule}')
    S.apply_fdr([main])        # the pre-named primary readout: its own family of one
    marker_rows = [
        S.compare_groups(f'marker detection rate: {f["marker"]}',
                         frac(a_samples, 'marker_detection_rate', f['marker']),
                         frac(b_samples, 'marker_detection_rate', f['marker']),
                         group_a=control, group_b=treatment,
                         effect_type='difference_in_fraction',
                         independence_note=f'{indep}; fraction of cells with {f["marker"]} '
                                           f'above {thr:g}')
        for f in found]
    S.apply_fdr(marker_rows)
    rows = [main] + marker_rows

    small = {s: v['n_cells'] for s, v in per_sample.items() if v['n_cells'] < MIN_CELLS_PER_SAMPLE}
    never = [f['marker'] for j, f in enumerate(found) if not pos[:, j].any()]
    norm = (manifest.get('modality_detail') or {}).get('normalization_status', 'unknown')
    checks = [
        {'check': 'markers', 'status': 'WARN' if missing else 'PASS',
         'detail': f'{m} of {len(markers)} named markers found ('
                   + ', '.join(f'{f["marker"]} via {f["matched_on"]}' for f in found) + ')'
                   + (f'; not in this matrix: {", ".join(missing)}' if missing else '')},
        {'check': 'identity rule', 'status': 'PASS',
         'detail': rule + (' (min_markers auto: ceil of half the markers present)'
                           if min_opt == AUTO else ' (min_markers set in the plan)')},
        {'check': 'negative markers',
         'status': 'WARN' if (negatives and (neg_missing or not neg_found)) else 'PASS',
         'detail': ('none named' if not negatives else
                    f'{len(neg_found)} of {len(negatives)} found'
                    + (f' ({", ".join(f["marker"] for f in neg_found)})' if neg_found else '')
                    + (f'; not in this matrix, so not applied: {", ".join(neg_missing)}'
                       if neg_missing else '')
                    + f'; {int((passes_positive & negative).sum())} cell(s) met the marker '
                      f'rule and were put off-identity by a negative marker')},
        {'check': 'markers never detected', 'status': 'WARN' if never else 'PASS',
         'detail': (f'{", ".join(never)} detected in no cell above {thr:g}; the rule is then '
                    f'effectively over fewer markers' if never else
                    'every marker present is detected in at least one cell')},
        {'check': 'cells per sample', 'status': 'WARN' if small else 'PASS',
         'detail': '; '.join(f'{s}: {v["n_cells"]}' for s, v in per_sample.items())
                   + (f'. Under {MIN_CELLS_PER_SAMPLE} cells in {", ".join(small)}: those '
                      f'fractions are noisy' if small else '')},
        {'check': 'group sizes',
         'status': 'PASS' if min(len(a_samples), len(b_samples)) >= MIN_SAMPLES_SOLID
         else 'WARN',
         'detail': f'{control}: {len(a_samples)} sample(s); {treatment}: {len(b_samples)}'
                   + (f'. Samples in other conditions, not compared: {", ".join(other)}'
                      if other else '')},
        {'check': 'replication unit', 'status': 'PASS', 'detail': indep},
        {'check': 'values the rule reads', 'status': 'PASS',
         'detail': f'X as stored ({norm}); detection above {thr:g}. Detection depends on depth '
                   f'and dropout, so the fraction compares conditions processed alike and is '
                   f'not an absolute purity'},
        {'check': 'method', 'status': 'PASS',
         'detail': 'per-cell marker rule, one on-identity fraction per sample, Welch between '
                   'conditions with n = samples. Not a clustering and not an annotation: no '
                   'reclustering, re-annotation, reference mapping or UMAP was run.'},
        {'check': 'multiplicity', 'status': 'PASS',
         'detail': f'the on-identity fraction is the primary readout and is tested alone; the '
                   f'{len(marker_rows)} per-marker detection rates are Benjamini-Hochberg '
                   f'corrected among themselves'},
    ]
    if len(treatments) > 1 and not comp.get('treatment'):
        checks.append({'check': 'treatment level', 'status': 'WARN',
                       'detail': f'the design lists {len(treatments)} treatments; compared '
                                 f'{treatment!r}, the first. Name another with '
                                 f'--treatment-level.'})
    return {
        'statistics': rows,
        'quality_control': {'checks': checks,
                            'passed': all(c['status'] != 'FAIL' for c in checks)},
        'comparison': {'group_column': cond, 'control': control, 'treatment': treatment,
                       'paired': False, 'sample_column': sample_col,
                       'samples_a': a_samples, 'samples_b': b_samples,
                       'samples_not_compared': other,
                       'cells_total': cells_used,
                       'rule': {'statement': rule, 'markers': [f['marker'] for f in found],
                                'markers_missing': missing,
                                'marker_features': [{x: f[x] for x in
                                                     ('marker', 'var_name', 'matched_on')}
                                                    for f in found],
                                'min_markers': k,
                                'min_markers_source': 'auto' if min_opt == AUTO else 'plan',
                                'detect_threshold': thr,
                                'negative_markers': [f['marker'] for f in neg_found],
                                'negative_markers_missing': neg_missing,
                                'is_clustering': False, 'is_annotation': False},
                       'per_sample': per_sample,
                       'independence_note': indep},
        'software': f'{S.SOFTWARE}, anndata',
        'independent_units_min': min(len(a_samples), len(b_samples)),
        'replicate_n_min': min(len(a_samples), len(b_samples)),
    }
