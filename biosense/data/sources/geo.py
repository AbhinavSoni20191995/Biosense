"""GEO (NCBI Gene Expression Omnibus) dataset search.

Offline by default. `search()` reads the committed index at
examples/datasets/geo_index.json — a small, hand-checked set of records used to
exercise the adapter contract — and reports which index answered. It is a
fixture, not a mirror of GEO, and says so in every result it returns.

The live path is written and gated behind an explicit permission flag plus
`gates['dataset_search_live']`. It has **not** been exercised against the real
service from this repository: outbound access to NCBI is blocked in the
environment this was developed in. Treat the live branch as untested code until
someone runs it with network access and confirms the parsing.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ... import contracts as K
from .. import roots
from .base import DatasetCandidate, SearchResult, offline_refusal

NAME = 'geo'
LABEL = 'NCBI GEO'
SEARCH_URL = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi'
SUMMARY_URL = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi'
INDEX = roots.FIXTURES / 'geo_index.json'

# Modality is read off the GEO library strategy / platform wording rather than
# guessed from the title.
STRATEGY_MODALITY = {
    'rna-seq': 'bulk_rna', 'expression profiling by high throughput sequencing': 'bulk_rna',
    'expression profiling by array': 'bulk_rna',
    'scrna-seq': 'single_cell_rna', 'single cell rna-seq': 'single_cell_rna',
    'chip-seq': 'chip_seq', 'atac-seq': 'atac_seq',
}


def query_plan(query, organism=None, modality=None):
    """The exact public queries a live search would send."""
    terms = [query]
    if organism:
        terms.append(f'"{organism}"[Organism]')
    if modality in ('bulk_rna',):
        terms.append('"expression profiling by high throughput sequencing"[DataSet Type]')
    elif modality == 'single_cell_rna':
        terms.append('"single cell"[All Fields]')
    elif modality in ('chip_seq', 'atac_seq'):
        terms.append('"genome binding/occupancy profiling by high throughput sequencing"[DataSet Type]')
    term = ' AND '.join(terms)
    q = urllib.parse.urlencode({'db': 'gds', 'term': term, 'retmode': 'json', 'retmax': 20})
    return [
        {'step': 'esearch', 'url': f'{SEARCH_URL}?{q}', 'returns': 'GDS/GSE record ids matching the term'},
        {'step': 'esummary', 'url': f'{SUMMARY_URL}?db=gds&id=<ids>&retmode=json',
         'returns': 'Accession, title, organism, sample count and supplementary file list'},
    ]


def _tokens(s):
    return {w for w in ''.join(c.lower() if c.isalnum() else ' ' for c in (s or '')).split()
            if len(w) > 2}


def _score(rec, query, organism, modality, cell_type, perturbation):
    """Token overlap plus field matches. Deliberately simple and inspectable."""
    hay = _tokens(' '.join([rec.get('title', ''), rec.get('summary', ''),
                            rec.get('cell_type') or '', rec.get('perturbation') or '',
                            ' '.join(rec.get('biological_conditions') or [])]))
    q = _tokens(query)
    overlap = q & hay
    score, why = len(overlap), []
    if overlap:
        why.append('matches ' + ', '.join(sorted(overlap)[:4]))
    if modality and rec.get('modality') == modality:
        score += 2
        why.append(f'modality {modality}')
    elif modality and rec.get('modality') != modality:
        return 0, []
    if organism and (rec.get('organism') or '').lower() != organism.lower():
        return 0, []
    if cell_type and cell_type.lower() in (rec.get('cell_type') or '').lower():
        score += 2
        why.append(f'cell type {rec["cell_type"]}')
    if perturbation and perturbation.lower() in (rec.get('perturbation') or '').lower():
        score += 2
        why.append(f'perturbation {rec["perturbation"]}')
    return score, why


def load_index(path=None):
    p = Path(path or INDEX)
    if not p.is_file():
        return []
    d = K.read_json(p)
    return d.get('records', [])


def search(query, *, organism=None, modality=None, cell_type=None, perturbation=None,
           limit=10, live=False, i_have_network_permission=False, index_path=None, timeout=20):
    """Find candidate GEO datasets. Offline unless both live flags are set."""
    if not (query or '').strip():
        raise K.ContractError('a dataset search needs a question to search for')
    plan = query_plan(query, organism, modality)
    if live:
        if not i_have_network_permission:
            raise K.ContractError(
                'a live GEO search needs i_have_network_permission=True as well as live=True. '
                'Two flags because a network call from inside an analysis loop should be a '
                'thing someone did on purpose.')
        return _search_live(query, plan, limit, timeout)

    records = load_index(index_path)
    scored = []
    for rec in records:
        s, why = _score(rec, query, organism, modality, cell_type, perturbation)
        if s > 0:
            scored.append((s, rec, why))
    scored.sort(key=lambda x: (-x[0], x[1].get('accession', '')))
    out = []
    for s, rec, why in scored[:limit]:
        out.append(DatasetCandidate(
            accession=rec['accession'], title=rec['title'], modality=rec['modality'],
            organism=rec['organism'], source=NAME,
            url=rec.get('url') or f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={rec["accession"]}',
            cell_type=rec.get('cell_type'), perturbation=rec.get('perturbation'),
            sample_count=rec.get('sample_count'),
            biological_conditions=rec.get('biological_conditions') or [],
            processed_available=rec.get('processed_available') or [],
            relevance_reason='; '.join(why) or 'term overlap',
            retrieved_at=rec.get('retrieved_at')))
    note = offline_refusal(LABEL, SEARCH_URL) + (
        ' The index searched is a small committed fixture for exercising this adapter, not a '
        'copy of GEO: an empty result means the fixture has no match, never that GEO has none.')
    return SearchResult(source=NAME, query=query, candidates=out, query_plan=plan, live=False,
                        searched_at=K.now_iso(), note=note,
                        index=str(index_path or INDEX))


def _search_live(query, plan, limit, timeout):
    """Never run against the real service from this repository.

    The parsing below IS covered, in tests/test_geo_live.py, by replacing the
    transport and feeding recorded eutils response shapes through it: that is the
    half most likely to be wrong, since a renamed field or a count arriving as a
    string breaks it silently. What those tests cannot tell you is whether NCBI
    is up or whether its schema still looks like this.
    """
    out = []
    try:
        with urllib.request.urlopen(plan[0]['url'], timeout=timeout) as r:
            ids = json.loads(r.read(4_000_001)).get('esearchresult', {}).get('idlist', [])[:limit]
        if ids:
            url = f'{SUMMARY_URL}?db=gds&id={",".join(ids)}&retmode=json'
            with urllib.request.urlopen(url, timeout=timeout) as r:
                payload = json.loads(r.read(8_000_001)).get('result', {})
            for i in ids:
                rec = payload.get(i) or {}
                if not str(rec.get('accession') or '').startswith('GSE'):
                    continue        # a GDS, platform or sample record: only series are fetched
                strategy = (rec.get('gdstype') or '').lower()
                modality = next((m for k, m in STRATEGY_MODALITY.items() if k in strategy), None)
                if modality is None:
                    continue
                out.append(DatasetCandidate(
                    accession=rec.get('accession', i), title=rec.get('title', ''),
                    modality=modality, organism=rec.get('taxon', ''), source=NAME,
                    url=f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={rec.get("accession", i)}',
                    sample_count=int(rec.get('n_samples') or 0) or None,
                    relevance_reason=(f'GEO esearch hit for {query!r}: '
                                      f'{(rec.get("summary") or "")[:300]}'),
                    retrieved_at=K.now_iso()))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return SearchResult(source=NAME, query=query, candidates=[], query_plan=plan, live=True,
                            searched_at=K.now_iso(),
                            note=f'Live GEO search failed: {type(e).__name__}: {e}. '
                                 f'No result is not the same as no such data.')
    return SearchResult(source=NAME, query=query, candidates=out, query_plan=plan, live=True,
                        searched_at=K.now_iso(),
                        note='Live GEO records. Nothing has been downloaded: read a series\' '
                             'samples with `datasets geo-samples`, then `datasets fetch-geo` '
                             'builds and registers an analysable table (any species: NCBI\'s '
                             'processed counts, or the depositors\' own table).')
