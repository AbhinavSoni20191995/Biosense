"""The recommendation in plain words: what goes in at each stage, and what each arm changes.

The protocol summary is complete and the round plan is exact, and between them
a scientist who was not in the loop cannot see the experiment. They have to
read a lever id against a stage table against an arm's setpoint dictionary and
hold the whole process in their head. What they asked for is the thing people
draw on a whiteboard before a round:

    the reference recipe — what goes into the vessel at each stage, once;
    then, per arm, only what that arm changes about it.

So this module computes exactly that, and both the page and the exported
document render it. It is pure: every value here already stands in the
validated protocol or the round plan, and nothing is averaged, filled in or
guessed. What it adds is placement and difference — which stage a lever
belongs to, and whether an arm's value differs from the reference — both of
which are arithmetic, not judgement.

Two things it exists to get right, because the old at-a-glance page got them
wrong and a reader could not tell:

**A lever finds its stage.** A project names a parameter `il12_maturation_ng_ml`
and the published route names the factor `IL-12`; the stage is written into the
id. Matching the two by string equality placed no arm change at all, so the
stage went on showing the reference dose while the arm's real change was
printed at the bottom of the page as an unplaced id. `place()` reads the stage
out of the id and matches the rest against the lines that stage carries.

**An arm shows what it changes and nothing else.** Printing the whole recipe in
every arm's column made five identical columns in which the one real difference
was invisible, and made it look as though each arm set every value. The recipe
is the control's, printed once; an arm is the control plus its own differences,
and `changes()` is that list.

A setpoint equal to the control's value is not a change and is not listed: an
arm whose every setpoint matches the control changes nothing, which is said in
those words rather than drawn as an empty row.
"""
from __future__ import annotations

import re

from . import first_pass as FP

# A factor is written with the Greek letter spelled out or abbreviated, and the
# two spellings are one name: "TGF-beta1" is the factor "tgfb1_ng_ml" sets,
# "IL-1alpha" is "il1a", "PPARgamma" is "pparg". Only a whole word is replaced
# (nothing before it, no letter after it), so "betaine" and "alphabet" are left
# alone. This widens what a setpoint can match and narrows nothing.
GREEK = {'alpha': 'a', 'beta': 'b', 'gamma': 'g', 'delta': 'd', 'epsilon': 'e',
         'zeta': 'z', 'theta': 't', 'kappa': 'k', 'lambda': 'l', 'sigma': 's',
         'omega': 'w'}
GREEK_RE = re.compile('(' + '|'.join(GREEK) + ')(?![a-z])', re.I)

# Suffixes a project writes into a parameter id that say the unit, not the
# thing. `first_pass._norm` already strips these; they are repeated here
# because the stage has to be read out of what is left.
UNIT_SUFFIX = re.compile(
    r'_(ng_ml|ug_ml|mg_ml|um|nm|mm|pct|percent|h|hr|hours|days?|c|degc|rpm|'
    r'e6_per_ml|per_ml|fraction|uM|nM)$', re.I)
# Words that are in a stage name but name no stage on their own, so they never
# decide which stage a lever speaks about.
STAGE_STOPWORDS = frozenset({'and', 'or', 'the', 'of', 'stage', 'phase', 'harvest', 'all'})


def _norm(text):
    """One key per real thing: 'M-CSF', 'mcsf_ng_ml' and 'M-CSF concentration' agree."""
    return FP._norm(text)


def _ids(text):
    """Every key one name answers to: itself, normalised, bracketed parts, Greek spellings."""
    text = str(text or '')
    if not text:
        return set()
    out = {text}
    parts = [text] + re.findall(r'\(([^()]*)\)', text) + [re.sub(r'\([^()]*\)', ' ', text)]
    for part in list(parts):
        swapped = GREEK_RE.sub(lambda m: GREEK[m.group(1).lower()], part)
        if swapped != part:
            parts.append(swapped)
    for part in parts:
        key = _norm(part)
        if key:
            out.add(key)
    out.discard('')
    return out


def _stage_words(stage):
    """The words that name one stage: its id, and each word of its label."""
    words = set()
    for raw in (stage.get('stage_id'), stage.get('label')):
        for word in re.split(r'[^A-Za-z0-9]+', str(raw or '')):
            key = _norm(word)
            if key and key not in STAGE_STOPWORDS:
                words.add(key)
    return words


def split_stage(lever, stages):
    """('il12_maturation_ng_ml', stages) -> ('maturation', 'il12').

    A project writes the stage into a parameter id, because one factor is a
    different lever in two stages. The published route names the factor alone.
    Reading the stage out of the id is what lets the two meet: the stage is the
    part that names a stage of *this* protocol, and the rest is the factor.

    Returns (stage_id or None, factor key). The factor key is normalised; where
    nothing in the id names a stage, it is the whole id normalised, which is
    what the old matching used and still works for an unqualified name.
    """
    raw = UNIT_SUFFIX.sub('', str(lever or ''))
    tokens = [t for t in re.split(r'[^A-Za-z0-9]+', raw) if t]
    by_word = {}
    for s in stages:
        for word in _stage_words(s):
            by_word.setdefault(word, s.get('stage_id'))
    stage_id, kept = None, []
    for token in tokens:
        key = _norm(token)
        # Only one token is spent on the stage: a lever named for two stages is
        # not a thing, and spending two would eat the factor out of
        # `rocki_hours_expansion_expansion`-shaped ids.
        if stage_id is None and key in by_word:
            stage_id = by_word[key]
            continue
        kept.append(token)
    factor = _norm('_'.join(kept)) if kept else ''
    return stage_id, (factor or _norm(raw))


def line_keys(item):
    """Every name one stage line answers to, so an arm's setpoint finds it."""
    return _ids(item.get('parameter_id')) | _ids(item.get('label'))


def stages_of(protocol):
    """[{stage_id, label, day_start, days, items}] — the process, stage by stage.

    The lines are the first pass's: the protocol's own parameters, plus the
    published route's factors where the protocol carries none, each with the
    provenance it already had. Physical settings that belong to no one stage
    are a stage of their own at the end, called what they are.
    """
    p = protocol or {}
    fp = p.get('first_pass') or FP.plan(p)
    out = []
    for s in fp.get('stages') or []:
        out.append({'stage_id': s['stage_id'], 'label': s.get('label') or s['stage_id'],
                    'day_start': s.get('day_start') or 0, 'days': s.get('days'),
                    'items': list(s.get('inducers') or [])})
    whole = [i for i in (fp.get('physical') or []) if (i.get('stage_id') or 'all') == 'all']
    staged = {}
    for i in fp.get('physical') or []:
        sid = i.get('stage_id') or 'all'
        if sid != 'all':
            staged.setdefault(sid, []).append(i)
    for s in out:
        s['items'] += staged.get(s['stage_id'], [])
    out += [{'stage_id': 'all', 'label': 'Every stage', 'day_start': None, 'days': None,
             'items': whole + list(fp.get('unstaged') or [])}] if whole or fp.get('unstaged') else []
    return out


def place(levers, stages):
    """{lever: (stage_id, index)} — which line each lever speaks about.

    Decided once for the whole plan, so every arm reads the same line as the
    same lever. Tried in order, and the order is the point: an exact id beats a
    name match, and a lever whose id names a stage is matched inside that stage
    before anywhere else — otherwise `il12_maturation_ng_ml` would bind to an
    IL-12 line in differentiation and the arm's change would be drawn in the
    wrong stage.

    A lever that matches nothing is absent from the result, and the caller says
    so rather than inventing a stage for it.
    """
    by_stage = {s['stage_id']: s['items'] for s in stages}
    order = [s['stage_id'] for s in stages]
    placed = {}
    for lever in levers:
        hint, factor = split_stage(lever, stages)
        mine = _ids(lever)
        found = None
        # 1. the id as written, in the stage it names first.
        search = ([hint] if hint in by_stage else []) + [s for s in order if s != hint]
        for sid in search:
            for n, item in enumerate(by_stage.get(sid) or []):
                if mine & line_keys(item):
                    found = (sid, n)
                    break
            if found:
                break
        # 2. the factor alone, inside the stage the id names.
        if found is None and factor and hint in by_stage:
            for n, item in enumerate(by_stage[hint]):
                if factor in {_norm(k) for k in line_keys(item)}:
                    found = (hint, n)
                    break
        # 3. the factor alone, anywhere, but only where exactly one line
        #    answers to it: two candidates and no stage to choose between them
        #    is not a placement, it is a guess.
        if found is None and factor:
            hits = [(sid, n) for sid in order
                    for n, item in enumerate(by_stage.get(sid) or [])
                    if factor in {_norm(k) for k in line_keys(item)}]
            if len(hits) == 1:
                found = hits[0]
        if found is not None:
            placed[lever] = found
    return placed


def _num(v):
    if isinstance(v, float):
        return f'{v:g}'
    return '' if v is None else str(v)


def value_of(item, round_mode):
    """What a control vessel gets for one line: (value, provenance letter).

    In a round the control runs the current process, so a line this run changed
    shows what it was ("current"); the new value is what an arm sets, if one
    does.
    """
    if round_mode and item.get('was') is not None:
        return item['was'], 'current'
    return item.get('value'), item.get('letter') or '?'


def _same(a, b):
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


# A name long enough to be a sentence is a sentence. An agent proposing a
# lever the project does not carry writes its whole rationale into the label
# ("Activin A concentration across days 1-4 of mesoderm induction (ng/mL). The
# CERO anchor protocol adds no activin A ... not a parameter this project has
# registered."), and a line of the recipe is a name and a dose, not a
# paragraph. So the name is cut at the first sentence and capped; everything
# after it is the description, shown where there is room for it.
NAME_MAX = 80


def _is_unit(text):
    """Whether a parenthetical is only the unit: '(ng/mL)', '(%)', '(um diameter)'.

    A gloss that explains the lever is kept as its description; a unit is
    already a field of its own, so repeating it reads as noise.
    """
    t = str(text or '').strip()
    if not t or len(t) > 18 or '.' in t:
        return False
    if FP.is_factor(t) or FP._is_physical(t):
        return True
    return bool(re.fullmatch(r'[A-Za-z%µ°]+(?:\s?/\s?[A-Za-z%µ°]+)?(?:\s[a-z]+)?', t)
                and ' ' not in t.strip())


def split_name(label):
    """A long label is a name and a description. Return both; drop nothing.

    An agent proposing a lever the project does not carry writes its whole
    rationale into the label, so the name is taken in this order: the first
    sentence, then up to any parenthetical gloss (which is where the unit and
    the "(0 = withdrawn on the day...)" asides live), then the status note
    `first_pass` already knows how to strip, then a word-boundary cap. What
    the name does not take is the description, and the caller shows it where a
    paragraph fits.
    """
    whole = str(label or '').strip()
    if not whole:
        return '', None
    head, rest = whole, ''
    m = re.search(r'(?<=[a-z0-9)\]])\.\s+(?=[A-Z(])', head)
    if m:
        head, rest = head[:m.start()].strip(), head[m.end():].strip()
    # A parenthetical past the first few words is a gloss, not part of the
    # name. A gloss that is only the unit is dropped rather than described:
    # the unit is its own field and prints beside the value.
    paren = head.find(' (')
    if paren > 12:
        close = head.find(')', paren)
        inside = head[paren + 2:close if close != -1 else len(head)].strip()
        after = head[close + 1:].strip() if close != -1 else ''
        gloss = '' if _is_unit(inside) else inside
        rest = '. '.join(x for x in (gloss, after.strip(' ,;:-–—'), rest) if x).strip()
        head = head[:paren].strip()
    head = (FP.split_label(head)[0] or head).strip()
    if len(head) > NAME_MAX:
        cut = head.rfind(' ', 0, NAME_MAX)
        cut = cut if cut > NAME_MAX // 2 else NAME_MAX
        rest = (head[cut:].strip() + ('. ' + rest if rest else '')).strip()
        head = head[:cut].rstrip(' ,;:') + '…'
    rest = str(rest or '').strip(' ,;:-–—.').strip()
    return head, (rest or None)


def item_text(item, value, name=None):
    """'IL-15 10 ng/mL', or what is missing, said as what it is."""
    label = name if name is not None else (item.get('label') or item.get('parameter_id') or '')
    unit = f' {item["unit"]}' if item.get('unit') else ''
    if value is None:
        if item.get('provenance') == 'gap':
            return f'{label} — not decided yet'
        if item.get('from_baseline'):
            return f'{label} — no dose published'
        return f'{label} — not set'
    return f'{label} {_num(value)}{unit}'.strip()


def recipe(protocol):
    """The reference recipe: what goes in at each stage, once, in plain words.

    This is the control arm — the process as it runs now — and it is what every
    arm is read against. Each line says what it is, how much, and where the
    number comes from, and a line nobody has settled says so rather than being
    left blank or filled in.
    """
    p = protocol or {}
    plan = p.get('round_plan') or {}
    round_mode = bool(plan.get('arms'))
    control = next((a for a in plan.get('arms') or [] if a.get('control')), None)
    csp = (control or {}).get('setpoints') or {}
    stages = stages_of(p)
    placed = place(list(csp), stages)
    out = []
    for s in stages:
        lines = []
        for n, item in enumerate(s['items']):
            value, letter = value_of(item, round_mode)
            # A control arm that sets a value explicitly is the reference for
            # that line; usually it sets none and the protocol's own value is.
            for lever, at in placed.items():
                if at == (s['stage_id'], n) and lever in csp:
                    value, letter = csp[lever], 'D'
            name, described = split_name(item.get('label') or item.get('parameter_id'))
            lines.append({
                'parameter_id': item.get('parameter_id'), 'label': item.get('label'),
                'name': name, 'description': described,
                'unit': item.get('unit'), 'value': value, 'letter': letter,
                'provenance': item.get('provenance'),
                'gap': value is None and item.get('provenance') == 'gap',
                'no_dose_published': value is None and bool(item.get('from_baseline')),
                'timing': item.get('timing'), 'why': item.get('why'),
                'text': item_text(item, value, name)})
        out.append({'stage_id': s['stage_id'], 'label': s['label'],
                    'day_start': s['day_start'], 'days': s['days'],
                    'window': window(s), 'lines': lines,
                    # An empty stage is not a stage with nothing in it. It is a
                    # stage this project registered no parameter for, and no
                    # published route filled in — which is a different thing,
                    # and the one a reader has to be told.
                    'empty_because': (None if lines else
                                      'This project has no parameter for this stage and no '
                                      'source read in this run gives one. It is not a stage '
                                      'with nothing in it; it is a stage nothing here '
                                      'describes.')})
    return out


def window(stage):
    """'day 7–17 (10 d)', or 'every stage' for the settings that span the run."""
    if stage.get('days') is None or stage.get('day_start') is None:
        return 'every stage'
    start = stage['day_start']
    return f'day {_num(start)}–{_num(start + stage["days"])} ({_num(stage["days"])} d)'


def changes(protocol):
    """Per arm, only what it changes about the reference, with the stage named.

    An arm is the reference plus this list, and that sentence is the whole
    design of the page. A setpoint equal to the reference is not a change and
    is not listed; an arm with no difference at all is returned with an empty
    list and the caller says so in words.
    """
    p = protocol or {}
    plan = p.get('round_plan') or {}
    arms = list(plan.get('arms') or [])
    if not arms:
        return []
    stages = stages_of(p)
    by_stage = {s['stage_id']: s for s in stages}
    ref = recipe(p)
    ref_lines = {(s['stage_id'], n): line
                 for s in ref for n, line in enumerate(s['lines'])}
    control = next((a for a in arms if a.get('control')), None)
    csp = (control or {}).get('setpoints') or {}
    levers = sorted({k for a in arms for k in (a.get('setpoints') or {})})
    placed = place(levers, stages)
    rules = plan.get('decision_rules') or []

    out = []
    for a in arms:
        sp = a.get('setpoints') or {}
        rows, unplaced = [], []
        for lever in levers:
            if lever not in sp:
                continue
            at = placed.get(lever)
            if at is None:
                # Said as what it is: this page could not find a line for the
                # lever, which is not the same as knowing the protocol has none.
                if not a.get('control') and not _same(sp[lever], csp.get(lever)):
                    unplaced.append({'parameter_id': lever, 'to': sp[lever],
                                     'from': csp.get(lever)})
                continue
            line = ref_lines.get(at) or {}
            was = line.get('value')
            if _same(sp[lever], was):
                continue
            if a.get('control'):
                continue      # the control is the reference; it changes nothing
            stage = by_stage.get(at[0]) or {}
            unit = f' {line["unit"]}' if line.get('unit') else ''
            label = line.get('name') or line.get('label') or lever
            rows.append({
                'parameter_id': lever, 'label': label, 'unit': line.get('unit'),
                'stage_id': at[0], 'stage_label': stage.get('label') or at[0],
                'window': window(stage), 'from': was, 'to': sp[lever],
                'text': (f'{label}: {_num(was) + unit if was is not None else "not used"}'
                         f' → {_num(sp[lever])}{unit}'),
                'short': f'{label} {_num(sp[lever])}{unit}'.strip()})
        aid = str(a.get('arm_id', ''))
        tests = sorted({r.get('unknown') for r in rules
                        if r.get('unknown') and re.search(rf'\b{re.escape(aid)}\b',
                                                          f'{r.get("if", "")} {r.get("then", "")}')})
        out.append({
            'arm_id': aid, 'label': a.get('label') or aid, 'control': bool(a.get('control')),
            'basis': a.get('basis'), 'changes': rows, 'unplaced': unplaced, 'tests': tests,
            'summary': ('The reference: the current process, unchanged. Every other arm is '
                        'read against it.' if a.get('control') else
                        '; '.join(r['short'] for r in rows) if rows else
                        'Every setpoint this arm names already matches the reference, so it '
                        'changes nothing on this page.')})
    return out


def key_points(protocol, narrative=None):
    """Three to six short sentences: what this round does, in plain words.

    The communication agent's own key points when it wrote some — it is the
    agent whose job is plain language, and its numbers were checked against the
    protocol. Otherwise they are assembled here from the plan itself, which is
    arithmetic over values the protocol already carries.
    """
    written = [s for s in ((narrative or {}).get('key_points') or []) if str(s).strip()]
    if written:
        return {'source': 'communication_agent', 'points': [str(s).strip() for s in written[:6]]}
    p = protocol or {}
    plan = p.get('round_plan') or {}
    arms = list(plan.get('arms') or [])
    reps = plan.get('replicates')
    total = (p.get('timeline') or {}).get('total_days')
    rows = changes(p)
    varied = [r for r in rows if not r['control']]
    points = []
    if arms and isinstance(reps, int):
        points.append(f'{len(arms)} arms × {reps} replicates = {len(arms) * reps} vessels'
                      + (f', {_num(total)} days from seeding to harvest.' if total else '.'))
    stages = [s for s in stages_of(p) if s['stage_id'] != 'all']
    if stages:
        points.append('The process runs ' + ', then '.join(
            f'{s["label"].lower()} ({window(s)})' for s in stages) + '.')
    if varied:
        levers = sorted({c['label'] for a in varied for c in a['changes']})
        if levers:
            points.append('Everything is held the same across arms except '
                          + _join(levers) + '.')
        points.append('Each arm is the reference recipe with its own change: '
                      + '; '.join(f'{a["arm_id"]} — {a["summary"]}' for a in varied[:4])
                      + ('.' if len(varied) <= 4 else '; and so on.'))
    gaps = p.get('gaps') or []
    if gaps:
        points.append(('One value is' if len(gaps) == 1 else f'{len(gaps)} values are')
                      + ' not decided yet, and the round cannot start until somebody decides '
                      + ('it: ' if len(gaps) == 1 else 'them: ')
                      + _join(sorted(g.get('parameter_id', '') for g in gaps)) + '.')
    readouts = plan.get('readouts') or []
    if readouts:
        points.append('What decides the result: '
                      + _join([r.get('name') or '' for r in readouts[:3]])
                      + (f', and {len(readouts) - 3} more.' if len(readouts) > 3 else '.'))
    # Always last, and never crowded out by the points above it: a reader who
    # stops after the summary must still have read this one.
    return {'source': 'computed',
            'points': points[:5] + ['Nothing here is approved. A named person approves the '
                                    'protocol before any of it is run.']}


def _join(items):
    items = [str(i) for i in items if str(i).strip()]
    if not items:
        return ''
    if len(items) == 1:
        return items[0]
    return ', '.join(items[:-1]) + ' and ' + items[-1]


def build(protocol, *, narrative=None):
    """The whole plain plan: the recipe, what each arm changes, and the key points.

    One shape, read by the page and by the exported document, so the two
    cannot disagree about what the experiment is.
    """
    p = protocol or {}
    plan = p.get('round_plan') or {}
    arms = list(plan.get('arms') or [])
    reps = plan.get('replicates')
    rows = changes(p)
    control = next((a for a in rows if a['control']), None)
    return {
        'kind': 'plain_plan',
        'title': p.get('title'),
        'objective': p.get('objective'),
        'round': plan.get('round') or 1,
        'status': 'proposed_not_approved',
        'vessels': (len(arms) * int(reps)) if arms and isinstance(reps, int) else None,
        'arms_count': len(arms), 'replicates': reps,
        'total_days': (p.get('timeline') or {}).get('total_days'),
        'reference_arm': (control or {}).get('arm_id'),
        'key_points': key_points(p, narrative),
        'recipe': recipe(p),
        'arms': rows,
        'measure': [{'name': r.get('name'), 'unit': r.get('unit'), 'when': r.get('when'),
                     'why': r.get('why')} for r in plan.get('readouts') or []],
        'decide_first': [{'parameter_id': g.get('parameter_id'), 'why': g.get('why')}
                         for g in p.get('gaps') or []],
        'note': ('The reference recipe is the current process, written out once. Each arm is '
                 'that recipe with the change(s) listed against it, and nothing else. Every '
                 'value here is the protocol\'s own; this page computes which stage a change '
                 'belongs to and whether it differs from the reference, and nothing more.'),
    }
