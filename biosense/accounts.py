"""Accounts this deployment keeps itself: who may sign in, and how many runs each has.

A lab wants its people to open the page, make an account, and try a few
discovery runs. Nothing in that sentence needs a second server, so this module
is the account system: one JSON file under the private data root, a password
hash per person, and a count of the real AI runs each account has started.

What it is, stated precisely:

* **Self-service.** Anyone who can reach the page can create an account, unless
  the deployment sets a sign-up code (`BIOSENSE_SIGNUP_CODE`), in which case the
  code is asked for once, at sign-up. Closing sign-up altogether is
  `BIOSENSE_SIGNUP=closed`.
* **A password is never stored.** `hashlib.scrypt` with a per-account salt,
  compared in constant time. The standard library does it, so sign-in works on
  an installation that has installed nothing optional.
* **A daily allowance, and a bank of extra runs.** Each account may start
  `run_limit` real AI runs in any rolling 24 hours (`BIOSENSE_RUNS_PER_ACCOUNT`,
  default 5). A run is counted when it starts; the synthetic demonstration path
  costs nothing and is not counted. When the day's allowance is spent, an
  account may ask for more: the request is kept here, and an operator —
  signed in as an administrator, on the page or at the CLI — grants extra runs
  into the account's bank, which are spent only once the daily allowance is.
  Administrators (`BIOSENSE_ADMIN_USER_IDS`) are exempt, as they are from every
  demo cap.
* **The id is the email address**, normalised the way `workspace.normalise_owner`
  does, so an account and its workspace are one name.

What it is not: a password-reset flow (an operator resets one with the CLI), an
e-mail verifier, or anything that reaches outside this process. Sessions stay
where they were, in memory, in `workspace.SessionStore`.

The file is written atomically and read under a lock, and every mutation
re-reads it first, so two server threads — or the server and the CLI — do not
overwrite each other's change.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

from . import contracts as K
from . import workspace as WS
from .data import roots as DR

ENV_ACCOUNTS = 'BIOSENSE_ACCOUNTS'            # on (default) | off
ENV_SIGNUP = 'BIOSENSE_SIGNUP'                # open (default) | closed
ENV_SIGNUP_CODE = 'BIOSENSE_SIGNUP_CODE'      # optional: a code sign-up must quote
ENV_RUN_LIMIT = 'BIOSENSE_RUNS_PER_ACCOUNT'   # real AI runs per account per day, default 5

DEFAULT_RUN_LIMIT = 5
DAY_S = 24 * 60 * 60
MIN_PASSWORD = 8
MAX_PASSWORD = 256
MAX_ACCOUNTS = 10000
MAX_REQUEST_RUNS = 100
MAX_REASON = 600
# The issuer written into an identity this module signs in. `authz.Policy`
# compares it to the trusted issuer, so an admin id listed for this deployment
# matches an account this deployment itself authenticated — and nothing else.
ISSUER = 'biosense-accounts'
FILE = 'accounts.json'

# scrypt parameters: ~16 MiB, fast enough for a sign-in, slow enough for a
# dictionary. Recorded in each account so they can be raised later without
# invalidating anyone.
SCRYPT = {'n': 2 ** 14, 'r': 8, 'p': 1}


class AccountError(WS.AuthError):
    """Sign-up or sign-in refused, with a reason the page can show as it stands."""


class RunsExhausted(K.ContractError):
    """The account has started every real AI run it may start today.

    A ContractError so the HTTP layer answers it as a refusal; it carries the
    counts and when the next run opens, so the page can say exactly that rather
    than "no".
    """

    def __init__(self, owner, used, limit, retry_after_s=None):
        self.limit = 'account'
        self.used, self.run_limit = used, limit
        self.retry_after_s = int(retry_after_s) if retry_after_s else None
        wait = _wait_words(retry_after_s)
        super().__init__(
            f'This account has started {used} real AI run(s) in the last 24 hours, and its '
            f'daily allowance is {limit}. '
            + (f'The next one opens in {wait}. ' if wait else '')
            + 'The demonstration path still works without limit. To run more today, ask for '
              'extra runs from the account area: the person running this BioSense can grant '
              'them while you wait. Nothing is lost; every run you started stays in your '
              'run list.')


def _wait_words(seconds):
    if not seconds or seconds <= 0:
        return ''
    seconds = int(seconds)
    if seconds < 90:
        return 'about a minute'
    if seconds < 3600:
        return f'{(seconds + 59) // 60} minutes'
    hours, rest = divmod(seconds, 3600)
    mins = (rest + 59) // 60
    return f'{hours} h' + (f' {mins} min' if mins else '')


def _norm_email(value):
    s = str(value or '').strip().lower()
    if '@' not in s or s.startswith('@') or s.endswith('@') or ' ' in s:
        raise AccountError('an account is named by an email address', reason='bad_email',
                           status=400)
    return WS.normalise_owner(s)


def _hash(password, salt, params=SCRYPT):
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=params['n'], r=params['r'],
                          p=params['p'], dklen=32, maxmem=64 * 1024 * 1024)


class Settings:
    """Whether accounts are on, whether sign-up is open, and the daily allowance."""

    def __init__(self, enabled=True, signup='open', signup_code=None, run_limit=DEFAULT_RUN_LIMIT):
        self.enabled = bool(enabled)
        self.signup = signup if signup in ('open', 'closed') else 'open'
        self.signup_code = (signup_code or '').strip() or None
        self.run_limit = int(run_limit)

    @property
    def signup_open(self):
        return self.enabled and self.signup == 'open'

    def describe(self):
        """What the page may be told. Never the code."""
        return {'enabled': self.enabled, 'signup_open': self.signup_open,
                'signup_code_required': bool(self.signup_code), 'run_limit': self.run_limit,
                'per': 'day',
                'note': (f'Create an account with your email address and a password, sign in, and '
                         f'start up to {self.run_limit} real AI runs a day. The demonstration '
                         f'path is not counted. Your projects, runs and reports are kept under '
                         f'your account, and you can ask for extra runs when the day\'s are '
                         f'spent.' if self.enabled else
                         'Accounts are off on this instance.')}


def settings_from_env(env=None):
    env = os.environ if env is None else env
    enabled = (env.get(ENV_ACCOUNTS) or 'on').strip().lower() not in ('0', 'off', 'false', 'no')
    raw = (env.get(ENV_RUN_LIMIT) or '').strip()
    try:
        limit = int(raw) if raw else DEFAULT_RUN_LIMIT
    except ValueError:
        raise K.ContractError(f'{ENV_RUN_LIMIT} must be a whole number of runs; got {raw!r}') from None
    if limit < 0:
        raise K.ContractError(f'{ENV_RUN_LIMIT} cannot be negative; got {limit}')
    return Settings(enabled=enabled, signup=(env.get(ENV_SIGNUP) or 'open').strip().lower(),
                    signup_code=env.get(ENV_SIGNUP_CODE), run_limit=limit)


class Store:
    """The accounts file, read and written under one lock."""

    def __init__(self, path=None, settings=None, now=time.time):
        self.path = Path(path) if path else DR.private_root() / 'accounts' / FILE
        self.settings = settings or Settings()
        self._now = now
        self._lock = threading.Lock()

    # ── file ────────────────────────────────────────────────
    def _read(self):
        try:
            doc = K.read_json(self.path)
        except (OSError, ValueError):
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        if not isinstance(doc.get('accounts'), dict):
            doc['accounts'] = {}
        if not isinstance(doc.get('requests'), list):
            doc['requests'] = []
        doc['kind'] = 'accounts'
        return doc

    def _write(self, doc):
        doc['kind'] = 'accounts'
        doc['note'] = ('BioSense accounts. Password hashes only (scrypt, per-account salt); '
                       'never a password. Not served over HTTP and never committed.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        K.write_json_atomic(self.path, doc)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ── accounts ────────────────────────────────────────────
    def create(self, email, password, *, signup_code=None, display=None):
        """A new account, signed in: the identity to open a session for."""
        if not self.settings.enabled:
            raise AccountError('accounts are off on this instance', reason='accounts_off',
                               status=400)
        if not self.settings.signup_open:
            raise AccountError('sign-up is closed on this instance; ask the person running it '
                               'for an account', reason='signup_closed', status=403)
        owner = _norm_email(email)
        password = password or ''
        if len(password) < MIN_PASSWORD:
            raise AccountError(f'choose a password of at least {MIN_PASSWORD} characters',
                               reason='weak_password', status=400)
        if len(password) > MAX_PASSWORD:
            raise AccountError('that password is longer than the limit', reason='weak_password',
                               status=400)
        if self.settings.signup_code:
            given = (signup_code or '').strip()
            if not hmac.compare_digest(given.encode(), self.settings.signup_code.encode()):
                raise AccountError('this instance asks for a sign-up code, and that is not it',
                                   reason='signup_code', status=403)
        salt = secrets.token_bytes(16)
        digest = _hash(password, salt)
        with self._lock:
            doc = self._read()
            accounts = doc['accounts']
            if owner in accounts:
                raise AccountError('an account with that email address already exists; sign in '
                                   'instead', reason='exists', status=409)
            if len(accounts) >= MAX_ACCOUNTS:
                raise AccountError('this instance has as many accounts as it will hold',
                                   reason='full', status=403)
            accounts[owner] = {
                'email': owner, 'display': (display or '').strip()[:80] or owner,
                'salt': salt.hex(), 'hash': digest.hex(), 'scrypt': dict(SCRYPT),
                'created_at': K.now_iso(), 'runs_used': 0, 'run_times': [],
                'extra_runs': 0, 'run_limit': None,
                'disabled': False, 'last_sign_in': K.now_iso(),
            }
            self._write(doc)
        return self._identity(accounts[owner])

    def sign_in(self, email, password):
        """The identity for a correct email and password, or a refusal that says no more."""
        if not self.settings.enabled:
            raise AccountError('accounts are off on this instance', reason='accounts_off',
                               status=400)
        try:
            owner = _norm_email(email)
        except (AccountError, K.ContractError):
            raise AccountError('that email address or password was not accepted',
                               reason='invalid_credentials') from None
        if not password:
            raise AccountError('an email address and a password are both needed',
                               reason='missing_credentials', status=400)
        with self._lock:
            doc = self._read()
            row = doc['accounts'].get(owner)
            if row is None:
                # Spend the same time as a real check, so an absent account is
                # not told apart from a wrong password by the clock.
                _hash(password, b'\0' * 16)
                raise AccountError('that email address or password was not accepted',
                                   reason='invalid_credentials')
            try:
                digest = _hash(password, bytes.fromhex(row['salt']), row.get('scrypt') or SCRYPT)
            except (ValueError, KeyError):
                raise AccountError('that email address or password was not accepted',
                                   reason='invalid_credentials') from None
            if not hmac.compare_digest(digest.hex(), row.get('hash') or ''):
                raise AccountError('that email address or password was not accepted',
                                   reason='invalid_credentials')
            if row.get('disabled'):
                raise AccountError('this account is disabled; ask the person running this '
                                   'BioSense', reason='disabled', status=403)
            row['last_sign_in'] = K.now_iso()
            self._write(doc)
        return self._identity(row)

    def _identity(self, row):
        return WS.WorkspaceIdentity(row['email'], authenticated=True, server=ISSUER,
                                    token=None, display=row.get('display') or row['email'])

    def get(self, owner):
        """One account's public row, or None. Never the hash."""
        try:
            owner = WS.normalise_owner(owner)
        except K.ContractError:
            return None
        with self._lock:
            doc = self._read()
            row = doc['accounts'].get(owner)
            return self._public(row, doc) if row else None

    def _limit_of(self, row):
        limit = row.get('run_limit')
        return self.settings.run_limit if limit is None else int(limit)

    def _today(self, row, now):
        """The starts inside the rolling day, pruned in place."""
        cut = now - DAY_S
        times = [float(t) for t in (row.get('run_times') or []) if float(t) > cut]
        row['run_times'] = times
        return times

    def _public(self, row, doc=None):
        now = self._now()
        limit = self._limit_of(row)
        today = self._today(row, now)
        extra = int(row.get('extra_runs') or 0)
        out = {'email': row['email'], 'display': row.get('display') or row['email'],
               'created_at': row.get('created_at'), 'last_sign_in': row.get('last_sign_in'),
               'runs_used': int(row.get('runs_used') or 0),
               'runs_used_today': len(today), 'run_limit': limit,
               'runs_remaining_today': (max(0, limit - len(today)) if limit else None),
               'extra_runs': extra, 'disabled': bool(row.get('disabled'))}
        if limit and len(today) >= limit:
            out['next_run_in_s'] = int(min(today) + DAY_S - now) + 1
        if doc is not None:
            mine = [r for r in doc['requests'] if r.get('email') == row['email']
                    and r.get('status') == 'open']
            out['open_request'] = mine[-1] if mine else None
        return out

    def listing(self):
        with self._lock:
            doc = self._read()
            rows = list(doc['accounts'].values())
            return sorted((self._public(r, doc) for r in rows), key=lambda r: r['email'])

    def count(self):
        with self._lock:
            return len(self._read()['accounts'])

    # ── the allowance ───────────────────────────────────────
    def allowance(self, owner):
        """What this account has left, for the page to show before it refuses."""
        row = self.get(owner)
        if row is None:
            return None
        left, limit, extra = row['runs_remaining_today'], row['run_limit'], row['extra_runs']
        out = {'role': 'user', 'capped': bool(limit), 'kind': 'account', 'per': 'day',
               'runs_limit': limit, 'runs_used_today': row['runs_used_today'],
               'runs_remaining_today': left, 'extra_runs': extra,
               'open_request': row.get('open_request')}
        if row.get('next_run_in_s'):
            out['next_run_in_s'] = row['next_run_in_s']
        if not limit:
            out['note'] = 'No daily limit on this account.'
        else:
            out['note'] = (f'{left} of {limit} real AI runs left today'
                           + (f', plus {extra} extra run(s) granted' if extra else '')
                           + '. The demonstration path is not counted.')
        return out

    def authorise_run(self, owner):
        """Count one real AI run against the account, or refuse by name.

        Called before anything is written, so a run that is not allowed never
        appears in the run list as though it might have worked. The day's
        allowance is spent first; an extra run granted by an operator is spent
        only once the day's are gone. Deleting a run later gives nothing back:
        the allowance is what was spent, not what was kept.
        """
        owner = WS.normalise_owner(owner)
        now = self._now()
        with self._lock:
            doc = self._read()
            row = doc['accounts'].get(owner)
            if row is None:
                raise AccountError('no account by that name on this instance; sign in again',
                                   reason='no_account', status=401)
            if row.get('disabled'):
                raise AccountError('this account is disabled', reason='disabled', status=403)
            limit = self._limit_of(row)
            today = self._today(row, now)
            spent = 'day'
            if limit and len(today) >= limit:
                extra = int(row.get('extra_runs') or 0)
                if extra <= 0:
                    raise RunsExhausted(owner, len(today), limit,
                                        retry_after_s=min(today) + DAY_S - now)
                row['extra_runs'] = extra - 1
                spent = 'extra'
            else:
                today.append(now)
            row['runs_used'] = int(row.get('runs_used') or 0) + 1
            self._write(doc)
            return {'spent': spent, 'runs_used_today': len(today), 'run_limit': limit,
                    'extra_runs': int(row.get('extra_runs') or 0)}

    # ── asking for more ─────────────────────────────────────
    def request_runs(self, owner, runs, reason):
        """An account asks for extra runs. Kept here until an operator decides.

        One open request per account: a second one replaces nothing and is
        refused with the first still standing, so an operator sees one ask per
        person rather than a pile.
        """
        owner = WS.normalise_owner(owner)
        try:
            runs = int(runs)
        except (TypeError, ValueError):
            raise K.ContractError('say how many extra runs you are asking for, as a number') from None
        if runs < 1 or runs > MAX_REQUEST_RUNS:
            raise K.ContractError(f'ask for between 1 and {MAX_REQUEST_RUNS} extra runs')
        reason = str(reason or '').strip()
        if len(reason) < 10:
            raise K.ContractError('say in a sentence what the extra runs are for; the person '
                                  'granting them reads it')
        reason = reason[:MAX_REASON]
        with self._lock:
            doc = self._read()
            row = doc['accounts'].get(owner)
            if row is None:
                raise AccountError('no account by that name on this instance', reason='no_account',
                                   status=401)
            for r in doc['requests']:
                if r.get('email') == owner and r.get('status') == 'open':
                    raise K.ContractError(
                        f'you already have a request open for {r["runs"]} run(s), made '
                        f'{r["created_at"]}; it is waiting for the person running this '
                        f'BioSense to decide.')
            req = {'request_id': secrets.token_hex(6), 'email': owner, 'runs': runs,
                   'reason': reason, 'created_at': K.now_iso(), 'status': 'open',
                   'decided_at': None, 'decided_by': None, 'granted_runs': None, 'note': None}
            doc['requests'].append(req)
            # Keep the file bounded: decided requests older than the newest
            # two hundred are history nobody reads here.
            decided = [r for r in doc['requests'] if r.get('status') != 'open']
            if len(decided) > 200:
                drop = {id(r) for r in decided[:-200]}
                doc['requests'] = [r for r in doc['requests'] if id(r) not in drop]
            self._write(doc)
            return dict(req)

    def requests(self, *, owner=None, open_only=False):
        """Requests, newest last. For one owner, or — for an operator — all."""
        with self._lock:
            rows = list(self._read()['requests'])
        if owner is not None:
            owner = WS.normalise_owner(owner)
            rows = [r for r in rows if r.get('email') == owner]
        if open_only:
            rows = [r for r in rows if r.get('status') == 'open']
        return [dict(r) for r in rows]

    def decide_request(self, request_id, *, grant, decided_by, runs=None, note=None):
        """An operator grants (adding runs to the account's bank) or declines."""
        decided_by = str(decided_by or '').strip() or 'operator'
        note = (str(note or '').strip() or None)
        with self._lock:
            doc = self._read()
            req = next((r for r in doc['requests'] if r.get('request_id') == request_id), None)
            if req is None:
                raise K.ContractError('no such request')
            if req.get('status') != 'open':
                raise K.ContractError(f'that request was already {req["status"]} by '
                                      f'{req.get("decided_by")} at {req.get("decided_at")}')
            row = doc['accounts'].get(req['email'])
            if grant:
                n = int(req['runs'] if runs is None else runs)
                if n < 0 or n > MAX_REQUEST_RUNS:
                    raise K.ContractError(f'grant between 0 and {MAX_REQUEST_RUNS} runs')
                if row is None:
                    raise K.ContractError('the account that asked no longer exists')
                row['extra_runs'] = int(row.get('extra_runs') or 0) + n
                req['granted_runs'] = n
                req['status'] = 'granted'
            else:
                req['status'] = 'declined'
            req['decided_at'] = K.now_iso()
            req['decided_by'] = decided_by
            req['note'] = note
            self._write(doc)
            return dict(req)

    # ── operator actions (page and CLI) ─────────────────────
    def grant_runs(self, owner, runs, *, granted_by='operator'):
        """Extra runs straight into one account's bank, with no request behind it."""
        owner = WS.normalise_owner(owner)
        runs = int(runs)
        if runs < 0 or runs > MAX_REQUEST_RUNS:
            raise K.ContractError(f'grant between 0 and {MAX_REQUEST_RUNS} runs')
        with self._lock:
            doc = self._read()
            row = doc['accounts'].get(owner)
            if row is None:
                raise K.ContractError(f'no account {owner}')
            row['extra_runs'] = int(row.get('extra_runs') or 0) + runs
            row.setdefault('grants', []).append({'runs': runs, 'by': str(granted_by)[:80],
                                                 'at': K.now_iso()})
            row['grants'] = row['grants'][-50:]
            self._write(doc)
            return self._public(row, doc)

    def set_run_limit(self, owner, limit):
        """An operator sets one account's daily limit. None restores the default; 0 means none."""
        owner = WS.normalise_owner(owner)
        if limit is not None and int(limit) < 0:
            raise K.ContractError('a run limit cannot be negative')
        with self._lock:
            doc = self._read()
            row = doc['accounts'].get(owner)
            if row is None:
                raise K.ContractError(f'no account {owner}')
            row['run_limit'] = None if limit is None else int(limit)
            self._write(doc)
            return self._public(row, doc)

    def reset_password(self, owner, password):
        owner = WS.normalise_owner(owner)
        if len(password or '') < MIN_PASSWORD:
            raise K.ContractError(f'a password has at least {MIN_PASSWORD} characters')
        salt = secrets.token_bytes(16)
        with self._lock:
            doc = self._read()
            row = doc['accounts'].get(owner)
            if row is None:
                raise K.ContractError(f'no account {owner}')
            row['salt'], row['hash'], row['scrypt'] = salt.hex(), _hash(password, salt).hex(), dict(SCRYPT)
            self._write(doc)
            return self._public(row, doc)

    def set_disabled(self, owner, disabled):
        owner = WS.normalise_owner(owner)
        with self._lock:
            doc = self._read()
            row = doc['accounts'].get(owner)
            if row is None:
                raise K.ContractError(f'no account {owner}')
            row['disabled'] = bool(disabled)
            self._write(doc)
            return self._public(row, doc)


def main(argv=None):
    """`python -m biosense.accounts` — list accounts, grant runs, decide requests."""
    import argparse
    import json
    ap = argparse.ArgumentParser(description='BioSense accounts on this machine')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list', help='every account with its runs today, its daily limit and its extra runs')
    g = sub.add_parser('grant', help='add extra runs to one account, spent once the day\'s are')
    g.add_argument('email')
    g.add_argument('runs', type=int)
    lim = sub.add_parser('limit', help="set one account's daily limit (0 = no limit)")
    lim.add_argument('email')
    lim.add_argument('runs', type=int)
    sub.add_parser('requests', help='every open request for extra runs')
    ok = sub.add_parser('approve', help='grant a request (its asked-for runs, or --runs)')
    ok.add_argument('request_id')
    ok.add_argument('--runs', type=int, default=None)
    ok.add_argument('--note', default=None)
    no = sub.add_parser('decline', help='decline a request')
    no.add_argument('request_id')
    no.add_argument('--note', default=None)
    r = sub.add_parser('reset-password', help='set a new password for an account')
    r.add_argument('email')
    r.add_argument('password')
    d = sub.add_parser('disable', help='refuse sign-in for an account')
    d.add_argument('email')
    e = sub.add_parser('enable', help='allow sign-in for an account again')
    e.add_argument('email')
    c = sub.add_parser('create', help='create an account from the command line')
    c.add_argument('email')
    c.add_argument('password')
    a = ap.parse_args(argv)
    store = Store(settings=settings_from_env())
    if a.cmd == 'list':
        print(json.dumps(store.listing(), indent=1))
    elif a.cmd == 'grant':
        print(json.dumps(store.grant_runs(a.email, a.runs, granted_by='cli'), indent=1))
    elif a.cmd == 'limit':
        print(json.dumps(store.set_run_limit(a.email, a.runs), indent=1))
    elif a.cmd == 'requests':
        print(json.dumps(store.requests(open_only=True), indent=1))
    elif a.cmd == 'approve':
        print(json.dumps(store.decide_request(a.request_id, grant=True, decided_by='cli',
                                              runs=a.runs, note=a.note), indent=1))
    elif a.cmd == 'decline':
        print(json.dumps(store.decide_request(a.request_id, grant=False, decided_by='cli',
                                              note=a.note), indent=1))
    elif a.cmd == 'reset-password':
        print(json.dumps(store.reset_password(a.email, a.password), indent=1))
    elif a.cmd == 'disable':
        print(json.dumps(store.set_disabled(a.email, True), indent=1))
    elif a.cmd == 'enable':
        print(json.dumps(store.set_disabled(a.email, False), indent=1))
    elif a.cmd == 'create':
        # The CLI is the operator, so a closed sign-up or a sign-up code does
        # not apply to it.
        open_settings = Settings(enabled=True, signup='open', run_limit=store.settings.run_limit)
        who = Store(store.path, settings=open_settings).create(a.email, a.password)
        print(json.dumps(store.get(who.owner), indent=1))


if __name__ == '__main__':
    main()
