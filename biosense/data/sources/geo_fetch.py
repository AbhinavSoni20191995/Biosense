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
import shlex
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
        elif key == '!Series_pubmed_id':
            # One line per paper, or several values on one line.
            ids = meta.setdefault('pubmed_ids', [])
            ids.extend(v for v in vals if v.isdigit() and v not in ids)
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
            'pubmed_ids': meta.get('pubmed_ids', []),
            'papers': [f'https://pubmed.ncbi.nlm.nih.gov/{p}/' for p in meta.get('pubmed_ids', [])],
            'samples': rows, 'groupable_fields': groups,
            'note': ('Choose condition_key from groupable_fields and name the control and '
                     'treatment values exactly as written. Nothing here is inferred. First read '
                     'series.summary, series.overall_design and the linked paper (pubmed_ids) '
                     'to learn how the depositors named samples and conditions — their '
                     'supplementary table may use other names than the GEO titles.')}


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


def parse_annotation_ensembl(text):
    """Ensembl gene id (unversioned) -> Symbol from NCBI's annotation table, or None.

    None when the table has no Ensembl column: the caller then says so rather
    than guessing. An Ensembl id NCBI gives two different symbols is left out.
    """
    reader = csv.DictReader(io.StringIO(text), delimiter='\t')
    col = next((f for f in reader.fieldnames or []
                if _norm(f) in ('ensemblgeneid', 'ensemblid', 'ensemblgene', 'ensembl')), None)
    if col is None:
        return None
    out, clash = {}, set()
    for r in reader:
        sym = (r.get('Symbol') or '').strip()
        for e in re.split(r'[;,| ]+', (r.get(col) or '').strip()):
            if not sym or not ENSEMBL_RE.match(e):
                continue
            e = _strip_version(e).upper()
            if out.get(e, sym) != sym:
                clash.add(e)
            out[e] = sym
    for e in clash:
        out.pop(e)
    return out


# ── a depositor's feature ids -> gene symbols ───────────────────────────
ENSEMBL_RE = re.compile(r'^ENS[A-Z]{0,6}[GTPRE]\d{6,}(?:\.\d+)?(?:_PAR_Y)?$', re.I)
REFSEQ_RE = re.compile(r'^[NX][MRP]_\d+(?:\.\d+)?$')
NUMERIC_RE = re.compile(r'^[-+]?\d+(?:[.,]\d+)?$')
BIOTYPES = frozenset((
    'protein_coding', 'protein-coding', 'lncrna', 'lincrna', 'mirna', 'snrna', 'snorna',
    'scarna', 'misc_rna', 'rrna', 'srna', 'scrna', 'vault_rna', 'ribozyme', 'mt_trna',
    'mt_rrna', 'trna', 'ncrna', 'antisense', 'sense_intronic', 'sense_overlapping',
    'processed_transcript', 'bidirectional_promoter_lncrna', '3prime_overlapping_ncrna',
    'macro_lncrna', 'non_coding', 'known_ncrna', 'retained_intron', 'nonsense_mediated_decay',
    'non_stop_decay', 'artifact', 'other', 'unknown'))


def _strip_version(ens):
    return re.sub(r'\.\d+(?=(_PAR_Y)?$)', '', ens)


def _is_biotype(p):
    low = p.lower()
    return (low in BIOTYPES or 'pseudogene' in low
            or bool(re.match(r'^(ig|tr)_[a-z]+_gene$', low)))


def feature_symbol(fid):
    """The gene symbol in a depositor's feature id, or the id itself. Pure.

    `ENSG00000125730.16|C3|protein_coding` -> `C3`: a composite id is split on
    '|' and the first component that is not an Ensembl, RefSeq or numeric id
    and not a biotype is taken. A bare Ensembl id loses only its version
    (`ENSG00000125730.16` -> `ENSG00000125730`); mapping it to a symbol needs
    an annotation table (parse_annotation_ensembl). Anything else is returned
    as written — nothing is looked up or guessed here.
    """
    s = (fid or '').strip().strip('"').strip()
    parts = [p.strip() for p in s.split('|')]
    if len(parts) > 1:
        sym = next((p for p in parts if p and not ENSEMBL_RE.match(p) and not REFSEQ_RE.match(p)
                    and not NUMERIC_RE.match(p) and not _is_biotype(p)), None)
        if sym:
            return sym
    ens = next((p for p in parts if ENSEMBL_RE.match(p)), None)
    return _strip_version(ens) if ens else s


def supplementary_symbols(gene_ids, ensembl_map=None, ensembl_note=None):
    """Feature id -> symbol for a depositor's table, and a sentence saying how. Pure.

    Composite ids give their symbol component; bare Ensembl ids are mapped with
    `ensembl_map` (unversioned id -> symbol, from NCBI's annotation) where it
    has them, else kept unversioned. A symbol already taken by an earlier row
    is not reused: the later row keeps its Ensembl (or own) id, so no gene
    appears twice under one name.
    """
    out, used, dups = {}, set(), []
    composite = versions = ens = mapped = 0
    for gid in gene_ids:
        sym = feature_symbol(gid)
        if '|' in gid and not ENSEMBL_RE.match(sym):
            composite += 1
        if ENSEMBL_RE.match(sym):
            ens += 1
            versions += sym != gid.strip().strip('"')
            hit = (ensembl_map or {}).get(sym.upper())
            if hit:
                sym, mapped = hit, mapped + 1
        if sym.upper() in used and sym != gid:
            dups.append(gid)
            sym = next((feature_symbol(p) for p in gid.split('|') if ENSEMBL_RE.match(p.strip())),
                       gid)
        used.add(sym.upper())
        if sym != gid:
            out[gid] = sym
    how = []
    if composite:
        ex = next(g for g in gene_ids if '|' in g)
        how.append(f'{composite} composite ids (e.g. {ex!r} -> {feature_symbol(ex)!r}) reduced '
                   f'to their symbol component')
    if ens:
        if ensembl_map is not None:
            how.append(f'{mapped} of {ens} bare Ensembl ids mapped to symbols with '
                       f'{ensembl_note or "an annotation table"}; '
                       f'{ens - mapped} unmapped keep their Ensembl id')
        else:
            how.append(f'{ens} bare Ensembl ids kept as Ensembl ids'
                       + (f' ({ensembl_note})' if ensembl_note else '')
                       + '; request such genes by Ensembl id')
        if versions:
            how.append(f'{versions} Ensembl version suffixes stripped')
    if dups:
        how.append(f'{len(dups)} features share a symbol with an earlier row and keep their own '
                   f'id (e.g. {dups[0]!r})')
    return out, ('; '.join(how) if how else 'symbols as written in the feature column')


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


DELIMITERS = {'\t': 'tab', ',': 'comma', ';': 'semicolon'}


def sniff_delimiter(lines):
    """Tab, comma or semicolon: the one that splits the header and the first data
    lines into the same number of fields (the header may have one fewer: R row
    names), and into the most. Pure.

    A semicolon table written with decimal commas splits inconsistently on
    commas, and its header not at all, so it is not mistaken for a comma table.
    """
    sample = lines[:8]
    best, best_key = None, None
    for d in DELIMITERS:
        widths = [len(r) for r in csv.reader(sample, delimiter=d)]
        head, data = widths[0], widths[1:]
        if head < 2:
            continue
        consistent = bool(data) and len(set(data)) == 1 and head in (data[0], data[0] - 1)
        key = (consistent, min([head] + data))
        if best_key is None or key > best_key:
            best, best_key = d, key
    if best is None:
        raise K.ContractError('the table splits into one column on tab, comma and semicolon')
    return best


def _number(x, decimal_comma=False):
    """float(x); with a semicolon delimiter, '1,23' is read as 1.23 (decimal comma)."""
    x = x.strip().strip('"')
    if decimal_comma and ',' in x and '.' not in x:
        x = x.replace(',', '.')
    return float(x)


def _numeric_columns(rows, width, decimal_comma, probe=50):
    """Indices of the columns whose values are numbers in (nearly) every probed row."""
    out = set()
    probe_rows = [r for r in rows[:probe] if len(r) == width]
    for j in range(width):
        vals = [r[j] for r in probe_rows if r[j].strip()]
        ok = 0
        for v in vals:
            try:
                _number(v, decimal_comma)
                ok += 1
            except ValueError:
                pass
        if vals and ok >= 0.8 * len(vals):
            out.add(j)
    return out


# ── which table column is which sample: suggested, never silently guessed ──
NULL_WORDS = frozenset('untreated control ctrl none no vehicle veh mock nt baseline unstimulated '
                       'naive medium basal'.split())
UNIT_WORDS = frozenset('ng ml pg ug h hr hrs d day days min um nm mm'.split())
STOP_WORDS = frozenset('with and of in for the on at by from to plus a an vs'.split())
DATA_WORDS = frozenset('count counts raw read reads tpm fpkm rpkm cpm norm normalized normalised '
                       'expr expression sample samples rnaseq rna seq value values'.split())
TIME_WORDS = UNIT_WORDS | frozenset('hour hours week weeks wk passage p dose time timepoint t'
                                    .split())
# Characteristic keys that name a replicate, not a condition (compared with _norm).
REPLICATE_KEYS = frozenset((
    'rep', 'replicate', 'replicatenumber', 'biologicalreplicate', 'technicalreplicate', 'biorep',
    'donor', 'donorid', 'batch', 'sample', 'sampleid', 'samplenumber', 'individual', 'subject',
    'subjectid', 'patient', 'patientid', 'lane', 'run', 'animal', 'animalid', 'mouse', 'mouseid',
    'id'))
_REP_MARKED = re.compile(r'^(?P<rest>.*?)'
                         r'(?:(?:^|[\s_\-,;:/|()#]+)(?:n|r|rep|repl|replicate|biorep|br)'
                         r'|(?:rep|replicate))[\s_\-.#]*(?P<n>\d{1,2})[\s)\]]*$', re.I)
_REP_PLAIN = re.compile(r'^(?P<rest>.*?)[\s_,;:/|()#]+(?P<n>\d{1,2})[\s)\]]*$')
_CHUNK_SPLIT = re.compile(r'[\s_,;/|()\[\]{}:+=#&]+')


def split_replicate(text):
    """(text without a trailing replicate number, the number or None). Pure.

    `MoCul_n1`, `x_rep2`, `x-r3`, `..., replicate 2` and `veh_1` carry one; a
    number glued to letters (`LN229`), after a hyphen without a marker (`IL-4`)
    or after a time or unit word (`day 3`) does not.
    """
    text = (text or '').strip()
    m = _REP_MARKED.match(text)
    if m:
        return m.group('rest').strip(' _-,;:/|(#'), int(m.group('n'))
    m = _REP_PLAIN.match(text)
    if m and m.group('rest').strip():
        words = _CHUNK_SPLIT.split(m.group('rest').strip())
        if words and words[-1].lower() not in TIME_WORDS:
            return m.group('rest').strip(' _-,;:/|(#'), int(m.group('n'))
    return text, None


def _camel(s):
    return [x.lower() for x in re.sub(r'([a-z])([A-Z])', r'\1 \2', s).split()]


def _units(text):
    """[(token, forms)]: lowercase tokens of a header or a sample field. Pure.

    Split on whitespace, underscore, comma, semicolon, slash, parentheses and
    pipe; a hyphenated piece is one token (GM-CSF -> gmcsf, IL-4 -> il4) whose
    pieces are kept as alternative forms; camelCase splits only at a lower ->
    upper step (MoCul -> mo, cul; GMCSF stays).
    """
    out = []
    for chunk in _CHUNK_SPLIT.split(text or ''):
        chunk = chunk.strip('.-')
        if not chunk:
            continue
        pieces = [p for p in chunk.split('-') if p]
        if len(pieces) == 1:
            out.extend((t, {t}) for t in _camel(pieces[0]))
        else:
            canon = ''.join(pieces).lower()
            forms = {canon}
            for p in pieces:
                forms.add(p.lower())
                forms.update(_camel(p))
            out.append((canon, forms))
    return out


def _ignorable(tok):
    return (bool(NUMERIC_RE.match(tok)) or tok in UNIT_WORDS or tok in STOP_WORDS
            or tok in DATA_WORDS or bool(re.match(r'^(gs[em]\d+|(n|r|rep|replicate)\d+)$', tok)))


def _sample_profile(s):
    title, rep = split_replicate(s.get('title') or '')
    chars = s.get('characteristics') or {}
    if rep is None:
        for k, v in chars.items():
            if _norm(k) in ('rep', 'replicate', 'replicatenumber', 'biologicalreplicate'):
                m = re.search(r'(\d{1,2})\s*$', v or '')
                rep = int(m.group(1)) if m else None
    fields = [(title, True), (s.get('source') or '', True)] + [
        (v or '', _norm(k) not in REPLICATE_KEYS) for k, v in chars.items()]
    units = [_units(t) for t, _ in fields]
    targets = []                          # (string, kind, unit positions); kind 0 = one token
    for fi, us in enumerate(units):
        for ui, (canon, forms) in enumerate(us):
            for f in forms:
                targets.append((f, 0, ((fi, ui),)))
        for n in (2, 3):                  # 'gm' 'csf' written apart still give 'gmcsf'
            for ui in range(len(us) - n + 1):
                targets.append((''.join(c for c, _ in us[ui:ui + n]), 1,
                                tuple((fi, ui + k) for k in range(n))))
    cond = {c for (_, is_cond), us in zip(fields, units) if is_cond for c, _ in us
            if not _ignorable(c) and c not in NULL_WORDS}
    signature = (_norm(title), _norm(s.get('source')),
                 tuple(sorted((_norm(k), _norm(v)) for k, v in chars.items()
                              if _norm(k) not in REPLICATE_KEYS)))
    return {'gsm': s['gsm'], 'title': s.get('title') or '', 'rep': rep, 'units': units,
            'targets': targets, 'cond': cond, 'signature': signature}


def _match_token(h, targets):
    """Unit positions a header token matches: equal first, then as an abbreviation
    (a prefix, len >= 2, never of a null or stop word: 'co' is not 'control')."""
    tests = (lambda x: x == h,
             lambda x: len(h) >= 2 and x.startswith(h) and x not in NULL_WORDS
             and x not in STOP_WORDS)
    for test in tests:
        for kind in (0, 1):
            hit = [ids for x, k, ids in targets if k == kind and test(x)]
            if hit:
                return {i for ids in hit for i in ids}
    return set()


def _suggest_one(header, profiles):
    rest, rep = split_replicate(header)
    toks = [c for c, _ in _units(rest) if not _ignorable(c)]
    if not toks:
        return 'none', [], 'the header has no name tokens to compare'
    scored = []
    for p in profiles:
        if rep is not None and p['rep'] is not None and rep != p['rep']:
            continue
        hit, matched = set(), []
        for t in toks:
            pos = _match_token(t, p['targets'])
            if pos:
                matched.append(t)
                hit |= pos
        if matched:
            scored.append((p, matched, {p['units'][fi][ui][0] for fi, ui in hit}))
    if not scored:
        return 'none', [], 'no replicate-compatible sample shares a name token with the header'
    top = max(len(m) for _, m, _ in scored)
    best = [x for x in scored if len(x[1]) == top]
    gsms = [p['gsm'] for p, _, _ in best]
    best = [x for x in best if all(t in x[1] or t in NULL_WORDS for t in toks)]
    if not best:
        missing = sorted({t for _, m, _ in scored if len(m) == top for t in toks
                          if t not in m and t not in NULL_WORDS})
        return 'none', gsms, (f'header token(s) {", ".join(missing)} match no candidate sample: '
                              f'a condition the sample metadata may name differently')
    why = f'header tokens {", ".join(toks)} matched'
    if len(best) > 1:
        # Prefer the candidates with the fewest condition tokens the header does
        # not mention: no treatment token in the header -> the untreated sample.
        common = set.intersection(*(p['cond'] for p, _, _ in best))
        extra = {p['gsm']: sorted((p['cond'] - common) - hits) for p, _, hits in best}
        low = min(len(v) for v in extra.values())
        dropped = [g for g, v in extra.items() if len(v) > low]
        best = [x for x in best if len(extra[x[0]['gsm']]) == low]
        if dropped:
            why += (f'; preferred over {", ".join(dropped)}, whose condition token(s) '
                    f'{", ".join(sorted({t for g in dropped for t in extra[g]}))} the header '
                    f'does not mention')
    score = round(top / len(toks), 2)
    if len(best) == 1:
        p = best[0][0]
        if rep is not None and p['rep'] == rep:
            why += f'; replicate {rep} agrees'
        return 'one', p['gsm'], f'name similarity to {p["title"]!r}: {why}', score
    if len({p['signature'] for p, _, _ in best}) == 1:
        return 'equiv', [p['gsm'] for p, _, _ in best], why, score, rep
    return 'none', [p['gsm'] for p, _, _ in best], (
        f'{why}, equally well by samples of different conditions')


def suggest_column_map(headers, sample_rows):
    """Proposed HEADER -> GSM for table columns whose names are neither GSM ids nor
    titles, from name similarity. Pure; conservative: an ambiguous header is
    left unresolved with its candidates, never guessed.

    A trailing replicate number (`_n1`, `_rep2`, `-r3`, ` replicate 2`, `_1`)
    must agree with the sample's when both have one. Header tokens match a
    sample's title, source and characteristic values, equal or as an
    abbreviation (mo -> monocyte, cul -> culture). The samples matching the most
    header tokens are kept; among them, those with the fewest condition tokens
    the header does not mention (numbers, units and null words like
    'untreated' aside). A header token no candidate has blocks the suggestion.
    Candidates still tied that are one condition (same title without replicate,
    source and characteristics apart from replicate/donor/batch keys) are
    assigned one-to-one in replicate, then column, order — which sample of an
    identical condition a column is does not change a group comparison. Each
    GSM is used at most once.

    Returns {'suggested': {header: {gsm, basis, score}},
             'unresolved': {header: {candidates, reason}}}.
    """
    profiles = [_sample_profile(s) for s in sample_rows]
    order = {p['gsm']: i for i, p in enumerate(profiles)}
    picks = {h: _suggest_one(h, profiles) for h in headers}
    suggested, unresolved = {}, {}
    ones = {}
    for h, r in picks.items():
        if r[0] == 'one':
            ones.setdefault(r[1], []).append(h)
    for h, r in picks.items():
        if r[0] == 'none':
            unresolved[h] = {'candidates': r[1], 'reason': r[2]}
        elif r[0] == 'one' and len(ones[r[1]]) > 1:
            unresolved[h] = {'candidates': [r[1]], 'reason': (
                f'competes with {", ".join(x for x in ones[r[1]] if x != h)} for {r[1]}')}
        elif r[0] == 'one':
            suggested[h] = {'gsm': r[1], 'basis': r[2], 'score': r[3]}
    taken = {v['gsm'] for v in suggested.values()}
    groups = {}
    for i, (h, r) in enumerate(picks.items()):
        if r[0] == 'equiv':
            groups.setdefault(tuple(r[1]), []).append((r[4] if r[4] is not None else 10 ** 6, i, h))
    for gsms, hs in groups.items():
        avail = sorted((g for g in gsms if g not in taken), key=order.get)
        hs.sort()
        if len(hs) > len(avail):
            for _, _, h in hs:
                unresolved[h] = {'candidates': list(gsms), 'reason': (
                    f'{len(hs)} headers for {len(avail)} free samples of one condition')}
            continue
        for (_, _, h), g in zip(hs, avail):
            r = picks[h]
            suggested[h] = {'gsm': g, 'score': r[3], 'basis': (
                f'replicate order within an identical condition ({", ".join(gsms)}): {r[2]}')}
            taken.add(g)
    return {'suggested': {h: suggested[h] for h in headers if h in suggested},
            'unresolved': {h: unresolved[h] for h in headers if h in unresolved}}


def column_map_args(suggested):
    """A ready-to-paste `--column-map HEADER=GSM ...` for suggested pairs."""
    return '--column-map ' + ' '.join(shlex.quote(f'{h}={v["gsm"]}')
                                      for h, v in suggested.items())


def parse_supplementary_table(text, sample_rows, column_map=None, *, infer_columns=False,
                              column_map_basis=None):
    """A depositor's gene x sample table -> (gene ids, gsms, matrix, how it was read).

    Tab, comma or semicolon delimited (decimal commas read in a semicolon
    table); an R-style header one field short of the data rows means the first
    data field is the row name. A column is a sample when its header is the
    GSM accession, contains it, or is the sample's title (ignoring case and
    punctuation), or when the caller's column_map says so. Only with
    `infer_columns` are confident name-similarity suggestions
    (suggest_column_map) applied to the numeric columns left over, each
    recorded with its basis; otherwise they are only offered.
    """
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith('#')]
    if len(lines) < 3:
        raise K.ContractError('the table has fewer than two data rows')
    delim = sniff_delimiter(lines)
    rows = list(csv.reader(lines, delimiter=delim))
    head = [h.strip().strip('"') for h in rows[0]]
    widths = [len(r) for r in rows[1:21]]
    row_names = sum(w == len(head) + 1 for w in widths) > len(widths) / 2
    if row_names:
        head = [''] + head
    dc = delim == ';'
    numeric = _numeric_columns(rows[1:], len(head), dc)
    by_title = {_norm(s['title']): s['gsm'] for s in sample_rows if s.get('title')}
    gsm_ids = {s['gsm'] for s in sample_rows}
    cmap = dict(column_map or {})
    cols, mapped = {}, {}
    for j, h in enumerate(head):
        g = cmap.get(h)
        by_map = g is not None
        if g is None:
            g = next((x for x in gsm_ids if x == h or re.search(rf'\b{x}\b', h)), None)
        if g is None:
            g = by_title.get(_norm(h))
        if g in gsm_ids and g not in cols.values():
            cols[j] = g
            if by_map:
                mapped[h] = g
    low = [_norm(h) for h in head]
    feat = next((low.index(_norm(n)) for n in FEATURE_NAMES if _norm(n) in low
                 and low.index(_norm(n)) not in cols), 0)
    rest = [(j, h) for j, h in enumerate(head)
            if j not in cols and j != feat and j in numeric and h]
    free = [s for s in sample_rows if s['gsm'] not in cols.values()]
    sug = (suggest_column_map([h for _, h in rest], free) if rest and free
           else {'suggested': {}, 'unresolved': {}})
    inferred = {}
    if infer_columns:
        for j, h in rest:
            s = sug['suggested'].get(h)
            if s and s['gsm'] not in cols.values():
                cols[j] = s['gsm']
                inferred[h] = s
    if not cols:
        offer = ''
        if sug['suggested']:
            offer += ('Suggested from header/sample-name similarity, NOT applied — check each '
                      'pair against the series\' overall design and its paper (geo-samples '
                      'pubmed_ids) first, then pass it with --column-map-basis saying why (or '
                      're-run with --infer-columns): '
                      f'{column_map_args(sug["suggested"])}. ')
        if sug['unresolved']:
            offer += 'Unresolved headers: ' + '; '.join(
                f'{h} ({", ".join(v["candidates"]) or "no candidate"}: {v["reason"]})'
                for h, v in list(sug['unresolved'].items())[:20]) + '. '
        raise K.ContractError(
            'no column of this table could be matched to a sample (by GSM, title or '
            f'column_map). {offer}Columns: {", ".join(head[:30])}. Sample titles: '
            + ', '.join(f'{s["gsm"]}={s["title"]}' for s in sample_rows[:30]))
    cols = dict(sorted(cols.items()))
    gene_ids, matrix = [], []
    for r in rows[1:]:
        if len(r) < len(head):
            continue
        try:
            vals = [_number(r[j], dc) for j in cols]
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
    how = {'delimiter': DELIMITERS[delim],
           'feature_column': head[feat] or ('(row names)' if row_names
                                             else '(unnamed first column)'),
           'transform': transform,
           'matched_columns': {head[j]: g for j, g in cols.items()},
           'unmatched_columns': [h for j, h in enumerate(head) if j not in cols and j != feat][:40]}
    if row_names:
        how['row_names'] = ('the header has one field fewer than the data rows (R row names): '
                            'the first data field is the feature id')
    if dc and any(',' in r[j] for r in rows[1:200] if len(r) >= len(head) for j in cols):
        how['decimal_comma'] = True
    if mapped:
        how['mapped_columns'] = mapped
    unused = sorted(h for h in cmap if h not in mapped)
    if unused:
        how['column_map_unused'] = unused
    if inferred:
        how['inferred_columns'] = inferred
        if sug['unresolved']:
            how['unresolved_columns'] = sug['unresolved']
    elif sug['suggested']:
        how['suggested_column_map'] = column_map_args(sug['suggested'])
    if mapped or inferred:
        how['column_map_basis'] = column_map_basis or 'none given'
    return gene_ids, gsms, matrix, how


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
          out_root=None, register=True, overwrite=True, supplementary=None, column_map=None,
          infer_columns=False, column_map_basis=None):
    """Fetch, normalise, write and register one comparison from a GEO series.

    On the supplementary route, `column_map` (HEADER -> GSM) and the opt-in
    `infer_columns` (apply confident name-similarity suggestions) assign
    columns whose headers are neither GSM ids nor titles; either is recorded
    in how_read with `column_map_basis` and stated as a dataset limitation.
    """
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
        errors, ens_cache = [], {}
        for name in files:
            url = f'{series_url(acc)}/suppl/{name}'
            try:
                gene_ids, gsms, matrix, read = parse_supplementary_table(
                    _text(_get(url, timeout)), info['samples'], column_map=column_map,
                    infer_columns=infer_columns, column_map_basis=column_map_basis)
                symbols, read['symbols'], annot = _table_symbols(gene_ids, organism, acc,
                                                                 timeout, ens_cache)
                rows, stats = build_long_table(info['samples'], gene_ids, gsms, matrix, symbols,
                                               transform=read['transform'], **common)
            except (K.ContractError, urllib.error.HTTPError) as e:
                # Long enough to keep a suggested --column-map whole.
                errors.append(f'{name}: {str(e)[:4000]}')
                continue
            route = 'depositor_supplementary'
            sources.update(table=url)
            if annot:
                sources.update(annotation=annot)
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
            extra_limitations=([weight['note']] if weight['note'] else [])
            + column_limitations(read),
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


def _table_symbols(gene_ids, organism, acc, timeout, cache):
    """Symbols for a depositor's table: the pure normaliser, plus NCBI's annotation
    for bare human/mouse Ensembl ids. -> (map, how, annotation url used or None)."""
    if not any(ENSEMBL_RE.match(feature_symbol(g)) for g in gene_ids):
        m, how = supplementary_symbols(gene_ids)
        return m, how, None
    if 'r' not in cache:
        if organism not in GENOMES:
            cache['r'] = (None, None, f'NCBI publishes its gene annotation for human and mouse '
                                      f'only; this series is {organism}')
        else:
            url = counts_urls(acc, organism)[1]
            try:
                emap = parse_annotation_ensembl(_text(_get(url, timeout)))
                cache['r'] = ((emap, url, f"NCBI's annotation table ({url}, EnsemblGeneID "
                                          f"column)") if emap is not None else
                              (None, None, f"NCBI's annotation table ({url}) has no Ensembl "
                                           f"column"))
            except (OSError, EOFError, ValueError, K.ContractError) as e:
                cache['r'] = (None, None, f"NCBI's annotation table could not be read: "
                                          f"{str(e)[:200]}")
    emap, url, note = cache['r']
    m, how = supplementary_symbols(gene_ids, emap, note)
    return m, how, url


def column_limitations(read):
    """Dataset limitations stating which sample columns were assigned other than by
    the depositors' own GSM ids or titles, and on what basis."""
    out = []
    basis = read.get('column_map_basis') or 'none given'
    if read.get('mapped_columns'):
        out.append('Sample columns assigned by a column map the caller supplied, not by the '
                   'depositors\' GSM ids or titles: '
                   + ', '.join(f'{h} -> {g}' for h, g in read['mapped_columns'].items())
                   + f'. Basis given: {basis}.')
    if read.get('inferred_columns'):
        out.append('Sample columns inferred by BioSense from header/sample-name similarity '
                   '(--infer-columns), not stated by the depositors: '
                   + '; '.join(f'{h} -> {v["gsm"]} ({v["basis"]})'
                               for h, v in read['inferred_columns'].items())
                   + f'. Basis given: {basis}. A wrong assignment mislabels conditions.')
    return out


def _slug(s):
    return re.sub(r'[^a-z0-9]+', '_', (s or '').lower()).strip('_')[:30] or 'condition'
