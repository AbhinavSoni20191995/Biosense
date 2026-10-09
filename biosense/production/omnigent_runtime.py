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
import time

from .. import contracts as K
from . import activity as AC
from . import runtime as RT
from . import stages as ST

# Enough for a long discovery run; a session that stops producing events for this
# long has stopped, whatever its status says.
STREAM_IDLE_TIMEOUT_S = 60 * 30
PROBE_TIMEOUT_S = 6.0
SESSION_TITLE_LIMIT = 200
# A turn ending is not a session ending.
#
# The orchestrator is an async agent, and its own instructions say to dispatch to
# a specialist and END THE TURN — the inbox wakes it when the specialist answers.
# So `response.completed` arrives within seconds of the run starting, long before
# any science has happened. Watching until the first one and then declaring the
# run finished is why a real run came back in thirty seconds having written
# nothing: BioSense stopped looking while the agents were still being asked.
#
# The session is finished when the SERVER says it is idle, having completed at
# least one turn, and nothing more has arrived for a quiet period. Omnigent's own
# session statuses are launching / running / waiting / idle / failed, and
# `waiting` means exactly "the parent turn is parked on sub-agent work".
STREAM_POLL_S = 5.0          # how often a quiet stream is checked against the server
TURN_QUIET_S = 25.0          # idle this long after a turn before believing it is over
IDLE_RECHECK_S = 3.0         # between the confirmations that the session really has stopped
IDLE_CONFIRMATIONS = 2       # a momentary idle between turns must not end a run
MAX_SILENCE_S = 900.0        # nothing at all for this long: stop watching and say so
SESSION_LIVE = ('launching', 'running', 'waiting', 'in_progress', 'queued')
SESSION_DONE = ('idle', 'completed', 'complete', 'finished', 'cancelled')
# The specialists run in their own child sessions. A parent reads `idle` once it
# has dispatched and ended its turn, while its children are still working, and
# none of a child's events appear on the parent's stream. So the session tree is
# polled, and each working child's stream is tailed beside the parent's.
CHILD_POLL_S = 10.0          # how often the session tree is asked who is working
MAX_CHILD_TAILS = 8          # streams followed at once; the tree poll covers the rest
CHILD_TASK_DONE = ('completed', 'failed', 'cancelled')
# A child's own lifecycle is not the run's: its turn ending, its deltas and its
# reasoning are not shown. Its tool calls, results and messages are.
CHILD_DROPPED = ('delta', 'reasoning', 'status', 'waiting', 'accepted', 'terminal')
# A web run has nobody to answer the orchestrator. If it ends its turn without
# having asked any specialist anything — usually because it stopped to ask the
# person a question — it is told so, once, and the run carries on. Once only: a
# second stop is the orchestrator's decision and the run ends on it.
AUTO_CONTINUE_LIMIT = 1
AUTO_CONTINUE_TEXT = (
    'BioSense: nobody can answer in this session. This is a one-shot web discovery '
    'run (see the brief you were given), so a question to the person ends it with '
    'nothing to show. Do not wait for a reply. Write each question you would have '
    'asked down as an open question, with the assumption you are making instead, '
    'and proceed: dispatch to `literature` and `bioinformatics` with '
    '`sys_session_send`, then follow the brief\'s steps and write the files it '
    'names. If you genuinely cannot proceed, write down why in the run directory.')



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


def drive(cfg, brief, *, title=None, on_event=None, on_session=None, should_stop=None,
          inbox=None, paused=None):
    """Start a session, send the brief, and stream what comes back.

    *brief* is the rendered message text. It is placed in the event payload as a
    string value and is not inspected, reformatted or escaped on the way — there
    is nothing to escape, because it never enters a syntax.

    *on_event* receives mapped BioSense events (see `stages.map_event`).
    *on_session* receives the `Session` as soon as the ids exist, so a run can
    record them even if the stream later fails.
    *should_stop* is polled between events so a shutting-down server can let go.
    *inbox* is polled beside it: a message it returns is posted to the
    orchestrator as the next user turn and recorded in the run's stream. It is
    how BioSense tells a running orchestrator that time is nearly up, or that
    the person gave it more; it never decides anything for it.
    *paused* is polled too: while it returns True the orchestrator and every
    working specialist are interrupted (nothing is spent while a person
    decides), and the run holds instead of ending. When it turns False the run
    resumes, and the inbox carries the message that tells the orchestrator so.

    Returns a summary dict. Raises RuntimeUnavailable, and never starts anything
    synthetic.
    """
    if not cfg.is_real:
        raise K.ContractError(
            f'drive() is for a real runtime; this configuration is {cfg.mode!r}. The '
            f'synthetic path is a different function on purpose, so one can never stand '
            f'in for the other.')
    RT.check_workspace(cfg)
    RT.check_sandbox(cfg)
    try:
        return _run_async(_drive(cfg, brief, title=title, on_event=on_event,
                                on_session=on_session, should_stop=should_stop,
                                inbox=inbox, paused=paused))
    except RT.RuntimeUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        code, detail = _classify(e)
        raise RT.RuntimeUnavailable(code, detail,
                                    hosted=bool(getattr(cfg, 'hosted', False))) from None


async def _drive(cfg, brief, *, title, on_event, on_session, should_stop, inbox=None,
                 paused=None):
    client = _client(cfg)
    emit = on_event or (lambda _e: None)
    children = None
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
        turns, last_event, stopped_because, settled = 0, time.monotonic(), None, 0
        dispatched, nudges, said = False, 0, None
        holding = False

        def out(ev):
            if ev.get('stage'):
                reached.add(ev['stage'])
            emit(ev)

        children = _Children(client, session_id, out)
        stream = client.sessions.stream(session_id).__aiter__()
        while terminal is None:
            if should_stop and should_stop():
                await client.sessions.interrupt(session_id)
                await children.interrupt()
                terminal, error = 'failed', 'the run was stopped while it was in flight'
                break
            if paused and paused():
                if not holding:
                    # Interrupted, not ended: the sessions stay, and a
                    # message resumes them. Nothing runs while a person decides.
                    holding = True
                    try:
                        await client.sessions.interrupt(session_id)
                    except Exception:  # noqa: BLE001 - stopping is advisory
                        pass
                    await children.interrupt()
                    emit({'kind': 'note', 'stage': None, 'omnigent_type': 'biosense.paused',
                          'simple': 'Paused at the time limit. The agents are stopped until '
                                    'you continue or finish the run.',
                          'technical': 'paused: session and working children interrupted'})
                await asyncio.sleep(IDLE_RECHECK_S)
                continue
            if holding:
                holding = False
                last_event, settled = time.monotonic(), 0
                if stream is None:
                    stream = await _reopen(client, session_id)
            message = inbox() if inbox else None
            if message:
                await _deliver(client, session_id, emit, message)
                last_event, settled = time.monotonic(), 0
                if stream is None:
                    stream = await _reopen(client, session_id)
            raw = None
            if stream is None:
                # No tail to wait on: pause instead of spinning, then ask.
                await asyncio.sleep(IDLE_RECHECK_S)
            else:
                try:
                    raw = await asyncio.wait_for(stream.__anext__(), timeout=STREAM_POLL_S)
                except asyncio.TimeoutError:
                    pass
                except StopAsyncIteration:
                    # The server closed the tail. That is not the session ending —
                    # it does not replay, so the way to find out is to ask it.
                    stream = None
                except Exception as e:  # noqa: BLE001 - a dropped tail is not a failed run
                    emit({'kind': 'note', 'stage': None, 'simple': None,
                          'technical': f'event stream dropped ({type(e).__name__}); '
                                       f'reconnecting'})
                    stream = None

            # What the specialists did since the last pass, from their own streams.
            for ev in children.drain():
                last_event, settled = time.monotonic(), 0
                out(ev)
            seen = children.changes
            kids = await children.poll()
            if children.changes != seen:
                last_event, settled = time.monotonic(), 0

            if raw is not None:
                last_event, settled = time.monotonic(), 0
                mapped = ST.map_event(raw)
                if mapped is None:
                    continue
                if mapped.get('stage'):
                    reached.add(mapped['stage'])
                if mapped['kind'] in ('terminal', 'error') \
                        and mapped.get('stage') in ST.TERMINAL:
                    if mapped['stage'] == 'failed':
                        terminal, error = 'failed', mapped.get('technical')
                        emit(mapped)
                        break
                    # A turn finished. The agents may now be working, or the
                    # orchestrator may be about to be woken by its inbox, so this
                    # is recorded and watched rather than treated as the end.
                    turns += 1
                    emit({'kind': 'waiting', 'stage': None, 'omnigent_type': 'turn.completed',
                          'simple': f'The orchestrator finished turn {turns}. Waiting to see '
                                    f'whether a specialist answers or it continues.',
                          'technical': f'turn {turns} complete; session still being watched'})
                    continue
                if mapped['kind'] == 'tool' and mapped.get('tool') in ST.DISPATCH_TOOLS:
                    dispatched = True
                if mapped['kind'] == 'message' and mapped.get('role') == 'assistant':
                    said = mapped.get('technical')
                emit(mapped)
                continue

            # Nothing arrived. Ask the server what the session is actually doing,
            # which is the only thing that can tell "parked on a sub-agent" from
            # "finished": those look identical from a silent stream.
            quiet = time.monotonic() - last_event
            live = None
            try:
                live = await client.sessions.get(session_id)
                status = (getattr(live, 'status', None) or '').lower()
            except Exception:  # noqa: BLE001 - a failed poll is not a failed run
                status = ''
            if status == 'failed':
                terminal, error = 'failed', _task_error(getattr(live, 'last_task_error', None))
                break
            if status not in SESSION_LIVE:
                # About to count towards "finished": ask the tree fresh, because
                # an idle parent with a working child is the normal async shape.
                seen = children.changes
                kids = await children.poll(force=True)
                if children.changes != seen:
                    last_event, quiet, settled = time.monotonic(), 0.0, 0
            if status in SESSION_LIVE or kids:
                # Working, parked waiting for a specialist, or idle while one
                # works. None of those is over, so the tail is reopened and the
                # wait continues.
                settled = 0
                if stream is None:
                    stream = await _reopen(client, session_id)
            else:
                # Not live. Confirmed more than once before being believed,
                # because a session is briefly idle between turns and ending the
                # run there is exactly the bug this loop exists to fix.
                settled += 1
                # Once a specialist has answered, the parent is woken by its
                # inbox; that wake is given the full quiet period to arrive.
                if settled >= IDLE_CONFIRMATIONS and (
                        (stream is None and not children.count()) or quiet >= TURN_QUIET_S):
                    if not dispatched and not children.count() \
                            and nudges < AUTO_CONTINUE_LIMIT:
                        nudges += 1
                        await _continue(client, session_id, emit, said)
                        last_event, settled = time.monotonic(), 0
                        if stream is None:
                            stream = await _reopen(client, session_id)
                        continue
                    terminal = 'complete'
                    break
            if quiet >= MAX_SILENCE_S and not kids:
                terminal = 'failed'
                stopped_because = (
                    f'nothing happened for {int(quiet // 60)} minutes and the session reports '
                    f'{status or "no status"}')
                error = stopped_because
                break

        snapshot = await client.sessions.get(session_id)
        if snapshot.last_task_error and terminal != 'complete':
            terminal = 'failed'
            error = error or _task_error(snapshot.last_task_error)
        return {'session': rec.public(), 'terminal': terminal or 'complete',
                'error': error, 'stages_reached': sorted(reached),
                'status': snapshot.status, 'sub_agents': children.count(),
                'auto_continued': nudges, 'last_message': said}
    finally:
        if children is not None:
            await children.close()
        await client.close()


# A question after the run is one turn, not a run: bounded on its own.
ANSWER_TIMEOUT_S = 600


def answer(cfg, text, *, session_id=None, title=None, timeout_s=None):
    """Put one message to an orchestrator and return what it says back.

    For a question about a finished run. The run's own session is used when it
    still exists, because it holds everything the orchestrator reasoned; when it
    is gone, a fresh session is started and *text* must then tell it where the
    run's files are. Returns {answer, session_id, fresh, error, timed_out}. One
    turn is waited for, within *timeout_s*: a reply is the assistant's messages
    in that turn, and a turn that dispatches specialists is not followed.
    """
    if not cfg.is_real:
        raise K.ContractError('answer() is for a real runtime; a synthetic run has no '
                              'orchestrator to ask')
    RT.check_workspace(cfg)
    RT.check_sandbox(cfg)
    try:
        return _run_async(_answer(cfg, text, session_id=session_id,
                                  title=title or 'BioSense question',
                                  timeout_s=timeout_s or ANSWER_TIMEOUT_S))
    except RT.RuntimeUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        code, detail = _classify(e)
        raise RT.RuntimeUnavailable(code, detail,
                                    hosted=bool(getattr(cfg, 'hosted', False))) from None


async def _answer(cfg, text, *, session_id, title, timeout_s):
    client = _client(cfg)
    try:
        fresh = False
        if session_id:
            try:
                live = await client.sessions.get(session_id)
                if (getattr(live, 'status', None) or '').lower() == 'failed':
                    session_id = None
            except Exception:  # noqa: BLE001 - a session the server lost is a fresh start
                session_id = None
        if not session_id:
            agent = await client.sessions.resolve_agent(cfg.agent)
            where = await _find_executor(client, agent.harness)
            if where['kind'] == 'none':
                raise RT.RuntimeUnavailable(where['reason'], where['detail'],
                                            hosted=bool(getattr(cfg, 'hosted', False)))
            created, _runner = await _create_session(client, cfg, agent, where,
                                                     title[:SESSION_TITLE_LIMIT])
            session_id, fresh = str(created['id']), True
        # Opened before the question is posted, so the reply cannot slip past.
        stream = client.sessions.stream(session_id).__aiter__()
        await _post_user(client, session_id, text)
        said, error, done, timed_out = [], None, False, False
        deadline = time.monotonic() + timeout_s
        while not done:
            if time.monotonic() > deadline:
                timed_out = True
                try:
                    await client.sessions.interrupt(session_id)
                except Exception:  # noqa: BLE001 - stopping is advisory
                    pass
                break
            raw = None
            if stream is None:
                await asyncio.sleep(IDLE_RECHECK_S)
            else:
                try:
                    raw = await asyncio.wait_for(stream.__anext__(), timeout=STREAM_POLL_S)
                except asyncio.TimeoutError:
                    pass
                except StopAsyncIteration:
                    stream = None
                except Exception:  # noqa: BLE001 - a dropped tail is reopened
                    stream = None
            if raw is None:
                if stream is None and said:
                    try:
                        live = await client.sessions.get(session_id)
                        if (getattr(live, 'status', None) or '').lower() not in SESSION_LIVE:
                            done = True
                    except Exception:  # noqa: BLE001
                        done = True
                elif stream is None:
                    stream = await _reopen(client, session_id)
                    if stream is None:
                        await asyncio.sleep(IDLE_RECHECK_S)
                continue
            mapped = ST.map_event(raw)
            if mapped is None:
                continue
            if mapped['kind'] == 'message' and mapped.get('role') == 'assistant':
                said.append(mapped.get('technical') or '')
            elif mapped['kind'] in ('terminal', 'error') and mapped.get('stage') in ST.TERMINAL:
                if mapped['stage'] == 'failed':
                    error = mapped.get('technical') or 'the session failed'
                done = True
        return {'answer': '\n\n'.join(t for t in said if t.strip()) or None,
                'session_id': session_id, 'fresh': fresh, 'error': error,
                'timed_out': timed_out}
    finally:
        await client.close()


def _child_busy(row):
    """Omnigent's own "is this sub-agent still working" predicate, or a copy of it."""
    try:
        from omnigent_client import child_summary_busy  # noqa: PLC0415 - lazy, like _sdk
        return bool(child_summary_busy(row))
    except ImportError:
        task = row.get('current_task_status')
        if int(row.get('pending_elicitations_count') or 0) > 0 or row.get('busy') \
                or task == 'launching':
            return True
        return task is not None and task not in CHILD_TASK_DONE


class _Children:
    """The specialists' sessions under one run, which its own stream never shows.

    Two sources, both read-only: the session tree, polled, which says who exists
    and who is working; and each working child's stream, tailed, which carries
    the tool calls that move the stage ticks. Nothing here starts, steers or
    answers a child — that is the orchestrator's job. A server without the tree
    endpoint gives `None` from `poll`, and the loop falls back to the parent's
    status alone, which is how it behaved before.
    """

    def __init__(self, client, root, out):
        self.client, self.root, self.out = client, root, out
        self.state = {}           # child id -> 'working' | 'finished' | 'failed'
        self.agent = {}           # child id -> specialist name, or None
        self.tails = {}           # child id -> asyncio task pumping its stream
        self.queue = asyncio.Queue()
        self.polled_at = None
        self.busy = None
        self.changes = 0          # state transitions seen; a change is activity
        self.supported = callable(getattr(client.sessions, 'child_sessions_tree', None))

    def count(self):
        return len(self.state)

    async def poll(self, force=False):
        """Whether any child is working: True, False, or None when unknowable."""
        if not self.supported:
            return None
        now = time.monotonic()
        if not force and self.polled_at is not None and now - self.polled_at < CHILD_POLL_S:
            return self.busy
        self.polled_at = now
        try:
            rows = await self.client.sessions.child_sessions_tree(self.root)
        except Exception:  # noqa: BLE001 - a failed poll says nothing either way
            self.busy = None
            return None
        busy = False
        for row in rows or ():
            if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                continue
            cid = row['id']
            working = _child_busy(row)
            busy = busy or working
            task = (row.get('current_task_status') or '').lower()
            now_state = 'working' if working else ('failed' if task == 'failed' else 'finished')
            if cid not in self.agent:
                self.agent[cid] = AC.agent_named(
                    f"{row.get('agent_name') or ''} {row.get('title') or ''}")
            prev = self.state.get(cid)
            if prev != 'working' and (working or prev is None):
                # New, or woken for another task. A child first seen already
                # finished was quick, and still gets its start recorded.
                self._say(cid, row, 'started')
            if not working and prev != now_state:
                # The tree row only carries a message preview; a child that
                # crashed before answering has none, and its reason is on the
                # session itself. One extra read, only on a failure.
                why = await self._why(cid) if now_state == 'failed' else None
                self._say(cid, row, now_state, why)
            self.state[cid] = now_state
            if working:
                self._tail(cid)
        self.busy = busy
        return busy

    async def _why(self, cid):
        try:
            live = await self.client.sessions.get(cid)
        except Exception:  # noqa: BLE001 - the failure is already being reported
            return None
        err = getattr(live, 'last_task_error', None)
        return _task_error(err) if err else None

    def _say(self, cid, row, state, why=None):
        self.changes += 1
        agent = self.agent.get(cid)
        name = (AC.AGENT_LABEL.get(agent)
                or AC._short(row.get('title') or row.get('agent_name') or 'A specialist', 60))
        preview = AC._short(row.get('last_message_preview'), 200)
        simple = {'started': f'{name} agent is working.',
                  'finished': f'{name} agent finished its task.',
                  'failed': f'{name} agent stopped with an error.'}[state]
        technical = f'child session {cid} ({row.get("agent_name") or row.get("title") or "?"})' \
                    f' {state}'
        detail = AC._short(why, 700) or preview
        if detail and state != 'started':
            technical += f': {detail}'
        self.out({'kind': 'subagent', 'agent': agent, 'child_session_id': cid, 'state': state,
                  'task': row.get('title'),
                  'stage': ST.stage_for_agent(agent) if state == 'started' else None,
                  'simple': simple, 'technical': technical})

    def _tail(self, cid):
        if cid in self.tails or len(self.tails) >= MAX_CHILD_TAILS:
            return
        self.tails[cid] = asyncio.ensure_future(self._pump(cid))

    async def _pump(self, cid):
        try:
            async for raw in self.client.sessions.stream(cid):
                self.queue.put_nowait((cid, raw))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the tree poll still sees this child
            pass
        finally:
            self.tails.pop(cid, None)

    def drain(self):
        """The children's events that arrived since the last call, mapped."""
        rows = []
        while True:
            try:
                cid, raw = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return rows
            mapped = ST.map_event(raw)
            if mapped is None or mapped['kind'] in CHILD_DROPPED:
                continue
            if mapped['kind'] == 'error':
                # A specialist's failed step is a limitation of its answer, not
                # the end of the run; the tree poll reports a child that died.
                mapped = dict(mapped, kind='warning')
            rows.append(dict(mapped, agent=self.agent.get(cid), child_session_id=cid))

    async def interrupt(self):
        """Stop every child that is still working. Best effort."""
        for cid, state in list(self.state.items()):
            if state == 'working':
                try:
                    await self.client.sessions.interrupt(cid)
                except Exception:  # noqa: BLE001 - stopping is advisory
                    pass

    async def close(self):
        tails = list(self.tails.values())
        for t in tails:
            t.cancel()
        if tails:
            await asyncio.gather(*tails, return_exceptions=True)
        self.tails.clear()


async def _continue(client, session_id, emit, said):
    """Tell an orchestrator that stopped to wait for a person that nobody is there.

    Recorded in the run's own stream, with what it last said, so the record shows
    BioSense spoke and what it was answering. It approves nothing and decides
    nothing: it restates what the brief already said, once.
    """
    emit({'kind': 'note', 'stage': None, 'omnigent_type': 'biosense.auto_continue',
          'simple': 'The orchestrator stopped without asking any specialist, usually to '
                    'wait for an answer nobody can give in a web run. BioSense told it '
                    'once to proceed on stated assumptions.',
          'technical': 'auto-continue: ' + ((said or '(no closing message)')[:600])})
    await _post_user(client, session_id, AUTO_CONTINUE_TEXT)


async def _deliver(client, session_id, emit, message):
    """A message from BioSense to a running orchestrator, recorded as such.

    *message* is {'text', 'simple', 'technical'}: what the orchestrator is told,
    and how the run's own stream describes it. BioSense speaks here only about
    time — nearly up, or extended — never about the science.
    """
    emit({'kind': 'note', 'stage': None, 'omnigent_type': 'biosense.message',
          'simple': message['simple'], 'technical': message.get('technical') or message['text']})
    await _post_user(client, session_id, message['text'])


async def _post_user(client, session_id, text):
    await client.sessions.post_event(session_id, {
        'type': 'message',
        'data': {'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}})


async def _reopen(client, session_id):
    """A fresh tail on a session that is still working. None if it cannot be had."""
    try:
        return client.sessions.stream(session_id).__aiter__()
    except Exception:  # noqa: BLE001 - try again on the next pass
        return None


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
