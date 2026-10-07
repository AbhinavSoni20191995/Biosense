"""Registering a local file as a dataset BioSense may analyse.

The shape of the step: read the file, hash it, describe what is actually in it,
and then ask for whatever the description does not supply. The last part is the
point. `condition_column` is not inferred from a column called "condition",
because a confident comparison of the wrong column is worse than a refusal, and
the person ingesting the file knows the answer.

What ingest will do for you:
  * validate that the file parses and that its columns are unique;
  * record every column, every non-numeric column's observed values, and which
    columns are fully numeric;
  * hash the bytes;
  * infer the modality ONLY when the signal is unambiguous (a population table
    with a viability column and marker-percentage columns is cytometry_summary);
  * write the manifest under the private root.

What it will not do: choose your control group, name your replicate column, or
invent a panel. Those come back as `missing_metadata`.
"""
from __future__ import annotations

from pathlib import Path

from .. import contracts as K
from . import manifest as MF
from . import registry as REG
from . import roots
from . import tables as TB

# A column whose name matches one of these, in a table that also looks like
# per-sample measurements, is evidence for a modality. Matching is on the
# lowercased name and is only ever used to SUGGEST a modality, never to pick a
# condition column or a control.
CYTO_HINTS = ('viability', 'cd14', 'cd16', 'cd206', 'cd3', 'cd4', 'cd8', 'cd11b',
              'live_pct', 'live%', 'pct', 'percent', 'freq', 'frequency', 'mfi')

MODALITIES = ('bulk_rna', 'single_cell_rna', 'chip_seq', 'atac_seq', 'flow_cytometry',
              'cytometry_summary', 'proteomics', 'secretome', 'generic_table')


def infer_modality(t):
    """A suggestion with a reason, or (None, why not). Never silently decisive."""
    low = [c.lower() for c in t.columns]
    hits = [c for c in low if any(h in c for h in CYTO_HINTS)]
    if len(hits) >= 2 and len(t.numeric) >= 2:
        return 'cytometry_summary', (
            f'{len(hits)} columns read as cytometry population or intensity measures '
            f'({", ".join(hits[:5])}) across {t.n} rows')
    return None, ('no unambiguous signal: declare the modality explicitly. Guessing it would '
                  'decide which tools may run over this file.')


def ingest_h5ad(path, *, dataset_id, title, organism='Homo sapiens', cell_type=None,
                perturbation=None, experimental_design=None, description=None,
                registered_by=None, visibility='private', notes=None, register=True,
                overwrite=False):
    """Register a single-cell .h5ad.

    The reader describes what is in the file; it never decides which obs column
    holds the condition. `design_candidates` lists what a person could choose
    from, and a plan that needs a field the design does not name is refused by
    name rather than running against a guess.
    """
    from .readers import h5ad as H5
    H5.require()
    p = Path(path)
    desc = H5.describe(p)
    design = dict(experimental_design or {})
    for key in ('condition_column', 'sample_id_column', 'donor_column', 'batch_column',
                'replicate_column', 'timepoint_column'):
        col = design.get(key)
        if col and col not in desc['obs_keys']:
            raise K.ContractError(
                f'experimental_design.{key} names obs column {col!r}, which is not in '
                f'{p.name}. Present: {", ".join(desc["obs_keys"])}')
    cond = design.get('condition_column')
    if cond:
        levels = desc['obs_levels'].get(cond, [])
        if design.get('control') and levels and design['control'] not in levels:
            raise K.ContractError(
                f'control {design["control"]!r} does not appear in obs[{cond!r}]. '
                f'Values present: {", ".join(map(str, levels))}')
        if levels and not design.get('treatments'):
            design['treatments'] = [v for v in levels if v != design.get('control')]

    files = [MF.file_entry(p, 'matrix', 'h5ad')]
    files[0]['rows'] = desc['n_cells']
    files[0]['columns'] = desc['obs_keys']
    lims = ['Ingested from a local .h5ad. BioSense did not generate this matrix and cannot '
            'verify how it was produced, filtered or normalised.',
            f'Normalisation: {desc["normalization_status"]}. A tool that needs counts checks '
            f'this and refuses rather than assuming.']
    if visibility == 'private':
        lims.append('Private user data: never served over HTTP, never committed, and never a '
                    'literature citation.')

    m = MF.build(dataset_id=dataset_id, title=title,
                 source='user_upload' if visibility == 'private' else 'fixture',
                 modality='single_cell_rna', organism=organism, files=files,
                 visibility=visibility, cell_type=cell_type, perturbation=perturbation,
                 conditions=desc['obs_levels'].get(cond, []) if cond else [],
                 sample_metadata={'sample_count': desc['n_cells'],
                                  'columns': desc['obs_keys'],
                                  'levels': desc['obs_levels']},
                 experimental_design=design,
                 modality_detail={'n_cells': desc['n_cells'], 'n_genes': desc['n_genes'],
                                  'obs_keys': desc['obs_keys'], 'var_keys': desc['var_keys'],
                                  'layers': desc['layers'], 'obsm': desc['obsm'],
                                  'normalization_status': desc['normalization_status'],
                                  'design_candidates': desc['design_candidates']},
                 description=description, registered_by=registered_by, notes=notes,
                 limitations=lims)
    path_out = None
    if register:
        if visibility == 'private':
            roots.ensure_roots()
        path_out = REG.register(m, overwrite=overwrite)
    return m, path_out


def ingest_peaks(path, *, dataset_id, title, modality='atac_seq', organism='Homo sapiens',
                 cell_type=None, perturbation=None, experimental_design=None,
                 peak_files=None, gene_anchors=None, description=None, registered_by=None,
                 visibility='private', notes=None, register=True, overwrite=False,
                 extra_files=()):
    """Register processed peak files (BED / narrowPeak / broadPeak).

    `peak_files` maps a condition to its file. It is required for a comparison
    and is never inferred from a filename: guessing would silently compare the
    wrong pair, and the mistake would look exactly like a result.
    """
    from .readers import peaks as PK
    paths = [Path(path)] + [Path(x) for x in extra_files]
    files, detail = [], {}
    for pth in paths:
        ft = pth.suffix.lstrip('.')
        d = PK.describe(pth, ft)
        e = MF.file_entry(pth, 'peaks', ft)
        e['rows'] = d['n_peaks']
        files.append(e)
        detail[e['path']] = {k: d[k] for k in ('n_peaks', 'chromosomes', 'total_bp',
                                               'median_width_bp', 'has_qvalues')}
    stored = {MF.stored_path(k): v for k, v in (peak_files or {}).items()}
    m = MF.build(dataset_id=dataset_id, title=title,
                 source='user_upload' if visibility == 'private' else 'fixture',
                 modality=modality, organism=organism, files=files, visibility=visibility,
                 cell_type=cell_type, perturbation=perturbation,
                 conditions=sorted(peak_files or {}),
                 experimental_design=dict(experimental_design or {}),
                 modality_detail={'peak_summaries': detail,
                                  'peak_files': {k: MF.stored_path(v)
                                                 for k, v in (peak_files or {}).items()},
                                  'gene_anchors': (MF.stored_path(gene_anchors)
                                                   if gene_anchors else None)},
                 description=description, registered_by=registered_by, notes=notes,
                 limitations=['BioSense did not call these peaks and cannot verify the calling '
                              'parameters. A difference between peak sets may reflect calling '
                              'thresholds rather than chromatin.'])
    path_out = None
    if register:
        if visibility == 'private':
            roots.ensure_roots()
        path_out = REG.register(m, overwrite=overwrite)
    return m, path_out


def ingest_local(path, *, dataset_id, title, modality=None, organism='Homo sapiens',
                 cell_type=None, perturbation=None, experimental_design=None,
                 modality_detail=None, description=None, registered_by=None,
                 visibility='private', role=None, notes=None, register=True,
                 dirs=None, overwrite=False):
    """Ingest one local table as a dataset. Returns (manifest, path_or_None)."""
    p = Path(path)
    if not p.is_file():
        raise K.ContractError(f'{p} is not a file')
    ft = p.suffix.lstrip('.').lower()
    if ft == 'txt':
        ft = 'tsv'
    if ft not in TB.DELIMS:
        route = {'h5ad': 'ingest_h5ad', 'bed': 'ingest_peaks', 'narrowpeak': 'ingest_peaks',
                 'broadpeak': 'ingest_peaks'}.get(ft)
        if route:
            raise K.ContractError(
                f'{p.name}: use {route}() for {ft} files. They are read by a different reader '
                f'and carry different metadata.')
        raise K.ContractError(
            f'{p.name}: this version ingests {", ".join(sorted(TB.DELIMS))} tables, .h5ad '
            f'(via ingest_h5ad) and BED/narrowPeak (via ingest_peaks). Raw FCS has no reader: '
            f'fcsparser cannot resolve against this project\'s numpy, so processed, gated '
            f'tables are the supported cytometry input.')

    t = TB.read_table(p, ft)
    if modality is None:
        modality, why = infer_modality(t)
        if modality is None:
            raise K.ContractError(f'{p.name}: {why} (one of {", ".join(MODALITIES)})')
    if modality not in MODALITIES:
        raise K.ContractError(f'unknown modality {modality!r}')

    sample_meta = TB.describe(t)
    design = dict(experimental_design or {})
    # Validate what WAS supplied against what is in the file. A design that names
    # a column the table does not have is an error now rather than a confusing
    # failure at analysis time.
    for key in ('condition_column', 'replicate_column', 'batch_column', 'sample_id_column',
                'timepoint_column', 'donor_column'):
        col = design.get(key)
        if col and col not in t.columns:
            raise K.ContractError(
                f'experimental_design.{key} names column {col!r}, which is not in {p.name}. '
                f'Columns present: {", ".join(t.columns)}')
    cond = design.get('condition_column')
    if cond:
        levels = t.levels(cond)
        if design.get('control') and design['control'] not in levels:
            raise K.ContractError(
                f'control {design["control"]!r} does not appear in column {cond!r}. '
                f'Values present: {", ".join(map(str, levels))}')
        for tr in design.get('treatments') or []:
            if tr not in levels:
                raise K.ContractError(
                    f'treatment {tr!r} does not appear in column {cond!r}. '
                    f'Values present: {", ".join(map(str, levels))}')
        if not design.get('treatments'):
            # Not a guess about biology: every non-control level in the column the
            # person named. Recorded so it is visible and correctable.
            design['treatments'] = [v for v in levels if v != design.get('control')]

    files = [MF.file_entry(p, role or ('population_table' if modality in
                                       ('cytometry_summary', 'flow_cytometry') else 'matrix'),
                           ft, columns=t.columns, rows=t.n)]
    lims = ['Ingested from a local table. BioSense did not generate these numbers and cannot '
            'verify how they were produced.']
    if visibility == 'private':
        lims.append('Private user data: it is never served over HTTP, never committed, and can '
                    'support or contradict a hypothesis but can never become a literature citation.')

    m = MF.build(
        dataset_id=dataset_id, title=title,
        source='user_upload' if visibility == 'private' else 'derived',
        modality=modality, organism=organism, files=files, visibility=visibility,
        cell_type=cell_type, perturbation=perturbation,
        conditions=t.levels(cond) if cond else [],
        sample_metadata=sample_meta, experimental_design=design,
        modality_detail=modality_detail, description=description,
        registered_by=registered_by, notes=notes, limitations=lims)

    path_out = None
    if register:
        if visibility == 'private':
            roots.ensure_roots()
        path_out = REG.register(m, root=(dirs or {}).get(visibility) if dirs else None,
                                overwrite=overwrite)
    return m, path_out


def ingest_public_table(path, *, dataset_id, accession, source, title, source_url=None,
                        organism='Homo sapiens', cell_type=None, perturbation=None,
                        modality='bulk_rna', experimental_design=None, description=None,
                        registered_by=None, notes=None, register=True, overwrite=False,
                        extra_limitations=()):
    """Register a table BioSense built from a public repository record.

    Public, with its accession and where it came from: evidence about another
    lab's experiment, citable by accession, never mistaken for a private upload
    or a fixture.
    """
    p = Path(path)
    t = TB.read_table(p, 'tsv' if p.suffix.lower() in ('.tsv', '.txt') else 'csv')
    design = dict(experimental_design or {})
    for key in ('condition_column', 'sample_id_column'):
        if design.get(key) and design[key] not in t.columns:
            raise K.ContractError(f'experimental_design.{key} names {design[key]!r}, which is '
                                  f'not in {p.name}')
    cond = design.get('condition_column')
    m = MF.build(
        dataset_id=dataset_id, title=title, source=source, modality=modality,
        organism=organism, visibility='public', accession=accession, source_url=source_url,
        retrieved_at=K.now_iso(),
        files=[MF.file_entry(p, 'matrix', p.suffix.lstrip('.').lower(), columns=t.columns,
                             rows=t.n)],
        cell_type=cell_type, perturbation=perturbation,
        conditions=t.levels(cond) if cond else [], sample_metadata=TB.describe(t),
        experimental_design=design, description=description, registered_by=registered_by,
        notes=notes,
        limitations=['Built by BioSense from a public repository record: the numbers are the '
                     'repository\'s processed values after the transform stated in the '
                     'description. Another lab\'s experiment — evidence to weigh, not a '
                     'measurement of this process.'] + [x for x in extra_limitations if x])
    path_out = REG.register(m, overwrite=overwrite) if register else None
    return m, path_out
