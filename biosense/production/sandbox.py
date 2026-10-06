"""Can the agents' OS sandbox start on this machine, and how?

Omnigent runs every agent shell and file tool inside bubblewrap on Linux
(`os_env.sandbox.type: auto`). If bwrap cannot build its sandbox, each of those
tools fails before it starts: the run dispatches its specialists and none of them
can write a byte. That happened on the hosted service twice — first because the
image had no `bwrap`, then because the container would not let bwrap build the
sandbox it asks for.

This module answers the question by doing it: it builds the exact bwrap command
Omnigent would build, from the discovery_loop's own `os_env`, and runs `true`
inside it. When that fails it narrows down why, because the three likely causes
have three different remedies:

    missing              bwrap is not installed                → install bubblewrap
    user_namespaces      the host forbids unprivileged user    → a host that allows them;
                         namespaces, so no bwrap sandbox at all   nothing in-container helps
    fresh_proc           namespaces work, but mounting a new   → bind the existing /proc
                         /proc is refused (masked /proc in a      (Omnigent's own switch,
                         container)                               below)

For the last one Omnigent has a supported switch, `OMNIGENT_HOST_SANDBOX_BACKEND`:
on a backend it lists, bwrap binds the host's `/proc` instead of mounting a fresh
one. Omnigent's comment on it is the trade-off, stated plainly: the sandboxed
tool can see the container's process list and each process's command line, and
still cannot read another process's environment or memory (the user namespace
keeps those ptrace-gated). This container is single-tenant, and no process in it
carries a secret on its command line — the model key is in the environment —
so the trade is taken here, and only when it is the thing that makes the sandbox
work. Every other part of the sandbox (read-only workspace, write access only to
the runs directory, no network for the specialists, seccomp) is unchanged.

The agents are never run without the sandbox as a fallback. This container holds
a model key.

Run as `python -m biosense.production.sandbox --env` at boot: it prints the
NAME=VALUE lines the runtime processes need, and a human verdict on stderr.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .. import contracts as K

PROC_BIND_ENV = 'OMNIGENT_HOST_SANDBOX_BACKEND'
# The backend name Omnigent 0.16 lists in its proc-bind allow-list. It is the
# only effect of that variable in this version (checked: it is read solely by
# the bwrap backend's /proc decision).
PROC_BIND_VALUE = 'lakebox'
PASSTHROUGH_ENV = 'OMNIGENT_RUNNER_ENV_PASSTHROUGH'
TRIAL_TIMEOUT_S = 15


def _run(argv, cwd=None, env=None):
    try:
        r = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=TRIAL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f'{type(e).__name__}: {e}'
    return r.returncode == 0, (r.stderr or r.stdout or '').strip()[:400]


def _omnigent_argv(workspace, *, bind_proc):
    """The bwrap argv Omnigent itself would use for the orchestrator, or None."""
    return wrap([shutil.which('true') or '/bin/true'], workspace, bind_proc=bind_proc)


def wrap(argv, workspace, *, bind_proc=False):
    """*argv* wrapped in the bwrap sandbox Omnigent builds for the orchestrator.

    None when Omnigent is not importable (a checkout without the extra), in which
    case the caller decides whether to run unwrapped and say so.
    """
    try:
        from omnigent.inner.bwrap_sandbox import BwrapSandboxBackend
        from omnigent.spec import parser
    except ImportError:
        return None
    spec = parser.parse(K.ROOT / 'discovery_loop')
    os_env = spec.os_env
    if os_env.sandbox is not None:
        os_env.sandbox.type = 'linux_bwrap'
    backend = BwrapSandboxBackend()
    saved = os.environ.get(PROC_BIND_ENV)
    try:
        if bind_proc:
            os.environ[PROC_BIND_ENV] = PROC_BIND_VALUE
        else:
            os.environ.pop(PROC_BIND_ENV, None)
        policy = backend.resolve(os_env, Path(workspace))
        return backend.wrap_launcher_argv(list(argv), policy, Path(workspace))
    finally:
        if saved is None:
            os.environ.pop(PROC_BIND_ENV, None)
        else:
            os.environ[PROC_BIND_ENV] = saved


def _plain_argv(*, bind_proc):
    proc = ['--bind', '/proc', '/proc'] if bind_proc else ['--proc', '/proc']
    return ['bwrap', '--ro-bind', '/', '/', *proc, '--dev', '/dev',
            '--unshare-all', '--die-with-parent', 'true']


def diagnose(workspace=None, *, run=None):
    """What the agents' sandbox needs here. Never raises.

    Returns {'ok', 'mode', 'problem', 'detail', 'env'}:
      mode     'fresh_proc' (Omnigent's default works), 'bind_proc' (works with
               the /proc bind), or None
      problem  None, 'not_linux', 'missing', 'user_namespaces' or 'failed'
      env      variables the Omnigent processes need for *mode*, e.g. the bind
    """
    run = run or _run
    workspace = workspace or str(K.ROOT)
    out = {'ok': False, 'mode': None, 'problem': None, 'detail': None, 'env': {}}
    if not sys.platform.startswith('linux'):
        out.update(ok=True, problem='not_linux',
                   detail='not Linux: Omnigent uses the platform sandbox here')
        return out
    if shutil.which('bwrap') is None:
        out.update(problem='missing', detail="the 'bwrap' binary is not on PATH")
        return out

    def trial(bind_proc):
        try:
            argv = _omnigent_argv(workspace, bind_proc=bind_proc)
        except Exception as e:  # noqa: BLE001 - a broken spec is reported, not raised
            return False, f'could not build the sandbox command: {type(e).__name__}: {e}'
        return run(argv or _plain_argv(bind_proc=bind_proc), cwd=workspace)

    ok, detail = trial(False)
    if ok:
        out.update(ok=True, mode='fresh_proc')
        return out
    first = detail
    # Can bwrap make a user namespace at all? If not, nothing below can help.
    ns_ok, ns_detail = run(['bwrap', '--ro-bind', '/', '/', '--unshare-user', 'true'])
    if not ns_ok:
        out.update(problem='user_namespaces',
                   detail=f'this host does not let bwrap create a user namespace: '
                          f'{ns_detail or first}')
        return out
    ok, detail = trial(True)
    if ok:
        out.update(ok=True, mode='bind_proc', detail=f'fresh /proc refused ({first})',
                   env={PROC_BIND_ENV: PROC_BIND_VALUE})
        return out
    out.update(problem='failed', detail=first or detail)
    return out


# The only names the boot script will export from this module's output. It reads
# NAME=VALUE lines and exports a name only if it is on this list, so nothing here
# is ever evaluated as shell.
EXPORTABLE = (PROC_BIND_ENV, PASSTHROUGH_ENV)


def env_lines(found, env=None):
    """NAME=VALUE lines that give the Omnigent processes what *found* needs."""
    env = os.environ if env is None else env
    lines = []
    for name, value in sorted((found.get('env') or {}).items()):
        lines.append(f'{name}={value}')
        # The host strips its environment before starting a runner; a name it
        # does not know has to be passed through explicitly.
        names = [n for n in (env.get(PASSTHROUGH_ENV) or '').split(',') if n.strip()]
        if name not in names:
            names.append(name)
        lines.append(f'{PASSTHROUGH_ENV}={",".join(names)}')
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--workspace', default=None)
    ap.add_argument('--env', action='store_true',
                    help='print the NAME=VALUE lines the runtime processes need')
    a = ap.parse_args(argv)
    found = diagnose(a.workspace)
    if found['ok']:
        how = {'fresh_proc': 'bubblewrap works as Omnigent configures it',
               'bind_proc': 'bubblewrap works with the container /proc bound in '
                            '(a fresh /proc mount is refused here)'}.get(found['mode'],
                                                                         found['detail'])
        print(f'[sandbox] agents sandbox OK: {how}', file=sys.stderr)
    else:
        print(f'[sandbox] agents sandbox UNAVAILABLE ({found["problem"]}): {found["detail"]}',
              file=sys.stderr)
        print('[sandbox] real AI runs will be refused with agent_sandbox_unavailable; '
              'the agents are never run unsandboxed.', file=sys.stderr)
    if a.env:
        for line in env_lines(found):
            print(line)
    return 0 if found['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
