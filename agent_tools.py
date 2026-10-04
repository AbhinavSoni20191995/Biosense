"""Literature tools for an Omnigent specialist agent. Python 3.10+, stdlib only.

The LLM executes the research instructions in agent_prompt.md. These functions
provide retrieval and deterministic validation; they do not themselves implement
an autonomous LLM or infer biological meaning from text.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import re
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest'
STAGES = {'ipsc_expansion', 'cardiac_differentiation', 'purification', 'recovery'}
ROLES = {'operating_condition', 'schedule', 'initial_condition', 'kinetic_parameter', 'outcome', 'geometry'}
# Factors convert a value into its family's reference unit. No geometry guessing.
UNITS = {
 'h': ('time', 1), 'day': ('time', 24),
 '1/h': ('rate', 1), '1/day': ('rate', 1/24),
 'uM': ('molarity', 1), 'mM': ('molarity', 1000),
 'mL': ('volume', 1), 'L': ('volume', 1000),
 'cells/mL': ('volume_density', 1), 'cells/L': ('volume_density', 1/1000),
 'cells/cm2': ('area_density', 1),
 'g/L': ('mass_concentration', 1), 'mg/mL': ('mass_concentration', 1),
 'g/L/day': ('volumetric_mass_rate', 1),
 'g/cell/day': ('cell_specific_mass_rate', 1),
 'mm': ('length', 1), 'um': ('length', .001), 'mm2': ('area', 1),
 'rpm': ('rotation', 1), 'degC': ('temperature', 1),
 'fraction': ('fraction', 1), '%': ('fraction', .01),
 '%air_saturation': ('oxygen_reference', 1), 'pH': ('ph', 1),
 'dimensionless': ('dimensionless', 1), 'text': ('text', 1),
}

def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', str(text)).split())

def get_bytes(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'BioSenseLiteraturePrototype/0.1', 'Accept': 'application/json,application/xml'})
    with urllib.request.urlopen(request, timeout=25) as response:
        data = response.read(20_000_001)
    if len(data) > 20_000_000:
        raise ValueError('Response exceeds 20 MB limit')
    return data

def search_literature(query, page_size=10, cursor='*'):
    """Query Europe PMC. Results locate evidence; abstracts are not full-text proof."""
    if not 1 <= page_size <= 25:
        raise ValueError('page_size must be 1..25')
    params = urllib.parse.urlencode({'query': query, 'format': 'json', 'resultType': 'core', 'pageSize': page_size, 'cursorMark': cursor})
    data = json.loads(get_bytes(BASE + '/search?' + params))
    papers = []
    for p in data.get('resultList', {}).get('result', []):
        papers.append({k: p.get(k) for k in ['id', 'source', 'pmcid', 'doi', 'title', 'authorString', 'firstPublicationDate', 'isOpenAccess', 'pubTypeList', 'abstractText']})
    return {'query': query, 'retrieved_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'hit_count': data.get('hitCount'), 'next_cursor': data.get('nextCursorMark'), 'papers': papers}

def fetch_full_text(pmcid):
    """Fetch open-access JATS XML through the documented Europe PMC endpoint."""
    if not re.fullmatch(r'PMC[0-9]+', pmcid):
        raise ValueError('Expected a PMCID such as PMC7076930')
    raw = get_bytes(BASE + '/' + pmcid + '/fullTextXML')
    root = ET.fromstring(raw)
    article_type = root.attrib.get('article-type', 'unknown')
    paragraphs = []
    def visit(node, section):
        if node.tag == 'sec':
            title = node.find('title')
            if title is not None:
                section = section + ' / ' + normalize(''.join(title.itertext()))
        if node.tag in {'p', 'table-wrap'}:
            paragraphs.append({'id': f'p{len(paragraphs):04}', 'section': section, 'text': normalize(''.join(node.itertext()))})
            return
        for child in node:
            visit(child, section)
    body = root.find('body')
    if body is None:
        raise ValueError('No article body available; cannot verify full-text evidence')
    visit(body, 'body')
    title = root.find('.//article-title')
    licenses = [normalize(''.join(n.itertext())) for n in root.findall('.//license')]
    supplements = [dict(n.attrib) for n in root.findall('.//supplementary-material')]
    return {'id': pmcid, 'url': 'https://europepmc.org/articles/' + pmcid, 'title': normalize(''.join(title.itertext())) if title is not None else '', 'retrieved_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'sha256': hashlib.sha256(raw).hexdigest(), 'article_type': article_type, 'license': licenses, 'supplements_retrieved': False, 'supplement_count': len(supplements), 'paragraphs': paragraphs}

def convert_unit(value, source_unit, target_unit):
    if source_unit not in UNITS or target_unit not in UNITS:
        raise ValueError('Unsupported unit; return a review item instead of guessing')
    sf, sm = UNITS[source_unit]
    tf, tm = UNITS[target_unit]
    if sf != tf or sf == 'text':
        raise ValueError('Incompatible units; no conversion permitted')
    return value * sm / tm

def effective_growth_rate(fold, duration_days):
    """Endpoint average net growth; NEVER a maximum growth rate or fitted kinetic."""
    if fold <= 0 or duration_days <= 0:
        raise ValueError('Positive fold and duration required')
    return {'value': math.log(fold) / duration_days, 'unit': '1/day', 'parameter': 'effective_net_growth_rate', 'formula': 'ln(fold)/duration_days', 'caveat': 'Includes lag, death and other losses; does not identify mu_max.'}

def compile_handoff(request, extraction, sources):
    """Reject structurally invalid claims; preserve conflicts; never invent priors.

    The quote check establishes provenance only. It cannot prove that the LLM's
    interpretation, numeric transcription, protocol assignment or context is right.
    Human semantic review remains explicit in the returned handoff.
    """
    source_map = {s['id']: s for s in sources}
    accepted, rejected = [], []
    ids = set()
    for c in extraction.get('claims', []):
        reasons = []
        required = ['id', 'protocol_id', 'parameter', 'stage', 'role', 'value', 'unit', 'context', 'evidence', 'condition_signature', 'notes']
        if any(k not in c for k in required):
            rejected.append({'claim': c, 'reasons': ['Missing required fields']})
            continue
        if not isinstance(c['id'], str) or c['id'] in ids:
            reasons.append('Invalid or duplicate claim ID')
        ids.add(c['id'])
        if c['stage'] not in STAGES or c['role'] not in ROLES:
            reasons.append('Unknown stage or role')
        if c['unit'] not in UNITS:
            reasons.append('Unknown unit')
        value = c['value']
        if c['unit'] == 'text':
            if not isinstance(value, str): reasons.append('Categorical value must be text')
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            reasons.append('Numeric scalar required; split ranges into explicitly qualified records')
        ctx = c['context']
        context_keys = ['species', 'cell_origin', 'target_cell', 'cell_line', 'culture_format', 'medium', 'time_origin', 'time_window']
        if not isinstance(ctx, dict) or any(k not in ctx for k in context_keys):
            reasons.append('Incomplete context')
        evidence = c['evidence']
        if not isinstance(evidence, dict):
            reasons.append('Evidence must be an object')
        else:
            s = source_map.get(evidence.get('source_id'))
            p = next((p for p in s.get('paragraphs', []) if p['id'] == evidence.get('paragraph_id')), None) if s else None
            quote = normalize(evidence.get('quote', ''))
            if not p or len(quote) < 3 or quote not in normalize(p['text']):
                reasons.append('Evidence excerpt not found in supplied source paragraph')
            if s and s.get('article_type') in {'retraction', 'retracted-article', 'expression-of-concern'}:
                reasons.append('Source has an adverse article-status flag')
        if c.get('status', 'reported') != 'reported':
            reasons.append('This MVP accepts directly reported claims only; derived values need separate reviewed records')
        if reasons:
            rejected.append({'claim': c, 'reasons': reasons})
        else:
            accepted.append(c)
    compatible, excluded = [], []
    for c in accepted:
        reasons = []
        ctx = c['context']
        for k in ['species', 'cell_origin', 'target_cell', 'cell_line', 'culture_format']:
            wanted = request.get('constraints', {}).get(k)
            if wanted is not None and wanted != ctx[k]:
                reasons.append(f'{k}: source {ctx[k]!r} differs from requested {wanted!r}')
        limit = request.get('constraints', {}).get('parameter_limits', {}).get(c['stage'] + ':' + c['parameter'])
        if limit:
            try:
                v = convert_unit(c['value'], c['unit'], limit['unit'])
                if ('min' in limit and v < limit['min']) or ('max' in limit and v > limit['max']):
                    reasons.append('Outside supplied hard constraint')
            except (ValueError, TypeError, KeyError):
                reasons.append('Constraint units or bounds require review')
        if reasons: excluded.append({'claim_id': c['id'], 'reasons': reasons})
        else: compatible.append(c)
    groups = {}
    for c in compatible:
        key = (c['protocol_id'], c['stage'], c['parameter'], c['condition_signature'], json.dumps(c['context'], sort_keys=True))
        groups.setdefault(key, []).append(c)
    conflicts, comparable = [], []
    for key, group in groups.items():
        unit = group[0]['unit']
        try:
            values = [convert_unit(c['value'], c['unit'], unit) if unit != 'text' else c['value'] for c in group]
            match = all(math.isclose(values[0], v, rel_tol=1e-8, abs_tol=1e-12) if isinstance(v, (int, float)) else v == values[0] for v in values)
        except ValueError:
            match = False
        if not match:
            conflicts.append({'protocol_id': key[0], 'stage': key[1], 'parameter': key[2], 'claim_ids': [c['id'] for c in group], 'resolution': 'unresolved; do not average or reinterpret as a tested range'})
        else:
            comparable.extend(group)
    available = {c['stage'] + ':' + c['parameter'] for c in comparable}
    missing = [k for k in request.get('required_parameters', []) if k not in available]
    manifest = [{k: s.get(k) for k in ['id', 'url', 'sha256', 'retrieved_at', 'article_type', 'supplement_count', 'supplements_retrieved']} for s in sources]
    protocols = [{'protocol_id': pid, 'contexts': [c['context'] for c in compatible if c['protocol_id'] == pid]} for pid in sorted({c['protocol_id'] for c in compatible})]
    return {'schema_version': '0.1', 'request_id': request['request_id'], 'ready_for_simulation': False, 'readiness_reason': 'Candidate evidence only: semantic review, protocol selection and model mapping are required.', 'source_manifest': manifest, 'candidate_protocols': protocols, 'claims': accepted, 'rejected_claims': rejected, 'excluded_claims': excluded, 'conflicts': conflicts, 'candidate_parameters': comparable, 'missing_required_parameters': missing, 'selected_parameters': [], 'optimization_domains': [], 'search_log': extraction.get('search_log', []), 'search_complete': extraction.get('search_complete', False), 'limitations': ['No automatic kinetic inference from endpoint purity.', 'Numeric/quote correctness and biological applicability need semantic review.', 'No optimal or safe operating range inferred from a reported setting.', 'Supplementary evidence is not retrieved by fetch_full_text.']}

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='command', required=True)
    s = sub.add_parser('search'); s.add_argument('query'); s.add_argument('--out', required=True); s.add_argument('--page-size', type=int, default=10)
    f = sub.add_parser('fetch'); f.add_argument('pmcid'); f.add_argument('--out', required=True)
    c = sub.add_parser('compile'); c.add_argument('--request', required=True); c.add_argument('--extraction', required=True); c.add_argument('--sources', nargs='+', required=True); c.add_argument('--out', required=True)
    args = ap.parse_args()
    read = lambda p: json.loads(Path(p).read_text())
    if args.command == 'search': result = search_literature(args.query, args.page_size)
    elif args.command == 'fetch': result = fetch_full_text(args.pmcid)
    else: result = compile_handoff(read(args.request), read(args.extraction), [read(p) for p in args.sources])
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(args.out)

if __name__ == '__main__':
    main()
