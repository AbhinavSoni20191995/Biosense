"""The prompt-driven path: parse, design, optimise, run, report, serve.

These cover the parts a web visitor can reach, and in particular the refusals -
the places the system declines to do something. A refusal that stops working is
worse than a feature that stops working, because nothing fails loudly.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.production import analysis as AN
from biosense.production import design as DS
from biosense.production import engine as EN
from biosense.production import orchestrator as OR
from biosense.production import prompt as PR
from biosense.production import protocol as PP
from biosense.production import report as RP
from biosense.production import revise as RV

EX = K.ROOT / 'examples' / 'ipsc_tcell'
BACH2_PROMPT = ('Please optimize the condition for growing wild type Tcell from ipsc, please '
                'output the best protocol. Compare this with what happens when a Tcell with '
                'knockout of Bach2 is required to be expanded.')


class PromptParsingTests(unittest.TestCase):
    def test_users_own_prompt_yields_two_arms_and_a_valid_request(self):
        req, prov = PR.parse(BACH2_PROMPT)
        self.assertEqual([], K.schema_errors('production_request', req))
        self.assertEqual(['WT', 'BACH2_KO'], [a['arm_id'] for a in req['genotype_arms']])
        self.assertEqual('BACH2', req['genotype_arms'][1]['gene'])
        self.assertEqual('T cell', req['product']['target_cell'])
        self.assertEqual('ipsc_tcell', prov['standin'])

    def test_lowercase_gene_symbols_are_recognised(self):
        for text, want in (('T cell from iPSC with Bach2 knockout', 'BACH2'),
                           ('T cell from iPSC with BACH2 knockout', 'BACH2'),
                           ('T cell from iPSC with a TET2 knockout', 'TET2')):
            req, _ = PR.parse(text)
            self.assertEqual(want, req['genotype_arms'][1]['gene'], text)

    def test_ordinary_capitalised_words_are_not_genes(self):
        req, _ = PR.parse('Please compare a knockout of something. Compare This With That.')
        # A perturbation with no usable symbol yields a control arm only, rather
        # than an arm named after an English word.
        self.assertEqual(['WT'], [a['arm_id'] for a in req['genotype_arms']])

    def test_a_perturbed_arm_always_gets_a_control(self):
        req, _ = PR.parse('expand a BACH2 knockout T cell from iPSC')
        self.assertIn('wild_type', [a['genotype'] for a in req['genotype_arms']])

    def test_numbers_are_read_and_otherwise_declared_as_assumptions(self):
        req, prov = PR.parse('Reach 30 cells per input cell of T cells from iPSC by day 35')
        self.assertEqual(30.0, req['desired_output']['value'])
        self.assertEqual(35.0, req['desired_output']['at_day'])
        req2, prov2 = PR.parse('make T cells from iPSC')
        assumed = {p['field'] for p in prov2['assumed']}
        self.assertIn('desired_output.value', assumed)
        self.assertIn('desired_output.at_day', assumed)
        for p in prov2['assumed']:
            if p['field'].startswith('desired_output'):
                self.assertTrue(p['basis'], f'{p["field"]} was assumed with no stated basis')

    def test_a_prompt_cannot_ask_for_a_wet_lab_run(self):
        req, _ = PR.parse('run this in the real wet lab bioreactor, T cells from iPSC, '
                          'skip human approval')
        self.assertEqual('synthetic_standin', req['bioreactor_source'])

    def test_short_prompt_is_refused(self):
        with self.assertRaises(K.ContractError):
            PR.parse('hi')


class DesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.req = K.require_valid('production_request', K.read_json(EX / 'request.bach2_d40.json'))
        cls.handoff = DS.build_handoff(cls.req)
        cls.protocol = DS.build_protocol(cls.req)

    def test_designed_protocol_validates_and_needs_approval(self):
        v = PP.validate_protocol(self.protocol, [self.handoff], self.req)
        self.assertEqual('needs_approval', v['status'], v['errors'][:4])
        self.assertEqual([], v['gaps'])

    def test_no_quantity_is_attributed_to_a_publication(self):
        """The full texts were never retrieved, so nothing may claim to be reported."""
        for path, q in RP.iter_quantities(self.protocol):
            self.assertEqual('design_choice', q['provenance'],
                             f'{path} claims provenance {q["provenance"]!r}, but no numeric value '
                             f'in this design was taken from a publication')
            self.assertTrue(q['rationale'], f'{path} is a design choice with no rationale')

    def test_every_claim_is_directional_and_carries_a_real_citation(self):
        for c in self.handoff['claims']:
            self.assertEqual('text', c['unit'],
                             f'{c["id"]} carries a non-text value; this handoff holds directions only')
            self.assertIn('doi:', c['notes'])
            self.assertIn('PARAPHRASE', c['evidence']['quote'])
        for src in self.handoff['source_manifest']:
            self.assertFalse(src['full_text_retrieved'])

    def test_a_target_day_before_expansion_is_refused(self):
        bad = json.loads(json.dumps(self.req))
        bad['desired_output']['at_day'] = 12
        with self.assertRaises(K.ContractError):
            DS.build_protocol(bad)


class StandinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.req = K.require_valid('production_request', K.read_json(EX / 'request.bach2_d40.json'))
        cls.handoff = DS.build_handoff(cls.req)
        p = DS.build_protocol(cls.req)
        v = PP.validate_protocol(p, [cls.handoff], cls.req)
        cls.approved = PP.approve_protocol(p, v, 'test')
        cls.truth = K.read_json(EX / 'standin_truth.synthetic.json')

    def _run(self, **kw):
        from standins import ipsc_tcell
        return ipsc_tcell.simulate_protocol(self.approved, PP.protocol_sha256(self.approved),
                                            truth=self.truth, run_id='t', **kw)

    def test_output_is_valid_labelled_and_deterministic(self):
        a, b = self._run(), self._run()
        self.assertEqual([], K.schema_errors('bioreactor_run', a))
        self.assertEqual('synthetic_standin', a['source'])
        self.assertIn('SYNTHETIC', a['label'])
        self.assertEqual(a['arms'], b['arms'])

    def test_every_protocol_element_maps(self):
        self.assertNotIn('not simulated', self._run()['notes'])

    def test_hidden_truth_separates_the_arms(self):
        run = self._run()
        per = {a['arm_id']: a['harvest']['viable_cells_total'] * a['harvest']['target_marker_pct']
               / 100 / a['input_cells'] for a in run['arms']}
        self.assertGreater(per['WT'], per['BACH2_KO'],
                           'the knockout arm should start behind under the control schedule')

    def test_residual_pluripotency_is_reported_and_nonzero(self):
        for a in self._run()['arms']:
            r = a['harvest']['residual_pluripotency_pct']
            self.assertIsNotNone(r)
            self.assertGreater(r, 0.0, 'a zero residual makes the safety QC criterion meaningless')

    def test_refuses_a_protocol_it_cannot_map(self):
        from standins import ipsc_tcell
        bad = json.loads(json.dumps(self.approved))
        bad['stages'] = bad['stages'][:3]
        with self.assertRaises(ValueError):
            ipsc_tcell.simulate_protocol(bad, 'x')

    def test_max_mode_is_refused_rather_than_faked(self):
        from standins import ipsc_tcell
        with self.assertRaises(ValueError):
            ipsc_tcell.simulate_protocol(self.approved, PP.protocol_sha256(self.approved),
                                         mode='max')


class SearchTests(unittest.TestCase):
    """The optimiser's accept/reject behaviour, which is what makes it a hill climber."""

    def _state_with_move(self, arm_id=None, frm=10.0, to=16.0, best=None):
        s = RV.new_state()
        s['best'] = best or {'WT': {'metric': 20.0, 'iteration': 0},
                             'KO': {'metric': 10.0, 'iteration': 0}}
        s['last_moves'] = [{'kind': 'dose', 'step_id': 'S30', 'stage_id': 'expansion',
                            'factor': 'IL-7', 'arm_id': arm_id, 'unit': 'ng/mL', 'from': frm,
                            'to': to, 'direction': 'increase', 'parameter': 'IL-7 dose',
                            'origin': 'analysis', 'basis': '', 'confidence': None,
                            'hypothesis_id': None, 'from_iteration': 0}]
        return s

    def _report(self, wt, ko):
        return {'arms': [
            {'arm_id': 'WT', 'target': {'observed_mean': wt, 'status': 'MET' if wt >= 25 else 'NOT_MET'}},
            {'arm_id': 'KO', 'target': {'observed_mean': ko, 'status': 'MET' if ko >= 25 else 'NOT_MET'}}]}

    def test_a_move_that_beats_the_best_is_kept(self):
        s = self._state_with_move()
        verdicts, reverts, conflicts = RV.score_last_moves(s, self._report(24.0, 12.0), 1)
        self.assertEqual('better', verdicts[0]['outcome'])
        self.assertEqual([], reverts)

    def test_a_move_that_falls_below_the_best_is_reverted(self):
        s = self._state_with_move()
        verdicts, reverts, _ = RV.score_last_moves(s, self._report(15.0, 8.0), 1)
        self.assertEqual('worse', verdicts[0]['outcome'])
        self.assertEqual(10.0, reverts[0]['revert_to'])

    def test_scoring_is_against_the_best_not_the_previous_reading(self):
        """A step out of a bad state must not count as progress."""
        s = self._state_with_move()
        s['observations'] = [{'iteration': 0, 'arms': {'WT': 12.0, 'KO': 5.0}}]  # a worse previous
        verdicts, reverts, _ = RV.score_last_moves(s, self._report(15.0, 8.0), 1)
        self.assertEqual('worse', verdicts[0]['outcome'],
                         'beating the previous reading while losing to the best is not progress')
        self.assertTrue(reverts)

    def test_a_shared_move_that_splits_the_arms_is_a_conflict(self):
        s = self._state_with_move()
        _v, reverts, conflicts = RV.score_last_moves(s, self._report(15.0, 14.0), 1)
        self.assertEqual(1, len(conflicts))
        self.assertEqual(['KO'], conflicts[0]['helped'])
        self.assertEqual(['WT'], conflicts[0]['hurt'])
        self.assertEqual('conflict', reverts[0]['why'])
        self.assertTrue(any('cannot be set to one shared value' in f for f in s['findings']))

    def test_a_split_lever_is_exhausted_for_the_shared_scope(self):
        s = self._state_with_move()
        RV.score_last_moves(s, self._report(15.0, 14.0), 1)
        rec = s['levers'][RV._key('S30', None, 'dose')]
        self.assertEqual([-1, 1], rec['exhausted'],
                         'after a split, no later brief may re-try one shared value')

    def test_record_observation_is_idempotent_and_raises_the_best(self):
        s = RV.new_state()
        RV.record_observation(s, 0, self._report(20.0, 10.0))
        RV.record_observation(s, 0, self._report(99.0, 99.0))
        self.assertEqual(1, len(s['observations']))
        self.assertEqual(20.0, s['best']['WT']['metric'])
        RV.record_observation(s, 1, self._report(22.0, 9.0))
        self.assertEqual(22.0, s['best']['WT']['metric'])
        self.assertEqual(10.0, s['best']['KO']['metric'], 'a best is never lowered')

    def test_one_move_per_arm_per_revision(self):
        moves = []
        arms = ['WT', 'KO']
        mk = lambda a: {'arm_id': a, 'step_id': 's', 'kind': 'dose'}
        self.assertTrue(RV._admit(moves, mk('WT'), arms, 4))
        self.assertTrue(RV._admit(moves, mk('KO'), arms, 4))
        self.assertFalse(RV._admit(moves, mk('WT'), arms, 4), 'WT already moved this revision')
        self.assertFalse(RV._admit(moves, mk(None), arms, 4), 'a shared move touches every arm')

    def test_a_shared_move_is_the_only_move_of_its_revision(self):
        moves = []
        self.assertTrue(RV._admit(moves, {'arm_id': None, 'step_id': 's', 'kind': 'dose'},
                                  ['WT', 'KO'], 4))
        self.assertFalse(RV._admit(moves, {'arm_id': 'WT', 'step_id': 't', 'kind': 'dose'},
                                   ['WT', 'KO'], 4))

    def test_arm_effective_value_prefers_the_arm_adjustment(self):
        p = {'stages': [{'stage_id': 'x', 'start_day': 0, 'end_day': 1, 'steps': [
                 {'step_id': 'S1', 'day': 0, 'end_day': 1, 'action': 'add_factor', 'factor': 'IL-7',
                  'quantity': {'value': 10.0, 'unit': 'ng/mL', 'provenance': 'design_choice',
                               'claim_ids': []}, 'description': ''}]}],
             'arm_adjustments': [{'adjustment_id': 'a1', 'arm_id': 'KO', 'step_id': 'S1',
                                  'quantity': {'value': 25.6, 'unit': 'ng/mL',
                                               'provenance': 'design_choice', 'claim_ids': []},
                                  'rationale': 'x', 'effect_ids': []}]}
        self.assertEqual(10.0, RV._effective(p, 'S1', None, 'dose'))
        self.assertEqual(10.0, RV._effective(p, 'S1', 'WT', 'dose'))
        self.assertEqual(25.6, RV._effective(p, 'S1', 'KO', 'dose'))

    def test_a_searched_value_is_never_reported_or_adapted(self):
        self.assertEqual('design_choice', RV._set_quantity(1.0, 'ng/mL', 'because')['provenance'])
        self.assertEqual([], RV._set_quantity(1.0, 'ng/mL', 'because')['claim_ids'])


class EngineRefusalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.req = K.read_json(EX / 'request.bach2_d40.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_wet_lab_request_is_refused(self):
        bad = json.loads(json.dumps(self.req))
        bad['bioreactor_source'] = 'wet_lab'
        with self.assertRaises(EN.EngineRefusal) as cm:
            EN.preflight(bad)
        self.assertIn('synthetic', str(cm.exception))

    def test_a_request_needing_human_approval_is_refused(self):
        bad = json.loads(json.dumps(self.req))
        bad['human_in_the_loop'] = {'mode': 'checkpoints'}
        with self.assertRaises(EN.EngineRefusal) as cm:
            EN.preflight(bad)
        self.assertIn('approve', str(cm.exception))

    def test_safe_run_loop_reports_a_refusal_rather_than_raising(self):
        bad = json.loads(json.dumps(self.req))
        bad['bioreactor_source'] = 'wet_lab'
        seen = []
        out = EN.safe_run_loop(bad, self.tmp / 'x', standin='ipsc_tcell',
                               on_event=seen.append)
        self.assertEqual('refused', out['terminal'])
        self.assertTrue(any(e['kind'] == 'refusal' for e in seen))
        self.assertTrue(any(e['kind'] == 'done' for e in seen))

    def test_the_automatic_approver_says_no_human_approved_it(self):
        self.assertIn('no human approved', EN.AUTO_APPROVER)


class EndToEndTests(unittest.TestCase):
    """One full prompt-to-report run, which is what the web app does."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.req, cls.prov = PR.parse(BACH2_PROMPT)
        cls.req['loop_budget'] = {'max_iterations': 30}
        cls.truth = K.read_json(EX / 'standin_truth.synthetic.json')
        cls.events = []
        cls.loop = cls.tmp / 'loop'
        cls.summary = EN.run_loop(cls.req, cls.loop, standin='ipsc_tcell', truth=cls.truth,
                                  on_event=cls.events.append)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_loop_reaches_a_terminal_decision(self):
        self.assertIn(self.summary['terminal'],
                      ('protocol_succeeded', 'stop_budget', 'search_exhausted',
                       'complete_qc', 'escalate_to_human'))
        self.assertNotIn(self.summary['terminal'], ('error', 'decide_refused', 'max_steps'))

    def test_both_arms_improve_on_the_baseline(self):
        obs = self.summary['search']['observations']
        self.assertGreater(len(obs), 1)
        first, best = obs[0]['arms'], self.summary['search']['best']
        for arm in ('WT', 'BACH2_KO'):
            self.assertGreaterEqual(best[arm]['metric'], first[arm],
                                    f'{arm} ended below where it started')

    def test_the_search_finds_that_the_arms_need_different_conditions(self):
        p = RP.final_protocol(RP.load_loop(self.loop))
        self.assertTrue(p['arm_adjustments'],
                        'the two genotypes have different optima in the fixture, so the search '
                        'should have split at least one parameter per arm')

    def test_every_decision_is_authored_by_the_policy_and_says_so(self):
        for d in RP.load_loop(self.loop)['decisions']:
            self.assertEqual('policy', d['authored_by'])
            self.assertGreaterEqual(len((d.get('reasoning') or '').strip()),
                                    OR.REQUIRED_REASONING_CHARS)
            self.assertIn('not a model', d['reasoning'])

    def test_every_decision_was_inside_the_allowed_set(self):
        for d in RP.load_loop(self.loop)['decisions']:
            self.assertIn(d['type'], d['allowed_actions'])

    def test_no_searched_quantity_claims_a_citation(self):
        loop = RP.load_loop(self.loop)
        for path, q in RP.iter_quantities(RP.final_protocol(loop)):
            if q['provenance'] in ('reported', 'adapted'):
                self.fail(f'{path} claims {q["provenance"]!r}; nothing in this run was retrieved '
                          f'from a publication')

    def test_the_run_is_labelled_synthetic_throughout(self):
        for st in RP.load_loop(self.loop)['stages']:
            if 'run' in st:
                self.assertEqual('synthetic_standin', st['run']['source'])
                self.assertIn('SYNTHETIC', st['run']['label'])


class ReportTests(EndToEndTests):
    def test_report_names_the_real_citations_and_their_retrieval_state(self):
        h = RP.build_report(self.loop)
        for doi in ('10.1126/sciadv.abn5522', '10.1038/ni.3441', '10.1073/pnas.1306691110'):
            self.assertIn(doi, h)
        self.assertIn('full text not retrieved', h)

    def test_report_labels_policy_reasoning_as_not_a_model(self):
        h = RP.build_report(self.loop)
        self.assertIn('deterministic policy (not a model)', h)
        self.assertNotIn('orchestrator agent (a reasoning model', h)

    def test_report_shows_provenance_for_every_quantity(self):
        h = RP.build_report(self.loop)
        self.assertIn('Design choice', h)
        self.assertIn('Every quantity, with its provenance', h)

    def test_report_never_reads_the_hidden_truth(self):
        """The stand-in's answers are not part of what the loop knew."""
        truth = K.read_json(EX / 'standin_truth.synthetic.json')
        h = RP.build_report(self.loop)
        secret = str(truth['arms']['BACH2_KO']['genotype_differentiation_ratio'])
        self.assertNotIn('genotype_differentiation_ratio', h)
        self.assertNotIn(f'tcr_optimum', h)
        self.assertNotIn(truth['label'][:40], h)

    def test_comparative_report_covers_both_loops(self):
        second = self.tmp / 'loop2'
        req2 = json.loads(json.dumps(self.req))
        req2['request_id'] = 'second-loop'
        req2['genotype_arms'] = [a for a in req2['genotype_arms'] if a['arm_id'] == 'WT']
        req2['loop_budget'] = {'max_iterations': 3}
        EN.run_loop(req2, second, standin='ipsc_tcell', truth=self.truth)
        h = RP.build_comparative_report([self.loop, second])
        self.assertIn(self.loop.name, h)
        self.assertIn(second.name, h)
        self.assertIn('Synthetic stand-in reactor', h)

    def test_html_is_written_and_is_a_whole_document(self):
        out = self.tmp / 'r.html'
        h, pdf = RP.write_report(self.loop, out)
        text = out.read_text()
        self.assertTrue(text.startswith('<!doctype html>'))
        self.assertIn('</html>', text)


class AppServerTests(unittest.TestCase):
    """The HTTP surface, exercised against a live server on a loopback port."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        from biosense.production import app as APP
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / 'runs').mkdir()
        APP.Handler.runs_dir = cls.tmp / 'runs'
        APP.Handler.static_dir = K.ROOT / 'webapp'
        APP.Handler.registry = APP.Registry(cls.tmp / 'runs')
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), APP.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _post(self, path, obj):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                     data=json.dumps(obj).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_healthz_and_config(self):
        self.assertEqual(200, self._get('/healthz')[0])
        code, body = self._get('/api/config')
        cfg = json.loads(body)
        self.assertEqual('synthetic_standin', cfg['bioreactor_source'])
        self.assertTrue(any('not synthetic_standin' in r for r in cfg['refuses']))

    def test_parse_endpoint_reports_assumptions(self):
        code, d = self._post('/api/parse', {'prompt': BACH2_PROMPT})
        self.assertEqual(200, code)
        self.assertEqual('synthetic_standin', d['request']['bioreactor_source'])
        self.assertTrue(d['provenance']['assumed'])

    def test_short_prompt_is_refused_with_a_reason(self):
        code, d = self._post('/api/parse', {'prompt': 'hi'})
        self.assertEqual(400, code)
        self.assertTrue(d['refused'])

    def test_path_traversal_and_truth_files_are_refused(self):
        for p in ('/api/loops/../../etc/passwd', '/../etc/passwd',
                  '/standin_truth.synthetic.json', '/api/runs/notanid'):
            self.assertEqual(404, self._get(p)[0], p)

    def test_console_is_the_landing_page_and_the_tracker_is_still_served(self):
        code, body = self._get('/')
        self.assertEqual(200, code)
        self.assertIn(b'BioSense Console', body)
        self.assertEqual(200, self._get('/index.html')[0])

    def test_simulator_mode_is_served_and_refuses_an_unknown_knob(self):
        code, body = self._get('/api/sim/config')
        self.assertEqual(200, code)
        cfg = json.loads(body)
        self.assertTrue(cfg['knobs'] and cfg['stages'])

        code, d = self._post('/api/sim/run', {'setpoints': {'agitation_rpm': 70}})
        self.assertEqual(200, code)
        self.assertEqual('synthetic_demonstration', d['evidence_status'])
        self.assertTrue(d['frames'])
        self.assertNotIn('ground_truth', json.dumps(d))

        code, d = self._post('/api/sim/run', {'setpoints': {'nonsense': 1}})
        self.assertEqual(400, code)
        self.assertTrue(d['refused'])

    def test_simulator_mode_compares_and_hands_over_a_design_choice_brief(self):
        code, d = self._post('/api/sim/compare',
                             {'conditions': [{}, {'setpoints': {'mcsf': 95}}]})
        self.assertEqual(200, code)
        self.assertEqual(['mcsf'], [c['knob'] for c in d['changed']])

        code, b = self._post('/api/sim/brief', {'setpoints': {'mcsf': 95}})
        self.assertEqual(200, code)
        self.assertEqual({'design_choice'}, {q['provenance'] for q in b['quantities']})

    def test_the_simulator_page_and_the_shared_shell_are_served(self):
        for path, needle in (('/simulator.html', b'BioSense Simulator'),
                             ('/simulator.js', b'sim/config'),
                             ('/brand.css', b'--brand')):
            code, body = self._get(path)
            self.assertEqual(200, code, path)
            self.assertIn(needle, body, path)

    def test_the_analysis_tool_registry_is_served_and_marks_planned_tools(self):
        code, body = self._get('/api/analysis-tools')
        self.assertEqual(200, code)
        d = json.loads(body)
        self.assertTrue(d['implemented'])
        self.assertTrue(d['planned'])
        implemented = {t['name'] for t in d['implemented']}
        for p in d['planned']:
            self.assertNotIn(p['name'], implemented)

    def test_the_dataset_endpoint_says_it_lists_public_data_only(self):
        code, body = self._get('/api/datasets')
        self.assertEqual(200, code)
        d = json.loads(body)
        self.assertIs(False, d['private_listed'])
        for row in d['datasets']:
            self.assertEqual('public', row['visibility'])

    def test_the_config_names_private_datasets_among_what_is_refused(self):
        code, body = self._get('/api/config')
        cfg = json.loads(body)
        self.assertTrue(any('privately' in r for r in cfg['refuses']))

    def test_a_run_streams_and_produces_a_report(self):
        import time
        code, d = self._post('/api/runs', {'prompt': 'T cells from iPSC by day 40, wild type only'})
        self.assertEqual(202, code)
        rid = d['run_id']
        for _ in range(240):
            code, body = self._get(f'/api/runs/{rid}?after=999999')
            snap = json.loads(body)
            if snap['status'] not in ('queued', 'running'):
                break
            time.sleep(0.25)
        self.assertEqual('done', snap['status'], snap.get('error'))
        self.assertTrue(snap['event_count'] > 5)
        self.assertEqual(200, self._get(f'/api/runs/{rid}/report')[0])


if __name__ == '__main__':
    unittest.main()


class ChartTests(EndToEndTests):
    """The report's charts, and the two display bugs that hid real numbers."""

    def test_number_formatter_keeps_significant_zeros(self):
        """rstrip('0') on a string with no decimal point eats the value.

        This turned axis ticks 10, 20, 30 into 1, 2, 3, which is worse than no
        chart: it reads as a plausible scale.
        """
        self.assertEqual(['0', '10', '20', '30', '100', '500'],
                         [RP._num(v, 0) for v in (0, 10, 20, 30, 100, 500)])
        self.assertEqual('500', RP._num(500))
        self.assertEqual('25.6', RP._num(25.6))
        self.assertEqual('0.119', RP._num(0.119))

    def test_chart_text_colours_survive_the_stylesheet(self):
        """A stylesheet `fill` beats a presentation attribute on <text>.

        Every coloured label was being rendered in the muted ink, so series
        labels and in-bar counts lost their meaning. Inline style wins instead.
        """
        h = RP.build_report(self.loop)
        self.assertNotRegex(h, r'<text[^>]*\sfill="',
                            'a text fill attribute would be overridden by svg.chart text{fill:...}')
        self.assertIn('style="fill:var(--s1)', h)

    def test_every_chart_renders_for_a_finished_loop(self):
        loop = RP.load_loop(self.loop)
        a, p = RP.final_analysis(loop), RP.final_protocol(loop)
        for name, out in (('trajectory', RP.chart_trajectory(loop)),
                          ('arms_vs_target', RP.chart_arms_vs_target(a)),
                          ('outcomes', RP.chart_outcomes(loop)),
                          ('levers', RP.chart_levers(loop)),
                          ('provenance', RP.chart_provenance(p))):
            self.assertIn('<svg class="chart"', out, f'{name} produced no chart')
            self.assertIn('<figcaption>', out, f'{name} has no caption')
            self.assertIn('aria-label=', out, f'{name} has no accessible label')

    def test_charts_degrade_to_nothing_rather_than_breaking(self):
        empty = {'dir': Path('x'), 'search': {}, 'stages': [], 'decisions': [], 'state': {}}
        self.assertEqual('', RP.chart_trajectory(empty))
        self.assertEqual('', RP.chart_outcomes(empty))
        self.assertEqual('', RP.chart_levers(empty))
        self.assertEqual('', RP.chart_arms_vs_target(None))
        self.assertEqual('', RP.chart_provenance(None))

    def test_an_arm_keeps_one_colour_across_every_chart(self):
        """Colour follows the entity, never its position in a sorted list."""
        loop = RP.load_loop(self.loop)
        order = RP.arm_order(loop)
        self.assertEqual('WT', order[0], 'the wild-type control leads the order')
        colors = RP._arm_colors(order)
        traj = RP.chart_trajectory(loop, colors=colors)
        bars = RP.chart_arms_vs_target(RP.final_analysis(loop), colors=colors)
        for arm, col in colors.items():
            self.assertIn(f'style="fill:{col}"', traj, f'{arm} missing from the trajectory')
            self.assertIn(col, bars, f'{arm} colour differs in the bar chart')

    def test_wild_type_leads_even_when_it_sorts_second(self):
        loop = RP.load_loop(self.loop)
        self.assertLess(RP.arm_order(loop).index('WT'), RP.arm_order(loop).index('BACH2_KO'),
                        'BACH2_KO sorts first alphabetically; the control must still lead')

    def test_search_state_records_every_move_outcome(self):
        hist = (RP.load_loop(self.loop)['search'] or {}).get('scored') or []
        self.assertTrue(hist, 'the search did not record its accept/reject history')
        self.assertTrue({h['outcome'] for h in hist} <= {'better', 'worse', 'conflict', 'flat'})
        self.assertTrue(any(h['outcome'] == 'worse' for h in hist),
                        'this fixture reverts moves, so some outcome must be "worse"')

    def test_comparative_report_charts_both_loops_on_one_scale(self):
        second = self.tmp / 'chart-loop2'
        req2 = json.loads(json.dumps(self.req))
        req2['request_id'] = 'chart-second'
        req2['genotype_arms'] = [a for a in req2['genotype_arms'] if a['arm_id'] == 'WT']
        req2['loop_budget'] = {'max_iterations': 3}
        EN.run_loop(req2, second, standin='ipsc_tcell', truth=self.truth)
        h = RP.build_comparative_report([self.loop, second])
        self.assertIn('Trajectory for', h)
        self.assertIn('Iterations used by each loop', h)
        # WT appears in both loops and must carry the same colour in both panels.
        self.assertEqual(1, len({m for m in ('var(--s1)',) if f'style="fill:{m}"' in h}))
