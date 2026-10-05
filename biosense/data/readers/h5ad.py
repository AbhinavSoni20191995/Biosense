"""Reading .h5ad single-cell matrices.

Needs `biosense[singlecell]` (anndata + h5py). When the extra is absent,
`require()` raises with the install line rather than letting an analysis
proceed on something cheaper.

**Scanpy is deliberately not a dependency.** QC, population proportions,
pseudobulk and score computation are numpy/scipy work this repository already
does well; clustering, re-annotation and UMAP are exactly the steps that should
not run unless they resolve the named uncertainty, and a dependency that makes
them one line away invites running them by default.

What `describe` records is what the manifest needs and nothing it would have to
guess: cell and gene counts, the obs and var keys actually present, the values
each candidate design column takes, whether a normalisation layer exists, and
which design fields are still missing. It never infers which column holds the
condition — that is asked for.
"""
from __future__ import annotations

from pathlib import Path

from ... import contracts as K

EXTRA = 'singlecell'
INSTALL = 'uv sync --extra singlecell   (anndata + h5py)'

# obs columns that commonly hold a design variable. Used ONLY to list candidates
# for a person to choose from, never to pick one.
DESIGN_HINTS = {
    'condition': ('condition', 'treatment', 'group', 'stim', 'perturbation', 'genotype'),
    'sample': ('sample', 'sample_id', 'library', 'orig.ident', 'channel'),
    'donor': ('donor', 'patient', 'subject', 'individual'),
    'batch': ('batch', 'run', 'lane', 'experiment'),
    'cell_type': ('cell_type', 'celltype', 'cluster', 'annotation', 'leiden', 'louvain'),
    'timepoint': ('time', 'timepoint', 'day', 'hour'),
}
MAX_LEVELS = 40


def available():
    try:
        import anndata  # noqa: F401
        return True
    except ImportError:
        return False


def require():
    if not available():
        raise K.ContractError(
            f'reading .h5ad needs the optional single-cell environment, which is not installed. '
            f'Install it with:  {INSTALL}. BioSense refuses rather than analysing something '
            f'cheaper and reporting it as a single-cell result.')


def read(path, *, backed=True):
    """Open an .h5ad. `backed` keeps the matrix on disk, which matters at scale."""
    require()
    import anndata
    p = Path(path)
    if not p.is_file():
        raise K.ContractError(f'{p} is not a file')
    try:
        return anndata.read_h5ad(p, backed='r' if backed else None)
    except (OSError, ValueError) as e:
        raise K.ContractError(f'{p.name} could not be read as .h5ad: {type(e).__name__}: {e}')


def _levels(series, limit=MAX_LEVELS):
    try:
        vals = list(dict.fromkeys(str(v) for v in series))
    except TypeError:
        return None
    return vals if len(vals) <= limit else vals[:limit] + ['…']


def describe(path):
    """Everything a DatasetManifest needs, and an explicit list of what is missing."""
    require()
    ad = read(path)
    try:
        obs_keys = list(ad.obs.columns)
        var_keys = list(ad.var.columns)
        n_cells, n_genes = int(ad.n_obs), int(ad.n_vars)
        layers = list(ad.layers.keys()) if ad.layers is not None else []
        obsm = list(ad.obsm.keys()) if ad.obsm is not None else []

        levels = {}
        for k in obs_keys:
            lv = _levels(ad.obs[k])
            if lv is not None and len(lv) <= MAX_LEVELS:
                levels[k] = lv

        candidates = {}
        low = {k.lower(): k for k in obs_keys}
        for role, hints in DESIGN_HINTS.items():
            hits = [low[k] for k in low if any(h in k for h in hints)]
            if hits:
                candidates[role] = hits

        # Whether the matrix looks normalised, stated as an observation rather
        # than a decision. A tool that needs counts checks this and refuses.
        norm = 'unknown'
        try:
            import numpy as np
            sample = ad.X[:min(50, n_cells)]
            arr = np.asarray(sample.todense() if hasattr(sample, 'todense') else sample,
                             dtype=float)
            if arr.size:
                if np.allclose(arr, np.round(arr)) and arr.max() > 30:
                    norm = 'looks like raw integer counts'
                elif arr.max() <= 30:
                    norm = 'looks log-transformed (max <= 30)'
                else:
                    norm = 'non-integer values; normalised in some way'
        except Exception:  # noqa: BLE001 - a description must not fail on a read quirk
            norm = 'unknown'

        return {
            'n_cells': n_cells, 'n_genes': n_genes,
            'obs_keys': obs_keys, 'var_keys': var_keys,
            'layers': layers, 'obsm': obsm,
            'obs_levels': levels,
            'design_candidates': candidates,
            'normalization_status': norm,
            'note': 'Candidate design columns are listed for a person to choose from. BioSense '
                    'does not pick which column holds the condition: a confident comparison of '
                    'the wrong column is worse than a question.',
        }
    finally:
        if getattr(ad, 'isbacked', False):
            ad.file.close()


def pseudobulk(path, *, sample_column, genes=None):
    """Mean expression per sample. The unit of replication is the sample, not the cell.

    Cell-level differential expression treats thousands of cells from one donor
    as thousands of independent observations, which makes any p-value tiny for a
    reason that has nothing to do with biology. Collapsing to one profile per
    sample first is the conservative, decision-oriented choice, and it is what
    the downstream two-group comparison then sees.
    """
    require()
    import numpy as np
    ad = read(path, backed=False)
    try:
        if sample_column not in ad.obs.columns:
            raise K.ContractError(
                f'{sample_column!r} is not an obs column. Present: '
                f'{", ".join(ad.obs.columns)}')
        var_names = list(ad.var_names)
        idx = list(range(len(var_names)))
        if genes:
            missing = [g for g in genes if g not in var_names]
            if missing:
                raise K.ContractError(f'gene(s) not in this dataset: {", ".join(missing[:8])}')
            pos = {g: i for i, g in enumerate(var_names)}
            idx = [pos[g] for g in genes]
            var_names = list(genes)

        samples, rows = [], []
        obs = ad.obs[sample_column].astype(str)
        for s in dict.fromkeys(obs):
            mask = (obs == s).to_numpy()
            sub = ad.X[mask]
            arr = np.asarray(sub.todense() if hasattr(sub, 'todense') else sub, dtype=float)
            samples.append(s)
            rows.append(arr[:, idx].mean(axis=0))
        return {'samples': samples, 'genes': var_names,
                'matrix': np.vstack(rows) if rows else np.zeros((0, len(idx))),
                'cells_per_sample': {s: int((obs == s).sum()) for s in samples},
                'note': 'Pseudobulk: one profile per sample. n is a count of samples, never of '
                        'cells.'}
    finally:
        if getattr(ad, 'isbacked', False):
            ad.file.close()
