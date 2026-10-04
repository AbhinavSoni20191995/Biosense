import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import campaign as CAM
from biosense import contracts as K
from biosense import outcomes as OC
from tests.helpers import EX, load


def fake_result(run_id, end, init=None, status='completed', draw='nominal', durations=(120.0, 216.0)):
    return {'run_id': run_id, 'experiment_id': 'e-' + run_id, 'parameter_draw_id': draw, 'status': status,
            'errors': [] if status == 'completed' else ['forced'],
            'stage_schedule': [{'stage': 'ipsc_expansion', 'duration_h': durations[0]},
                               {'stage': 'cardiac_differentiation', 'duration_h': durations[1]}],
            'endpoint_state': end if status == 'completed' else None,
            'initial_state': init or {'R': 100.0, 'C': 0.0, 'O': 0.0, 'D': 0.0},
            'cost_accounting': {'culture_time_h': sum(durations)}}


def rows_for(end):
    return [{'t_h': 0.0, 'stage': 'ipsc_expansion', 'R': 100.0, 'C': 0.0, 'O': 0.0, 'D': 0.0},
            {'t_h': 120.0, 'stage': 'ipsc_expansion', 'R': 400.0, 'C': 0.0, 'O': 0.0, 'D': 10.0},
            {'t_h': 336.0, 'stage': 'cardiac_differentiation', **end}]


OBJ = {'schema_version': '1.0', 'objective_id': 'o', 'label': 'test', 'maximize': 'target_cell_yield',
       'constraints': [{'id': 'pur', 'metric': 'target_purity', 'op': '>=', 'value': 0.5}],
       'target': None, 'total_culture_time_h': 336}


class MetricTests(unittest.TestCase):
    def metrics(self, end, **kw):
        r = fake_result('r1', end, **kw)
        return OC.compute_metrics(r, OBJ, rows_for(end) if r['status'] == 'completed' else None)

    def test_9_hand_calculated_metrics(self):
        m = self.metrics({'R': 20.0, 'C': 60.0, 'O': 20.0, 'D': 25.0})['metrics']
        self.assertEqual(m['total_viable_cells']['value'], 100.0)
        self.assertEqual(m['target_cell_yield']['value'], 60.0)
        self.assertAlmostEqual(m['target_purity']['value'], 0.6)
        self.assertAlmostEqual(m['viability_proxy']['value'], 0.8)
        self.assertAlmostEqual(m['target_yield_per_initial_cell']['value'], 0.6)
        self.assertEqual(m['cells_entering_differentiation']['value'], 400.0)
        self.assertIsNone(m['resource_cost']['value'])
        self.assertIn('no target', m['time_to_target_h']['flag'])

    def test_time_to_target_and_censoring(self):
        obj = dict(OBJ, target={'id': 't', 'metric': 'target_cell_yield', 'op': '>=', 'value': 50})
        end = {'R': 20.0, 'C': 60.0, 'O': 20.0, 'D': 25.0}
        m = OC.compute_metrics(fake_result('r', end), obj, rows_for(end))['metrics']
        self.assertEqual(m['time_to_target_h']['value'], 336.0)
        obj['target']['value'] = 1e9
        m = OC.compute_metrics(fake_result('r', end), obj, rows_for(end))['metrics']
        self.assertIsNone(m['time_to_target_h']['value'])
        self.assertIn('censored', m['time_to_target_h']['flag'])

    def test_8_zero_denominators_undefined_and_unranked(self):
        zero = {'R': 0.0, 'C': 0.0, 'O': 0.0, 'D': 0.0}
        rec = self.metrics(zero, init={'R': 0.0, 'C': 0.0, 'O': 0.0, 'D': 0.0})
        for k in ('target_purity', 'viability_proxy', 'target_yield_per_initial_cell'):
            self.assertIsNone(rec['metrics'][k]['value'])
            self.assertIn('undefined', rec['metrics'][k]['flag'])
        cmp = OC.compare_results([rec], 'r1', OBJ)
        self.assertEqual(cmp['ranking'], [])
        self.assertFalse(cmp['feasibility']['r1']['feasible'])

    def test_8_failed_runs_counted_not_ranked(self):
        ok = OC.compute_metrics(fake_result('a', {'R': 0.0, 'C': 9.0, 'O': 1.0, 'D': 0.0}), OBJ, rows_for({'R': 0.0, 'C': 9.0, 'O': 1.0, 'D': 0.0}))
        bad = OC.compute_metrics(fake_result('b', None, status='failed'), OBJ)
        self.assertIsNone(bad['metrics'])
        cmp = OC.compare_results([ok, bad], 'a', OBJ)
        self.assertEqual([r['run_id'] for r in cmp['ranking']], ['a'])
        self.assertIn('b', cmp['feasibility'])

    def test_9_constraints_before_ranking_and_zero_baseline(self):
        hi_impure = {'R': 0.0, 'C': 90.0, 'O': 110.0, 'D': 0.0}   # purity 0.45 -> infeasible
        lo_pure = {'R': 0.0, 'C': 40.0, 'O': 10.0, 'D': 0.0}      # purity 0.8
        base = {'R': 50.0, 'C': 0.0, 'O': 50.0, 'D': 0.0}         # zero target yield
        recs = [OC.compute_metrics(fake_result(i, e), OBJ, rows_for(e)) for i, e in (('base', base), ('hi', hi_impure), ('lo', lo_pure))]
        cmp = OC.compare_results(recs, 'base', OBJ)
        self.assertEqual(cmp['best_run_id'], 'lo')
        lo = next(c for c in cmp['paired_comparisons'] if c['run_id'] == 'lo')
        self.assertEqual(lo['target_cell_yield']['absolute_difference'], 40.0)
        self.assertIsNone(lo['target_cell_yield']['ratio'])
        self.assertIn('zero baseline', lo['target_cell_yield']['flag'])
        pareto = {p['run_id']: p['pareto_optimal'] for p in cmp['pareto_table']}
        self.assertEqual(pareto, {'base': False, 'hi': True, 'lo': True})

    def test_infeasible_reported_without_lowering_threshold(self):
        e = {'R': 0.0, 'C': 10.0, 'O': 90.0, 'D': 0.0}
        cmp = OC.compare_results([OC.compute_metrics(fake_result('x', e), OBJ, rows_for(e))], 'x', OBJ)
        self.assertEqual(cmp['status'], 'infeasible')

    def test_10_comparison_flags_unmatched_draws(self):
        e = {'R': 0.0, 'C': 9.0, 'O': 1.0, 'D': 0.0}
        a = OC.compute_metrics(fake_result('a', e), OBJ, rows_for(e))
        b = OC.compute_metrics(fake_result('b', e, draw='d1'), OBJ, rows_for(e))
        c = OC.compare_results([a, b], 'a', OBJ)['paired_comparisons'][0]
        self.assertFalse(c['matched_conditions'])
        with self.assertRaises(ValueError):
            OC.summarize_ensemble([(a, b)], OBJ, {'ensemble_id': 'x', 'seed': 1, 'parameters': {}})


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = load('campaign.synthetic.json')
        self.cfg.pop('uncertainty')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_11_full_campaign_result_dependent_and_cited(self):
        state = CAM.run_campaign(self.cfg, self.tmp / 'c', config_dir=EX)
        evals = [K.read_json(e['path']) for e in state['evaluations']]
        self.assertGreaterEqual(len(evals), 2)
        first = evals[0]['next_action']
        self.assertEqual(first['type'], 'run_experiment')
        lo, hi = first['numerical_basis']['bracket_h']
        tried = first['numerical_basis']['tried_expansion_h']
        proposed = [e['stage_durations_h']['ipsc_expansion'] for e in first['experiments']]
        self.assertTrue(all(lo < p < hi and p not in tried for p in proposed))  # refinements inside the bracket
        for ev in evals:
            K.require_valid('evaluation', ev)
            self.assertTrue(ev['next_action']['supporting_run_ids'])
            self.assertTrue(ev['next_action']['alternatives'])
            self.assertTrue(set(ev['next_action']['supporting_run_ids']) <= set(ev['result_ids']))
        self.assertIn(evals[-1]['next_action']['type'], ('request_calibration', 'stop_budget'))
        self.assertLessEqual(state['budget']['runs_used'], state['budget']['max_runs'])

    def test_11_next_action_changes_with_numbers(self):
        a = CAM.run_campaign(self.cfg, self.tmp / 'a', config_dir=EX)
        strict = copy.deepcopy(load('objective.json'))
        strict['constraints'][0]['value'] = 0.99
        (self.tmp / 'strict.json').write_text(json.dumps(strict))
        cfg = dict(self.cfg, objective=str(self.tmp / 'strict.json'))
        b = CAM.run_campaign(cfg, self.tmp / 'b', config_dir=EX)
        na_a = K.read_json(a['evaluations'][0]['path'])['next_action']
        na_b = K.read_json(b['evaluations'][0]['path'])['next_action']
        self.assertNotEqual((na_a['type'], na_a.get('experiments')), (na_b['type'], na_b.get('experiments')))
        self.assertEqual(K.read_json(b['evaluations'][0]['path'])['comparison']['status'], 'infeasible')

    def test_11_budget_exhaustion_stops_execution(self):
        cfg = dict(self.cfg, budget={'max_runs': 2, 'max_rounds': 3})
        d = self.tmp / 'small'
        CAM.init_campaign(cfg, d, config_dir=EX)
        prop = CAM.propose_experiments(d)
        self.assertEqual(len(prop['experiments']), 2)  # truncated from 3 to the budget
        for e in prop['experiments']:
            CAM.run_experiment(d, e)
        with self.assertRaises(CAM.BudgetExhausted):
            CAM.run_experiment(d, prop['experiments'][0])
        ev = CAM.evaluate_campaign(d)
        self.assertIn(ev['next_action']['type'], ('stop_budget', 'request_calibration'))

    def test_blocked_scenario_requests_evidence(self):
        cfg = load('campaign.evidence.json')
        state = CAM.run_campaign(cfg, self.tmp / 'ev', config_dir=EX)
        self.assertEqual(state['runs'], [])
        na = K.read_json(state['evaluations'][0]['path'])['next_action']
        self.assertEqual(na['type'], 'request_evidence')
        self.assertIn('cardiac_differentiation:differentiation_transition_rate', na['missing_parameter_ids'])
        self.assertTrue(na['suggested_queries'])

    def test_results_and_evaluations_are_immutable(self):
        d = self.tmp / 'imm'
        CAM.init_campaign(self.cfg, d, config_dir=EX)
        r = CAM.run_experiment(d, CAM.propose_experiments(d)['experiments'][0])
        path = d / 'runs' / r['run_id'] / 'result.json'
        with self.assertRaises(K.ContractError):
            K.write_json_atomic(path, {}, overwrite=False)
        with self.assertRaises(K.ContractError):
            CAM.init_campaign(self.cfg, d, config_dir=EX)

    def test_check_action_rejects_over_budget_or_invalid(self):
        d = self.tmp / 'chk'
        CAM.init_campaign(dict(self.cfg, budget={'max_runs': 4, 'max_rounds': 3}), d, config_dir=EX)
        for e in CAM.propose_experiments(d)['experiments']:
            CAM.run_experiment(d, e)
        action = CAM.evaluate_campaign(d)['next_action']
        self.assertEqual(CAM.check_action(d, action), [])
        bad = copy.deepcopy(action)
        bad['experiments'] = bad['experiments'] * 3
        self.assertTrue(CAM.check_action(d, bad))
        bad = copy.deepcopy(action)
        bad['experiments'][0]['stage_durations_h']['ipsc_expansion'] = 500.0
        self.assertTrue(CAM.check_action(d, bad))
        self.assertTrue(CAM.check_action(d, {'type': 'run_experiment', 'reason': 'x'}))

    def test_10_campaign_reproducible(self):
        a = CAM.run_campaign(self.cfg, self.tmp / 'r1', config_dir=EX)
        b = CAM.run_campaign(self.cfg, self.tmp / 'r2', config_dir=EX)
        strip = lambda s: [(r['stage_durations_h'], r['status'], r['experiment_sha256']) for r in s['runs']]
        self.assertEqual(strip(a), strip(b))
        ra = [K.read_json(r['result_path'])['endpoint_state'] for r in a['runs']]
        rb = [K.read_json(r['result_path'])['endpoint_state'] for r in b['runs']]
        self.assertEqual(ra, rb)


class EnsembleTests(unittest.TestCase):
    def test_10_paired_ensemble_semantics(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = load('campaign.synthetic.json')
            cfg['budget'] = {'max_runs': 3, 'max_rounds': 2}
            CAM.init_campaign(cfg, tmp / 'e', config_dir=EX)
            for e in CAM.propose_experiments(tmp / 'e')['experiments']:
                CAM.run_experiment(tmp / 'e', e)
            ev = CAM.evaluate_campaign(tmp / 'e')
            u = ev['uncertainty']
            self.assertEqual(u['n_draws'], 16)
            self.assertIn('not a confidence interval', u['semantics'].lower())
            self.assertEqual(ev['budget']['runs_used'], 3)  # ensemble/sensitivity runs are diagnostic only
            self.assertGreater(ev['budget']['diagnostic_simulations'], 32)
        finally:
            shutil.rmtree(tmp)


if __name__ == '__main__':
    unittest.main()
