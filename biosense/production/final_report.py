"""The whole run as one document somebody outside the loop can read.

A finished run writes its record in pieces, each exact and none of them a
report: the protocol as a bench copy, the round on a landscape page, the
specialists' sections, the narrative the communication agent wrote. The person
taking the result to a lab meeting, a collaborator or a sponsor was assembling
those by hand. This composes them — cover, executive summary, the process
stage by stage as it looks at the bench, the round, then the full protocol and
each specialist's section as chapters — into one Word document or PDF, in the
order a reader reads rather than the order the run wrote.

Two rules keep it honest:

* **nothing new.** Every section renders what the run wrote, with the same
  blocks the standalone exports use; this module computes layout and nothing
  else. A narrative is included only when the communication agent wrote one
  that passed its contract (every number in it already stands in the
  protocol), it is labelled as prose, and every value still prints from the
  protocol's own rows beside it.
* **nothing approved.** The report says PROPOSED — NOT APPROVED where the
  bench copy says it, and the approval block at the end is empty. A prettier
  layout changes no provenance letter.

`flow_svg` is the same process as a picture — the stages to scale, what goes
into each, the gaps shaded as gaps — for the run page and anything that can
show an image, which the Word and PDF writers deliberately cannot.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from ..evidence import breakthrough as BT
from ..evidence import communication as CM
from . import first_pass as FP
from . import lab_protocol as LP
from . import section_report as SR
from . import visual_protocol as VP
from .docx import Document

NOT_APPROVED = VP.NOT_APPROVED
HOW_TO_READ = ('How to read this report: every value carries a letter saying what stands '
               'behind it — R reported (equals a cited claim), A adapted (transferred with a '
               'rationale), D design choice (no citation; a person\'s to approve), '
               'GAP (no value; blocks the wet lab), NEW (a lever this project does not carry '
               'yet), PUB (the published route, not carried by this protocol). Plain-language '
               'passages are the communication agent\'s words for a reader outside the loop; '
               'every number in them also stands in the validated protocol, and the tables '
               'beside them are the protocol\'s own.')


def _v(x):
    if x is None or x == '':
        return '—'
    if isinstance(x, float):
        return f'{x:g}'
    return str(x)


def _days(s):
    start, days = s.get('day_start') or 0, s.get('days')
    if days is None:
        return 'days not set'
    return f'day {_v(start)}–{_v(start + days)} ({_v(days)} d)'


def _item_line(i):
    """'M-CSF 100 ng/mL [A] (was 50)' from a first-pass item."""
    if i.get('value') is None and i.get('provenance') == 'gap':
        return (f'{i.get("label", "")}: GAP — no value established; decide before the round',
                True)
    if i.get('value') is None and i.get('from_baseline'):
        return (f'{i.get("label", "")}: no dose published [PUB]', False)
    unit = f' {i["unit"]}' if i.get('unit') else ''
    was = f' (was {_v(i["was"])})' if i.get('was') is not None else ''
    return (f'{i.get("label", "")} {_v(i.get("value"))}{unit} [{i.get("letter", "?")}]{was}',
            i.get('was') is not None)


def _executive(doc, p, fp, narrative):
    doc.heading('Executive summary', 1)
    if narrative:
        if narrative.get('audience'):
            doc.caveat(f'Written for {narrative["audience"]}.')
        for para in narrative.get('executive_summary') or []:
            doc.para(para)
        return
    # No narrative written: the summary is assembled, not phrased.
    if p.get('objective'):
        doc.para(f'The question: {p["objective"]}')
    ledger = p.get('hypothesis_ledger') or []
    adopted = [r for r in ledger if r.get('adopted')]
    plan = p.get('round_plan') or {}
    arms, reps = plan.get('arms') or [], plan.get('replicates')
    total = (p.get('timeline') or {}).get('total_days')
    facts = [f'{len(ledger)} hypothesis(es) weighed, {len(adopted)} adopted',
             f'{fp.get("changes", 0)} change(s) from the current process',
             f'{len(arms)} arm(s) × {_v(reps)} replicate(s)' if arms else None,
             f'{_v(total)} days in all' if total else None,
             f'{len(p.get("gaps") or [])} value(s) still undecided' if p.get('gaps') else None]
    doc.para('; '.join(f for f in facts if f) + '.')
    for r in adopted:
        doc.bullet(f'{r.get("hypothesis_id", "")}: {r.get("statement") or ""} '
                   f'(confidence {r.get("confidence") or "not stated"})')
    if not adopted:
        doc.para('This run adopted no hypothesis: the protocol is the process as the project '
                 'and the evidence already describe it.')


def _overview(doc, p, fp):
    stages = fp.get('stages') or []
    if not stages:
        return
    doc.heading('The process at a glance', 1)
    span = sum(s.get('days') or 0 for s in stages) or 1
    widths = [max(s.get('days') or 0, span * 0.09) for s in stages]
    doc.table([[(s.get('label') or s['stage_id'], True), (_days(s), False)] for s in stages],
              [[[_item_line(i) for i in (s.get('inducers') or [])] or [('nothing set', False)]
                for s in stages]],
              widths=widths,
              head_fills=[VP.STAGE_FILLS[n % len(VP.STAGE_FILLS)]
                          for n in range(len(stages))])


def _stage_walkthrough(doc, p, fp, narrative):
    stages = fp.get('stages') or []
    if not stages:
        return
    captions = CM.by_stage(narrative)
    phys = fp.get('physical') or []
    doc.heading('Stage by stage, at the bench', 1)
    for n, s in enumerate(stages):
        sid = s['stage_id']
        doc.heading(f'{n + 1}. {s.get("label") or sid} — {_days(s)}', 2)
        cap = captions.get(sid)
        if cap:
            doc.para(cap['headline'], bold=True)
            doc.para(cap['at_the_bench'])
        for i in s.get('inducers') or []:
            text, strong = _item_line(i)
            doc.bullet(runs=[('Into the medium: ', {}), (text, {'bold': strong})])
        for i in (x for x in phys if x.get('stage_id') == sid):
            text, strong = _item_line(i)
            doc.bullet(runs=[('Vessel: ', {}), (text, {'bold': strong})])
        if not (s.get('inducers') or any(x.get('stage_id') == sid for x in phys)):
            doc.para('No factor is set for this stage by this run; that is not a statement '
                     'that the stage needs none.')
        if cap and cap.get('watch_for'):
            doc.caveat(f'Watch for: {cap["watch_for"]}')
    whole = [i for i in phys if i.get('stage_id') == 'all']
    if whole:
        doc.heading('For the whole run', 2)
        for i in whole:
            text, strong = _item_line(i)
            doc.bullet(runs=[(text, {'bold': strong})])
    if narrative and narrative.get('closing'):
        doc.heading('Where this leaves the project', 2)
        doc.para(narrative['closing'])


def _round(doc, p):
    plan = p.get('round_plan') or {}
    arms = plan.get('arms') or []
    if not arms:
        return
    doc.heading('The round', 1)
    doc.para(f'Round {_v(plan.get("round") or 1)}: {len(arms)} arm(s) × '
             f'{_v(plan.get("replicates"))} replicate(s). Each arm is the control plus what '
             f'it sets; the full arms-by-stages page exports separately as "the round at a '
             f'glance".')
    doc.table(['Arm', 'What it sets'],
              [[a.get('arm_id', '') + (' (control)' if a.get('control') else ''),
                '; '.join(f'{k} {_v(v)}' for k, v in (a.get('setpoints') or {}).items())
                or 'the process as it runs now'] for a in arms], widths=[1, 3])


def build(protocol, *, run_dir=None, run_id=None, narrative=None, translation=None,
          doc=None, title=None):
    """The final report as one Document (Word by default; pass `pdf.Document` for a PDF).

    *protocol* is the display payload the other exports take. *run_dir*, when
    given, is the finished run's directory: the narrative is read from it
    unless one is passed, and the specialists' sections are appended as
    chapters from the files the run wrote.
    """
    p = protocol or {}
    fp = p.get('first_pass') or FP.plan(p)
    heading = title or p.get('title') or 'Production protocol'
    doc = doc if doc is not None else Document(title=f'{heading} — final report')
    if hasattr(doc, 'footer') and not doc.footer:
        doc.footer = f'{heading} — final report — PROPOSED, NOT APPROVED'
    if narrative is None and run_dir is not None:
        narrative = CM.load(run_dir)

    # ── cover ─────────────────────────────────────────────────────────
    doc.title_block(f'{heading} — final report', p.get('objective'))
    tl = p.get('timeline') or {}
    meta = [('Project', (p.get('project') or {}).get('name')
             or (p.get('project') or {}).get('project_id')),
            ('Run', run_id or p.get('run_id')),
            ('Produced', p.get('created_at')),
            ('Process', ' → '.join(s.get('label') or s['stage_id']
                                   for s in tl.get('stages') or []) or '—'),
            ('Total', f'{_v(tl.get("total_days"))} days' if tl.get('total_days') else '—'),
            ('Status', 'proposed — not approved')]
    doc.table(['', ''], [[k, _v(v)] for k, v in meta if v], widths=[1, 3])
    doc.caveat(NOT_APPROVED)
    if p.get('evidence_status'):
        doc.caveat(p['evidence_status'])
    doc.caveat(HOW_TO_READ)

    _executive(doc, p, fp, narrative)
    _overview(doc, p, fp)
    _stage_walkthrough(doc, p, fp, narrative)
    _round(doc, p)
    gaps = p.get('gaps') or []
    if gaps:
        doc.para('Not decided — the wet lab is blocked until these are: '
                 + ', '.join(g.get('parameter_id', '') for g in gaps), bold=True)

    # ── the chapters: the same blocks the standalone exports write ────
    doc.page_break()
    LP.build(p, run_id=run_id, translation=translation, doc=doc)
    if run_dir is not None:
        doc.page_break()
        SR.literature(run_dir, doc=doc, run_id=run_id)
        doc.page_break()
        SR.bioinformatics(run_dir, doc=doc, run_id=run_id)
        if BT.load(run_dir):
            doc.page_break()
            SR.breakthrough(run_dir, doc=doc, run_id=run_id)
    doc.caveat(NOT_APPROVED)
    return doc


def tobytes(protocol, **kw):
    """The Word file."""
    return build(protocol, **kw).tobytes()


def tobytes_pdf(protocol, **kw):
    """The PDF, from the same blocks."""
    from . import pdf as PDF
    p = protocol or {}
    heading = kw.get('title') or p.get('title') or 'Production protocol'
    return build(protocol, doc=PDF.Document(title=f'{heading} — final report'), **kw).tobytes()


# ── the process as a picture ─────────────────────────────────────────────
LIGHT = {'bg': '#ffffff', 'ink': '#15211c', 'muted': '#5b6b64', 'rule': '#c9d2cd',
         'gap': '#b3362a', 'changed': '#7a5a00'}
DARK = {'bg': '#14191c', 'ink': '#e8edea', 'muted': '#9fb0a8', 'rule': '#3a4540',
        'gap': '#ff8a7a', 'changed': '#ffd166'}
_W, _PAD, _BAND, _LINE = 960, 24, 46, 15


def _e(s):
    return escape(str(s or ''), {'"': '&quot;'})


def flow_svg(protocol, *, dark=False, title=None):
    """The process on one picture: the stages to scale, what goes into each.

    Deterministic — the same protocol twice is the same bytes — and it draws
    only what the protocol carries: a gap is written as a gap, a changed value
    shows the control it replaces, and the banner says nothing is approved.
    """
    p = protocol or {}
    c = DARK if dark else LIGHT
    fp = p.get('first_pass') or FP.plan(p)
    stages = fp.get('stages') or []
    phys = fp.get('physical') or []
    heading = title or p.get('title') or 'Production protocol'
    span = sum(s.get('days') or 0 for s in stages) or 1
    widths = [max(s.get('days') or 0, span * 0.09) for s in stages] or [1]
    inner = _W - 2 * _PAD
    scale = inner / sum(widths)

    def lines_of(s):
        rows = [_item_line(i) for i in (s.get('inducers') or [])]
        rows += [_item_line(i) for i in phys if i.get('stage_id') == s['stage_id']]
        return rows or [('nothing set by this run', False)]

    per_stage = [lines_of(s) for s in stages]
    whole = [i for i in phys if i.get('stage_id') == 'all']
    depth = max((len(r) for r in per_stage), default=1)
    h = 96 + _BAND + 14 + depth * _LINE + (22 + _LINE * len(whole) if whole else 0) + 34
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{h}" '
           f'viewBox="0 0 {_W} {h}" role="img" aria-label="{_e(heading)}">',
           f'<rect width="{_W}" height="{h}" fill="{c["bg"]}"/>',
           f'<text x="{_PAD}" y="32" fill="{c["ink"]}" font-family="Helvetica,Arial,sans-serif"'
           f' font-size="17" font-weight="bold">{_e(heading)} — the process</text>',
           f'<text x="{_PAD}" y="52" fill="{c["gap"]}" font-family="Helvetica,Arial,sans-serif"'
           f' font-size="11">PROPOSED — NOT APPROVED. A named person approves a protocol '
           f'before any of it is run.</text>']
    x, top = float(_PAD), 72.0
    for n, s in enumerate(stages):
        w = widths[n] * scale
        fill = '#' + VP.STAGE_FILLS[n % len(VP.STAGE_FILLS)]
        out.append(f'<rect x="{x:.1f}" y="{top}" width="{w - 3:.1f}" height="{_BAND}" rx="6" '
                   f'fill="{fill}" stroke="{c["rule"]}"/>')
        out.append(f'<text x="{x + 8:.1f}" y="{top + 19}" fill="#15211c" font-family='
                   f'"Helvetica,Arial,sans-serif" font-size="12.5" font-weight="bold">'
                   f'{_e(s.get("label") or s["stage_id"])}</text>')
        out.append(f'<text x="{x + 8:.1f}" y="{top + 36}" fill="#3c4a44" font-family='
                   f'"Helvetica,Arial,sans-serif" font-size="10.5">{_e(_days(s))}</text>')
        y = top + _BAND + 16
        # Clipped to the stage's own column, so a long line under a narrow
        # stage does not run into the next one's.
        room = max(8, int((w - 10) / 5.6))
        for text, strong in per_stage[n]:
            color = (c['gap'] if 'GAP' in text else c['changed'] if strong else c['muted'])
            shown = text if len(text) <= room else text[:room - 1] + '…'
            out.append(f'<text x="{x + 2:.1f}" y="{y:.1f}" fill="{color}" font-family='
                       f'"Helvetica,Arial,sans-serif" font-size="10.5">{_e(shown)}</text>')
            y += _LINE
        x += w
    y = top + _BAND + 16 + depth * _LINE + 8
    if whole:
        out.append(f'<text x="{_PAD}" y="{y:.1f}" fill="{c["ink"]}" font-family='
                   f'"Helvetica,Arial,sans-serif" font-size="11" font-weight="bold">'
                   f'For the whole run</text>')
        y += _LINE + 2
        for i in whole:
            text, strong = _item_line(i)
            color = c['gap'] if 'GAP' in text else c['muted']
            out.append(f'<text x="{_PAD}" y="{y:.1f}" fill="{color}" font-family='
                       f'"Helvetica,Arial,sans-serif" font-size="10.5">{_e(text)}</text>')
            y += _LINE
    out.append(f'<text x="{_PAD}" y="{h - 12}" fill="{c["muted"]}" font-family='
               f'"Helvetica,Arial,sans-serif" font-size="10">R reported · A adapted · '
               f'D design choice · GAP no value (blocks the wet lab) · NEW not registered '
               f'yet · PUB published route, not carried by this protocol</text>')
    out.append('</svg>')
    return '\n'.join(out) + '\n'
