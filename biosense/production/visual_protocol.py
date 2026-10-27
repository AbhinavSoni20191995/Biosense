"""The round on a page somebody can act on: the recipe, then what each arm changes.

The full bench copy (`lab_protocol.py`) is everything somebody needs to run
round 1 and sign for it. This is the page before that one — what a person
draws on a whiteboard when they are asked "so what are we actually doing?":

* the stages left to right at their real lengths;
* **the reference recipe**, written out once: what goes into the vessel at
  each stage, at what dose, with a gap shown as a gap;
* **what each arm changes** about that recipe, and nothing else;
* what to measure, how many vessels, and that none of it is approved.

That order is the whole design, and it replaces an arms × stages grid in which
every arm's column repeated the entire recipe. The repetition was not neutral:
five columns of identical text hid the one line that differed, and it read as
though each arm set every value in it. An arm sets what it sets. The detail
grid is still here, after a page break, for a reader who wants every value in
one place — it is the appendix now, not the page.

Everything shown is computed by `plain_plan`, which both this document and the
web page read, so the two cannot disagree about what the experiment is. This
module owns layout and nothing else.
"""
from __future__ import annotations

from . import first_pass as FP
from . import plain_plan as PP
from .docx import Document

STAGE_FILLS = ('DCEFE6', 'E1E7F7', 'F7EBDD', 'ECE3F4', 'DDF1F3', 'F3EFD9', 'F4E1E6')
CHANGED = 'FFE9A8'
GAP = 'F6D4CF'
WHOLE = 'EEF1F0'
NOT_APPROVED = ('PROPOSED — NOT APPROVED. Nothing on this page has been approved; a named '
                'person approves a protocol (approve-protocol --approved-by "<name>") before '
                'any of it is run.')
LEGEND = ('Each arm is the reference recipe plus the change(s) listed against it, and nothing '
          'else: a value an arm does not name is the reference\'s. Shaded red: nothing '
          'established — decide it before the round. Letters say where a number comes from: '
          'current = the process as it runs now, R reported, A adapted, D design choice, PUB '
          'the published route (not carried by this protocol), NEW a lever this project does '
          'not carry yet.')
DETAIL_NOTE = ('The same round with every value written out for each arm, for whoever wants it '
               'in one table. Shaded yellow: where an arm differs from the reference (the '
               'reference value in brackets).')
VESSEL_WORD = {'six_well': '6-well plate', 'cero': 'CERO 3D rotating tube',
               'stirred_tank': 'stirred-tank bioreactor'}


def _v(x):
    if x is None or x == '':
        return '—'
    if isinstance(x, float):
        return f'{x:g}'
    return str(x)


def _same(a, b):
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _base(item, round_mode):
    """(value, tag) a control vessel gets for one line.

    In a round the control runs the current process, so a line the run changed
    shows what it was ("current"); the new value is what an arm sets, if one
    does. Without a round, the page is the protocol as written.
    """
    return PP.value_of(item, round_mode)


def _line(item, value, tag, changed=False, control=None):
    """'M-CSF 50 ng/mL [current]', or an arm's value against the control's."""
    unit = f' {item["unit"]}' if item.get('unit') else ''
    label = item.get('label') or item.get('parameter_id') or ''
    if changed:
        return (f'{label} {_v(value)}{unit}  (control {_v(control)}) [D]', True)
    if value is None:
        if item.get('provenance') == 'gap':
            return (f'{label}: GAP — decide before the round', True)
        if item.get('from_baseline'):
            return (f'{label}: no dose published [PUB]', False)
        return (f'{label}: not set', False)
    return (f'{label} {_v(value)}{unit} [{tag}]', False)


def _placements(arms, by_stage):
    """Which stage line each arm's setpoint speaks about: {lever: (stage_id, index)}.

    Decided once for the whole page, so every arm reads the same line as the
    same lever. `plain_plan.place` is the rule, and it is shared with the web
    page deliberately: the two must not disagree about which stage a change
    happens in.
    """
    wanted = {k for a in arms for k in (a.get('setpoints') or {})}
    stages = [{'stage_id': sid, 'label': sid, 'items': items}
              for sid, items in by_stage.items()]
    return PP.place(sorted(wanted), stages)


def grid(protocol):
    """(columns, rows) for the detail table: every value, arm by arm. Pure.

    columns: [{'id', 'label', 'days', 'fill'}]; rows: [{'arm', 'cells':
    [{'lines': [(text, bold)], 'fill'}]}]. A setpoint for a lever no stage
    carries goes under "Whole run", said as what it is.
    """
    p = protocol or {}
    fp = p.get('first_pass') or FP.plan(p)
    stages = list(fp.get('stages') or [])
    phys = list(fp.get('physical') or [])
    plan = p.get('round_plan') or {}
    round_mode = bool(plan.get('arms'))
    arms = list(plan.get('arms') or []) or [
        {'arm_id': 'Protocol', 'label': 'as written (no round plan)', 'control': True,
         'setpoints': {}}]
    by_stage = {s['stage_id']: list(s.get('inducers') or []) for s in stages}
    for i in phys:
        sid = i.get('stage_id') or 'all'
        by_stage.setdefault(sid if sid in by_stage else 'all', []).append(i)
    by_stage.setdefault('all', [])
    placed = _placements(arms, by_stage)
    loose = sorted({k for a in arms for k in (a.get('setpoints') or {}) if k not in placed})
    cols = [{'id': s['stage_id'], 'label': s.get('label') or s['stage_id'],
             'days': s.get('days'), 'start': s.get('day_start') or 0,
             'fill': STAGE_FILLS[n % len(STAGE_FILLS)]} for n, s in enumerate(stages)]
    if by_stage['all'] or loose:
        cols.append({'id': 'all', 'label': 'Whole run', 'days': None, 'start': None,
                     'fill': WHOLE})
    rows = []
    for a in arms:
        sp = a.get('setpoints') or {}
        cells = []
        for c in cols:
            lines, fill = [], None
            for n, item in enumerate(by_stage.get(c['id'], [])):
                value, tag = _base(item, round_mode)
                # The levers this arm sets that belong to this line, by the
                # page-wide placement rather than by this line's own id.
                mine = [k for k, at in placed.items()
                        if at == (c['id'], n) and k in sp and not _same(sp[k], value)]
                if not a.get('control') and mine:
                    lines.append(_line(item, sp[mine[0]], 'D', changed=True, control=value))
                    fill = CHANGED
                else:
                    lines.append(_line(item, value, tag))
                    if value is None and item.get('provenance') == 'gap' and fill is None:
                        fill = GAP
            if c['id'] == 'all':
                for pid in loose:
                    if pid in sp and not a.get('control'):
                        # Not "not in the protocol": the page could not find a
                        # line for this lever, which is not the same as knowing
                        # the protocol does not carry it. Say only what is true.
                        lines.append((f'{pid} {_v(sp[pid])}  (this arm sets it; no stage line '
                                      f'on this page carries it) [D]', True))
                        fill = CHANGED
            cells.append({'lines': lines or [('—', False)], 'fill': fill})
        aid = str(a.get('arm_id', ''))
        label = [(aid + (' · control' if a.get('control') and 'control' not in aid.lower()
                         else ''), True)]
        if a.get('label'):
            label.append((a['label'], False))
        rows.append({'arm': label, 'cells': cells})
    return cols, rows


def _days(c):
    if c.get('days') is None:
        return 'all stages'
    return f'day {_v(c["start"])}–{_v(c["start"] + c["days"])} ({_v(c["days"])} d)'


def _recipe_lines(stage):
    """One stage's lines for the recipe table, a gap marked as one."""
    out = []
    for line in stage['lines']:
        out.append((line['text'], bool(line['gap'])))
    return out or [(stage.get('empty_because')
                    or 'Nothing in this run describes this stage.', False)]


def build(protocol, *, run_id=None, translation=None, doc=None, title=None, narrative=None):
    """A Document (Word by default; pass `pdf.Document` for a PDF)."""
    p = protocol or {}
    plan = p.get('round_plan') or {}
    heading = title or p.get('title') or 'Production protocol'
    doc = doc if doc is not None else Document(title=heading, landscape=True)
    if hasattr(doc, 'footer') and not doc.footer:
        doc.footer = f'{heading} — the round at a glance — PROPOSED, NOT APPROVED'
    # The page computed this already and put it in the payload; reading it back
    # is what keeps the document and the screen saying the same thing. A
    # protocol that never went through `protocol_summary.display` (a fixture, a
    # file read straight off disk) is laid out the same way from scratch.
    plain = p.get('plain_plan') if isinstance(p.get('plain_plan'), dict) else None
    if plain is None or narrative is not None:
        plain = PP.build(p, narrative=narrative)
    rnd = plan.get('round') or 1
    doc.title_block(f'{heading} — round {_v(rnd)} at a glance', p.get('objective'))

    fmt = (translation or {}).get('to') or {}
    vol = (translation or {}).get('working_volume_ml')
    arms = plan.get('arms') or []
    reps = plan.get('replicates')
    total = (p.get('timeline') or {}).get('total_days')
    facts = [f'Vessel: {fmt.get("label") or VESSEL_WORD.get(p.get("culture_format") or "") or "the one this protocol was written for"}'
             + (f' ({fmt["vessel"]})' if fmt.get('vessel') else ''),
             f'working volume {_v(vol)} mL' if vol else None,
             f'{len(arms)} arm(s) × {_v(reps)} replicate(s) = {len(arms) * int(reps)} vessels'
             if arms and isinstance(reps, int) else None,
             f'{_v(total)} days in all' if total else None,
             f'run {run_id}' if run_id else None]
    doc.para(' · '.join(f for f in facts if f), bold=True)
    doc.caveat(NOT_APPROVED)

    # What this round is, in sentences, before any table.
    points = (plain.get('key_points') or {}).get('points') or []
    if points:
        doc.heading('In short', 2)
        for line in points:
            doc.bullet(line)

    staged = [s for s in plain['recipe'] if s['days'] is not None]
    if staged:
        # The stages as a band, each as wide as it is long. A two-day stage
        # in a forty-day process still needs room for its name.
        span = sum(s['days'] or 0 for s in staged) or 1
        widths = [max(s['days'] or 0, span * 0.09) for s in staged]
        doc.heading('The stages, to scale', 2)
        doc.table([[(s['label'], True), (s['window'], False)] for s in staged], [],
                  widths=widths,
                  head_fills=[STAGE_FILLS[n % len(STAGE_FILLS)] for n in range(len(staged))])

    # The recipe, once. This is the control, and every arm is read against it.
    ref = plain.get('reference_arm')
    named = f' ({ref})' if ref and 'control' not in str(ref).lower() else ''
    doc.heading('The recipe every arm starts from', 2)
    doc.para(f'What goes into the vessel at each stage. This is the control{named}: the '
             f'process as it runs now, and what every other arm is compared with.')
    doc.table(['Stage', 'When', 'What goes in'],
              [[[(s['label'], True)], s['window'], _recipe_lines(s)] for s in plain['recipe']],
              widths=[2, 1.6, 7],
              fills=[[None, None,
                      GAP if any(line['gap'] for line in s['lines']) else None]
                     for s in plain['recipe']])

    # Then, per arm, only the difference.
    doc.heading('What each arm changes about it', 2)
    rows = []
    fills = []

    def arm_cell(a):
        """The arm's id, and its label only where the label says something more."""
        cell = [(a['arm_id'], True)]
        if a['label'] and a['label'].strip().lower() != a['arm_id'].strip().lower():
            cell.append((a['label'], False))
        return cell

    for a in plain['arms']:
        if a['control']:
            rows.append([arm_cell(a),
                         'the reference', [('Every other arm is this recipe with one or more '
                                            'changes.', False)], ', '.join(a['tests']) or '—'])
            fills.append([None, None, None, None])
            continue
        where = ', '.join(sorted({c['stage_label'] for c in a['changes']})) or '—'
        what = [(c['text'], True) for c in a['changes']]
        for u in a['unplaced']:
            # Said as what it is: the page could not place the lever, which is
            # not the same as knowing the protocol does not carry it.
            what.append((f'{u["parameter_id"]} → {_v(u["to"])} (this arm sets it; no stage on '
                         f'this page carries it)', True))
        if not what:
            what = [('Nothing: every setpoint it names already matches the reference.', False)]
        rows.append([arm_cell(a), where, what, ', '.join(a['tests']) or '—'])
        fills.append([None, None, CHANGED if a['changes'] or a['unplaced'] else None, None])
    doc.table(['Arm', 'In which stage', 'What changes, against the reference', 'Tests'],
              rows, widths=[2.4, 2, 6, 1.2], fills=fills)
    for a in plain['arms']:
        if not a['control'] and a.get('basis'):
            doc.para(f'{a["arm_id"]}: {a["basis"]}')
    doc.caveat(LEGEND)

    measure = plain.get('measure') or []
    if measure:
        doc.heading('What to measure', 2)
        doc.table(['Readout', 'Unit', 'When'],
                  [[m.get('name') or '', m.get('unit') or '', m.get('when') or '']
                   for m in measure], widths=[6, 1.5, 2.5])
    decide = plain.get('decide_first') or []
    if decide:
        doc.heading('Decide these before the round starts', 2)
        for g in decide:
            doc.bullet(f'{g["parameter_id"]}: {g.get("why") or "not established"}')
    doc.caveat(NOT_APPROVED)

    # The appendix: every value, arm by arm, for whoever wants one table.
    cols, grid_rows = grid(p)
    if cols and grid_rows:
        doc.page_break()
        doc.heading('Every value, arm by arm', 2)
        doc.para(DETAIL_NOTE)
        headers = ['Arm'] + [[(c['label'], True), (_days(c), False)] for c in cols]
        doc.table(headers,
                  [[r['arm']] + [cell['lines'] for cell in r['cells']] for r in grid_rows],
                  widths=[2] + [3] * len(cols),
                  head_fills=[None] + [c['fill'] for c in cols],
                  fills=[[None] + [cell['fill'] for cell in r['cells']] for r in grid_rows])
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
    return build(protocol, doc=PDF.Document(title=heading, landscape=True), **kw).tobytes()
