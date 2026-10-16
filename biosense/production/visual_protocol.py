"""The protocol on one landscape page: the arms, the vessel, the stages.

The full bench copy (`lab_protocol.py`) is everything somebody needs to run
round 1 and sign for it, and it reads like the document it is. What a
scientist asked for beside it is the picture they draw on a whiteboard before
a round: the stages left to right at their real lengths, one row per arm, and
in each cell what goes into that arm at that stage — so the one thing each arm
changes stands out from everything it keeps.

So this page is that and only that:

* the vessel and the round on one line;
* the stages as a band, each as wide as it is long;
* an arms x stages grid, every dose with its provenance letter, a cell
  shaded where an arm differs from the control (with the control's value
  beside it), and a gap shaded as a gap: nothing is filled in to make a
  cell look complete;
* what to measure, how many vessels, and that none of it is approved.

It renders the same validated protocol as the full copy and computes nothing
but the layout. Word and PDF are written from the same blocks.
"""
from __future__ import annotations

import re

from . import first_pass as FP
from .docx import Document

STAGE_FILLS = ('DCEFE6', 'E1E7F7', 'F7EBDD', 'ECE3F4', 'DDF1F3', 'F3EFD9', 'F4E1E6')
CHANGED = 'FFE9A8'
GAP = 'F6D4CF'
WHOLE = 'EEF1F0'
NOT_APPROVED = ('PROPOSED — NOT APPROVED. Nothing on this page has been approved; a named '
                'person approves a protocol (approve-protocol --approved-by "<name>") before '
                'any of it is run.')
LEGEND = ('Shaded yellow: where an arm differs from the control (the control\'s value in '
          'brackets). Shaded red: nothing established — decide it before the round. Each arm '
          'is the control plus what it sets. Letters: current = the process as it runs now, '
          'R reported, A adapted, D design choice, PUB the published route (not carried by '
          'this protocol), NEW a lever this project does not carry yet.')
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
    if round_mode and item.get('was') is not None:
        return item['was'], 'current'
    return item.get('value'), item.get('letter') or '?'


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


# A factor is written with the Greek letter spelled out or abbreviated, and the
# two spellings are one name: "TGF-beta1" is the factor "tgfb1_ng_ml" sets,
# "IL-1alpha" is "il1a", "PPARgamma" is "pparg". Only a whole word is replaced
# (nothing before it, no letter after it), so "betaine" and "alphabet" are left
# alone. This widens what a setpoint can match and narrows nothing.
GREEK = {'alpha': 'a', 'beta': 'b', 'gamma': 'g', 'delta': 'd', 'epsilon': 'e',
         'zeta': 'z', 'theta': 't', 'kappa': 'k', 'lambda': 'l', 'sigma': 's',
         'omega': 'w'}
_GREEK_RE = re.compile('(' + '|'.join(GREEK) + ')(?![a-z])', re.I)


def _ids(text):
    """Every key one name answers to: itself, normalised, and its aliases.

    `first_pass._norm` is the identity rule this repository already uses for
    this question ("M-CSF", "mcsf_ng_ml" and "M-CSF concentration" agree). On
    top of it: a name in brackets ("GM-CSF (CSF2)") is matched on either part,
    since both name the one factor, and a Greek letter is matched spelled out
    or abbreviated.
    """
    text = str(text or '')
    if not text:
        return set()
    out = {text}
    parts = [text] + re.findall(r'\(([^()]*)\)', text) + [re.sub(r'\([^()]*\)', ' ', text)]
    for part in list(parts):
        swapped = _GREEK_RE.sub(lambda m: GREEK[m.group(1).lower()], part)
        if swapped != part:
            parts.append(swapped)
    for part in parts:
        key = FP._norm(part)
        if key:
            out.add(key)
    return out


def _keys(item):
    """Every name one stage line answers to, so an arm's setpoint finds it.

    A line can reach the page under more than one id. The protocol's own
    parameters carry the project's parameter id (`gmcsf_ng_ml`); a line merged
    from the published route carries the baseline's factor id (`gmcsf_csf2`)
    and the factor's printed name. A round plan sets levers by the project's
    id, so matching the two by string equality missed every baseline line —
    the arm's change then vanished from the stage that carries the factor.
    """
    return _ids(item.get('parameter_id')) | _ids(item.get('label'))


def _placements(arms, by_stage):
    """Which stage line each arm's setpoint speaks about: {lever: (stage_id, index)}.

    Decided once for the whole page, so every arm reads the same line as the
    same lever. A lever that matches more than one line binds to the first, in
    stage order; a lever that matches none is placed nowhere and the caller
    says so rather than guessing a stage for it.
    """
    wanted = {k for a in arms for k in (a.get('setpoints') or {})}
    placed = {}
    for sid, items in by_stage.items():
        for n, item in enumerate(items):
            keys = _keys(item)
            for lever in wanted - set(placed):
                if _ids(lever) & keys:
                    placed[lever] = (sid, n)
    return placed


def grid(protocol):
    """(columns, rows) for the arms x stages page. Pure.

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


def build(protocol, *, run_id=None, translation=None, doc=None, title=None):
    """A one-page Document (Word by default; pass `pdf.Document` for a PDF)."""
    p = protocol or {}
    plan = p.get('round_plan') or {}
    heading = title or p.get('title') or 'Production protocol'
    doc = doc if doc is not None else Document(title=heading, landscape=True)
    if hasattr(doc, 'footer') and not doc.footer:
        doc.footer = f'{heading} — the round at a glance — PROPOSED, NOT APPROVED'
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

    cols, rows = grid(p)
    staged = [c for c in cols if c['days'] is not None]
    if staged:
        # The stages as a band, each as wide as it is long. A two-day stage
        # in a forty-day process still needs room for its name.
        span = sum(c['days'] or 0 for c in staged) or 1
        widths = [max(c['days'] or 0, span * 0.09) for c in staged]
        doc.heading('The stages, to scale', 2)
        doc.table([[(c['label'], True), (_days(c), False)] for c in staged], [],
                  widths=widths, head_fills=[c['fill'] for c in staged])

    doc.heading('What each arm gets, stage by stage', 2)
    headers = ['Arm'] + [[(c['label'], True), (_days(c), False)] for c in cols]
    doc.table(headers,
              [[r['arm']] + [cell['lines'] for cell in r['cells']] for r in rows],
              widths=[2] + [3] * len(cols),
              head_fills=[None] + [c['fill'] for c in cols],
              fills=[[None] + [cell['fill'] for cell in r['cells']] for r in rows])
    doc.caveat(LEGEND)

    readouts = plan.get('readouts') or []
    if readouts:
        doc.para('Measure: ' + '; '.join(
            ' '.join(str(x) for x in (r.get('name'), f'({r["unit"]})' if r.get('unit') else None,
                                      r.get('when')) if x) for r in readouts))
    gaps = p.get('gaps') or []
    if gaps:
        doc.para('Not decided — the round cannot start until these are: '
                 + ', '.join(g.get('parameter_id', '') for g in gaps), bold=True)
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
