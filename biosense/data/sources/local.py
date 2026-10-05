"""The datasets already registered on this machine, as a searchable source.

The planner asks the same question of a repository and of the person's own data,
so this adapter exists to make "what do I already have?" answerable in the same
vocabulary. It reaches no network under any flag.

`include_private` defaults to True because the point is to find the person's own
data; the HTTP layer passes False, and `registry.list_datasets` applies that to
the roots it reads rather than to the rows it returns.
"""
from __future__ import annotations

from ... import contracts as K
from .. import registry as REG
from .base import DatasetCandidate, SearchResult

NAME = 'local'
LABEL = 'Registered on this machine'
SEARCH_URL = None


def _tokens(s):
    return {w for w in ''.join(c.lower() if c.isalnum() else ' ' for c in (s or '')).split()
            if len(w) > 2}


def search(query, *, organism=None, modality=None, cell_type=None, perturbation=None,
           limit=10, include_private=True, dirs=None, **_):
    if not (query or '').strip():
        raise K.ContractError('a dataset search needs a question to search for')
    q = _tokens(query)
    out = []
    for m in REG.list_datasets(include_private=include_private, dirs=dirs):
        if modality and m['modality'] != modality:
            continue
        if organism and (m.get('organism') or '').lower() != organism.lower():
            continue
        hay = _tokens(' '.join([m['title'], m.get('description') or '', m.get('cell_type') or '',
                                m.get('perturbation') or '', ' '.join(m.get('conditions') or [])]))
        overlap = q & hay
        why = []
        if overlap:
            why.append('matches ' + ', '.join(sorted(overlap)[:4]))
        if cell_type and cell_type.lower() in (m.get('cell_type') or '').lower():
            why.append(f'cell type {m["cell_type"]}')
        if perturbation and perturbation.lower() in (m.get('perturbation') or '').lower():
            why.append(f'perturbation {m["perturbation"]}')
        if not why:
            continue
        out.append(DatasetCandidate(
            accession=m['dataset_id'], title=m['title'], modality=m['modality'],
            organism=m.get('organism') or '', source=NAME, url=f'dataset:{m["dataset_id"]}',
            cell_type=m.get('cell_type'), perturbation=m.get('perturbation'),
            sample_count=(m.get('sample_metadata') or {}).get('sample_count'),
            biological_conditions=m.get('conditions') or [],
            relevance_reason='; '.join(why),
            processed_available=sorted({f['file_type'] for f in m['files']}),
            retrieved_at=m.get('created_at')))
    return SearchResult(source=NAME, query=query, candidates=out[:limit],
                        query_plan=[], live=False, searched_at=K.now_iso(),
                        note='Datasets already registered here. Private ones are listed for the '
                             'person who ingested them and are never served or published.')
