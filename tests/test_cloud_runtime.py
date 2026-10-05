"""The hosted deployment: what it must install, cap, recover and never leak.

Phase 3 made a public URL able to run the real agents. That changes the threat
model of this repository in three specific ways, and this file is the guard on
each of them:

* the deployment now needs a dependency it never needed before, and a deployment
  that installs it by hand is a deployment that will one day not;
* the container now holds a model key, so "how much may a stranger spend" has an
  answer that is enforced rather than assumed;
* a run now outlives the page that started it, so losing a browser must not lose
  a run — and a run that was interrupted must never come back looking finished.

Nothing here contacts a server, starts a container or calls a model. The probe is
injected, as everywhere else in this suite.
"""
import json
import os
import re
import stat
import time
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.production import budget as BU
from biosense.production import health as HL
from biosense.production import run_store as RS
from biosense.production import runtime as RT

ROOT = K.ROOT


class DependencyTests(unittest.TestCase):
    """The client library must arrive with the project, not with an instruction."""

    def test_the_omnigent_extra_is_declared_in_the_project(self):
        text = (ROOT / 'pyproject.toml').read_text()
        self.assertIn('omnigent = [', text,
                      'pyproject.toml declares no omnigent extra, so nothing installs the '
                      'client library that biosense/production/omnigent_runtime.py imports')
        self.assertRegex(text, r'omnigent = \["omnigent-client>=')

    def test_the_extra_is_resolved_in_the_lockfile(self):
        """A declared extra that is not locked still needs a network resolve at
        install time, which is exactly the hand step this must remove."""
        lock = (ROOT / 'uv.lock').read_text()
        self.assertIn('name = "omnigent-client"', lock)
        self.assertIn('name = "omnigent"', lock)

    def test_the_base_install_is_still_three_dependencies(self):
        """Ordinary CI, and the synthetic deployment, must not grow the whole
        Omnigent stack: the extra exists so they do not."""
        text = (ROOT / 'pyproject.toml').read_text()
        base = text.split('[project.optional-dependencies]')[0]
        self.assertNotIn('omnigent', base)

    def test_the_ai_image_installs_the_extra_and_the_other_one_does_not(self):
        ai = (ROOT / 'deploy' / 'Dockerfile.ai').read_text()
        plain = (ROOT / 'deploy' / 'Dockerfile').read_text()
        self.assertIn('uv sync --locked --extra omnigent', ai)
        self.assertNotIn('--extra omnigent', plain,
                         'the synthetic image must stay credential-free and small')

    def test_only_one_module_imports_the_sdk(self):
        """The import stays lazy and local, so an install without the extra runs
        the whole synthetic path and still explains why real AI is unavailable."""
        offenders = []
        for p in sorted((ROOT / 'biosense').rglob('*.py')):
            if 'omnigent_client' in p.read_text() and p.name != 'omnigent_runtime.py':
                offenders.append(str(p.relative_to(ROOT)))
        self.assertEqual([], offenders)


class HostedPostureTests(unittest.TestCase):
    """A hosted deployment runs the runtime itself, and must say so truthfully."""

    def cfg(self, **env):
        base = {'BIOSENSE_RUNTIME_MODE': 'local', 'BIOSENSE_HOSTED': '1',
                'BIOSENSE_ALLOWED_RUNTIMES': 'synthetic,local,remote'}
        return RT.from_env(env={**base, **env})

    def test_the_badge_says_online_not_local(self):
        """`local_real_ai` is the truth about the topology and a lie to the
        reader: LOCAL in a browser means "your computer"."""
        cfg = self.cfg()
        self.assertEqual('local_real_ai', cfg.mode)
        self.assertEqual('REAL AI — ONLINE', cfg.label)
        self.assertEqual('REAL AI — ONLINE', cfg.public()['label'])
        self.assertTrue(cfg.public()['hosted'])

    def test_only_this_deployments_own_runtime_is_called_online(self):
        """A hosted service that also offers a remote server is offering somebody
        else's machine; two different things must not read identically."""
        cfg = self.cfg()
        self.assertEqual('REAL AI — ONLINE', cfg.label_for('local_real_ai'))
        self.assertEqual('REAL AI — REMOTE', cfg.label_for('remote_real_ai'))
        self.assertEqual('SYNTHETIC DEMO', cfg.label_for('synthetic_demo'))

    def test_an_unhosted_deployment_is_unchanged(self):
        cfg = RT.from_env(env={'BIOSENSE_RUNTIME_MODE': 'local'})
        self.assertFalse(cfg.hosted)
        self.assertEqual('REAL AI — LOCAL', cfg.label)

    def test_the_remedies_a_visitor_is_shown_are_ones_a_visitor_can_act_on(self):
        """`omnigent host` is the right answer on your own machine and useless in
        a browser, where there is no shell and no access to the container."""
        for code in ('runtime_unreachable', 'no_runner_available', 'model_auth_missing'):
            own = RT.next_step_for(code, hosted=False)
            hosted = RT.next_step_for(code, hosted=True)
            self.assertNotEqual(own, hosted, code)
            self.assertNotIn('omnigent ', hosted, f'{code} tells a visitor to run a command')
            self.assertTrue(hosted)

    def test_a_hosted_failure_carries_the_hosted_remedy(self):
        e = RT.RuntimeUnavailable('no_runner_available', hosted=True)
        self.assertNotIn('omnigent host', str(e))
        self.assertIn('demonstration path', e.next_step)


class ReadinessTests(unittest.TestCase):
    """The four-part truth, and the promise that it carries no secret."""

    def cfg(self, **env):
        base = {'BIOSENSE_RUNTIME_MODE': 'remote', 'BIOSENSE_HOSTED': '1',
                'BIOSENSE_OMNIGENT_SERVER': 'https://omnigent.example.org/secret-path',
                'BIOSENSE_OMNIGENT_TOKEN': 'super-secret-token',
                'BIOSENSE_ALLOWED_RUNTIMES': 'synthetic,remote'}
        return RT.from_env(runs_dir=ROOT / 'runs', env={**base, **env})

    def test_ready_is_200_and_names_the_executor(self):
        code, body = HL.readiness(self.cfg(), probe_fn=lambda c: {
            **RT.reason('ok'), 'executor': 'host'})
        self.assertEqual(200, code)
        self.assertTrue(body['ok'])
        self.assertEqual('host', body['checks']['remote_real_ai']['executor'])

    def test_each_failure_names_which_part_is_missing(self):
        want = {
            'runtime_unreachable': (False, False, False, True),
            'agent_not_registered': (True, False, False, True),
            'no_runner_available': (True, True, False, True),
            'model_auth_missing': (True, True, True, False),
            'ok': (True, True, True, True),
        }
        for code, (reach, agent, ex, key) in want.items():
            p = HL.parts(code)
            self.assertEqual(reach, p['omnigent_reachable'], code)
            self.assertEqual(agent, p['agent_registered'], code)
            self.assertEqual(ex, p['executor_available'], code)
            self.assertEqual(key, p['model_credentials'], code)

    def test_an_unavailable_runtime_is_503_with_the_reason(self):
        code, body = HL.readiness(self.cfg(),
                                  probe_fn=lambda c: RT.reason('no_runner_available',
                                                               hosted=True))
        self.assertEqual(503, code)
        self.assertFalse(body['ok'])
        self.assertEqual('no_runner_available', body['checks']['remote_real_ai']['reason'])

    def test_an_unwritable_runs_directory_is_not_ready(self):
        """A real run writes artifacts. A service that cannot write them is not
        ready, however healthy the runtime is."""
        code, body = HL.readiness(self.cfg(), runs_writable=False,
                                  probe_fn=lambda c: RT.reason('ok'))
        self.assertEqual(503, code)
        self.assertFalse(body['runs_dir_writable'])

    def test_a_synthetic_only_deployment_is_ready_without_a_runtime(self):
        cfg = RT.from_env(env={})

        def explode(_c):
            raise AssertionError('the synthetic path must not be probed')

        code, body = HL.readiness(cfg, probe_fn=explode)
        self.assertEqual(200, code)
        self.assertEqual({}, body['checks'])
        self.assertIn('needs no runtime', body['note'])

    def test_readiness_carries_no_token_no_key_and_no_path(self):
        cfg = self.cfg()
        _, body = HL.readiness(cfg, limits=BU.from_env({'BIOSENSE_PUBLIC_DEMO': '1'}),
                               usage={'real_runs_last_24h': 2},
                               probe_fn=lambda c: RT.reason('ok'))
        blob = json.dumps(body)
        self.assertNotIn('super-secret-token', blob)
        self.assertNotIn(cfg.workspace, blob)
        self.assertNotIn('secret-path', blob, 'a URL path could itself be a credential')
        for word in ('ANTHROPIC_API_KEY', 'sk-ant', 'password'):
            self.assertNotIn(word, blob)


class BudgetTests(unittest.TestCase):
    """What a stranger may spend, and the refusal that is never a downgrade."""

    def test_no_caps_unless_a_deployment_asks(self):
        self.assertFalse(BU.from_env({}).any_cap)
        self.assertFalse(BU.from_env({'OTHER': '1'}).any_cap)

    def test_the_public_demo_posture_turns_them_all_on(self):
        lim = BU.from_env({'BIOSENSE_PUBLIC_DEMO': '1'})
        self.assertTrue(lim.public)
        self.assertEqual(BU.DEFAULTS['per_client'], lim.per_client)
        self.assertEqual(BU.DEFAULTS['daily'], lim.daily)
        self.assertTrue(lim.timeout_s)

    def test_one_cap_can_be_set_without_the_posture_and_zero_removes_one(self):
        lim = BU.from_env({'BIOSENSE_MAX_REAL_RUNS_PER_CLIENT': '5'})
        self.assertEqual(5, lim.per_client)
        self.assertTrue(lim.any_cap)
        off = BU.from_env({'BIOSENSE_PUBLIC_DEMO': '1', 'BIOSENSE_REAL_RUN_COOLDOWN_S': '0'})
        self.assertEqual(0, off.cooldown_s)

    def test_a_nonsense_limit_is_refused_at_startup(self):
        for bad in ({'BIOSENSE_MAX_REAL_RUNS_PER_DAY': 'lots'},
                    {'BIOSENSE_REAL_RUN_TIMEOUT_S': '-1'}):
            with self.assertRaises(K.ContractError):
                BU.from_env(bad)

    def test_the_timeout_is_ceilinged_however_it_is_configured(self):
        lim = BU.from_env({'BIOSENSE_REAL_RUN_TIMEOUT_S': str(999 * 3600)})
        self.assertEqual(BU.MAX_TIMEOUT_S, lim.timeout_s)

    def test_the_per_client_cap_refuses_by_name_and_says_when(self):
        clock = [1_000_000.0]
        lim = BU.Limits(public=True, per_client=2, daily=0, cooldown_s=0)
        led = BU.Ledger(lim, now=lambda: clock[0])
        led.authorise('a')
        clock[0] += 10
        led.authorise('a')
        clock[0] += 10
        with self.assertRaises(BU.BudgetExceeded) as e:
            led.authorise('a')
        self.assertEqual('per_client', e.exception.limit)
        self.assertGreater(e.exception.retry_after_s, 0)
        led.authorise('b')              # a different caller is unaffected

    def test_the_deployment_cap_refuses_everyone(self):
        clock = [1_000_000.0]
        led = BU.Ledger(BU.Limits(public=True, daily=2, cooldown_s=0), now=lambda: clock[0])
        led.authorise('a')
        led.authorise('b')
        with self.assertRaises(BU.BudgetExceeded) as e:
            led.authorise('c')
        self.assertEqual('daily', e.exception.limit)

    def test_counts_roll_off_after_a_day(self):
        clock = [1_000_000.0]
        led = BU.Ledger(BU.Limits(public=True, per_client=1, cooldown_s=0), now=lambda: clock[0])
        led.authorise('a')
        clock[0] += BU.DAY_S + 1
        led.authorise('a')
        self.assertEqual(1, led.state()['real_runs_last_24h'])

    def test_the_synthetic_path_is_never_capped(self):
        led = BU.Ledger(BU.Limits(public=True, per_client=1, cooldown_s=60))
        for _ in range(50):
            led.authorise('a', is_real=False)

    def test_a_refusal_points_at_the_uncapped_ways_to_run(self):
        """A cap that only says no sends somebody away. These say where to go."""
        led = BU.Ledger(BU.Limits(public=True, per_client=1, cooldown_s=0))
        led.authorise('a')
        with self.assertRaises(BU.BudgetExceeded) as e:
            led.authorise('a')
        self.assertIn('start_local_ai.sh', str(e.exception))

    def test_a_client_key_is_a_bucket_and_not_an_address(self):
        k = BU.client_key('10.0.0.7', '203.0.113.9, 10.0.0.1')
        self.assertNotIn('203.0.113.9', k)
        self.assertNotIn('10.0.0.7', k)
        self.assertEqual(k, BU.client_key('10.0.0.8', '203.0.113.9'),
                         'the left-most forwarded hop is the caller')
        self.assertNotEqual(k, BU.client_key('10.0.0.7', None))

    def test_the_limits_are_public_and_the_counts_name_nobody(self):
        blob = json.dumps(BU.from_env({'BIOSENSE_PUBLIC_DEMO': '1'}).describe())
        self.assertIn('max_real_runs_per_day', blob)
        led = BU.Ledger(BU.Limits(public=True, per_client=9, cooldown_s=0))
        key = BU.client_key('203.0.113.9')
        led.authorise(key)
        state = json.dumps(led.state())
        self.assertEqual(1, led.state()['real_runs_last_24h'])
        self.assertNotIn(key, state, 'the counts must not carry a caller bucket')

    def test_a_deadline_exists_only_when_a_timeout_does(self):
        self.assertIsNone(BU.Limits().deadline_from(100.0))
        self.assertEqual(700.0, BU.Limits(timeout_s=600).deadline_from(100.0))


class RunRecoveryTests(unittest.TestCase):
    """A run outlives the page that started it, and says what it really was."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.dir = self.tmp / 'ai-20260101-abc123'

    def test_a_finished_run_comes_back_from_its_id_alone(self):
        RS.write_state(self.dir, {'run_id': 'abc123def4567890', 'status': 'done',
                                  'runtime_mode': 'local_real_ai',
                                  'result': {'protocol': {'title': 'x'}}})
        for i in range(3):
            RS.append_event(self.dir, {'seq': i, 'kind': 'note', 'simple': f'e{i}'})
        got = RS.restore(self.tmp, 'abc123def4567890')
        self.assertEqual('done', got['status'])
        self.assertTrue(got['recovered'])
        self.assertEqual(3, got['event_count'])
        self.assertEqual('x', got['result']['protocol']['title'])
        self.assertEqual(2, len(RS.restore(self.tmp, 'abc123def4567890', after=0)['events']))

    def test_a_run_in_flight_when_the_process_died_is_interrupted_not_finished(self):
        """The one substitution that must never happen in either direction: a
        halted run is not a result, and it is not still running either."""
        RS.write_state(self.dir, {'run_id': 'abc123def4567890', 'status': 'running'})
        got = RS.restore(self.tmp, 'abc123def4567890')
        self.assertEqual('interrupted', got['status'])
        self.assertEqual('interrupted', got['error_reason'])
        self.assertTrue(got['interrupted'])
        self.assertIn('restarted', got['error'])
        self.assertIsNone(got.get('result'))
        self.assertTrue(got['next_step'])

    def test_an_unknown_id_is_nothing_rather_than_something(self):
        self.assertIsNone(RS.restore(self.tmp, 'ffffffffffffffff'))
        self.assertIsNone(RS.restore(self.tmp, ''))

    def test_a_corrupt_record_is_not_a_crash(self):
        self.dir.mkdir(parents=True)
        RS.state_path(self.dir).write_text('{not json')
        RS.events_path(self.dir).write_text('{"seq":0}\nnot json\n{"seq":1}\n')
        self.assertIsNone(RS.restore(self.tmp, 'abc123def4567890'))
        self.assertEqual(2, len(RS.read_events(self.dir)))

    def test_the_journal_is_bounded(self):
        self.dir.mkdir(parents=True)
        RS.events_path(self.dir).write_text('x' * (RS.MAX_JOURNAL_BYTES + 1))
        self.assertFalse(RS.append_event(self.dir, {'seq': 0}))

    def test_a_record_is_found_by_scan_when_the_directory_name_says_nothing(self):
        odd = self.tmp / 'somewhere-else'
        RS.write_state(odd, {'run_id': 'abc123def4567890', 'status': 'done'})
        self.assertEqual(odd, RS.find(self.tmp, 'abc123def4567890'))

    def test_writing_a_record_never_raises_into_a_run(self):
        """Losing recoverability is bad; killing a paid-for run because a disk
        was full would be worse."""
        self.assertFalse(RS.write_state('/proc/nonexistent/x', {'run_id': 'a'}))
        self.assertFalse(RS.append_event('/proc/nonexistent/x', {'seq': 0}))


class DeploymentTests(unittest.TestCase):
    """The deployment's own invariants, asserted rather than reviewed by eye."""

    def ai_env(self):
        """The ENV pairs declared in the AI image."""
        text = (ROOT / 'deploy' / 'Dockerfile.ai').read_text()
        env = {}
        for m in re.finditer(r'^\s*(?:ENV\s+)?([A-Z_][A-Z0-9_]*)=(\S+)', text, re.M):
            env.setdefault(m.group(1), m.group(2))
        return env

    def test_the_runs_directory_is_inside_the_runner_workspace(self):
        """The subtle one. The agents write under ./runs relative to the runner's
        workspace, and `check_workspace` refuses a real run when BioSense reads
        anywhere else — because that run would succeed and produce artifacts
        nobody ever sees. Mounting the volume at /data/runs instead of /app/runs
        is the mistake this test exists to catch.
        """
        env = self.ai_env()
        runs, ws = env.get('RUNS_DIR'), env.get('BIOSENSE_OMNIGENT_WORKSPACE')
        self.assertTrue(runs and ws, 'the AI image declares no runs dir or no workspace')
        self.assertTrue(Path(runs).is_relative_to(Path(ws)), f'{runs} is not inside {ws}')
        cfg = RT.from_env(runs_dir=runs, env={'BIOSENSE_RUNTIME_MODE': 'local',
                                              'BIOSENSE_OMNIGENT_WORKSPACE': ws})
        self.assertEqual('runs', RT.check_workspace(cfg))

    def test_the_ai_image_declares_the_hosted_posture_and_the_caps(self):
        env = self.ai_env()
        self.assertEqual('1', env.get('BIOSENSE_HOSTED'))
        self.assertEqual('1', env.get('BIOSENSE_PUBLIC_DEMO'))
        self.assertEqual('local', env.get('BIOSENSE_RUNTIME_MODE'))
        self.assertIn('local', env.get('BIOSENSE_ALLOWED_RUNTIMES', ''))

    def test_no_image_bakes_in_a_credential(self):
        for name in ('Dockerfile', 'Dockerfile.ai', 'start-ai.sh', 'railway.json',
                     'railway.ai.json'):
            text = (ROOT / 'deploy' / name).read_text()
            self.assertNotIn('sk-ant', text, name)
            self.assertNotRegex(text, r'ANTHROPIC_API_KEY\s*=\s*\S', name)

    def test_the_platform_healthcheck_is_liveness_not_readiness(self):
        """Pointing it at /readyz would restart the container while the runtime
        comes up, and keep restarting it if a key were missing — taking the
        demonstration path down with it."""
        for name in ('railway.json', 'railway.ai.json'):
            cfg = json.loads((ROOT / 'deploy' / name).read_text())
            self.assertEqual('/healthz', cfg['deploy']['healthcheckPath'], name)
        self.assertIn('/healthz', (ROOT / 'deploy' / 'Dockerfile.ai').read_text())

    def test_the_agent_bundle_is_in_the_ai_image_and_registered_from_source(self):
        """A request can never name an agent: the one agent is registered at boot
        from a path in the image."""
        docker = (ROOT / 'deploy' / 'Dockerfile.ai').read_text()
        boot = (ROOT / 'deploy' / 'start-ai.sh').read_text()
        self.assertIn('COPY discovery_loop/', docker)
        self.assertIn('--agent "$APP_ROOT/discovery_loop"', boot)

    def test_the_runtime_is_bound_to_loopback_only(self):
        boot = (ROOT / 'deploy' / 'start-ai.sh').read_text()
        self.assertIn('--host 127.0.0.1 --port "$OMNI_PORT"', boot)
        # the web app is the only thing bound to every interface
        self.assertEqual(1, boot.count('--host 0.0.0.0'))

    def test_the_boot_script_serves_the_page_even_when_the_runtime_fails(self):
        """A broken runtime must not take the public URL down: BioSense says
        which part is missing, and never answers with a synthetic run instead."""
        boot = (ROOT / 'deploy' / 'start-ai.sh').read_text()
        self.assertNotIn('exit 1', boot, 'a boot failure must not kill the web process')
        for marker in ('fall back to synthetic', 'synthetic_demo'):
            self.assertNotIn(marker, boot)

    def test_the_shell_scripts_interpolate_no_user_input(self):
        """There is no user input in them to interpolate — the objective travels
        as a JSON value over HTTP — and `eval` is how that would stop being true.
        """
        for name in ('deploy/start-ai.sh', 'scripts/start_local_ai.sh',
                     'scripts/check_local_ai.sh'):
            text = (ROOT / name).read_text()
            self.assertNotRegex(text, r'\beval\b', name)
            self.assertNotIn('shell=True', name)

    def test_the_launcher_and_the_checker_exist_and_are_runnable(self):
        for name in ('scripts/start_local_ai.sh', 'scripts/check_local_ai.sh'):
            p = ROOT / name
            self.assertTrue(p.is_file(), name)
            self.assertTrue(p.stat().st_mode & stat.S_IXUSR, f'{name} is not executable')
            text = p.read_text()
            self.assertTrue(text.startswith('#!/usr/bin/env bash'), name)
            self.assertRegex(text, r'\nset -[eu]',
                             f'{name} does not stop on an unset variable or a failed step')

    def test_the_launcher_is_loopback_only_and_names_the_one_agent(self):
        text = (ROOT / 'scripts' / 'start_local_ai.sh').read_text()
        self.assertIn('BIOSENSE_RUNTIME_MODE=local', text)
        self.assertIn('--host 127.0.0.1', text)
        self.assertIn('--agent "$ROOT/discovery_loop"', text)
        self.assertNotIn('0.0.0.0', text,
                         'the local launcher must never expose the app to a network')

    def test_the_launcher_refuses_rather_than_starting_something_cheaper(self):
        text = (ROOT / 'scripts' / 'start_local_ai.sh').read_text()
        self.assertIn('die ', text)
        self.assertIn('will not start one while you asked for real AI', text)


class StopAndTimeoutTests(unittest.TestCase):
    """Stopping a run is available, bounded, and recorded as what it was."""

    def test_both_ways_a_run_stops_short_have_their_own_words(self):
        from biosense.production import app as APP
        self.assertIn('cancelled', APP.STOP_MESSAGES)
        self.assertIn('timed_out', APP.STOP_MESSAGES)
        for key, msg in APP.STOP_MESSAGES.items():
            self.assertTrue(msg, key)
        self.assertIn('finished answer', APP.STOP_MESSAGES['cancelled'])
        self.assertIn('finished answer', APP.STOP_MESSAGES['timed_out'])

    def test_a_run_notices_its_deadline_and_a_request_to_stop(self):
        from biosense.production import app as APP
        req = {'request_id': 'r', 'project_id': 'p', 'objective': 'o'}
        run = APP.DiscoveryRun('a' * 16, req, Path('/tmp/does-not-matter'), 'synthetic_demo',
                               deadline=time.time() - 1)
        self.assertTrue(run.should_stop())
        self.assertEqual('timed_out', run.cancelled)
        live = APP.DiscoveryRun('b' * 16, req, Path('/tmp/does-not-matter'), 'local_real_ai',
                                deadline=time.time() + 600)
        self.assertFalse(live.should_stop())
        live.stop('cancelled', APP.STOP_MESSAGES['cancelled'])
        self.assertTrue(live.should_stop())

    def test_a_finished_run_cannot_be_stopped_again(self):
        from biosense.production import app as APP
        req = {'request_id': 'r', 'project_id': 'p', 'objective': 'o'}
        run = APP.DiscoveryRun('c' * 16, req, Path('/tmp/does-not-matter'), 'synthetic_demo')
        run.finish('done', {'x': 1})
        self.assertFalse(run.stop('cancelled', 'x'))

    def test_the_adapter_polls_for_a_stop_between_events(self):
        """The timeout is only a bill cap if the stream actually honours it."""
        import inspect
        from biosense.production import omnigent_runtime as OMNI
        src = inspect.getsource(OMNI._drive)
        self.assertIn('should_stop()', src)
        self.assertIn('interrupt(session_id)', src)


class HttpTests(unittest.TestCase):
    """The new HTTP surface, against a live server on a loopback port.

    A synthetic-only deployment, because that is what CI is: no runtime is
    contacted anywhere in this class.
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        import tempfile
        import threading
        from http.server import ThreadingHTTPServer
        from biosense import workspace as WS
        from biosense.production import app as APP
        cls.APP = APP
        cls.shutil = shutil
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / 'runs').mkdir()
        cfg = RT.from_env(runs_dir=cls.tmp / 'runs', env={})
        limits = BU.from_env({'BIOSENSE_PUBLIC_DEMO': '1'})
        APP.Handler.runs_dir = cls.tmp / 'runs'
        APP.Handler.static_dir = K.ROOT / 'webapp'
        APP.Handler.registry = APP.Registry(cls.tmp / 'runs')
        APP.Handler.runtime_cfg = cfg
        APP.Handler.limits = limits
        APP.Handler.discovery = APP.DiscoveryRegistry(cls.tmp / 'runs', cfg, limits)
        APP.Handler.sessions = WS.SessionStore()
        APP.Handler.default_identity = WS.local_identity()
        APP.Handler._ready = {'at': 0.0, 'body': None, 'code': 503}
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), APP.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.shutil.rmtree(cls.tmp, ignore_errors=True)

    def _req(self, method, path, body=None):
        import urllib.error
        import urllib.request
        r = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}', method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json',
                     'X-Forwarded-For': '203.0.113.9'})
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                return resp.status, json.loads(resp.read() or b'{}')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def test_healthz_stays_liveness_and_readyz_answers_separately(self):
        self.assertEqual(200, self._req('GET', '/healthz')[0])
        code, d = self._req('GET', '/readyz')
        self.assertEqual(200, code)
        self.assertEqual({}, d['checks'])
        self.assertTrue(d['runs_dir_writable'])

    def test_the_caps_are_readable_before_they_refuse_anything(self):
        code, d = self._req('GET', '/api/config')
        self.assertEqual(200, code)
        self.assertTrue(d['limits']['public_demo'])
        _, rt = self._req('GET', '/api/runtime')
        self.assertIn('limits', rt)
        self.assertIn('usage', rt)

    def test_an_unknown_run_id_is_a_404_that_says_so(self):
        code, d = self._req('GET', '/api/discovery/' + 'f' * 16)
        self.assertEqual(404, code)
        self.assertEqual('f' * 16, d['run_id'])
        self.assertEqual(404, self._req('POST', '/api/discovery/' + 'f' * 16 + '/cancel')[0])

    def test_a_finished_run_survives_being_dropped_from_memory(self):
        """What a browser reload does, and then what a redeploy does."""
        code, run = self._req('POST', '/api/discovery', {
            'project_id': 'ipsc_macrophage',
            'objective': 'Increase viable macrophage yield while keeping identity.',
            'runtime_mode': 'synthetic_demo'})
        self.assertEqual(202, code)
        rid = run['run_id']
        for _ in range(240):
            snap = self._req('GET', f'/api/discovery/{rid}')[1]
            if snap['status'] not in ('queued', 'running'):
                break
            time.sleep(0.25)
        self.assertEqual('done', snap['status'])
        self.assertFalse(snap['recovered'])
        # Now forget it, exactly as a restarted process would have.
        self.APP.Handler.discovery.runs.clear()
        code, back = self._req('GET', f'/api/discovery/{rid}')
        self.assertEqual(200, code)
        self.assertTrue(back['recovered'])
        self.assertEqual('done', back['status'])
        self.assertTrue(back['result']['protocol'])
        self.assertGreater(back['event_count'], 0)
        self.assertEqual(200, self._req('GET', f'/api/discovery/{rid}/protocol')[0])

    def test_a_budget_refusal_is_a_429_that_names_the_cap(self):
        """The HTTP mapping, with the ledger's refusal injected: a cap must reach
        the browser as a refusal with a retry time, never as a 500 and never as a
        synthetic run."""
        reg = self.APP.Handler.discovery
        real_start = reg.start

        def refuse(*a, **kw):
            raise BU.BudgetExceeded('per_client', 'You have had three for today.',
                                    retry_after_s=120)

        reg.start = refuse
        try:
            code, d = self._req('POST', '/api/discovery', {
                'project_id': 'ipsc_macrophage',
                'objective': 'Increase viable macrophage yield while keeping identity.',
                'runtime_mode': 'synthetic_demo'})
        finally:
            reg.start = real_start
        self.assertEqual(429, code)
        self.assertTrue(d['refused'])
        self.assertEqual('per_client', d['limit'])
        self.assertEqual(120, d['retry_after_s'])
        self.assertNotIn('run_id', d)


if __name__ == '__main__':
    unittest.main()
