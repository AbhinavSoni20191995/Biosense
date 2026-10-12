"""The protocol as a document somebody can carry to the bench.

Everything this system produces is a web page or a JSON file, and neither of
those gets printed, taped inside a hood, signed by a supervisor or emailed to
the collaborator who is actually going to run the thing. This writes the same
protocol as a Word document or a PDF in the order the work happens, for a
reader who has never seen the run page:

1. what is still undecided — a protocol with a gap cannot be run, so it is
   stated on the first page, not discovered at the bench on the day;
2. the whole process on one page: each stage, its days, what goes into the
   medium, the vessel settings, what to measure;
3. what to have ready: media and reagents, with working concentrations and
   where each number came from;
4. each stage as numbered lines to tick off, in the order they happen, every
   dose in bold with the letter that says what stands behind it;
5. the vessel settings an operator enters; what to measure; the round as one
   row per vessel;
6. then, after the procedure, the reasoning: what the run is testing, the
   letters, the limitations, the other plans — and the approval block.

Three things it must never let a reader forget, so each is a block on the page
rather than a line in a footer:

* **nothing here is approved.** The approval block is at the end, empty, with
  the command that fills it, and "PROPOSED — NOT APPROVED" is at the top and
  the bottom. A protocol this system wrote is a proposal until a named person
  says otherwise, and a document that looks like a work order is the most
  dangerous artifact it could produce;
* **every number says where it came from.** R reported, A adapted, D a design
  choice somebody has to accept, PUB from the published route and not carried
  by this protocol, P the person's own instruction, GAP nothing at all. A
  document that drops provenance is how an adapted dose becomes a fact three
  photocopies later;
* **a person's edits are theirs.** A value or a note asked for after the run
  (`evidence/protocol_edits.py`) is on the page as what it is — requested by
  name, a design choice or an instruction, never a result of the run.

The writing is deliberately mechanical: this is a rendering of a validated
protocol document, and it computes nothing, decides nothing and rounds
nothing. A value it cannot show is shown as missing. The same `build` writes
to the Word writer and the PDF writer; the two say the same things.
"""
from __future__ import annotations

from . import first_pass as FP
from .docx import Document

PROV_WORD = {
    'reported': 'R — reported: a source states this value for a comparable system.',
    'adapted': 'A — adapted: a source states it for a different system and it was moved here.',
    'design_choice': 'D — design choice: a reasoned starting value with no source. '
                     'Somebody has to accept it.',
    'gap': 'GAP — nothing established. Decide this before the run; it is not a number to guess.',
    'candidate': 'NEW — a lever a hypothesis named that this project does not carry yet.',
    'published': 'PUB — from the published route: what the literature states this stage uses. '
                 'This protocol does not carry it, and nobody here approved it.',
    'person': 'P — the person\'s own instruction, written after the run at their request. '
              'Not a checked value and not evidence; the approver reads it as theirs.',
}
PROV_LETTER = {'reported': 'R', 'adapted': 'A', 'design_choice': 'D', 'gap': 'GAP',
               'candidate': 'NEW'}
NOT_APPROVED = ('PROPOSED — NOT APPROVED. No part of this document has been approved by '
                'anybody, and nothing in this system can approve it. A named person approves '
                'a protocol from the command line (approve-protocol --approved-by "<name>") '
                'after reading it. Until then this is a proposal to read, not a procedure '
                'to run.')


VESSEL_WORD = {'six_well': 'one well of a 6-well plate',
               'cero': 'one CERO 50 mL rotating tube',
               'stirred_tank': 'a stirred-tank vessel'}


def _vessel_word(fid):
    return VESSEL_WORD.get(fid or '')


def _v(x):
    if x is None or x == '':
        return '—'
    if isinstance(x, float):
        return f'{x:g}'
    return str(x)


def _dose(item):
    if item.get('value') is None:
        return ('no dose published' if item.get('from_baseline')
                else 'not set' if item.get('provenance') == 'gap' else '—')
    out = f'{_v(item["value"])}{" " + item["unit"] if item.get("unit") else ""}'
    # What it was matters to whoever ran the last round, so it travels with
    # the number rather than being left to a diff of two documents.
    if item.get('was') is not None:
        out += f' (was {_v(item["was"])})'
    return out


def _letter(item):
    """The plan's own tag where it has one (PUB for a published factor), else
    the provenance letter. The two must not disagree on one page."""
    return item.get('letter') or PROV_LETTER.get(item.get('provenance'), '?')


def _stands(item):
    """'A · PMID:123' — the letter and what it cites, for a table cell."""
    s = _letter(item)
    if item.get('refs'):
        s += ' · ' + ', '.join(str(r) for r in item['refs'][:3])
    if item.get('requested_by'):
        s += f' · asked for by {item["requested_by"]}'
    return s


def _when(item):
    """When within the stage, where anything says so. Never invented."""
    if item.get('timing'):
        return item['timing']
    # The protocol holds one value per parameter per stage and no finer
    # schedule, so that is what is said — not a timing nobody recorded.
    return 'stage-wide (no finer timing recorded)' if item.get('value') is not None else '—'


def _stage_days(s):
    if s.get('days') is None:
        return 'duration not set'
    start = s.get('day_start') or 0
    return f'day {_v(start)} to {_v(start + s["days"])} ({_v(s["days"])} d)'


def _verb(item):
    a = item.get('action')
    if item.get('provenance') == 'gap':
        return 'Decide the dose of'
    if item.get('from_baseline'):
        return 'The published route adds'
    return {'raise': 'Raise', 'lower': 'Lower', 'set': 'Set', 'keep': 'Keep'}.get(a, 'Add')


def _step_runs(item):
    """One line to tick: the verb, the thing, the dose in bold, when, and the letter."""
    dose = _dose(item)
    runs = [(f'{_verb(item)} {item.get("label", "")}', {})]
    if item.get('value') is not None:
        runs.append((' to ' if item.get('action') in ('raise', 'lower', 'set') else ' at ', {}))
        runs.append((dose, {'bold': True}))
    elif item.get('provenance') == 'gap':
        runs.append((': nothing established', {}))
    elif item.get('from_baseline'):
        runs.append((' (no dose published)', {}))
    when = item.get('timing')
    if when:
        runs.append((f', {when}', {}))
    runs.append((f'  [{_stands(item)}]', {'italic': True}))
    return runs


def _setting_runs(item):
    runs = [(f'Set {item.get("label", "")}', {})]
    if item.get('value') is not None:
        runs += [(' to ', {}), (_dose(item), {'bold': True})]
    else:
        runs.append((': not set', {}))
    runs.append((f'  [{_stands(item)}]', {'italic': True}))
    return runs


def _glance_rows(fp, phys, readouts):
    rows = []
    measure = '; '.join(r.get('label') or r.get('readout_id', '') for r in readouts[:4])
    for s in fp.get('stages') or []:
        adds = [f'{i.get("label", "")} {_dose(i)} [{_letter(i)}]'
                for i in s.get('inducers') or []]
        sets = [f'{i.get("label", "")} {_dose(i)} [{_letter(i)}]'
                for i in phys if i.get('stage_id') == s['stage_id']]
        rows.append([s.get('label') or s['stage_id'], _stage_days(s),
                     '; '.join(adds) or 'nothing set by this run',
                     '; '.join(sets) or 'as for the whole run', measure or '—'])
    return rows


def _materials_rows(fp, phys):
    """Media reagents from the plan itself: nothing the plan does not carry."""
    rows = []
    for s in fp.get('stages') or []:
        for i in s.get('inducers') or []:
            rows.append([i.get('label', ''), _dose(i), s.get('label') or s['stage_id'],
                         _stands(i)])
    return rows


def build(protocol, *, title=None, run_id=None, project_name=None, translation=None, doc=None):
    """A Document for *protocol* — the display payload `protocol_summary.display` returns.

    *translation*, when given, is `formats.translate(...)` for the vessel the
    person is actually going to use: the amounts per vessel go beside the
    concentrations, and what does not cross between vessels is listed as
    theirs to decide rather than printed as though it had. *doc* is the
    writer to use (the Word one by default; `pdf.Document` for a PDF).
    """
    p = protocol or {}
    fp = p.get('first_pass') or FP.plan(p)
    tl = p.get('timeline') or {}
    gaps = p.get('gaps') or []
    edits = p.get('edits') or {}
    omit = set(edits.get('omit') or [])
    heading_text = title or p.get('title') or 'Production protocol'
    doc = doc if doc is not None else Document(title=heading_text)
    if hasattr(doc, 'footer') and not doc.footer:
        doc.footer = f'{heading_text} — PROPOSED, NOT APPROVED'

    doc.title_block(heading_text, p.get('objective'))
    fmt = (translation or {}).get('to') or {}
    vol = (translation or {}).get('working_volume_ml')
    meta = [('Project', project_name or (p.get('project') or {}).get('name')
             or (p.get('project') or {}).get('project_id')),
            ('Vessel', fmt.get('vessel') or _vessel_word(p.get('culture_format'))),
            ('Working volume', f'{_v(vol)} mL' if vol else None),
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
    if edits:
        doc.caveat(f'This copy carries changes {edits.get("requested_by") or "the person"} '
                   f'asked for after the run'
                   + (f' (“{edits["request"]}”)' if edits.get('request') else '')
                   + ': a value they set is marked D with their name, their instructions are '
                     'marked P. ' + (edits.get('does_not_change') or ''))

    # The gaps first. A protocol with one cannot be run, and finding that out
    # on the day is the failure this ordering exists to prevent.
    if gaps:
        doc.heading('Before you run this: what is not decided', 1)
        doc.para(f'{len(gaps)} value(s) in this protocol have nothing behind them. The run '
                 f'left them open rather than filling them with a plausible number. Each one '
                 f'needs a decision from somebody who knows this process.')
        doc.table(['Parameter', 'Why it is open'],
                  [[g.get('parameter_id', ''), g.get('why', '')] for g in gaps], widths=[1, 2])

    phys = fp.get('physical') or []
    readouts = p.get('readouts') or []
    stages = fp.get('stages') or []

    # ── the whole process on one page ────────────────────────────────
    if 'glance' not in omit and stages:
        doc.heading('At a glance', 1)
        doc.para('The whole process, one row per stage. Each number carries the letter that '
                 'says what stands behind it; the stage-by-stage lines below are the same '
                 'values in the order you act on them.')
        doc.table(['Stage', 'Days', 'Into the medium', 'Vessel settings', 'Measure'],
                  _glance_rows(fp, phys, readouts), widths=[2, 2, 4, 3, 2])
        whole = [f'{i.get("label", "")} {_dose(i)} [{_letter(i)}]'
                 for i in phys if i.get('stage_id') == 'all']
        if whole:
            doc.para('For the whole run: ' + '; '.join(whole) + '.')

    # Asked for in a particular vessel, the vessel's own numbers come first:
    # that is what the person is going to pipette. The stage-by-stage section
    # below it is the protocol as the run wrote it, in its own vessel.
    if translation:
        _vessel_section(doc, translation)

    # ── what to have ready ──────────────────────────────────────────
    bench = p.get('bench_setup')
    if 'materials' not in omit:
        _materials_section(doc, fp, phys, bench)

    # ── the procedure ───────────────────────────────────────────────
    doc.heading('The protocol, stage by stage'
                + (' (as written, in the original vessel)' if translation else ''), 1)
    doc.para('Each stage as lines to tick off, in the order they happen. The dose is in bold '
             'and the letter after each line says what stands behind the number; a value with '
             '"was" is one this run changed from the control.')
    handling = (bench or {}).get('handling') or []
    notes = edits.get('notes') or []
    for s in stages:
        sid = s['stage_id']
        doc.heading(f'{s.get("label") or sid} — {_stage_days(s)}', 2)
        inducers = s.get('inducers') or []
        here_phys = [i for i in phys if i.get('stage_id') == sid]
        here_hand = [h for h in handling if h.get('stage_id') == sid]
        here_notes = [n for n in notes if n.get('stage_id') == sid]
        if not (inducers or here_phys or here_hand or here_notes):
            doc.para('No factor is set for this stage by this run. That is not a statement '
                     'that the stage needs none: it means this project registered no parameter '
                     'that could carry one and the run established nothing to put here.')
            continue
        for h in here_hand:
            doc.check(runs=[(f'{h.get("step", "")}', {'bold': True}),
                            (f' ({h["when"]})' if h.get('when') else '', {}),
                            (f', {h["duration"]}' if h.get('duration') else '', {}),
                            (f' — {h.get("what", "")}', {}),
                            (f'  [{_letter({"provenance": h.get("provenance")})}'
                             + (' · ' + ', '.join(x.get('ref', '') for x in h.get('sources') or [])
                                if h.get('sources') else '') + ']', {'italic': True})])
        for i in inducers:
            doc.check(runs=_step_runs(i))
        for i in here_phys:
            doc.check(runs=_setting_runs(i))
        for n in here_notes:
            doc.check(runs=[(n.get('text', ''), {}),
                            (f'  [P · {n.get("requested_by") or "the person"}]', {'italic': True})])
        for i in inducers:
            if i.get('provenance') == 'gap':
                doc.caveat(f'{i.get("label")}: no dose established. '
                           + (f'To settle it, search: {i["what_to_search"]}'
                              if i.get('what_to_search') else
                              'Decide a value before the run, and record it as a design '
                              'choice.'))
    loose = [n for n in notes if not n.get('stage_id')]
    unstaged = fp.get('unstaged') or []
    if loose or unstaged:
        doc.heading('For the whole run', 2)
        for i in unstaged:
            doc.check(runs=_step_runs(i))
        for n in loose:
            doc.check(runs=[(n.get('text', ''), {}),
                            (f'  [P · {n.get("requested_by") or "the person"}]', {'italic': True})])

    # Already listed above, with what becomes of each in the new vessel.
    if phys and not translation and 'settings' not in omit:
        doc.heading('Bioreactor settings', 1)
        doc.para('What an operator enters. A setting marked "whole run" holds throughout; '
                 'the others change at the stage named.')
        doc.table(['Setting', 'Value', 'Stage', 'Stands on'],
                  [[i.get('label', ''), _dose(i),
                    'whole run' if i.get('stage_id') == 'all'
                    else (i.get('stage_label') or i.get('stage_id') or '—'),
                    _stands(i)] for i in phys],
                  widths=[3, 2, 2, 2])

    if bench and 'bench' not in omit:
        _bench_section(doc, bench)

    if readouts and 'measure' not in omit:
        doc.heading('What to measure', 1)
        doc.table(['Readout', 'Unit', 'Note'],
                  [[r.get('label') or r.get('readout_id', ''), _v(r.get('unit')),
                    r.get('note') or r.get('kind') or ''] for r in readouts],
                  widths=[2, 1, 3])

    plan = p.get('round_plan')
    if plan and 'round' not in omit:
        doc.heading('The experiment this protocol is for', 1)
        doc.para(f'Round {_v(plan.get("round") or 1)}: {len(plan.get("arms") or [])} arm(s) '
                 f'× {_v(plan.get("replicates"))} replicate(s). The arms and the decision '
                 f'rules were fixed before any result existed, which is what makes the '
                 f'result mean anything.')
        reps = max(1, int(plan.get('replicates') or 1))
        rows, vessel = [], 1
        for a in plan.get('arms') or []:
            setp = ', '.join(f'{k} {v}' for k, v in (a.get('setpoints') or {}).items())
            for rep in range(1, reps + 1):
                rows.append([f'V{vessel}', f'{a.get("arm_id", "")}'
                             + (' (control)' if a.get('control') else ''),
                             str(rep), setp or 'as above'])
                vessel += 1
        if rows:
            doc.table(['Vessel', 'Arm', 'Replicate', 'Differs by'], rows, widths=[1, 2, 1, 3])
        for u in plan.get('unknowns') or []:
            doc.bullet(f'{u.get("question") or u.get("unknown_id", "")}'
                       + (f' — {u.get("why_unresolved")}' if u.get('why_unresolved') else ''))

    # ── the reasoning, after the procedure ──────────────────────────
    if 'testing' not in omit:
        doc.heading('What this run is testing', 1)
        ledger = [r for r in p.get('hypothesis_ledger') or [] if r.get('adopted')]
        if not ledger:
            doc.para('This run adopted no hypothesis: the protocol above is the process as the '
                     'project and the evidence already describe it, with nothing proposed to '
                     'change.')
        for r in ledger:
            lev = r.get('lever') or {}
            doc.para(runs=[(f'{r.get("hypothesis_id", "")} ', {'bold': True}),
                           (r.get('statement') or '', {})])
            doc.bullet(f'Lever: {lev.get("label") or lev.get("parameter_id") or "—"}'
                       f' · {lev.get("direction") or "direction not stated"}'
                       f' · {_v(lev.get("candidate_value"))}'
                       f'{" " + lev["unit"] if lev.get("unit") else ""}'
                       f' · confidence {r.get("confidence") or "not stated"}')

    if edits and (edits.get('values') or edits.get('notes')):
        _edits_section(doc, edits)

    alts = p.get('alternate_plans')
    if alts and alts.get('items') and 'alternates' not in omit:
        _alternates_section(doc, alts)

    if 'letters' not in omit:
        doc.heading('What the letters mean', 1)
        keys = ['reported', 'adapted', 'design_choice', 'candidate', 'gap']
        if any(i.get('from_baseline') for s in stages for i in s.get('inducers') or []):
            keys.insert(3, 'published')
        if edits.get('notes'):
            keys.append('person')
        for key in keys:
            doc.bullet(PROV_WORD[key])

    lims = p.get('limitations') or []
    if lims and 'limitations' not in omit:
        doc.heading('Limitations', 1)
        for l in lims:
            doc.bullet(l if isinstance(l, str) else str(l.get('text') or l))

    doc.heading('Approval', 1)
    doc.para('A person approves this protocol by name before anything is run. Nothing in '
             'BioSense can do it for them, and no part of the web application can either.')
    doc.table(['', ''], [['Reviewed by', ''], ['Date', ''], ['Signature', ''],
                         ['Changes made at the bench', '']], widths=[1, 3])
    doc.caveat(NOT_APPROVED)
    return doc


def _materials_section(doc, fp, phys, bench):
    """What to have ready before day 0: media per stage and every reagent the
    plan puts into them, with the working concentration and its basis.

    From the bench set-up where the run wrote one (a supplier and a catalogue
    number appear only where a paper states them); otherwise from the plan
    itself, which carries no more than the factors and their doses.
    """
    media = (bench or {}).get('media') or []
    reagents = (bench or {}).get('reagents') or []
    rows = _materials_rows(fp, phys)
    if not (media or reagents or rows):
        return
    doc.heading('Materials', 1)
    if media:
        doc.heading('Medium, per stage', 2)
        doc.table(['Stage', 'Base medium', 'Supplements', 'Stands on'],
                  [[m.get('stage_label') or m.get('stage_id', ''),
                    (m.get('base_medium') or '')
                    + (f' ({m["serum"]})' if m.get('serum') else ''),
                    '; '.join(m.get('supplements') or []) or '—',
                    m.get('provenance', '')
                    + (' · ' + ', '.join(s['ref'] for s in m.get('sources') or [])
                       if m.get('sources') else '')] for m in media],
                  widths=[2, 3, 4, 2])
        for m in media:
            if m.get('prepare'):
                doc.bullet(f'{m.get("stage_label") or m.get("stage_id")}: {m["prepare"]}')
    if reagents:
        doc.heading('Reagents', 2)
        doc.table(['Reagent', 'For', 'Working concentration', 'Where from'],
                  [[r.get('name', ''), r.get('role', ''),
                    r.get('working_concentration') or '—',
                    (' '.join(x for x in (r.get('supplier'), r.get('catalogue')) if x)
                     if r.get('sourcing') == 'from_source'
                     else 'your own qualified stock — no source read names one')]
                   for r in reagents], widths=[2, 4, 2, 3])
        for r in reagents:
            bits = [f'{k.title()}: {r[k]}' for k in ('stock', 'storage', 'handling') if r.get(k)]
            if bits:
                doc.bullet(f'{r.get("name")} — ' + ' · '.join(bits))
    if rows and not reagents:
        doc.heading('Factors this plan adds to the medium', 2)
        doc.para('Working concentrations from the plan. No source read for this run names a '
                 'supplier or a stock recipe, so use your own qualified stock and record '
                 'the lot.')
        doc.table(['Factor', 'Working concentration', 'Stage', 'Stands on'], rows,
                  widths=[3, 2, 2, 3])


def _edits_section(doc, e):
    """What the person asked to change, in full, so the approver sees it as theirs."""
    doc.heading('Changes requested after the run', 1)
    doc.para(f'Asked for by {e.get("requested_by") or "the person"}'
             + (f' on {e["created_at"][:10]}' if e.get('created_at') else '')
             + (f': “{e["request"]}”' if e.get('request') else '') + '.')
    if e.get('values'):
        doc.table(['Value', 'Stage', 'Why', 'Reasoned from', 'Confidence'],
                  [[f'{v.get("label") or v["parameter_id"]} = {_v(v["value"])} {v.get("unit") or ""}',
                    v.get('stage_label') or v.get('stage_id') or 'whole run',
                    v.get('rationale') or '', ', '.join(v.get('derived_from') or []),
                    v.get('confidence') or ''] for v in e['values']],
                  widths=[3, 2, 5, 3, 1])
    for n in e.get('notes') or []:
        doc.bullet(f'{n.get("stage_label") or "whole run"}: {n.get("text", "")}')
    doc.caveat(e.get('does_not_change') or '')


def _alternates_section(doc, a):
    """The other plans somebody asked for, as an appendix.

    They are in the document because whoever runs round 1 is the person who
    decides whether any of them is worth the next vessel — but they are after
    it, and they say plainly that round 1 is unchanged.
    """
    doc.heading('Alternate and extended plans', 1)
    doc.para(f'Asked for: “{a.get("request", "")}”')
    doc.caveat(a.get('does_not_change') or '')
    for it in a['items']:
        doc.heading(f'{it["alt_id"]} — {it["title"]}', 2)
        doc.para(it['claim'])
        doc.bullet(f'Why it is worth a vessel: {it["rationale"]}')
        doc.bullet(f'How it differs from the main plan: {it["differs_from_main"]}')
        doc.bullet(f'Confidence: {it["confidence"]} ({it["evidence_basis"]}'
                   + (f'; {", ".join(it["refs"])}' if it.get('refs') else '') + ')')
        doc.bullet(f'What it costs: {it["cost"]}')
        doc.table(['Arm', 'What differs', 'Basis'],
                  [[x.get('arm_id', ''),
                    x.get('label', '')
                    + (f' (on top of {x["on_top_of"]})' if x.get('on_top_of') else '')
                    + ('; ' + ', '.join(f'{k} {v}' for k, v in (x.get('setpoints') or {}).items())
                       if x.get('setpoints') else ''),
                    x.get('basis') or '—'] for x in it.get('arms') or []],
                  widths=[1, 4, 3])
        doc.para('Measure: ' + '; '.join(
            ' '.join(str(y) for y in (r.get('name'), r.get('unit'), r.get('when')) if y)
            for r in it.get('readouts') or []))
        doc.para(f'If {it["decision_rule"]["if"]} — then {it["decision_rule"]["then"]}')
    for x in a.get('not_pursued') or []:
        doc.caveat(f'Asked for, not worked up — {x["what"]}: {x["why"]}')


def _bench_section(doc, b):
    """Handling, the mistakes that ruin the run, consumables and safety.

    The media and reagents are under Materials, before the procedure; the
    handling steps are in their stages. What is left here is what applies to
    the whole run.
    """
    rest = [h for h in b.get('handling') or []
            if not h.get('stage_id')]
    if not (rest or b.get('do') or b.get('dont') or b.get('consumables') or b.get('safety')
            or b.get('limitations') or b.get('note')):
        return
    doc.heading('At the bench', 1)
    if b.get('note'):
        doc.para(b['note'])
    if rest:
        doc.heading('How it is run', 2)
        for h in rest:
            doc.step(f'{h.get("step")}'
                     + (f' ({h["when"]})' if h.get('when') else '')
                     + (f', {h["duration"]}' if h.get('duration') else '')
                     + f' — {h.get("what", "")}')
    for key, title in (('do', 'Do'), ('dont', 'Do not')):
        if b.get(key):
            doc.heading(title, 2)
            for r in b[key]:
                doc.bullet(f'{r.get("rule")} — {r.get("why")}')
    if b.get('consumables'):
        doc.heading('Consumables', 2)
        for x in b['consumables']:
            doc.bullet(x)
    for x in b.get('safety') or []:
        doc.caveat(x)
    for x in b.get('limitations') or []:
        doc.caveat(x)


def _vessel_section(doc, t):
    """The same protocol in the vessel it is actually going into."""
    to = t['to']
    doc.heading(f'In a {to["label"]}', 1)
    doc.para(t['headline'])
    doc.caveat(t['note'])
    rows = [r for st in t.get('stages') or [] for r in st.get('items') or []]
    rows += list(t.get('physical') or [])
    if rows:
        doc.table(['Line', 'Value', 'Per vessel', 'In this vessel'],
                  [[r.get('label') or r.get('parameter_id', ''),
                    (f'{_v(r.get("value"))} {r.get("unit") or ""}'.strip()
                     if r.get('value') is not None else '—'),
                    (f'{_v(r["amount"]["amount"])} {r["amount"]["unit"]}'
                     if r.get('amount') else '—'),
                    f'{r["kind"]}: {r["why"]}'] for r in rows],
                  widths=[3, 2, 2, 5])
    if t.get('per_unit'):
        doc.para(f'Amounts are per {to["vessel"]}. One {t["per_unit"]["label"]} is '
                 f'{t["per_unit"]["vessels"]} times that.')
    doc.heading('What this vessel needs you to decide', 2)
    for d in t.get('decide') or []:
        doc.bullet(f'{d["what"]} — {d["why"]}'
                   + (f' (typical: {d["typical"]})' if d.get('typical') else ''))
    doc.caveat(t['simulator_note'])


def tobytes(protocol, **kw):
    """The Word file."""
    return build(protocol, **kw).tobytes()


def tobytes_pdf(protocol, **kw):
    """The PDF, from the same blocks."""
    from . import pdf as PDF
    p = protocol or {}
    title = kw.get('title') or p.get('title') or 'Production protocol'
    return build(protocol, doc=PDF.Document(title=title), **kw).tobytes()
