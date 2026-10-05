"""Which runtime answers a discovery request, and whether it can.

Three modes, and the whole point of the module is that they are never confused
for one another:

    synthetic_demo   deterministic code over committed fixtures. No model, no
                     credentials, no network. It demonstrates what the system can
                     express; it measures no cell.
    local_real_ai    an Omnigent session on this machine, driving the real
                     discovery_loop agents. Spends model credits.
    remote_real_ai   the same, against a configured Omnigent server elsewhere.

Two rules this module exists to hold.

**No silent substitution.** If someone asks for real AI and the runtime is not
there, `probe()` says which of the seven things is missing and `RuntimeUnavailable`
carries that reason to the interface. It never starts a synthetic run instead. A
synthetic result presented as a real one is the single worst thing this product
could do, so the substitution is not available anywhere in the code, not even as
a fallback on an unexpected exception.

**No Omnigent import here.** This module decides and describes; it does not talk
to a server. `omnigent_runtime.py` is the only module that imports the SDK, and
it does so lazily, so an install without the optional extra still runs the whole
synthetic path and still produces an accurate "that is why you cannot use real
AI" answer rather than an ImportError at startup.

Configuration comes from the environment, never from a request:

    BIOSENSE_RUNTIME_MODE          synthetic | local | remote     (default: synthetic)
    BIOSENSE_ALLOWED_RUNTIMES      comma-separated, what this deployment may offer
    BIOSENSE_OMNIGENT_SERVER       http://127.0.0.1:6767 for local; required for remote
    BIOSENSE_OMNIGENT_AGENT        registered agent name (default: biosense_discovery_loop)
    BIOSENSE_OMNIGENT_WORKSPACE    the runner's working directory (default: repo root)
    BIOSENSE_OMNIGENT_TOKEN        bearer token; or
    BIOSENSE_OMNIGENT_TOKEN_FILE   a file holding one
    BIOSENSE_OMNIGENT_ALLOW_INSECURE=1   permit http:// to a non-loopback host

A browser never sees the token, the token file's path, or the workspace path.
`public_config()` is what the interface gets, and it carries the server's scheme
and host and a boolean.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from .. import contracts as K

MODES = ('synthetic_demo', 'local_real_ai', 'remote_real_ai')
REAL_MODES = ('local_real_ai', 'remote_real_ai')

# What the badge says. Never abbreviated anywhere else: the interface reads this.
LABELS = {
    'synthetic_demo': 'SYNTHETIC DEMO',
    'local_real_ai': 'REAL AI — LOCAL',
    'remote_real_ai': 'REAL AI — REMOTE',
}
BLURBS = {
    'synthetic_demo': 'Deterministic code over committed fixtures. No model is called and no '
                      'number measures a real cell.',
    'local_real_ai': 'Real AI agents, orchestrated by Omnigent on this machine. Spends model '
                     'credits.',
    'remote_real_ai': 'Real AI agents, orchestrated by a configured Omnigent server. Spends '
                      'model credits.',
}

# Short spellings accepted from the environment and from the interface.
ALIASES = {
    'synthetic': 'synthetic_demo', 'synthetic_demo': 'synthetic_demo', 'demo': 'synthetic_demo',
    'local': 'local_real_ai', 'local_real_ai': 'local_real_ai', 'real_local': 'local_real_ai',
    'remote': 'remote_real_ai', 'remote_real_ai': 'remote_real_ai', 'real_remote': 'remote_real_ai',
}

DEFAULT_LOCAL_SERVER = 'http://127.0.0.1:6767'
DEFAULT_AGENT = 'biosense_discovery_loop'

ENV_MODE = 'BIOSENSE_RUNTIME_MODE'
ENV_ALLOWED = 'BIOSENSE_ALLOWED_RUNTIMES'
ENV_SERVER = 'BIOSENSE_OMNIGENT_SERVER'
ENV_AGENT = 'BIOSENSE_OMNIGENT_AGENT'
ENV_WORKSPACE = 'BIOSENSE_OMNIGENT_WORKSPACE'
ENV_TOKEN = 'BIOSENSE_OMNIGENT_TOKEN'
ENV_TOKEN_FILE = 'BIOSENSE_OMNIGENT_TOKEN_FILE'
ENV_INSECURE = 'BIOSENSE_OMNIGENT_ALLOW_INSECURE'

# Every reason a real runtime can be unusable, with the step that fixes it. A
# reason code is part of the API: the interface switches on it, the tests assert
# on it, and nothing is reported as a bare "failed".
REASONS = {
    'ok': ('Ready.', None),
    'not_configured': (
        'This deployment does not offer that runtime.',
        f'Set {ENV_ALLOWED} to include it.'),
    'no_server_configured': (
        'No Omnigent server is configured for this mode.',
        f'Set {ENV_SERVER} to the server URL.'),
    'sdk_not_installed': (
        'The Omnigent client library is not installed in this environment.',
        'Install the optional extra: uv sync --extra omnigent'),
    'runtime_unreachable': (
        'Nothing answered at the configured Omnigent server.',
        'Start it with: omnigent start   (then reload this page)'),
    'auth_required': (
        'The Omnigent server requires authentication and none was configured.',
        'Run: omnigent login <server>   then set BIOSENSE_OMNIGENT_TOKEN'),
    'auth_invalid': (
        'The configured Omnigent credential was rejected or has expired.',
        'Run: omnigent login <server>   and replace BIOSENSE_OMNIGENT_TOKEN'),
    'agent_not_registered': (
        'The discovery_loop agent is not registered on that Omnigent server.',
        'Register it: omnigent server --agent discovery_loop'),
    'no_runner_available': (
        'The Omnigent server has no online runner, so no session can execute.',
        'Register this machine as a host: omnigent host --server <server>'),
    'workspace_mismatch': (
        'The runs directory is not inside the Omnigent runner workspace, so the agents would '
        'write their artifacts where BioSense cannot read them.',
        'Start BioSense with --runs inside the workspace, or set BIOSENSE_OMNIGENT_WORKSPACE.'),
    'model_auth_missing': (
        'The Omnigent runtime has no model provider credentials.',
        'Set ANTHROPIC_API_KEY where the runner runs, or run: claude auth login'),
    'probe_failed': (
        'The Omnigent server answered, but not in a way this version understands.',
        'Check that the server and the omnigent client library are the same version.'),
}


class RuntimeUnavailable(K.ContractError):
    """A real runtime was asked for and cannot be used.

    Carries a reason code from REASONS. It is deliberately a ContractError so the
    HTTP layer already answers it as a refusal with a message, and deliberately
    NOT something any caller may handle by running the synthetic path instead.
    """

    def __init__(self, reason, detail=None):
        self.reason = reason
        self.detail = detail
        headline, step = REASONS.get(reason, ('The runtime is unavailable.', None))
        parts = [headline]
        if detail:
            parts.append(detail)
        if step:
            parts.append(step)
        super().__init__(' '.join(parts))
        self.next_step = step
        self.headline = headline


def normalise_mode(name, *, required=True):
    """A mode name, however it was spelled, or a refusal naming the three."""
    key = (name or '').strip().lower().replace('-', '_')
    if key in ALIASES:
        return ALIASES[key]
    if not required and not key:
        return None
    raise K.ContractError(
        f'unknown runtime mode {name!r}. The modes are: ' + ', '.join(MODES))


@dataclass(frozen=True)
class RuntimeConfig:
    """A resolved runtime: the mode, where its server is, and what it may run."""

    mode: str
    server: str = None
    agent: str = DEFAULT_AGENT
    workspace: str = None
    token: str = None
    allowed: tuple = MODES
    runs_dir: str = None

    # ── description ────────────────────────────────────────────────────
    @property
    def label(self):
        return LABELS[self.mode]

    @property
    def is_real(self):
        return self.mode in REAL_MODES

    @property
    def server_display(self):
        """Scheme and host only. A path or a query could carry a token."""
        if not self.server:
            return None
        u = urlsplit(self.server)
        return f'{u.scheme}://{u.netloc}'

    def public(self):
        """What the browser may see. No token, no filesystem path."""
        return {
            'mode': self.mode, 'label': self.label, 'blurb': BLURBS[self.mode],
            'is_real': self.is_real, 'agent': self.agent if self.is_real else None,
            'server': self.server_display if self.is_real else None,
            'token_configured': bool(self.token) if self.is_real else None,
            'allowed_modes': list(self.allowed),
            'modes': [{'mode': m, 'label': LABELS[m], 'blurb': BLURBS[m],
                       'offered': m in self.allowed} for m in MODES],
        }

    def for_mode(self, mode):
        """This configuration as it applies to *mode*.

        One server is configured per deployment. Asked for a mode other than the
        configured one, local falls back to the loopback default — a server on
        this machine has a conventional address — and remote does not, because
        guessing a remote address is how a request ends up at the wrong server.
        """
        mode = normalise_mode(mode)
        if mode == self.mode:
            return self
        if mode == 'synthetic_demo':
            return replace(self, mode=mode, server=None)
        server = self.server
        if mode == 'local_real_ai' and (server is None or not is_loopback(server)):
            server = DEFAULT_LOCAL_SERVER
        if mode == 'remote_real_ai' and server is not None and is_loopback(server):
            server = None
        if server:
            server = validate_server(server, mode)
        return replace(self, mode=mode, server=server)


def _read_token(env):
    """The bearer token, from the variable or the file it names.

    A token file exists so a deployment can mount a secret rather than put it in
    an environment listing. Either way it is read here, held in the config, and
    never written to a response, an event or a log line.
    """
    tok = (env.get(ENV_TOKEN) or '').strip()
    if tok:
        return tok
    path = (env.get(ENV_TOKEN_FILE) or '').strip()
    if not path:
        return None
    try:
        tok = Path(path).expanduser().read_text().strip()
    except OSError as e:
        raise K.ContractError(
            f'{ENV_TOKEN_FILE} is set but the file could not be read ({e.strerror}). '
            f'The token is never read from a request, so there is no fallback.') from None
    return tok or None


def is_loopback(url):
    """Whether *url* addresses this machine.

    Mirrors the client library's own rule rather than importing it, because this
    module must work with the optional extra absent.
    """
    import ipaddress
    host = urlsplit(url).hostname
    if host is None:
        return False
    if host == 'localhost' or host.endswith('.localhost'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_server(url, mode):
    """Refuse a server URL that is malformed, or plaintext where it must not be.

    Validation happens once, at configuration time, rather than at each request:
    a URL that would send a bearer token over plaintext to a third party is a
    deployment mistake, and refusing to start with it is the right outcome.
    """
    u = urlsplit(url or '')
    if u.scheme not in ('http', 'https'):
        raise K.ContractError(
            f'{ENV_SERVER} must be an http:// or https:// URL; got {url!r}')
    if not u.hostname:
        raise K.ContractError(f'{ENV_SERVER} has no host: {url!r}')
    if u.username or u.password:
        raise K.ContractError(
            'credentials in the server URL are refused. Use BIOSENSE_OMNIGENT_TOKEN, '
            'which is never logged and never reaches the browser.')
    if mode == 'local_real_ai' and not is_loopback(url):
        raise K.ContractError(
            f'local real-AI mode points at {u.hostname!r}, which is not this machine. '
            f'Use remote mode for a server elsewhere, so its authentication is required '
            f'rather than assumed.')
    if (u.scheme == 'http' and not is_loopback(url)
            and (os.environ.get(ENV_INSECURE) or '').strip().lower() not in ('1', 'true', 'yes')):
        raise K.ContractError(
            f'refusing to send a bearer token to {url!r} over plaintext http. Use https, '
            f'or set {ENV_INSECURE}=1 if the hop is already inside a trusted network.')
    return url.rstrip('/')


def _allowed_from_env(env, default_mode):
    raw = (env.get(ENV_ALLOWED) or '').strip()
    if not raw:
        # Nothing declared: offer the synthetic path, which always works, plus
        # whatever this deployment was actually configured for. A deployment that
        # has not been pointed at a server does not advertise real AI.
        return tuple(dict.fromkeys(['synthetic_demo'] + (
            [default_mode] if default_mode in REAL_MODES else [])))
    modes = []
    for part in raw.split(','):
        part = part.strip()
        if part:
            modes.append(normalise_mode(part))
    if not modes:
        raise K.ContractError(f'{ENV_ALLOWED} is set but names no runtime mode')
    return tuple(dict.fromkeys(modes))


def from_env(*, runs_dir=None, env=None):
    """The configured runtime, read once at startup.

    A misconfiguration raises here rather than on the first request, because a
    server that starts and then refuses every run is harder to diagnose than one
    that declines to start and says why.
    """
    env = os.environ if env is None else env
    mode = normalise_mode(env.get(ENV_MODE) or 'synthetic_demo')
    server = (env.get(ENV_SERVER) or '').strip() or None
    if mode == 'local_real_ai' and not server:
        server = DEFAULT_LOCAL_SERVER
    if mode == 'remote_real_ai' and not server:
        raise K.ContractError(
            f'{ENV_MODE}=remote needs {ENV_SERVER}. BioSense does not guess a server '
            f'address, and it does not fall back to the synthetic path.')
    server = validate_server(server, mode) if mode in REAL_MODES else None
    allowed = _allowed_from_env(env, mode)
    if mode not in allowed:
        allowed = tuple(dict.fromkeys(list(allowed) + [mode]))
    workspace = (env.get(ENV_WORKSPACE) or '').strip() or str(K.ROOT)
    return RuntimeConfig(mode=mode, server=server,
                         agent=(env.get(ENV_AGENT) or '').strip() or DEFAULT_AGENT,
                         workspace=str(Path(workspace).expanduser().resolve()),
                         token=_read_token(env), allowed=allowed,
                         runs_dir=str(Path(runs_dir).resolve()) if runs_dir else None)


def check_workspace(cfg):
    """A local real-AI run needs the runs directory inside the runner's workspace.

    The runner resolves its own cwd from OMNIGENT_RUNNER_WORKSPACE, which is the
    directory `omnigent start` was launched from, and the agent bundle writes
    under `./runs` relative to it. If BioSense is reading somewhere else, the run
    would succeed and produce artifacts nobody ever sees. Refusing with both
    paths named is better than that silence.
    """
    if cfg.mode != 'local_real_ai' or not cfg.runs_dir or not cfg.workspace:
        return None
    runs, ws = Path(cfg.runs_dir).resolve(), Path(cfg.workspace).resolve()
    try:
        rel = runs.relative_to(ws)
    except ValueError:
        raise RuntimeUnavailable(
            'workspace_mismatch',
            f'The runs directory is {runs} and the workspace is {ws}.') from None
    return str(rel)


def available(cfg, mode=None, *, probe_fn=None):
    """Can this runtime run? Returns {'ok', 'reason', 'headline', 'next_step', …}.

    *probe_fn* is injected by the Omnigent adapter so this module never imports
    it; tests pass their own. Nothing here starts work.
    """
    mode = normalise_mode(mode or cfg.mode)
    out = {'mode': mode, 'label': LABELS[mode], 'offered': mode in cfg.allowed}
    if mode not in cfg.allowed:
        return {**out, **_reason('not_configured')}
    if mode == 'synthetic_demo':
        return {**out, **_reason('ok'), 'server': None}
    target = cfg.for_mode(mode)
    out['server'] = target.server_display
    if not target.server:
        return {**out, **_reason('no_server_configured')}
    try:
        check_workspace(target)
    except RuntimeUnavailable as e:
        return {**out, **_reason(e.reason, e.detail)}
    if probe_fn is None:
        from . import omnigent_runtime as OMNI
        probe_fn = OMNI.probe
    found = probe_fn(target)
    return {**out, **found}


def _reason(code, detail=None):
    headline, step = REASONS[code]
    return {'ok': code == 'ok', 'reason': code, 'headline': headline,
            'next_step': step, 'detail': detail}


def reason(code, detail=None):
    """Public spelling of _reason, for the adapter and the tests."""
    return _reason(code, detail)
