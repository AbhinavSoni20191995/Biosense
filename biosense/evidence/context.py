"""ResearchContext: the biological scope evidence has to fit.

"Mature human alveolar macrophages in pulmonary fibrosis" is a different
question from "macrophages". Evidence from mouse bone-marrow-derived cells is
not a weaker answer to the first; it is an answer to a second question. So
`assess()` returns `in_context` or `context_mismatch` with the field that did
not match, and `strictness` decides what happens next:

    strict   out-of-scope evidence is excluded
    prefer   it is shown, ranked lower, and labelled CONTEXT MISMATCH
    open     no restriction

What this module will not do is generalise silently — mouse to human, cell line
to primary, healthy to disease, in vitro to in vivo. Each of those is a
judgement a scientist should make knowingly, so the mismatch is surfaced rather
than absorbed.
"""
from __future__ import annotations

from .. import contracts as K

STRICTNESS = ('strict', 'prefer', 'open')
# Fields compared when deciding whether a piece of evidence is in context.
MATCHED_FIELDS = (('species', 'species'), ('cell_types', 'cell_type'),
                  ('tissues', 'tissue'), ('states', 'state'),
                  ('disease_context', 'disease'), ('modalities', 'modality'),
                  ('assays', 'assay'))

# Generalisations BioSense refuses to make quietly. Each is a real inferential
# leap that routinely gets made by accident.
NEVER_GENERALISE = (
    ('mouse', 'human', 'a mouse result is not a human result'),
    ('rat', 'human', 'a rat result is not a human result'),
    ('cell line', 'primary', 'an immortalised line is not a primary cell'),
    ('in vitro', 'in vivo', 'a dish is not an organism'),
    ('healthy', 'disease', 'a healthy donor is not a patient'),
    ('embryonic', 'mature', 'a developmental state is not a mature state'),
)


def context(*, strictness='prefer', species=(), cell_types=(), tissues=(), states=(),
            disease_context=(), modalities=(), assays=(), therapy_context=(),
            exclude=(), publication_from_year=None, publication_to_year=None,
            time_range=None, notes=None, context_id=None):
    if strictness not in STRICTNESS:
        raise K.ContractError(f'strictness must be one of {STRICTNESS}')
    c = {
        'schema_version': K.PRODUCTION_VERSION, 'context_id': context_id,
        'created_at': K.now_iso(), 'strictness': strictness,
        'species': list(species), 'cell_types': list(cell_types), 'tissues': list(tissues),
        'states': list(states), 'disease_context': list(disease_context),
        'modalities': list(modalities), 'assays': list(assays),
        'therapy_context': list(therapy_context), 'exclude': list(exclude),
        'publication_from_year': publication_from_year,
        'publication_to_year': publication_to_year,
        'time_range': time_range, 'notes': notes, 'applied': None,
    }
    K.require_valid('research_context', c)
    return c


OPEN = None


# Names for one thing. Not a generaliser: every pair here is the SAME entity
# written two ways, which is why 'Homo sapiens' matching 'human' is correct and
# 'mouse' matching 'human' is in NEVER_GENERALISE instead.
SYNONYMS = {
    'homo sapiens': 'human', 'h. sapiens': 'human', 'hsapiens': 'human',
    'mus musculus': 'mouse', 'm. musculus': 'mouse',
    'rattus norvegicus': 'rat',
    'macaca mulatta': 'rhesus macaque',
}


def _norm(s):
    v = str(s or '').strip().lower()
    return SYNONYMS.get(v, v)


def assess(ctx, item):
    """Is this evidence in context? Returns {match, mismatches, excluded, reason}.

    `item` is any dict with some of: species, cell_type, tissue, state, disease,
    modality, assay, year. A field the context does not restrict never causes a
    mismatch; a field the evidence does not state is reported as unknown rather
    than assumed to match.
    """
    if not ctx or ctx['strictness'] == 'open':
        return {'match': 'in_context', 'mismatches': [], 'unknown': [], 'excluded': False,
                'reason': 'no context restriction was requested'}

    mismatches, unknown = [], []
    for ctx_field, item_field in MATCHED_FIELDS:
        wanted = [_norm(x) for x in (ctx.get(ctx_field) or [])]
        if not wanted:
            continue
        have = _norm(item.get(item_field))
        if not have:
            unknown.append(item_field)
            continue
        if not any(w in have or have in w for w in wanted):
            mismatches.append({'field': item_field, 'wanted': ctx[ctx_field], 'found': item.get(item_field)})

    for term in (ctx.get('exclude') or []):
        blob = ' '.join(_norm(v) for v in item.values() if isinstance(v, str))
        if _norm(term).split()[0] in blob:
            return {'match': 'context_mismatch', 'mismatches': mismatches, 'unknown': unknown,
                    'excluded': True,
                    'reason': f'explicitly excluded by the context: {term}'}

    year = item.get('year')
    if year:
        lo, hi = ctx.get('publication_from_year'), ctx.get('publication_to_year')
        if (lo and year < lo) or (hi and year > hi):
            mismatches.append({'field': 'year', 'wanted': [lo, hi], 'found': year})

    if not mismatches:
        return {'match': 'in_context', 'mismatches': [], 'unknown': unknown, 'excluded': False,
                'reason': 'every restricted field matched' if not unknown else
                          f'matched on what was stated; {", ".join(unknown)} not stated'}

    bits = '; '.join(f'{m["field"]} is {m["found"]!r}, context asks for '
                     f'{", ".join(map(str, m["wanted"]))}' for m in mismatches)
    return {'match': 'context_mismatch', 'mismatches': mismatches, 'unknown': unknown,
            'excluded': ctx['strictness'] == 'strict',
            'reason': bits}


def generalisation_warnings(ctx, item):
    """Name the leaps a reader would otherwise make without noticing."""
    out = []
    blob = ' '.join(_norm(v) for v in item.values() if isinstance(v, str))
    wanted = ' '.join(_norm(x) for xs in (ctx or {}).values() if isinstance(xs, list) for x in xs)
    for found, asked, why in NEVER_GENERALISE:
        if found in blob and asked in wanted:
            out.append(f'This evidence is {found}; the context asks for {asked}. {why}, so it is '
                       f'labelled a context mismatch rather than generalised.')
    return out


def search_terms(ctx):
    """The terms a search should carry, with what was asked recorded for provenance."""
    if not ctx:
        return []
    terms = []
    for field in ('species', 'cell_types', 'states', 'tissues', 'disease_context',
                  'therapy_context', 'assays'):
        terms += [t for t in (ctx.get(field) or []) if t]
    return list(dict.fromkeys(terms))


def record_application(ctx, *, searched, query_terms, unsatisfied=()):
    """Write back what was actually searched, so the scope is auditable."""
    out = dict(ctx)
    out['applied'] = {
        'requested': {k: v for k, v in ctx.items()
                      if k in ('species', 'cell_types', 'tissues', 'states', 'disease_context',
                               'modalities', 'assays', 'therapy_context', 'exclude',
                               'publication_from_year', 'publication_to_year')},
        'searched': list(searched), 'query_terms': list(query_terms),
        'unsatisfied': [dict(u) for u in unsatisfied],
    }
    K.require_valid('research_context', out)
    return out


def describe(ctx):
    if not ctx or ctx['strictness'] == 'open':
        return 'No biological restriction: evidence from any context is considered.'
    bits = []
    for field, label in (('species', 'Species'), ('cell_types', 'Cell type'),
                         ('states', 'State'), ('tissues', 'Tissue'),
                         ('disease_context', 'Disease'), ('assays', 'Assay'),
                         ('therapy_context', 'Therapy context')):
        if ctx.get(field):
            bits.append(f'{label}: {", ".join(ctx[field])}')
    if ctx.get('exclude'):
        bits.append(f'Excluded: {", ".join(ctx["exclude"])}')
    mode = {'strict': 'Evidence outside this scope is excluded.',
            'prefer': 'In-scope evidence ranks first; out-of-scope evidence is shown '
                      'separately and labelled a context mismatch.'}[ctx['strictness']]
    return ' · '.join(bits) + '. ' + mode
