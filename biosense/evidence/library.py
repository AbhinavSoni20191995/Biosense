"""The cell production library: what the literature says about making cells, kept.

Every run used to start its literature search from nothing, and a run about one
cell type found nothing for a setpoint that a neighbouring protocol states
plainly (progenitor density, when the papers report embryoid bodies per well).
The library is the landscape a "Build the cell production library" run reads
once — expansion, aggregates and embryoid bodies, differentiation, maturation,
harvest, in static and suspension formats, across iPSC-derived lineages — and
every later run starts from.

What an entry is: one paper, its system (cells, format, vessel), the stages it
covers, a short summary, and the values it reports, each with the sentence
that states it. A value about a neighbouring quantity says so (`proxy_for`)
with the conversion in words, so "10 EBs per well in 3 mL" can inform a
progenitor-density setpoint without pretending to be one.

What it is not: evidence a run may cite without reading. A library value is a
lead with its quote; a run that relies on it re-reads the paragraph so its
claim quotes the source itself. The library saves the search, not the reading.

Stored on the server (private data root), built by runs, merged by paper id.

    python -m biosense.evidence.library template
    python -m biosense.evidence.library check --draft <file>
    python -m biosense.evidence.library search --terms "embryoid body" density --stage differentiation
    python -m biosense.evidence.library show
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .. import contracts as K

NAME = 'library.json'
DRAFT_GLOB = 'library*.draft.json'
REF_RE = re.compile(r'^(PMID:\d+|PMC\d+|DOI:10\.\S+|doi:10\.\S+|10\.\d{4,}/\S+)$')
STAGES = ('expansion', 'aggregation', 'differentiation', 'maturation', 'harvest',
          'cryopreservation', 'whole_process', 'other')
MAX_PAPERS = 2000


def path():
    from ..data import roots as DR
    return DR.private_root() / 'library' / NAME


def load():
    try:
        doc = K.read_json(path())
    except (OSError, ValueError):
        return {'papers': [], 'updated_at': None}
    return doc if isinstance(doc, dict) else {'papers': [], 'updated_at': None}


def _num(v, what):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise K.ContractError(f'{what} must be a number or null, got {v!r}')
    return float(v)


def check_paper(p):
    """A paper record, normalised, or a refusal saying what is wrong."""
    if not isinstance(p, dict):
        raise K.ContractError('each paper is an object')
    ref = str(p.get('paper_id') or '').strip()
    if not REF_RE.match(ref):
        raise K.ContractError(f'paper_id {ref!r} must be PMID:<n>, PMC<n> or a DOI')
    title = str(p.get('title') or '').strip()
    if len(title) < 8:
        raise K.ContractError(f'{ref}: give the paper\'s title')
    stages = [s for s in (p.get('stages') or []) if s in STAGES]
    values = []
    for i, v in enumerate(p.get('values') or []):
        if not isinstance(v, dict):
            raise K.ContractError(f'{ref} value {i + 1}: an object')
        quote = str(v.get('quote') or '').strip()
        if len(quote) < 15:
            raise K.ContractError(f'{ref} value {i + 1}: quote the sentence that states it')
        val, lo, hi = (_num(v.get('value'), 'value'), _num(v.get('low'), 'low'),
                       _num(v.get('high'), 'high'))
        if val is None and lo is None and hi is None:
            raise K.ContractError(f'{ref} value {i + 1}: a value, or a low/high range')
        if lo is not None and hi is not None and lo > hi:
            raise K.ContractError(f'{ref} value {i + 1}: low is above high')
        param = str(v.get('parameter') or '').strip()
        if not param:
            raise K.ContractError(f'{ref} value {i + 1}: name the quantity (parameter)')
        stage = v.get('stage') if v.get('stage') in STAGES else 'other'
        values.append({
            'parameter': param[:80], 'parameter_id': (v.get('parameter_id') or None),
            'stage': stage, 'value': val, 'low': lo, 'high': hi,
            'unit': str(v.get('unit') or '').strip()[:40] or None,
            'context': str(v.get('context') or '').strip()[:300] or None,
            'proxy_for': (v.get('proxy_for') or None),
            'conversion': str(v.get('conversion') or '').strip()[:300] or None,
            'quote': quote[:600], 'locator': str(v.get('locator') or '').strip()[:120] or None})
        if values[-1]['proxy_for'] and not values[-1]['conversion']:
            raise K.ContractError(f'{ref} value {i + 1}: a proxy says how it relates to '
                                  f'{values[-1]["proxy_for"]} (conversion, in words)')
    sysd = p.get('system') or {}
    return {
        'paper_id': ref, 'title': title[:300],
        'year': p.get('year') if isinstance(p.get('year'), int) else None,
        'system': {k: (str(sysd.get(k)).strip()[:120] if sysd.get(k) else None)
                   for k in ('cells', 'target', 'format', 'vessel')},
        'stages': stages, 'summary': str(p.get('summary') or '').strip()[:1200] or None,
        'values': values, 'full_text_read': bool(p.get('full_text_read')),
    }


def merge(draft, *, from_run=None, now=None):
    """Add a draft's papers. A paper already there is replaced by the newer reading.

    Returns {added, updated, refused: [reasons]}. One bad paper is refused with
    its reason; the rest are kept — a landscape is many independent records.
    """
    papers = (draft or {}).get('papers') or []
    lib = load()
    by_id = {p['paper_id']: p for p in lib.get('papers') or []}
    added, updated, refused = [], [], []
    for raw in papers:
        try:
            p = check_paper(raw)
        except K.ContractError as e:
            refused.append(str(e)[:200])
            continue
        p.update(from_run=from_run, added_at=now or K.now_iso(), status='unreviewed')
        (updated if p['paper_id'] in by_id else added).append(p['paper_id'])
        by_id[p['paper_id']] = p
    if added or updated:
        rows = sorted(by_id.values(), key=lambda p: p.get('added_at') or '')[-MAX_PAPERS:]
        out = {'schema_version': K.PRODUCTION_VERSION, 'updated_at': now or K.now_iso(),
               'note': 'Built by literature runs. Each value quotes its source; a run that '
                       'relies on one re-reads the paragraph before citing it.',
               'papers': rows}
        path().parent.mkdir(parents=True, exist_ok=True)
        K.write_json_atomic(path(), out)
    return {'added': added, 'updated': updated, 'refused': refused[:20]}


def merge_run(out_dir, *, from_run=None):
    """Every library draft a run wrote (one per literature agent), merged."""
    total = {'added': [], 'updated': [], 'refused': [], 'drafts': 0}
    for f in sorted(Path(out_dir).rglob(DRAFT_GLOB)):
        try:
            doc = K.read_json(f)
        except (OSError, ValueError) as e:
            total['refused'].append(f'{f.name}: unreadable ({type(e).__name__})')
            continue
        r = merge(doc, from_run=from_run)
        total['drafts'] += 1
        for k in ('added', 'updated', 'refused'):
            total[k] += r[k]
    return total


def _tokens(s):
    return {w for w in re.findall(r'[a-z0-9]+', (s or '').lower()) if len(w) > 2}


def search(terms, *, stage=None, limit=25, lib=None):
    """Values ranked by how many of the terms they mention, with their paper."""
    want = set()
    for t in terms or []:
        want |= _tokens(t)
    rows = []
    for p in (lib or load()).get('papers') or []:
        ptext = ' '.join([p['title'], p.get('summary') or '',
                          ' '.join(v for v in (p.get('system') or {}).values() if v)])
        for v in p.get('values') or []:
            if stage and v['stage'] != stage:
                continue
            hay = _tokens(' '.join(str(x or '') for x in (
                v['parameter'], v.get('parameter_id'), v.get('proxy_for'), v.get('context'),
                v.get('unit'), v['stage']))) | _tokens(ptext)
            score = len(want & hay) if want else 1
            if score:
                rows.append((score, p, v))
    rows.sort(key=lambda x: (-x[0], x[1]['paper_id']))
    return [dict(v, paper_id=p['paper_id'], title=p['title'], year=p.get('year'),
                 system=p.get('system'), status=p.get('status')) for _, p, v in rows[:limit]]


def stats(lib=None):
    lib = lib or load()
    papers = lib.get('papers') or []
    by_stage = {}
    for p in papers:
        for v in p.get('values') or []:
            by_stage[v['stage']] = by_stage.get(v['stage'], 0) + 1
    return {'papers': len(papers), 'values': sum(len(p.get('values') or []) for p in papers),
            'values_by_stage': by_stage, 'updated_at': lib.get('updated_at')}


def _fmt(v):
    if v.get('value') is not None:
        n = f'{v["value"]:g}'
    else:
        n = f'{v["low"]:g}' if v.get('low') is not None else '?'
        n += f'–{v["high"]:g}' if v.get('high') is not None else ''
    return n + (f' {v["unit"]}' if v.get('unit') else '')


def for_brief(project, *, limit=30):
    """The library values most relevant to a project, as lines for a run's brief."""
    lib = load()
    if not lib.get('papers'):
        return None, stats(lib)
    terms = []
    bs = (project.doc.get('biological_system') or {}) if project is not None else {}
    terms += [bs.get('starting_cell') or '', bs.get('target_cell') or '',
              bs.get('culture_format') or '']
    if project is not None:
        terms += [s.get('label') or s['stage_id'] for s in project.stages]
        terms += [project.parameter(p).label for p in project.parameter_ids]
    rows = search(terms, limit=limit, lib=lib)
    lines = []
    for r in rows:
        sysd = r.get('system') or {}
        where = ', '.join(x for x in (sysd.get('cells'), sysd.get('format'), sysd.get('vessel'))
                          if x)
        proxy = (f' [proxy for {r["proxy_for"]}: {r["conversion"]}]' if r.get('proxy_for')
                 else '')
        lines.append(f'- {r["parameter"]} = {_fmt(r)} ({r["stage"]}; {where or "context n/a"})'
                     f'{proxy} — {r["paper_id"]}: "{r["quote"][:180]}"')
    return '\n'.join(lines), stats(lib)


TEMPLATE = {
    'papers': [{
        'paper_id': 'PMC3741356', 'title': '<the paper title>', 'year': 2013,
        'system': {'cells': 'hPSC (HUES2, iPSC)', 'target': 'monocyte / macrophage',
                   'format': 'static, adherent EB culture', 'vessel': '6-well plate, 3 mL'},
        'stages': ['aggregation', 'differentiation', 'harvest'],
        'summary': '<two or three sentences: what the process is and what it yields>',
        'full_text_read': True,
        'values': [
            {'parameter': 'M-CSF concentration', 'parameter_id': 'mcsf_ng_ml',
             'stage': 'differentiation', 'value': 100, 'unit': 'ng/mL',
             'context': 'X-VIVO 15, with IL-3 25 ng/mL', 'quote': '<the sentence>',
             'locator': 'Methods, paragraph p0012'},
            {'parameter': 'EBs per well', 'stage': 'differentiation', 'value': 10,
             'unit': 'EBs per well (6-well, 3 mL)', 'proxy_for': 'seed_density',
             'conversion': 'about 10 EBs in 3 mL; cells per EB are not stated, so this '
                           'bounds density only once EB size is known',
             'quote': '<the sentence>', 'locator': 'Methods'},
        ]}],
}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('template', help='the shape of a library draft')
    p = sub.add_parser('check', help='check a draft before the run ends')
    p.add_argument('--draft', required=True)
    p = sub.add_parser('search', help='library values that mention the terms')
    p.add_argument('--terms', nargs='+', required=True)
    p.add_argument('--stage', choices=STAGES)
    p.add_argument('--limit', type=int, default=25)
    sub.add_parser('show', help='how big the library is')
    p = sub.add_parser('merge', help='add a draft to this server\'s library')
    p.add_argument('--draft', required=True)
    p.add_argument('--from-run')
    a = ap.parse_args(argv)
    if a.cmd == 'template':
        print(json.dumps(TEMPLATE, indent=2))
        return 0
    if a.cmd == 'show':
        print(json.dumps(dict(stats(), file=str(path())), indent=1))
        return 0
    if a.cmd == 'search':
        print(json.dumps(search(a.terms, stage=a.stage, limit=a.limit), indent=1))
        return 0
    try:
        doc = K.read_json(a.draft)
    except (OSError, ValueError) as e:
        print(json.dumps({'refused': True, 'reason': f'unreadable draft: {e}'}))
        return 1
    if a.cmd == 'check':
        ok, bad = [], []
        for raw in (doc or {}).get('papers') or []:
            try:
                ok.append(check_paper(raw)['paper_id'])
            except K.ContractError as e:
                bad.append(str(e))
        print(json.dumps({'ok': ok, 'refused': bad}, indent=1))
        return 0 if not bad else 1
    print(json.dumps(merge(doc, from_run=a.from_run), indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
