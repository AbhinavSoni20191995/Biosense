"""Registering datasets and finding them again.

A manifest is written once, immutably (`overwrite=False`), under the root its
visibility dictates:

    private_data/<dataset_id>/manifest.json      visibility private
    data_cache/<dataset_id>/manifest.json        visibility public, fetched
    examples/datasets/<dataset_id>/manifest.json committed synthetic fixtures

The root is chosen from the manifest, not from the caller, so a private dataset
cannot be registered into a served directory by passing the wrong argument.
Listing is the one place that reads across roots, and it returns manifests, which
hold no payload: `files[].path` names where the data is, and for a private
dataset that path is inside the private root and `roots.assert_servable` refuses
it.
"""
from __future__ import annotations

from pathlib import Path

from .. import contracts as K
from . import roots

MANIFEST = 'manifest.json'


def root_for(m):
    """Where a manifest of this visibility belongs."""
    return roots.private_root() if m['visibility'] == 'private' else roots.cache_root()


def _dirs():
    """Every root a dataset may be registered in, public ones first."""
    return [roots.FIXTURES, roots.cache_root(), roots.private_root()]


def register(m, *, root=None, overwrite=False):
    """Write a manifest under the root its visibility dictates. Returns the path."""
    K.require_valid('dataset_manifest', m)
    base = Path(root) if root is not None else root_for(m)
    if m['visibility'] == 'private' and not roots.is_private_path(base / m['dataset_id']):
        raise K.ContractError(
            f'refusing to register private dataset {m["dataset_id"]!r} outside the private root '
            f'({roots.private_root()}). Private data does not get stored somewhere servable.')
    path = Path(base) / m['dataset_id'] / MANIFEST
    K.write_json_atomic(path, m, overwrite=overwrite)
    return path


def load(dataset_id, *, dirs=None):
    """The manifest for *dataset_id*, or None. Searches public roots before private."""
    for base in (dirs or _dirs()):
        p = Path(base) / dataset_id / MANIFEST
        if p.is_file():
            try:
                return K.read_json(p)
            except ValueError:
                continue
    return None


def require(dataset_id, *, dirs=None):
    m = load(dataset_id, dirs=dirs)
    if m is None:
        raise K.ContractError(
            f'no dataset {dataset_id!r} is registered. Register it before planning an analysis; '
            f'BioSense does not analyse data it cannot describe.')
    return m


def list_datasets(*, include_private=True, dirs=None):
    """Every registered manifest, newest first.

    `include_private=False` is what an HTTP handler passes: it is a filter on the
    roots that are read at all, not a filter applied to the rows afterwards, so a
    bug in the row filter cannot leak one.
    """
    bases = dirs if dirs is not None else _dirs()
    if not include_private:
        bases = [b for b in bases if Path(b).resolve() != roots.private_root()]
    out, seen = [], set()
    for base in bases:
        b = Path(base)
        if not b.is_dir():
            continue
        for p in sorted(b.glob(f'*/{MANIFEST}')):
            try:
                m = K.read_json(p)
            except ValueError:
                continue
            if m.get('dataset_id') in seen:
                continue
            if not include_private and m.get('visibility') == 'private':
                continue  # belt and braces: a private manifest found anywhere is still filtered
            seen.add(m.get('dataset_id'))
            out.append(m)
    return sorted(out, key=lambda m: m.get('created_at') or '', reverse=True)


def summary(m):
    """The row an interface shows. Carries no file path and no payload."""
    files = m.get('files') or []
    return {
        'dataset_id': m['dataset_id'], 'title': m['title'],
        'source': m['source'], 'accession': m.get('accession'),
        'visibility': m['visibility'], 'evidence_class': m['evidence_class'],
        'modality': m['modality'], 'organism': m.get('organism'),
        'cell_type': m.get('cell_type'), 'perturbation': m.get('perturbation'),
        'conditions': m.get('conditions') or [],
        'sample_count': (m.get('sample_metadata') or {}).get('sample_count'),
        'file_count': len(files),
        'file_types': sorted({f['file_type'] for f in files}),
        'analysable': any(f['file_type'] in ('csv', 'tsv', 'json') for f in files),
        'missing_metadata': [x['field'] for x in m.get('missing_metadata') or []],
        'derived_from': (m.get('derived_from') or {}).get('parent_dataset_id'),
        'created_at': m.get('created_at'),
        'citable': False,
    }


def lineage_of(dataset_id, *, dirs=None):
    from . import manifest as MF
    m = require(dataset_id, dirs=dirs)
    return MF.lineage(m, lambda i: load(i, dirs=dirs))
