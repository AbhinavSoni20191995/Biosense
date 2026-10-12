"""Two runs of the same question, compared by code.

Ask a language model the same thing twice and it does not answer twice the
same way. A person running this system had one answer and no way to tell
whether it was *the* answer or one of several the run could have produced.

What these tests hold is the shape of an honest comparison: the inputs are
checked first, because two runs asked different things did not measure
variability; agreement is counted rather than asserted; and nothing anywhere
calls one of the two runs better than the other.
"""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.production import compare_runs as CR

REQUEST = {
    'project_id': 'ipsc_macrophage', 'runtime_mode': 'local_real_ai',
    'objective': 'increase viable macrophage yield from iPSC',
    'effort': 'standard', 'mode': 'specialist', 'dataset_ids': [], 'public_data': True,
    'constraints': [], 'research_context': {'strictness': 'prefer'},
}


def protocol(*, levers, values):
    return {
        'kind': 'protocol_summary',
        'hypothesis_ledger': [
            {'hypothesis_id': f'H{i:02d}', 'adopted': True, 'status': 'proposed',
             'confidence': 'moderate',
             'lever': {'parameter_id': pid, 'label': pid.replace('_', ' '),
                       'direction': d, 'candidate_value': v, 'unit': 'ng/mL',
                       'stage': 'myeloid'}}
            for i, (pid, d, v) in enumerate(levers, 1)],
        'stages': [{'stage_id': 'myeloid', 'parameters': [
            {'parameter_id': pid, 'label': pid.replace('_', ' '), 'unit': 'ng/mL',
             'recommended_value': val, 'provenance': 'adapted', 'changed': True}
            for pid, val in values]}],
        'gaps': [], 'limitations': [],
    }


def write_run(root, name, *, request=None, levers=(), values=(), model='claude-opus-5',
              route=None, fidelity=None, no_protocol=False):
    d = Path(root) / name
    d.mkdir(parents=True)
    (d / 'discovery_request.json').write_text(json.dumps(dict(REQUEST, **(request or {}))))
    (d / 'app_run.json').write_text(json.dumps(
        {'run_id': name, 'status': 'done', 'title': None, 'started_at': 1.0,
         'session': {'llm_model': model, 'harness': 'omnigent'}}))
    if not no_protocol:
        (d / 'protocol_summary.json').write_text(json.dumps(
            protocol(levers=levers, values=values)))
    if route:
        (d / 'route_selection.json').write_text(json.dumps(
            {'chosen': {'route_id': route, 'label': route}, 'need_assessment':
             {'verdict': 'well_matched'}}))
    if fidelity:
        (d / 'cell_fidelity.json').write_text(json.dumps({'overall': {'expected_match': fidelity}}))
    return d


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def two(self, **b_kw):
        a = write_run(self.root, 'ai-a', levers=[('mcsf_ng_ml', 'increase', 100)],
                      values=[('mcsf_ng_ml', 100)])
        b = write_run(self.root, 'ai-b', **b_kw)
        return CR.compare(a, b)

    def test_the_same_answer_twice_is_recognised_as_the_same_answer(self):
        c = self.two(levers=[('mcsf_ng_ml', 'increase', 100)], values=[('mcsf_ng_ml', 100)])
        self.assertEqual('same_inputs_same_answer', c['verdict'])
        self.assertEqual((1, 1, 0), (c['summary']['levers_total'], c['summary']['levers_agreed'],
                                     c['summary']['levers_opposed']))
        self.assertIn('not evidence that either answer is biologically right',
                      c['verdict_text'])

    def test_a_lever_only_one_run_found_is_a_candidate_not_a_finding(self):
        c = self.two(levers=[('mcsf_ng_ml', 'increase', 100), ('il3_ng_ml', 'increase', 25)],
                     values=[('mcsf_ng_ml', 100), ('il3_ng_ml', 25)])
        self.assertEqual('same_inputs_different_answer', c['verdict'])
        only = [l for l in c['levers'] if l['agreement'] == 'only_b']
        self.assertEqual(['il3_ng_ml'], [l['parameter_id'] for l in only])
        self.assertTrue(any('round plan as an arm' in t for t in c['to_make_them_comparable']))

    def test_two_runs_pushing_a_lever_opposite_ways_is_called_out(self):
        """The most important disagreement there is, and the easiest to miss in
        two long reports read a week apart."""
        c = self.two(levers=[('mcsf_ng_ml', 'decrease', 40)], values=[('mcsf_ng_ml', 40)])
        row = c['levers'][0]
        self.assertEqual('opposed', row['agreement'])
        self.assertEqual(1, c['summary']['levers_opposed'])
        self.assertTrue(any('opposite directions' in t for t in c['to_make_them_comparable']))

    def test_the_same_direction_with_a_different_dose_is_its_own_answer(self):
        c = self.two(levers=[('mcsf_ng_ml', 'increase', 150)], values=[('mcsf_ng_ml', 150)])
        self.assertEqual('same_direction', c['levers'][0]['agreement'])
        self.assertEqual(1, c['summary']['values_differing'])
        self.assertTrue(any('the range the two runs fall in' in t
                            for t in c['to_make_them_comparable']))

    def test_a_rounded_value_is_not_a_disagreement(self):
        c = self.two(levers=[('mcsf_ng_ml', 'increase', 100.0)], values=[('mcsf_ng_ml', 100.0)])
        self.assertEqual('agreed', c['levers'][0]['agreement'])


class InputTests(unittest.TestCase):
    """Two runs of different questions did not measure variability."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.a = write_run(self.root, 'ai-a', levers=[('mcsf_ng_ml', 'increase', 100)],
                           values=[('mcsf_ng_ml', 100)])

    def test_a_different_model_is_reported_before_anything_else(self):
        b = write_run(self.root, 'ai-b', model='some-other-model',
                      levers=[('mcsf_ng_ml', 'increase', 100)], values=[('mcsf_ng_ml', 100)])
        c = CR.compare(self.a, b)
        self.assertEqual('different_inputs', c['verdict'])
        self.assertEqual(['model'], [d['field'] for d in c['input_differences']])
        self.assertIn('BIOSENSE_AGENT_MODEL', c['to_make_them_comparable'][0])
        self.assertIn('nothing below measures run-to-run variability', c['verdict_text'])

    def test_a_different_objective_is_the_first_thing_to_fix(self):
        b = write_run(self.root, 'ai-b', request={'objective': 'something else entirely'},
                      levers=[('mcsf_ng_ml', 'increase', 100)], values=[('mcsf_ng_ml', 100)])
        c = CR.compare(self.a, b)
        self.assertIn('objective', [d['field'] for d in c['input_differences']])

    def test_every_input_difference_says_what_it_means_for_the_comparison(self):
        b = write_run(self.root, 'ai-b', request={'effort': 'deep', 'public_data': False,
                                                  'dataset_ids': ['GSE1']},
                      levers=[('mcsf_ng_ml', 'increase', 100)], values=[('mcsf_ng_ml', 100)])
        c = CR.compare(self.a, b)
        fields = {d['field'] for d in c['input_differences']}
        self.assertEqual({'effort', 'public_data', 'dataset_ids'}, fields)
        for d in c['input_differences']:
            self.assertTrue(d['means'], d['field'])

    def test_the_variability_of_the_method_is_stated_whatever_the_verdict(self):
        """Even two perfectly matched runs are two samples of a stochastic
        process, and a comparison that implies otherwise would mislead."""
        b = write_run(self.root, 'ai-b', levers=[('mcsf_ng_ml', 'increase', 100)],
                      values=[('mcsf_ng_ml', 100)])
        c = CR.compare(self.a, b)
        self.assertTrue(any('identical output' in t for t in c['to_make_them_comparable']))
        self.assertTrue(any('literature cache' in t for t in c['to_make_them_comparable']))


class EdgeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_a_run_with_no_protocol_is_said_to_be_uncomparable_rather_than_agreeing(self):
        a = write_run(self.root, 'ai-a', levers=[('mcsf_ng_ml', 'increase', 100)],
                      values=[('mcsf_ng_ml', 100)])
        b = write_run(self.root, 'ai-b', no_protocol=True)
        c = CR.compare(a, b)
        self.assertEqual('not_comparable', c['verdict'])
        self.assertFalse(c['b']['has_protocol'])

    def test_comparing_a_run_with_itself_is_refused(self):
        a = write_run(self.root, 'ai-a', levers=[('mcsf_ng_ml', 'increase', 100)],
                      values=[('mcsf_ng_ml', 100)])
        with self.assertRaisesRegex(K.ContractError, 'says nothing'):
            CR.compare(a, a)

    def test_a_missing_directory_reads_as_an_empty_run_rather_than_crashing(self):
        a = write_run(self.root, 'ai-a', levers=[('mcsf_ng_ml', 'increase', 100)],
                      values=[('mcsf_ng_ml', 100)])
        c = CR.compare(a, self.root / 'does-not-exist')
        self.assertEqual('not_comparable', c['verdict'])

    def test_the_route_and_the_expected_fidelity_are_compared_where_both_have_them(self):
        a = write_run(self.root, 'ai-a', levers=[], values=[], route='directed_diff',
                      fidelity='partial')
        b = write_run(self.root, 'ai-b', levers=[], values=[], route='forward_prog',
                      fidelity='partial')
        c = CR.compare(a, b)
        self.assertFalse(c['summary']['route_agreed'])
        self.assertTrue(c['summary']['fidelity_agreed'])

    def test_the_written_comparison_names_both_runs_and_never_a_winner(self):
        a = write_run(self.root, 'ai-a', levers=[('mcsf_ng_ml', 'increase', 100)],
                      values=[('mcsf_ng_ml', 100)])
        b = write_run(self.root, 'ai-b', levers=[('mcsf_ng_ml', 'decrease', 40)],
                      values=[('mcsf_ng_ml', 40)])
        md = CR.markdown(CR.compare(a, b))
        self.assertIn('ai-a', md)
        self.assertIn('ai-b', md)
        self.assertIn('OPPOSED', md)
        self.assertIn('neither run is called better than the other', md)
        # The disclaimer is the only place a judgement word may appear.
        body = md.replace('neither run is called better than the other here.', '')
        for word in ('better', 'worse', 'winner', 'more reliable', 'outperform'):
            self.assertNotIn(word, body.lower(), f'{word!r} is a judgement this cannot make')


class ApiTests(unittest.TestCase):
    """Through the server, because that is how a person reaches it."""

    @classmethod
    def setUpClass(cls):
        import os
        import threading
        from http.server import ThreadingHTTPServer
        from biosense import workspace as WS
        from biosense.production import app as APP
        from biosense.production import authz as AZ
        from biosense.production import budget as BU
        from biosense.production import runtime as RT
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
        cls.srv.shutdown(); cls.srv.server_close()
        if cls._private is None:
            os.environ.pop('BIOSENSE_PRIVATE_DATA', None)
        else:
            os.environ['BIOSENSE_PRIVATE_DATA'] = cls._private
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, rid, **kw):
        from biosense.production import run_store as RS
        d = write_run(self.tmp / 'runs', f'ai-20260101-{rid[:6]}', **kw)
        RS.write_state(d, {'run_id': rid, 'status': 'done', 'is_real': True,
                           'runtime_mode': 'local_real_ai', 'owner': None,
                           'project_id': 'ipsc_macrophage', 'objective': REQUEST['objective'],
                           'run_dir': d.name, 'started_at': 1.0, 'finished_at': 2.0,
                           'session': {'llm_model': kw.get('model', 'claude-opus-5'),
                                       'harness': 'omnigent'}})
        return d

    def _get(self, path):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{self.port}{path}', timeout=30) as r:
                body = r.read()
                return r.status, (json.loads(body) if r.headers.get('Content-Type', '')
                                  .startswith('application/json') else body.decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def test_two_of_my_runs_compare_over_http(self):
        a, b = 'a' * 16, 'b' * 16
        self._run(a, levers=[('mcsf_ng_ml', 'increase', 100)], values=[('mcsf_ng_ml', 100)])
        self._run(b, levers=[('mcsf_ng_ml', 'decrease', 40)], values=[('mcsf_ng_ml', 40)])
        code, d = self._get(f'/api/discovery/compare?a={a}&b={b}')
        self.assertEqual(200, code, d)
        self.assertEqual('opposed', d['levers'][0]['agreement'])
        code, md = self._get(f'/api/discovery/compare?a={a}&b={b}&format=md')
        self.assertEqual(200, code)
        self.assertIn('Two runs compared', md)

    def test_a_run_that_does_not_exist_is_a_404_not_a_comparison(self):
        a = 'c' * 16
        self._run(a, levers=[], values=[])
        code, d = self._get(f'/api/discovery/compare?a={a}&b={"d" * 16}')
        self.assertEqual(404, code, d)

    def test_the_page_offers_the_comparison(self):
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function compareBar', js)
        self.assertIn('/api/discovery/compare?a=', js)
        self.assertIn('function renderComparison', js)


if __name__ == '__main__':
    unittest.main()
