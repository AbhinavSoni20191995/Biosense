"""The one module that talks to Omnigent, through its own client library.

Everything here goes over the documented `/v1/sessions` API, via the
`omnigent_client` SDK that ships with omnigent. Nothing shells out. The sequence
is the one the SDK documents and the one the Omnigent REPL itself uses:

    resolve_agent(name)            GET   /v1/agents
    resolve_online_runner(harness) GET   /v1/runners
    create_from_agent_id(...)      POST  /v1/sessions
    bind_runner(sid, runner_id)    PATCH /v1/sessions/{id}
    post_event(sid, message)       POST  /v1/sessions/{id}/events
    stream(sid)                    GET   /v1/sessions/{id}/stream   (SSE)

Four properties this module exists to hold.

**The objective is never syntax.** It reaches the agent as a JSON string value in
an HTTP body. There is no `subprocess` import in this file and no code path that
builds a command line from anything a person typed. `tests/test_omnigent_runtime.py`
asserts both, including that a shell-metacharacter objective arrives byte for
byte.

**The import is lazy.** `omnigent-client` depends on the whole Omnigent platform,
which is an optional extra here. An installation without it must still run the
synthetic path and must still be able to say *why* real AI is unavailable, so the
import happens inside the functions and an ImportError becomes a reason code.

**Failures are named.** Every exception the SDK can raise is mapped to one of the
reason codes in `runtime.REASONS`, each with the command that fixes it. There is
no generic failure, and — the rule that matters most — **no fallback to the
synthetic runtime**. A person who asked for real AI and got a synthetic answer
would have no way of knowing.

**The token never leaves.** It is read from the environment by `runtime.py`, sent
as an Authorization header, and never placed in an event, a response body, an
exception message or a log line.
"""
from __future__ import annotations

import asyncio
import threading

from .. import contracts as K
from . import runtime as RT
from . import stages as ST

# Enough for a long discovery run; a session that stops producing events for this
# long has stopped, whatever its status says.
STREAM_IDLE_TIMEOUT_S = 60 * 30
PROBE_TIMEOUT_S = 6.0
SESSION_TITLE_LIMIT = 200


def _sdk():
    """The client library, or a RuntimeUnavailable naming the extra to install."""
    try:
        import omnigent_client  # noqa: PLC0415 - deliberately lazy; see the module docstring
    except ImportError as e:
        raise RT.RuntimeUnavailable('sdk_not_installed', str(e)) from None
    return omnigent_client


def _headers(cfg):
    return {'Authorization': f'Bearer {cfg.token}'} if cfg.token else {}


def _client(cfg, *, timeout=30.0):
    sdk = _sdk()
    return sdk.OmnigentClient(base_url=cfg.server, headers=_headers(cfg), timeout=timeout)


def _classify(exc):
    """An SDK or transport exception as a (reason, detail) pair.

    Matched on the exception's own type and status code rather than on the text
    of its message, so a reworded server error does not silently become
    'probe_failed'. The message is carried as detail, never as the reason.
    """
    status = getattr(exc, 'status_code', None)
    name = type(exc).__name__
    text = str(exc)
    if isinstance(exc, LookupError):            # resolve_agent found no such agent
        return 'agent_not_registered', text
    if status in (401, 407):
        return ('auth_invalid' if _has_token_hint(text) else 'auth_required'), text
    if status == 403:
        return 'auth_invalid', text
    if name in ('ConnectError', 'ConnectTimeout', 'ReadTimeout', 'TimeoutException',
                'RemoteProtocolError', 'NetworkError', 'ProxyError'):
        return 'runtime_unreachable', text
    if 'no runner' in text.lower():
        # The server's own wording for the one dispatch precondition. Verified
        # against a live server: posting a turn to an unbound session answers
        # "No runner bound for session".
        return 'no_runner_available', text
    if status is not None and status >= 500:
        return 'probe_failed', text
    return 'probe_failed', f'{name}: {text}'


def _has_token_hint(text):
    low = (text or '').lower()
    return 'expired' in low or 'invalid' in low or 'signature' in low


def _run_async(coro):
    """Run one coroutine on a private loop.

    The app is a threaded HTTP server and the SDK is async. A private loop per
    call keeps the two apart: nothing here assumes an ambient event loop, and a
    worker thread that dies does not take a shared loop with it.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
            asyncio.set_event_loop(None)


# ── probing ─────────────────────────────────────────────────────────────
def probe(cfg):
    """Can a session actually be started here? Returns a runtime.reason dict.

    Checks the three preconditions in the order they fail in practice: is the
    server there, is the agent registered, is a runner online. Each gets its own
    reason code because each has a different fix.
    """
    hosted = bool(getattr(cfg, 'hosted', False))
    if not cfg.server:
        return RT.reason('no_server_configured', hosted=hosted)
    try:
        return _run_async(_probe(cfg))
    except RT.RuntimeUnavailable as e:
        return RT.reason(e.reason, e.detail, hosted=hosted)
    except Exception as e:  # noqa: BLE001 - every failure must become a named reason
        code, detail = _classify(e)
        return RT.reason(code, detail, hosted=hosted)


async def _probe(cfg):
    client = _client(cfg, timeout=PROBE_TIMEOUT_S)
    try:
        agent = await client.sessions.resolve_agent(cfg.agent)
        where = await _find_executor(client, agent.harness)
        if where['kind'] == 'none':
            return RT.reason(where['reason'], where['detail'],
                             hosted=bool(getattr(cfg, 'hosted', False)))
        return {**RT.reason('ok'), 'agent_id': agent.id, 'harness': agent.harness,
                'runner_id': where.get('runner_id'), 'host_id': where.get('host_id'),
                'executor': where['kind']}
    finally:
        await client.close()


async def _find_executor(client, harness):
    """Who would actually run this session: a bound runner, or a host that spawns one.

    Omnigent has two topologies and they look different from the outside.

    * `omnigent run` spawns a **runner** itself, which registers at
      `/v1/runners`, and the client binds the session to it.
    * `omnigent start` / `omnigent host` registers a **host** at `/v1/hosts`.
      No runner exists until one is needed: creating a session with that
      `host_id` makes the server generate a binding token and tell the host to
      launch one.

    Checking only for a runner — which this adapter did at first — refuses the
    documented `omnigent start` flow, with a host sitting right there online.
    Verified against a real local server: after `omnigent host`, `/v1/runners`
    is `{"data": []}` and `/v1/hosts` holds an online host.
    """
    runner = await client.sessions.resolve_online_runner(
        harness=harness, canonicalize=_canonicalize())
    if runner is not None:
        return {'kind': 'runner', 'runner_id': runner}

    hosts = await _list_hosts(client)
    online = [h for h in hosts if h.get('status') == 'online']
    if not online:
        return {'kind': 'none', 'reason': 'no_runner_available',
                'detail': 'No online runner and no registered host, so nothing can execute '
                          'a session.'}
    canon = _canonicalize()
    wanted = {harness} | ({canon(harness)} if canon else set())
    # The harness name as a host reports it: `claude-sdk` becomes `claude_sdk`.
    wanted |= {w.replace('-', '_') for w in wanted}
    needs_auth = []
    for h in online:
        configured = h.get('configured_harnesses') or {}
        for name in wanted:
            state = configured.get(name)
            if state is True:
                return {'kind': 'host', 'host_id': h.get('host_id'), 'host_name': h.get('name')}
            if isinstance(state, str) and 'auth' in state:
                needs_auth.append((h.get('name'), name, state))
    if needs_auth:
        # A much better answer than "no runner": the machine is there and the
        # model provider is what is missing.
        name, hn, state = needs_auth[0]
        return {'kind': 'none', 'reason': 'model_auth_missing',
                'detail': f'Host {name!r} has the {hn!r} harness but reports {state!r}.'}
    return {'kind': 'none', 'reason': 'no_runner_available',
            'detail': f'{len(online)} host(s) are online but none has the {harness!r} '
                      f'harness configured.'}


async def _list_hosts(client):
    """Online hosts, from the server's documented `/v1/hosts` listing.

    The typed SDK has no helper for this one, so the call goes through the same
    client's HTTP session rather than a second transport: same base URL, same
    auth header, same redirect and proxy rules.
    """
    try:
        resp = await client.sessions._http.get(f'{client.sessions._base}/v1/hosts')
    except Exception:  # noqa: BLE001 - a server without the route is simply not host-based
        return []
    if resp.status_code != 200:
        return []
    try:
        body = resp.json()
    except ValueError:
        return []
    hosts = body.get('hosts') if isinstance(body, dict) else None
    return [h for h in (hosts or []) if isinstance(h, dict)]


async def _create_session(client, cfg, agent, where, title):
    """Create the session on whichever executor was found.

    With a host, `host_id` and `workspace` go in the create body and the server
    runs its launch flow; `workspace` is required there and the server validates
    it. With a caller-managed runner the typed helper is used and the runner is
    bound afterwards, which is the sequence the REPL itself follows.
    """
    if where['kind'] == 'host':
        resp = await client.sessions._http.post(
            f'{client.sessions._base}/v1/sessions',
            json={'agent_id': agent.id, 'title': title, 'host_id': where['host_id'],
                  'workspace': cfg.workspace})
        if resp.status_code >= 400:
            raise RT.RuntimeUnavailable(
                'probe_failed',
                f'the server refused to launch a session on host '
                f'{where.get("host_name") or where["host_id"]}: '
                f'{resp.status_code} {resp.text[:300]}')
        return resp.json(), None
    session = await client.sessions.create_from_agent_id(
        agent.id, title=title, workspace=cfg.workspace)
    bound = await client.sessions.bind_runner(session.id, runner_id=where['runner_id'])
    return {'id': session.id, 'harness': bound.harness or session.harness,
            'llm_model': bound.llm_model}, where['runner_id']


def _canonicalize():
    """Omnigent's own harness-name normaliser, when it is importable.

    `claude` and `claude-sdk` are the same harness. Matching a spec's spelling
    against a runner's without this misses a runner that is right there.
    """
    try:
        from omnigent.harness_aliases import canonicalize_harness
    except ImportError:
        return None
    return lambda name: canonicalize_harness(name) or name


# ── driving a run ───────────────────────────────────────────────────────
class Session:
    """A started Omnigent session, as BioSense records it."""

    def __init__(self, session_id, agent_id, runner_id, harness, llm_model=None):
        self.session_id = session_id
        self.agent_id = agent_id
        self.runner_id = runner_id
        self.harness = harness
        self.llm_model = llm_model

    def public(self):
        """What may be shown. Ids and a model name; never a token or a path."""
        return {'omnigent_session_id': self.session_id, 'agent_id': self.agent_id,
                'runner_id': self.runner_id, 'harness': self.harness,
                'llm_model': self.llm_model}


def drive(cfg, brief, *, title=None, on_event=None, on_session=None, should_stop=None):
    """Start a session, send the brief, and stream what comes back.

    *brief* is the rendered message text. It is placed in the event payload as a
    string value and is not inspected, reformatted or escaped on the way — there
    is nothing to escape, because it never enters a syntax.

    *on_event* receives mapped BioSense events (see `stages.map_event`).
    *on_session* receives the `Session` as soon as the ids exist, so a run can
    record them even if the stream later fails.
    *should_stop* is polled between events so a shutting-down server can let go.

    Returns a summary dict. Raises RuntimeUnavailable, and never starts anything
    synthetic.
    """
    if not cfg.is_real:
        raise K.ContractError(
            f'drive() is for a real runtime; this configuration is {cfg.mode!r}. The '
            f'synthetic path is a different function on purpose, so one can never stand '
            f'in for the other.')
    RT.check_workspace(cfg)
    try:
        return _run_async(_drive(cfg, brief, title=title, on_event=on_event,
                                on_session=on_session, should_stop=should_stop))
    except RT.RuntimeUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        code, detail = _classify(e)
        raise RT.RuntimeUnavailable(code, detail,
                                    hosted=bool(getattr(cfg, 'hosted', False))) from None


async def _drive(cfg, brief, *, title, on_event, on_session, should_stop):
    client = _client(cfg)
    emit = on_event or (lambda _e: None)
    try:
        agent = await client.sessions.resolve_agent(cfg.agent)
        where = await _find_executor(client, agent.harness)
        if where['kind'] == 'none':
            raise RT.RuntimeUnavailable(where['reason'], where['detail'],
                                        hosted=bool(getattr(cfg, 'hosted', False)))

        created, runner = await _create_session(
            client, cfg, agent, where,
            (title or 'BioSense discovery')[:SESSION_TITLE_LIMIT])
        session_id = str(created['id'])
        rec = Session(session_id, agent.id, runner or where.get('host_id'),
                      created.get('harness') or agent.harness, created.get('llm_model'))
        if on_session:
            on_session(rec)
        emit({'kind': 'accepted', 'stage': 'understanding_objective',
              'simple': 'Your question reached the discovery agents.',
              'technical': f'omnigent session {session_id} on '
                           f'{where["kind"]} {runner or where.get("host_id")} '
                           f'({rec.harness})'})

        # The one place the person's words cross into Omnigent: as a string value
        # in a JSON body. No argv, no shell, no template.
        await client.sessions.post_event(session_id, {
            'type': 'message',
            'data': {'role': 'user',
                     'content': [{'type': 'input_text', 'text': brief}]}})

        reached, terminal, error = set(), None, None
        async for raw in client.sessions.stream(session_id):
            if should_stop and should_stop():
                await client.sessions.interrupt(session_id)
                terminal = terminal or 'failed'
                error = error or 'the server stopped while the run was in flight'
                break
            mapped = ST.map_event(raw)
            if mapped is None:
                continue
            if mapped.get('stage'):
                reached.add(mapped['stage'])
            if mapped['kind'] in ('terminal', 'error') and mapped.get('stage') in ST.TERMINAL:
                terminal = mapped['stage']
                if mapped['stage'] == 'failed':
                    error = mapped.get('technical')
            emit(mapped)
            if terminal:
                break

        snapshot = await client.sessions.get(session_id)
        if snapshot.last_task_error and terminal != 'complete':
            terminal = 'failed'
            error = error or _task_error(snapshot.last_task_error)
        return {'session': rec.public(), 'terminal': terminal or 'complete',
                'error': error, 'stages_reached': sorted(reached),
                'status': snapshot.status}
    finally:
        await client.close()


def _task_error(err):
    """A session's last task error as one line, with its provider cause kept."""
    if isinstance(err, dict):
        code, msg = err.get('code'), err.get('message')
        return ' '.join(str(x) for x in (code, msg) if x) or str(err)
    return str(err)


def model_auth_reason(error_text):
    """Whether a failure is really missing model credentials, dressed as an LLM error.

    Worth distinguishing: 'the model provider rejected the key' and 'the model
    said something unexpected' need completely different next steps, and only
    the first is something the person can fix.
    """
    low = (error_text or '').lower()
    hints = ('api key', 'anthropic_api_key', 'unauthorized', 'authentication',
             'credential', 'no api key', '401')
    return 'model_auth_missing' if any(h in low for h in hints) else None


def interrupt(cfg, session_id):
    """Ask a running session to stop. Best effort; never raises into a handler."""
    try:
        _run_async(_interrupt(cfg, session_id))
        return True
    except Exception:  # noqa: BLE001 - stopping is advisory
        return False


async def _interrupt(cfg, session_id):
    client = _client(cfg, timeout=PROBE_TIMEOUT_S)
    try:
        await client.sessions.interrupt(session_id)
    finally:
        await client.close()


# A module-level guard so two runs cannot race on the same lazily imported SDK
# during the first import. Importing twice is harmless; importing while another
# thread is halfway through is not worth finding out about in production.
_IMPORT_LOCK = threading.Lock()


def ensure_available(cfg):
    """Raise RuntimeUnavailable unless a session could be started right now."""
    with _IMPORT_LOCK:
        _sdk()
    found = probe(cfg)
    if not found.get('ok'):
        raise RT.RuntimeUnavailable(found['reason'], found.get('detail'),
                                    hosted=bool(getattr(cfg, 'hosted', False)))
    return found
