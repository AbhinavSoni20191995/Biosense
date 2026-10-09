"""The recommended protocol as a first-pass plan a person can read at a glance.

The protocol document is complete but long: every parameter, every hypothesis,
every limitation. Whoever takes it to the bench wants to see three things first:
which inducers go into each stage, at what dose, and which physical settings the
run uses. This module lays the same document out that way, stage by stage.

It is pure rendering of an already validated ProtocolSummary. Nothing here
decides, computes or invents a value:

* an inducer the protocol carries shows its protocol value and provenance;
* a lever a live hypothesis named that the project does not have yet is shown
  as NEW, with the hypothesis's own dose or "dose not set" — never a guessed
  one — and the hypothesis it comes from;
* a ruled-out hypothesis contributes nothing.
"""
from __future__ import annotations

import re

RULED_OUT = ('contradicted', 'superseded', 'rejected')
MOLAR = ('pM', 'nM', 'uM', 'µM', 'mM', 'M')
# Units that name a physical setting rather than something added to the medium.
PHYSICAL_UNITS = {'rpm', 'degc', '°c', 'c', 'h', 'hours', 'hr', 'd', 'day', 'days',
                  'fraction', 'fraction of volume', 'fraction of working volume',
                  'cells/ml', '1e6 cells/ml', 'e6 cells/ml', 'um diameter', 'ml', 'l'}
VERB = {'add': 'Add', 'raise': 'Raise', 'lower': 'Lower', 'set': 'Set', 'keep': 'Keep',
        'gap': 'Set'}
LETTER = {'reported': 'R', 'adapted': 'A', 'design_choice': 'D', 'gap': 'GAP',
          'candidate': 'NEW'}


def _num(v):
    if isinstance(v, float):
        return f'{v:g}'
    return '' if v is None else str(v)


def is_factor(unit):
    """Something given in the medium: a molar or per-volume concentration."""
    u = (unit or '').strip()
    return u in MOLAR or (u.lower().endswith(('/ml', '/l')) and 'cell' not in u.lower())


def _is_physical(unit):
    return (unit or '').strip().lower() in PHYSICAL_UNITS


_TRAILING_UNIT = re.compile(r'\s*\(\s*([^()]*?(?:/|\b(?:%|[pnuµm]M|rpm|h|days?|degC)\b)[^()]*?)'
                            r'\s*\)\s*$')


def split_label(label):
    """'CNTF concentration in maturation medium (ng/mL) - CANDIDATE PARAMETER, not
    registered in project neuron' -> ('CNTF concentration in maturation medium', 'ng/mL').

    Agents write the unit and a status note into the label; the plan shows the
    name, and takes the unit from the label only when the hypothesis gave none."""
    s = str(label or '').strip()
    s = re.split(r'\s+[-–—]\s+(?=[A-Z]{3,}|not\b|candidate\b)', s, maxsplit=1)[0].strip()
    m = _TRAILING_UNIT.search(s)
    if m:
        return s[:m.start()].strip() or s, m.group(1).strip()
    return s or str(label or ''), None


def short_label(label, unit=None):
    return split_label(label)[0]


def _match_stage(stage, stages):
    """The project stage a hypothesis's own stage word names, loosely."""
    if not stage:
        return None
    w = str(stage).lower()
    for s in stages:
        if s['stage_id'] == stage:
            return s['stage_id']
    for s in stages:
        hay = f'{s["stage_id"]} {s.get("label") or ""}'.lower()
        if w in hay or s['stage_id'].lower() in w:
            return s['stage_id']
    return None


def _action(q):
    if q['provenance'] == 'gap':
        return 'gap'
    if not q.get('changed'):
        return 'keep'
    was, now = q.get('control_value'), q.get('recommended_value')
    if not was and isinstance(now, (int, float)) and now:
        return 'add'
    if isinstance(was, (int, float)) and isinstance(now, (int, float)):
        return 'raise' if now > was else 'lower' if now < was else 'set'
    return 'set'


def _confidence(q, ledger):
    if q.get('design_choice'):
        return q['design_choice'].get('confidence')
    if q.get('hypothesis_ref'):
        return ledger.get(q['hypothesis_ref'])
    return None


def action_text(action, label, value=None, unit=None, stage_label=None, dose_known=True):
    """'Add CNTF at 20 ng/mL in Maturation' — one line a person can act on."""
    s = f'{VERB.get(action, "Set")} {label}'
    if value is not None and value != '':
        s += f' {"to" if action in ("raise", "lower", "set") else "at"} {_num(value)}' \
             + (f' {unit}' if unit else '')
    elif not dose_known:
        s += ' (dose not set by this run)'
    if stage_label:
        s += f' in {stage_label}'
    return s


def lever_action(lever, stage_labels=None):
    """The action a hypothesis's lever asks for, in the same words as the plan."""
    if not lever:
        return None
    d = lever.get('direction')
    action = 'lower' if d == 'decrease' else 'add' if lever.get('registered') is False else (
        'raise' if d == 'increase' else 'set')
    stage = lever.get('stage')
    label, unit_in_label = split_label(lever.get('label') or lever.get('parameter_id'))
    return action_text(action, label, lever.get('candidate_value'),
                       lever.get('unit') or unit_in_label,
                       (stage_labels or {}).get(stage, stage),
                       dose_known=lever.get('candidate_value') is not None)


def plan(doc):
    """{stages: [{..., inducers}], unstaged: [...], physical: [...]} from a protocol."""
    tl = doc.get('timeline') or {}
    stages = [dict(s, inducers=[]) for s in tl.get('stages') or []]
    by_id = {s['stage_id']: s for s in stages}
    labels = {s['stage_id']: s.get('label') or s['stage_id'] for s in stages}
    ledger_rows = {r['hypothesis_id']: r for r in doc.get('hypothesis_ledger') or []}
    ledger = {k: r.get('confidence') for k, r in ledger_rows.items()}
    physical, unstaged = [], []

    for st in doc.get('stages') or []:
        for q in st.get('parameters') or []:
            action = _action(q)
            item = {
                'parameter_id': q['parameter_id'], 'label': q['label'], 'unit': q.get('unit'),
                'value': None if q['provenance'] == 'gap' else q.get('recommended_value'),
                'was': q.get('control_value') if q.get('changed') else None,
                'action': action, 'provenance': q['provenance'],
                'letter': LETTER.get(q['provenance'], '?'),
                'confidence': _confidence(q, ledger), 'hypothesis_id': q.get('hypothesis_ref'),
                'registered': True, 'stage_id': st['stage_id'],
                'stage_label': labels.get(st['stage_id']) or st.get('label'),
            }
            item['text'] = action_text(action, q['label'], item['value'], q.get('unit'),
                                       dose_known=q['provenance'] != 'gap')
            if is_factor(q.get('unit')) and st['stage_id'] in by_id:
                by_id[st['stage_id']]['inducers'].append(item)
            elif is_factor(q.get('unit')):
                unstaged.append(item)
            else:
                physical.append(item)

    for c in tl.get('candidates') or []:
        row = ledger_rows.get(c.get('hypothesis_id')) or {}
        if row.get('status') in RULED_OUT:
            continue
        lever = row.get('lever') or {}
        sid = c.get('stage_id') or _match_stage(lever.get('stage'), stages)
        label, unit_in_label = split_label(c.get('label'))
        unit = c.get('unit') or unit_in_label
        action = 'lower' if c.get('direction') == 'decrease' else 'add'
        item = {
            'parameter_id': c['parameter_id'], 'label': label, 'unit': unit,
            'value': c.get('candidate_value'), 'was': None, 'action': action,
            'provenance': 'candidate', 'letter': 'NEW', 'confidence': c.get('confidence'),
            'hypothesis_id': c.get('hypothesis_id'), 'registered': False, 'stage_id': sid,
            'stage_label': labels.get(sid),
            'text': action_text(action, label, c.get('candidate_value'), unit,
                                dose_known=c.get('candidate_value') is not None),
        }
        if _is_physical(unit):
            physical.append(item)
        elif sid in by_id:
            by_id[sid]['inducers'].append(item)
        else:
            unstaged.append(item)

    order = {'add': 0, 'raise': 1, 'lower': 2, 'set': 3, 'gap': 4, 'keep': 5}
    for s in stages:
        s['inducers'].sort(key=lambda i: (order.get(i['action'], 9), i['label']))
    physical.sort(key=lambda i: (i['stage_id'] != 'all', order.get(i['action'], 9), i['label']))
    return {
        'stages': stages, 'unstaged': unstaged, 'physical': physical,
        'changes': sum(1 for s in stages for i in s['inducers'] if i['action'] != 'keep')
                   + sum(1 for i in unstaged + physical if i['action'] != 'keep'),
        'note': ('A first pass: the protocol\'s own values stage by stage. NEW marks a lever a '
                 'live hypothesis named that this project does not have yet; it carries that '
                 'hypothesis\'s dose or says none was set, and enters the protocol only once '
                 'it is registered and a person approves it.'),
    }


def markdown(p):
    """The plan as the report's first section."""
    L = ['## First-pass plan', '', p['note'], '']
    for s in p['stages']:
        days = (f'd{_num(s["day_start"])}–{_num(s["day_start"] + s["days"])}'
                if s.get('days') is not None else 'days not set')
        L.append(f'**{s["label"]}** ({days})')
        if not s['inducers']:
            L.append('- No inducer is set for this stage in this run.')
        L += [f'- {i["text"]} [{i["letter"]}'
              + (f', {i["confidence"]}' if i.get('confidence') else '')
              + (f', {i["hypothesis_id"]}' if i.get('hypothesis_id') else '') + ']'
              for i in s['inducers']]
        L.append('')
    if p['unstaged']:
        L += ['**Stage not stated**'] + [f'- {i["text"]} [{i["letter"]}]'
                                         for i in p['unstaged']] + ['']
    if p['physical']:
        L += ['**Physical parameters**'] + [
            f'- {i["text"]}' + (f' ({i["stage_label"]})' if i['stage_id'] != 'all'
                                and i.get('stage_label') else '')
            + f' [{i["letter"]}' + (f', {i["confidence"]}' if i.get('confidence') else '') + ']'
            for i in p['physical']] + ['']
    return L
