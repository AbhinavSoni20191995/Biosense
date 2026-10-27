"""Accounts this deployment keeps itself: who may sign in, and how many runs each has.

A lab wants its people to open the page, make an account, and try a few
discovery runs. Nothing in that sentence needs a second server, so this module
is the account system: one JSON file under the private data root, a password
hash per person, and a counter of the real AI runs each account has started.

What it is, stated precisely:

* **Self-service.** Anyone who can reach the page can create an account, unless
  the deployment sets a sign-up code (`BIOSENSE_SIGNUP_CODE`), in which case the
  code is asked for once, at sign-up. Closing sign-up altogether is
  `BIOSENSE_SIGNUP=closed`.
* **A password is never stored.** `hashlib.scrypt` with a per-account salt,
  compared in constant time. The standard library does it, so sign-in works on
  an installation that has installed nothing optional.
* **An allowance, not a meter.** Each account may start `run_limit` real AI runs
  in all (`BIOSENSE_RUNS_PER_ACCOUNT`, default 5). A run is counted when it
  starts; deleting it gives nothing back; an operator may raise one account's
  limit with the CLI. The synthetic demonstration path costs nothing and is not
  counted. Administrators (`BIOSENSE_ADMIN_USER_IDS`) are exempt, as they are
  from every demo cap.
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
from pathlib import Path

from . import contracts as K
from . import workspace as WS
from .data import roots as DR

ENV_ACCOUNTS = 'BIOSENSE_ACCOUNTS'            # on (default) | off
ENV_SIGNUP = 'BIOSENSE_SIGNUP'                # open (default) | closed
ENV_SIGNUP_CODE = 'BIOSENSE_SIGNUP_CODE'      # optional: a code sign-up must quote
ENV_RUN_LIMIT = 'BIOSENSE_RUNS_PER_ACCOUNT'   # real AI runs per account, default 5

DEFAULT_RUN_LIMIT = 5
MIN_PASSWORD = 8
MAX_PASSWORD = 256
MAX_ACCOUNTS = 10000
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
    """The account has started every real AI run it was given.

    A ContractError so the HTTP layer answers it as a refusal; it carries the
    counts so the page can say exactly what was used rather than "no".
    """

    def __init__(self, owner, used, limit):
        self.limit = 'account'
        self.used, self.run_limit = used, limit
        super().__init__(
            f'This account has started {used} of its {limit} real AI run(s), which is all it '
            f'was given for now. The demonstration path still works without limit. Ask the '
            f'person running this BioSense for more runs; nothing is lost, every run you '
            f'started stays in your run list.')


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
    """Whether accounts are on, whether sign-up is open, and the allowance."""

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
                'note': (f'Create an account with your email address and a password, sign in, and '
                         f'start up to {self.run_limit} real AI runs. The demonstration path is '
                         f'not counted. Your projects, runs and reports are kept under your '
                         f'account.' if self.enabled else
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

    def __init__(self, path=None, settings=None):
        self.path = Path(path) if path else DR.private_root() / 'accounts' / FILE
        self.settings = settings or Settings()
        self._lock = threading.Lock()

    # ── file ────────────────────────────────────────────────
    def _read(self):
        try:
            doc = K.read_json(self.path)
        except (OSError, ValueError):
            return {'kind': 'accounts', 'accounts': {}}
        if not isinstance(doc, dict) or not isinstance(doc.get('accounts'), dict):
            return {'kind': 'accounts', 'accounts': {}}
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
                'created_at': K.now_iso(), 'runs_used': 0, 'run_limit': None,
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
            row = self._read()['accounts'].get(owner)
        return self._public(row) if row else None

    def _public(self, row):
        limit = row.get('run_limit')
        limit = self.settings.run_limit if limit is None else int(limit)
        used = int(row.get('runs_used') or 0)
        return {'email': row['email'], 'display': row.get('display') or row['email'],
                'created_at': row.get('created_at'), 'last_sign_in': row.get('last_sign_in'),
                'runs_used': used, 'run_limit': limit,
                'runs_remaining': max(0, limit - used), 'disabled': bool(row.get('disabled'))}

    def listing(self):
        with self._lock:
            rows = list(self._read()['accounts'].values())
        return sorted((self._public(r) for r in rows), key=lambda r: r['email'])

    def count(self):
        with self._lock:
            return len(self._read()['accounts'])

    # ── the allowance ───────────────────────────────────────
    def allowance(self, owner):
        """What this account has left, for the page to show before it refuses."""
        row = self.get(owner)
        if row is None:
            return None
        return {'role': 'user', 'capped': True, 'kind': 'account',
                'runs_limit': row['run_limit'], 'runs_used': row['runs_used'],
                'runs_remaining': row['runs_remaining'],
                'note': (f'{row["runs_remaining"]} of {row["run_limit"]} real AI runs left on '
                         f'this account. The demonstration path is not counted.')}

    def authorise_run(self, owner):
        """Count one real AI run against the account, or refuse by name.

        Called before anything is written, so a run that is not allowed never
        appears in the run list as though it might have worked. Deleting a run
        later gives nothing back: the allowance is what was spent, not what
        was kept.
        """
        owner = WS.normalise_owner(owner)
        with self._lock:
            doc = self._read()
            row = doc['accounts'].get(owner)
            if row is None:
                raise AccountError('no account by that name on this instance; sign in again',
                                   reason='no_account', status=401)
            if row.get('disabled'):
                raise AccountError('this account is disabled', reason='disabled', status=403)
            limit = row.get('run_limit')
            limit = self.settings.run_limit if limit is None else int(limit)
            used = int(row.get('runs_used') or 0)
            if limit and used >= limit:
                raise RunsExhausted(owner, used, limit)
            row['runs_used'] = used + 1
            self._write(doc)
            return {'runs_used': used + 1, 'run_limit': limit}

    # ── operator actions (CLI) ──────────────────────────────
    def set_run_limit(self, owner, limit):
        """An operator gives one account more (or fewer) runs. None restores the default."""
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
            return self._public(row)

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
            return self._public(row)

    def set_disabled(self, owner, disabled):
        owner = WS.normalise_owner(owner)
        with self._lock:
            doc = self._read()
            row = doc['accounts'].get(owner)
            if row is None:
                raise K.ContractError(f'no account {owner}')
            row['disabled'] = bool(disabled)
            self._write(doc)
            return self._public(row)


def main(argv=None):
    """`python -m biosense.accounts` — list accounts, give runs, reset a password."""
    import argparse
    import json
    ap = argparse.ArgumentParser(description='BioSense accounts on this machine')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list', help='every account with its runs used and left')
    g = sub.add_parser('grant', help="set one account's run limit")
    g.add_argument('email')
    g.add_argument('runs', type=int, help='total real AI runs the account may start')
    r = sub.add_parser('reset-password', help="set a new password for an account")
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
        print(json.dumps(store.set_run_limit(a.email, a.runs), indent=1))
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
