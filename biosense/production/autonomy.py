"""How much the person stays in the loop, and the gates code enforces regardless.

The request asks for a mode. This module turns that into concrete gates and
records every place a safety rule overrode what was asked, so the override is
visible rather than silent.

  full         approve every protocol and every decision
  checkpoints  approve every protocol; decisions commit unless flagged
  autonomous   the orchestrator decides and notifies; protocols still need
               approval whenever a real bioreactor is involved

One rule outranks the mode: a wet-lab run always needs a named human to approve
the protocol. Cells, reagents and operator time are spent in the physical world,
and nothing in this system can unspend them.

That rule does not mean "stop and ask every round". A closed loop is a loop: the
orchestrator sets the conditions, the instruments feed their readings straight
back, and it iterates toward the target. Asking for a signature between every
round would not make that safer, it would make it not a loop. So the person may
give a **campaign authorisation** instead — once, by name, covering a bounded
number of iterations inside a stated parameter envelope with stated stop
conditions. It satisfies the rule rather than bypassing it: a named human
authorised the physical work, and `check_envelope` verifies every round that the
loop is still inside what they authorised. A protocol that would step outside the
envelope, or an iteration past the count, stops and asks again — and cannot
authorise itself.

What this module does not do, and no part of this system does: actuate anything.
The closed loop here is the decision and measurement path. Moving a physical
setpoint on physical equipment is out of scope.
"""
MODES = ('full', 'checkpoints', 'autonomous')
CONSULT_POLICIES = ('always', 'high_value', 'never')
DEFAULT = {'mode': 'full'}


def resolve(request):
    """Return the effective gates for this request."""
    h = dict(request.get('human_in_the_loop') or DEFAULT)
    mode = h.get('mode', 'full')
    if mode not in MODES:
        raise ValueError(f'human_in_the_loop.mode must be one of {MODES}')
    consults = h.get('consults', 'high_value')
    if consults not in CONSULT_POLICIES:
        raise ValueError(f'human_in_the_loop.consults must be one of {CONSULT_POLICIES}')
    source = request.get('bioreactor_source', 'wet_lab')
    overrides = []

    campaign = validate_campaign(h.get('campaign_authorisation'))

    approve_protocols = h.get('approve_protocols')
    if approve_protocols is None:
        approve_protocols = mode in ('full', 'checkpoints')
    if source == 'wet_lab' and not approve_protocols and not campaign:
        approve_protocols = True
        overrides.append('A wet_lab run always requires named human protocol approval, whatever the '
                         'autonomy mode asks for. Give a campaign authorisation to approve a '
                         'bounded run of iterations at once, or set bioreactor_source to '
                         'synthetic_standin to run protocols without an approver.')
    if campaign and approve_protocols and mode == 'autonomous':
        # The authorisation is the approval, for as long as the loop stays inside
        # it. Per-round approval is what it was given instead of.
        approve_protocols = False
        overrides.append(
            f'Protocols run without a per-round signature because {campaign["authorised_by"]} '
            f'authorised up to {campaign["max_iterations"]} iteration(s) inside a stated '
            f'envelope. Every round is checked against it, and stepping outside stops the loop.')

    approve_decisions = h.get('approve_decisions')
    if approve_decisions is None:
        approve_decisions = mode == 'full'

    return {
        'mode': mode,
        'bioreactor_source': source,
        'campaign_authorisation': campaign,
        'protocol_approval_required': bool(approve_protocols),
        'decision_approval_required': bool(approve_decisions),
        'consults': consults,
        'max_info_actions_per_iteration': int(h.get('max_info_actions_per_iteration', 4)),
        'notify': h.get('notify'),
        'safety_overrides': overrides,
        'bioinformatics_allowed': bool((request.get('bioinformatics') or {}).get('allowed', True)),
        'live_lookups': bool((request.get('bioinformatics') or {}).get('live_lookups', False)),
        'knowledge_sets': (request.get('bioinformatics') or {}).get('knowledge_sets'),
        # Dataset analysis is off unless the request asks for it. A loop that was
        # never told to look at data should not start reading files, and a loop
        # that may read public data should not thereby reach a person's own.
        'datasets_allowed': bool((request.get('bioinformatics') or {}).get('datasets', False)),
        'private_data_allowed': bool((request.get('bioinformatics') or {}).get('private_data', False)),
        'dataset_search_live': bool((request.get('bioinformatics') or {}).get('dataset_search_live', False)),
    }


def describe(gates):
    """One paragraph a person can check against what they asked for."""
    bits = [f'Autonomy mode: {gates["mode"]}.',
            'Protocol approval: ' + ('a named human must approve every protocol.'
                                     if gates['protocol_approval_required'] else
                                     'not required (synthetic stand-in runs only).'),
            'Decisions: ' + ('each one waits for human approval.' if gates['decision_approval_required']
                             else 'commit when they pass validation; you are notified, not asked.'),
            f'Consults: {gates["consults"]}.',
            f'Information-gathering actions before the orchestrator must revise or stop: '
            f'{gates["max_info_actions_per_iteration"]} per iteration.',
            'Bioinformatics: ' + ('available' if gates['bioinformatics_allowed'] else 'not available')
            + (', live lookups permitted.' if gates['live_lookups'] else ', offline knowledge sets only.'),
            'Datasets: ' + ('not enabled for this request.' if not gates['datasets_allowed'] else
                            ('public and private datasets may be analysed.'
                             if gates['private_data_allowed'] else
                             'public datasets only; private user data is not read.'))
            + (' Live repository search permitted.' if gates['dataset_search_live']
               else ' Repository search is offline (cached index only).')]
    c = gates.get('campaign_authorisation')
    if c:
        bits.append(
            f'Campaign: {c["authorised_by"]} authorised up to {c["max_iterations"]} '
            f'iteration(s) inside a stated envelope '
            f'({", ".join(b["parameter_id"] for b in c["parameter_bounds"])}), stopping on: '
            f'{"; ".join(c["stop_conditions"])}. Every round is checked against it.')
    if gates['safety_overrides']:
        bits.append('Overridden by a safety rule: ' + ' '.join(gates['safety_overrides']))
    return ' '.join(bits)


def validate_campaign(c):
    """Check a campaign authorisation, or return None when there is none.

    Refuses the shapes that would make it meaningless: no name, no envelope, no
    stop condition, an inverted bound. An authorisation that authorises
    everything authorises nothing a reader could check afterwards.
    """
    if not c:
        return None
    who = (c.get('authorised_by') or '').strip()
    if len(who) < 2:
        raise ValueError('a campaign authorisation names the person who gave it. A role or a '
                         'system name is not a person.')
    n = c.get('max_iterations')
    if not isinstance(n, int) or not 1 <= n <= 50:
        raise ValueError('a campaign authorisation covers between 1 and 50 iterations, stated')
    bounds = c.get('parameter_bounds') or []
    if not bounds:
        raise ValueError('a campaign authorisation needs at least one parameter bound. Without '
                         'an envelope it is a blank cheque, and nothing could be checked '
                         'against it afterwards.')
    out = []
    for b in bounds:
        lo, hi = b.get('minimum'), b.get('maximum')
        if lo is None or hi is None or lo >= hi:
            raise ValueError(f'{b.get("parameter_id")}: a bound needs a minimum below a maximum')
        out.append({'parameter_id': b['parameter_id'], 'minimum': float(lo),
                    'maximum': float(hi)})
    stops = [s for s in (c.get('stop_conditions') or []) if (s or '').strip()]
    if not stops:
        raise ValueError('a campaign authorisation says what ends it without asking: the target '
                         'met, a QC failure, a viability floor, a cost ceiling.')
    return {'authorised_by': who, 'authorised_at': c.get('authorised_at'),
            'max_iterations': n, 'parameter_bounds': out, 'stop_conditions': stops,
            'note': c.get('note')}


def check_envelope(gates, *, iteration, setpoints):
    """Is this round still inside what the person authorised?

    Returns {'inside': bool, 'reasons': [...]}. Called before a protocol runs,
    every round. Outside the envelope the loop stops and asks; it never widens
    the envelope to fit what it wanted to do.
    """
    campaign = gates.get('campaign_authorisation')
    if not campaign:
        return {'inside': False, 'reasons': ['no campaign authorisation was given'],
                'authorised': False}
    reasons = []
    if iteration > campaign['max_iterations']:
        reasons.append(
            f'iteration {iteration} is past the {campaign["max_iterations"]} '
            f'{campaign["authorised_by"]} authorised')
    allowed = {b['parameter_id']: b for b in campaign['parameter_bounds']}
    for pid, value in (setpoints or {}).items():
        b = allowed.get(pid)
        if b is None:
            reasons.append(f'{pid} is not in the authorised envelope')
            continue
        if value is None:
            continue
        if not b['minimum'] <= float(value) <= b['maximum']:
            reasons.append(f'{pid}={value:g} is outside the authorised '
                           f'{b["minimum"]:g}–{b["maximum"]:g}')
    return {'inside': not reasons, 'reasons': reasons, 'authorised': True,
            'authorised_by': campaign['authorised_by'],
            'iterations_authorised': campaign['max_iterations'],
            'stop_conditions': campaign['stop_conditions']}
