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

    approve_protocols = h.get('approve_protocols')
    if approve_protocols is None:
        approve_protocols = mode in ('full', 'checkpoints')
    if source == 'wet_lab' and not approve_protocols:
        approve_protocols = True
        overrides.append('A wet_lab run always requires named human protocol approval, whatever the '
                         'autonomy mode asks for. Set bioreactor_source to synthetic_standin to run '
                         'protocols without an approver.')

    approve_decisions = h.get('approve_decisions')
    if approve_decisions is None:
        approve_decisions = mode == 'full'

    return {
        'mode': mode,
        'bioreactor_source': source,
        'protocol_approval_required': bool(approve_protocols),
        'decision_approval_required': bool(approve_decisions),
        'consults': consults,
        'max_info_actions_per_iteration': int(h.get('max_info_actions_per_iteration', 4)),
        'notify': h.get('notify'),
        'safety_overrides': overrides,
        'bioinformatics_allowed': bool((request.get('bioinformatics') or {}).get('allowed', True)),
        'live_lookups': bool((request.get('bioinformatics') or {}).get('live_lookups', False)),
        'knowledge_sets': (request.get('bioinformatics') or {}).get('knowledge_sets'),
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
            + (', live lookups permitted.' if gates['live_lookups'] else ', offline knowledge sets only.')]
    if gates['safety_overrides']:
        bits.append('Overridden by a safety rule: ' + ' '.join(gates['safety_overrides']))
    return ' '.join(bits)
