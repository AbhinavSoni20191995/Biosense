"""Building and validating a DatasetManifest, and resolving a derived dataset's lineage.

Three facts stay apart, because collapsing them is how provenance is lost:

    evidence_class          what this object IS
    visibility              who may see it
    source / derived_from   where it came from

An analysis over a private FACS table is therefore a `derived_analysis` whose
source is `private_user_dataset` and whose visibility is `private` — not a
"private dataset". The distinction survives one hop or ten: `lineage` walks the
`derived_from` chain to the roots and reports what they were.

Nothing in here infers a design field. A manifest with no `condition_column`
records that in `missing_metadata`; the plan that needs it is refused and names
it. Guessing which column holds the condition is exactly the kind of help that
produces a confident comparison of the wrong thing.
"""
from __future__ import annotations

from pathlib import Path

from .. import contracts as K

# Fields an analysis commonly needs. Each is asked for by name when absent.
DESIGN_FIELDS = {
    'condition_column': 'which column separates the groups being compared',
    'control': 'which value of the condition column is the reference',
    'treatments': 'which values of the condition column are the test groups',
    'replicate_column': 'how replicates are identified, so n is a count of '
                        'independent observations rather than of rows',
    'batch_column': 'whether a batch effect could explain a difference',
    'sample_id_column': 'how to join the measurement table to its metadata',
    'donor_column': 'whether observations are independent between people',
    'timepoint_column': 'whether a change is a trend or a snapshot',
}

# Flow-cytometry description. Held in `modality_detail` rather than the schema so
# that single-cell, ChIP and ATAC blocks can be added without a schema migration.
FLOW_FIELDS = ('panel_markers', 'fluorophores', 'gating_hierarchy', 'population_names',
               'compensation', 'instrument')

READABLE_FILE_TYPES = ('csv', 'tsv', 'json')
DECLARABLE_FILE_TYPES = ('h5ad', 'fcs', 'bed', 'narrowPeak', 'mtx', 'other')

CLASS_FOR_SOURCE = {
    'user_upload': 'private_user_dataset',
    'derived': 'derived_analysis',
    'fixture': 'synthetic_fixture',
}
PUBLIC_SOURCES = ('geo', 'encode', 'biostudies', 'sc_expression_atlas', 'flow_repository', 'immport')


def stored_path(path):
    """How a path is written into a manifest.

    Relative to the repository root when the file is inside it, absolute
    otherwise. A committed fixture manifest holding `/home/someone/...` is a
    manifest that works on exactly one machine, and a private dataset outside
    the tree keeps its real path because nothing else can find it.
    """
    p = Path(path).resolve()
    try:
        return str(p.relative_to(K.ROOT))
    except ValueError:
        return str(p)


def resolve_path(stored):
    """The inverse: a stored path back to a file on this machine."""
    p = Path(stored)
    return p if p.is_absolute() else (K.ROOT / p)


def file_entry(path, role, file_type=None, columns=None, rows=None):
    """One file, hashed. The checksum is the thing that makes a result traceable."""
    p = Path(path)
    if not p.is_file():
        raise K.ContractError(f'{p} is not a file')
    ft = file_type or p.suffix.lstrip('.').lower()
    if ft == 'txt':
        ft = 'tsv'
    if ft not in READABLE_FILE_TYPES + DECLARABLE_FILE_TYPES:
        ft = 'other'
    return {'path': stored_path(p), 'role': role, 'file_type': ft,
            'checksum_sha256': K.sha256_file(p), 'bytes': p.stat().st_size,
            'rows': rows, 'columns': list(columns) if columns else None}


def missing_design(design, needed=()):
    """Design fields that were not supplied, each with why it matters."""
    out = []
    for field in needed or DESIGN_FIELDS:
        v = (design or {}).get(field)
        if v in (None, '', []):
            out.append({'field': field, 'why_it_matters': DESIGN_FIELDS.get(
                field, 'required by the analysis that was planned')})
    return out


def build(dataset_id, title, source, modality, organism, files, *, visibility=None,
          evidence_class=None, accession=None, source_url=None, retrieved_at=None,
          cell_type=None, perturbation=None, conditions=(), sample_metadata=None,
          experimental_design=None, modality_detail=None, derived_from=None,
          description=None, registered_by=None, notes=None, limitations=()):
    """Assemble and validate a manifest. Refuses rather than filling anything in."""
    if source not in PUBLIC_SOURCES + ('user_upload', 'derived', 'fixture'):
        raise K.ContractError(f'unknown dataset source {source!r}')

    # visibility and class follow from the source unless explicitly narrowed. The
    # one direction that is never inferred is private -> public.
    if visibility is None:
        visibility = 'private' if source == 'user_upload' else 'public'
    if visibility not in K.VISIBILITIES:
        raise K.ContractError(f'visibility must be one of {K.VISIBILITIES}')
    if evidence_class is None:
        evidence_class = CLASS_FOR_SOURCE.get(source, 'public_dataset')
    if source == 'user_upload' and visibility != 'private':
        raise K.ContractError(
            'a user_upload is private. Nothing a person ingests is published by this system; '
            'to share it, publish it to a repository and register the accession.')
    if source == 'derived' and not derived_from:
        raise K.ContractError('a derived dataset must name derived_from.parent_dataset_id, '
                              'the tool and its version')
    if source in PUBLIC_SOURCES and not accession:
        raise K.ContractError(f'a {source} dataset must carry its accession')

    design = dict(experimental_design or {})
    design.setdefault('condition_column', None)
    design.setdefault('control', None)
    design.setdefault('treatments', [])
    design.setdefault('replicate_column', None)

    detail = dict(modality_detail or {})
    if modality == 'flow_cytometry':
        # Never invented: an absent panel is reported absent.
        for f in FLOW_FIELDS:
            detail.setdefault(f, None)

    lims = list(limitations)
    if source == 'fixture':
        lims.append('A synthetic fixture committed to this repository. The numbers are invented '
                    'for exercising the contracts; nothing derived from them is biological '
                    'evidence.')
    unreadable = sorted({f['file_type'] for f in files if f['file_type'] in DECLARABLE_FILE_TYPES})
    if unreadable:
        lims.append(f'File type(s) {", ".join(unreadable)} can be described but not read in this '
                    f'version. The manifest is valid; an analysis over them is refused rather than '
                    f'approximated.')

    m = {
        'schema_version': K.PRODUCTION_VERSION,
        'dataset_id': dataset_id, 'created_at': K.now_iso(), 'title': title,
        'description': description,
        'source': source, 'accession': accession, 'source_url': source_url,
        'retrieved_at': retrieved_at,
        'visibility': visibility, 'evidence_class': evidence_class,
        'modality': modality, 'modality_detail': detail or None,
        'organism': organism, 'cell_type': cell_type, 'perturbation': perturbation,
        'conditions': list(conditions),
        'files': list(files),
        'sample_metadata': sample_metadata,
        'experimental_design': design,
        'derived_from': derived_from,
        'missing_metadata': missing_design(design),
        'citable': False,
        'limitations': lims,
        'registered_by': registered_by,
        'notes': notes,
    }
    K.require_valid('dataset_manifest', m)
    return m


def readable_files(m, role=None):
    return [f for f in m['files']
            if f['file_type'] in READABLE_FILE_TYPES and (role is None or f['role'] == role)]


def lineage(m, resolve):
    """Walk `derived_from` to the roots.

    `resolve(dataset_id)` returns a manifest or None. Returns
    {chain, root_ids, source_evidence_class, source_visibility}. Visibility takes
    the most restrictive value in the chain: one private ancestor makes everything
    downstream private to handle, which is the only safe direction for the rule to
    fail in.
    """
    chain, seen = [], set()
    cur, roots_ = m, []
    while True:
        if cur['dataset_id'] in seen:
            raise K.ContractError(f'derived_from cycle at {cur["dataset_id"]!r}')
        seen.add(cur['dataset_id'])
        chain.append(cur['dataset_id'])
        parent_id = (cur.get('derived_from') or {}).get('parent_dataset_id')
        if not parent_id:
            roots_.append(cur)
            break
        parent = resolve(parent_id)
        if parent is None:
            raise K.ContractError(
                f'{cur["dataset_id"]!r} is derived from {parent_id!r}, which is not registered. '
                f'A derived dataset whose parent cannot be resolved has no provenance.')
        cur = parent
    classes = {r['evidence_class'] for r in roots_}
    vis = {r['visibility'] for r in roots_}
    return {
        'chain': chain,
        'root_ids': [r['dataset_id'] for r in roots_],
        'source_evidence_class': classes.pop() if len(classes) == 1 else 'mixed',
        'source_visibility': 'private' if 'private' in vis else (
            vis.pop() if len(vis) == 1 else 'mixed'),
    }


def combined_source(manifests):
    """The source class and visibility of a set of inputs, used by an AnalysisResult.

    Any private input makes the whole result private to handle. Mixed classes are
    reported as mixed rather than averaged away.
    """
    classes = {m['evidence_class'] for m in manifests}
    vis = {m['visibility'] for m in manifests}
    return ('mixed' if len(classes) > 1 else classes.pop(),
            'private' if 'private' in vis else ('mixed' if len(vis) > 1 else vis.pop()))
