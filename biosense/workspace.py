"""Who is asking, and where their projects live.

BioSense does not have a password database, and this module is the reason it does
not need one. Real AI already requires an Omnigent account — a person who wants
it runs `omnigent login <server>` or signs in through BioSense, and Omnigent's
own accounts system (first-user-is-admin, invite-only, argon2 hashes, signed
session JWTs) does the authenticating. BioSense forwards the credentials once,
keeps the token it gets back **server-side**, and asks `GET /auth/me` who that
token belongs to. The answer is the workspace owner.

What that buys, stated precisely, because the difference matters:

* **A signed-in workspace is authenticated.** The owner id came from a server
  that checked a password. Another person cannot claim it by typing it.
* **A local workspace is not.** When no Omnigent server is configured — the
  synthetic-only deployment, or a laptop before anyone logs in — there is one
  workspace called `local`, and it is private because the machine is private,
  not because anything checked. `WorkspaceIdentity.authenticated` says which
  kind it is, and the interface is expected to say so in words.

Projects a person creates go under the private data root, in a directory named
for the owner, and are never written into the repository's committed `projects/`
directory and never served as static files. The two committed profiles stay what
they are: read-only templates to start from.

What is never stored here: a password (forwarded once, never written), a token
belonging to someone else's session, or anything at all in the browser beyond an
opaque session id.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import unicodedata
from pathlib import Path

from . import contracts as K
from .data import roots as DR

OWNER_RE = re.compile(r'^[a-z0-9][a-z0-9._@+-]{0,95}$')
LOCAL_OWNER = 'local'
SESSION_TTL_S = 12 * 60 * 60
MAX_SESSIONS = 500
SESSION_COOKIE = 'biosense_session'


class AuthError(K.ContractError):
    """Sign-in failed. Carries a reason the interface can show as it stands."""

    def __init__(self, message, *, reason='auth_failed', status=401):
        super().__init__(message)
        self.reason = reason
        self.status = status


def normalise_owner(name):
    """An owner id from an email or username, or a refusal.

    Lower-cased and NFKC-normalised so `Abhinav@x.com` and `abhinav@x.com` are
    one workspace rather than two, which is the kind of split that loses
    somebody's projects without any error ever being raised.
    """
    s = unicodedata.normalize('NFKC', (name or '').strip()).lower()
    if not OWNER_RE.match(s):
        raise K.ContractError(
            'a workspace is named by an email address or a username: lower-case letters, '
            'digits and . _ @ + - , up to 96 characters.')
    return s


def owner_dir_name(owner):
    """A filesystem-safe directory for an owner.

    Hashed rather than slugged: an email address contains characters that are
    awkward in a path on some systems, and two addresses must never collide into
    one directory. The readable name is kept inside the directory, not in it.
    """
    owner = normalise_owner(owner)
    if owner == LOCAL_OWNER:
        return LOCAL_OWNER
    return 'u-' + hashlib.sha256(owner.encode()).hexdigest()[:24]


class WorkspaceIdentity:
    """Who a request is acting as."""

    def __init__(self, owner, *, authenticated=False, server=None, token=None,
                 display=None, is_admin=False):
        self.owner = normalise_owner(owner)
        self.authenticated = bool(authenticated)
        self.server = server
        self.token = token            # never serialised; see public()
        self.display = display or self.owner
        self.is_admin = bool(is_admin)

    @property
    def is_local(self):
        return self.owner == LOCAL_OWNER and not self.authenticated

    def public(self):
        """What the browser may see. No token, ever."""
        return {
            'owner': self.owner, 'display': self.display,
            'authenticated': self.authenticated,
            'server': self.server, 'is_admin': self.is_admin,
            'note': (
                'Signed in through Omnigent. Your projects are stored under your account and '
                'are not listed for anyone else.' if self.authenticated else
                'Not signed in. This instance has one local workspace, private because this '
                'machine is private — nothing here checked who you are. Sign in through an '
                'Omnigent server for an account-owned workspace.'),
        }


def local_identity():
    return WorkspaceIdentity(LOCAL_OWNER, authenticated=False, display='local workspace')


# ── sessions ────────────────────────────────────────────────────────────
class SessionStore:
    """Server-side sessions. The browser gets an opaque id and nothing else.

    In memory on purpose: a restart signs people out, which is the correct
    trade for a process that is not a credential store. The token lives here and
    in no response body, so an XSS in the page cannot read it.
    """

    def __init__(self, ttl_s=SESSION_TTL_S, max_sessions=MAX_SESSIONS):
        self._by_id = {}
        self._lock = threading.Lock()
        self.ttl_s = ttl_s
        self.max_sessions = max_sessions

    def _evict(self):
        now = time.time()
        for sid, row in list(self._by_id.items()):
            if now - row['created_at'] > self.ttl_s:
                self._by_id.pop(sid, None)
        while len(self._by_id) > self.max_sessions:
            oldest = min(self._by_id, key=lambda k: self._by_id[k]['created_at'])
            self._by_id.pop(oldest, None)

    def create(self, identity):
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._evict()
            self._by_id[sid] = {'identity': identity, 'created_at': time.time()}
        return sid

    def get(self, sid):
        if not sid:
            return None
        with self._lock:
            row = self._by_id.get(sid)
            if row is None:
                return None
            if time.time() - row['created_at'] > self.ttl_s:
                self._by_id.pop(sid, None)
                return None
            return row['identity']

    def drop(self, sid):
        with self._lock:
            return self._by_id.pop(sid, None) is not None

    def count(self):
        with self._lock:
            return len(self._by_id)


def sign_in(server, username, password, *, login_fn=None):
    """Exchange Omnigent credentials for a workspace identity.

    The password is forwarded to the configured Omnigent server once and is never
    written anywhere by BioSense. What comes back is a session JWT, which is held
    server-side for the life of the session.

    *login_fn* is injected so the tests never need a server; it defaults to the
    real HTTP call.
    """
    from .production import runtime as RT
    server = RT.validate_server(server, 'remote_real_ai' if not RT.is_loopback(server)
                                else 'local_real_ai')
    if not (username or '').strip() or not password:
        raise AuthError('an email address or username and a password are both needed',
                        reason='missing_credentials', status=400)
    fn = login_fn or _omnigent_login
    result = fn(server, username.strip(), password)
    token = result.get('token')
    user = result.get('user') or {}
    uid = user.get('id') or username
    if not token:
        raise AuthError('the Omnigent server accepted the request but returned no session',
                        reason='auth_failed')
    return WorkspaceIdentity(uid, authenticated=True, server=server, token=token,
                             display=uid, is_admin=bool(user.get('is_admin')))


def _omnigent_login(server, username, password):
    """POST /auth/login on an Omnigent server. The one place a password travels.

    Uses the standard library rather than the Omnigent SDK: this is sign-in, and
    it must work on an installation that has not installed the optional extra —
    otherwise a person cannot even reach the page that tells them to install it.
    """
    import urllib.error
    import urllib.request
    body = json.dumps({'username': username, 'password': password}).encode()
    req = urllib.request.Request(f'{server}/auth/login', data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            # Deliberately not distinguishing "no such user" from "wrong
            # password", which is the same posture the server itself takes.
            raise AuthError('that email address or password was not accepted',
                            reason='invalid_credentials') from None
        if e.code == 404:
            raise AuthError(
                'that server has no account sign-in. A loopback Omnigent server runs as a '
                'single local user and needs no login; a shared one has to be started with '
                'accounts enabled.', reason='accounts_not_enabled', status=400) from None
        raise AuthError(f'the Omnigent server answered {e.code}', reason='auth_failed') from None
    except urllib.error.URLError as e:
        raise AuthError(f'could not reach {server}: {e.reason}',
                        reason='runtime_unreachable', status=502) from None


def whoami(server, token, *, fetch_fn=None):
    """Who a token belongs to, from the server that issued it. None if it does not."""
    fn = fetch_fn or _omnigent_me
    try:
        user = fn(server, token)
    except AuthError:
        return None
    if not user or not user.get('id'):
        return None
    return WorkspaceIdentity(user['id'], authenticated=True, server=server, token=token,
                             display=user['id'], is_admin=bool(user.get('is_admin')))


def _omnigent_me(server, token):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f'{server}/auth/me',
                                 headers={'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b'{}')
    except urllib.error.HTTPError:
        return None
    except urllib.error.URLError as e:
        raise AuthError(f'could not reach {server}: {e.reason}',
                        reason='runtime_unreachable', status=502) from None


# ── where a workspace's own files live ──────────────────────────────────
def workspace_root(identity):
    """The directory this workspace owns, inside the private data root.

    Inside the private root deliberately: that boundary already exists, is
    already enforced by `roots.is_private_path`, and is already refused by every
    static handler. A second, weaker boundary for projects would be a second
    thing to get wrong.
    """
    base = DR.private_root() / 'workspaces' / owner_dir_name(identity.owner)
    return base


def projects_dir(identity, *, create=False):
    d = workspace_root(identity) / 'projects'
    if create:
        d.mkdir(parents=True, exist_ok=True)
        _write_owner_card(workspace_root(identity), identity)
    return d


def runs_dir(identity, *, create=False):
    d = workspace_root(identity) / 'runs'
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _write_owner_card(root, identity):
    """A readable note saying whose directory this is, since the name is a hash."""
    card = root / 'OWNER.json'
    if card.exists():
        return
    K.write_json_atomic(card, {
        'owner': identity.owner, 'authenticated': identity.authenticated,
        'server': identity.server, 'created_at': K.now_iso(),
        'note': 'A BioSense workspace. Its projects and runs belong to this owner and are '
                'never served over HTTP or committed to the repository.'})


def assert_owned(identity, path):
    """Refuse a path outside this workspace. The last check before a read or a write."""
    root = workspace_root(identity).resolve()
    target = Path(path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise K.ContractError(
            'that file is not inside your workspace. A workspace reads and writes its own '
            'directory and nothing else.') from None
    return target


def identity_from_env(env=None):
    """The workspace a process runs as when nobody has signed in.

    `BIOSENSE_WORKSPACE_OWNER` lets a single-user deployment name its workspace
    without a sign-in — useful on a laptop, and explicitly not authentication.
    """
    env = os.environ if env is None else env
    named = (env.get('BIOSENSE_WORKSPACE_OWNER') or '').strip()
    if not named:
        return local_identity()
    return WorkspaceIdentity(named, authenticated=False, display=named)
