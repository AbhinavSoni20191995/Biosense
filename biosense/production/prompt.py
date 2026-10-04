"""Turn a typed prompt into a valid ProductionRequest, and say what was assumed.

A person types one sentence. A request has a measurable target, a QC profile,
genotype arms, budgets and an autonomy mode. Most of that is not in the
sentence, so this module fills it in from defaults - and records, for every
field, whether it came from the prompt or from a default, with the exact phrase
it matched.

That record is the point. A loop that silently invented a target would be
answering a question nobody asked, and the number it reported would look like a
result. `parse` returns `(request, provenance)` where provenance has one entry
per field saying `from_prompt` or `default`, what was matched, and what was
assumed. The web app shows it before the loop starts, and the report carries it.

This is deliberately a small, legible rule set rather than a model: regular
expressions over a vocabulary, with every default stated. It extracts a gene
symbol, a target cell, a day, a fold target, the arms implied by a knockout, and
a handful of flags. Anything it cannot find it defaults and says so. It never
guesses a number that changes what "success" means without marking it.

Limits, stated plainly: it recognises the vocabulary in the tables below and
nothing else. A gene symbol it extracts is a token that looks like one, not a
validated identifier - the bioinformatics tools decide whether an annotation
exists, and a gene with no entry gets no effect. A prompt in another language,
or one that buries the target in prose, will fall through to the defaults, which
is why the assumption list is shown rather than hidden.
"""
from __future__ import annotations

import re

from .. import contracts as K

# Target cell vocabulary: phrase -> (target_cell, target_marker, standin, qc_profile)
CELL_TYPES = [
    (('t cell', 't-cell', 'tcell', 't lymphocyte', 'cd8', 'cd4', 'cytotoxic t'),
     ('T cell', 'CD3', 'ipsc_tcell', 'generic_ipsc_derived_product')),
    (('car-t', 'car t', 'chimeric antigen receptor'),
     ('CAR-T cell', 'CAR', 'tcell', 'cart_research')),
    (('monocyte', 'macrophage'),
     ('monocyte', 'CD14', 'monocyte', 'monocyte_macrophage_research')),
]
ORIGINS = [(('ipsc', 'ips cell', 'induced pluripotent', 'hipsc', 'pluripotent stem'),
            'induced pluripotent stem cell'),
           (('peripheral blood', 'pbmc', 'donor t cell', 'primary t'),
            'peripheral blood T cell')]
PERTURBATIONS = [(('knockout', 'knock-out', 'ko', 'null', 'deleted', 'deficient'), 'knockout'),
                 (('knockdown', 'knock-down', 'kd', 'shrna', 'sirna'), 'knockdown'),
                 (('overexpress', 'over-express', 'oe'), 'overexpression'),
                 (('knock-in', 'knockin'), 'knock_in')]
# Tokens that look like a gene symbol but are words from the vocabulary above.
NOT_GENES = {
    'IPSC', 'IPS', 'CAR', 'CART', 'CD3', 'CD4', 'CD8', 'CD14', 'QC', 'IL', 'HLA', 'TCR', 'PDF',
    'HTML', 'DNA', 'RNA', 'KO', 'KD', 'OE', 'WT', 'AND', 'OR', 'THE', 'FOR', 'WITH', 'FROM',
    'BEST', 'GENE', 'CELL', 'CELLS', 'T', 'A', 'I', 'II', 'III', 'GMP', 'FDA', 'USP', 'DAY',
    'CRISPR', 'CAS9', 'SOP', 'NB', 'ALSO', 'THIS', 'WHAT', 'PLEASE', 'OUTPUT', 'COMPARE',
}
#; People write "Bach2" as often as "BACH2", so the pattern accepts a mixed-case
# token and normalises it. To keep ordinary capitalised words out, a candidate must
# either contain a digit ("Bach2", "FOXP3") or be written in full caps ("TET2").
GENE_RE = re.compile(r'\b([A-Za-z][A-Za-z0-9]{1,9}(?:-[A-Za-z0-9]{1,4})?)\b')
DAY_RE = re.compile(r'\b(?:at|by|on|day|d)\s*[-=]?\s*(?:day\s*)?(\d{1,3})\b(?!\s*%)', re.I)
FOLD_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:-|\s)?(?:fold|x)\b', re.I)
PER_INPUT_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:cells?\s*)?per\s*(?:input|starting|seeded)', re.I)

# Defaults. Every one of these is stated in the provenance record when used.
DEFAULTS = {
    'target_value': 25.0,
    'at_day': 40.0,
    'day_tolerance': 2.0,
    'max_iterations': 30,
    'max_searches': 12,
    'max_full_texts': 6,
    'max_culture_days': 60.0,
    'identity_purity_pct': 60.0,
    'residual_pluripotency_pct': 0.5,
    'endotoxin_eu_per_ml': 0.5,
}
DEFAULT_BASIS = {
    'target_value': 'No numeric target was found in the prompt. 25 cells per input cell is this '
                    'tool\'s demonstration default; it is not derived from any published yield and '
                    'you should replace it with your own specification.',
    'at_day': 'No harvest day was found in the prompt. Day 40 is the designed schedule\'s own end '
              'point for an iPSC to T-lineage run, chosen here, not retrieved.',
    'identity_purity_pct': 'No identity/purity limit was given. 60% is a demonstration default; a '
                           'real limit is a QA decision and there is no universal value.',
    'residual_pluripotency_pct': 'No residual-pluripotency limit was given. 0.5% is a demonstration '
                                 'default; the real limit depends on assay sensitivity and dose, and '
                                 'residual undifferentiated iPSC is a tumorigenicity risk.',
    'endotoxin_eu_per_ml': 'No endotoxin limit was given. 0.5 EU/mL is a demonstration default; the '
                           'real limit follows USP <85> K/M for the intended dose and route.',
}


def _find(text, table):
    low = text.lower()
    for phrases, value in table:
        for p in phrases:
            if p in low:
                return value, p
    return None, None


def _genes(text):
    """Candidate gene symbols, uppercased, in order of appearance.

    A token qualifies only if it carries a digit or is written in full caps, which
    is what keeps 'Please' and 'Compare' out while letting 'Bach2' through. This
    is a shape test, not identity validation: whether an annotation exists for the
    symbol is the bioinformatics tools' answer, and they report 'not found' rather
    than inventing an effect.
    """
    out = []
    for m in GENE_RE.finditer(text):
        raw = m.group(1)
        sym = raw.upper()
        has_digit = any(c.isdigit() for c in raw)
        all_caps = raw.isupper() and len(raw) >= 3
        if not (has_digit or all_caps):
            continue
        if sym in NOT_GENES or sym in out:
            continue
        if re.fullmatch(r'(IL|CD)-?\d+', sym):
            continue
        out.append(sym)
    return out


def _slug(text, n=28):
    s = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return (s[:n].rstrip('-') or 'prompt')


def parse(prompt, request_id=None):
    """Return (request, provenance). Raises ContractError if the result is invalid."""
    text = (prompt or '').strip()
    if len(text) < 10:
        raise K.ContractError('the prompt needs at least 10 characters to read a question from')
    prov = []

    def note(field, source, value, matched=None, basis=None):
        prov.append({'field': field, 'source': source, 'value': value,
                     'matched_phrase': matched, 'basis': basis})

    cells, cell_phrase = _find(text, CELL_TYPES)
    if cells:
        target_cell, marker, standin, qc = cells
        note('product.target_cell', 'from_prompt', target_cell, cell_phrase)
    else:
        target_cell, marker, standin, qc = 'T cell', 'CD3', 'ipsc_tcell', \
            'generic_ipsc_derived_product'
        note('product.target_cell', 'default', target_cell, None,
             'No target cell was recognised, so the tool assumed a T cell. It recognises T cell, '
             'CAR-T cell and monocyte/macrophage and nothing else.')
    note('product.target_marker', 'default' if not cells else 'from_prompt', marker, cell_phrase,
         'The identity marker follows the target cell. It stands for whichever marker your assay '
         'actually reads.')
    note('standin', 'default' if not cells else 'from_prompt', standin, cell_phrase,
         'The stand-in reactor is chosen by target cell. It is synthetic in every case.')

    origin, origin_phrase = _find(text, ORIGINS)
    if origin:
        note('product.cell_origin', 'from_prompt', origin, origin_phrase)
    else:
        origin = 'induced pluripotent stem cell'
        note('product.cell_origin', 'default', origin, None,
             'No starting cell was recognised, so the tool assumed iPSC.')

    pert, pert_phrase = _find(text, PERTURBATIONS)
    genes = _genes(text)
    arms = [{'arm_id': 'WT', 'genotype': 'wild_type', 'gene': None, 'modification': None,
             'zygosity': 'not_applicable', 'line_id': None,
             'description': 'Unedited control arm'}]
    if pert and genes:
        gene = genes[0]
        arms.append({'arm_id': f'{gene}_{"KO" if pert == "knockout" else pert[:2].upper()}',
                     'genotype': pert, 'gene': gene,
                     'modification': f'CRISPR-Cas9 {pert}, isogenic to the control line '
                                     f'(assumed; the prompt did not state the method)',
                     'zygosity': 'homozygous' if pert == 'knockout' else None, 'line_id': None,
                     'description': f'Isogenic {gene} {pert} of the same line'})
        note('genotype_arms', 'from_prompt', [a['arm_id'] for a in arms],
             f'{pert_phrase} + {gene}',
             f'A control arm is always added: a perturbed arm with nothing to compare against '
             f'cannot answer whether the genotype needs different conditions. '
             f'{gene} was read as a gene symbol because it looks like one - whether any annotation '
             f'exists for it is decided by the bioinformatics tools, which report "not found" '
             f'rather than guessing.')
        if len(genes) > 1:
            note('genotype_arms.ignored_genes', 'from_prompt', genes[1:], None,
                 'More than one gene-like token was found. Only the first became an arm; add the '
                 'others explicitly if you want them compared.')
    elif pert and not pert_phrase is None:
        note('genotype_arms', 'default', ['WT'], pert_phrase,
             f'A perturbation ({pert}) was mentioned but no gene symbol was found, so only a '
             f'control arm was created. Name the gene to get a comparison.')
    else:
        note('genotype_arms', 'default', ['WT'], None,
             'No perturbation was mentioned, so the loop runs a single wild-type arm.')

    day_m = DAY_RE.search(text)
    at_day = float(day_m.group(1)) if day_m else DEFAULTS['at_day']
    if day_m:
        note('desired_output.at_day', 'from_prompt', at_day, day_m.group(0))
    else:
        note('desired_output.at_day', 'default', at_day, None, DEFAULT_BASIS['at_day'])

    per_m = PER_INPUT_RE.search(text) or FOLD_RE.search(text)
    value = float(per_m.group(1)) if per_m else DEFAULTS['target_value']
    if per_m:
        note('desired_output.value', 'from_prompt', value, per_m.group(0),
             'Read as cells per input cell. A fold number and a per-input-cell number are not '
             'always the same quantity; check that this is what you meant.')
    else:
        note('desired_output.value', 'default', value, None, DEFAULT_BASIS['target_value'])

    note('qc_profile', 'default', qc, None,
         'The QC profile follows the product. Its limits are configurable starting points and your '
         'QA team owns the final specification.')
    for key, cid in (('identity_purity_pct', 'qc-identity-purity'),
                     ('residual_pluripotency_pct', 'qc-residual-pluripotency'),
                     ('endotoxin_eu_per_ml', 'qc-endotoxin')):
        note(f'qc_overrides.{cid}', 'default', DEFAULTS[key], None, DEFAULT_BASIS[key])
    note('loop_budget.max_iterations', 'default', DEFAULTS['max_iterations'], None,
         'The search moves one lever per arm per iteration, so it needs room to work through the '
         'protocol\'s coordinates. Lower it to stop sooner.')
    note('bioreactor_source', 'default', 'synthetic_standin', None,
         'FORCED, not assumed. A prompt-driven run always uses the synthetic stand-in: an '
         'unattended loop must not produce a run sheet that reads as if a person approved it.')
    note('human_in_the_loop.mode', 'default', 'autonomous', None,
         'Autonomous is the only mode an unattended run can use, and it is permitted here solely '
         'because the reactor is synthetic. A wet-lab run always needs a named human approver, '
         'whatever the mode asks for.')

    rid = request_id or f'prompt-{_slug(target_cell)}-{_slug(arms[-1]["arm_id"])}-d{at_day:g}'
    request = {
        'schema_version': '2.0',
        'request_id': re.sub(r'[^A-Za-z0-9_.-]', '-', rid)[:64].strip('-'),
        'question': text if len(text) >= 10 else f'Optimise production of {target_cell}',
        'product': {
            'species': 'human', 'cell_origin': origin, 'target_cell': target_cell,
            'target_subtype': None, 'target_marker': marker, 'cell_line': None,
            'culture_format': 'suspension_aggregate', 'intended_use': 'research',
        },
        'desired_output': {
            'metric': 'target_cells_per_input_cell', 'op': '>=', 'value': value,
            'unit': 'cells/input_cell', 'at_day': at_day,
            'day_tolerance': DEFAULTS['day_tolerance'],
            'day_origin': f'{origin} seeding into the vessel',
            'basis': ('Read from the prompt.' if per_m else DEFAULT_BASIS['target_value']),
        },
        'qc_profile': qc,
        'qc_overrides': [
            {'id': 'qc-identity-purity', 'value': DEFAULTS['identity_purity_pct'],
             'basis': DEFAULT_BASIS['identity_purity_pct']},
            {'id': 'qc-residual-pluripotency', 'value': DEFAULTS['residual_pluripotency_pct'],
             'basis': DEFAULT_BASIS['residual_pluripotency_pct']},
            {'id': 'qc-endotoxin', 'value': DEFAULTS['endotoxin_eu_per_ml'],
             'basis': DEFAULT_BASIS['endotoxin_eu_per_ml']},
        ],
        'genotype_arms': arms,
        'current_protocol': None,
        'measurement_mode': 'minimal',
        'constraints': {
            'max_culture_days': max(DEFAULTS['max_culture_days'], at_day + 10),
            'xeno_free': True, 'gmp_grade_reagents': False,
            'notes': 'Prompt-driven run against a synthetic stand-in reactor. No wet-lab work is '
                     'authorised by this request.',
        },
        'allowed_sources': ['europepmc_open_access', 'publisher_records'],
        'search_budget': {'max_searches': DEFAULTS['max_searches'],
                          'max_full_texts': DEFAULTS['max_full_texts']},
        'loop_budget': {'max_iterations': DEFAULTS['max_iterations']},
        'human_in_the_loop': {'mode': 'autonomous', 'consults': 'high_value',
                              'max_info_actions_per_iteration': 4},
        'bioreactor_source': 'synthetic_standin',
        'bioinformatics': {'allowed': True,
                           'knowledge_sets': ['tcell_curated_genes', 'synthetic_fixture_genes'],
                           'live_lookups': False},
    }
    errors = K.schema_errors('production_request', request)
    if errors:
        raise K.ContractError('the prompt produced an invalid request: ' + '; '.join(errors[:5]))
    return request, {'prompt': text, 'standin': standin, 'fields': prov,
                     'assumed': [p for p in prov if p['source'] == 'default'],
                     'read_from_prompt': [p for p in prov if p['source'] == 'from_prompt'],
                     'note': 'Every default below changes what the loop treats as success. Check '
                             'them before reading any number the run produces as an answer.'}


def render(provenance):
    """A plain-text summary of what was understood and what was assumed."""
    lines = [f'Prompt: {provenance["prompt"]}', '']
    if provenance['read_from_prompt']:
        lines.append('Read from your prompt:')
        for p in provenance['read_from_prompt']:
            lines.append(f'  {p["field"]} = {p["value"]!r}'
                         + (f'   (matched "{p["matched_phrase"]}")' if p['matched_phrase'] else ''))
        lines.append('')
    lines.append('Assumed, because the prompt did not say:')
    for p in provenance['assumed']:
        lines.append(f'  {p["field"]} = {p["value"]!r}')
        if p['basis']:
            lines.append(f'      {p["basis"]}')
    return '\n'.join(lines)
