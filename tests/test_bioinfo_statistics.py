"""The deterministic statistics, the public-dataset search, and the orchestrator
contract for a data-backed analysis request.

The statistics tests check arithmetic against values computed by hand or by an
independent route, and the refusals that keep a number from meaning more than it
should. The orchestrator tests check that an analysis request must name the
uncertainty it resolves and cannot spend an iteration.
"""
import unittest

import numpy as np
from scipy import stats as sps

from biosense import contracts as K
from biosense.bioinformatics import implications as IMP
from biosense.bioinformatics.toolkit import statistics as S
from biosense.data.sources import geo
from biosense.production import autonomy as AU
from biosense.production import orchestrator as OR


class FdrTests(unittest.TestCase):
    def test_bh_matches_the_textbook_calculation(self):
        p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205]
        q = S.benjamini_hochberg(p)
        m = len(p)
        expected, running = [], 1.0
        for i in range(m - 1, -1, -1):
            running = min(running, p[i] * m / (i + 1))
            expected.insert(0, running)
        for got, want in zip(q, expected):
            self.assertAlmostEqual(want, got, places=12)

    def test_q_values_are_monotone_and_never_below_p(self):
        p = [0.04, 0.01, 0.2, 0.001, 0.5]
        q = S.benjamini_hochberg(p)
        for pi, qi in zip(p, q):
            self.assertGreaterEqual(qi + 1e-12, pi)
        order = np.argsort(p)
        qs = [q[i] for i in order]
        self.assertEqual(qs, sorted(qs))

    def test_none_passes_through(self):
        self.assertEqual([None, None], S.benjamini_hochberg([None, None]))
        self.assertIsNone(S.benjamini_hochberg([0.5, None])[1])

    def test_a_single_test_is_its_own_q_value(self):
        self.assertAlmostEqual(0.03, S.benjamini_hochberg([0.03])[0])


class ComparisonTests(unittest.TestCase):
    def test_the_p_value_matches_scipy_directly(self):
        a, b = [41.0, 38.2, 44.1, 40.0], [68.0, 65.4, 71.2, 69.1]
        row = S.compare_groups('CD14', a, b)
        self.assertAlmostEqual(float(sps.ttest_ind(b, a, equal_var=False).pvalue),
                               row['p_value'], places=12)

    def test_the_effect_is_the_difference_in_the_readouts_own_units(self):
        row = S.compare_groups('x', [10, 10, 10, 10], [12, 12, 12, 12])
        self.assertAlmostEqual(2.0, row['effect'])
        self.assertEqual('difference', row['effect_type'])
        self.assertEqual('increase', row['direction'])

    def test_a_group_of_one_is_refused_rather_than_tested(self):
        row = S.compare_groups('x', [5.0], [9.0, 9.5, 9.1])
        self.assertIsNone(row['p_value'])
        self.assertEqual('none', row['test'])
        self.assertIn('no spread', row['note'])

    def test_two_constant_groups_produce_no_test(self):
        row = S.compare_groups('x', [3, 3, 3], [7, 7, 7])
        self.assertEqual('none', row['test'])
        self.assertIsNone(row['p_value'])
        self.assertAlmostEqual(4.0, row['effect'])

    def test_a_rank_test_is_not_run_below_its_resolution(self):
        row = S.compare_groups('x', [1, 2, 3], [4, 5, 6])
        self.assertNotIn('Mann-Whitney', row['test'])
        self.assertIn('at least 4 per group', row['note'])

    def test_a_rank_test_is_reported_alongside_when_n_allows(self):
        row = S.compare_groups('x', [1, 2, 3, 4], [5, 6, 7, 8])
        self.assertIn('Mann-Whitney', row['test'])

    def test_the_bootstrap_ci_is_reproducible_and_brackets_the_effect(self):
        a, b = [41.0, 38.2, 44.1, 40.0], [68.0, 65.4, 71.2, 69.1]
        r1 = S.compare_groups('x', a, b, seed=3)
        r2 = S.compare_groups('x', a, b, seed=3)
        self.assertEqual(r1['ci_low'], r2['ci_low'])
        self.assertLess(r1['ci_low'], r1['effect'])
        self.assertGreater(r1['ci_high'], r1['effect'])

    def test_a_paired_comparison_needs_equal_groups(self):
        row = S.compare_groups('x', [1, 2, 3], [4, 5], paired=True)
        self.assertIsNone(row['p_value'])
        self.assertIn('equal group sizes', row['note'])

    def test_a_log2_fold_change_on_a_non_positive_mean_is_refused(self):
        row = S.compare_groups('x', [0.0, 0.0, 0.0, 0.0], [2.0, 2.0, 2.0, 2.0],
                               effect_type='log2_fold_change')
        self.assertEqual('undefined', row['effect_type'])
        self.assertIn('positive means', row['note'])

    def test_fisher_exact_matches_scipy(self):
        row = S.fisher_counts('gate', a_pos=10, a_n=100, b_pos=30, b_n=100)
        self.assertAlmostEqual(float(sps.fisher_exact([[30, 70], [10, 90]])[1]), row['p_value'])
        self.assertAlmostEqual(0.2, row['effect'])

    def test_a_count_above_its_total_is_refused(self):
        with self.assertRaises(ValueError):
            S.fisher_counts('gate', a_pos=10, a_n=5, b_pos=1, b_n=5)

    def test_spearman_needs_three_paired_points(self):
        row = S.spearman('x', [1, 2], [3, 4])
        self.assertIsNone(row['p_value'])
        self.assertIn('at least 3', row['note'])

    def test_a_correlation_says_it_is_not_a_direction_to_move_a_parameter(self):
        row = S.spearman('x', [1, 2, 3, 4], [2, 4, 6, 9])
        self.assertIn('not a mechanism', row['note'])


class ImplicationTests(unittest.TestCase):
    def test_a_yield_readout_is_not_classified_as_a_viability_constraint(self):
        self.assertEqual('yield', IMP.readout_kind('viable_cells_e6')[0])
        self.assertEqual('viability', IMP.readout_kind('viability_pct')[0])

    def test_the_mapping_is_driven_by_the_declared_perturbation(self):
        self.assertEqual('mcsf_ng_ml', IMP.parameter_for_perturbation('M-CSF concentration')[0])
        self.assertEqual('agitation_rpm', IMP.parameter_for_perturbation('agitation rate')[0])
        self.assertEqual('il7_ng_ml', IMP.parameter_for_perturbation('IL-7 concentration')[0])

    def test_an_unmapped_perturbation_suggests_nothing_and_says_why(self):
        param, why = IMP.parameter_for_perturbation('lunar phase')
        self.assertIsNone(param)
        self.assertIn('inventing the link', why)

    def test_a_result_with_nothing_significant_implicates_no_direction(self):
        rows = [{'readout': 'CD14_pos_pct', 'q_value': 0.4, 'direction': 'increase'}]
        m = {'perturbation': 'M-CSF concentration', 'dataset_id': 'd',
             'evidence_class': 'private_user_dataset'}
        levers, why = IMP.candidate_parameters(rows, m, confidence='low')
        self.assertEqual([], levers)
        self.assertIn('implicates no direction', why)

    def test_only_a_constraint_readout_moving_does_not_argue_for_a_change(self):
        rows = [{'readout': 'viability_pct', 'q_value': 0.001, 'direction': 'decrease'}]
        m = {'perturbation': 'M-CSF concentration', 'dataset_id': 'd',
             'evidence_class': 'private_user_dataset'}
        levers, why = IMP.candidate_parameters(rows, m, confidence='moderate')
        self.assertEqual([], levers)
        self.assertIn('without arguing for pushing it', why)

    def test_readouts_moving_both_ways_produce_revisit_not_a_direction(self):
        rows = [{'readout': 'CD14_pos_pct', 'q_value': 0.001, 'direction': 'increase'},
                {'readout': 'CD16_pos_pct', 'q_value': 0.001, 'direction': 'decrease'}]
        m = {'perturbation': 'M-CSF concentration', 'dataset_id': 'd',
             'evidence_class': 'private_user_dataset'}
        levers, _ = IMP.candidate_parameters(rows, m, confidence='moderate')
        self.assertEqual('revisit', levers[0]['direction'])
        self.assertIn('does not point one way', levers[0]['basis'])


class PublicSearchTests(unittest.TestCase):
    def test_the_fixture_index_answers_offline(self):
        r = geo.search('BACH2 knockout T cell')
        self.assertFalse(r.live)
        self.assertTrue(r.candidates)
        self.assertIn('SYNTHETIC-GSE000102', [c.accession for c in r.candidates])

    def test_every_fixture_accession_is_marked_synthetic(self):
        """An invented accession presented as a real one would be a fabricated
        citation, so the fixture names itself."""
        for rec in geo.load_index():
            self.assertTrue(rec['accession'].startswith('SYNTHETIC-'), rec['accession'])

    def test_a_search_with_no_match_still_reports_what_it_would_have_asked(self):
        r = geo.search('zebrafish fin regeneration proteomics')
        self.assertEqual([], r.candidates)
        self.assertTrue(r.query_plan)
        self.assertIn('eutils.ncbi.nlm.nih.gov', r.query_plan[0]['url'])
        self.assertIn('never that GEO has none', r.note)

    def test_modality_and_organism_filters_exclude_rather_than_rank(self):
        r = geo.search('BACH2', modality='chip_seq')
        self.assertEqual({'chip_seq'}, {c.modality for c in r.candidates})
        r2 = geo.search('BACH2', organism='Mus musculus')
        self.assertEqual([], r2.candidates)

    def test_a_live_search_needs_two_explicit_flags(self):
        with self.assertRaises(K.ContractError) as e:
            geo.search('anything', live=True)
        self.assertIn('i_have_network_permission', str(e.exception))

    def test_an_empty_query_is_refused(self):
        with self.assertRaises(K.ContractError):
            geo.search('   ')

    def test_a_candidate_with_an_unknown_modality_is_refused(self):
        from biosense.data.sources.base import DatasetCandidate
        with self.assertRaises(K.ContractError):
            DatasetCandidate('X1', 't', 'metabolomics', 'Homo sapiens', 'geo', 'http://x')


class GateTests(unittest.TestCase):
    def test_datasets_are_off_unless_the_request_asks(self):
        g = AU.resolve({'bioreactor_source': 'synthetic_standin'})
        self.assertFalse(g['datasets_allowed'])
        self.assertFalse(g['private_data_allowed'])
        self.assertFalse(g['dataset_search_live'])

    def test_public_datasets_can_be_enabled_without_private_ones(self):
        g = AU.resolve({'bioreactor_source': 'synthetic_standin',
                        'bioinformatics': {'datasets': True}})
        self.assertTrue(g['datasets_allowed'])
        self.assertFalse(g['private_data_allowed'])
        self.assertIn('private user data is not read', AU.describe(g))

    def test_the_summary_says_search_is_offline_by_default(self):
        g = AU.resolve({'bioreactor_source': 'synthetic_standin',
                        'bioinformatics': {'datasets': True}})
        self.assertIn('offline', AU.describe(g))


class OrchestratorContractTests(unittest.TestCase):
    """A data-analysis request must name the uncertainty it resolves."""

    REPORT = {'analysis_id': 'a1',
              'diagnosis': [{'hypothesis_id': 'H01', 'statement': 'yield is limited'}]}

    def _decision(self, analysis):
        return {'type': 'request_bioinformatics', 'instruction': {'to': 'bioinformatics',
                                                                  'ask': 'x',
                                                                  'analysis': analysis}}

    def _full(self, **over):
        a = {'uncertainty_ref': {'kind': 'hypothesis', 'ref': 'H01', 'statement': 'yield limited'},
             'dataset_ids': ['d1'],
             'plan': {'uncertainty_ref': {'kind': 'hypothesis', 'ref': 'H01'},
                      'dataset_ids': ['d1']},
             'parameter_decision': 'whether to move il7_ng_ml'}
        a.update(over)
        return a

    def test_a_complete_request_passes(self):
        self.assertEqual([], OR.data_analysis_errors(
            self._decision(self._full()), self.REPORT, {}))

    def test_each_required_field_is_named_when_absent(self):
        for field in OR.DATA_ANALYSIS_FIELDS:
            errs = OR.data_analysis_errors(
                self._decision(self._full(**{field: None})), self.REPORT, {})
            self.assertTrue(any(field in e for e in errs), f'{field} not reported')

    def test_an_uncertainty_the_report_does_not_raise_is_refused(self):
        errs = OR.data_analysis_errors(self._decision(self._full(
            uncertainty_ref={'kind': 'hypothesis', 'ref': 'H99', 'statement': 'invented'})),
            self.REPORT, {})
        self.assertTrue(any('H99' in e and 'H01' in e for e in errs))

    def test_a_plan_without_an_uncertainty_is_refused(self):
        errs = OR.data_analysis_errors(self._decision(self._full(
            plan={'dataset_ids': ['d1']})), self.REPORT, {})
        self.assertTrue(any('uncertainty_ref' in e for e in errs))

    def test_datasets_that_disagree_with_the_plan_are_refused(self):
        errs = OR.data_analysis_errors(self._decision(self._full(dataset_ids=['other'])),
                                       self.REPORT, {})
        self.assertTrue(any('disagree' in e for e in errs))

    def test_an_annotation_request_is_untouched_by_the_new_rules(self):
        self.assertEqual([], OR.data_analysis_errors(
            {'type': 'request_bioinformatics', 'instruction': {'to': 'bioinformatics',
                                                               'ask': 'annotate BACH2'}},
            self.REPORT, {}))

    def test_an_analysis_request_never_advances_the_iteration(self):
        self.assertNotIn('request_bioinformatics', OR.ADVANCING)
        self.assertIn('request_bioinformatics', OR.INFO_ACTIONS)


if __name__ == '__main__':
    unittest.main()
