"""Adapters that find datasets in public repositories.

Every adapter answers the same question — "which datasets are relevant to this
biological question?" — and returns the same normalised `DatasetCandidate`, so
the planner never learns a repository's vocabulary.

All of them are offline by default, in the pattern the gene lookups already use:
`search()` reads a cached index and, when it has nothing, returns the exact query
it *would* run rather than an empty list that reads like "no such data exists".
Reaching the network needs an explicit permission flag, and no test ever sets it.
"""
from . import base, geo, local  # noqa: F401

REGISTRY = {s.NAME: s for s in (geo, local)}


def get(name):
    from ... import contracts as K
    if name not in REGISTRY:
        raise K.ContractError(f'unknown dataset source {name!r}; '
                              f'available: {", ".join(sorted(REGISTRY))}')
    return REGISTRY[name]
