"""Who may do what: one place, server-side, consulted by every privileged path.

BioSense has three kinds of caller and they differ only in what they are allowed
to spend, never in what science means:

    public    nobody signed in. One shared local workspace on a laptop; an
              opaque per-browser workspace on a hosted deployment. Demo policy
              applies in full.
    user      signed in through the configured Omnigent accounts server. Owns
              their projects, their runs and their reports. Demo policy applies.
    admin     a signed-in user whose id the deployment lists in
              BIOSENSE_ADMIN_USER_IDS. Demo policy does not apply. Every system
              safety limit still does.

Three rules this module exists to hold, and each is a thing that would otherwise
be got wrong somewhere:

**Admin is bound to an issuer, not just to a name.** `/api/auth/login` used to
take the Omnigent server URL from the request body, so anyone could sign in
against a server they run themselves and return any user id — including an
id this deployment lists as an admin. Matching an id alone would therefore have
been a one-line privilege escalation. An identity is only ever admin when it was
issued by BIOSENSE_AUTH_SERVER, the one server this deployment trusts to say who
somebody is. With no trusted issuer configured there are no admins at all, which
is the correct default for a deployment that has not thought about it.

**The provider's own admin flag grants nothing here.** Omnigent reports
`is_admin` for its own server administration. It is kept as
`provider_is_admin` for display and never consulted for authorisation: a BioSense
admin is a BioSense operator, and the two are deliberately different roles.

**Admin is a spending privilege, not a reading privilege.** Nothing here grants
an admin access to another account's projects, datasets, runs or reports. Those
checks are ownership checks and they do not consult role at all. If a BioSense
administrative function over other people's data is ever wanted, it should be
added deliberately and visibly, not inherited from a quota exemption.
"""
from __future__ import annotations

import os

from .. import contracts as K

ROLE_PUBLIC = 'public'
ROLE_USER = 'user'
ROLE_ADMIN = 'admin'
ROLES = (ROLE_PUBLIC, ROLE_USER, ROLE_ADMIN)

LABELS = {
    ROLE_PUBLIC: 'DEMO ACCESS',
    ROLE_USER: 'ACCOUNT',
    ROLE_ADMIN: 'ADMIN',
}

# How the model call a run makes is paid for. The run records it, so a
# diagnostic can tell a public run from an operator's development run without
# anything sensitive being written down.
#
#   platform_demo   BioSense's own provider key, under the public demo caps
#   admin_platform  BioSense's own provider key, by a named operator, uncapped
#                   by demo policy
#   user_byok       PLANNED — NOT CURRENTLY IMPLEMENTED. The signed-in user's
#                   own provider key. Reserved here so adding it later is a new
#                   enum value and a new branch, not a migration of every run
#                   record and contract that already exists.
CREDENTIAL_MODES = ('platform_demo', 'admin_platform', 'user_byok')
IMPLEMENTED_CREDENTIAL_MODES = ('platform_demo', 'admin_platform')

ENV_ADMIN_IDS = 'BIOSENSE_ADMIN_USER_IDS'
ENV_AUTH_SERVER = 'BIOSENSE_AUTH_SERVER'


class Forbidden(K.ContractError):
    """An authenticated-or-not caller asked for something their role does not allow.

    Separate from ContractError's usual 400 because the request was well formed
    and the answer is about authority, not about shape. The HTTP layer answers it
    403, and it says what role is required without saying who holds it.
    """

    def __init__(self, message, *, required_role=ROLE_ADMIN):
        super().__init__(message)
        self.required_role = required_role
        self.status = 403


class Policy:
    """Which identities this deployment treats as operators.

    Immutable, read once at startup from the environment. Nothing in a request
    can change it: there is no body field, header or cookie that reaches here,
    which is the property that makes the admin role worth having.
    """

    def __init__(self, admin_ids=(), auth_server=None):
        self.admin_ids = frozenset(i for i in (_norm(x) for x in admin_ids) if i)
        self.auth_server = (auth_server or '').rstrip('/') or None

    @property
    def has_admins(self):
        return bool(self.admin_ids) and bool(self.auth_server)

    def describe(self):
        """What may be said about the policy in public. Never who the admins are."""
        return {
            'admin_configured': self.has_admins,
            'trusted_issuer_configured': bool(self.auth_server),
            # A count, not a list: it tells an operator their configuration was
            # read without naming anybody to a visitor.
            'admin_count': len(self.admin_ids) if self.auth_server else 0,
        }

    def is_admin(self, identity):
        """Whether *identity* is an operator of this deployment.

        Four things must all hold, and the second is the one that matters: an
        identity issued by a server this deployment does not trust is never an
        admin here, however it names itself.
        """
        if identity is None or not getattr(identity, 'authenticated', False):
            return False
        if not self.has_admins:
            return False
        if (getattr(identity, 'server', None) or '').rstrip('/') != self.auth_server:
            return False
        return _norm(getattr(identity, 'owner', '')) in self.admin_ids

    def role_for(self, identity):
        if self.is_admin(identity):
            return ROLE_ADMIN
        if identity is not None and getattr(identity, 'authenticated', False):
            return ROLE_USER
        return ROLE_PUBLIC

    def credential_mode(self, identity):
        """How a real run by *identity* would be paid for.

        Both implemented modes spend the deployment's own provider key. An
        admin's runs are not free — they are uncapped by demo policy, which is a
        different statement, and the interface says so in those words.
        """
        return 'admin_platform' if self.is_admin(identity) else 'platform_demo'

    def require_admin(self, identity, what='that'):
        if not self.is_admin(identity):
            raise Forbidden(
                f'{what} needs a BioSense operator account. Sign in with an account this '
                f'deployment lists as an administrator.')
        return True


def _norm(value):
    """Compare ids the way the account system stores them: trimmed, lower-case."""
    return (value or '').strip().lower()


def from_env(env=None):
    """Read the policy once, at startup.

    A misconfiguration is worth noticing here rather than at the first sign-in:
    naming admins without naming the server that authenticates them grants
    nothing, and a deployment that did that almost certainly meant to do both.
    """
    env = os.environ if env is None else env
    raw = (env.get(ENV_ADMIN_IDS) or '').replace('\n', ',')
    ids = [part.strip() for part in raw.split(',') if part.strip()]
    server = (env.get(ENV_AUTH_SERVER) or '').strip() or None
    if server:
        from . import runtime as RT
        mode = 'local_real_ai' if RT.is_loopback(server) else 'remote_real_ai'
        server = RT.validate_server(server, mode, env=env)
    if ids and not server:
        raise K.ContractError(
            f'{ENV_ADMIN_IDS} names administrators but {ENV_AUTH_SERVER} is not set, so there '
            f'is no server this deployment trusts to say who somebody is. An id alone is not '
            f'an identity: anyone could sign in against their own Omnigent server and claim '
            f'it. Set {ENV_AUTH_SERVER} to the accounts server that authenticates your users.')
    return Policy(admin_ids=ids, auth_server=server)
