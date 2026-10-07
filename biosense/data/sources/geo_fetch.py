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

Why NCBI's processed counts rather than the depositors' supplementary files:
those are in any format anyone chose, while NCBI re-aligns human and mouse
RNA-seq series to one reference and publishes one GeneID x GSM matrix per
series. A series without them (microarrays, older or non-human/mouse data) is
refused with that reason and the supplementary-file location — never parsed
by guesswork.

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


def _value_of(sample, key):
    if key == 'title':
        return sample['title']
    if key == 'source':
        return sample['source']
    return sample['characteristics'].get(key.lower())


def build_long_table(sample_rows, gene_ids, gsms, matrix, symbols, *, condition_key, control,
                     treatments, keep=None, genes=None, max_genes=DEFAULT_MAX_GENES):
    """The analysable table: one row per gene and sample, with log2(CPM + 1).

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

    # Library size over every gene, before any subsetting: CPM must not depend
    # on which genes someone asked about.
    lib = {j: sum(row[j] for row in matrix) for j, _, _ in chosen}
    if any(v <= 0 for v in lib.values()):
        raise K.ContractError('a chosen sample has no counts at all')
    logcpm = []
    for i, gid in enumerate(gene_ids):
        sym = symbols.get(gid, gid)
        vals = [math.log2(matrix[i][j] / lib[j] * 1e6 + 1) for j, _, _ in chosen]
        logcpm.append((sym, vals))

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
          out_root=None, register=True, overwrite=True):
    """Fetch, normalise, write and register one comparison from a GEO series."""
    from .. import ingest as ING
    _need(i_have_network_permission)
    acc = check_accession(acc)
    if not treatments:
        raise K.ContractError('name at least one treatment value to compare with the control')
    info = samples(acc, i_have_network_permission=True, timeout=timeout)
    orgs = info['organisms']
    if len(orgs) != 1:
        raise K.ContractError(f'{acc} mixes organisms ({", ".join(orgs) or "none stated"}); '
                              f'this fetch handles one')
    counts_url, annot_url = counts_urls(acc, orgs[0])
    try:
        counts_text = _text(_get(counts_url, timeout))
    except urllib.error.HTTPError as e:
        raise K.ContractError(
            f'NCBI has no processed RNA-seq counts for {acc} (HTTP {e.code}): it is probably a '
            f'microarray, single-cell or not-yet-processed series. Its supplementary files are '
            f'at {series_url(acc)}/suppl/ — a person can register a processed table from there.'
        ) from None
    gene_ids, gsms, matrix = parse_counts(counts_text)
    symbols = parse_annotation(_text(_get(annot_url, timeout)))
    rows, stats = build_long_table(info['samples'], gene_ids, gsms, matrix, symbols,
                                   condition_key=condition_key, control=control,
                                   treatments=treatments, keep=keep, genes=genes,
                                   max_genes=max_genes)

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
            organism=orgs[0], cell_type=cell_type, modality='bulk_rna',
            experimental_design={'condition_column': 'condition', 'control': control,
                                 'treatments': list(treatments),
                                 'sample_id_column': 'sample_id'},
            description=(f'NCBI-generated raw counts for {acc}, normalised to log2(CPM+1) with '
                         f'library size over all genes, restricted to '
                         + (f'{stats["genes_in_table"]} requested genes'
                            if genes else f'the {stats["genes_in_table"]} most expressed genes')
                         + f'. Samples grouped by {condition_key!r}'
                         + (f', kept where {keep}' if keep else '') + '.'),
            registered_by=registered_by,
            notes=f'counts: {counts_url}; annotation: {annot_url}; samples: {info["url"]}',
            overwrite=overwrite)
    return {'dataset_id': did, 'accession': acc, 'table': str(table),
            'samples_table': str(sample_tsv), 'organism': orgs[0],
            'condition_key': condition_key, 'control': control, 'treatments': list(treatments),
            'keep': keep or {}, **stats,
            'sources': {'counts': counts_url, 'annotation': annot_url, 'samples': info['url']},
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
