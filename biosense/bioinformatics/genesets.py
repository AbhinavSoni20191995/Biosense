"""Gene-set libraries for pathway enrichment: fetched once, versioned, openly licensed.

Two public libraries, chosen for their terms of reuse as much as their content:

    reactome   Reactome pathways (human), CC0. ReactomePathways.gmt.zip.
    go_bp      Gene Ontology biological processes, CC BY 4.0, as Enrichr serves
               them (GO_Biological_Process_2023). Cite the Gene Ontology.

MSigDB is deliberately absent: its licence restricts redistribution, and a
result built on it could not be shared the way this project shares results.

A person may also add their own library from a GMT file (an internal marker
panel, say). It is recorded as theirs and says so wherever it is used.

Each library is stored under the cache root with its source URL, retrieval
time, SHA-256 and set count, so an enrichment result names exactly which
version of which library it was computed against.

    python -m biosense.bioinformatics.cli datasets fetch-genesets --library reactome \
        --i-have-network-permission
    python -m biosense.bioinformatics.cli datasets add-genesets --file my.gmt --name my_panel \
        --license "internal"
    python -m biosense.bioinformatics.cli datasets genesets
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from pathlib import Path

from .. import contracts as K
from ..data import roots

LIBRARIES = {
    'reactome': {
        'label': 'Reactome pathways (human)',
        'url': 'https://reactome.org/download/current/ReactomePathways.gmt.zip',
        'license': 'CC0 1.0 (Reactome)', 'format': 'reactome_zip'},
    'go_bp': {
        'label': 'Gene Ontology biological process (via Enrichr, 2023)',
        'url': ('https://maayanlab.cloud/Enrichr/geneSetLibrary?mode=text'
                '&libraryName=GO_Biological_Process_2023'),
        'license': 'CC BY 4.0 (Gene Ontology Consortium); cite the GO',
        'format': 'enrichr_text'},
}
NAME_RE = re.compile(r'^[a-z][a-z0-9_]{2,40}$')
MAX_BYTES = 60 * 1024 * 1024


def _dir(name):
    return Path(roots.cache_root()) / 'genesets' / name


def parse_gmt(text):
    """GMT text -> {set name: {'id': …, 'genes': [SYMBOL, …]}}.

    Tolerates both GMT dialects in use: Reactome's (name, stable id, genes) and
    Enrichr's (name, empty description, genes optionally written GENE,weight).
    Symbols are upper-cased; a set with fewer than two genes is dropped.
    """
    sets = {}
    for line in text.splitlines():
        cells = line.rstrip('\n').split('\t')
        if len(cells) < 3 or not cells[0].strip():
            continue
        name, ident = cells[0].strip(), cells[1].strip() or None
        genes = []
        for g in cells[2:]:
            g = g.split(',')[0].strip().upper()
            if g and g not in genes:
                genes.append(g)
        if len(genes) >= 2:
            sets[name] = {'id': ident, 'genes': genes}
    if not sets:
        raise K.ContractError('no gene sets could be read: expected GMT (name, id or blank, '
                              'genes...), tab-separated')
    return sets


def _store(name, sets, meta):
    d = _dir(name)
    d.mkdir(parents=True, exist_ok=True)
    K.write_json_atomic(d / 'library.json', {'meta': meta, 'sets': sets})
    return meta


def _get(url, timeout=120):
    """Bytes at *url*. Replaced in tests; the one place this module touches the network."""
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'BioSenseBioinformatics/0.1'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise K.ContractError(f'{url} is larger than {MAX_BYTES // 1024 // 1024} MB; not fetched')
    return data


def fetch(library, *, i_have_network_permission=False):
    """Download and store one public library. Returns its record."""
    if not i_have_network_permission:
        raise K.ContractError('fetching a gene-set library touches the network: pass '
                              '--i-have-network-permission, which a run may only do when its '
                              'request permits public-database access')
    spec = LIBRARIES.get(library)
    if spec is None:
        raise K.ContractError(f'unknown library {library!r}; the public ones are '
                              f'{", ".join(LIBRARIES)}. Add your own with add-genesets.')
    data = _get(spec['url'])
    if spec['format'] == 'reactome_zip':
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next((n for n in z.namelist() if n.lower().endswith('.gmt')), None)
            if member is None:
                raise K.ContractError('the Reactome archive holds no .gmt file')
            text = z.read(member).decode('utf-8', errors='replace')
    else:
        text = data.decode('utf-8', errors='replace')
    sets = parse_gmt(text)
    return _store(library, sets, {
        'library': library, 'label': spec['label'], 'source': 'public', 'url': spec['url'],
        'license': spec['license'], 'retrieved_at': K.now_iso(),
        'sha256': hashlib.sha256(data).hexdigest(), 'sets': len(sets)})


def add_local(path, *, name, license, added_by=None):
    """Store a person's own GMT library under *name*."""
    if not NAME_RE.match(name or '') or name in LIBRARIES:
        raise K.ContractError('a library name is 3-41 lower-case letters, digits or underscores, '
                              f'and not one of the public ones ({", ".join(LIBRARIES)})')
    if not (license or '').strip():
        raise K.ContractError('say under what terms this library may be used (--license), '
                              'even if the answer is "internal, not for publication"')
    data = Path(path).read_bytes()
    sets = parse_gmt(data.decode('utf-8', errors='replace'))
    return _store(name, sets, {
        'library': name, 'label': f'{name} (added by {added_by or "a person"})',
        'source': 'local', 'url': None, 'license': license.strip(),
        'retrieved_at': K.now_iso(), 'sha256': hashlib.sha256(data).hexdigest(),
        'sets': len(sets)})


def load(name):
    """(sets, meta) for a stored library, or a refusal saying how to get it."""
    try:
        doc = K.read_json(_dir(name) / 'library.json')
    except (OSError, ValueError):
        how = (f'fetch it: datasets fetch-genesets --library {name} --i-have-network-permission'
               if name in LIBRARIES else 'add it: datasets add-genesets --file <gmt> --name ...')
        raise K.ContractError(f'gene-set library {name!r} is not on this machine; {how}') from None
    return doc['sets'], doc['meta']


def available():
    """The libraries stored here, and the public ones that could be fetched."""
    root = Path(roots.cache_root()) / 'genesets'
    stored = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            try:
                stored.append(K.read_json(d / 'library.json')['meta'])
            except (OSError, ValueError, KeyError):
                continue
    return {'stored': stored,
            'fetchable': [{'library': k, **{x: v[x] for x in ('label', 'url', 'license')}}
                          for k, v in LIBRARIES.items()]}
