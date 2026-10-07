"""A real public RNA-seq series from GEO, made into a dataset the tools can analyse.

The bioinformatics agent could search GEO and could analyse a table, and had
nothing in between: every run ended "only synthetic fixtures are reachable".
This is the in-between, in two steps a person or an agent can follow:

    samples(acc)  the series' samples and what GEO says about each (title,
                  source, characteristics) — so the condition to compare is
                  chosen from what the depositors wrote, never guessed;
    fetch(acc, condition_key=..., control=..., treatments=...)
                  NCBI's own uniformly processed raw counts for the series,
                  library-size normalised to log2(CPM + 1), restricted to the
                  genes asked about (or the most expressed), written as a long
                  table and registered as a PUBLIC dataset with its accession,
                  URLs, retrieval time and the transform applied.

Two routes, in this order:

* NCBI's uniformly processed raw counts (human and mouse RNA-seq): one
  reference, one GeneID x GSM matrix per series — preferred where it exists;
* the depositors' own processed table from the series' supplementary files,
  for ANY species and for human or mouse series NCBI has not processed. Its
  columns are used only where they match a sample (GSM, title, or a column
  map the agent supplies), and what the values are (counts, linear, log) is
  read from the numbers and stated.

Every result carries an evidence weight — a confidence ceiling from the
species and the route: human via NCBI can support high confidence; another
mammal, or a depositor's table, moderate or low; a non-mammal low — with the
reason, so a zebrafish or pig series still informs a hypothesis, labelled for
what it is.

Network only with an explicit permission flag, like every live path here.
Nothing in this module decides which comparison is interesting; it only
refuses a comparison the samples cannot support.
"""
from __future__ import annotations

import csv
import gzip
import io
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ... import contracts as K
from .. import roots

ACC_RE = re.compile(r'^GSE\d{3,8}$')
FTP = 'https://ftp.ncbi.nlm.nih.gov/geo/series'
DOWNLOAD = 'https://www.ncbi.nlm.nih.gov/geo/download/'
GENOMES = {  # organism -> (counts file tag, annotation file)
    'Homo sapiens': ('GRCh38.p13', 'Human.GRCh38.p13.annot.tsv.gz'),
    'Mus musculus': ('GRCm39', 'Mouse.GRCm39.annot.tsv.gz'),
}
MAX_DOWNLOAD = 150 * 1024 * 1024
MAX_ROWS = 200_000          # the table reader's own limit
DEFAULT_MAX_GENES = 2000
MIN_PER_GROUP = 2


def _need(i_have_network_permission):
    if not i_have_network_permission:
        raise K.ContractError(
            'fetching from GEO touches the network: pass --i-have-network-permission, which a '
            'run may only do when its request permits public-database access')


def check_accession(acc):
    acc = (acc or '').strip().upper()
    if not ACC_RE.match(acc):
        raise K.ContractError(f'{acc!r} is not a GEO series accession (GSE followed by digits)')
    return acc


def series_url(acc):
    stem = acc[:-3] + 'nnn' if len(acc) > 6 else 'GSEnnn'
    return f'{FTP}/{stem}/{acc}'


def _get(url, timeout=60, max_bytes=MAX_DOWNLOAD):
    """Bytes at *url*. Replaced in tests; the one place this module touches the network."""
    req = urllib.request.Request(url, headers={'User-Agent': 'BioSenseBioinformatics/0.1'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise K.ContractError(f'{url} is larger than {max_bytes // 1024 // 1024} MB; not fetched')
    return data


def _text(data):
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    return data.decode('utf-8', errors='replace')


def _cells(line):
    return [c.strip().strip('"') for c in line.rstrip('\n').split('\t')]


# ── step 1: what the samples are ────────────────────────────────────────
def parse_series_matrix(text):
    """Sample rows from a series matrix's header block. Pure; tested on recorded shapes."""
    fields, chars, meta = {}, [], {}
    for line in text.splitlines():
        if line.startswith('!series_matrix_table_begin'):
            break
        if not line.startswith('!'):
            continue
        cells = _cells(line)
        key, vals = cells[0], cells[1:]
        if key == '!Sample_characteristics_ch1':
            chars.append(vals)
        elif key in ('!Sample_geo_accession', '!Sample_title', '!Sample_source_name_ch1',
                     '!Sample_organism_ch1', '!Sample_platform_id', '!Sample_library_strategy'):
            fields[key] = vals
        elif key in ('!Series_title', '!Series_summary', '!Series_overall_design'):
            meta[key[8:].lower()] = ' '.join(vals)[:1500]
    gsms = fields.get('!Sample_geo_accession') or []
    if not gsms:
        raise K.ContractError('the series matrix lists no samples')
    rows = []
    for i, gsm in enumerate(gsms):
        def at(k):
            v = fields.get(k) or []
            return v[i] if i < len(v) else None
        ch = {}
        for line in chars:
            v = line[i] if i < len(line) else ''
            if ':' in v:
                k, val = v.split(':', 1)
                ch[k.strip().lower()] = val.strip()
            elif v:
                ch.setdefault('characteristics', v)
        rows.append({'gsm': gsm, 'title': at('!Sample_title'),
                     'source': at('!Sample_source_name_ch1'),
                     'organism': at('!Sample_organism_ch1'),
                     'platform': at('!Sample_platform_id'),
                     'library_strategy': at('!Sample_library_strategy'),
                     'characteristics': ch})
    return rows, meta


def samples(acc, *, i_have_network_permission=False, timeout=60):
    """The series' samples, and every field a condition could be read from."""
    _need(i_have_network_permission)
    acc = check_accession(acc)
    url = f'{series_url(acc)}/matrix/{acc}_series_matrix.txt.gz'
    try:
        text = _text(_get(url, timeout))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        # A series on several platforms has one matrix per platform.
        listing = _text(_get(f'{series_url(acc)}/matrix/', timeout))
        names = sorted(set(re.findall(rf'({acc}-GPL\d+_series_matrix\.txt\.gz)', listing)))
        if not names:
            raise K.ContractError(f'{acc} has no series matrix at {series_url(acc)}/matrix/')
        url = f'{series_url(acc)}/matrix/{names[0]}'
        text = _text(_get(url, timeout))
    rows, meta = parse_series_matrix(text)
    keys = {}
    for r in rows:
        for k, v in [('title', r['title']), ('source', r['source'])] + list(
                r['characteristics'].items()):
            keys.setdefault(k, {}).setdefault(v, 0)
            keys[k][v] += 1
    # A field whose every value differs (a title, a replicate id) groups nothing.
    groups = {k: v for k, v in keys.items() if 1 < len(v) < len(rows)}
    return {'accession': acc, 'url': url, 'retrieved_at': K.now_iso(), 'series': meta,
            'organisms': sorted({r['organism'] for r in rows if r['organism']}),
            'library_strategies': sorted({r['library_strategy'] for r in rows
                                          if r['library_strategy']}),
            'samples': rows, 'groupable_fields': groups,
            'note': ('Choose condition_key from groupable_fields and name the control and '
                     'treatment values exactly as written. Nothing here is inferred.')}


# ── step 2: the counts, normalised, as a long table ─────────────────────
def counts_urls(acc, organism):
    if organism not in GENOMES:
        raise K.ContractError(
            f'NCBI publishes processed RNA-seq counts for human and mouse only; this series is '
            f'{organism!r}. Its supplementary files are at {series_url(acc)}/suppl/')
    tag, annot = GENOMES[organism]
    q = urllib.parse.urlencode({'type': 'rnaseq_counts', 'acc': acc, 'format': 'file',
                                'file': f'{acc}_raw_counts_{tag}_NCBI.tsv.gz'})
    a = urllib.parse.urlencode({'type': 'rnaseq_counts', 'format': 'file', 'file': annot})
    return f'{DOWNLOAD}?{q}', f'{DOWNLOAD}?{a}'


def parse_counts(text):
    """GeneID x GSM integer matrix -> (gene ids, gsm columns, rows of floats)."""
    lines = text.splitlines()
    if not lines:
        raise K.ContractError('the counts file is empty')
    head = _cells(lines[0])
    gsms = head[1:]
    genes, rows = [], []
    for line in lines[1:]:
        if not line.strip():
            continue
        cells = _cells(line)
        try:
            vals = [float(x) for x in cells[1:len(gsms) + 1]]
        except ValueError:
            raise K.ContractError(f'a non-numeric count in row {cells[0]!r}') from None
        if len(vals) != len(gsms):
            raise K.ContractError(f'row {cells[0]!r} has {len(vals)} values for {len(gsms)} samples')
        genes.append(cells[0])
        rows.append(vals)
    return genes, gsms, rows


def parse_annotation(text):
    """GeneID -> Symbol from NCBI's annotation table."""
    reader = csv.DictReader(io.StringIO(text), delimiter='\t')
    out = {}
    for r in reader:
        gid, sym = (r.get('GeneID') or '').strip(), (r.get('Symbol') or '').strip()
        if gid and sym:
            out[gid] = sym
    return out


# ── any species: the depositors' own processed table ────────────────────
TABLE_RE = re.compile(r'href="([^"/?]+\.(?:txt|tsv|csv)(?:\.gz)?)"', re.I)
PREFER = ('count', 'expression', 'matrix', 'tpm', 'fpkm', 'rpkm', 'cpm', 'norm', 'gene')
SKIP = ('filelist', 'raw.tar', 'readme', 'peaks', 'barcodes', 'features', 'clusters')
MAMMALS = ('Homo sapiens', 'Mus musculus', 'Rattus norvegicus', 'Macaca mulatta',
           'Macaca fascicularis', 'Pan troglodytes', 'Sus scrofa', 'Bos taurus',
           'Ovis aries', 'Canis lupus familiaris', 'Equus caballus', 'Oryctolagus cuniculus')
FEATURE_NAMES = ('symbol', 'gene_symbol', 'gene_name', 'genename', 'gene', 'name', 'id',
                 'gene_id', 'geneid', 'ensembl', 'ensembl_id', 'feature')


def supplementary_files(listing_html):
    """Table-like supplementary files from a GEO directory listing, likeliest first."""
    names = []
    for n in TABLE_RE.findall(listing_html or ''):
        low = n.lower()
        if any(s in low for s in SKIP) or n in names:
            continue
        names.append(n)
    return sorted(names, key=lambda n: (not any(w in n.lower() for w in PREFER), n))


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def parse_supplementary_table(text, sample_rows, column_map=None):
    """A depositor's gene x sample table -> (gene ids, gsms, matrix, how it was read).

    A column is a sample when its header is the GSM accession, contains it, or
    is the sample's title (ignoring case and punctuation), or when the caller's
    column_map says so. Nothing else is guessed: unmatched columns are listed
    so the agent can supply the map.
    """
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith('#')]
    if len(lines) < 3:
        raise K.ContractError('the table has fewer than two data rows')
    delim = '\t' if lines[0].count('\t') >= lines[0].count(',') else ','
    rows = list(csv.reader(lines, delimiter=delim))
    head = [h.strip().strip('"') for h in rows[0]]
    by_title = {_norm(s['title']): s['gsm'] for s in sample_rows if s.get('title')}
    gsm_ids = {s['gsm'] for s in sample_rows}
    cmap = dict(column_map or {})
    cols = {}
    for j, h in enumerate(head):
        g = cmap.get(h)
        if g is None:
            g = next((x for x in gsm_ids if x == h or re.search(rf'\b{x}\b', h)), None)
        if g is None:
            g = by_title.get(_norm(h))
        if g in gsm_ids and g not in cols.values():
            cols[j] = g
    if not cols:
        raise K.ContractError(
            'no column of this table could be matched to a sample (by GSM, title or '
            f'column_map). Columns: {", ".join(head[:30])}. Sample titles: '
            + ', '.join(f'{s["gsm"]}={s["title"]}' for s in sample_rows[:30]))
    low = [_norm(h) for h in head]
    feat = next((low.index(_norm(n)) for n in FEATURE_NAMES if _norm(n) in low
                 and low.index(_norm(n)) not in cols), 0)
    gene_ids, matrix = [], []
    for r in rows[1:]:
        if len(r) < len(head):
            continue
        try:
            vals = [float(r[j]) for j in cols]
        except ValueError:
            continue                     # a summary or annotation row
        gid = r[feat].strip().strip('"')
        if gid:
            gene_ids.append(gid)
            matrix.append(vals)
    if len(gene_ids) < 2:
        raise K.ContractError('fewer than two numeric rows in the sample columns')
    flat = [v for row in matrix for v in row]
    integer = all(abs(v - round(v)) < 1e-9 for v in flat[:200000])
    top = max(flat)
    transform = ('counts' if integer and top > 1000 else
                 'log' if top <= 50 and min(flat) < 0 or top <= 25 else 'linear')
    gsms = list(cols.values())
    return gene_ids, gsms, matrix, {
        'feature_column': head[feat], 'transform': transform,
        'matched_columns': {head[j]: g for j, g in cols.items()},
        'unmatched_columns': [h for j, h in enumerate(head) if j not in cols and j != feat][:40]}


def species_weight(organism, route):
    """How far evidence from this species and processing route can carry here."""
    if organism == 'Homo sapiens':
        ceiling = 'high' if route == 'ncbi_processed' else 'moderate'
        note = None
    elif organism in MAMMALS:
        ceiling = 'moderate' if route == 'ncbi_processed' else 'low'
        note = (f'{organism}: another mammal. Genes are matched to human by symbol only, '
                f'orthology is not resolved, and regulation can differ.')
    else:
        ceiling = 'low'
        note = (f'{organism}: not a mammal. Conserved pathways may transfer, specific doses '
                f'and kinetics rarely do; genes are matched by symbol only.')
    if route != 'ncbi_processed':
        rnote = ('Values are the depositors\' own processed table, not a uniform '
                 're-processing: methods differ between labs.')
        note = f'{note} {rnote}' if note else rnote
    return {'route': route, 'organism': organism, 'confidence_ceiling': ceiling, 'note': note}


def _value_of(sample, key):
    if key == 'title':
        return sample['title']
    if key == 'source':
        return sample['source']
    if key == 'organism':
        return sample.get('organism')
    return sample['characteristics'].get(key.lower())


def build_long_table(sample_rows, gene_ids, gsms, matrix, symbols, *, condition_key, control,
                     treatments, keep=None, genes=None, max_genes=DEFAULT_MAX_GENES,
                     transform='counts'):
    """The analysable table: one row per gene and sample, on a log2 scale.

    `transform` says what the matrix holds: `counts` (raw, normalised here to
    log2(CPM + 1)), `linear` (already normalised, e.g. TPM or FPKM: log2(x + 1))
    or `log` (already on a log scale: used as it is).

    Pure, so every refusal is tested without a network: an unknown field or
    level, too few samples per group, a gene list with nothing in this series.
    """
    keep = dict(keep or {})
    by_gsm = {s['gsm']: s for s in sample_rows}
    chosen, groups = [], {}
    wanted = [control] + list(treatments)
    for j, gsm in enumerate(gsms):
        s = by_gsm.get(gsm)
        if s is None:
            continue
        if any((_value_of(s, k) or '').lower() != v.lower() for k, v in keep.items()):
            continue
        v = _value_of(s, condition_key)
        if v is None:
            continue
        match = next((w for w in wanted if w.lower() == v.lower()), None)
        if match:
            chosen.append((j, gsm, match))
            groups[match] = groups.get(match, 0) + 1
    present = sorted({_value_of(s, condition_key) for s in sample_rows
                      if _value_of(s, condition_key) is not None})
    if not present:
        raise K.ContractError(f'no sample has a field {condition_key!r}; use one of the '
                              f'groupable fields the samples step listed')
    for w in wanted:
        if groups.get(w, 0) < MIN_PER_GROUP:
            raise K.ContractError(
                f'{w!r} has {groups.get(w, 0)} sample(s) with counts after filtering; a '
                f'comparison needs at least {MIN_PER_GROUP} per group. Values of '
                f'{condition_key!r}: {", ".join(present)}')

    logcpm = []
    if transform == 'counts':
        # Library size over every gene, before any subsetting: CPM must not
        # depend on which genes someone asked about.
        lib = {j: sum(row[j] for row in matrix) for j, _, _ in chosen}
        if any(v <= 0 for v in lib.values()):
            raise K.ContractError('a chosen sample has no counts at all')
        conv = lambda x, j: math.log2(max(x, 0.0) / lib[j] * 1e6 + 1)
    elif transform == 'linear':
        conv = lambda x, j: math.log2(max(x, 0.0) + 1)
    elif transform == 'log':
        conv = lambda x, j: x
    else:
        raise K.ContractError(f'unknown transform {transform!r}')
    for i, gid in enumerate(gene_ids):
        sym = symbols.get(gid, gid)
        logcpm.append((sym, [conv(matrix[i][j], j) for j, _, _ in chosen]))

    limit = max(1, min(max_genes, MAX_ROWS // max(1, len(chosen))))
    missing = []
    if genes:
        want = {g.upper() for g in genes}
        picked = [x for x in logcpm if x[0].upper() in want]
        found = {x[0].upper() for x in picked}
        missing = sorted(g for g in genes if g.upper() not in found)
        if not picked:
            raise K.ContractError(f'none of {", ".join(genes)} is in this series\' counts')
        picked = picked[:limit]
    else:
        picked = sorted(logcpm, key=lambda x: -sum(x[1]) / len(x[1]))[:limit]

    rows = []
    for sym, vals in picked:
        for (j, gsm, cond), v in zip(chosen, vals):
            rows.append({'gene': sym, 'sample_id': gsm, 'condition': cond,
                         'logcpm': f'{v:.5f}'})
    return rows, {'samples_per_group': groups, 'genes_in_table': len(picked),
                  'genes_missing': missing, 'samples_used': [g for _, g, _ in chosen],
                  'values_of_condition_key': present}


def fetch(acc, *, condition_key, control, treatments, keep=None, genes=None,
          max_genes=DEFAULT_MAX_GENES, dataset_id=None, title=None, cell_type=None,
          registered_by='bioinformatics_agent', i_have_network_permission=False, timeout=90,
          out_root=None, register=True, overwrite=True, supplementary=None, column_map=None):
    """Fetch, normalise, write and register one comparison from a GEO series."""
    from .. import ingest as ING
    _need(i_have_network_permission)
    acc = check_accession(acc)
    if not treatments:
        raise K.ContractError('name at least one treatment value to compare with the control')
    info = samples(acc, i_have_network_permission=True, timeout=timeout)
    orgs = info['organisms']
    keep = dict(keep or {})
    if len(orgs) > 1 and 'organism' not in keep:
        raise K.ContractError(f'{acc} mixes organisms ({", ".join(orgs)}); choose one with '
                              f'--keep "organism=<name>"')
    organism = keep.get('organism') or (orgs[0] if orgs else 'unspecified')
    common = dict(condition_key=condition_key, control=control, treatments=treatments,
                  keep=keep, genes=genes, max_genes=max_genes)
    route, sources, why_not_ncbi, read = None, {'samples': info['url']}, None, {}
    if organism in GENOMES:
        counts_url, annot_url = counts_urls(acc, organism)
        try:
            gene_ids, gsms, matrix = parse_counts(_text(_get(counts_url, timeout)))
            symbols = parse_annotation(_text(_get(annot_url, timeout)))
            rows, stats = build_long_table(info['samples'], gene_ids, gsms, matrix, symbols,
                                           **common)
            route = 'ncbi_processed'
            sources.update(counts=counts_url, annotation=annot_url)
            read = {'transform': 'counts'}
        except urllib.error.HTTPError as e:
            why_not_ncbi = f'NCBI has no processed counts for {acc} (HTTP {e.code})'
    else:
        why_not_ncbi = f'NCBI processes human and mouse RNA-seq only; this is {organism}'
    if route is None:
        # Any species, and human or mouse series NCBI has not processed: the
        # depositors' own table, read only where its columns match samples.
        try:
            listing = _text(_get(f'{series_url(acc)}/suppl/', timeout))
        except urllib.error.HTTPError:
            listing = ''
        files = [f for f in supplementary_files(listing)
                 if not supplementary or f == supplementary][:4]
        if not files:
            raise K.ContractError(
                f'{why_not_ncbi}, and {acc} has no table-like supplementary file at '
                f'{series_url(acc)}/suppl/ (only archives or raw data). A person can register a '
                f'processed table from there.')
        errors = []
        for name in files:
            url = f'{series_url(acc)}/suppl/{name}'
            try:
                gene_ids, gsms, matrix, read = parse_supplementary_table(
                    _text(_get(url, timeout)), info['samples'], column_map=column_map)
                rows, stats = build_long_table(info['samples'], gene_ids, gsms, matrix, {},
                                               transform=read['transform'], **common)
            except (K.ContractError, urllib.error.HTTPError) as e:
                errors.append(f'{name}: {str(e)[:400]}')
                continue
            route = 'depositor_supplementary'
            sources.update(table=url)
            break
        if route is None:
            raise K.ContractError(f'{why_not_ncbi}. No supplementary table could be used: '
                                  + ' | '.join(errors))
    weight = species_weight(organism, route)
    counts_url = sources.get('counts') or sources.get('table')
    annot_url = sources.get('annotation')

    did = dataset_id or f'{acc.lower()}_{_slug(condition_key)}'
    base = Path(out_root or roots.cache_root()) / 'geo' / did
    base.mkdir(parents=True, exist_ok=True)
    table = base / f'{did}.tsv'
    with open(table, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['gene', 'sample_id', 'condition', 'logcpm'],
                           delimiter='\t')
        w.writeheader()
        w.writerows(rows)
    sample_tsv = base / f'{did}.samples.tsv'
    with open(sample_tsv, 'w', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(['gsm', 'title', 'source', 'characteristics'])
        for s in info['samples']:
            w.writerow([s['gsm'], s['title'], s['source'],
                        '; '.join(f'{k}: {v}' for k, v in s['characteristics'].items())])

    m = None
    if register:
        m, _ = ING.ingest_public_table(
            table, dataset_id=did, accession=acc, source='geo',
            title=title or f'{acc}: {info["series"].get("title") or "GEO series"} '
                           f'({condition_key}: {", ".join(treatments)} vs {control})',
            source_url=f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={acc}',
            organism=organism, cell_type=cell_type, modality='bulk_rna',
            experimental_design={'condition_column': 'condition', 'control': control,
                                 'treatments': list(treatments),
                                 'sample_id_column': 'sample_id'},
            description=((f'NCBI-generated raw counts for {acc}, normalised to log2(CPM+1) '
                          f'with library size over all genes' if route == 'ncbi_processed' else
                          f'The depositors\' processed table {sources["table"].rsplit("/", 1)[-1]} '
                          f'for {acc}, read as {read["transform"]} and put on a log2 scale')
                         + ', restricted to '
                         + (f'{stats["genes_in_table"]} requested genes'
                            if genes else f'the {stats["genes_in_table"]} most expressed genes')
                         + f'. Samples grouped by {condition_key!r}'
                         + (f', kept where {keep}' if keep else '') + '.'),
            registered_by=registered_by,
            notes=(f'route: {route}; sources: {sources}; confidence ceiling for this species '
                   f'and route: {weight["confidence_ceiling"]}'),
            extra_limitations=[weight['note']] if weight['note'] else [],
            overwrite=overwrite)
    return {'dataset_id': did, 'accession': acc, 'table': str(table),
            'samples_table': str(sample_tsv), 'organism': organism, 'route': route,
            'evidence_weight': weight, 'how_read': read,
            'why_not_ncbi': why_not_ncbi,
            'condition_key': condition_key, 'control': control, 'treatments': list(treatments),
            'keep': keep or {}, **stats,
            'sources': sources,
            'registered': bool(m), 'retrieved_at': K.now_iso(),
            'next': (f'analyse plan --dataset-ids {did} --analysis-type '
                     f'bulk_expression_comparison --tool bulk.expression_comparison '
                     f'--group-column condition --control "{control}" --treatment-level '
                     f'"{treatments[0]}" ...'),
            'note': ('A public dataset from another lab\'s experiment: evidence about that '
                     'experiment\'s cells and conditions, to be weighed for this process, not '
                     'a measurement of it.')}


def _slug(s):
    return re.sub(r'[^a-z0-9]+', '_', (s or '').lower()).strip('_')[:30] or 'condition'
