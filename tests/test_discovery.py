"""The discovery workflow end to end: request, artifacts, protocol, benchmark, API.

Everything here runs the deterministic path, so none of it needs a model, a
server or a credential. What it checks is the properties that would mislead a
scientist if they broke: that a card is built from a validated artifact and not
from prose, that a parameter the project cannot model is labelled rather than
dropped, that a discarded hypothesis stays visible, that a protocol is never
presented as approved, and that a private lineage cannot become a public
benchmark.
"""
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from biosense import contracts as K
from biosense import projects as PJ
from biosense import workspace as WS
from biosense.benchmark import from_run as FR
from biosense.production import discovery as DISC
from biosense.production import discovery_runner as DR
from biosense.production import ingest as IN
from biosense.production import project_builder as PB
from biosense.production import protocol_summary as PS
from biosense.production import response_model as RM
from biosense.production import runtime as RT
from biosense.production import sim_candidate as SC

OBJECTIVE = 'Increase viable macrophage production while maintaining identity and viability.'


def a_request(**kw):
    kw.setdefault('project_id', 'ipsc_macrophage')
    kw.setdefault('objective', OBJECTIVE)
    kw.setdefault('runtime_mode', 'synthetic_demo')
    return DISC.build(**kw)


class RequestTests(unittest.TestCase):
    def test_a_request_is_structure_not_a_blob(self):
        req = a_request(
            research_context={'schema_version': '2.0', 'strictness': 'strict',
                              'species': ['human'], 'cell_types': ['macrophage'],
                              'tissues': [], 'states': [], 'disease_context': [],
                              'modalities': [], 'assays': [], 'therapy_context': [],
                              'exclude': [], 'context_id': None, 'created_at': K.now_iso(),
                              'publication_from_year': None, 'publication_to_year': None,
                              'time_range': None, 'notes': None, 'applied': None},
            dataset_ids=['facs-mcsf-fixture'], candidate_values={'mcsf_ng_ml': 50})
        self.assertEqual([], K.schema_errors('discovery_request', req))
        # the strictness survives rather than being flattened into prose
        self.assertEqual('strict', req['research_context']['strictness'])
        self.assertEqual({'mcsf_ng_ml': 50}, req['candidate_values'])

    def test_a_parameter_the_project_does_not_have_is_refused_not_dropped(self):
        with self.assertRaises(K.ContractError) as e:
            a_request(candidate_values={'il7_ng_ml': 10})
        self.assertIn('does not expose', str(e.exception))

    def test_an_alias_resolves_to_the_canonical_name(self):
        req = a_request(candidate_values={'mcsf': 50})
        self.assertEqual({'mcsf_ng_ml': 50}, req['candidate_values'])

    def test_a_constraint_must_say_where_it_came_from(self):
        with self.assertRaises(K.ContractError) as e:
            a_request(process_constraints={'parameter_bounds': [
                {'parameter_id': 'mcsf_ng_ml', 'minimum': 20, 'maximum': 80}]})
        self.assertIn('wearing the clothes of a constraint', str(e.exception))

    def test_an_uncertainty_has_to_say_something(self):
        with self.assertRaises(K.ContractError):
            a_request(uncertainty={'kind': 'evidence_gap', 'ref': 'G1', 'statement': 'dunno'})

    def test_a_private_dataset_makes_the_run_private(self):
        req = a_request(expert_knowledge_ids=['EK-1'])
        p = DISC.privacy(req)
        self.assertTrue(p['has_private_lineage'])
        self.assertEqual('private', p['export_policy'])
        self.assertIn('expert_knowledge:EK-1', p['private_sources'])

    def test_the_brief_carries_the_request_as_data(self):
        req = a_request(candidate_values={'mcsf_ng_ml': 50})
        brief = DISC.render_brief(req, loop_dir='ai-x')
        self.assertIn(OBJECTIVE, brief)
        self.assertIn('ai-x/', brief)
        self.assertIn('"runtime_mode": "synthetic_demo"', brief)
        # the embedded document is parseable: nothing was lost in paraphrase
        blob = brief.split('```json', 1)[1].split('```', 1)[0]
        self.assertEqual(req['request_id'], json.loads(blob)['request_id'])

    def test_a_benchmark_config_converts_rather_than_being_retyped(self):
        cfg = K.read_json(K.ROOT / 'benchmarks' / 'configs' / 'macrophage_mcsf_demo.json')
        req = DISC.from_benchmark_config(cfg, runtime_mode='synthetic_demo')
        self.assertEqual(cfg['objective'], req['objective'])
        self.assertEqual(cfg['datasets'], req['dataset_ids'])
        self.assertEqual('prefer', req['research_context']['strictness'])


class RunTests(unittest.TestCase):
    """One deterministic run, reused by everything below."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.req = a_request(dataset_ids=['facs-mcsf-fixture'],
                            candidate_values={'mcsf_ng_ml': 50, 'temperature_c': 38})
        cls.events = []
        cls.out = cls.tmp / 'demo-run'
        cls.res = DR.run_synthetic(cls.req, cls.out, on_event=cls.events.append)
        cls.final = DR.finish(cls.req, cls.out, runtime_mode='synthetic_demo',
                              benchmark=cls.res['benchmark'])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_run_reaches_every_stage_it_claims(self):
        reached = {e['stage'] for e in self.events if e.get('stage')}
        for stage in ('understanding_objective', 'identifying_uncertainty', 'running_analysis',
                      'building_hypothesis', 'testing_simulator', 'generating_report',
                      'complete'):
            self.assertIn(stage, reached, stage)

    def test_the_artifacts_are_on_disk_and_validate(self):
        bundle = self.final['bundle']
        self.assertGreater(bundle['artifacts_ingested'], 3)
        self.assertEqual([], bundle['artifacts_rejected'])
        self.assertTrue(bundle['analyses'])
        self.assertTrue(bundle['hypotheses'])

    def test_a_file_that_fails_its_schema_is_reported_not_rendered(self):
        broken = self.out / 'analysis_result-broken.json'
        K.write_json_atomic(broken, {'analysis_id': 'nope', 'not': 'an analysis'})
        try:
            bundle = IN.run_bundle(self.out, project_id='ipsc_macrophage')
            self.assertTrue(any('broken' in r['file'] for r in bundle['artifacts_rejected']))
            self.assertFalse(any(a['analysis_id'] == 'nope' for a in bundle['analyses']))
        finally:
            broken.unlink()

    def test_the_analysis_card_keeps_the_statistics_that_make_it_checkable(self):
        a = self.final['bundle']['analyses'][0]
        self.assertTrue(a['method']['tool'])
        self.assertTrue(a['comparison']['control'])
        self.assertTrue(a['technical']['statistics'])
        self.assertTrue(a['technical']['provenance']['code_sha256'])
        # the finding carries its own basis rather than a bare number
        self.assertTrue(a['findings'][0]['basis'])

    def test_lineage_is_three_facts_never_merged(self):
        a = self.final['bundle']['analyses'][0]
        self.assertEqual('derived_analysis', a['evidence_class'])
        self.assertEqual('synthetic_fixture', a['source_evidence_class'])
        self.assertEqual('public', a['visibility'])

    def test_an_unmodelled_candidate_is_labelled_rather_than_dropped(self):
        """temperature_c is a real variable in this project with no model term."""
        rows = {c['parameter_id']: c for c in self.final['bundle']['candidate_parameters']}
        self.assertIn('mcsf_ng_ml', rows)
        self.assertTrue(rows['mcsf_ng_ml']['modelled'])
        hand = self.res['benchmark']['simulator']['handoff']
        skipped = {r['parameter_id'] for r in hand['skipped']}
        self.assertIn('temperature_c', skipped)
        self.assertTrue(next(r for r in hand['skipped']
                             if r['parameter_id'] == 'temperature_c')['reason'])

    def test_every_simulated_effect_says_it_is_simulated(self):
        for e in self.res['benchmark']['simulator']['effects']:
            self.assertEqual('simulated', e['estimate_type'])
            self.assertTrue(any('not a validated digital twin' in x for x in e['limitations']))


class ProtocolTests(RunTests):
    def test_the_protocol_is_a_proposal_and_says_who_would_approve_it(self):
        p = self.final['protocol']
        self.assertEqual('proposed_not_approved', p['status'])
        self.assertIsNone(p['approval']['approved_by'])
        self.assertIn('approve-protocol', p['approval']['how'])
        self.assertIs(True, p['approval']['required'])

    def test_the_adopted_change_appears_with_its_provenance(self):
        changed = self.final['protocol']['changed_parameters']
        self.assertTrue(changed)
        mcsf = next(c for c in changed if c['parameter_id'] == 'mcsf_ng_ml')
        self.assertEqual(25.0, mcsf['control_value'])
        self.assertEqual(50, mcsf['recommended_value'])
        self.assertIn(mcsf['provenance'], ('reported', 'adapted', 'design_choice'))
        self.assertEqual('modelled', mcsf['simulator_coverage'])

    def test_a_discarded_hypothesis_stays_on_the_page(self):
        """Showing only the winner hides that alternatives were ruled out."""
        project = PJ.load('ipsc_macrophage')
        hyps = [
            {'hypothesis_id': 'H1', 'statement': 'Raise M-CSF', 'status': 'supported',
             'confidence': 'high', 'parameter': {'parameter_id': 'mcsf_ng_ml',
                                                 'direction': 'increase', 'current_value': 25,
                                                 'candidate_value': 50,
                                                 'simulator_coverage': 'modelled'},
             'expected_effects': [], 'evidence': [], 'next_experiment': {'summary': 'test it'},
             'may_change_protocol': False, 'limitations': [], 'uncertainty_ref': 'G',
             'created_at': K.now_iso(), 'schema_version': '2.0', 'project_id':
                 'ipsc_macrophage'},
            {'hypothesis_id': 'H2', 'statement': 'Raise agitation instead',
             'status': 'contradicted', 'confidence': 'low',
             'superseded_reason': 'the viability readout moved the wrong way',
             'parameter': {'parameter_id': 'agitation_rpm', 'direction': 'increase',
                           'current_value': 60, 'candidate_value': 110,
                           'simulator_coverage': 'modelled'},
             'expected_effects': [], 'evidence': [], 'next_experiment': {'summary': 'no'},
             'may_change_protocol': False, 'limitations': [], 'uncertainty_ref': 'G',
             'created_at': K.now_iso(), 'schema_version': '2.0', 'project_id':
                 'ipsc_macrophage'},
        ]
        doc = PS.build(project=project, objective=OBJECTIVE, hypotheses=hyps,
                       runtime_mode='synthetic_demo')
        ledger = {r['hypothesis_id']: r for r in doc['hypothesis_ledger']}
        self.assertEqual(2, len(ledger))
        self.assertTrue(ledger['H1']['adopted'])
        self.assertFalse(ledger['H2']['adopted'])
        self.assertIn('contradicts', ledger['H2']['reason'])
        # and the contradicted parameter is NOT in the protocol
        changed = {p['parameter_id'] for st in doc['stages'] for p in st['parameters']
                   if p['changed']}
        self.assertIn('mcsf_ng_ml', changed)
        self.assertNotIn('agitation_rpm', changed)

    def test_a_gap_blocks_the_wet_lab_and_is_never_filled(self):
        project = PJ.load('cart_expansion')
        doc = PS.build(project=project, objective='x' * 20, hypotheses=[],
                       runtime_mode='synthetic_demo')
        gaps = {g['parameter_id'] for g in doc['gaps']}
        if gaps:
            self.assertTrue(doc['blocks_wet_lab'])
            for st in doc['stages']:
                for p in st['parameters']:
                    if p['parameter_id'] in gaps:
                        self.assertIsNone(p['recommended_value'])
                        self.assertEqual('gap', p['provenance'])

    def test_the_markdown_export_carries_the_warning_and_the_ledger(self):
        md = PS.markdown({k: v for k, v in self.final['protocol'].items()
                          if k not in ('summary_counts', 'changed_parameters', 'badge')})
        self.assertIn('PROPOSED — NOT APPROVED', md)
        self.assertIn('Hypotheses this run formed', md)
        self.assertIn('approve-protocol', md)


class BenchmarkTests(RunTests):
    def test_a_benchmark_is_built_from_the_run_not_by_rerunning_it(self):
        b = FR.build(request=self.req, bundle=self.final['bundle'], out_dir_name='demo-run',
                     runtime_mode='synthetic_demo', benchmark=self.res['benchmark'])
        self.assertEqual([], K.schema_errors('benchmark_result', b))
        self.assertEqual('demo-run', b['run_id'])
        self.assertEqual('offline', b['mode'])

    def test_the_scorecard_is_derived_from_what_is_actually_there(self):
        b = FR.build(request=self.req, bundle=self.final['bundle'], out_dir_name='demo-run',
                     runtime_mode='local_real_ai',
                     session={'omnigent_session_id': 'conv_1', 'harness': 'claude-sdk'})
        self.assertEqual('ai', b['mode'])
        self.assertEqual('conv_1', b['provenance']['ai']['omnigent_session_id'])
        rows = {r['capability']: r for r in b['scorecard']['rows']}
        self.assertEqual('PASS', rows['deterministic_analysis_executed']['status'])

    def test_an_empty_run_fails_the_scorecard_rather_than_passing_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            bundle = IN.run_bundle(d, project_id='ipsc_macrophage')
            b = FR.build(request=self.req, bundle=bundle, out_dir_name='empty',
                         runtime_mode='local_real_ai')
            rows = {r['capability']: r for r in b['scorecard']['rows']}
            self.assertEqual('FAIL', rows['hypothesis_generated']['status'])
            self.assertEqual('FAIL', rows['deterministic_analysis_executed']['status'])
            self.assertGreater(b['scorecard']['failed'], 8)

    def test_a_public_benchmark_is_badged_and_a_private_lineage_refuses(self):
        b = FR.build(request=self.req, bundle=self.final['bundle'], out_dir_name='demo-run',
                     runtime_mode='synthetic_demo', benchmark=self.res['benchmark'])
        self.assertEqual('PUBLIC-SAFE', FR.display(b)['privacy']['badge'])
        private_req = a_request(dataset_ids=['facs-mcsf-fixture'],
                                expert_knowledge_ids=['EK-private'])
        pb = FR.build(request=private_req, bundle=self.final['bundle'],
                      out_dir_name='demo-run', runtime_mode='synthetic_demo',
                      benchmark=self.res['benchmark'])
        self.assertEqual('PRIVATE — DO NOT PUBLISH', FR.display(pb)['privacy']['badge'])
        self.assertFalse(pb['privacy']['safe_to_publish'])

    def test_the_scorecard_says_what_it_does_not_measure(self):
        b = FR.build(request=self.req, bundle=self.final['bundle'], out_dir_name='demo-run',
                     runtime_mode='synthetic_demo', benchmark=self.res['benchmark'])
        d = FR.display(b)
        self.assertIn('NOT BIOLOGICAL TRUTH', d['scorecard']['headline'])


class ProjectBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = WS.DR.private_root
        WS.DR.private_root = lambda: self.tmp
        self.addCleanup(setattr, WS.DR, 'private_root', self._old)
        self.identity = WS.WorkspaceIdentity('scientist@example.org', authenticated=True,
                                             server='https://omni.example')

    def test_the_universal_set_is_the_vessel_everyone_shares(self):
        cat = PB.catalogue()
        ids = {r['parameter_id'] for r in cat['universal']}
        for knob in ('agitation_rpm', 'do_setpoint', 'feed_fraction', 'seed_density'):
            self.assertIn(knob, ids)
        self.assertNotIn('mcsf_ng_ml', ids, 'a cytokine is not a universal vessel control')

    def test_a_new_project_cannot_claim_a_model_term_it_does_not_have(self):
        with self.assertRaises(K.ContractError) as e:
            PB.build(project_id='x_proj', name='X project',
                     biological_system={'species': 'human', 'starting_cell': 'iPSC',
                                        'target_cell': 'T cell'},
                     stages=[{'stage_id': 'expansion', 'label': 'Expansion'}],
                     parameters=[{'parameter_id': 'il7_ng_ml', 'stage': 'expansion',
                                  'simulator_coverage': 'modelled'}])
        self.assertIn('cannot be marked "modelled"', str(e.exception))

    def test_a_proposed_response_must_say_what_it_computes(self):
        with self.assertRaises(K.ContractError) as e:
            PB.build(project_id='x_proj', name='X project',
                     biological_system={'species': 'human', 'starting_cell': 'iPSC',
                                        'target_cell': 'T cell'},
                     stages=[{'stage_id': 'expansion', 'label': 'Expansion'}],
                     parameters=[{'parameter_id': 'il7_ng_ml', 'stage': 'expansion',
                                  'simulator_coverage': 'de_novo_ai'}])
        self.assertIn('has to say what it is', str(e.exception))

    def _tcell(self, coverage='de_novo_ai'):
        return PB.build(
            project_id='tcell_demo', name='iPSC to T cell (demo)',
            biological_system={'species': 'human', 'starting_cell': 'iPSC',
                               'target_cell': 'T cell'},
            stages=[{'stage_id': 'expansion', 'label': 'Expansion', 'default_days': 5}],
            parameters=[
                {'parameter_id': 'agitation_rpm', 'stage': 'all'},
                {'parameter_id': 'il7_ng_ml', 'stage': 'expansion',
                 'simulator_coverage': coverage, 'default_value': 10,
                 'response_model': {'shape': 'bell', 'target': 'transition_efficiency',
                                    'optimum': 10, 'tolerance': 4, 'max_effect': 0.25,
                                    'basis': 'Cited claims put IL-7 support at 5-20 ng/mL.'}}])

    def test_a_proposed_term_predicts_and_says_it_was_never_fitted(self):
        doc = self._tcell()
        project = PJ.Project(doc)
        q = project.parameter('il7_ng_ml')
        self.assertTrue(q.predicts)
        self.assertTrue(q.uncalibrated)
        d = RM.describe(q.response_model)
        self.assertEqual('DE NOVO · UNCALIBRATED', d['badge'])
        self.assertIn('fitted to no data', d['evidence_status'])

    def test_an_expert_declared_term_is_private_and_a_design_choice(self):
        doc = self._tcell(coverage='expert_declared')
        d = RM.describe(PJ.Project(doc).parameter('il7_ng_ml').response_model)
        self.assertEqual('EXPERT-DECLARED', d['badge'])
        self.assertIn('design choice', d['evidence_status'])

    def test_a_project_with_no_calibrated_model_predicts_only_relatively(self):
        project = PJ.Project(self._tcell())
        cmp_ = SC.compare_conditions(project, candidate_values={'il7_ng_ml': 14})
        self.assertEqual('relative_only', cmp_['prediction'])
        row = cmp_['relative_effects'][0]
        self.assertIs(False, row['absolute_value_available'])
        self.assertIn('no calibrated model', row['why_no_absolute'])
        self.assertEqual('predicted', row['estimate_type'])

    def test_a_proposed_term_cannot_overwhelm_the_calibrated_biology(self):
        rm = {'shape': 'linear', 'slope': 100.0, 'target': 'growth', 'max_effect': 4.0,
              'origin': 'ai_proposed', 'calibrated': False, 'basis': 'an enthusiastic claim'}
        m = RM.multiplier(rm, 1, 1000)
        self.assertLessEqual(m, RM.MULTIPLIER_CEILING)
        self.assertGreaterEqual(m, RM.MULTIPLIER_FLOOR)

    def test_a_project_is_saved_to_its_owner_and_never_to_the_repository(self):
        doc = self._tcell()
        path = PB.save(self.identity, doc)
        self.assertTrue(str(path).startswith(str(self.tmp)))
        self.assertFalse((K.ROOT / 'projects' / 'tcell_demo.json').exists())
        owned = {r['project_id']: r for r in PB.available_for(self.identity)}
        self.assertEqual('workspace', owned['tcell_demo']['source'])
        self.assertEqual('template', owned['ipsc_macrophage']['source'])

    def test_one_workspace_cannot_see_another(self):
        PB.save(self.identity, self._tcell())
        other = WS.WorkspaceIdentity('someone.else@example.org', authenticated=True,
                                     server='https://omni.example')
        ids = {r['project_id'] for r in PB.available_for(other)}
        self.assertNotIn('tcell_demo', ids)

    def test_a_workspace_cannot_read_outside_itself(self):
        with self.assertRaises(K.ContractError):
            WS.assert_owned(self.identity, '/etc/passwd')


class ApiTests(unittest.TestCase):
    """The HTTP surface, against a live server on a loopback port."""

    @classmethod
    def setUpClass(cls):
        from biosense.production import app as APP
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / 'runs').mkdir()
        cfg = RT.from_env(runs_dir=cls.tmp / 'runs', env={})
        APP.Handler.runs_dir = cls.tmp / 'runs'
        APP.Handler.static_dir = K.ROOT / 'webapp'
        APP.Handler.registry = APP.Registry(cls.tmp / 'runs')
        APP.Handler.runtime_cfg = cfg
        APP.Handler.discovery = APP.DiscoveryRegistry(cls.tmp / 'runs', cfg)
        APP.Handler.sessions = WS.SessionStore()
        APP.Handler.default_identity = WS.local_identity()
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), APP.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
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

    def test_the_runtime_endpoint_says_what_is_offered_and_hides_the_token(self):
        code, d = self._req('GET', '/api/runtime')
        self.assertEqual(200, code)
        self.assertEqual(['synthetic_demo'], d['allowed_modes'])
        self.assertEqual(3, len(d['modes']))
        self.assertNotIn('token', json.dumps(d).lower().replace('token_configured', ''))
        self.assertIn('never substitutes', d['note'])

    def test_the_glossary_is_served_so_a_badge_can_explain_itself(self):
        code, d = self._req('GET', '/api/glossary')
        self.assertEqual(200, code)
        self.assertIn('estimate_type', d['groups'])
        self.assertIn('DERIVED', d['groups']['estimate_type']['terms']['derived']['label'])

    def test_a_real_run_on_a_synthetic_deployment_is_refused_with_its_reason(self):
        """And emphatically not answered with a synthetic run."""
        code, d = self._req('POST', '/api/discovery', {
            'project_id': 'ipsc_macrophage', 'objective': OBJECTIVE,
            'runtime_mode': 'local_real_ai'})
        self.assertEqual(503, code)
        self.assertTrue(d['refused'])
        self.assertEqual('not_configured', d['reason'])
        self.assertTrue(d['next_step'])
        self.assertNotIn('run_id', d)

    def test_a_discovery_run_streams_and_produces_a_protocol(self):
        import time
        code, run = self._req('POST', '/api/discovery', {
            'project_id': 'ipsc_macrophage', 'objective': OBJECTIVE,
            'dataset_ids': ['facs-mcsf-fixture'], 'candidate_values': {'mcsf_ng_ml': 50},
            'runtime_mode': 'synthetic_demo'})
        self.assertEqual(202, code)
        self.assertEqual('SYNTHETIC DEMO', run['runtime_label'])
        self.assertIs(False, run['is_real'])
        for _ in range(240):
            code, snap = self._req('GET', f'/api/discovery/{run["run_id"]}')
            if snap['status'] not in ('running', 'queued'):
                break
            time.sleep(0.25)
        self.assertEqual('done', snap['status'])
        self.assertTrue(snap['result']['protocol'])
        self.assertEqual('proposed_not_approved', snap['result']['protocol']['status'])
        done = {p['stage'] for p in snap['progress'] if p['status'] == 'done'}
        self.assertIn('running_analysis', done)
        # the benchmark is built from what that run already produced
        code, b = self._req('POST', f'/api/discovery/{run["run_id"]}/benchmark')
        self.assertEqual(201, code)
        self.assertIn('NOT BIOLOGICAL TRUTH', b['benchmark']['scorecard']['headline'])

    def test_benchmarks_are_readable_and_runnable_without_a_clone(self):
        code, d = self._req('GET', '/api/benchmarks')
        self.assertEqual(200, code)
        self.assertTrue(d['built'], 'no built benchmark is exposed')
        self.assertTrue(d['configs'], 'no benchmark can be re-run from the browser')
        bid = d['built'][0]['benchmark_id']
        code, one = self._req('GET', f'/api/benchmarks/{bid}')
        self.assertEqual(200, code)
        self.assertIn('rows', one['scorecard'])
        self.assertIn(one['privacy']['badge'], ('PUBLIC-SAFE', 'PRIVATE — DO NOT PUBLISH'))

    def test_the_project_catalogue_separates_the_vessel_from_the_biology(self):
        code, d = self._req('GET', '/api/project-catalogue')
        self.assertEqual(200, code)
        self.assertTrue(d['universal'])
        self.assertTrue(d['process_specific'])
        self.assertEqual({'not_modelled', 'de_novo_ai', 'expert_declared'},
                         {o['value'] for o in d['coverage_options']})

    def test_an_unsigned_in_workspace_says_it_is_not_authenticated(self):
        code, d = self._req('GET', '/api/auth/me')
        self.assertEqual(200, code)
        self.assertIs(False, d['identity']['authenticated'])
        self.assertIn('Not signed in', d['identity']['note'])

    def test_signing_in_without_a_server_configured_says_so(self):
        code, d = self._req('POST', '/api/auth/login',
                            {'username': 'a@b.c', 'password': 'x'})
        self.assertEqual(400, code)
        self.assertIn('no Omnigent server', d['error'])

    def test_an_oversized_body_is_refused(self):
        code, d = self._req('POST', '/api/discovery',
                            {'project_id': 'ipsc_macrophage', 'objective': 'x' * 300000})
        self.assertEqual(400, code)

    def test_the_discovery_page_loads_its_script_and_the_glossary(self):
        for path in ('/console.html', '/discovery.js'):
            r = urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=20)
            self.assertEqual(200, r.status, path)


if __name__ == '__main__':
    unittest.main()
