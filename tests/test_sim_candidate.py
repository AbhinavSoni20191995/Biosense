"""The candidate -> project-simulator handoff.

Phase 1's gap: a candidate called `mcsf_ng_ml` could not reach a knob called
`mcsf`, and the failure was silent. These tests pin the join and, more
importantly, the three refusals — because a handoff that quietly drops what it
cannot model is worse than the original bug.
"""
import unittest

from biosense import contracts as K
from biosense import projects as PJ
from biosense.evidence import estimates as E
from biosense.production import sim_candidate as SC

MAC, CART = 'ipsc_macrophage', 'cart_expansion'
CANDS = [{'parameter': 'mcsf_ng_ml', 'direction': 'increase'},
         {'parameter': 'temperature_c', 'direction': 'increase'},
         {'parameter': 'il7_ng_ml', 'direction': 'increase'}]


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.mac = PJ.load(MAC)
        self.cart = PJ.load(CART)

    def test_a_canonical_candidate_reaches_the_right_simulator_knob(self):
        """The join Phase 1 could not make."""
        h = SC.plan_handoff(self.mac, [{'parameter': 'mcsf_ng_ml', 'direction': 'increase'}])
        self.assertEqual(1, len(h['applied']))
        self.assertEqual('mcsf', h['applied'][0]['knob'])
        self.assertEqual([], h['skipped'])

    def test_an_alias_resolves_before_the_handoff(self):
        h = SC.plan_handoff(self.mac, [{'parameter': 'M-CSF', 'direction': 'increase'}])
        self.assertEqual('mcsf_ng_ml', h['applied'][0]['parameter_id'])

    def test_nothing_is_dropped_silently(self):
        h = SC.plan_handoff(self.mac, CANDS)
        self.assertEqual(len(CANDS), len(h['applied']) + len(h['skipped']))
        for row in h['skipped']:
            self.assertTrue(row['reason'])

    def test_a_not_modelled_parameter_is_reported_as_present_but_unpredictable(self):
        h = SC.plan_handoff(self.mac, CANDS)
        temp = next(r for r in h['skipped'] if r['parameter_id'] == 'temperature_c')
        self.assertEqual('not_modelled', temp['simulator_coverage'])
        self.assertIn('no term for it', temp['reason'])

    def test_a_parameter_the_project_does_not_have_says_so(self):
        h = SC.plan_handoff(self.mac, CANDS)
        il7 = next(r for r in h['skipped'] if r['parameter_id'] == 'il7_ng_ml')
        self.assertEqual('not_in_project', il7['simulator_coverage'])
        self.assertIn('not a knob of this process', il7['reason'])

    def test_a_project_with_no_model_borrows_nobody_elses(self):
        h = SC.plan_handoff(self.cart, [{'parameter': 'il7_ng_ml', 'direction': 'increase'}])
        self.assertEqual([], h['applied'])
        self.assertFalse(h['coverage']['has_model'])
        self.assertIn('none is borrowed', h['skipped'][0]['reason'])


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.mac = PJ.load(MAC)
        self.control = {'mcsf': self.mac.parameter('mcsf_ng_ml').default_value}
        self.cmp = SC.compare_conditions(
            self.mac, control=self.control, candidates=CANDS,
            candidate_values={'mcsf_ng_ml': 50})

    def test_every_predicted_number_is_labelled_simulated(self):
        self.assertTrue(self.cmp['effects'])
        for e in self.cmp['effects']:
            self.assertEqual('simulated', e['estimate_type'])
            self.assertEqual('simulated', e['baseline']['estimate_type'])
            self.assertEqual('simulated', e['candidate']['estimate_type'])
            self.assertEqual([], K.schema_errors('estimate', e))

    def test_a_percentage_readout_reports_percentage_points(self):
        gate = next(e for e in self.cmp['effects'] if e['metric'] == 'monocyte_gate_pct')
        self.assertEqual('percentage_points', gate['change_unit'])
        self.assertIsNotNone(gate['relative_change_pct'])

    def test_a_count_readout_reports_its_own_unit_and_a_percentage(self):
        y = next(e for e in self.cmp['effects'] if e['metric'] == 'harvest_total_e6_per_ml')
        self.assertEqual('1e6 cells/mL', y['change_unit'])
        self.assertGreater(y['relative_change_pct'], 0)

    def test_the_changed_parameter_is_reported_with_both_values(self):
        c = self.cmp['candidate']['changed'][0]
        self.assertEqual('mcsf_ng_ml', c['parameter_id'])
        self.assertEqual(25.0, c['from'])
        self.assertEqual(50.0, c['to'])
        self.assertEqual('ng/mL', c['unit'])

    def test_readouts_come_from_the_project(self):
        metrics = {e['metric'] for e in self.cmp['effects']}
        project_readouts = {r['readout_id'] for r in self.mac.readouts}
        self.assertTrue(metrics <= project_readouts)
        self.assertIn('monocyte_gate_pct', metrics)

    def test_every_effect_says_it_is_a_stand_in_not_a_measurement(self):
        for e in self.cmp['effects']:
            self.assertTrue(any('not a validated digital twin' in x for x in e['limitations']))

    def test_the_same_comparison_twice_gives_the_same_prediction(self):
        again = SC.compare_conditions(self.mac, control=self.control, candidates=CANDS,
                                      candidate_values={'mcsf_ng_ml': 50})
        self.assertEqual([e['absolute_change'] for e in self.cmp['effects']],
                         [e['absolute_change'] for e in again['effects']])

    def test_a_candidate_outside_the_projects_range_is_clamped_and_reported(self):
        c = SC.compare_conditions(self.mac, control=self.control, candidates=CANDS,
                                  candidate_values={'mcsf_ng_ml': 9000})
        self.assertTrue(c['clamped'])
        self.assertEqual(150.0, c['clamped'][0]['used'])
        self.assertTrue(c['clamped'][0]['why'])

    def test_the_model_identity_travels_with_the_prediction(self):
        self.assertEqual('ipsc_monocyte_v1', self.cmp['model']['model_id'])
        self.assertEqual('synthetic_demonstration', self.cmp['evidence_status'])
        for e in self.cmp['effects']:
            self.assertIn('ipsc_monocyte_v1', e['baseline']['source_ref'])


class NoPredictionTests(unittest.TestCase):
    """A project with no model produces no prediction, and says why."""

    def test_a_cart_comparison_returns_no_effects_and_an_explanation(self):
        cart = PJ.load(CART)
        c = SC.compare_conditions(cart, candidates=[{'parameter': 'il7_ng_ml',
                                                     'direction': 'increase'}],
                                  candidate_values={'il7_ng_ml': 20})
        self.assertEqual('none', c['prediction'])
        self.assertEqual([], c['effects'])
        self.assertIn('no mechanistic model', c['prediction_note'])
        self.assertIn('real experiment', c['prediction_note'])

    def test_a_candidate_set_that_reaches_nothing_says_so(self):
        mac = PJ.load(MAC)
        c = SC.compare_conditions(mac, candidates=[{'parameter': 'temperature_c',
                                                    'direction': 'increase'}],
                                  candidate_values={'temperature_c': 38})
        self.assertEqual('none', c['prediction'])
        self.assertIn('No candidate parameter reached the simulator', c['prediction_note'])
        self.assertEqual(1, len(c['handoff']['skipped']))

    def test_no_fabricated_il7_prediction_anywhere(self):
        """The review's named failure mode, asserted directly."""
        for pid in (MAC, CART):
            p = PJ.load(pid)
            c = SC.compare_conditions(p, candidates=[{'parameter': 'il7_ng_ml',
                                                      'direction': 'increase'}],
                                      candidate_values={'il7_ng_ml': 20})
            self.assertEqual([], c['effects'], pid)
            self.assertEqual('none', c['prediction'], pid)


class SignatureTests(unittest.TestCase):
    def test_the_phenotype_readout_is_computed_from_frames_not_invented(self):
        from biosense.production import sim_mode as SM
        r = SM.simulate({})
        sig = SC.enrich_signature(r)
        self.assertIn('monocyte_gate_pct', sig)
        panel = [f.get('cd14_pct') for f in r['frames'] if f.get('cd14_pct') is not None]
        self.assertEqual(round(float(panel[-1]), 3), sig['monocyte_gate_pct'])

    def test_every_signature_metric_a_project_names_has_a_unit(self):
        mac = PJ.load(MAC)
        for r in mac.readouts:
            if r.get('simulator_metric'):
                self.assertTrue(r['unit'], r['readout_id'])


if __name__ == '__main__':
    unittest.main()
