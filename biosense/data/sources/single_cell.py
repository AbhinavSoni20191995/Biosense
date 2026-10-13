"""A single-cell series as a per-sample pseudobulk, with its time axis kept.

The run's specialists all hit the same wall: the levers are timing levers, and
the one dataset that could date a receptor window — a sci-RNA-seq time course
of the endothelial-to-haematopoietic transition — was left unregistered,
because nothing could bring a single-cell series in. The single-cell tools
read an `.h5ad`; a GEO series usually holds a sparse count matrix (Matrix
Market), its gene and cell lists, and a cell annotation table instead.

This turns those into what a timing question needs: for each sample, the
summed counts of its cells — optionally of one annotated population only —
on a log2(CPM + 1) scale, with each sample's condition and timepoint. The
sample, not the cell, is the unit: cells from one sample are not independent
replicates, and a pseudobulk is the established way to respect that.

What it never does: cluster, annotate or call a cell type. A population is
chosen by the depositors' own annotation column, named explicitly; cells are
matched to their annotation by barcode, or by matrix order only when told the
two are in the same order. A matrix of normalised values is refused: a
pseudobulk sums counts.
"""
from __future__ import annotations

import csv
import gzip
import io
import itertools
from pathlib import Path

import numpy as np

from ... import contracts as K
from .. import roots
from . import geo_fetch as GF
from . import http as HTTP

MAX_DOWNLOAD = 2 * 1024 ** 3
CHUNK_LINES = 1_000_000
MIN_CELLS = 20
FEATURE_NAMES = ('gene_short_name', 'gene_name', 'gene_symbol', 'symbol', 'name', 'gene')


def _open(path):
    p = Path(path)
    with open(p, 'rb') as fh:
        gz = fh.read(2) == b'\x1f\x8b'
    return gzip.open(p, 'rt', encoding='utf-8', newline='') if gz else open(
        p, encoding='utf-8', newline='')


def _delimiter(path, first_line):
    name = str(path).lower().replace('.gz', '')
    if name.endswith('.csv'):
        return ','
    if name.endswith(('.tsv', '.tab', '.txt')):
        return '\t'
    return '\t' if first_line.count('\t') >= first_line.count(',') else ','


def read_features(path, name_column=None):
    """Feature names in matrix order.

    A 10x-style file (no header; id, name, type) gives the name column; a
    table with a header gives *name_column* or the first of the usual names.
    """
    with _open(path) as fh:
        lines = [ln.rstrip('\r\n') for ln in fh if ln.strip()]
    if not lines:
        raise K.ContractError(f'{Path(path).name} lists no features')
    sep = _delimiter(path, lines[0])
    rows = list(csv.reader(lines, delimiter=sep))
    head = [c.strip().lower() for c in rows[0]]
    has_header = bool(name_column) or any(h in FEATURE_NAMES or h in ('gene_id', 'id')
                                          for h in head)
    if has_header:
        want = (name_column or '').strip().lower()
        col = (head.index(want) if want in head else
               next((head.index(n) for n in FEATURE_NAMES if n in head), None))
        if col is None:
            raise K.ContractError(f'{Path(path).name}: no column named '
                                  f'{name_column or " / ".join(FEATURE_NAMES)}; its header is '
                                  f'{", ".join(rows[0])}')
        return [r[col].strip() if col < len(r) else '' for r in rows[1:]]
    return [(r[1] if len(r) > 1 and r[1].strip() else r[0]).strip() for r in rows]


def read_barcodes(path):
    with _open(path) as fh:
        return [ln.split('\t')[0].split(',')[0].strip() for ln in fh if ln.strip()]


def read_cells(path, *, id_column, columns, in_matrix_order=False):
    """{cell id or row index: {column: value}} for the columns named. Streamed."""
    with _open(path) as fh:
        first = fh.readline()
        sep = _delimiter(path, first)
        header = [c.strip() for c in next(csv.reader([first], delimiter=sep))]
        need = [c for c in columns if c]
        if not in_matrix_order:
            need = [id_column] + need
        missing = [c for c in need if c not in header]
        if missing:
            raise K.ContractError(f'{Path(path).name} has no column '
                                  f'{", ".join(map(repr, missing))}; its columns are '
                                  f'{", ".join(header[:30])}')
        idx = {c: header.index(c) for c in need}
        out = {}
        for n, row in enumerate(csv.reader(fh, delimiter=sep)):
            if not row:
                continue
            key = n if in_matrix_order else row[idx[id_column]].strip()
            out[key] = {c: (row[i].strip() if i < len(row) else '') for c, i in idx.items()
                        if c != id_column}
    return out


def _mtx_header(fh):
    line = fh.readline()
    if not line.lower().startswith('%%matrixmarket matrix coordinate'):
        raise K.ContractError('the matrix is not a Matrix Market coordinate file')
    field = line.split()[3].lower() if len(line.split()) > 3 else 'real'
    if field == 'pattern':
        raise K.ContractError('the matrix holds a pattern (no values); a pseudobulk needs counts')
    for line in fh:
        if not line.startswith('%'):
            rows, cols, nnz = (int(x) for x in line.split()[:3])
            return rows, cols, nnz, field
    raise K.ContractError('the matrix has no size line')


def pseudobulk(matrix_path, features, cell_groups, *, n_cells_expected):
    """(sums genes x groups, cells per group) from a coordinate matrix. Streamed.

    *cell_groups* is an int array: for each cell (matrix order), its group's
    index, or -1 for a cell that is not used.
    """
    n_groups = int(cell_groups.max()) + 1 if len(cell_groups) and cell_groups.max() >= 0 else 0
    if n_groups == 0:
        raise K.ContractError('no cell belongs to a chosen sample')
    with _open(matrix_path) as fh:
        rows, cols, nnz, field = _mtx_header(fh)
        if rows == len(features) and cols == n_cells_expected:
            gene_axis = 0
        elif cols == len(features) and rows == n_cells_expected:
            gene_axis = 1
        else:
            raise K.ContractError(
                f'the matrix is {rows} x {cols}, which matches neither {len(features)} features '
                f'x {n_cells_expected} cells nor the reverse: the gene or cell list does not '
                f'belong to this matrix')
        sums = np.zeros((len(features), n_groups), dtype=np.float64)
        while True:
            block = list(itertools.islice(fh, CHUNK_LINES))
            if not block:
                break
            arr = np.loadtxt(io.StringIO(''.join(block)), ndmin=2)
            if arr.shape[1] < 3:
                raise K.ContractError('a matrix entry has no value')
            i, j, v = arr[:, 0].astype(np.int64) - 1, arr[:, 1].astype(np.int64) - 1, arr[:, 2]
            if not np.allclose(v, np.round(v)):
                raise K.ContractError(
                    'the matrix holds non-integer values: it is normalised, and a pseudobulk '
                    'sums counts. Register the depositors\' counts, or an .h5ad with them')
            g, c = (i, j) if gene_axis == 0 else (j, i)
            grp = cell_groups[c]
            keep = grp >= 0
            np.add.at(sums, (g[keep], grp[keep]), v[keep])
    cells = np.bincount(cell_groups[cell_groups >= 0], minlength=n_groups)
    return sums, cells


def build(*, matrix, features, cells, barcodes=None, cells_in_matrix_order=False,
          cell_id_column=None, sample_column, condition_column, control, treatments,
          time_column=None, cell_type_column=None, cell_types=(), feature_name_column=None,
          genes=None, max_genes=GF.DEFAULT_MAX_GENES, min_cells=None):
    """The pseudobulk long table and what it rests on, from local files. Pure of the network."""
    ctl = GF.controls_of(control)
    min_cells = MIN_CELLS if min_cells is None else int(min_cells)
    if min_cells < 1:
        raise K.ContractError('--min-cells is at least 1')
    feats = read_features(features, feature_name_column)
    if barcodes:
        order = read_barcodes(barcodes)
    elif not cells_in_matrix_order:
        raise K.ContractError('give the barcodes file that names the matrix\'s cells, or say '
                              'the cell table is in matrix order (--cells-in-matrix-order)')
    else:
        order = None
    if not cells_in_matrix_order and not cell_id_column:
        raise K.ContractError('name the cell table\'s cell id column (--cell-id-column)')
    meta = read_cells(cells, id_column=cell_id_column,
                      columns=[sample_column, condition_column, time_column, cell_type_column],
                      in_matrix_order=cells_in_matrix_order)
    n_cells = len(order) if order is not None else len(meta)
    wanted_types = {t.lower() for t in cell_types or ()}
    if wanted_types and not cell_type_column:
        raise K.ContractError('--cell-type needs --cell-type-column: a population is chosen by '
                              'the depositors\' own annotation, never inferred')
    samples, index, cond_of, time_of = [], {}, {}, {}
    groups = np.full(n_cells, -1, dtype=np.int64)
    unmatched, conflicts = 0, set()
    for k in range(n_cells):
        rec = meta.get(order[k] if order is not None else k)
        if rec is None:
            unmatched += 1
            continue
        if wanted_types and rec.get(cell_type_column, '').lower() not in wanted_types:
            continue
        sid, cond = rec.get(sample_column), rec.get(condition_column)
        if not sid or not cond:
            continue
        tp = (GF.parse_time(rec.get(time_column)) or rec.get(time_column) or None) \
            if time_column else None
        if sid in cond_of and (cond_of[sid] != cond or time_of[sid] != tp):
            conflicts.add(sid)
        if sid not in index:
            cond_of[sid], time_of[sid] = cond, tp
            index[sid] = len(samples)
            samples.append(sid)
        groups[k] = index[sid]
    if conflicts:
        raise K.ContractError(
            f'{len(conflicts)} sample(s) have cells under more than one condition or timepoint '
            f'({", ".join(sorted(conflicts)[:5])}): a sample is the unit of a pseudobulk, so '
            f'use a sample column that is one condition and one time per sample')
    if order is not None and unmatched == n_cells:
        raise K.ContractError('no barcode in the matrix is in the cell table: the cell id '
                              'column does not hold the barcodes')
    sums, n_per = pseudobulk(matrix, feats, groups, n_cells_expected=n_cells)
    thin = [samples[i] for i, n in enumerate(n_per) if n < min_cells]
    keep = [i for i, n in enumerate(n_per) if n >= min_cells]
    if not keep:
        raise K.ContractError(f'no sample has {min_cells} or more cells'
                              + (' of the chosen population' if wanted_types else ''))
    sample_rows = [{'gsm': samples[i], 'title': samples[i], 'source': '',
                    'characteristics': {'condition': cond_of[samples[i]],
                                        **({'timepoint': time_of[samples[i]]}
                                           if time_column else {})}} for i in keep]
    matrix_rows = [[float(sums[g, i]) for i in keep] for g in range(len(feats))]
    rows, stats = GF.build_long_table(
        sample_rows, feats, [samples[i] for i in keep], matrix_rows, {},
        condition_key='condition', control=ctl, treatments=treatments, genes=genes,
        max_genes=max_genes, transform='counts',
        time_key='timepoint' if time_column else None)
    for r in rows:
        r['n_cells'] = str(int(n_per[index[r['sample_id']]]))
    stats.update(cells_per_sample={samples[i]: int(n_per[i]) for i in keep},
                 samples_dropped_for_few_cells=thin, cells_unmatched=unmatched,
                 cells_in_matrix=n_cells, min_cells=min_cells,
                 population=sorted(cell_types) if cell_types else None)
    return rows, stats


def _fieldnames(rows):
    return list(rows[0]) if rows else ['gene', 'sample_id', 'condition', 'logcpm']


def write_table(rows, path):
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=_fieldnames(rows), delimiter='\t')
        w.writeheader()
        w.writerows(rows)
    return path


def limitations(stats, *, condition_column, control, time_column, cell_types):
    ctl = GF.controls_of(control)
    out = ['A pseudobulk built by BioSense: each sample\'s cells summed and put on '
           'log2(CPM + 1). It answers per-sample questions and hides every difference between '
           'cells within a sample.']
    if cell_types:
        out.append(f'Only the cells the depositors annotated as {", ".join(cell_types)} are '
                   f'summed; BioSense did not annotate or check that annotation.')
    if stats.get('samples_dropped_for_few_cells'):
        out.append(f'{len(stats["samples_dropped_for_few_cells"])} sample(s) were dropped for '
                   f'having fewer than {stats.get("min_cells", MIN_CELLS)} cells: '
                   + ', '.join(stats['samples_dropped_for_few_cells'][:8]))
    if stats.get('cells_unmatched'):
        out.append(f'{stats["cells_unmatched"]} of {stats["cells_in_matrix"]} cells in the '
                   f'matrix have no row in the cell table and were not used.')
    if ctl[1:]:
        out.append(f'The control pools {", ".join(ctl)} of {condition_column!r}.')
    if time_column and stats.get('thin_timepoints'):
        out.append('Fewer than two samples per group at ' + '; '.join(stats['thin_timepoints'])
                   + ': a comparison within that timepoint has no replicate spread.')
    return out


def register_local(*, dataset_id, title, out_dir=None, organism='Homo sapiens',
                   cell_type=None, registered_by=None, overwrite=False, **kw):
    """A pseudobulk from files a person holds, registered as a private dataset."""
    from .. import ingest as ING
    rows, stats = build(**kw)
    base = Path(out_dir or roots.private_root()) / dataset_id
    base.mkdir(parents=True, exist_ok=True)
    table = write_table(rows, base / f'{dataset_id}.pseudobulk.tsv')
    design = {'condition_column': 'condition', 'control': GF.controls_of(kw['control'])[0],
              'treatments': list(kw['treatments']), 'sample_id_column': 'sample_id',
              'replicate_column': 'sample_id'}
    if kw.get('time_column'):
        design['timepoint_column'] = 'timepoint'
    m, path = ING.ingest_local(
        table, dataset_id=dataset_id, title=title, modality='generic_table', organism=organism,
        cell_type=cell_type, experimental_design=design, registered_by=registered_by,
        description='Pseudobulk of a single-cell count matrix: '
                    + '; '.join(limitations(stats, condition_column=kw['condition_column'],
                                            control=kw['control'],
                                            time_column=kw.get('time_column'),
                                            cell_types=kw.get('cell_types'))),
        overwrite=overwrite)
    return {'dataset_id': dataset_id, 'table': str(table), 'manifest': str(path), **stats}


def fetch(acc, *, matrix, features, cells, barcodes=None, dataset_id=None, title=None,
          cell_type=None, organism='Homo sapiens', registered_by='bioinformatics_agent', i_have_network_permission=False,
          timeout=180, out_root=None, register=True, overwrite=True, max_bytes=MAX_DOWNLOAD,
          **kw):
    """A GEO single-cell series' files, downloaded, made into a pseudobulk and registered."""
    from .. import ingest as ING
    GF._need(i_have_network_permission)
    acc = GF.check_accession(acc)
    try:
        listing = GF._text(GF._get(f'{GF.series_url(acc)}/suppl/', timeout))
    except Exception:  # noqa: BLE001 - an unreadable listing is said below
        listing = ''
    held = GF.listing_names(listing)
    names = {'matrix': matrix, 'features': features, 'cells': cells, 'barcodes': barcodes}
    absent = [n for n in names.values() if n and n not in held]
    if absent:
        raise K.ContractError(f'{", ".join(absent)} not in {acc}\'s series directory (it holds '
                              f'{", ".join(held[:12]) or "nothing readable"})')
    did = dataset_id or f'{acc.lower()}_pseudobulk'
    base = Path(out_root or roots.cache_root()) / 'geo' / did
    (base / 'raw').mkdir(parents=True, exist_ok=True)
    local = {}
    for role, name in names.items():
        if name:
            local[role] = download(f'{GF.series_url(acc)}/suppl/{name}', base / 'raw' / name,
                                   timeout=timeout, max_bytes=max_bytes)
    rows, stats = build(matrix=local['matrix'], features=local['features'],
                        cells=local['cells'], barcodes=local.get('barcodes'), **kw)
    table = write_table(rows, base / f'{did}.tsv')
    ctl = GF.controls_of(kw['control'])
    notes = limitations(stats, condition_column=kw['condition_column'], control=kw['control'],
                        time_column=kw.get('time_column'), cell_types=kw.get('cell_types'))
    m = None
    if register:
        m, _ = ING.ingest_public_table(
            table, dataset_id=did, accession=acc, source='geo',
            title=title or f'{acc}: pseudobulk ({kw["condition_column"]}: '
                           f'{", ".join(kw["treatments"])} vs {ctl[0]})'
                           + (f' over {", ".join(stats["timepoints"])}'
                              if kw.get('time_column') else ''),
            source_url=f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={acc}',
            organism=organism, cell_type=cell_type,
            modality='generic_table',
            experimental_design={'condition_column': 'condition', 'control': ctl[0],
                                 'treatments': list(kw['treatments']),
                                 'sample_id_column': 'sample_id',
                                 'replicate_column': 'sample_id',
                                 'timepoint_column': 'timepoint' if kw.get('time_column')
                                 else None},
            description=(f'Pseudobulk of {acc}\'s single-cell counts ({matrix}): '
                         f'{len(stats["cells_per_sample"])} samples, '
                         f'{sum(stats["cells_per_sample"].values())} cells'
                         + (f' annotated {", ".join(kw["cell_types"])}'
                            if kw.get('cell_types') else '') + '.'),
            registered_by=registered_by,
            notes=f'route: single-cell pseudobulk; files: {names}',
            extra_limitations=notes, overwrite=overwrite)
    return {'dataset_id': did, 'accession': acc, 'table': str(table), **stats,
            'limitations_added': notes, 'registered': bool(m), 'retrieved_at': K.now_iso(),
            'next': (f'analyse plan --dataset-ids {did} --analysis-type '
                     f'bulk_expression_comparison --tool bulk.expression_comparison '
                     f'--group-column condition --control "{ctl[0]}" --treatment-level '
                     f'"{kw["treatments"][0]}"'
                     + (' --timepoint "<one of: ' + ', '.join(stats['timepoints']) + '>"'
                        if kw.get('time_column') else '') + ' ...'),
            'note': ('Another lab\'s cells, summed per sample: evidence about that experiment, '
                     'to be weighed for this process, not a measurement of it.')}


def download(url, dest, *, timeout=180, max_bytes=MAX_DOWNLOAD):
    """The one place this module touches the network. Replaced in tests."""
    return HTTP.download(url, dest, timeout=timeout, max_bytes=max_bytes)
