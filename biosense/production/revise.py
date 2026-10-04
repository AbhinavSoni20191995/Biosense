"""Deterministic protocol reviser: the optimiser that turns a brief into the next protocol.

This is a bounded, auditable coordinate search with backtracking. It is not a
model, not an inference engine, and not a design-of-experiments package. Given
the parent protocol, the revision brief the analysis produced, the levers the
bioinformatics tools implicated, and the search state carried across iterations,
it does four things:

1. **Scores the moves it made last time.** For every lever it moved, it compares
   the arms that move was meant to help, before and after. A move that helped is
   kept and the lever stays open in the same direction. A move that hurt is
   reverted and that direction is marked exhausted for that scope, so the search
   does not keep walking downhill - which is the failure an open-loop ramp makes.

2. **Detects a conflict between arms.** When one shared move helps one arm and
   hurts another, no shared value can satisfy both. The search reverts the shared
   value and re-issues the lever as separate per-arm adjustments, each stepping in
   the direction that arm's own result asks for. Finding that two genotypes need
   different values of the same parameter is the main thing this search can
   discover, and it is recorded as a finding rather than buried in a diff.

3. **Picks the next lever** from those whose (scope, direction) is not exhausted,
   preferring an arm-scoped move whenever some arms already pass, so a passing
   arm's result is never traded away for a failing one.

4. **Records everything.** Each change lands in `changes_from_parent` with the
   brief and hypothesis it addresses, and each revised quantity becomes a
   `design_choice` whose rationale names the old value, the new value, the lever,
   the lever's own stated basis, and that the number came from a search step.

What it will not do: invent evidence (a searched value is never `reported` or
`adapted`), move the harvest day (the request fixes the day the target is read
at), run past `max_culture_days`, or let a dose leave a band around its baseline.

Honest limits. This is one coordinate at a time on a surface measured with one
replicate per arm, so it is climbing noisy ground and cannot see interactions
between levers. Reverting on a single worse observation will sometimes revert a
good move that noise spoiled. It can find a better recipe; it never establishes
why one works, and it produces no evidence about the biology of any gene.
"""
from __future__ import annotations

import copy
import re

from .. import contracts as K

STEP_UP = 1.6          # multiplicative step for "increase"
STEP_DOWN = 0.55       # multiplicative step for "decrease"
BAND_LO = 0.15         # a dose may not fall below this fraction of its baseline
BAND_HI = 6.0          # nor rise above this multiple of it
DAY_STEP = 3.0         # days added or removed when a window is the lever
# At most one move may touch any given arm in one revision. Arms are separate
# cultures, so a change scoped to one arm cannot affect another arm's measurement
# and several arm-scoped moves in one iteration stay attributable. A shared move
# touches every arm, so it is the only move of that revision. Without this rule
# three simultaneous moves produce one observation per arm and the scorer
# attributes the whole change to each move independently, which is wrong.
MAX_CHANGES = 4        # ceiling regardless; the per-arm rule is the real limit
MIN_REL_GAIN = 0.02    # a move must move the metric 2% to count as having helped

# Lever parameter text -> the protocol factor it refers to. A lever that matches
# nothing is reported as unmapped, never silently dropped.
FACTOR_WORDS = {
    'IL-7': ('il-7', 'il7', 'interleukin-7'),
    'IL-15': ('il-15', 'il15', 'interleukin-15'),
    'IL-2': ('il-2', 'il2', 'interleukin-2'),
    'DLL4': ('dll4', 'dll-4', 'notch', 'delta-like'),
    'SCF': ('scf', 'kit ligand'),
    'FLT3L': ('flt3l', 'flt3'),
    'BMP4': ('bmp4', 'bmp-4'),
    'VEGF': ('vegf',),
    'FGF2': ('fgf2', 'bfgf', 'fgf-2'),
    'TPO': ('tpo', 'thrombopoietin'),
    'anti-CD3': ('anti-cd3', 'cd3', 'tcr', 'activation signal', 'activation strength', 'okt3',
                 'stimulation'),
    'CHIR99021': ('chir', 'wnt'),
    'SB431542': ('sb431542', 'tgf'),
}
DURATION_WORDS = ('duration', 'window', 'length', 'longer', 'shorter', 'timing')
SEED_WORDS = ('seed', 'seeding', 'initial density', 'inoculation')
FEED_WORDS = ('feed', 'feeding', 'medium exchange')
UP = ('increase', 'raise', 'higher', 'lengthen', 'later', 'longer', 'more')
DOWN = ('decrease', 'reduce', 'lower', 'shorten', 'earlier', 'shorter', 'less')
# Levers naming a parameter the loop cannot change from inside a protocol.
UNCHANGEABLE = ('harvest day', 'harvest_day', 'replicate', 'measurement mode')


def _dir(direction):
    d = (direction or '').strip().lower()
    if any(w in d for w in UP):
        return 1
    if any(w in d for w in DOWN):
        return -1
    return 0


def _factor_for(parameter):
    low = (parameter or '').lower()
    best = None
    for name, words in FACTOR_WORDS.items():
        for w in words:
            if w in low and (best is None or len(w) > best[1]):
                best = (name, len(w))
    return best[0] if best else None


def _arm_in(parameter, arm_ids):
    """The arm a lever's text scopes itself to, as the analysis writes it ('... for ARM')."""
    low = (parameter or '').lower()
    for a in arm_ids:
        if re.search(rf'\bfor {re.escape(a.lower())}\b', low):
            return a
    return None


def _steps_for_factor(protocol, factor, stage_id=None):
    out = []
    for st in protocol['stages']:
        if stage_id and st['stage_id'] != stage_id:
            continue
        for s in st['steps']:
            if ((s.get('factor') or '').lower() == factor.lower() and s['action'] == 'add_factor'
                    and s.get('quantity')):
                out.append((st, s))
    return out


def _step_by_id(protocol, step_id):
    for st in protocol['stages']:
        for s in st['steps']:
            if s['step_id'] == step_id:
                return st, s
    return None, None


def _effective(protocol, step_id, arm_id, kind):
    """The value this arm actually runs for a step: the base, overridden by its own
    adjustment if it has one.

    An arm-scoped move must step from here, not from the shared base. Reading the
    base instead silently discards a per-arm value the search already established
    and re-measures a configuration it has left behind.
    """
    st, s = _step_by_id(protocol, step_id)
    if s is None:
        return None
    base = float(s['end_day']) if kind == 'window' else (
        float(s['quantity']['value']) if s.get('quantity')
        and isinstance(s['quantity'].get('value'), (int, float)) else None)
    if arm_id is None or base is None:
        return base
    for a in protocol.get('arm_adjustments', []):
        if a['arm_id'] != arm_id or a['step_id'] != step_id:
            continue
        if kind == 'window' and a.get('day_shift'):
            base = base + float(a['day_shift'])
        elif kind != 'window' and isinstance((a.get('quantity') or {}).get('value'), (int, float)):
            base = float(a['quantity']['value'])
    return base


def _fmt(v):
    return f'{v:g}'


def _band(baseline, value):
    return max(baseline * BAND_LO, min(baseline * BAND_HI, value))


def failing_arms(report):
    return [a['arm_id'] for a in report['arms'] if a['target']['status'] != 'MET']


def passing_arms(report):
    return [a['arm_id'] for a in report['arms'] if a['target']['status'] == 'MET']


def observed(report):
    return {a['arm_id']: a['target']['observed_mean'] for a in report['arms']}


# ── search state ────────────────────────────────────────────

def new_state():
    """The search's memory. The engine persists this beside the loop artifacts."""
    return {'schema': 'biosense.revise.search_state/2',
            'observations': [],      # [{iteration, arms: {arm_id: metric}}]
            'best': {},              # arm_id -> {metric, iteration}: the best reading so far
            'levers': {},            # key -> lever record
            'last_moves': [],        # the moves that produced the current protocol
            'findings': [],          # human-readable search findings, e.g. arm conflicts
            'note': 'Memory of a deterministic coordinate search. Metric values are the request\'s '
                    'own target metric per arm, taken from the analysis reports. Because every move '
                    'that does not beat the best reading is reverted, the protocol in hand is always '
                    'the best configuration found so far.'}


def _key(step_id, arm_id, kind):
    return f'{step_id}|{arm_id or ""}|{kind}'


def record_observation(state, iteration, report):
    """Log this iteration's readings and raise each arm's best.

    Call this AFTER `score_last_moves` for the same iteration: the scorer needs
    the previous best to judge against, and this call is what moves it.
    """
    if any(o['iteration'] == iteration for o in state['observations']):
        return state          # idempotent, so a caller may record defensively
    arms = observed(report)
    state['observations'].append({'iteration': iteration, 'arms': arms})
    best = state.setdefault('best', {})
    for a, v in arms.items():
        if v is None:
            continue
        cur = (best.get(a) or {}).get('metric')
        if cur is None or v > cur:
            best[a] = {'metric': v, 'iteration': iteration}
    return state


def _metric_at(state, iteration):
    for o in state['observations']:
        if o['iteration'] == iteration:
            return o['arms']
    return {}


def score_last_moves(state, report, iteration):
    """Judge the previous revision. Returns (verdicts, reverts, conflicts).

    Each arm is scored against its own BEST reading so far, not against the
    immediately previous one. Scoring against the previous reading makes any step
    out of a bad state look like progress, so the search drifts away from a good
    configuration it already found and never returns - a random walk wearing a
    hill climber's clothes. Comparing to the best, and reverting anything that
    does not beat it, keeps the protocol in hand equal to the best found so far.

    A move's intended arms are the arms it was scoped to, or every arm for a
    shared move. `helped` means every intended arm beat its best by more than
    MIN_REL_GAIN; `hurt` means any arm fell below its best by more than that.
    """
    now = observed(report)
    best = state.setdefault('best', {})
    verdicts, reverts, conflicts = [], [], []
    for mv in state.get('last_moves', []):
        intended = [mv['arm_id']] if mv['arm_id'] else sorted(now)
        deltas = {}
        for a in sorted(now):
            ref = (best.get(a) or {}).get('metric')
            n = now.get(a)
            if ref is None or n is None or ref == 0:
                continue
            deltas[a] = (n - ref) / abs(ref)
        helped = [a for a in intended if deltas.get(a, 0) > MIN_REL_GAIN]
        hurt = [a for a in intended if deltas.get(a, 0) < -MIN_REL_GAIN]
        # A shared move that helps one arm and hurts another is a conflict, not a
        # bad move: both arms want this lever, in opposite directions.
        other_hurt = [a for a in deltas if a not in intended and deltas[a] < -MIN_REL_GAIN]
        rec = state['levers'].setdefault(_key(mv['step_id'], mv['arm_id'], mv['kind']), {
            'step_id': mv['step_id'], 'arm_id': mv['arm_id'], 'kind': mv['kind'],
            'factor': mv['factor'], 'unit': mv['unit'], 'baseline': mv['from'],
            'tried': [], 'exhausted': [], 'best': None})
        rec['tried'].append({'value': mv['to'], 'iteration': iteration, 'deltas': deltas})
        sign = 1 if mv['direction'] in ('increase', 'lengthen') else -1

        if mv['arm_id'] is None and helped and (hurt or other_hurt):
            losers = sorted(set(hurt) | set(other_hurt))
            conflicts.append({'step_id': mv['step_id'], 'factor': mv['factor'], 'kind': mv['kind'],
                              'unit': mv['unit'], 'from': mv['from'], 'to': mv['to'],
                              'helped': helped, 'hurt': losers, 'deltas': deltas})
            reverts.append(dict(mv, revert_to=mv['from'], why='conflict'))
            finding = (f'{mv["factor"]} cannot be set to one shared value: moving it '
                       f'{_fmt(mv["from"])} -> {_fmt(mv["to"])} {mv["unit"]} improved '
                       f'{", ".join(helped)} and degraded {", ".join(losers)}. The arms are being given '
                       f'separate values of this parameter from here on.')
            if finding not in state['findings']:
                state['findings'].append(finding)
            # The shared scope is spent for this lever: it has been split per arm,
            # so a later brief naming it again must not re-try one shared value.
            rec['exhausted'] = sorted({-1, 1})
            verdicts.append({**mv, 'outcome': 'conflict', 'deltas': deltas})
            continue

        if hurt or (not helped and other_hurt):
            rec['exhausted'] = sorted(set(rec['exhausted']) | {sign})
            reverts.append(dict(mv, revert_to=mv['from'], why='made it worse'))
            verdicts.append({**mv, 'outcome': 'worse', 'deltas': deltas})
            continue

        if helped:
            best = rec.get('best')
            score = min(deltas.get(a, 0) for a in intended)
            if best is None or score > best['score']:
                rec['best'] = {'value': mv['to'], 'score': score, 'iteration': iteration}
            verdicts.append({**mv, 'outcome': 'better', 'deltas': deltas})
        else:
            # No material change either way: stop stepping this direction, it is flat.
            rec['exhausted'] = sorted(set(rec['exhausted']) | {sign})
            verdicts.append({**mv, 'outcome': 'flat', 'deltas': deltas})
    return verdicts, reverts, conflicts


def _exhausted(state, step_id, arm_id, kind, sign):
    rec = state['levers'].get(_key(step_id, arm_id, kind))
    return bool(rec) and sign in (rec.get('exhausted') or [])


def _already(state, step_id, arm_id, kind, value):
    rec = state['levers'].get(_key(step_id, arm_id, kind))
    return bool(rec) and any(abs(t['value'] - value) < 1e-9 for t in rec.get('tried', []))


# ── planning ────────────────────────────────────────────────

def _candidate_levers(brief, bioinfo_levers, arm_ids):
    """One ordered, de-duplicated lever list from both sources."""
    out, seen = [], set()
    n_bio = len(list(bioinfo_levers or ()))
    for i, lv in enumerate(list(bioinfo_levers or ()) + list((brief or {}).get('levers', ()))):
        origin = lv.get('origin') or ('bioinformatics' if i < n_bio else 'analysis')
        param = lv.get('parameter') or ''
        key = (param.lower(), lv.get('step_id'), (lv.get('direction') or lv.get('direction_hint')))
        if key in seen:
            continue
        seen.add(key)
        out.append({'parameter': param, 'step_id': lv.get('step_id'), 'stage_id': lv.get('stage_id'),
                    'direction': lv.get('direction') or lv.get('direction_hint'),
                    'basis': lv.get('basis') or '', 'confidence': lv.get('confidence'),
                    'hypothesis_id': lv.get('hypothesis_id'),
                    'arm_scope': lv.get('arm_scope') or _arm_in(param, arm_ids),
                    'origin': origin})
    return out


def fallback_levers(protocol, state):
    """The protocol's own numeric quantities, as a last resort when the implicated
    levers are spent.

    Nothing in the evidence or the annotations points at these parameters. They
    are offered only so that an exhausted search can keep exploring rather than
    stopping while obvious coordinates sit untouched, and every move built from
    one is labelled `unimplicated_search` so a reader can tell blind exploration
    from an evidence-led step. Direction is a guess: more of a growth-supporting
    input is tried first, and the scorer reverts it if that was wrong.
    """
    # Ordering, when nothing points anywhere: the feeding regime first, because it
    # scales the total yield directly, then later stages before earlier ones, since
    # the harvest readout is most sensitive to what happened nearest the harvest.
    # This is a search heuristic about the measurement, not a claim about biology.
    out = []
    n_stages = len(protocol['stages'])
    for depth, st in enumerate(protocol['stages']):
        rank = depth + 1                 # last stage highest, first stage lowest
        for s in st['steps']:
            q = s.get('quantity')
            if (s['action'] not in ('add_factor', 'seed') or not q
                    or not isinstance(q.get('value'), (int, float))):
                continue
            name = s.get('factor') or 'seeding density'
            key_prefix = f'{s["step_id"]}|'
            tried = sum(len(r.get('tried') or []) for k, r in state['levers'].items()
                        if k.startswith(key_prefix))
            out.append(((tried, -rank), {
                'parameter': f'{name} ({st["stage_id"]})', 'step_id': s['step_id'],
                'stage_id': st['stage_id'], 'direction': 'increase',
                'basis': 'NOT IMPLICATED BY ANY EVIDENCE OR ANNOTATION. The levers the analysis and '
                         'the bioinformatics tools named are exhausted, so the search is exploring a '
                         'parameter nothing pointed it at. Treat a gain here as a search result, not '
                         'as a finding about this parameter.',
                'confidence': None, 'hypothesis_id': None, 'arm_scope': None,
                'origin': 'unimplicated_search'}))
    p = protocol['culture_system']['parameters'].get('feed_fraction')
    if p and isinstance(p.get('value'), (int, float)):
        tried = sum(len(r.get('tried') or []) for k, r in state['levers'].items()
                    if k.startswith('culture:feed_fraction|'))
        out.append(((tried, -(n_stages + 1)), {
            'parameter': 'feeding regime (feed_fraction)', 'step_id': None, 'stage_id': None,
            'direction': 'increase',
            'basis': 'NOT IMPLICATED BY ANY EVIDENCE OR ANNOTATION. Explored because the implicated '
                     'levers are exhausted. A fed-batch split changes how much medium the culture '
                     'gets and therefore the total yield, so a gain here says something about the '
                     'vessel, not about the biology.',
            'confidence': None, 'hypothesis_id': None, 'arm_scope': None,
            'origin': 'unimplicated_search'}))
    out.sort(key=lambda r: r[0])
    return [lv for _, lv in out]


def _resolve_target(protocol, lv):
    """Which protocol element a lever points at: (kind, stage, step) or (None, ...)."""
    param = (lv['parameter'] or '').lower()
    if any(w in param for w in UNCHANGEABLE):
        return None, None, None, 'names a parameter a protocol revision cannot change'
    if lv.get('step_id'):
        st, s = _step_by_id(protocol, lv['step_id'])
        if s is not None and s.get('quantity') and isinstance(s['quantity'].get('value'), (int, float)):
            kind = 'window' if any(w in param for w in DURATION_WORDS) and s.get('end_day') is not None \
                else 'dose'
            return kind, st, s, None
    factor = _factor_for(lv['parameter'])
    if factor:
        hits = _steps_for_factor(protocol, factor, lv.get('stage_id'))
        if not hits:
            return None, None, None, f'no {factor} step with a numeric quantity in this protocol'
        st, s = hits[-1]
        if any(w in param for w in DURATION_WORDS) and s.get('end_day') is not None:
            return 'window', st, s, None
        return 'dose', st, s, None
    if any(w in param for w in FEED_WORDS):
        return 'culture', None, None, None
    if any(w in param for w in SEED_WORDS):
        s = next((x for st in protocol['stages'] for x in st['steps']
                  if x['action'] == 'seed' and x.get('quantity')), None)
        if s is None:
            return None, None, None, 'no numeric seed step'
        return 'dose', protocol['stages'][0], s, None
    if any(w in param for w in DURATION_WORDS):
        return None, None, None, ('names a duration but no factor window; the harvest day is fixed by '
                                  'the request, so there is nothing here to move')
    return None, None, None, 'no protocol step, culture parameter or window matched this lever'


def _admit(moves, mv, arm_ids, max_changes):
    """Add `mv` only if it keeps the one-move-per-arm rule. Returns True if added.

    A shared move (arm_id None) touches every arm, so it may only be the sole
    move of the revision. An arm-scoped move touches one arm, so one per arm is
    allowed. This is what keeps each observation attributable to one change.
    """
    if len(moves) >= max_changes:
        return False
    touched = set()
    for m in moves:
        touched |= set(arm_ids) if m['arm_id'] is None else {m['arm_id']}
    want = set(arm_ids) if mv['arm_id'] is None else {mv['arm_id']}
    if touched & want:
        return False
    moves.append(mv)
    return True


def plan_changes(protocol, report, request, brief, bioinfo_levers=(), state=None,
                 max_changes=MAX_CHANGES):
    """Decide what to change, changing nothing. Returns the plan for a caller to inspect."""
    state = state or new_state()
    iteration = protocol['iteration']
    arm_ids = [a['arm_id'] for a in protocol['genotype_arms']]
    failing, passing = failing_arms(report), passing_arms(report)
    # Score against the previous best, then raise it. This order matters.
    verdicts, reverts, conflicts = score_last_moves(state, report, iteration)
    record_observation(state, iteration, report)

    moves, unmapped = [], []
    # A conflict is re-issued immediately as per-arm moves, in each arm's own
    # direction. This takes priority over anything the brief suggests next.
    for c in conflicts:
        st, s = _step_by_id(protocol, c['step_id'])
        # The shared move went in direction `moved`. An arm that improved wants
        # more of that direction; an arm that got worse wants the opposite. Taking
        # the sign of the arm's own delta instead would send the arm that liked a
        # decrease back upwards, which is the opposite of what it asked for.
        moved = 1 if c['to'] > c['from'] else -1
        for arm in arm_ids:
            d = c['deltas'].get(arm, 0.0)
            improved = d > 0
            sign = moved if improved else -moved
            # An arm that improved already has a reading at c['to'], so re-applying
            # that value per arm would spend an iteration measuring the same thing.
            # Step on from there instead. An arm that got worse steps back from the
            # shared value that preceded the move, which is its own best.
            cur = c['to'] if improved else c['from']
            if c['kind'] == 'window':
                new = max(s['day'] + 1.0, min(st['end_day'], cur + DAY_STEP * sign))
            else:
                new = _band(c['from'], cur * (STEP_UP if sign > 0 else STEP_DOWN))
            if abs(new - cur) < 1e-9 or _already(state, c['step_id'], arm, c['kind'], new):
                continue
            _admit(moves, {
                'kind': c['kind'], 'step_id': c['step_id'],
                'stage_id': st['stage_id'] if st else None, 'factor': c['factor'], 'arm_id': arm,
                'unit': c['unit'], 'from': cur, 'to': round(new, 4),
                'direction': ('increase' if sign > 0 else 'decrease') if c['kind'] == 'dose'
                             else ('lengthen' if sign > 0 else 'shorten'),
                'parameter': f'{c["factor"]} for {arm}', 'origin': 'search',
                'basis': f'Split from a shared value after it improved {", ".join(c["helped"])} and '
                         f'degraded {", ".join(c["hurt"])}. Each arm now moves in the direction its own '
                         f'result asked for.',
                'confidence': None, 'hypothesis_id': None, 'from_iteration': iteration,
            }, arm_ids, max_changes)

    # Resolve every lever onto a protocol element first, then take the least-tried
    # one. A coordinate search cycles through its coordinates; walking one lever to
    # exhaustion before touching the next wastes iterations on a parameter the
    # readout is barely sensitive to while an untouched lever carries the gain.
    def _resolve_all(levers, collect_unmapped):
        out = []
        for lv in levers:
            sign = _dir(lv['direction'])
            if sign == 0:
                if collect_unmapped:
                    unmapped.append({'parameter': lv['parameter'],
                                     'why': 'lever gives no usable direction'})
                continue
            kind, st, s, why = _resolve_target(protocol, lv)
            if kind is None:
                if collect_unmapped:
                    unmapped.append({'parameter': lv['parameter'], 'why': why})
                continue
            sid = ('culture:feed_fraction' if kind == 'culture' else s['step_id'])
            attempts = sum(len(rec.get('tried') or [])
                           for key, rec in state['levers'].items() if key.startswith(f'{sid}|'))
            out.append((attempts, len(out), lv, sign, kind, st, s, sid))
        out.sort(key=lambda r: (r[0], r[1]))
        return out

    def _plan_one(lv, sign, kind, st, s, scopes):
        """Emit at most one move for this lever, for each scope offered."""
        if len(moves) >= max_changes or not scopes:
            return
        if kind == 'culture' and any(sc is not None for sc in scopes):
            unmapped.append({'parameter': lv['parameter'],
                             'why': 'feed_fraction is a shared culture parameter; this contract has no '
                                    'way to set it per arm, and some arms already pass'})
            return

        for scope in scopes:
            if len(moves) >= max_changes:
                break
            if kind == 'culture':
                sid = 'culture:feed_fraction'
                p = protocol['culture_system']['parameters'].get('feed_fraction')
                if not p or not isinstance(p.get('value'), (int, float)):
                    unmapped.append({'parameter': lv['parameter'],
                                     'why': 'feed_fraction is not a numeric culture parameter here'})
                    break
                cur, unit = float(p['value']), p['unit']
            elif kind == 'window':
                sid, unit = s['step_id'], 'day'
                cur = _effective(protocol, sid, scope, kind)
            else:
                sid, unit = s['step_id'], s['quantity']['unit']
                cur = _effective(protocol, sid, scope, kind)
            if cur is None:
                continue

            # Try the lever's own direction; if that is spent for this scope, try
            # the other way before giving up. The flip is the search's own idea,
            # and the move says so rather than crediting it to the annotation.
            chosen = None
            for use_sign, flipped in ((sign, False), (-sign, True)):
                if _exhausted(state, sid, scope, kind, use_sign):
                    continue
                if kind == 'culture':
                    new = round(min(0.9, max(0.1, cur * (STEP_UP if use_sign > 0 else STEP_DOWN))), 3)
                elif kind == 'window':
                    new = max(s['day'] + 1.0, min(st['end_day'], cur + DAY_STEP * use_sign))
                else:
                    rec = state['levers'].get(_key(sid, scope, kind))
                    base = rec['baseline'] if rec else cur
                    new = _band(base, cur * (STEP_UP if use_sign > 0 else STEP_DOWN))
                if abs(new - cur) < 1e-9 or _already(state, sid, scope, kind, new):
                    continue
                chosen = (use_sign, flipped, round(new, 4))
                break
            if chosen is None:
                unmapped.append({'parameter': lv['parameter'],
                                 'why': f'{sid} is exhausted in both directions for '
                                        f'{scope or "all arms"}, or the step would repeat a value '
                                        f'already tried'})
                continue
            use_sign, flipped, new = chosen
            basis = lv['basis']
            if flipped:
                basis = (f'DIRECTION FLIPPED BY THE SEARCH, not by the lever. The lever asked to '
                         f'{lv["direction"]} this parameter and that direction is exhausted for '
                         f'{scope or "all arms"}, so the opposite is being tried. Original basis given '
                         f'for the lever: {lv["basis"] or "none recorded"}')
            _admit(moves, {
                'kind': kind, 'step_id': sid, 'stage_id': st['stage_id'] if st else None,
                'factor': ((s.get('factor') or 'seeding density') if s is not None
                           else 'feed_fraction'),
                'arm_id': scope, 'unit': unit, 'from': cur, 'to': new,
                'direction': ('increase' if use_sign > 0 else 'decrease') if kind != 'window'
                             else ('lengthen' if use_sign > 0 else 'shorten'),
                'parameter': lv['parameter'],
                'origin': 'search' if flipped else lv['origin'], 'basis': basis,
                'confidence': lv['confidence'], 'hypothesis_id': lv['hypothesis_id'],
                'from_iteration': iteration,
            }, arm_ids, max_changes)

    # A lever that just paid off is stepped again in the same direction before
    # anything else. A hill climber keeps walking uphill while the ground rises;
    # dropping back into round-robin after every success would spend an iteration
    # elsewhere and lose the ascent.
    for sc in [v for v in verdicts if v['outcome'] == 'better']:
        if len(moves) >= max_changes:
            break
        st, s = (None, None) if sc['kind'] == 'culture' else _step_by_id(protocol, sc['step_id'])
        if sc['kind'] != 'culture' and s is None:
            continue
        up = sc['direction'] in ('increase', 'lengthen')
        cur = sc['to']
        if sc['kind'] == 'culture':
            new = round(min(0.9, max(0.1, cur * (STEP_UP if up else STEP_DOWN))), 3)
        elif sc['kind'] == 'window':
            new = max(s['day'] + 1.0, min(st['end_day'], cur + DAY_STEP * (1 if up else -1)))
        else:
            rec = state['levers'].get(_key(sc['step_id'], sc['arm_id'], sc['kind'])) or {}
            new = _band(rec.get('baseline', cur), cur * (STEP_UP if up else STEP_DOWN))
        if (abs(new - cur) < 1e-9
                or _already(state, sc['step_id'], sc['arm_id'], sc['kind'], round(new, 4))):
            continue
        _admit(moves, {
            'kind': sc['kind'], 'step_id': sc['step_id'], 'stage_id': sc.get('stage_id'),
            'factor': sc['factor'], 'arm_id': sc['arm_id'], 'unit': sc['unit'],
            'from': cur, 'to': round(new, 4), 'direction': sc['direction'],
            'parameter': sc['parameter'], 'origin': 'search',
            'basis': f'Continuing an ascent: the previous step on this lever '
                     f'({_fmt(sc["from"])} -> {_fmt(sc["to"])} {sc["unit"]}) improved '
                     f'{", ".join(a for a, d in sc["deltas"].items() if d > MIN_REL_GAIN)}, so the '
                     f'same direction is tried again. Original basis: {sc["basis"] or "none recorded"}',
            'confidence': sc.get('confidence'), 'hypothesis_id': sc.get('hypothesis_id'),
            'from_iteration': iteration,
        }, arm_ids, max_changes)

    resolved = _resolve_all(_candidate_levers(brief, bioinfo_levers, arm_ids), True)
    for _attempts, _order, lv, sign, kind, st, s, _sid in resolved:
        if len(moves) >= max_changes:
            break
        # Scope. An explicit arm wins. Otherwise, if some arms already pass, a
        # shared move would risk their result, so scope it to the failing arms.
        if lv['arm_scope']:
            scopes = [lv['arm_scope']] if lv['arm_scope'] in (failing or arm_ids) else []
        elif passing and failing:
            scopes = list(failing)
        else:
            scopes = [None]
        if scopes:
            _plan_one(lv, sign, kind, st, s, scopes)

    # Every implicated lever is spent. Rather than stop while obvious coordinates
    # sit untouched, explore the protocol's own quantities - clearly labelled as
    # exploration nothing pointed the loop at.
    exploring = False
    if not moves:
        exploring = True
        for _a, _o, lv, sign, kind, st, s, _sid in _resolve_all(
                fallback_levers(protocol, state), False):
            if len(moves) >= max_changes:
                break
            _plan_one(lv, sign, kind, st, s, [None] if not (passing and failing) else list(failing))

    return {'moves': moves[:max_changes], 'reverts': reverts, 'conflicts': conflicts,
            'scored': verdicts, 'unmapped': unmapped, 'findings': list(state['findings']),
            'failing_arms': failing, 'passing_arms': passing,
            'exploring_unimplicated': exploring}


# ── application ─────────────────────────────────────────────

def _rationale(mv, brief):
    origin = {'bioinformatics': 'the bioinformatics annotation', 'analysis': 'the analysis diagnosis',
              'search': 'the search itself, after scoring the previous move'}.get(mv['origin'],
                                                                                 mv['origin'])
    conf = f' (annotation confidence: {mv["confidence"]})' if mv.get('confidence') else ''
    return (f'Search step from {origin}{conf}. Lever: {mv["parameter"]!r} {mv["direction"]}. '
            f'Basis given for the lever: {mv["basis"] or "none recorded"}. Moved from '
            f'{_fmt(mv["from"])} to {_fmt(mv["to"])} {mv["unit"]}'
            + (f' for {mv["arm_id"]} only' if mv['arm_id'] else ' for every arm')
            + f'. Brief: {(brief or {}).get("brief_id", "none")}. This value came from a bounded search '
              f'step, not from any publication, so it is a design choice.')


def _set_quantity(value, unit, rationale):
    """A searched value is always a design choice: it came from a step, not a paper."""
    return {'value': value, 'unit': unit, 'provenance': 'design_choice', 'claim_ids': [],
            'rationale': rationale}


def apply_moves(protocol, plan, request, brief, iteration, state=None):
    """Build the child protocol. Pure with respect to the parent; updates `state`."""
    state = state or new_state()
    if not plan['moves'] and not plan['reverts']:
        raise K.ContractError(
            'the lever search has nothing left to move: every lever the brief and the annotations '
            'offer is either unmappable onto this protocol or already exhausted in both directions. '
            'That is a real outcome - the loop should gather different evidence, ask a person, or '
            'stop, rather than emit a protocol identical to its parent.')
    p = copy.deepcopy(protocol)
    p['protocol_id'] = f'{request["request_id"]}-it{iteration}'
    p['parent_protocol_id'] = protocol['protocol_id']
    p['revision_brief_id'] = (brief or {}).get('brief_id')
    p['iteration'] = iteration
    p['approval'] = None
    p['changes_from_parent'] = []
    steps = {s['step_id']: s for st in p['stages'] for s in st['steps']}
    n_adj = len(p.get('arm_adjustments', []))

    # Reverts first: undo what the last revision got wrong before stepping again.
    for rv in plan['reverts']:
        why = (f'Reverted to {_fmt(rv["revert_to"])} {rv["unit"]}: the previous revision moved this to '
               f'{_fmt(rv["to"])} and the result {rv["why"]}. '
               + ('The arms are given separate values of this parameter instead.'
                  if rv['why'] == 'conflict' else
                  'That direction is now treated as exhausted for this scope.'))
        if rv['kind'] == 'culture':
            q = p['culture_system']['parameters']['feed_fraction']
            p['culture_system']['parameters']['feed_fraction'] = _set_quantity(
                rv['revert_to'], q['unit'], why)
        elif rv['arm_id'] is None:
            s = steps.get(rv['step_id'])
            if s is None:
                continue
            if rv['kind'] == 'window':
                s['end_day'] = rv['revert_to']
                s['description'] = f'{rv["factor"]} through day {_fmt(rv["revert_to"])}'
            else:
                s['quantity'] = _set_quantity(rv['revert_to'], rv['unit'], why)
        else:
            # Restore this arm's previous value rather than dropping the
            # adjustment: dropping it would silently return the arm to the shared
            # base, discarding a per-arm value the search had already proved best.
            p['arm_adjustments'] = [a for a in p.get('arm_adjustments', [])
                                    if not (a['arm_id'] == rv['arm_id']
                                            and a['step_id'] == rv['step_id'])]
            shared = steps.get(rv['step_id'])
            shared_val = (shared or {}).get('quantity', {}).get('value') if rv['kind'] != 'window' \
                else (shared or {}).get('end_day')
            if shared_val is None or abs(float(shared_val) - rv['revert_to']) > 1e-9:
                n_adj += 1
                adj = {'adjustment_id': f'adj-it{iteration}-{n_adj:02d}', 'arm_id': rv['arm_id'],
                       'step_id': rv['step_id'], 'rationale': f'{rv["arm_id"]} only. {why}',
                       'effect_ids': [e['effect_id'] for e in p.get('genotype_effects', [])
                                      if e['arm_id'] == rv['arm_id']]}
                if rv['kind'] == 'window':
                    adj['day_shift'] = rv['revert_to'] - float(shared_val or rv['revert_to'])
                else:
                    adj['quantity'] = _set_quantity(rv['revert_to'], rv['unit'], why)
                p.setdefault('arm_adjustments', []).append(adj)
        p['changes_from_parent'].append({
            'step_id': rv['step_id'],
            'change': f'{rv["factor"]}: reverted {_fmt(rv["to"])} -> {_fmt(rv["revert_to"])} '
                      f'{rv["unit"]} ({rv["arm_id"] or "all arms"}) because the result {rv["why"]}',
            'addresses': [(brief or {}).get('brief_id')] if brief else [],
            'claim_ids': []})

    for mv in plan['moves']:
        why = _rationale(mv, brief)
        if mv['kind'] == 'culture':
            q = p['culture_system']['parameters']['feed_fraction']
            p['culture_system']['parameters']['feed_fraction'] = _set_quantity(
                mv['to'], q['unit'], why)
        elif mv['arm_id'] is None:
            s = steps[mv['step_id']]
            if mv['kind'] == 'window':
                s['end_day'] = mv['to']
                s['description'] = f'{mv["factor"]} through day {_fmt(mv["to"])}'
            else:
                s['quantity'] = _set_quantity(mv['to'], mv['unit'], why)
        else:
            effect_ids = [e['effect_id'] for e in p.get('genotype_effects', [])
                          if e['arm_id'] == mv['arm_id']]
            p['arm_adjustments'] = [a for a in p.get('arm_adjustments', [])
                                    if not (a['arm_id'] == mv['arm_id']
                                            and a['step_id'] == mv['step_id'])]
            n_adj += 1
            adj = {'adjustment_id': f'adj-it{iteration}-{n_adj:02d}', 'arm_id': mv['arm_id'],
                   'step_id': mv['step_id'], 'rationale': f'{mv["arm_id"]} only. {why}',
                   'effect_ids': effect_ids}
            if mv['kind'] == 'window':
                adj['day_shift'] = mv['to'] - mv['from']
            else:
                adj['quantity'] = _set_quantity(mv['to'], mv['unit'], why)
            p.setdefault('arm_adjustments', []).append(adj)
        p['changes_from_parent'].append({
            'step_id': mv['step_id'],
            'change': f'{mv["factor"]}: {_fmt(mv["from"])} -> {_fmt(mv["to"])} {mv["unit"]} '
                      f'({mv["arm_id"] or "all arms"})',
            'addresses': [a for a in [mv.get('hypothesis_id'), (brief or {}).get('brief_id')] if a],
            'claim_ids': []})

    state['last_moves'] = [dict(m) for m in plan['moves']]
    p['title'] = (protocol['title'].split(' (iteration')[0]
                  + f' (iteration {iteration}: '
                  + '; '.join(f'{m["factor"]} {m["direction"]}'
                              + (f' in {m["arm_id"]}' if m['arm_id'] else '')
                              for m in plan['moves'])
                  + (f'; {len(plan["reverts"])} reverted' if plan['reverts'] else '') + ')')
    notes = ['Revised by the deterministic coordinate search in biosense.production.revise. Every '
             'revised quantity is a design choice, and no searched value is attributed to a '
             'publication.']
    if plan['findings']:
        notes += [f'Search finding: {f}' for f in plan['findings']]
    p['limitations'] = sorted(set(list(protocol.get('limitations', [])) + notes))
    if plan['unmapped']:
        p['open_questions'] = sorted(set(list(protocol.get('open_questions', [])) + [
            'Levers the reviser could not act on, which remain unaddressed rather than resolved: '
            + '; '.join(sorted({f'{u["parameter"]} ({u["why"]})' for u in plan['unmapped']}))]))
    return p


def revise(protocol, report, request, brief, bioinfo_levers=(), iteration=None, state=None,
           max_changes=MAX_CHANGES):
    """plan_changes then apply_moves. Returns (child_protocol, plan, state)."""
    state = state or new_state()
    it = protocol['iteration'] + 1 if iteration is None else iteration
    plan = plan_changes(protocol, report, request, brief, bioinfo_levers, state, max_changes)
    child = apply_moves(protocol, plan, request, brief, it, state)
    return child, plan, state
