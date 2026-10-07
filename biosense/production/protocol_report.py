"""The protocol summary as a report a person can act on, decision first.

Order matters more than completeness here: the person reading wants to know
what to do next and what exactly would be compared, before why. So the report
opens with the decision (at a glance, the hypotheses one line each, what would
be compared stage by stage and what the simulator played), then the protocol
and the next round at the bench, and puts everything that justifies it — every
hypothesis in full, including the ones not adopted, the reasoned starting
values, the limitations — in a supplement.

Pure rendering of an already validated ProtocolSummary: nothing here decides,
computes or infers a value.
"""
from __future__ import annotations

RULED_OUT = ('contradicted', 'superseded', 'rejected')
TAG = {'reported': 'R', 'adapted': 'A', 'design_choice': 'D', 'gap': 'GAP'}
COVERAGE = {'modelled': 'modelled', 'not_modelled': 'NOT MODELLED', 'no_simulator': 'no model',
            'de_novo_ai': 'DE NOVO', 'expert_declared': 'EXPERT-DECLARED'}


def _fmt(v):
    if v is None:
        return '—'
    if isinstance(v, float):
        return f'{v:g}'
    return str(v)


def _unit(u):
    return f' {u}' if u else ''


def lever_text(lv):
    """'GM-CSF (maturation) → 100 ng/mL' from a ledger row's lever."""
    if not lv:
        return '—'
    label = lv.get('label') or lv.get('parameter_id') or 'lever'
    if lv.get('candidate_value') is not None:
        return f'{label} → {_fmt(lv["candidate_value"])}{_unit(lv.get("unit"))}'
    return f'{label} ({lv.get("direction") or "direction only"})'


def played_text(ran):
    """One line per effect the reactor was given."""
    rows = ran.get('effects') or ([ran['genotype']]
                                  if (ran.get('genotype') or {}).get('label') else [])
    parts = []
    for e in rows:
        where = (f'{e["stage_mapped_from"]} (played as {e["stage"]})'
                 if e.get('stage_mapped_from') else e.get('stage') or 'all stages')
        parts.append(f'{e.get("label")} in {where}: growth ×{_fmt(e.get("growth_ratio"))}, '
                     f'differentiation ×{_fmt(e.get("diff_ratio"))}')
    return '; '.join(parts) or 'an assumed effect'


def glance(doc):
    led = doc['hypothesis_ledger']
    changed = sum(1 for st in doc['stages'] for p in st['parameters'] if p['changed'])
    unregistered = sum(1 for r in led if not (r.get('lever') or {}).get('registered', True))
    plan, nx = doc.get('round_plan'), doc.get('next_experiment') or {}
    L = ['## At a glance', '']
    if plan:
        L.append(f'- **Next step: round {plan.get("round") or 1} at the bench.** '
                 f'{plan.get("purpose", "")} ({len(plan.get("arms") or [])} arms, one of them '
                 f'the control, {plan.get("replicates")} replicate(s) each.)')
    elif nx.get('summary'):
        L.append(f'- **Next step.** {nx["summary"]}')
    L.append(f'- **Hypotheses.** {len(led)} formed; {sum(r["adopted"] for r in led)} in the '
             f'protocol; {unregistered} name a lever this project does not have yet; '
             f'{sum(1 for r in led if r["status"] in RULED_OUT)} ruled out.')
    L.append(f'- **Protocol.** {changed} setpoint(s) changed; '
             f'{len(doc.get("design_choices") or [])} reasoned starting value(s); '
             f'{len(doc.get("gaps") or [])} gap(s)'
             + (' — blocks the wet lab.' if doc['blocks_wet_lab'] else '.'))
    ran = doc.get('simulation_ran')
    L.append('- **Simulator.** ' + (f'played {played_text(ran)} — an assumption, not a '
                                    f'prediction.' if ran else
                                    'nothing was played through the reactor in this run.'))
    return L + ['']


def hypotheses_glance(doc):
    led = doc['hypothesis_ledger']
    if not led:
        return []
    L = ['## Hypotheses at a glance', '',
         'The lever each one changes, where, and whether the protocol carries it. Full '
         'statements and reasons are in the supplement.', '',
         '| ID | Changes | Stage | Confidence | Status | In the protocol |',
         '|---|---|---|---|---|---|']
    for r in led:
        lv = r.get('lever') or {}
        carried = 'yes' if r['adopted'] else (
            'no — lever not registered yet' if not lv.get('registered', True) else 'no')
        L.append(f'| {r["hypothesis_id"]} | {lever_text(lv)} | {lv.get("stage") or "—"} | '
                 f'{r.get("confidence") or "—"} | {r["status"]} | {carried} |')
    return L + ['']


def _plan_arms(plan):
    levers = sorted({k for a in plan.get('arms') or [] for k in (a.get('setpoints') or {})})
    L = ['| Arm | ' + ' | '.join(levers) + ' | Basis |',
         '|---|' + '---|' * len(levers) + '---|']
    for a in plan.get('arms') or []:
        sp = a.get('setpoints') or {}
        cells = [f'{_fmt(sp[k])} [D]' if k in sp else ('current' if a.get('control')
                                                        else 'as control') for k in levers]
        L.append(f'| {a["arm_id"]} {a.get("label", "")}{" (control)" if a.get("control") else ""}'
                 f' | ' + ' | '.join(cells) + f' | {a.get("basis") or ""} |')
    return L


def comparison(doc, is_factor):
    """What would be compared, stage by stage.

    Each hypothesis changes one lever and leaves the rest as the control. When
    the run wrote a round plan, its arms state every setpoint and are the regime
    that would actually be run; combinations stated only in a hypothesis's prose
    are pointed to, never guessed.
    """
    stages = [(s['stage_id'], s['label']) for s in (doc.get('timeline') or {}).get('stages') or []]
    live = [r for r in doc['hypothesis_ledger']
            if r['status'] not in RULED_OUT and r.get('lever')]
    plan = doc.get('round_plan')
    if not stages or not (live or plan):
        return []
    L = ['## What would be compared, stage by stage', '']
    if plan:
        L += ['The round plan states every arm exactly; a setpoint it does not list stays as '
              'the control. Every value is a design choice **[D]**.', '']
        L += _plan_arms(plan) + ['']
    if live:
        by_stage = {st['stage_id']: st['parameters'] for st in doc['stages']}
        known = {sid for sid, _ in stages}
        L += ['Each hypothesis changes only its own lever; everything else stays as the control. '
              'Whether one is meant on top of another is stated in its full text in the '
              'supplement' + ('; the round plan above is the regime that would be run.'
                              if plan else '.'), '',
              '| Stage | Control (current process) | '
              + ' | '.join(r['hypothesis_id'] for r in live) + ' |',
              '|---|---|' + '---|' * len(live)]
        for sid, label in stages:
            ctrl = ', '.join(f'{p["label"]} {_fmt(p["control_value"])}{_unit(p.get("unit"))}'
                             for p in by_stage.get(sid, []) if is_factor(p)) or 'current process'
            cells = [f'**{lever_text(r["lever"])}**' if r['lever'].get('stage') == sid
                     else 'as control' for r in live]
            L.append(f'| {label} | {ctrl} | ' + ' | '.join(cells) + ' |')
        if any(r['lever'].get('stage') not in known for r in live):
            L.append('| Whole process | — | ' + ' | '.join(
                f'**{lever_text(r["lever"])}**' if r['lever'].get('stage') not in known
                else 'as control' for r in live) + ' |')
        L.append('')
    L += ['### What the simulator played', '']
    ran = doc.get('simulation_ran')
    if ran:
        if ran.get('stand_in_note'):
            L += [f'> {ran["stand_in_note"]}', '']
        L.append(f'- {played_text(ran)}.')
        L.append(f'- Assumption: {ran.get("assumption") or "—"}')
        if ran.get('verdict'):
            L.append(f'- Result: {ran["verdict"]}')
        if ran.get('not_represented'):
            L.append(f'- Not represented in the reactor: {", ".join(ran["not_represented"])}')
    else:
        L.append('- Nothing was played through the reactor for these levers in this run.')
    for t in doc.get('proposed_terms') or []:
        d = t.get('description') or {}
        L.append(f'- Proposed term [{d.get("badge", "DE NOVO")}] {t.get("label")}: '
                 f'{d.get("summary", "")}; cites {", ".join(d.get("references") or []) or "—"}.')
    return L + ['']


def protocol(doc, timeline_md, effect_line):
    L = ['## Recommended protocol', '',
         'Provenance: **R** reported · **A** adapted from cited claims · '
         '**D** design choice · **GAP** no evidence.', '']
    for st in doc['stages']:
        L += [f'### {st["label"]}' + (f' — {st["goal"]}' if st.get('goal') else ''), '',
              '| Parameter | Current | Recommended | | Provenance | Simulator |',
              '|---|---|---|---|---|---|']
        for p in st['parameters']:
            L.append(f'| {p["label"]} ({p.get("unit") or ""}) | {_fmt(p["control_value"])} '
                     f'| {_fmt(p["recommended_value"])} | {"**→**" if p["changed"] else ""} '
                     f'| {TAG[p["provenance"]]} '
                     f'| {COVERAGE.get(p["simulator_coverage"], p["simulator_coverage"])} |')
        L.append('')
    L += timeline_md
    if doc['expected_effects']:
        L += ['### Expected effect', ''] + [f'- {effect_line(e)}'
                                            for e in doc['expected_effects']] + ['']
    for t in doc.get('trade_offs') or []:
        L += [f'**Trade-off.** {t.get("summary", "")}', '']
    return L


def next_round(doc):
    plan, nx = doc.get('round_plan'), doc.get('next_experiment') or {}
    if plan:
        L = [f'## Next experiment — round {plan.get("round") or 1} at the bench', '',
             plan.get('purpose', ''), '']
        for u in plan.get('unknowns') or []:
            L.append(f'- **{u["id"]}** {u["question"]} — unresolved because: '
                     f'{u["why_unresolved"]}')
        L += ['', f'Measure: ' + '; '.join(f'{r["name"]} ({r["unit"]}, {r["when"]})'
                                          for r in plan.get('readouts') or [])
              + f'. {plan.get("replicates")} replicate(s) per arm.', '',
              'What the next run does with each outcome (fixed before any result exists'
              + (f'; sha256 {plan["commitment_sha256"][:12]}…' if plan.get('commitment_sha256')
                 else '') + '):', '']
        L += [f'- {r["unknown"]}: if {r["if"]} → {r["then"]}'
              for r in plan.get('decision_rules') or []]
        L += [''] + [f'> {x}' for x in plan.get('limitations') or []]
        L += ['', f'*{plan.get("status", "")}* When results come back, follow this run up with '
                  f'them: the next run reads them against these rules.', '']
        return L
    if nx.get('summary'):
        return ['## Recommended next experiment', '', nx['summary'], '']
    return []


def supplement(doc):
    L = ['## S1. Hypotheses this run formed, in full', '',
         'Including the ones that did not make it. A discarded hypothesis is part of the result.',
         '']
    order = sorted(doc['hypothesis_ledger'],
                   key=lambda r: (r['status'] in RULED_OUT, not r['adopted']))
    for r in order:
        L += [f'### {r["hypothesis_id"]} — {r["status"]}'
              + (' · in the protocol' if r['adopted'] else ''), '',
              r['statement'], '',
              f'- Changes: {lever_text(r.get("lever"))}',
              f'- Confidence: {r.get("confidence") or "—"}; evidence classes: '
              f'{", ".join(r.get("evidence_sources") or []) or "—"}',
              f'- Why {"it is" if r["adopted"] else "not"} in the protocol: {r["reason"]}']
        if r.get('supersedes'):
            L.append(f'- Supersedes: {r["supersedes"]}')
        L.append('')
    if doc.get('design_choices'):
        L += ['## S2. Reasoned starting values', '',
              'Not measurements and not citations: numbers proposed from adjacent practice so the '
              'process can be run at all. A person approves each one.', '',
              '| Parameter | Value | Confidence | Derived from | What would settle it |',
              '|---|---|---|---|---|']
        for c in doc['design_choices']:
            L.append(f'| {c["label"] or c["parameter_id"]} | {c["value"]}{_unit(c.get("unit"))} | '
                     f'{c["confidence"]} | {"; ".join(c["derived_from"])} | '
                     f'{c.get("would_settle_it") or "—"} |')
        L.append('')
    groups = doc.get('limitation_groups')
    if groups:
        L += ['## S3. Limitations', '']
        for g in groups:
            L += [f'### {g["label"]}', ''] + [f'- {x}' for x in g['items']] + ['']
    elif doc.get('limitations'):
        L += ['## S3. Limitations', ''] + [f'- {x}' for x in doc['limitations']] + ['']
    return L
