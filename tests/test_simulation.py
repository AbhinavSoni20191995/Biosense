import math
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from biosense import simulation as SIM
from tests.helpers import experiment, scenario, zero_rates


class NumericalModelTests(unittest.TestCase):
    def run_sc(self, sc, exp_h=120.0, diff_h=216.0, **kw):
        r, rows = SIM.run_simulation(sc, experiment(sc, exp_h, diff_h), **kw)
        self.assertEqual(r['status'], 'completed', r['errors'])
        return r, rows

    def test_1_no_dynamics_preserves_populations(self):
        sc = zero_rates()
        r, rows = self.run_sc(sc)
        self.assertEqual(r['endpoint_state'], {'R': 1.2e7, 'C': 0.0, 'O': 0.0, 'D': 0.0})
        self.assertTrue(all(row['R'] == 1.2e7 for row in rows))

    def test_2_conversion_matches_analytic_and_conserves(self):
        k = 0.02
        sc = zero_rates(**{'cardiac_differentiation.k_C': k})
        r, rows = self.run_sc(sc, 24, 100)
        R0 = 1.2e7
        self.assertAlmostEqual(r['endpoint_state']['R'] / (R0 * math.exp(-k * 100)), 1, places=6)
        self.assertAlmostEqual(r['endpoint_state']['C'] / (R0 * (1 - math.exp(-k * 100))), 1, places=6)
        for row in rows:
            self.assertAlmostEqual((row['R'] + row['C'] + row['O'] + row['D']) / R0, 1, places=9)

    def test_3_death_matches_exponential_and_accounting(self):
        d = 0.01
        sc = zero_rates(**{'ipsc_expansion.delta_R': d, 'cardiac_differentiation.delta_R': d})
        r, _ = self.run_sc(sc, 50, 100)
        R0 = 1.2e7
        self.assertAlmostEqual(r['endpoint_state']['R'] / (R0 * math.exp(-d * 150)), 1, places=6)
        self.assertAlmostEqual((r['endpoint_state']['R'] + r['endpoint_state']['D']) / R0, 1, places=9)

    def test_4_stage_boundary_preserves_state_without_conversion(self):
        sc = scenario()
        r, rows = self.run_sc(sc)
        end_exp = [e for e in r['event_log'] if e['event'] == 'stage_end' and e['stage'] == 'ipsc_expansion'][0]
        start_dif = [e for e in r['event_log'] if e['event'] == 'stage_start' and e['stage'] == 'cardiac_differentiation'][0]
        self.assertEqual(end_exp['state_after'], start_dif['state_before'])
        self.assertFalse(start_dif['state_changed'])
        self.assertEqual(end_exp['state_after']['C'], 0.0)  # expansion has structural k_C = 0
        self.assertTrue(all(row['C'] == 0.0 for row in rows if row['stage'] == 'ipsc_expansion'))

    def test_logistic_growth_suppressed_at_capacity(self):
        sc = zero_rates(**{'ipsc_expansion.mu_R': 0.05})
        r, _ = self.run_sc(sc, 600, 100)
        self.assertLessEqual(r['endpoint_state']['R'], 3.0e8 * (1 + 1e-6))
        self.assertGreater(r['endpoint_state']['R'], 0.99 * 3.0e8)

    def test_8_solver_failure_is_propagated(self):
        sc = scenario()
        fail = SimpleNamespace(success=False, message='forced failure', t=np.array([0.0]), y=np.zeros((5, 1)), nfev=1, njev=0)
        with mock.patch.object(SIM, 'solve_ivp', return_value=fail):
            r, rows = SIM.run_simulation(sc, experiment(sc))
        self.assertEqual(r['status'], 'failed')
        self.assertIsNone(r['endpoint_state'])
        self.assertEqual(rows, [])

    def test_8_negative_or_nonfinite_states_fail(self):
        sc = scenario()
        t = np.linspace(0, 120, 121)
        y = np.ones((5, t.size)) * 1e6
        y[1, -1] = -5e3
        neg = SimpleNamespace(success=True, message='', t=t, y=y, nfev=1, njev=0)
        with mock.patch.object(SIM, 'solve_ivp', return_value=neg):
            r, _ = SIM.run_simulation(sc, experiment(sc))
        self.assertEqual(r['status'], 'failed')
        self.assertIn('negative', ' '.join(r['errors']))
        y2 = np.ones((5, t.size)); y2[0, 3] = np.nan
        with mock.patch.object(SIM, 'solve_ivp', return_value=SimpleNamespace(success=True, message='', t=t, y=y2, nfev=1, njev=0)):
            r, _ = SIM.run_simulation(sc, experiment(sc))
        self.assertEqual(r['status'], 'failed')

    def test_experiment_validation_rejects_bad_specs(self):
        sc = scenario()
        self.assertTrue(SIM.validate_experiment(sc, experiment(sc, -1, 10)))
        self.assertTrue(SIM.validate_experiment(sc, experiment(sc, 200, 200), max_total_h=336))
        bad = experiment(sc, parameter_overrides={'ipsc_expansion.not_a_rate': 1.0}, parameter_draw_id='d1')
        self.assertTrue(SIM.validate_experiment(sc, bad))
        unlabeled = experiment(sc, parameter_overrides={'ipsc_expansion.mu_R': 0.02})
        self.assertTrue(SIM.validate_experiment(sc, unlabeled))

    def test_unknown_scenario_parameter_refused(self):
        sc = scenario()
        extra = dict(sc['parameters'][0]); extra['id'] = 'ipsc_expansion.k_C'  # structural zero, not an input
        sc['parameters'].append(extra)
        self.assertEqual(SIM.validate_scenario(sc)['status'], 'invalid')

    def test_10_deterministic_and_hashed(self):
        sc = scenario()
        a, _ = SIM.run_simulation(sc, experiment(sc))
        b, _ = SIM.run_simulation(sc, experiment(sc))
        self.assertEqual(a['endpoint_state'], b['endpoint_state'])
        self.assertEqual(a['hashes']['scenario_sha256'], b['hashes']['scenario_sha256'])
        self.assertEqual(a['hashes']['experiment_sha256'], b['hashes']['experiment_sha256'])

    def test_10_seeded_draws_reproducible(self):
        sc = scenario()
        spec = {'ensemble_id': 'e', 'label': 'synthetic test', 'seed': 7, 'n_draws': 4,
                'parameters': {'cardiac_differentiation.k_C': {'distribution': 'uniform', 'low': 0.01, 'high': 0.02, 'unit': '1/h'}}}
        self.assertEqual(SIM.generate_draws(sc, spec), SIM.generate_draws(sc, spec))
        spec2 = dict(spec, seed=8)
        self.assertNotEqual(SIM.generate_draws(sc, spec), SIM.generate_draws(sc, spec2))
        unlabeled = dict(spec, label='ranges')
        with self.assertRaises(ValueError):
            SIM.generate_draws(sc, unlabeled)

    def test_output_sampling_does_not_change_endpoint(self):
        sc = scenario()
        a, _ = SIM.run_simulation(sc, experiment(sc), solver={'output_interval_h': 1.0})
        b, _ = SIM.run_simulation(sc, experiment(sc), solver={'output_interval_h': 6.0})
        for s in 'RCOD':
            self.assertLess(abs(a['endpoint_state'][s] - b['endpoint_state'][s]) / max(1.0, b['endpoint_state'][s]), 1e-6)

    def test_convergence_under_tighter_tolerance(self):
        sc = scenario()
        r = SIM.convergence_check(sc, experiment(sc))
        self.assertTrue(r['passed'], r)


if __name__ == '__main__':
    unittest.main()
