"""Bioinformatics tool functions. Deterministic, offline by default, never inventive."""
import json
import re
import urllib.error
import urllib.request

from .. import contracts as K
from . import knowledge as KB

READOUT_METRICS = ('target_cells_total', 'target_cells_per_input_cell', 'target_marker_pct', 'viability_pct',
                   'residual_pluripotency_pct', 'viable_cells_total', 'harvest_onset_day', 'growth_rate_per_h',
                   'doubling_time_h', 'lactate_per_glucose')


def annotate(genes, perturbation='knockout', cell_type=None, knowledge_sets=None, loop_id=None,
             iteration=None, question=None, asked_by=None):
    """Build a BioinformaticsReport for one or more genes."""
    if isinstance(genes, str):
        genes = [genes]
    if not genes:
        raise K.ContractError('annotate needs at least one gene symbol')
    sets = KB.load_sets(knowledge_sets)
    out_genes, levers, untestable, lims = [], [], [], []
    for sym in genes:
        sym = KB.validate_symbol(sym)
        entry, src = KB.lookup(sym, sets)
        if entry is None:
            out_genes.append({'symbol': sym, 'found': False, 'status': None, 'description': None,
                              'aliases': [], 'effects': [], 'measurable_readouts': [],
                              'live_lookup_plan': KB.live_lookup_plan(sym)})
            lims.append(f'{sym}: no entry in the loaded knowledge sets, so no effect is reported. '
                        'Run the live lookup plan or curate an annotation; do not assume absence of effect.')
            continue
        effects = KB.effects_for(entry, src, perturbation)
        bad = [e['readout_metric'] for e in effects if e['readout_metric'] not in READOUT_METRICS]
        if bad:
            lims.append(f'{sym}: readout metric(s) {bad} are not metrics this loop measures, so the '
                        'corresponding effects cannot be scored against a run.')
        out_genes.append({'symbol': sym, 'found': True, 'status': entry.get('status'),
                          'description': entry.get('description'), 'aliases': entry.get('aliases', []),
                          'effects': effects,
                          'measurable_readouts': entry.get('measurable_readouts', []),
                          'live_lookup_plan': None})
        for lv in KB.levers_for(entry, perturbation):
            levers.append({**lv, 'arm_scope': None,
                           'basis': f'{sym} {perturbation}: {lv["basis"]}'.strip()})
        untestable += KB.untestable_for(entry)
        if not effects:
            lims.append(f'{sym}: the entry exists but records no {perturbation!r} effect.')
        if entry.get('status') == 'synthetic_placeholder':
            lims.append(f'{sym} is a SYNTHETIC placeholder symbol invented for the demo fixtures. '
                        'Nothing derived from it is biological evidence.')
    if cell_type:
        lims.append(f'Annotations describe genes in general. Whether these effects hold in {cell_type} at this '
                    'stage and culture format is a hypothesis for the loop to test, not a fact.')
    lims.append('A perturbation annotation is not a dose or a schedule. It suggests which parameter to '
                'investigate; the literature agent still needs evidence before changing a value.')
    report = {
        'schema_version': K.PRODUCTION_VERSION,
        'report_id': f'bioinfo-{"-".join(g.upper() for g in genes)[:48]}-{perturbation}',
        'loop_id': loop_id, 'iteration': iteration, 'created_at': K.now_iso(),
        'request': {'genes': [KB.validate_symbol(g) for g in genes], 'perturbation': perturbation,
                    'cell_type': cell_type, 'asked_by': asked_by, 'question': question},
        'knowledge_sets': KB.set_manifest(sets),
        'genes': out_genes,
        'lever_suggestions': levers,
        'untestable_here': untestable,
        'limitations': sorted(set(lims)),
    }
    K.require_valid('bioinformatics_report', report)
    return report


def consult_candidates(report):
    """The untestable assays, shaped for a human consult notice."""
    return [{'assay': u['assay'], 'why_not_feasible': u['why_not_feasible'],
             'what_it_would_resolve': u['what_it_would_resolve']} for u in report['untestable_here']]


# Words that carry no parameter identity, plus synonyms linking a lever's wording
# to how a step is actually named in a protocol.
_STOP = {'duration', 'dose', 'concentration', 'level', 'amount', 'timing', 'time', 'day', 'window',
         'rate', 'fraction', 'of', 'the', 'per'}
_SYNONYMS = {
    'activation': ('activation', 'anti-cd3', 'cd28', 'stimulation', 'stimulus', 'bead'),
    'transduction': ('transduction', 'vector', 'lentiviral', 'retroviral', 'moi'),
    'seeding': ('seed', 'seeding', 'inoculation'),
    'feeding': ('feed', 'medium_exchange', 'exchange'),
    'harvest': ('harvest', 'start_harvest'),
}


def _lever_words(parameter):
    words = [w for w in re.split(r'[^a-z0-9-]+', parameter.lower()) if w and w not in _STOP]
    out = set(words)
    for w in words:
        out.update(_SYNONYMS.get(w, ()))
    return out


def cross_check(report, protocol):
    """Compare annotated levers with what the protocol actually does per arm.

    Returns one row per lever: whether the protocol already differentiates that
    parameter between the engineered arm and the control. This is what turns an
    annotation into an actionable gap.
    """
    from ..production.protocol import arm_schedule
    arms = {a['arm_id']: a for a in protocol['genotype_arms']}
    control = next((a for a in arms if arms[a]['genotype'] == 'wild_type'), None)
    engineered = [a for a in arms if arms[a]['genotype'] != 'wild_type']
    rows = []
    for lv in report['lever_suggestions']:
        words = _lever_words(lv['parameter'])
        differs = []
        for aid in engineered:
            if control is None:
                continue
            ctl = {s['step_id']: s for s in arm_schedule(protocol, control)}
            arm = {s['step_id']: s for s in arm_schedule(protocol, aid)}
            changed = []
            for sid, s in arm.items():
                c = ctl.get(sid)
                if c is None:
                    changed.append(f'{sid} (absent in control)')
                    continue
                same_q = json.dumps(c.get('quantity'), sort_keys=True) == json.dumps(s.get('quantity'), sort_keys=True)
                if not same_q or c['day'] != s['day'] or c.get('end_day') != s.get('end_day'):
                    label = (s.get('factor') or s['action']).lower()
                    changed.append(f'{sid} ({label})')
            hit = [ch for ch in changed if any(w in ch.lower() for w in words)]
            differs.append({'arm_id': aid, 'arm_differs_from_control': bool(changed),
                            'changed_steps': changed,
                            'lever_already_addressed': bool(hit), 'matching_steps': hit})
        rows.append({'parameter': lv['parameter'], 'direction': lv['direction'], 'confidence': lv['confidence'],
                     'basis': lv['basis'], 'per_arm': differs})
    return {'control_arm_id': control, 'engineered_arm_ids': engineered, 'levers': rows,
             'note': 'Matching is by parameter keywords and a small synonym table against step factors and '
                     'actions, so read the changed_steps list rather than trusting the flag alone.'}


def live_lookup(symbol, sources=None, timeout=20):
    """Fetch public annotations. Only for a request with live_lookups enabled.

    Returns raw payloads with their URLs and retrieval time. It does NOT turn them
    into perturbation effects: a human curates that into a knowledge set, because
    a database record about a gene is not a statement about your process.
    """
    symbol = KB.validate_symbol(symbol)
    wanted = set(sources or ('Ensembl', 'UniProt'))
    out = {'symbol': symbol, 'retrieved_at': K.now_iso(), 'results': [], 'errors': []}
    for name, url, returns in KB.LIVE_SOURCES:
        if name not in wanted:
            continue
        u = url.format(symbol=symbol)
        if not u.startswith('https://'):
            out['errors'].append({'source': name, 'error': 'refusing a non-HTTPS endpoint'})
            continue
        if 'graphql' in u:
            out['errors'].append({'source': name, 'error': 'needs a POST GraphQL body; run it yourself and curate the result'})
            continue
        try:
            req = urllib.request.Request(u, headers={'User-Agent': 'BioSenseBioinformatics/0.1',
                                                     'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read(4_000_001))
            out['results'].append({'source': name, 'url': u, 'returns': returns, 'payload': payload})
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as e:
            out['errors'].append({'source': name, 'url': u, 'error': f'{type(e).__name__}: {e}'})
    out['note'] = ('Raw public records. Curate them into a knowledge set with an explicit source and '
                   'confidence before any loop decision leans on them.')
    return out


def _uniprot_summary(payload):
    rec = ((payload or {}).get('results') or [None])[0] or {}
    if not rec:
        return {}
    out = {'uniprot': rec.get('primaryAccession'),
           'protein': (((rec.get('proteinDescription') or {}).get('recommendedName') or {})
                       .get('fullName') or {}).get('value')}
    for c in rec.get('comments') or []:
        if c.get('commentType') == 'FUNCTION' and not out.get('function'):
            out['function'] = ' '.join(x.get('value', '') for x in c.get('texts') or [])[:1200]
        if c.get('commentType') == 'SUBCELLULAR LOCATION':
            out['subcellular'] = sorted({(x.get('location') or {}).get('value')
                                         for x in c.get('subcellularLocations') or []
                                         if (x.get('location') or {}).get('value')})[:8]
    go = []
    for x in rec.get('uniProtKBCrossReferences') or []:
        if x.get('database') != 'GO':
            continue
        for prop in x.get('properties') or []:
            v = prop.get('value') or ''
            if prop.get('key') == 'GoTerm' and v.startswith('P:'):
                go.append(v[2:])
    out['go_biological_process'] = go[:20]
    out['keywords'] = [k.get('name') for k in rec.get('keywords') or [] if k.get('name')][:15]
    return out


def gene_info(symbols, *, timeout=20, lookup=None):
    """What public databases say about each gene, read into a few fields.

    Ensembl (identity), UniProt (function, GO biological process, location) and
    STRING (interaction partners). General records about the gene: no direction
    of effect, no statement about this cell type, stage or process — the
    agent may cite them as background and must say so. A gene the databases do
    not answer for says which source failed, never "no role".
    """
    lookup = lookup or live_lookup
    genes = []
    for sym in symbols:
        res = lookup(sym, sources=('Ensembl', 'UniProt', 'STRING'), timeout=timeout)
        g = {'symbol': res['symbol'], 'sources': [{'source': r['source'], 'url': r['url']}
                                                   for r in res['results']],
             'errors': res['errors'], 'retrieved_at': res['retrieved_at']}
        for r in res['results']:
            pl = r['payload']
            if r['source'] == 'Ensembl' and isinstance(pl, dict):
                g.update(ensembl_id=pl.get('id'), description=pl.get('description'),
                         biotype=pl.get('biotype'))
            elif r['source'] == 'UniProt':
                g.update(_uniprot_summary(pl))
            elif r['source'] == 'STRING' and isinstance(pl, list):
                partners = {}
                for e in pl:
                    for a, b in (('preferredName_A', 'preferredName_B'),
                                 ('preferredName_B', 'preferredName_A')):
                        if (e.get(a) or '').upper() == res['symbol'].upper() and e.get(b):
                            partners[e[b]] = max(partners.get(e[b], 0), e.get('score') or 0)
                g['interaction_partners'] = [k for k, _ in sorted(partners.items(),
                                                                  key=lambda x: -x[1])][:12]
        g['found'] = bool(g.get('ensembl_id') or g.get('uniprot'))
        genes.append(g)
    return {'genes': genes, 'evidence_class': 'live_annotation', 'retrieved_at': K.now_iso(),
            'note': ('Public database records about each gene in general. Background for a '
                     'hypothesis, citable by source URL; not a direction of effect and not a '
                     'statement about this cell type, stage or process.')}


def render_report(report):
    """Markdown summary for the orchestrator and the loop record."""
    r = report['request']
    L = [f'# Bioinformatics report {report["report_id"]}', '',
         f'Genes: {", ".join(r["genes"])} · perturbation: {r["perturbation"]}'
         + (f' · cell type: {r["cell_type"]}' if r['cell_type'] else ''), '',
         'Knowledge sets: ' + ', '.join(f'{s["knowledge_set_id"]} ({"live" if s["live"] else "committed"})'
                                        for s in report['knowledge_sets']), '']
    for g in report['genes']:
        L.append(f'## {g["symbol"]}' + ('' if g['found'] else ' — not found'))
        if not g['found']:
            L += ['', 'No entry in the loaded knowledge sets. Queries that would answer this:', '']
            L += [f'- {p["source"]}: `{p["url"]}` — {p["returns"]}' for p in (g['live_lookup_plan'] or [])]
            L.append('')
            continue
        L += ['', g['description'] or '', '']
        if g['effects']:
            L += ['| Affected process | Direction | Readout | Confidence | Source |', '|---|---|---|---|---|']
            L += [f'| {e["affected_process"]} | {e["direction"]} | `{e["readout_metric"]}` | '
                  f'{e["confidence"]} | {e["source"]} |' for e in g['effects']]
            L.append('')
            for e in g['effects']:
                if e.get('conditions'):
                    L.append(f'- Condition on *{e["affected_process"]}*: {e["conditions"]}')
            L.append('')
        else:
            L += ['No annotated effect for this perturbation.', '']
    if report['lever_suggestions']:
        L += ['## Protocol parameters implicated', '', '| Parameter | Direction | Confidence | Basis |', '|---|---|---|---|']
        L += [f'| {lv["parameter"]} | {lv["direction"]} | {lv["confidence"]} | {lv["basis"]} |'
              for lv in report['lever_suggestions']]
        L += ['', 'Suggestions only. A value changes when the literature agent finds evidence for it.', '']
    if report['untestable_here']:
        L += ['## Would settle it, but this machine cannot run it', '']
        L += [f'- **{u["assay"]}** — {u["why_not_feasible"]} Would resolve: {u["what_it_would_resolve"]}'
              for u in report['untestable_here']]
        L += ['', 'These are the candidates for a human consult.', '']
    L += ['## Limitations', ''] + [f'- {x}' for x in report['limitations']] + ['']
    return '\n'.join(L)
