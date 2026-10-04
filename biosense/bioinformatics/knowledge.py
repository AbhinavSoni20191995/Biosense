"""Loading and querying annotation sets from bioinfo_knowledge/."""
import re

from .. import contracts as K

KNOWLEDGE_DIR = K.ROOT / 'bioinfo_knowledge'
DEFAULT_SETS = ('synthetic_fixture_genes',)
SYMBOL_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_.-]{0,31}$')
CONFIDENCE = ('synthetic_fixture', 'local_annotation', 'live_annotation', 'inference', 'unknown')
DIRECTIONS = ('increase', 'decrease', 'earlier', 'later', 'no_change', 'unknown')

# Public endpoints a live lookup would use. Emitted as a plan even when offline,
# so a reader can see exactly what would be asked and check it by hand.
LIVE_SOURCES = (
    ('Ensembl', 'https://rest.ensembl.org/lookup/symbol/homo_sapiens/{symbol}?content-type=application/json',
     'Gene identifier, location and biotype'),
    ('UniProt', 'https://rest.uniprot.org/uniprotkb/search?query=gene:{symbol}+AND+organism_id:9606&format=json&size=1',
     'Protein function, domains and subcellular location'),
    ('Open Targets', 'https://api.platform.opentargets.org/api/v4/graphql',
     'Target-disease associations and tractability (POST a GraphQL query for this symbol)'),
    ('STRING', 'https://string-db.org/api/json/network?identifiers={symbol}&species=9606',
     'Functional interaction partners, for pathway context'),
)


def validate_symbol(symbol):
    if not isinstance(symbol, str) or not SYMBOL_RE.match(symbol):
        raise K.ContractError(f'{symbol!r} is not a usable gene symbol')
    return symbol.strip()


def live_lookup_plan(symbol):
    return [{'source': name, 'url': url.format(symbol=symbol), 'returns': returns}
            for name, url, returns in LIVE_SOURCES]


def load_sets(set_ids=None):
    """Load annotation sets by file stem. Unknown ids are refused, never skipped."""
    ids = list(set_ids or DEFAULT_SETS)
    out = []
    for sid in ids:
        path = KNOWLEDGE_DIR / f'{sid}.json'
        if not path.exists():
            raise K.ContractError(f'unknown knowledge set {sid!r} (looked in {KNOWLEDGE_DIR})')
        d = K.read_json(path)
        for required in ('knowledge_set_id', 'label', 'live', 'genes'):
            if required not in d:
                raise K.ContractError(f'knowledge set {sid!r} is missing {required!r}')
        out.append(d)
    return out


def set_manifest(sets):
    return [{'knowledge_set_id': s['knowledge_set_id'], 'label': s['label'], 'live': bool(s['live']),
             'retrieved_at': s.get('retrieved_at')} for s in sets]


def _index(sets):
    """symbol (upper) -> (entry, set). Later sets win; aliases resolve too."""
    idx = {}
    for s in sets:
        for sym, entry in s['genes'].items():
            idx[sym.upper()] = (entry, s)
            for alias in entry.get('aliases', []):
                idx.setdefault(alias.upper(), (entry, s))
    return idx


def lookup(symbol, sets):
    """Return (entry, set) or (None, None). No fuzzy matching: a near miss is a miss."""
    return _index(sets).get(validate_symbol(symbol).upper(), (None, None))


def effects_for(entry, source_set, perturbation=None):
    """Annotated effects, filtered by perturbation. Malformed entries are refused loudly."""
    out = []
    for p in entry.get('perturbations', []):
        if perturbation and p.get('perturbation') != perturbation:
            continue
        for required in ('perturbation', 'affected_process', 'direction', 'readout_metric', 'confidence', 'source'):
            if required not in p:
                raise K.ContractError(f'{entry["symbol"]}: perturbation record is missing {required!r}')
        if p['direction'] not in DIRECTIONS:
            raise K.ContractError(f'{entry["symbol"]}: direction {p["direction"]!r} is not one of {DIRECTIONS}')
        if p['confidence'] not in CONFIDENCE:
            raise K.ContractError(f'{entry["symbol"]}: confidence {p["confidence"]!r} is not one of {CONFIDENCE}')
        out.append({'perturbation': p['perturbation'], 'affected_process': p['affected_process'],
                    'direction': p['direction'], 'readout_metric': p['readout_metric'],
                    'conditions': p.get('conditions'), 'mechanism': p.get('mechanism'),
                    'confidence': p['confidence'],
                    'source': f'{p["source"]} [{source_set["knowledge_set_id"]}]'})
    return out


def levers_for(entry, perturbation=None):
    out = []
    for p in entry.get('perturbations', []):
        if perturbation and p.get('perturbation') != perturbation:
            continue
        for lv in p.get('suggested_protocol_levers', []):
            out.append({'parameter': lv['parameter'], 'direction': lv['direction'],
                        'basis': lv.get('basis', ''), 'confidence': p['confidence']})
    return out


def untestable_for(entry):
    return [{'assay': x['assay'], 'why_not_feasible': x['why_not_feasible'],
             'what_it_would_resolve': x['what_it_would_resolve']}
            for x in entry.get('experiments_not_possible_in_machine', [])]
