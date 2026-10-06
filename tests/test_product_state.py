"""Durable projects, durable runs, and one authoritative object for each.

The product failure these guard against is not a crash. It is a scientist
starting a fifteen-minute AI investigation, opening another page, and finding
that the work is gone — or worse, that two pages disagree about whether it is
still running. So:

* a project is a file on the server, created once and surviving a refresh, a
  restart and a redeploy;
* a run is a background job on the server, which the browser observes and does
  not own; closing the browser, switching project or losing the stream changes
  what is visible and nothing about what is executing;
* there is exactly one run object, and every view renders it, so Discovery and
  Runs cannot disagree.

Nothing here calls a model or contacts Omnigent: the real-AI path is exercised
through its own seams.
"""
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from biosense import contracts as K
from biosense import workspace as WS
from biosense.production import activity as ACT
from biosense.production import authz as AZ
from biosense.production import budget as BU
from biosense.production import project_builder as PB
from biosense.production import run_store as RS
from biosense.production import runtime as RT

OBJECTIVE = 'Increase viable macrophage production while maintaining identity and viability.'


class ProjectCreationTests(unittest.TestCase):
    """A project is a durable scientific workspace, not a page's state."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.who = WS.WorkspaceIdentity(f'owner{id(self)}@lab.example', authenticated=True,
                                        server='https://id.example')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_few_fields_make_a_real_validated_project(self):
        doc = PB.quick_build(project_id='human_macrophage_opt',
                             name='Human Macrophage Optimization', species='Human',
                             starting_cell='iPSC', target_cell='Macrophage',
                             process_context='iPSC to macrophage',
                             goal='Improve viable macrophage output')
        K.require_valid('project_profile', doc)
        self.assertEqual('Human', doc['biological_system']['species'])
        self.assertEqual('Macrophage', doc['biological_system']['target_cell'])
        self.assertIn('Improve viable macrophage output', doc['description'])
        self.assertTrue(doc['stages'])
        self.assertTrue(doc['parameters'])

    def test_it_invents_no_biology_and_says_so(self):
        """The shared vessel can be defaulted because it means the same thing in
        every project. Biology cannot: no simulator, no modelled parameter, no
        cell-type-specific knob appears because nobody asked for one."""
        doc = PB.quick_build(project_id='some_process', name='Some Process')
        self.assertIsNone(doc['simulator']['model_id'])
        self.assertEqual('none', doc['simulator']['status'])
        from biosense import projects as PJ
        project = PJ.Project(doc)
        self.assertEqual([], sorted(project.modelled_ids()))
        self.assertTrue(any('lightweight project form' in l for l in doc['limitations']))

    def test_a_created_project_outlives_the_process_that_created_it(self):
        import os
        os.environ['BIOSENSE_PRIVATE_DATA'] = str(self.tmp)
        try:
            doc = PB.quick_build(project_id='durable_one', name='Durable One',
                                 species='Human', target_cell='Neutrophil')
            PB.save(self.who, doc)
            # Nothing is cached: a second reader, as a restarted process would be,
            # finds it on disk.
            again = PB.load_for(self.who, 'durable_one')
            self.assertEqual('Durable One', again.name)
            self.assertIn('durable_one',
                          [p.project_id for p in PB.workspace_projects(self.who)])
        finally:
            os.environ.pop('BIOSENSE_PRIVATE_DATA', None)

    def test_a_project_belongs_to_one_account(self):
        import os
        os.environ['BIOSENSE_PRIVATE_DATA'] = str(self.tmp)
        try:
            other = WS.WorkspaceIdentity('other@lab.example', authenticated=True,
                                         server='https://id.example')
            PB.save(self.who, PB.quick_build(project_id='mine_only', name='Mine Only'))
            self.assertEqual([], [p.project_id for p in PB.workspace_projects(other)])
            with self.assertRaises(K.ContractError):
                PB.load_for(other, 'mine_only')
        finally:
            os.environ.pop('BIOSENSE_PRIVATE_DATA', None)


class ActivityTests(unittest.TestCase):
    """What the live viewer is told, derived from typed events alone."""

    def events(self):
        return [
            {'seq': 0, 'at': 10, 'kind': 'accepted', 'stage': 'understanding_objective',
             'simple': 'Your question reached the discovery agents.'},
            {'seq': 1, 'at': 20, 'kind': 'tool', 'tool': 'sys_session_send',
             'technical': 'literature-it1: find claims on G-CSF and neutrophil maturation'},
            {'seq': 2, 'at': 90, 'kind': 'tool_result', 'tool': 'sys_session_send',
             'technical': 'literature returned 7 claims'},
            {'seq': 3, 'at': 95, 'kind': 'tool', 'tool': 'sys_session_send',
             'technical': 'bioinformatics-it1: evaluate GSE155719'},
            {'seq': 4, 'at': 150, 'kind': 'tool_result', 'tool': 'bash',
             'technical': 'analysis refused: metadata join unsupported for this dataset'},
            {'seq': 5, 'at': 200, 'kind': 'tool', 'tool': 'bash',
             'technical': 'wrote quantified_hypothesis.json'},
        ]

    def test_an_agent_is_running_because_a_dispatch_was_seen(self):
        a = ACT.Activity.from_events(self.events(), started_at=0)
        by = {r['agent']: r for r in a.snapshot(now=210)['agents']}
        self.assertEqual('complete', by['literature']['status'])
        self.assertEqual('running', by['bioinformatics']['status'])
        self.assertEqual('queued', by['biosimulator']['status'])
        self.assertIn('G-CSF', by['literature']['task'])

    def test_prose_about_an_agent_does_not_start_one(self):
        """A bash line containing the word "analysis" is prose. Showing work that
        is not happening is the failure this rule exists to prevent."""
        a = ACT.Activity.from_events(self.events(), started_at=0)
        by = {r['agent']: r for r in a.snapshot()['agents']}
        self.assertEqual('queued', by['analysis']['status'])

    def test_a_refusal_is_kept_where_a_scientist_can_see_it(self):
        a = ACT.Activity.from_events(self.events(), started_at=0)
        snap = a.snapshot()
        self.assertTrue(snap['limitations'])
        self.assertIn('metadata join', snap['limitations'][0]['text'])
        self.assertIn('limitation', [t['kind'] for t in snap['timeline']])

    def test_artifacts_appear_as_they_are_written(self):
        a = ACT.Activity.from_events(self.events(), started_at=0)
        self.assertIn('quantified_hypothesis.json',
                      [x['name'] for x in a.snapshot()['artifacts']])

    def test_last_activity_is_what_says_a_quiet_run_is_alive(self):
        a = ACT.Activity.from_events(self.events(), started_at=0)
        self.assertEqual(200, a.snapshot(now=200)['last_activity_at'])
        self.assertEqual(45.0, a.snapshot(now=245)['idle_s'])

    def test_reasoning_moves_the_clock_and_is_never_shown(self):
        a = ACT.Activity(started_at=0)
        a.observe({'kind': 'reasoning', 'at': 5, 'technical': 'the model thinking out loud'})
        snap = a.snapshot(now=5)
        self.assertEqual(5, snap['last_activity_at'])
        self.assertEqual([], snap['timeline'])

    def test_the_whole_picture_rebuilds_from_the_journal(self):
        """A restarted process must show what a live one shows, or a reader
        concludes a working run is dead."""
        live = ACT.Activity(started_at=0)
        for e in self.events():
            live.observe(e)
        rebuilt = ACT.Activity.from_events(self.events(), started_at=0)
        self.assertEqual(live.snapshot(now=300)['agents'], rebuilt.snapshot(now=300)['agents'])
        self.assertEqual(live.snapshot(now=300)['limitations'],
                         rebuilt.snapshot(now=300)['limitations'])

    def test_progress_is_counted_and_never_computed_as_a_fraction(self):
        """The activity bar is indeterminate on purpose: the work left is not
        proportional to the stages left — a run can spend eleven minutes inside
        one analysis — so a bar implying otherwise lies once a second."""
        from biosense.production import stages as ST
        counts = ST.stage_counts({'understanding_objective'})
        self.assertEqual({'done', 'total', 'current'}, set(counts))
        self.assertIsInstance(counts['done'], int)
        snap = ACT.Activity.from_events(self.events(), started_at=0).snapshot()
        for key in snap:
            self.assertNotIn('percent', key)
            self.assertNotIn('fraction', key)
        css = (K.ROOT / 'webapp' / 'brand.css').read_text()
        bar = css[css.index('.rv-bar{'):css.index('.rv-stats')]
        self.assertIn('animation', bar, 'the bar must be indeterminate')
        self.assertNotIn('width:var(', bar, 'nothing may drive the bar from data')


class CanonicalDirectoryTests(unittest.TestCase):
    """One directory per run, named the way the runner will resolve it."""

    def test_agents_are_given_the_run_directory_relative_to_their_workspace(self):
        """It used to be a bare directory name, so the agents wrote under the
        workspace root while BioSense read under the runs directory, and a real
        run left artifacts in two places with the person seeing neither."""
        from biosense.production import discovery_runner as DR
        cfg = RT.from_env(runs_dir=K.ROOT / 'runs',
                          env={'BIOSENSE_RUNTIME_MODE': 'local',
                               'BIOSENSE_OMNIGENT_WORKSPACE': str(K.ROOT)})
        self.assertEqual('runs/ai-x', DR.run_dir_for_agents(cfg, K.ROOT / 'runs' / 'ai-x'))

    def test_the_brief_names_that_one_directory_everywhere(self):
        from biosense.production import discovery as DISC
        req = DISC.build(project_id='ipsc_macrophage', objective=OBJECTIVE,
                         runtime_mode='local_real_ai')
        brief = DISC.render_brief(req, loop_dir='runs/ai-20260101-abc')
        self.assertIn('runs/ai-20260101-abc/', brief)
        for name, _kind, _why in DISC.ARTIFACTS:
            self.assertIn(f'runs/ai-20260101-abc/{name}', brief)


class RunStoreTests(unittest.TestCase):
    """The durable half of the run list."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rid, **over):
        state = {'run_id': rid, 'owner': 'me@lab.example', 'project_id': 'proj_a',
                 'objective': 'x', 'status': 'done', 'started_at': 100.0,
                 'runtime_mode': 'synthetic_demo', 'runtime_label': 'SYNTHETIC DEMO'}
        state.update(over)
        RS.write_state(self.tmp / f'run-{rid}', state)

    def test_runs_are_grouped_the_way_the_runs_page_shows_them(self):
        for status, group in (('running', 'active'), ('finalizing', 'active'),
                              ('done', 'complete'), ('stopped', 'cancelled'),
                              ('error', 'failed'), ('interrupted', 'failed')):
            self.assertEqual(group, RS.group_for(status), status)

    def test_a_listing_is_scoped_to_its_owner_and_its_project(self):
        self._write('a' * 16)
        self._write('b' * 16, owner='other@lab.example')
        self._write('c' * 16, project_id='proj_b')
        mine = RS.listing(self.tmp, owner='me@lab.example')
        self.assertEqual({'a' * 16, 'c' * 16}, {r['run_id'] for r in mine})
        one = RS.listing(self.tmp, owner='me@lab.example', project_id='proj_a')
        self.assertEqual({'a' * 16}, {r['run_id'] for r in one})

    def test_a_run_nothing_is_running_is_interrupted_not_running(self):
        self._write('d' * 16, status='running')
        row = RS.listing(self.tmp, owner='me@lab.example')[0]
        self.assertEqual('interrupted', row['status'])
        self.assertEqual('failed', row['group'])
        row = RS.listing(self.tmp, owner='me@lab.example', live_ids={'d' * 16})[0]
        self.assertEqual('running', row['status'])
        self.assertEqual('active', row['group'])


class ApiTests(unittest.TestCase):
    """The product behaviour, against a live server on a loopback port."""

    @classmethod
    def setUpClass(cls):
        import os
        from biosense.production import app as APP
        cls.APP = APP
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / 'runs').mkdir()
        cls._private = os.environ.get('BIOSENSE_PRIVATE_DATA')
        os.environ['BIOSENSE_PRIVATE_DATA'] = str(cls.tmp / 'private')
        cfg = RT.from_env(runs_dir=cls.tmp / 'runs', env={})
        APP.Handler.runs_dir = cls.tmp / 'runs'
        APP.Handler.static_dir = K.ROOT / 'webapp'
        APP.Handler.registry = APP.Registry(cls.tmp / 'runs')
        APP.Handler.runtime_cfg = cfg
        APP.Handler.limits = BU.Limits()
        APP.Handler.policy = AZ.Policy()
        APP.Handler.discovery = APP.DiscoveryRegistry(cls.tmp / 'runs', cfg)
        APP.Handler.sessions = WS.SessionStore()
        APP.Handler.default_identity = WS.local_identity()
        APP.Handler._ready = {'at': 0.0, 'body': None, 'code': 503}
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), APP.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        import os
        cls.srv.shutdown()
        cls.srv.server_close()
        if cls._private is None:
            os.environ.pop('BIOSENSE_PRIVATE_DATA', None)
        else:
            os.environ['BIOSENSE_PRIVATE_DATA'] = cls._private
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _req(self, method, path, body=None):
        r = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}', method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                return resp.status, json.loads(resp.read() or b'{}')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def _project(self, name):
        code, d = self._req('POST', '/api/projects',
                            {'name': name, 'species': 'Human', 'target_cell': 'Macrophage',
                             'goal': 'more cells', 'quick': True, 'overwrite': True})
        self.assertEqual(201, code, d)
        return d['project_id']

    def _run(self, project_id, objective=OBJECTIVE):
        code, d = self._req('POST', '/api/discovery', {
            'project_id': project_id, 'objective': objective,
            'runtime_mode': 'synthetic_demo'})
        self.assertEqual(202, code, d)
        return d['run_id']

    def _settle(self, rid, timeout=90):
        for _ in range(int(timeout * 4)):
            snap = self._req('GET', f'/api/discovery/{rid}')[1]
            if snap['status'] not in ('queued', 'running', 'finalizing'):
                return snap
            time.sleep(0.25)
        self.fail('the run never settled')

    # ── creating and selecting ──────────────────────────────
    def test_creating_a_project_returns_the_one_to_select(self):
        code, d = self._req('POST', '/api/projects', {
            'name': 'Human Macrophage Optimization', 'species': 'Human',
            'starting_cell': 'iPSC', 'target_cell': 'Macrophage',
            'process_context': 'iPSC to macrophage',
            'goal': 'Improve viable macrophage output', 'quick': True, 'overwrite': True})
        self.assertEqual(201, code, d)
        self.assertEqual('human_macrophage_optimization', d['selected'])
        listed = self._req('GET', '/api/workspace/projects')[1]['projects']
        self.assertIn('human_macrophage_optimization', [p['project_id'] for p in listed])

    def test_a_created_project_can_actually_be_run_against(self):
        """The gap this closes: a project could be created and selected, and then
        every run against it refused with "no such project" — because the run
        looked only in the committed set."""
        pid = self._project('Runnable Project')
        snap = self._settle(self._run(pid))
        self.assertEqual('done', snap['status'], snap.get('error'))
        self.assertEqual(pid, snap['project_id'])

    def test_a_project_a_request_names_but_nobody_owns_is_refused(self):
        code, d = self._req('POST', '/api/discovery/preview',
                            {'project_id': 'no_such_project_here', 'objective': OBJECTIVE})
        self.assertEqual(400, code)
        self.assertTrue(d['refused'])

    # ── one object, every view ──────────────────────────────
    def test_the_run_appears_in_the_list_immediately(self):
        pid = self._project('Instant Listing')
        rid = self._run(pid)
        listed = self._req('GET', '/api/discovery')[1]
        self.assertIn(rid, [r['run_id'] for r in listed['runs']])

    def test_discovery_and_runs_read_the_same_object(self):
        pid = self._project('Same Object')
        rid = self._run(pid)
        self._settle(rid)
        detail = self._req('GET', f'/api/discovery/{rid}')[1]
        row = next(r for r in self._req('GET', '/api/discovery')[1]['runs']
                   if r['run_id'] == rid)
        self.assertEqual(detail['status'], row['status'])
        self.assertEqual(detail['project_id'], row['project_id'])
        self.assertEqual(detail['runtime_mode'], row['runtime_mode'])
        self.assertEqual(detail['objective'], row['objective'])

    def test_a_run_is_filterable_by_project_and_counted_per_project(self):
        a, b = self._project('Project Alpha'), self._project('Project Beta')
        ra, rb = self._run(a), self._run(b)
        self._settle(ra), self._settle(rb)
        only_a = self._req('GET', f'/api/discovery?project_id={a}')[1]
        self.assertEqual([ra], [r['run_id'] for r in only_a['runs']])
        projects = self._req('GET', '/api/workspace/projects')[1]['projects']
        counts = {p['project_id']: p['runs'] for p in projects if 'runs' in p}
        self.assertEqual(1, counts[a]['total'])
        self.assertEqual(1, counts[b]['total'])

    def test_the_project_detail_view_holds_the_projects_work(self):
        pid = self._project('Detail View')
        rid = self._run(pid)
        self._settle(rid)
        code, d = self._req('GET', f'/api/projects/{pid}')
        self.assertEqual(200, code)
        self.assertEqual(pid, d['project']['project_id'])
        self.assertTrue(d['owned'])
        self.assertIn(rid, [r['run_id'] for r in d['runs']])

    # ── the run outlives the page ───────────────────────────
    def test_switching_project_and_navigating_does_not_touch_a_run(self):
        """The regression test the product asked for by name: selecting another
        project is a view change and must have zero execution impact."""
        pid = self._project('Keeps Running')
        rid = self._run(pid)
        self._project('Another Project')             # created while it runs
        self._req('GET', '/api/discovery')           # the Runs page
        self._req('GET', '/api/datasets')            # the Data page
        self._req('GET', '/api/sim/config')          # the Simulator page
        snap = self._settle(rid)
        self.assertEqual('done', snap['status'])
        self.assertIsNone(snap['error'])

    def test_only_an_explicit_stop_cancels_a_run(self):
        from biosense.production import app as APP
        run = APP.DiscoveryRun('f' * 16, {'request_id': 'r', 'project_id': 'p',
                                          'objective': 'o'},
                               self.tmp / 'runs' / 'nowhere', 'synthetic_demo')
        self.assertFalse(run.should_stop())
        run.stop('cancelled', APP.STOP_MESSAGES['cancelled'])
        self.assertTrue(run.should_stop())

    def test_a_finished_run_survives_this_process_forgetting_it(self):
        pid = self._project('Survives Restart')
        rid = self._run(pid)
        self._settle(rid)
        self.APP.Handler.discovery.runs.clear()      # what a restart looks like
        code, back = self._req('GET', f'/api/discovery/{rid}')
        self.assertEqual(200, code)
        self.assertTrue(back['recovered'])
        self.assertEqual('done', back['status'])
        listed = self._req('GET', '/api/discovery')[1]['runs']
        self.assertIn(rid, [r['run_id'] for r in listed])

    def test_events_can_be_replayed_from_where_a_client_left_off(self):
        pid = self._project('Replay')
        rid = self._run(pid)
        snap = self._settle(rid)
        self.assertGreater(snap['event_count'], 2)
        half = snap['events'][1]['seq']
        after = self._req('GET', f'/api/discovery/{rid}?after={half}')[1]
        self.assertTrue(all(e['seq'] > half for e in after['events']))
        self.assertLess(len(after['events']), len(snap['events']))

    def test_a_run_carries_its_live_picture_and_its_stage_count(self):
        pid = self._project('Live Picture')
        snap = self._settle(self._run(pid))
        self.assertIn('activity', snap)
        self.assertIn('agents', snap['activity'])
        self.assertTrue(snap['activity']['timeline'])
        self.assertEqual({'done', 'total', 'current'}, set(snap['stage_counts']))
        self.assertLessEqual(snap['stage_counts']['done'], snap['stage_counts']['total'])

    def test_a_run_records_the_project_and_the_account_it_belongs_to(self):
        pid = self._project('Belongs To')
        snap = self._settle(self._run(pid))
        self.assertEqual(pid, snap['project_id'])
        self.assertEqual(WS.LOCAL_OWNER, snap['owner'])
        state = json.loads((self.tmp / 'runs' / snap['run_dir'] / 'app_run.json').read_text())
        self.assertEqual(pid, state['project_id'])
        self.assertEqual(WS.LOCAL_OWNER, state['owner'])


if __name__ == '__main__':
    unittest.main()
