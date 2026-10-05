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
              'cytometry_summary', 'generic_table')


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
        raise K.ContractError(
            f'{p.name}: this version ingests {", ".join(sorted(TB.DELIMS))} tables. '
            f'h5ad, FCS and peak files are describable in a manifest but have no reader yet, '
            f'so ingesting one would register data nothing can open.')

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
