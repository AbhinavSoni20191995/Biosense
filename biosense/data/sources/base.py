"""The contract every dataset source implements.

A source is a module with:

    NAME                 the key in the registry and in a manifest's `source`
    LABEL                what to call it in an interface
    SEARCH_URL           the public endpoint a live search would use
    search(query, ...)   -> SearchResult

`search` never raises for "found nothing". It returns a SearchResult whose
`candidates` may be empty and whose `query_plan` says what was asked, so the
caller can tell "this index has no match" apart from "nobody looked".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ... import contracts as K

MODALITIES = ('bulk_rna', 'single_cell_rna', 'chip_seq', 'atac_seq', 'flow_cytometry',
              'cytometry_summary', 'proteomics', 'secretome', 'generic_table')


@dataclass
class DatasetCandidate:
    """One search hit, normalised. Not a dataset: nothing has been fetched."""
    accession: str
    title: str
    modality: str
    organism: str
    source: str
    url: str
    cell_type: str = None
    perturbation: str = None
    sample_count: int = None
    biological_conditions: list = field(default_factory=list)
    relevance_reason: str = ''
    processed_available: list = field(default_factory=list)
    retrieved_at: str = None

    def __post_init__(self):
        if self.modality not in MODALITIES:
            raise K.ContractError(f'{self.accession}: unknown modality {self.modality!r}')

    def to_dict(self):
        return asdict(self)


@dataclass
class SearchResult:
    source: str
    query: str
    candidates: list
    query_plan: list          # the exact public queries this would run live
    live: bool
    searched_at: str
    note: str = ''
    index: str = None         # which cached index answered, when offline

    def to_dict(self):
        d = dict(self.__dict__)
        d['candidates'] = [c.to_dict() if isinstance(c, DatasetCandidate) else c
                           for c in self.candidates]
        return d


def offline_refusal(source, url):
    return (f'Offline. No network call was made to {source}. The query plan below is what would '
            f'be sent to {url} if the request enabled live dataset search, so you can run it by '
            f'hand and check the answer.')
