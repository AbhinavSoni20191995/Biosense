"""Tool settings named in a plan: known ones with their defaults, unknown ones refused.

A setting a tool silently ignores is worse than one it refuses: the plan would
record a choice that never happened.
"""
from __future__ import annotations

from ... import contracts as K


def take(plan, defaults):
    """The plan's options over *defaults*; a key the tool does not know is refused."""
    given = dict(((plan or {}).get('comparison') or {}).get('options') or {})
    unknown = sorted(set(given) - set(defaults))
    if unknown:
        raise K.ContractError(f'unknown option(s) {", ".join(unknown)}; this tool takes '
                              f'{", ".join(f"{k} (default {v})" for k, v in defaults.items())}')
    return {**defaults, **given}
