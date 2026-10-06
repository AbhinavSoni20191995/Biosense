"""What an anonymous visitor may spend on real AI, and for how long.

The synthetic path costs nothing: it is deterministic code over committed
fixtures, bounded by a semaphore, and a visitor may run it all day. A real run is
different in kind — it starts model calls on somebody's credit card and lasts
minutes rather than a second — so the moment a public URL can start one, "how
much of this may a stranger do" stops being a theoretical question.

This module is the answer, and it is deliberately small. It is not a billing
platform, not a quota service and not an identity system. It caps four things:

    concurrency     one real run at a time (the app already enforced this)
    per client      how many real runs one caller may start in a day
    per deployment  how many real runs this service may start in a day
    duration        a wall-clock deadline, after which the run is stopped

**Demo policy and system safety are different categories**, and the difference
decides what an operator may bypass. What is in this module — the per-caller
cap, the deployment cap and the cooldown — exists to bound what strangers can
spend on BioSense's provider key. An administrator of the deployment is not a
stranger, so `authorise()` exempts them by role and records their runs
separately. What is NOT in this module — the single-real-run concurrency gate,
the wall-clock deadline, the prompt and body size limits, the private-data
boundary, the fixed agent bundle — exists to keep the service and its data safe
from any caller, including its operator, and none of it consults role anywhere.

The deadline is the one that sits across the line: it is configured here because
it is a cost control, and it is applied to every run including an admin's,
because a run nobody can stop is a bill nobody can stop.

Design notes worth keeping:

* **A refusal is never a downgrade.** Hitting a cap raises `BudgetExceeded`,
  which the app answers as a refusal naming the limit and when to come back. It
  never turns into a synthetic run wearing a real badge — that substitution does
  not exist anywhere in this codebase.
* **The client key is a bucket, not an identity.** It is a hash of the caller's
  address, kept in memory only, used for nothing but counting, and never logged,
  exported or shown. A shared NAT shares a bucket; that is the accepted cost of
  not tracking people.
* **Limits are public.** `describe()` is safe for the browser and the interface
  shows it, because a visitor who is about to be refused should have been able to
  read the rule first.
* **Off by default.** A private instance someone runs for their own lab has no
  caps unless it asks for them. The hosted deployment sets BIOSENSE_PUBLIC_DEMO=1.

    BIOSENSE_PUBLIC_DEMO=1                   turn the caps on
    BIOSENSE_MAX_REAL_RUNS_PER_DAY           deployment-wide, default 40
    BIOSENSE_MAX_REAL_RUNS_PER_CLIENT        per caller per day, default 3
    BIOSENSE_REAL_RUN_TIMEOUT_S              wall clock, default 1800
    BIOSENSE_REAL_RUN_COOLDOWN_S             between one caller's runs, default 60

Any of them may be set without BIOSENSE_PUBLIC_DEMO; setting one turns it on.
A value of 0 means "no limit of that kind", which is the only way to remove one,
so a cap can never be removed by accident or by a typo.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass

from .. import contracts as K

ENV_PUBLIC = 'BIOSENSE_PUBLIC_DEMO'
ENV_DAILY = 'BIOSENSE_MAX_REAL_RUNS_PER_DAY'
ENV_PER_CLIENT = 'BIOSENSE_MAX_REAL_RUNS_PER_CLIENT'
ENV_TIMEOUT = 'BIOSENSE_REAL_RUN_TIMEOUT_S'
ENV_COOLDOWN = 'BIOSENSE_REAL_RUN_COOLDOWN_S'

DAY_S = 24 * 60 * 60
DEFAULTS = {'daily': 40, 'per_client': 3, 'timeout_s': 1800, 'cooldown_s': 60}
# A hard ceiling on the deadline whatever the environment says, because a run
# that cannot be stopped is a bill that cannot be stopped.
MAX_TIMEOUT_S = 4 * 60 * 60


class BudgetExceeded(K.ContractError):
    """A real run was refused because a cap was reached.

    Carries the cap that was hit and when it is worth trying again. It is a
    ContractError so the HTTP layer already answers it as a refusal, and it is
    emphatically not something any caller may handle by starting a synthetic run
    and calling it the same thing.
    """

    def __init__(self, limit, message, retry_after_s=None):
        self.limit = limit
        self.retry_after_s = int(retry_after_s) if retry_after_s else None
        super().__init__(message)


def _int(env, name, default):
    raw = (env.get(name) or '').strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise K.ContractError(
            f'{name} must be a whole number of {"seconds" if name.endswith("_S") else "runs"}; '
            f'got {raw!r}') from None
    if v < 0:
        raise K.ContractError(f'{name} cannot be negative; got {v}')
    return v


@dataclass(frozen=True)
class Limits:
    """The caps in force. Zero means that particular cap is off."""

    public: bool = False
    daily: int = 0
    per_client: int = 0
    timeout_s: int = 0
    cooldown_s: int = 0

    @property
    def any_cap(self):
        return bool(self.daily or self.per_client or self.timeout_s or self.cooldown_s)

    def describe(self):
        """What the browser may be told. Numbers and words, nothing identifying."""
        return {
            'public_demo': self.public,
            'max_real_runs_per_day': self.daily or None,
            'max_real_runs_per_client_per_day': self.per_client or None,
            'real_run_timeout_s': self.timeout_s or None,
            'real_run_cooldown_s': self.cooldown_s or None,
            'admin_exempt': True,
            'note': ('This deployment runs real AI on its own credentials, so real runs are '
                     'capped. A cap refuses the run and says when to come back; it never '
                     'silently gives you a synthetic one instead.'
                     if self.any_cap else
                     'No run caps are configured on this instance.'),
        }

    def deadline_from(self, started_at):
        """When a run that began at *started_at* must be stopped, or None."""
        return (started_at + self.timeout_s) if self.timeout_s else None


def from_env(env=None):
    env = os.environ if env is None else env
    asked = [n for n in (ENV_DAILY, ENV_PER_CLIENT, ENV_TIMEOUT, ENV_COOLDOWN)
             if (env.get(n) or '').strip()]
    public = (env.get(ENV_PUBLIC) or '').strip().lower() in ('1', 'true', 'yes')
    if not public and not asked:
        return Limits()
    timeout = _int(env, ENV_TIMEOUT, DEFAULTS['timeout_s'])
    return Limits(
        public=public,
        daily=_int(env, ENV_DAILY, DEFAULTS['daily']),
        per_client=_int(env, ENV_PER_CLIENT, DEFAULTS['per_client']),
        timeout_s=min(timeout, MAX_TIMEOUT_S) if timeout else 0,
        cooldown_s=_int(env, ENV_COOLDOWN, DEFAULTS['cooldown_s']))


def client_key(remote_addr, forwarded_for=None):
    """A counting bucket for a caller, which is not a record of who they are.

    Behind Railway's proxy the socket address is the proxy, so the left-most hop
    of X-Forwarded-For is the caller. It is attacker-controlled, which is fine
    for a cap meant to stop casual waste and is the reason this is not used for
    anything but counting. The result is a short hash: nothing downstream can
    recover an address from it, so it cannot leak one into a log or a response.
    """
    first = (forwarded_for or '').split(',')[0].strip()
    raw = first or (remote_addr or 'unknown')
    return hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()[:16]


class Ledger:
    """Real-run starts in this process, in a rolling 24 hours.

    In memory on purpose: a restart forgets the counts, which is the correct
    trade for a cap that exists to stop waste rather than to enforce an
    entitlement. Nothing here is written to disk, so nothing here can become a
    record of who ran what.
    """

    def __init__(self, limits=None, now=time.time):
        self.limits = limits or Limits()
        self._now = now
        self._lock = threading.Lock()
        self._by_client = {}
        self._all = []
        # Counted apart from the public ones rather than exempted into
        # invisibility. An operator's runs are not free, and a deployment should
        # be able to see how much of its spend was its own development.
        self._admin = []

    def _prune(self, now):
        cut = now - DAY_S
        self._all = [t for t in self._all if t > cut]
        self._admin = [t for t in self._admin if t > cut]
        for key in list(self._by_client):
            kept = [t for t in self._by_client[key] if t > cut]
            if kept:
                self._by_client[key] = kept
            else:
                del self._by_client[key]

    def authorise(self, client, *, is_real=True, role=None):
        """Record one real-run start, or refuse it by name.

        Called before anything is written, so a run that is not allowed never
        appears in the run list as though it might have worked.

        *role* comes from `authz.Policy.role_for`, which reads a server-side
        configuration and a server-verified identity. An administrator is
        recorded and exempted; nothing a request can carry reaches this
        argument, which is the property that makes the exemption safe.
        """
        lim = self.limits
        if not is_real:
            return
        now = self._now()
        if role == 'admin':
            with self._lock:
                self._prune(now)
                self._admin.append(now)
            return
        # Recorded whether or not caps are configured: the counts are
        # observability, and a deployment that has not set a limit still wants to
        # know what it spent. Each check below is guarded by its own limit, so
        # "no caps" means nothing is refused, not that nothing is counted.
        with self._lock:
            self._prune(now)
            mine = self._by_client.get(client, [])
            if lim.cooldown_s and mine:
                wait = lim.cooldown_s - (now - mine[-1])
                if wait > 0:
                    raise BudgetExceeded(
                        'cooldown',
                        f'This deployment runs real AI on its own credentials, so it asks '
                        f'{lim.cooldown_s} seconds between runs from one place. Try again in '
                        f'{int(wait) + 1} seconds, or run the demonstration path now.',
                        retry_after_s=wait + 1)
            if lim.per_client and len(mine) >= lim.per_client:
                oldest = min(mine)
                raise BudgetExceeded(
                    'per_client',
                    f'You have started {len(mine)} real AI runs here in the last 24 hours and '
                    f'the limit for one caller is {lim.per_client}. The demonstration path has '
                    f'no limit. To run real AI without a cap, run BioSense yourself: '
                    f'./scripts/start_local_ai.sh',
                    retry_after_s=(oldest + DAY_S) - now)
            if lim.daily and len(self._all) >= lim.daily:
                oldest = min(self._all)
                raise BudgetExceeded(
                    'daily',
                    f'This deployment has run its daily budget of {lim.daily} real AI runs. '
                    f'The demonstration path still works, and running BioSense yourself has no '
                    f'cap: ./scripts/start_local_ai.sh',
                    retry_after_s=(oldest + DAY_S) - now)
            self._all.append(now)
            self._by_client.setdefault(client, []).append(now)

    def state(self):
        """Counts, for /readyz and the interface. No client keys leave here."""
        now = self._now()
        with self._lock:
            self._prune(now)
            return {'real_runs_last_24h': len(self._all),
                    'public_real_runs_today': len(self._all),
                    'admin_real_runs_today': len(self._admin),
                    'callers_last_24h': len(self._by_client),
                    'limits': self.limits.describe()}

    def allowance(self, client, *, role=None):
        """What this caller has left, for the interface to show before it refuses.

        Only facts the server actually knows: a count it is keeping, or a
        cooldown it is enforcing. A deployment with no caps reports none rather
        than inventing a number.
        """
        lim = self.limits
        if role == 'admin':
            return {'role': 'admin', 'capped': False,
                    'note': 'Operator account: public demo caps do not apply. Runs still '
                            'spend this deployment\'s model credits, and every safety limit '
                            '— the run timeout, the one-at-a-time gate, the size limits — '
                            'still applies.'}
        if not lim.any_cap:
            return {'role': role or 'public', 'capped': False,
                    'note': 'No run caps are configured on this instance.'}
        now = self._now()
        with self._lock:
            self._prune(now)
            mine = list(self._by_client.get(client, []))
            used_all = len(self._all)
        out = {'role': role or 'public', 'capped': True,
               'real_runs_used_today': len(mine)}
        if lim.per_client:
            out['real_runs_remaining_today'] = max(0, lim.per_client - len(mine))
        if lim.daily:
            out['deployment_runs_remaining_today'] = max(0, lim.daily - used_all)
        if lim.cooldown_s and mine:
            wait = lim.cooldown_s - (now - max(mine))
            if wait > 0:
                out['next_run_in_s'] = int(wait) + 1
        return out
