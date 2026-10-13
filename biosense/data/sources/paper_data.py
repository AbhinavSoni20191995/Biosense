"""The datasets a paper deposited, found from the paper rather than by keyword.

A run searched GEO nine times for PPARG, CSF2RB, TGFBR2 and SPI1 perturbations
in macrophages, found nothing, and asked a person for "the GEO accessions of
the canonical Pparg-conditional and Csf2-null alveolar macrophage studies".
Those studies are cited in the literature the run had already read; their
deposits are described as Cre lines and knockouts, which is why a gene keyword
missed them. The accession is one lookup away from the paper itself.

Two routes, both public, both answering "which datasets came from this paper":

* **GEO's own link** (NCBI ELink, pubmed -> gds). GEO records the publication
  a series supports, so this works for a paywalled paper as well as an open
  one. It returns series (GSE) the depositors linked.
* **Europe PMC's accession mining** of the paper's text — the data-availability
  statement, the methods — for open-access papers: GEO and ArrayExpress ids the
  authors wrote down, including ones deposited before the GEO link was made.

What each accession is (title, organism, sample count) comes from GEO, and an
ArrayExpress mirror of a GEO series (E-GEOD-n) is the series GSEn. Nothing is
inferred: a paper with no deposit returns nothing, and that is said.
"""
from __future__ import annotations

import json
import re
import urllib.parse

from ... import contracts as K
from . import gene_ids as GI
from . import http as HTTP

ELINK = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi'
ESUMMARY = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi'
EPMC_ANNOTATIONS = 'https://www.ebi.ac.uk/europepmc/annotations_api/annotationsByArticleIds'
EPMC_BATCH = 8                       # the endpoint's own limit per request
MAX_PAPERS = 40
GSE_RE = re.compile(r'\bGSE\d{3,8}\b')
AE_RE = re.compile(r'\bE-[A-Z]{4}-\d+\b')
PMID_RE = re.compile(r'^(?:PMID:?|MED:)?\s*(\d{1,9})$', re.I)
PMC_RE = re.compile(r'^(?:PMC:)?\s*(PMC\d{3,9})$', re.I)


def normalise(paper):
    """('MED', '28111214') or ('PMC', 'PMC5355937') from how a run writes a paper id."""
    s = str(paper or '').strip()
    m = PMID_RE.match(s)
    if m:
        return 'MED', m.group(1)
    m = PMC_RE.match(s)
    if m:
        return 'PMC', m.group(1).upper()
    raise K.ContractError(f'{paper!r} is not a PubMed id (PMID:123 or 123) or a PubMed Central '
                          f'id (PMC123); a DOI has to be resolved to one of those first')


def _json(get, url, timeout):
    return json.loads(GI.decode(get(url, timeout)))


def geo_links(pmids, *, get, timeout):
    """({pmid: [gds uid]}, error or None) from GEO's own pubmed -> gds link."""
    if not pmids:
        return {}, None
    q = urllib.parse.urlencode([('dbfrom', 'pubmed'), ('db', 'gds'), ('retmode', 'json')]
                               + [('id', p) for p in pmids])
    try:
        doc = _json(get, f'{ELINK}?{q}', timeout)
    except GI.FETCH_ERRORS as e:
        return {}, f'GEO\'s publication links could not be read: {type(e).__name__}: {str(e)[:160]}'
    out = {}
    for ls in doc.get('linksets') or []:
        ids = [str(i) for i in ls.get('ids') or []]
        uids = [str(u) for db in ls.get('linksetdbs') or [] if db.get('dbto') == 'gds'
                for u in db.get('links') or []]
        for pid in ids:
            out.setdefault(pid, []).extend(u for u in uids if u not in out.get(pid, []))
    return out, None


def geo_series(uids, *, get, timeout):
    """({gds uid: record}, error or None): accession, title, organism, samples."""
    if not uids:
        return {}, None
    q = urllib.parse.urlencode({'db': 'gds', 'id': ','.join(uids), 'retmode': 'json'})
    try:
        res = _json(get, f'{ESUMMARY}?{q}', timeout).get('result') or {}
    except GI.FETCH_ERRORS as e:
        return {}, f'GEO\'s series records could not be read: {type(e).__name__}: {str(e)[:160]}'
    out = {}
    for uid in uids:
        rec = res.get(uid) or {}
        acc = str(rec.get('accession') or '')
        if acc.startswith('GSE'):
            out[uid] = {'accession': acc, 'title': rec.get('title') or '',
                        'organism': rec.get('taxon') or '', 'type': rec.get('gdstype') or '',
                        'samples': int(rec.get('n_samples') or 0) or None}
    return out, None


def text_mined(papers, *, get, timeout):
    """({(source, id): [{'accession', 'section'}]}, error or None) from Europe PMC."""
    out, error = {}, None
    for k in range(0, len(papers), EPMC_BATCH):
        chunk = papers[k:k + EPMC_BATCH]
        q = urllib.parse.urlencode({'articleIds': ','.join(f'{s}:{i}' for s, i in chunk),
                                    'type': 'Accession Numbers', 'format': 'JSON'})
        try:
            doc = _json(get, f'{EPMC_ANNOTATIONS}?{q}', timeout)
        except GI.FETCH_ERRORS as e:
            error = (f'Europe PMC\'s accession mining could not be read: '
                     f'{type(e).__name__}: {str(e)[:160]}')
            break
        for art in doc if isinstance(doc, list) else []:
            key = (str(art.get('source') or ''), str(art.get('extId') or ''))
            found = out.setdefault(key, [])
            for an in art.get('annotations') or []:
                names = [an.get('exact') or ''] + [t.get('name') or '' for t in an.get('tags') or []]
                for name in names:
                    for acc in GSE_RE.findall(name) + AE_RE.findall(name):
                        if not any(f['accession'] == acc for f in found):
                            found.append({'accession': acc, 'section': an.get('section')})
    return out, error


def _as_geo(acc):
    """An ArrayExpress mirror of a GEO series is that series: E-GEOD-n is GSEn."""
    m = re.match(r'^E-GEOD-(\d+)$', acc)
    return f'GSE{m.group(1)}' if m else acc


def for_papers(papers, *, i_have_network_permission=False, timeout=60, get=None):
    """Every dataset the given papers deposited, by paper, with where it was found."""
    if not i_have_network_permission:
        raise K.ContractError('finding a paper\'s datasets touches the network: pass '
                              '--i-have-network-permission, which a run may only do when its '
                              'request permits public-database access')
    get = get or HTTP.get
    ids = []
    for p in papers or []:
        n = normalise(p)
        if n not in ids:
            ids.append(n)
    if not ids:
        raise K.ContractError('name at least one paper (PMID:123 or PMC123)')
    if len(ids) > MAX_PAPERS:
        raise K.ContractError(f'{len(ids)} papers; look up at most {MAX_PAPERS} at once')
    errors = []
    pmids = [i for s, i in ids if s == 'MED']
    links, err = geo_links(pmids, get=get, timeout=timeout)
    errors += [err] if err else []
    records, err = geo_series(sorted({u for us in links.values() for u in us}),
                              get=get, timeout=timeout)
    errors += [err] if err else []
    mined, err = text_mined(ids, get=get, timeout=timeout)
    errors += [err] if err else []
    by_acc = {r['accession']: r for r in records.values()}

    rows = []
    for s, i in ids:
        found = {}
        for uid in links.get(i, []) if s == 'MED' else []:
            rec = records.get(uid)
            if rec:
                found.setdefault(rec['accession'], {**rec, 'via': []})['via'].append(
                    'GEO links this series to the paper')
        for m in mined.get((s, i), []):
            acc = _as_geo(m['accession'])
            row = found.setdefault(acc, {**by_acc.get(acc, {'accession': acc}), 'via': []})
            row['via'].append('named in the paper\'s text'
                              + (f' ({m["section"]})' if m.get('section') else '')
                              + (f' as {m["accession"]}' if acc != m['accession'] else ''))
        rows.append({'paper': f'PMID:{i}' if s == 'MED' else i,
                     'datasets': list(found.values()),
                     'note': None if found else (
                         'No deposit found: GEO links no series to this paper and its text '
                         '(where Europe PMC has it) names none. A paper whose data went '
                         'elsewhere, or nowhere, is what a person can be asked about.')})
    return {'kind': 'paper_datasets', 'retrieved_at': K.now_iso(), 'papers': rows,
            'errors': errors,
            'next': ('Read a series\' samples with `datasets geo-samples --accession GSE…`, '
                     'then `datasets fetch-geo` builds and registers an analysable table. '
                     'ArrayExpress-only accessions (E-MTAB-…) are not read by fetch-geo.'),
            'note': ('Found from the papers, not by keyword: GEO\'s own link from a series to '
                     'the paper it supports, and the accessions the paper\'s text names. A '
                     'dataset here is another lab\'s experiment, to be weighed for this '
                     'process.')}
