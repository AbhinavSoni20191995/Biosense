"""Readiness: whether this deployment can do what it offers, in named parts.

`/healthz` answers "the web process is alive". That is what a platform
healthcheck must depend on and all it should ever depend on: a runtime still
coming up must not get the container killed and restarted in a loop, and a
missing model key must not take the deterministic demonstration path down with
it.

`/readyz` answers the harder question a person has when the button does not
work. It is here, rather than in the HTTP handler, because it is the part worth
testing: the mapping from one probe reason code to the four things somebody wants
to know — is the runtime reachable, is the agent registered, is there an
executor, does it have model credentials — and the guarantee that none of the
answers carries a secret.

What may appear in the body: modes, labels, reason codes, remedies, booleans,
counts, and the runtime server's scheme and host. What may not, and is asserted
in the tests rather than reviewed by eye: the Omnigent token, the model key, any
filesystem path, or the configured workspace.
"""
from __future__ import annotations

from .. import contracts as K
from . import runtime as RT

# Which of the four parts a given reason code says is broken. Derived from the
# single reason the probe returns, so this page cannot disagree with the run that
# follows it: both come from the same probe.
_UNREACHABLE = ('runtime_unreachable', 'no_server_configured', 'sdk_not_installed')
_NO_AGENT = _UNREACHABLE + ('agent_not_registered', 'auth_required', 'auth_invalid')
# An executor is known to exist only when the probe got far enough to find one.
# `model_auth_missing` is reported by a host that IS online and has no key.
_HAVE_EXECUTOR = ('ok', 'model_auth_missing')


def parts(reason):
    """The four readiness facts implied by one probe reason code."""
    return {
        'omnigent_reachable': reason not in _UNREACHABLE,
        'agent_registered': reason not in _NO_AGENT,
        'executor_available': reason in _HAVE_EXECUTOR,
        'model_credentials': reason != 'model_auth_missing',
    }


def readiness(cfg, *, limits=None, usage=None, runs_writable=True, probe_fn=None):
    """`(http_status, body)` for /readyz.

    503 when this deployment offers a real runtime that cannot currently run, or
    when its runs directory is not writable — a real run needs to write
    artifacts, and a service that cannot is not ready even if it answers.

    A deployment that offers only the deterministic path is ready as soon as the
    web process is: that path needs no runtime at all.
    """
    real = [m for m in RT.REAL_MODES if m in cfg.allowed]
    body = {
        'ok': True,
        'mode': cfg.mode,
        'label': cfg.label,
        'hosted': cfg.hosted,
        'offers_real_ai': bool(real),
        'runtimes_offered': [cfg.label_for(m) for m in cfg.allowed],
        'runs_dir_writable': bool(runs_writable),
        'checks': {},
    }
    if limits is not None:
        body['limits'] = limits.describe()
    if usage is not None:
        body['usage'] = usage
    for mode in real:
        try:
            found = RT.available(cfg, mode, probe_fn=probe_fn)
        except K.ContractError as e:
            found = RT.reason('probe_failed', str(e), hosted=cfg.hosted)
        code = found.get('reason')
        body['checks'][mode] = {
            'ok': bool(found.get('ok')),
            'reason': code,
            'headline': found.get('headline'),
            'next_step': found.get('next_step'),
            'server': found.get('server'),
            'agent': cfg.agent,
            'executor': found.get('executor'),
            **parts(code),
        }
    if real and not any(c['ok'] for c in body['checks'].values()):
        body['ok'] = False
    if not runs_writable:
        body['ok'] = False
    if not real:
        body['note'] = ('This deployment offers the deterministic demonstration path only. '
                        'That path needs no runtime, so it is ready whenever the web process '
                        'is.')
    return (200 if body['ok'] else 503), body
