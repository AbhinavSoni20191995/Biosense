"""Gene identifiers to gene symbols, so a run does not have to ask a person.

A run fetched three human series and could analyse none of them by gene. NCBI's
counts are keyed by GeneID and one depositor's table by bare Ensembl id; the
one table that maps either to a symbol — NCBI's annotation — did not arrive,
every symbol-based tool then refused ("none of PPARG, MARCO … is in this
series' counts"), and the run asked a person for an identifier map that NCBI
and Ensembl both publish for free.

Mapping is now a lookup that ends in a symbol or an honest miss, in order:

1. this server's copy of the table, fetched once and reused by every series
   and every run — a burst of fetches is one download, not one per series,
   and one download is what a throttling service lets through;
2. NCBI's GEO annotation table for the organism, fetched with backoff;
3. NCBI's gene_info for the organism, from the FTP site GEO itself is served
   from, when the annotation will not come — GeneID, symbol and the Ensembl
   cross-reference in one file;
4. Ensembl's REST lookup, for Ensembl gene ids neither NCBI table covers, in
   any species.

A table is kept only once it parses into a map: an error page kept as an
annotation would be a failure that never retries. Every result says which
source gave it, and an id nothing maps keeps its own id — so a gene is never
matched to a similar-looking one, and can always be asked for by its id.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import urllib.error
import urllib.parse
import zlib
from pathlib import Path

from ... import contracts as K

ENSEMBL_RE = re.compile(r'^ENS[A-Z]{0,6}[GTPRE]\d{6,}(?:\.\d+)?(?:_PAR_Y)?$', re.I)
_ENSEMBL_GENE_RE = re.compile(r'^ENS[A-Z]{0,6}G\d{6,}$', re.I)
ENSEMBL_LOOKUP = 'https://rest.ensembl.org/lookup/id'
ENSEMBL_BATCH = 1000              # the endpoint's own limit per request
GENE_INFO = {
    'Homo sapiens': 'https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Mammalia/'
                    'Homo_sapiens.gene_info.gz',
    'Mus musculus': 'https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Mammalia/'
                    'Mus_musculus.gene_info.gz',
}
CACHE_DIR = 'gene_ids'
ID_HEADERS = ('geneid', 'gene_id', 'entrezid', 'entrezgeneid', 'ncbigeneid', 'id')
SYMBOL_HEADERS = ('symbol', 'genesymbol', 'gene_symbol', 'genename', 'name')
# What a failed fetch or a garbled file raises. A read that fails is a source
# that did not answer, recorded and passed over, never a crash of the fetch.
FETCH_ERRORS = (urllib.error.URLError, OSError, EOFError, ValueError, zlib.error,
                K.ContractError)


def decode(data):
    """Text from bytes. A .gz can arrive gzipped twice (the file, and the
    transfer) and urllib undoes neither: unwrap until it is no longer gzip."""
    for _ in range(3):
        if data[:2] != b'\x1f\x8b':
            break
        data = gzip.decompress(data)
    return data.decode('utf-8', errors='replace').lstrip('﻿')


def strip_version(ens):
    return re.sub(r'\.\d+(?=(_PAR_Y)?$)', '', ens)


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


# ── the tables, parsed. Pure. ───────────────────────────────────────────
def parse_annotation(text):
    """GeneID -> Symbol from NCBI's annotation table.

    Read by meaning, not by exact header: a byte-order mark, a comment line or
    a header in another case once made this return nothing, silently, and every
    gene kept its number. The caller reports how many ids got a symbol.
    """
    lines = [l for l in text.lstrip('﻿').splitlines() if l.strip() and not l.startswith('#')]
    if not lines:
        return {}
    reader = csv.reader(lines, delimiter='\t')
    head = [_norm(h.strip().strip('"')) for h in next(reader)]
    idc = next((head.index(_norm(h)) for h in ID_HEADERS if _norm(h) in head), None)
    syc = next((head.index(_norm(h)) for h in SYMBOL_HEADERS if _norm(h) in head), None)
    if idc is None or syc is None:
        return {}
    out = {}
    for r in reader:
        if len(r) <= max(idc, syc):
            continue
        gid, sym = r[idc].strip().strip('"'), r[syc].strip().strip('"')
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
            e = strip_version(e).upper()
            if out.get(e, sym) != sym:
                clash.add(e)
            out[e] = sym
    for e in clash:
        out.pop(e)
    return out


def parse_gene_info(text):
    """({GeneID: symbol}, {Ensembl gene id: symbol}) from NCBI's gene_info. Pure.

    Its header begins with '#' (`#tax_id  GeneID  Symbol …`), which the
    annotation reader would skip as a comment, and the Ensembl id is one of the
    `dbXrefs` (`MIM:601487|HGNC:HGNC:9236|Ensembl:ENSG00000132170`). An Ensembl
    id that two GeneIDs claim with different symbols is left out.
    """
    lines = text.lstrip('﻿').splitlines()
    if not lines:
        return {}, {}
    head = [_norm(h) for h in lines[0].lstrip('#').split('\t')]
    if 'geneid' not in head or 'symbol' not in head:
        return {}, {}
    gi, sy = head.index('geneid'), head.index('symbol')
    xi = head.index('dbxrefs') if 'dbxrefs' in head else None
    by_id, by_ens, clash = {}, {}, set()
    for line in lines[1:]:
        if not line.strip() or line.startswith('#'):
            continue
        c = line.split('\t')
        if len(c) <= max(gi, sy):
            continue
        gid, sym = c[gi].strip(), c[sy].strip()
        if not gid or not sym or sym == '-':
            continue
        by_id[gid] = sym
        if xi is not None and len(c) > xi:
            for x in c[xi].split('|'):
                if x.startswith('Ensembl:'):
                    e = strip_version(x[len('Ensembl:'):].strip()).upper()
                    if by_ens.get(e, sym) != sym:
                        clash.add(e)
                    by_ens[e] = sym
    for e in clash:
        by_ens.pop(e)
    return by_id, by_ens


def _parse_annot_both(text):
    return parse_annotation(text), parse_annotation_ensembl(text)


# ── a table, from this server's copy or the service ─────────────────────
def _name(url):
    """The file a URL names: `file=` for GEO's download endpoint, else the path."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return (q.get('file') or [None])[0] or urllib.parse.urlparse(url).path.rsplit('/', 1)[-1]


def _cached(url, *, get, timeout, cache_dir, parse):
    """(parsed, where) for the table at *url*.

    This server's copy when it parses; else fetched, parsed and kept — only if
    it parsed into a map, so a transient error page is never remembered.
    """
    path = Path(cache_dir) / CACHE_DIR / _name(url) if cache_dir else None
    if path is not None and path.is_file():
        try:
            parsed = parse(decode(path.read_bytes()))
            if parsed[0]:
                return parsed, 'a copy kept on this server'
        except FETCH_ERRORS:
            pass
        try:
            path.unlink()                # a copy that does not parse is not a copy
        except OSError:
            pass
    data = get(url, timeout)
    text = decode(data)
    parsed = parse(text)
    if not parsed[0]:
        raise K.ContractError(f'it has no GeneID and symbol columns (it began '
                              f'{" ".join(text[:80].split())!r})')
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + '.tmp')
            tmp.write_bytes(data)
            tmp.replace(path)
            K.write_json_atomic(path.with_suffix(path.suffix + '.json'), {
                'url': url, 'retrieved_at': K.now_iso(), 'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest()})
        except OSError:
            pass                         # the map is still good; it is only not kept
    return parsed, 'fetched now'


class NcbiMaps:
    """What NCBI gave for one organism: the maps, where they came from, what failed."""

    def __init__(self):
        self.by_geneid, self.by_ensembl = {}, None
        self.geneid_source = self.ensembl_source = None
        self.geneid_note = self.ensembl_note = None
        self.errors = []


def ncbi_maps(organism, *, annot_url, get, timeout, cache_dir=None, need_ensembl=False):
    """GeneID -> symbol (and, if asked, Ensembl -> symbol) from NCBI. Never raises.

    The GEO annotation first; NCBI's gene_info when the annotation will not come
    or, with *need_ensembl*, when it has no Ensembl column.
    """
    m = NcbiMaps()
    if annot_url:
        try:
            (gid, ens), where = _cached(annot_url, get=get, timeout=timeout,
                                        cache_dir=cache_dir, parse=_parse_annot_both)
            m.by_geneid, m.geneid_source = gid, annot_url
            m.geneid_note = f"NCBI's annotation table ({where})"
            if ens is not None:
                m.by_ensembl, m.ensembl_source = ens, annot_url
                m.ensembl_note = f"NCBI's annotation table ({annot_url}, EnsemblGeneID column)"
            else:
                m.errors.append(f"NCBI's annotation table ({annot_url}) has no Ensembl column")
        except FETCH_ERRORS as e:
            m.errors.append(f"NCBI's annotation table could not be read: "
                            f"{type(e).__name__}: {str(e)[:200]}")
    url = GENE_INFO.get(organism)
    if url and (not m.by_geneid or (need_ensembl and m.by_ensembl is None)):
        try:
            (gid, ens), where = _cached(url, get=get, timeout=timeout, cache_dir=cache_dir,
                                        parse=parse_gene_info)
            if not m.by_geneid:
                m.by_geneid, m.geneid_source = gid, url
                m.geneid_note = f"NCBI's gene_info ({where})"
            if m.by_ensembl is None and ens:
                m.by_ensembl, m.ensembl_source = ens, url
                m.ensembl_note = f"NCBI's gene_info ({url}, Ensembl cross-references)"
        except FETCH_ERRORS as e:
            m.errors.append(f"NCBI's gene_info could not be read: "
                            f"{type(e).__name__}: {str(e)[:200]}")
    return m


def ensembl_symbols(ids, *, get, timeout):
    """({Ensembl gene id: symbol}, error or None) from Ensembl's REST lookup.

    Gene ids only — a transcript's display name is a transcript name — in
    batches of the endpoint's own limit. Never raises; a lookup that fails
    part-way keeps what it had.
    """
    want = sorted({strip_version(i).upper() for i in ids
                   if _ENSEMBL_GENE_RE.match(strip_version(i or ''))})
    out = {}
    for k in range(0, len(want), ENSEMBL_BATCH):
        try:
            data = get(ENSEMBL_LOOKUP, timeout,
                       data=json.dumps({'ids': want[k:k + ENSEMBL_BATCH]}).encode(),
                       headers={'Content-Type': 'application/json',
                                'Accept': 'application/json'})
            doc = json.loads(decode(data))
        except FETCH_ERRORS as e:
            return out, (f"Ensembl's lookup could not be reached: "
                         f"{type(e).__name__}: {str(e)[:160]}")
        for e, rec in (doc if isinstance(doc, dict) else {}).items():
            sym = rec.get('display_name') if isinstance(rec, dict) else None
            if sym:
                out[str(e).upper()] = str(sym)
    return out, None


def ensembl_map(ids, organism, *, annot_url, get, timeout, cache_dir=None):
    """(map or None, sources used or None, note) for the bare Ensembl ids in *ids*.

    NCBI's tables for human and mouse, then Ensembl's lookup for what they did
    not cover, in any species. The map is None only when nothing mapped any id,
    which the caller reports as ids kept as they are.
    """
    ens = {strip_version(i).upper() for i in ids if ENSEMBL_RE.match(i or '')}
    emap, used, notes = {}, [], []
    if annot_url or organism in GENE_INFO:
        m = ncbi_maps(organism, annot_url=annot_url, get=get, timeout=timeout,
                      cache_dir=cache_dir, need_ensembl=True)
        notes.extend(m.errors)
        if m.by_ensembl:
            emap.update({e: s for e, s in m.by_ensembl.items() if e in ens})
            used.append(m.ensembl_source)
            notes.insert(0, m.ensembl_note)
    else:
        notes.append(f'NCBI publishes its gene annotation for human and mouse only; this series '
                     f'is {organism}')
    rest = sorted(ens - set(emap))
    if rest:
        got, err = ensembl_symbols(rest, get=get, timeout=timeout)
        if got:
            emap.update(got)
            used.append(ENSEMBL_LOOKUP)
            notes.append(f"Ensembl's REST lookup ({ENSEMBL_LOOKUP}) for {len(got)} more")
        if err:
            notes.append(err)
    return (emap or None), (' ; '.join(u for u in used if u) or None), '; '.join(notes)
