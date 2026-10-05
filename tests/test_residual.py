"""Prediction residuals: where the loop closes, and where it only looks closed.

The property worth protecting is that a residual cannot flatter the model. The
tests are mostly refusals for that reason: a prediction written after the result,
a measurement that is really another prediction, a stand-in run counted as
evidence, and a single reading standing in for a replicated one.
"""
import unittest

from biosense import contracts as K
from biosense.evidence import estimates as E
from biosense.evidence import residual as R

BEFORE = '2026-10-01T09:00:00Z'
AFTER = '2026-10-04T16:00:00Z'
PRED = E.value(68.0, 'simulated', source_ref='sim:ipsc_monocyte_v1')
MEAS = E.value(64.9, 'measured', source_ref='RUN-014', n=3, sd=2.4)
IV = E.interval(62.0, 74.0, 'prediction_interval', level=0.9, method='parameter draws')


def commitment(**kw):
    d = dict(source='biosimulator', readout='monocyte_gate_pct', value=PRED, unit='%',
             model_id='ipsc_monocyte_v1', committed_at=BEFORE)
    d.update(kw)
    return R.commit(**d)


def measurement(**kw):
    d = dict(run_id='RUN-014', source='wet_lab', measured_at=AFTER, n=3, sd=2.4)
    d.update(kw)
    return R.measurement(**d)


def residual(**kw):
    rid = kw.pop('residual_id', 'RES-001')
    d = dict(commitment=commitment(), measurement_=measurement(), predicted=PRED, measured=MEAS,
             higher_is_better=True)
    d.update(kw)
    return R.residual(rid, 'monocyte_gate_pct', '%', **d)


class ContractTests(unittest.TestCase):
    def test_a_residual_validates_and_carries_both_sides(self):
        r = residual()
        self.assertEqual([], K.schema_errors('prediction_residual', r))
        self.assertEqual(68.0, r['predicted']['value'])
        self.assertEqual(64.9, r['measured']['value'])

    def test_the_gap_is_never_labelled_measured(self):
        """No instrument measured a gap. It takes the weaker of the two sides, so
        a real reading against a simulated prediction is simulated: what a reader
        needs to know is that a model is standing on one end of it."""
        r = residual()
        self.assertNotEqual('measured', r['residual']['estimate_type'])
        self.assertEqual('simulated', r['residual']['estimate_type'])

    def test_the_sign_reads_as_measured_minus_predicted(self):
        r = residual()
        self.assertAlmostEqual(64.9 - 68.0, r['residual']['absolute_change'], places=6)
        self.assertEqual('decrease', r['residual']['direction'])

    def test_percentages_subtract_into_percentage_points(self):
        self.assertEqual('percentage_points', residual()['residual']['change_unit'])


class ItMustNotFlatterTheModelTests(unittest.TestCase):
    def test_a_prediction_made_after_the_result_is_refused(self):
        """The failure mode the whole contract exists to prevent: a model fitted
        to a result and then congratulated for matching it."""
        with self.assertRaises(K.ContractError) as e:
            residual(commitment=commitment(committed_at='2026-10-05T10:00:00Z'))
        msg = str(e.exception)
        self.assertIn('not a prediction', msg)
        self.assertIn('told the answer', msg)

    def test_comparing_a_prediction_against_a_prediction_is_refused(self):
        with self.assertRaises(K.ContractError) as e:
            residual(measured=E.value(64.9, 'simulated'))
        self.assertIn('model talking to itself', str(e.exception))

    def test_a_target_cannot_stand_in_for_a_prediction(self):
        with self.assertRaises(K.ContractError) as e:
            residual(predicted=E.value(68.0, 'target'))
        self.assertIn('must be simulated or predicted', str(e.exception))

    def test_the_simulator_cannot_commit_to_a_readout_it_does_not_model(self):
        """Refused at commitment, before any run is done against it: a residual
        on an invented number would be reported as model performance."""
        for coverage in ('not_modelled', 'no_simulator'):
            with self.assertRaises(K.ContractError) as e:
                commitment(simulator_coverage=coverage)
            self.assertIn('would be invented', str(e.exception))

    def test_a_direction_without_a_magnitude_is_not_a_prediction(self):
        with self.assertRaises(K.ContractError) as e:
            commitment(value=E.value(None, 'simulated'))
        self.assertIn('nobody can be wrong about', str(e.exception))

    def test_editing_the_prediction_changes_its_hash(self):
        """What makes a quietly-revised prediction visible rather than deniable."""
        a = commitment()
        b = commitment(value=E.value(70.0, 'simulated'))
        self.assertNotEqual(a['values_sha256'], b['values_sha256'])
        self.assertEqual(a['values_sha256'], commitment()['values_sha256'])

    def test_the_interval_is_part_of_what_was_committed_to(self):
        self.assertNotEqual(commitment()['values_sha256'],
                            commitment(interval_=IV)['values_sha256'])


class WhatCountsAsModelEvidenceTests(unittest.TestCase):
    def test_a_replicated_wet_lab_run_against_a_modelled_readout_counts(self):
        r = residual(commitment=commitment(interval_=IV), interval_=IV)
        self.assertTrue(r['counts_as_model_evidence'])
        self.assertIn('committed to before the run', r['why_it_does_or_does_not'])

    def test_a_synthetic_standin_never_counts_and_says_so_twice(self):
        r = residual(measurement_=measurement(source='synthetic_standin'))
        self.assertFalse(r['counts_as_model_evidence'])
        self.assertIn('not biological evidence', r['why_it_does_or_does_not'])
        self.assertIn('SYNTHETIC STAND-IN: not biological evidence', r['limitations'])

    def test_an_unreplicated_measurement_does_not_count(self):
        """A gap of one reading cannot be told apart from the run being noisy."""
        for n in (None, 0, 1):
            r = residual(measurement_=measurement(n=n), residual_id=f'RES-n{n}')
            self.assertFalse(r['counts_as_model_evidence'])
            self.assertIn('variation', r['why_it_does_or_does_not'])

    def test_a_human_expectation_is_recorded_but_is_not_model_evidence(self):
        r = residual(commitment=commitment(source='human_expectation'))
        self.assertFalse(r['counts_as_model_evidence'])
        self.assertIn('not a model output', r['why_it_does_or_does_not'])

    def test_the_reason_always_travels_with_the_residual(self):
        for kw in ({}, {'measurement_': measurement(source='synthetic_standin')},
                   {'measurement_': measurement(n=1)}):
            r = residual(**kw)
            self.assertTrue(r['why_it_does_or_does_not'])
            if not r['counts_as_model_evidence']:
                self.assertIn(r['why_it_does_or_does_not'], r['limitations'])


class IntervalTests(unittest.TestCase):
    def test_a_measurement_inside_the_committed_interval(self):
        r = residual(commitment=commitment(interval_=IV), interval_=IV)
        self.assertIs(True, r['within_interval'])

    def test_a_measurement_outside_it(self):
        r = residual(commitment=commitment(interval_=IV), interval_=IV,
                     measured=E.value(80.0, 'measured', n=3))
        self.assertIs(False, r['within_interval'])
        self.assertIn('OUTSIDE', R.render(r))

    def test_without_an_interval_nothing_was_agreed_beforehand(self):
        """A point prediction with no interval cannot be wrong in a way anyone
        signed up to, and the residual says that rather than inventing a bound."""
        r = residual()
        self.assertIsNone(r['within_interval'])
        self.assertIn('nothing was agreed beforehand', r['why_it_does_or_does_not'])


class SummaryTests(unittest.TestCase):
    def test_an_empty_set_says_the_loop_has_not_closed(self):
        s = R.summarise([])
        self.assertEqual(0, s['n_residuals'])
        self.assertIn('has not closed', s['limitations'][0])

    def test_it_never_says_the_model_is_validated(self):
        s = R.summarise([residual(commitment=commitment(interval_=IV), interval_=IV)])
        text = s['statement'] + ' '.join(s['limitations'])
        self.assertNotIn('validated', text)
        self.assertIn('not a calibration', s['statement'])
        self.assertIn('never validation', ' '.join(s['limitations']))

    def test_residuals_that_do_not_bear_on_the_model_are_counted_separately(self):
        rs = [residual(measurement_=measurement(source='synthetic_standin')),
              residual(measurement_=measurement(n=1), residual_id='RES-2')]
        s = R.summarise(rs)
        self.assertEqual(2, s['n_residuals'])
        self.assertEqual(0, s['n_counting_as_evidence'])
        self.assertIn('none of which says anything about the model', s['statement'])
        self.assertIsNone(s['mean_absolute_residual'])

    def test_the_mean_gap_uses_only_the_residuals_that_count(self):
        good = residual(commitment=commitment(interval_=IV), interval_=IV)
        junk = residual(measurement_=measurement(source='synthetic_standin'),
                        measured=E.value(10.0, 'measured', n=3), residual_id='RES-junk')
        s = R.summarise([good, junk])
        self.assertEqual(1, s['n_counting_as_evidence'])
        self.assertAlmostEqual(abs(64.9 - 68.0), s['mean_absolute_residual'], places=6)


class RenderTests(unittest.TestCase):
    def test_it_states_both_numbers_their_gap_and_the_verdict(self):
        text = R.render(residual(commitment=commitment(interval_=IV), interval_=IV))
        for part in ('68', '64.9', '%', 'percentage_points', 'model evidence: yes', 'inside'):
            self.assertIn(part, text)


if __name__ == '__main__':
    unittest.main()
