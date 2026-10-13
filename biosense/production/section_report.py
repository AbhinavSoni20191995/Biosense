"""What the literature and bioinformatics specialists found, as a document.

The run page shows what each specialist read and computed, and none of it
left the page: a scientist who wanted the papers and the claims for a lab
meeting, or the analyses for a collaborator, copied them out by hand. This
writes either section as a Word document or a PDF, from the files the run
wrote — so it works for any run that has ended, after a restart as well.

Two things it keeps from the page:

* **what each thing is.** The literature agent's running notes are leads it
  wrote as it read, before the orchestrator weighed them; the evidence rows
  are what the hypotheses rest on, each with its stance, strength and how far
  its context is from this process. An analysis result is what a tool
  computed; an interpretation is capped by the result, the species and how
  close the experiment is. The document says which is which.
* **nothing new.** It renders what was written and computes nothing. A file
  that does not parse is left out and said to be, never repaired into prose.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import contracts as K
from .docx import Document

LIT_NOTE = ('What the literature agent read and found. Its notes are leads written as it read, '
            'before the orchestrator weighed them; the evidence rows are what the hypotheses '
            'rest on. Another lab\'s result is evidence to weigh for this process, never a '
            'measurement of it.')
BIO_NOTE = ('What the bioinformatics specialists looked up and computed. A result is what a tool '
            'computed on a registered dataset; an interpretation\'s confidence is capped by the '
            'result, the species and route, and how close the experiment is to this process. '
            'Nothing here is a measurement of this process.')
MAX_PAPERS = 150


def _read(path):
    try:
        return K.read_json(path)
    except (OSError, ValueError):
        return None


def _text(path):
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None


def _plain(s):
    """Markdown emphasis and code marks off; the words stay."""
    s = re.sub(r'`([^`]*)`', r'\1', str(s or ''))
    s = re.sub(r'\*\*([^*]+)\*\*', r'\1', s)
    s = re.sub(r'(?<!\w)\*([^*]+)\*(?!\w)', r'\1', s)
    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', s)
    return s.strip()


def markdown(doc, text, *, level=2):
    """A specialist's notes, as headings, paragraphs, bullets and tables."""
    para, table = [], []

    def flush_para():
        if para:
            doc.para(_plain(' '.join(para)))
            para.clear()

    def flush_table():
        rows = [r for r in table if not re.match(r'^\s*\|?\s*:?-{3,}', r)]
        cells = [[_plain(c) for c in r.strip().strip('|').split('|')] for r in rows]
        if cells:
            width = max(len(r) for r in cells)
            head, body = cells[0] + [''] * (width - len(cells[0])), cells[1:]
            doc.table(head, [r + [''] * (width - len(r)) for r in body])
        table.clear()

    for raw in (text or '').splitlines():
        line = raw.rstrip()
        if line.strip().startswith('|'):
            flush_para()
            table.append(line)
            continue
        if table:
            flush_table()
        h = re.match(r'^(#{1,4})\s+(.*)$', line)
        b = re.match(r'^\s*(?:[-*•]|\d+[.)])\s+(.*)$', line)
        if h:
            flush_para()
            doc.heading(_plain(h.group(2)), min(3, level + len(h.group(1)) - 1))
        elif b:
            flush_para()
            doc.bullet(_plain(b.group(1)))
        elif not line.strip() or re.match(r'^-{3,}$', line.strip()):
            flush_para()
        else:
            para.append(line.strip())
    flush_para()
    if table:
        flush_table()


def _header(doc, kind, run_dir, *, title, run_id, note):
    req = _read(Path(run_dir) / 'discovery_request.json') or {}
    heading = f'{kind} — {title or req.get("title") or req.get("objective") or "this run"}'
    doc.title_block(heading, req.get('objective'))
    doc.table(['', ''], [[k, v] for k, v in (
        ('Run', run_id or Path(run_dir).name), ('Project', req.get('project_id')),
        ('Written', K.now_iso()[:10])) if v], widths=[1, 3])
    doc.caveat(note)


def _stage_dirs(base):
    try:
        return sorted(d for d in Path(base).iterdir()
                      if d.is_dir() and d.name not in ('cache', 'scratch'))
    except OSError:
        return []


# ── literature ─────────────────────────────────────────────────────────
def papers(run_dir):
    """Every paper the literature agents found, once each, with who found it."""
    lit = Path(run_dir) / 'literature'
    sources = [lit / 'merged' / 'discover.json', lit / 'discover.json'] + [
        d / 'discover.json' for d in _stage_dirs(lit) if d.name != 'merged']
    seen, out = {}, []
    for path in sources:
        doc = _read(path)
        for p in (doc or {}).get('papers') or []:
            key = p.get('pmcid') or p.get('pmid') or p.get('doi') or p.get('title')
            if not key:
                continue
            row = seen.get(key)
            if row is None:
                row = seen[key] = {
                    'title': (p.get('title') or '').strip(), 'year': p.get('pubYear'),
                    'id': p.get('pmcid') or (f'PMID:{p["pmid"]}' if p.get('pmid') else
                                             p.get('doi') or ''),
                    'open_access': p.get('isOpenAccess'), 'read': bool(p.get('source_file')),
                    'found_by': set(p.get('found_by') or [])}
                out.append(row)
            else:
                row['read'] = row['read'] or bool(p.get('source_file'))
                row['found_by'].update(p.get('found_by') or [])
            if path.parent.name not in ('literature', 'merged'):
                row['found_by'].add(path.parent.name)
    return out


def hypotheses(run_dir):
    out = []
    for path in sorted(Path(run_dir).glob('quantified_hypothesis*.json')):
        h = _read(path)
        if isinstance(h, dict) and h.get('statement'):
            out.append(h)
    return out


def literature(run_dir, *, doc=None, title=None, run_id=None):
    doc = doc if doc is not None else Document(title='Literature')
    _header(doc, 'Literature', run_dir, title=title, run_id=run_id, note=LIT_NOTE)

    rows = papers(run_dir)
    doc.heading('Papers found', 1)
    if not rows:
        doc.para('The literature agent wrote no list of papers for this run.')
    else:
        read = sum(1 for r in rows if r['read'])
        doc.para(f'{len(rows)} paper(s) found; {read} read in full. Sorted as the agents ranked '
                 f'them.' + (f' The first {MAX_PAPERS} are listed.' if len(rows) > MAX_PAPERS
                             else ''))
        doc.table(['Paper', 'Year', 'Id', 'Read in full', 'Found by'],
                  [[r['title'] or '—', str(r['year'] or '—'), r['id'] or '—',
                    'yes' if r['read'] else ('open access' if r['open_access'] == 'Y'
                                             else 'no'),
                    ', '.join(sorted(r['found_by'])) or '—'] for r in rows[:MAX_PAPERS]],
                  widths=[6, 1, 2, 1.5, 2])

    hyps = hypotheses(run_dir)
    if hyps:
        doc.heading('The evidence the hypotheses rest on', 1)
        doc.para('Each row is a source the orchestrator weighed for a hypothesis: whether it '
                 'supports or contradicts it, how strongly, and how far its context is from '
                 'this process.')
        for h in hyps:
            doc.heading(f'{h.get("hypothesis_id", "")} — {_plain(h.get("statement"))}', 2)
            ev = [e for e in h.get('evidence') or [] if isinstance(e, dict)]
            if not ev:
                doc.para('No evidence row was recorded for this hypothesis.')
                continue
            doc.table(['Source', 'Kind', 'Stance', 'Strength', 'What it shows', 'Bearing here'],
                      [[str(e.get('ref') or '—'), str(e.get('evidence_class') or '—'),
                        str(e.get('stance') or '—'), str(e.get('strength') or '—'),
                        _plain(e.get('summary')) or '—',
                        _plain(e.get('bearing') or e.get('context_mismatch_note')
                               or e.get('relevance')) or '—'] for e in ev],
                      widths=[2, 1.4, 1.2, 1.2, 5, 3])

    lit = Path(run_dir) / 'literature'
    notes = [('', lit / 'insights.md')] + [
        (d.name, d / 'insights.md') for d in _stage_dirs(lit)]
    notes = [(n, t) for n, p in notes if (t := _text(p))]
    if notes:
        doc.heading('Notes as it read', 1)
        doc.caveat('Written by the literature agent while it read, before anything was weighed. '
                   'Leads, not findings.')
        for name, text in notes:
            if name:
                doc.heading(f'Stage: {name}', 2)
            markdown(doc, text, level=3 if name else 2)
    if not (rows or hyps or notes):
        doc.para('This run wrote nothing under literature/ yet.')
    return doc


# ── bioinformatics ─────────────────────────────────────────────────────
def _analysis_files(run_dir):
    base = Path(run_dir)
    bases = [base, base / 'analyst', base / 'bioinformatics'] + _stage_dirs(base / 'bioinformatics')
    found = {'plans': [], 'results': [], 'interps': [], 'agent': [], 'skipped': []}
    for b in bases:
        if not b.is_dir():
            continue
        for kind, pattern, schema in (('plans', 'analysis_plan*.json', 'analysis_plan'),
                                      ('results', 'analysis_result*.json', 'analysis_result'),
                                      ('interps', 'interpretation*.json',
                                       'analysis_interpretation'),
                                      ('agent', '*/agent_analysis.json', 'agent_analysis')):
            for f in sorted(b.glob(pattern)):
                doc = _read(f)
                if isinstance(doc, dict) and not K.schema_errors(schema, doc):
                    found[kind].append(dict(doc, _file=f.name))
                else:
                    found['skipped'].append(str(f.relative_to(base)))
    return found


def _interprets(it, r):
    """Whether interpretation *it* is of result *r*: the analyst writes the result's
    path as its reference, a script writes the analysis id."""
    ref = str(it.get('analysis_ref') or '')
    return bool(ref) and (ref == str(r.get('analysis_id') or '')
                          or Path(ref).name == r.get('_file'))


def _finding(f):
    if isinstance(f, dict):
        return ' — '.join(x for x in (_plain(f.get('finding')), _plain(f.get('basis'))) if x)
    return _plain(f)


def bioinformatics(run_dir, *, doc=None, title=None, run_id=None):
    doc = doc if doc is not None else Document(title='Bioinformatics')
    _header(doc, 'Bioinformatics', run_dir, title=title, run_id=run_id, note=BIO_NOTE)
    a = _analysis_files(run_dir)
    used_results, used_interps = set(), set()

    doc.heading('Analyses', 1)
    if not (a['plans'] or a['results'] or a['agent']):
        doc.para('No analysis was planned or run in this run.')
    for p in a['plans']:
        pid = p.get('plan_id') or ''
        doc.heading(f'{pid} — {_plain(p.get("question"))}', 2)
        tool = p.get('tool') or {}
        for label, value in (
                ('Datasets', ', '.join(p.get('dataset_ids') or [])),
                ('Tool', f'{tool.get("name") if isinstance(tool, dict) else tool} '
                         f'({p.get("analysis_type")})'),
                ('Why it was asked', p.get('why_requested')),
                ('The uncertainty it targets', (p.get('uncertainty_ref') or {}).get('statement')),
                ('What it could change', p.get('decision_relevance'))):
            if value:
                doc.bullet(f'{label}: {_plain(value)}')
        for i, r in enumerate(a['results']):
            if str(r.get('plan_ref') or '') == pid:
                used_results.add(i)
                _result(doc, r)
                for j, it in enumerate(a['interps']):
                    if j not in used_interps and _interprets(it, r):
                        used_interps.add(j)
                        _interpretation(doc, it)
    rest = [r for i, r in enumerate(a['results']) if i not in used_results]
    for r in rest:
        doc.heading(f'{r.get("analysis_id") or "Result"} — {_plain(r.get("question"))}', 2)
        _result(doc, r)
        for j, it in enumerate(a['interps']):
            if j not in used_interps and _interprets(it, r):
                used_interps.add(j)
                _interpretation(doc, it)
    for j, it in enumerate(a['interps']):
        if j not in used_interps:
            doc.heading(f'Interpretation of {it.get("analysis_ref") or "an analysis"}', 2)
            _interpretation(doc, it)

    if a['agent']:
        doc.heading('Analyses an agent wrote itself', 1)
        doc.caveat('A script the analyst wrote and ran. Its numbers come from the script\'s own '
                   'output file; nothing was retyped.')
        for x in a['agent']:
            doc.heading(_plain(x.get('question')) or (x.get('analysis_id') or 'Analysis'), 2)
            if x.get('method'):
                doc.bullet(f'Method: {_plain(x["method"])}')
            for f in x.get('findings') or []:
                doc.bullet(_plain(f))
            if not x.get('succeeded'):
                doc.caveat(f'Did not succeed: {_plain(x.get("problem")) or "no reason given"}')

    _datasets(doc, a['plans'])
    _paper_datasets(doc, run_dir)
    _genes(doc, run_dir)

    bio = Path(run_dir) / 'bioinformatics'
    notes = [('', bio / 'insights.md')] + [(d.name, d / 'insights.md') for d in _stage_dirs(bio)]
    notes = [(n, t) for n, p in notes if (t := _text(p))]
    if notes:
        doc.heading('Notes as it worked', 1)
        doc.caveat('Written by the bioinformatics agents while they worked. Leads, not findings.')
        for name, text in notes:
            if name:
                doc.heading(f'Specialist: {name}', 2)
            markdown(doc, text, level=3 if name else 2)
    if a['skipped']:
        doc.caveat('Left out because they do not pass their contract: '
                   + ', '.join(a['skipped'][:12]))
    return doc


def _result(doc, r):
    doc.para(runs=[('Result. ', {'bold': True}),
                   (f'Confidence {r.get("confidence") or "not stated"}; from '
                    f'{r.get("source_evidence_class") or "an unstated source"}.', {})])
    for f in (r.get('key_findings') or [])[:12]:
        doc.bullet(_finding(f))
    for lim in (r.get('limitations') or [])[:6]:
        doc.caveat(_plain(lim if isinstance(lim, str) else str(lim)))


def _interpretation(doc, it):
    doc.para(runs=[('What it means here. ', {'bold': True}),
                   (_plain(it.get('meaning_for_process')) or '—', {})])
    if it.get('what_it_shows'):
        doc.bullet(f'What it shows: {_plain(it["what_it_shows"])}')
    conf = it.get('confidence')
    if conf:
        capped = it.get('confidence_capped_by')
        doc.bullet(f'Confidence {conf}' + (f' (claimed {it["confidence_claimed"]}, capped by '
                                            f'{capped})' if capped else '')
                   + (f': {_plain(it["confidence_reason"])}' if it.get('confidence_reason')
                      else ''))
    if it.get('recommendation'):
        doc.bullet(f'Recommendation: {_plain(it["recommendation"])}')
    if it.get('confirm_with'):
        doc.bullet(f'Confirm with: {_plain(it["confirm_with"])}')
    for c in it.get('caveats') or []:
        doc.caveat(_plain(c))


def _datasets(doc, plans):
    ids = []
    for p in plans:
        ids += [d for d in p.get('dataset_ids') or [] if d not in ids]
    if not ids:
        return
    from ..data import registry as DREG
    rows = []
    for did in ids:
        try:
            m = DREG.require(did)
        except Exception:  # noqa: BLE001 - a dataset this server no longer holds is said
            rows.append([did, '—', '—', '—', 'not registered on this server any more'])
            continue
        rows.append([did, str(m.get('accession') or '—'), str(m.get('organism') or '—'),
                     str(m.get('evidence_class') or m.get('visibility') or '—'),
                     '; '.join(_plain(x) for x in (m.get('limitations') or [])[:2]) or '—'])
    doc.heading('Datasets analysed', 1)
    doc.table(['Dataset', 'Accession', 'Organism', 'Kind', 'Limitations'], rows,
              widths=[2.2, 1.4, 1.6, 2.6, 4.4])


def _paper_datasets(doc, run_dir):
    pd = _read(Path(run_dir) / 'bioinformatics' / 'paper_datasets.json')
    rows = [[p.get('paper', ''), d.get('accession', ''), _plain(d.get('title')) or '—',
             '; '.join(d.get('via') or [])]
            for p in (pd or {}).get('papers') or [] for d in p.get('datasets') or []]
    if rows:
        doc.heading('Datasets found from the papers', 1)
        doc.table(['Paper', 'Accession', 'Title', 'How it was found'], rows,
                  widths=[1.5, 1.5, 5, 3])


def _genes(doc, run_dir):
    gi = _read(Path(run_dir) / 'bioinformatics' / 'gene_info.json')
    genes = [g for g in (gi or {}).get('genes') or [] if isinstance(g, dict)]
    if not genes:
        return
    doc.heading('Genes looked up', 1)
    doc.caveat('Public annotation (Ensembl, UniProt, STRING): what each gene is, citable by its '
               'source, never a direction of effect for this process.')
    doc.table(['Gene', 'Ensembl', 'What it is', 'Interaction partners'],
              [[g.get('symbol', ''), g.get('ensembl_id') or '—',
                _plain(g.get('function') or g.get('description')) or '—',
                ', '.join(g.get('interaction_partners') or []) or '—'] for g in genes],
              widths=[1.2, 2.9, 5, 3])


# ── beyond the apparatus ───────────────────────────────────────────────
GAP_FILL = 'F6D4CF'


def breakthrough(run_dir, *, doc=None, title=None, run_id=None):
    """The experiment past the apparatus, laid out to be read beside round 1."""
    from ..evidence import breakthrough as BT
    doc = doc or Document(title='Beyond the apparatus')
    bt = BT.load(run_dir)
    _header(doc, 'Beyond the apparatus', run_dir, title=title, run_id=run_id,
            note=BT.NOTE)
    if not bt:
        doc.para('This run has no breakthrough design yet. Ask the orchestrator for one from '
                 'the run page ("Design past the apparatus").')
        return doc
    doc.caveat('CONCEPT — not runnable on the current apparatus and not approved. It becomes '
               'a protocol only when one is written for the new apparatus and a named person '
               'approves it.')
    doc.heading(bt['title'], 1)
    for label, key in (('Aim', 'aim'), ('Why this, now', 'why_now'),
                       ('Why it gets closer', 'closest_to_aim'),
                       ('What it still leaves', 'gap_to_aim')):
        doc.para(f'{label}. {bt[key]}')
    doc.heading('The constraints it lifts', 2)
    doc.table(['Constraint', 'Now', 'Lifted to', 'Why it stands in the way', 'Basis'],
              [[c['constraint'], c['now'], c['lifted_to'], c['why_it_limits'],
                c['evidence_basis'] + (f' ({", ".join(c["refs"])})' if c['refs'] else '')]
               for c in bt['constraints_lifted']], widths=[2, 2, 2.4, 3, 1.6])
    doc.heading('What it keeps', 2)
    for k in bt['kept']:
        doc.bullet(f'{k["what"]} — {k["why"]}')
    a = bt['apparatus']
    doc.heading('The apparatus', 2)
    doc.para(f'{a["name"]} ({a["exists"].replace("_", " ")}). {a["description"]}')
    for f in a['features']:
        doc.bullet(f)
    doc.heading('The stages', 2)
    rows, fills = [], []
    for st in bt['stages']:
        lines = [(BT._cond(c), c['provenance'] == 'gap') for c in st['conditions']] \
            or [('no factor set', False)]
        rows.append([f'Day {st["day_start"]:g}–{st["day_end"]:g}', st['label'], lines])
        fills.append([None, None, GAP_FILL if any(c['provenance'] == 'gap'
                                                  for c in st['conditions']) else None])
    doc.table(['Days', 'Stage', 'Conditions (provenance; refs)'], rows, widths=[1.3, 2.4, 6.3],
              fills=fills)
    doc.heading('The arms', 2)
    doc.table(['Arm', 'What it is', 'What differs', 'Basis'],
              [[x['arm_id'] + (' (control)' if x['control'] else ''), x['label'], x['differs'],
                x.get('basis') or '—'] for x in bt['arms']], widths=[1.4, 3, 3, 2.6])
    doc.para('Measure: ' + '; '.join(
        r['name'] + (f' ({r["unit"]})' if r.get('unit') else '')
        + (f' — {r["when"]}' if r.get('when') else '') for r in bt['readouts'])
        + (f'. {bt["replicates"]} replicate(s) per arm.' if bt.get('replicates') else '.'))
    doc.para(f'If {bt["decision_rule"]["if"]}, then {bt["decision_rule"]["then"]}.')
    x = bt.get('expected')
    if x:
        doc.heading('What it might do (a judgement, not a prediction)', 2)
        doc.para(f'{x["metric"]}: {x["interval"]["lower"]:g} to {x["interval"]["upper"]:g} '
                 f'{x["change_unit"]} over the control, {x["judgement"]["confidence"]} '
                 f'confidence. {x["judgement"]["rationale"]}'
                 + (f' ({", ".join(x["evidence_refs"])})' if x['evidence_refs'] else ''))
    f = bt['feasibility']
    doc.heading('What it takes', 2)
    doc.para(f'{f["cost"]}. Time: {f["time"]}.'
             + (f' Expertise: {f["expertise"]}.' if f.get('expertise') else ''))
    for step in f['build_steps']:
        doc.step(step)
    doc.heading('Risks', 2)
    for r in bt['risks']:
        doc.bullet(f'{r["risk"]} — {r["mitigation"]}')
    doc.para(f'Design confidence: {bt["confidence"]} ({bt["evidence_basis"]}'
             + (f'; {", ".join(bt["refs"])}' if bt['refs'] else '') + ').')
    doc.caveat(bt['does_not_change'])
    return doc


SECTIONS = {'literature': literature, 'bioinformatics': bioinformatics,
            'breakthrough': breakthrough}


TITLES = {'literature': 'Literature', 'bioinformatics': 'Bioinformatics',
          'breakthrough': 'Beyond the apparatus'}


def tobytes(section, run_dir, fmt='docx', **kw):
    """The section as a Word file or a PDF."""
    if section not in SECTIONS:
        raise K.ContractError(f'section must be one of {", ".join(SECTIONS)}; got {section!r}')
    if fmt == 'pdf':
        from . import pdf as PDF
        doc = PDF.Document(title=TITLES[section],
                           footer=f'{TITLES[section]} — run {kw.get("run_id") or ""}'.strip())
    else:
        doc = Document(title=TITLES[section])
    return SECTIONS[section](run_dir, doc=doc, **kw).tobytes()
