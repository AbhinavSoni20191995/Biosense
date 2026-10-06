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
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest'
# PubMed through NCBI E-utilities: a second index with its own ranking, and the
# way on when Europe PMC is unreachable. NCBI_API_KEY, when set, raises the
# rate limit from 3 to 10 requests a second; nothing else needs it.
EUTILS = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'
RETRIES = 3          # attempts per request: an index that hiccups once is not "no evidence"
_sleep = time.sleep  # replaced in tests
# Stage names are product-specific snake_case (STAGE_PATTERN). STAGES only lists
# those of the legacy cardiac example, kept for reference; nothing requires them.
STAGES = {'ipsc_expansion', 'cardiac_differentiation', 'purification', 'recovery'}
STAGE_PATTERN = re.compile(r'[a-z][a-z0-9_]{1,63}')
ROLES = {'operating_condition', 'schedule', 'initial_condition', 'kinetic_parameter', 'outcome', 'geometry',
         'genotype_effect'}
# Factors convert a value into its family's reference unit. No geometry guessing.
UNITS = {
 'h': ('time', 1), 'day': ('time', 24),
 '1/h': ('rate', 1), '1/day': ('rate', 1/24),
 'uM': ('molarity', 1), 'mM': ('molarity', 1000),
 'mL': ('volume', 1), 'L': ('volume', 1000),
 'cells/mL': ('volume_density', 1), 'cells/L': ('volume_density', 1/1000),
 'cells/cm2': ('area_density', 1),
 'g/L': ('mass_concentration', 1), 'mg/mL': ('mass_concentration', 1),
 'ug/mL': ('mass_concentration', 1e-3), 'ng/mL': ('mass_concentration', 1e-6),
 'cells': ('count', 1), 'cells/input_cell': ('yield_per_input_cell', 1),
 'EU/mL': ('endotoxin', 1), 'fold': ('dimensionless', 1),
 'IU/mL': ('activity_concentration', 1),
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
    """GET with retries on what is worth retrying: a dropped connection, a
    timeout, 429 and 5xx. A 4xx other than 429 is an answer and raised at once."""
    request = urllib.request.Request(url, headers={'User-Agent': 'BioSenseLiteraturePrototype/0.2', 'Accept': 'application/json,application/xml'})
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                data = response.read(20_000_001)
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == RETRIES - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == RETRIES - 1:
                raise
        _sleep(2 ** attempt)
    if len(data) > 20_000_000:
        raise ValueError('Response exceeds 20 MB limit')
    return data

def europepmc_query(query, open_access=False, since=None):
    """The query actually sent: the agent's own, plus the filters it asked for."""
    q = f'({query})'
    if open_access:
        q += ' AND OPEN_ACCESS:y'
    if since:
        q += f' AND PUB_YEAR:[{int(since)} TO 3000]'
    return q

SORTS = {'relevance': None, 'cited': 'CITED desc', 'recent': 'P_PDATE_D desc'}

def search_literature(query, page_size=10, cursor='*', open_access=False, since=None, sort='relevance'):
    """Query Europe PMC. Results locate evidence; abstracts are not full-text proof."""
    if not 1 <= page_size <= 25:
        raise ValueError('page_size must be 1..25')
    sent = europepmc_query(query, open_access, since)
    params = {'query': sent, 'format': 'json', 'resultType': 'core', 'pageSize': page_size, 'cursorMark': cursor}
    if SORTS[sort]:
        params['sort'] = SORTS[sort]
    data = json.loads(get_bytes(BASE + '/search?' + urllib.parse.urlencode(params)))
    papers = []
    for p in data.get('resultList', {}).get('result', []):
        papers.append({k: p.get(k) for k in ['id', 'source', 'pmid', 'pmcid', 'doi', 'title', 'authorString', 'journalTitle', 'pubYear', 'firstPublicationDate', 'citedByCount', 'isOpenAccess', 'pubTypeList', 'abstractText']})
    return {'index': 'europepmc', 'query': query, 'query_sent': sent, 'retrieved_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'hit_count': data.get('hitCount'), 'next_cursor': data.get('nextCursorMark'), 'papers': papers}

def _eutils(path, **params):
    params['tool'] = 'BioSenseLiterature'
    if os.environ.get('NCBI_API_KEY'):
        params['api_key'] = os.environ['NCBI_API_KEY']
    return get_bytes(f'{EUTILS}/{path}?' + urllib.parse.urlencode(params))

def parse_pubmed(xml_bytes):
    """PubmedArticleSet XML -> papers in the same shape as a Europe PMC search."""
    papers = []
    for art in ET.fromstring(xml_bytes).iter('PubmedArticle'):
        pmid = art.findtext('.//MedlineCitation/PMID')
        ids = {i.get('IdType'): (i.text or '').strip() for i in art.findall('.//PubmedData/ArticleIdList/ArticleId')}
        abstract = ' '.join(((a.get('Label') + ': ') if a.get('Label') else '') + normalize(''.join(a.itertext())) for a in art.findall('.//Abstract/AbstractText'))
        authors = [' '.join(x for x in (a.findtext('LastName'), a.findtext('Initials')) if x) for a in art.findall('.//AuthorList/Author')]
        year = art.findtext('.//JournalIssue/PubDate/Year') or (art.findtext('.//JournalIssue/PubDate/MedlineDate') or '')[:4] or None
        types = [normalize(t.text or '') for t in art.findall('.//PublicationTypeList/PublicationType')]
        pmcid = ids.get('pmc') or None
        papers.append({'id': pmid, 'source': 'MED', 'pmid': pmid, 'pmcid': pmcid, 'doi': ids.get('doi') or None,
                       'title': normalize(''.join(art.find('.//ArticleTitle').itertext())) if art.find('.//ArticleTitle') is not None else '',
                       'authorString': ', '.join(authors[:6]) + (' et al.' if len(authors) > 6 else ''),
                       'journalTitle': art.findtext('.//Journal/Title'), 'pubYear': year,
                       # PubMed does not say whether a PMC copy is open access; fetch tells.
                       'isOpenAccess': None, 'in_pmc': bool(pmcid), 'pubTypeList': {'pubType': types},
                       'abstractText': abstract or None})
    return papers

def search_pubmed(query, page_size=10, since=None, sort='relevance'):
    """Query PubMed (NCBI E-utilities): esearch for ids, efetch for the abstracts."""
    if not 1 <= page_size <= 25:
        raise ValueError('page_size must be 1..25')
    params = {'db': 'pubmed', 'term': query, 'retmode': 'json', 'retmax': page_size,
              'sort': 'pub_date' if sort == 'recent' else 'relevance'}
    if since:
        params.update(datetype='pdat', mindate=str(int(since)), maxdate='3000')
    found = json.loads(_eutils('esearch.fcgi', **params)).get('esearchresult', {})
    ids = found.get('idlist') or []
    papers = parse_pubmed(_eutils('efetch.fcgi', db='pubmed', id=','.join(ids), retmode='xml', rettype='abstract')) if ids else []
    return {'index': 'pubmed', 'query': query, 'query_sent': params['term'], 'retrieved_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'hit_count': int(found.get('count') or 0), 'next_cursor': None, 'papers': papers}

def abstract_source(paper):
    """A search hit's abstract as a source document compile can check quotes
    against. Marked `abstract` so nobody reads it as Methods-level evidence."""
    pid = paper.get('pmcid') or ('PMID' + str(paper.get('pmid') or paper.get('id')))
    return {'id': pid + '-abstract', 'url': ('https://europepmc.org/abstract/MED/' + str(paper['pmid'])) if paper.get('pmid') else None,
            'title': paper.get('title') or '', 'retrieved_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'sha256': hashlib.sha256((paper.get('abstractText') or '').encode()).hexdigest(),
            'article_type': 'abstract', 'license': [], 'supplements_retrieved': False, 'supplement_count': 0,
            'paragraphs': [{'id': 'p0000', 'section': 'abstract', 'text': normalize(paper.get('abstractText') or '')}]}

def find_paragraphs(source, terms, limit=40):
    """Paragraphs mentioning any of the terms (case-insensitive), for reading
    Methods and tables without loading a whole article into the conversation."""
    wanted = [t.lower() for t in terms if t.strip()]
    hits = [p for p in source.get('paragraphs', []) if any(t in p['text'].lower() for t in wanted)]
    return [{'id': p['id'], 'section': p.get('section'), 'text': p['text'][:700]} for p in hits[:limit]]

def summary(result):
    """What the command prints: enough to choose papers without opening the file."""
    return [{'id': p.get('id'), 'pmcid': p.get('pmcid'), 'year': p.get('pubYear') or (p.get('firstPublicationDate') or '')[:4] or None,
             'open_access': p.get('isOpenAccess'), 'cited_by': p.get('citedByCount'), 'title': (p.get('title') or '')[:160]}
            for p in result.get('papers', [])]

def fetch_full_text(pmcid):
    """Fetch open-access JATS XML: Europe PMC first, then NCBI's PMC when it errors.

    Both archives carry the open-access subset; either can be down, or answer
    500 for one article, and a paper lost to one outage is a claim nobody gets
    to cite. The source records which archive answered."""
    if not re.fullmatch(r'PMC[0-9]+', pmcid):
        raise ValueError('Expected a PMCID such as PMC7076930')
    try:
        raw, archive = get_bytes(BASE + '/' + pmcid + '/fullTextXML'), 'europepmc'
    except (urllib.error.URLError, TimeoutError, ConnectionError) as first:
        try:
            raw, archive = _eutils('efetch.fcgi', db='pmc', id=pmcid, retmode='xml'), 'ncbi_pmc'
        except (urllib.error.URLError, TimeoutError, ConnectionError) as second:
            raise ValueError(f'no archive returned {pmcid}: Europe PMC {first}; NCBI PMC {second}')
    root = ET.fromstring(raw)
    article = root if root.tag == 'article' else root.find('.//article')
    if article is None:
        raise ValueError(f'{archive} returned no article for {pmcid}')
    article_type = article.attrib.get('article-type', 'unknown')
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
    body = article.find('body')
    if body is None:
        raise ValueError(f'No article body available from {archive}; cannot verify full-text evidence')
    visit(body, 'body')
    title = article.find('.//article-title')
    licenses = [normalize(''.join(n.itertext())) for n in article.findall('.//license')]
    supplements = [dict(n.attrib) for n in article.findall('.//supplementary-material')]
    return {'id': pmcid, 'url': 'https://europepmc.org/articles/' + pmcid, 'archive': archive, 'title': normalize(''.join(title.itertext())) if title is not None else '', 'retrieved_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'sha256': hashlib.sha256(raw).hexdigest(), 'article_type': article_type, 'license': licenses, 'supplements_retrieved': False, 'supplement_count': len(supplements), 'paragraphs': paragraphs}

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
        if not isinstance(c['stage'], str) or not STAGE_PATTERN.fullmatch(c['stage']) or c['role'] not in ROLES:
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
        elif c['role'] == 'genotype_effect' and not ctx.get('genotype'):
            reasons.append('genotype_effect claims need context.genotype (e.g. "GENE knockout" vs "wild_type")')
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
    # A ProductionRequest 2.0 carries the cell context under 'product'.
    constraints = dict(request.get('constraints', {}))
    for k in ['species', 'cell_origin', 'target_cell', 'cell_line', 'culture_format']:
        if k not in constraints and k in request.get('product', {}):
            constraints[k] = request['product'][k]
    for c in accepted:
        reasons = []
        ctx = c['context']
        for k in ['species', 'cell_origin', 'target_cell', 'cell_line', 'culture_format']:
            wanted = constraints.get(k)
            if wanted is not None and wanted != ctx[k]:
                reasons.append(f'{k}: source {ctx[k]!r} differs from requested {wanted!r}')
        limit = constraints.get('parameter_limits', {}).get(c['stage'] + ':' + c['parameter'])
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

NEXT_STEP = {
    'search': 'Europe PMC did not answer. Run the same query with `pubmed`, or retry later; '
              'record the failure in search_log rather than reporting no evidence.',
    'pubmed': 'PubMed did not answer. Run the same query with `search` (Europe PMC), or retry later.',
    'fetch': 'Neither Europe PMC nor NCBI PMC returned open-access full text. Use the '
             'paper\'s abstract (`abstract`), mark claims from it abstract-level, or choose '
             'another open-access paper.',
}

def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='command', required=True)
    for name in ('search', 'pubmed'):
        s = sub.add_parser(name); s.add_argument('query'); s.add_argument('--out', required=True); s.add_argument('--page-size', type=int, default=10)
        s.add_argument('--since', type=int, help='publication year from'); s.add_argument('--sort', choices=sorted(SORTS), default='relevance')
        if name == 'search': s.add_argument('--open-access', action='store_true', help='only papers whose full text can be fetched')
    f = sub.add_parser('fetch'); f.add_argument('pmcid'); f.add_argument('--out', required=True); f.add_argument('--find', action='append', default=[], help='print paragraphs containing this text (repeatable)')
    a = sub.add_parser('abstract'); a.add_argument('--search-file', required=True, help='a saved search/pubmed result'); a.add_argument('--id', required=True, help='its id, PMID or PMCID'); a.add_argument('--out', required=True)
    c = sub.add_parser('compile'); c.add_argument('--request', required=True); c.add_argument('--extraction', required=True); c.add_argument('--sources', nargs='+', required=True); c.add_argument('--out', required=True)
    args = ap.parse_args(argv)
    read = lambda p: json.loads(Path(p).read_text())
    shown = {}
    try:
        if args.command == 'search':
            result = search_literature(args.query, args.page_size, open_access=args.open_access, since=args.since, sort=args.sort)
        elif args.command == 'pubmed':
            result = search_pubmed(args.query, args.page_size, since=args.since, sort=args.sort)
        elif args.command == 'fetch':
            result = fetch_full_text(args.pmcid)
            if args.find:
                shown['matches'] = find_paragraphs(result, args.find)
        elif args.command == 'abstract':
            papers = read(args.search_file).get('papers', [])
            paper = next((p for p in papers if args.id in (p.get('id'), p.get('pmid'), p.get('pmcid'))), None)
            if not paper or not paper.get('abstractText'):
                raise ValueError(f'{args.id} has no abstract in {args.search_file}')
            result = abstract_source(paper)
        else:
            result = compile_handoff(read(args.request), read(args.extraction), [read(p) for p in args.sources])
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, ET.ParseError) as e:
        # One readable refusal, not a traceback: the orchestrator and the run
        # page show `reason`, and `next` says how to carry on.
        print(json.dumps({'refused': True, 'command': args.command, 'error': type(e).__name__,
                          'reason': f'{args.command}: {str(e)[:300]}', 'next': NEXT_STEP.get(args.command)}))
        return 1
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    if args.command in ('search', 'pubmed'):
        shown.update(index=result['index'], hit_count=result['hit_count'], papers=summary(result))
    elif args.command == 'fetch':
        shown.update(paragraphs=len(result['paragraphs']), title=result['title'])
    print(json.dumps({'out': args.out, **shown}, ensure_ascii=False, indent=1))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
