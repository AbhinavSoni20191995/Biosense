"""Quantified evidence: numbers that know where they came from, prose that cannot
invent one, scope that is not silently generalised, and expertise that stays
expertise.

The rule under test throughout: BioSense may not say "yield improves by 20%"
unless 20 is traceable to a measurement, a deterministic calculation, a
simulation or a stated model.
"""
import os
import shutil
import tempfile
import unittest

from biosense import contracts as K
from biosense import parameters as PR
from biosense import projects as PJ
from biosense.data import roots as DR
from biosense.evidence import context as CTX
from biosense.evidence import estimates as E
from biosense.evidence import expert as EK
from biosense.evidence import hypothesis as HY
from biosense.evidence import narrative as N

MAC, CART = 'ipsc_macrophage', 'cart_expansion'


def pct(metric, a, b, kind='measured', **kw):
    return E.estimate(metric, '%', E.value(a, kind), E.value(b, kind), **kw)


class EstimateArithmeticTests(unittest.TestCase):
    def test_percentages_subtract_into_percentage_points(self):
        e = pct('phenotype', 42, 68)
        self.assertEqual(26.0, e['absolute_change'])
        self.assertEqual('percentage_points', e['change_unit'])
        self.assertAlmostEqual(61.9, e['relative_change_pct'], places=1)

    def test_a_non_percentage_keeps_its_own_unit(self):
        e = E.estimate('yield', 'cells', E.value(125e6, 'simulated'),
                       E.value(148e6, 'simulated'))
        self.assertEqual('cells', e['change_unit'])
        self.assertAlmostEqual(18.4, e['relative_change_pct'], places=1)

    def test_fold_change_only_when_asked_and_only_when_defined(self):
        e = E.estimate('vcd', '1e6 cells/mL', E.value(6.5, 'measured'),
                       E.value(8.0, 'measured'), fold_change_ok=True)
        self.assertAlmostEqual(1.2308, e['fold_change'], places=3)
        z = E.estimate('vcd', '1e6 cells/mL', E.value(0.0, 'measured'),
                       E.value(8.0, 'measured'), fold_change_ok=True)
        self.assertIsNone(z['fold_change'])
        self.assertTrue(any('positive' in x for x in z['limitations']))

    def test_a_ratio_against_a_near_zero_baseline_is_withheld_with_a_reason(self):
        e = pct('x', 0.1, 5.0)
        self.assertIsNone(e['relative_change_pct'])
        self.assertIn('too small', e['relative_withheld_reason'])
        self.assertEqual(4.9, round(e['absolute_change'], 6))   # the difference still stands

    def test_a_zero_baseline_withholds_the_ratio(self):
        e = E.estimate('x', 'cells', E.value(0, 'measured'), E.value(10, 'measured'))
        self.assertIsNone(e['relative_change_pct'])
        self.assertIn('undefined', e['relative_withheld_reason'])

    def test_favourability_follows_the_projects_direction_not_the_sign(self):
        up = pct('viability', 94, 91, higher_is_better=True)
        self.assertEqual('decrease', up['direction'])
        self.assertFalse(up['favourable'])
        down = pct('exhaustion', 30, 20, higher_is_better=False)
        self.assertTrue(down['favourable'])


class EstimateTypeTests(unittest.TestCase):
    """estimate_type travels per number, and a change is never 'measured'."""

    def test_a_difference_between_measurements_is_derived(self):
        e = pct('phenotype', 42, 68)
        self.assertEqual('derived', e['estimate_type'])
        self.assertEqual('measured', e['baseline']['estimate_type'])
        self.assertEqual('measured', e['candidate']['estimate_type'])

    def test_a_difference_between_simulations_stays_simulated(self):
        e = E.estimate('yield', 'cells', E.value(1, 'simulated'), E.value(2, 'simulated'))
        self.assertEqual('simulated', e['estimate_type'])

    def test_the_weakest_input_decides(self):
        e = E.estimate('yield', 'cells', E.value(1, 'measured'), E.value(2, 'simulated'))
        self.assertEqual('simulated', e['estimate_type'])
        self.assertEqual('predicted', E.change_type('measured', 'predicted'))
        self.assertEqual('derived', E.change_type('measured', 'measured'))

    def test_a_statistics_row_splits_measured_means_from_a_derived_effect(self):
        row = {'readout': 'CD14_pos_pct', 'mean_a': 41.18, 'mean_b': 67.58, 'n_a': 3, 'n_b': 3,
               'sd_a': 2.0, 'sd_b': 3.0, 'ci_low': 18.0, 'ci_high': 34.0,
               'ci_method': 'bootstrap', 'p_value': 0.001, 'q_value': 0.00176,
               'direction': 'increase', 'test': "Welch's t-test", 'note': None}
        e = E.from_statistics_row(row, '%', source_ref='facs-1', higher_is_better=True)
        self.assertEqual('measured', e['baseline']['estimate_type'])
        self.assertEqual('derived', e['estimate_type'])
        self.assertEqual('confidence_interval', e['interval']['type'])
        self.assertTrue(any('BH-q' in x for x in e['limitations']))

    def test_a_non_significant_row_says_so_in_its_limitations(self):
        row = {'readout': 'x', 'mean_a': 1.0, 'mean_b': 1.1, 'n_a': 3, 'n_b': 3,
               'q_value': 0.4, 'p_value': 0.3, 'direction': 'increase', 'test': 't', 'note': None}
        e = E.from_statistics_row(row, '%', source_ref='d')
        self.assertTrue(any('does not survive' in x for x in e['limitations']))


class MagnitudeWithheldTests(unittest.TestCase):
    """'Direction predicted: increase. Magnitude: not yet estimated.' is a state."""

    def test_direction_only_is_a_valid_estimate(self):
        e = E.direction_only('yield', 'cells', 'increase', 'derived',
                             reason='no dose-response data to interpolate from')
        self.assertEqual([], K.schema_errors('estimate', e))
        self.assertFalse(e['magnitude_estimated'])
        self.assertIsNone(e['absolute_change'])
        self.assertIn('not yet estimated', E.render(e))

    def test_withholding_a_magnitude_requires_a_reason(self):
        with self.assertRaises(K.ContractError):
            E.direction_only('yield', 'cells', 'increase', 'derived', reason='  ')

    def test_a_missing_side_degrades_to_direction_only_rather_than_guessing(self):
        e = E.estimate('x', 'cells', E.value(None, 'measured'), E.value(5, 'measured'))
        self.assertFalse(e['magnitude_estimated'])


class ResearchContextTests(unittest.TestCase):
    def setUp(self):
        self.ctx = CTX.context(species=['human'], cell_types=['alveolar macrophage'],
                               states=['mature'], disease_context=['pulmonary fibrosis'],
                               exclude=['mouse-only evidence'], strictness='prefer')

    def test_the_context_validates(self):
        self.assertEqual([], K.schema_errors('research_context', self.ctx))

    def test_in_context_evidence_matches(self):
        a = CTX.assess(self.ctx, {'species': 'human', 'cell_type': 'alveolar macrophage',
                                  'state': 'mature', 'disease': 'pulmonary fibrosis'})
        self.assertEqual('in_context', a['match'])
        self.assertFalse(a['excluded'])

    def test_out_of_context_evidence_is_labelled_not_hidden(self):
        a = CTX.assess(self.ctx, {'species': 'mouse',
                                  'cell_type': 'bone-marrow-derived macrophage',
                                  'state': 'mature', 'disease': 'healthy'})
        self.assertEqual('context_mismatch', a['match'])
        self.assertFalse(a['excluded'])          # prefer, not strict
        self.assertIn('species', a['reason'])

    def test_strict_excludes_what_prefer_only_labels(self):
        strict = dict(self.ctx, strictness='strict')
        a = CTX.assess(strict, {'species': 'mouse', 'cell_type': 'macrophage'})
        self.assertTrue(a['excluded'])

    def test_open_restricts_nothing(self):
        a = CTX.assess(CTX.context(strictness='open'), {'species': 'mouse'})
        self.assertEqual('in_context', a['match'])

    def test_the_forbidden_generalisations_are_named(self):
        w = CTX.generalisation_warnings(self.ctx, {'species': 'mouse', 'cell_type': 'macrophage'})
        self.assertTrue(w)
        self.assertIn('not a human result', w[0])

    def test_a_field_the_evidence_does_not_state_is_unknown_not_matched(self):
        a = CTX.assess(self.ctx, {'species': 'human'})
        self.assertIn('cell_type', a['unknown'])

    def test_what_was_actually_searched_is_recorded(self):
        applied = CTX.record_application(self.ctx, searched=['geo'],
                                         query_terms=CTX.search_terms(self.ctx),
                                         unsatisfied=[{'restriction': 'pulmonary fibrosis',
                                                       'why': 'the index has no disease field'}])
        self.assertEqual([], K.schema_errors('research_context', applied))
        self.assertIn('human', applied['applied']['query_terms'])
        self.assertEqual(1, len(applied['applied']['unsatisfied']))


class ExpertKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get(DR.PRIVATE_ENV)
        os.environ[DR.PRIVATE_ENV] = self.tmp
        self.k = EK.knowledge(
            'EK-001', 'Agitation above 120 rpm has previously reduced viability after day 8.',
            'prior_internal_experiment',
            scope={'project_id': MAC, 'cell_type': 'iPSC-derived macrophage', 'stage': 'myeloid',
                   'cell_line': None, 'donor': None, 'equipment': None},
            parameter_claims=[{'parameter_id': 'agitation_rpm', 'claim': 'avoid_above',
                               'value': 120, 'consequence': 'viability fell after day 8'}])

    def tearDown(self):
        if self._old is None:
            os.environ.pop(DR.PRIVATE_ENV, None)
        else:
            os.environ[DR.PRIVATE_ENV] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_expert_knowledge_is_private_and_never_citable(self):
        self.assertEqual([], K.schema_errors('expert_knowledge', self.k))
        self.assertEqual('private', self.k['visibility'])
        self.assertEqual('expert_knowledge', self.k['evidence_class'])
        self.assertIs(False, self.k['citable'])
        self.assertIs(False, self.k['may_set_protocol_value'])

    def test_it_is_stored_under_the_private_root(self):
        path = EK.save(self.k)
        self.assertTrue(DR.is_private_path(path))
        self.assertEqual(['EK-001'], [x['knowledge_id'] for x in EK.load_all()])

    def test_an_opinion_cannot_claim_the_confidence_of_an_experiment(self):
        with self.assertRaises(K.ContractError) as e:
            EK.knowledge('EK-9', 'I think more M-CSF helps.', 'expert_judgement',
                         confidence='high')
        self.assertIn('cap', str(e.exception))

    def test_it_narrows_a_search_range_and_says_who_narrowed_it(self):
        lo, hi, notes = EK.narrow_range('agitation_rpm', 20, 140, [self.k])
        self.assertEqual((20.0, 120.0), (lo, hi))
        self.assertTrue(any('EK-001' in n for n in notes))

    def test_a_claim_in_the_wrong_unit_is_refused(self):
        with self.assertRaises(K.ContractError) as e:
            EK.knowledge('EK-8', 'Something about M-CSF concentration here.',
                         'unpublished_observation',
                         parameter_claims=[{'parameter_id': 'mcsf_ng_ml', 'claim': 'avoid_above',
                                            'value': 100, 'unit': 'ug/mL'}])
        self.assertIn('ng/mL', str(e.exception))

    def test_conflicting_knowledge_keeps_the_range_and_reports_the_conflict(self):
        other = EK.knowledge('EK-002', 'Agitation below 130 rpm gives poor mixing here.',
                             'unpublished_observation',
                             parameter_claims=[{'parameter_id': 'agitation_rpm',
                                                'claim': 'avoid_below', 'value': 130}])
        lo, hi, notes = EK.narrow_range('agitation_rpm', 20, 140, [self.k, other])
        self.assertEqual((20.0, 140.0), (lo, hi))
        self.assertTrue(any('narrowed this range to nothing' in n for n in notes))

    def test_a_parameter_claim_is_stored_canonically(self):
        k = EK.knowledge('EK-003', 'Our line goes adherent above 100 ng/mL M-CSF.',
                         'unpublished_observation',
                         parameter_claims=[{'parameter_id': 'M-CSF', 'claim': 'avoid_above',
                                            'value': 100}])
        self.assertEqual('mcsf_ng_ml', k['parameter_claims'][0]['parameter_id'])


class QuantifiedHypothesisTests(unittest.TestCase):
    def setUp(self):
        self.mac = PJ.load(MAC)
        self.cart = PJ.load(CART)
        self.unc = {'kind': 'evidence_gap', 'ref': 'GAP-mcsf',
                    'statement': 'The optimal M-CSF concentration for this process is unknown.'}
        self.ev = [HY.evidence_row('private_user_dataset', 'supportive',
                                   'FACS shows the target population rising', strength='strong',
                                   visibility='private'),
                   HY.evidence_row('published_literature', 'supportive',
                                   'M-CSF supports myeloid commitment', strength='moderate')]
        self.next = {'summary': 'Test 75, 90 and 100 ng/mL M-CSF with viability and phenotype '
                                'measured at harvest.'}

    def build(self, **kw):
        kw.setdefault('effects', [pct('monocyte_gate_pct', 42, 68, higher_is_better=True),
                                  pct('final_viability_pct', 94, 91, higher_is_better=True)])
        kw.setdefault('project', self.mac)
        kw.setdefault('parameter_id', 'mcsf_ng_ml')
        kw.setdefault('direction', 'increase')
        kw.setdefault('evidence', self.ev)
        kw.setdefault('next_experiment', self.next)
        kw.setdefault('uncertainty_ref', self.unc)
        return HY.hypothesis('HYP-001', 'Raising M-CSF may improve macrophage output.', **kw)

    def test_a_hypothesis_validates_and_cannot_change_a_protocol(self):
        h = self.build()
        self.assertEqual([], K.schema_errors('quantified_hypothesis', h))
        self.assertIs(False, h['may_change_protocol'])
        for forbidden in ('revision_brief', 'protocol', 'route_to'):
            self.assertNotIn(forbidden, h)

    def test_it_carries_several_outcomes_so_a_trade_off_is_structural(self):
        h = self.build()
        self.assertEqual(2, len(h['expected_effects']))
        t = h['trade_offs'][0]
        self.assertTrue(t['gains'])
        self.assertTrue(t['costs'])
        self.assertIn('trade-off', t['summary'])

    def test_the_parameter_is_canonical_and_carries_project_coverage(self):
        h = self.build()
        self.assertEqual('mcsf_ng_ml', h['parameter']['parameter_id'])
        self.assertEqual('modelled', h['parameter']['simulator_coverage'])
        self.assertEqual('ng/mL', h['parameter']['unit'])

    def test_evidence_stays_broken_out_by_class(self):
        h = self.build()
        classes = [e['evidence_class'] for e in HY.evidence_table(h)]
        self.assertIn('private_user_dataset', classes)
        self.assertIn('published_literature', classes)
        self.assertTrue(h['confidence_basis'])

    def test_confidence_is_capped_without_a_real_measurement(self):
        h = self.build()
        self.assertIn(h['confidence'], ('low', 'moderate'))
        self.assertTrue(any('real experimental measurement' in b for b in h['confidence_basis']))

    def test_contradicting_evidence_lowers_confidence_and_stays_visible(self):
        ev = self.ev + [HY.evidence_row('expert_knowledge', 'contradicting',
                                        'our line goes adherent above 100 ng/mL',
                                        visibility='private', ref='EK-003')]
        h = self.build(evidence=ev)
        self.assertEqual('low', h['confidence'])
        self.assertTrue(any(e['stance'] == 'contradicting' for e in h['evidence']))

    def test_a_not_modelled_parameter_still_yields_a_hypothesis(self):
        h = HY.hypothesis('HYP-T', 'Raising temperature may change output.',
                          project=self.mac, parameter_id='temperature_c', direction='increase',
                          effects=[E.direction_only('harvest_total_e6_per_ml', '1e6 cells/mL',
                                                    'increase', 'derived',
                                                    reason='no data relates temperature to yield here')],
                          evidence=self.ev, next_experiment=self.next, uncertainty_ref=self.unc)
        self.assertEqual('not_modelled', h['parameter']['simulator_coverage'])
        self.assertIn('no term for it', h['parameter']['coverage_note'])

    def test_a_simulated_effect_on_an_uncovered_parameter_is_refused(self):
        """The failure mode the whole coverage machinery exists to stop."""
        with self.assertRaises(K.ContractError) as e:
            HY.hypothesis('HYP-X', 'IL-7 will raise CAR-T yield.',
                          project=self.cart, parameter_id='il7_ng_ml', direction='increase',
                          effects=[E.estimate('viable_cells_total', 'cells',
                                              E.value(1e8, 'simulated'),
                                              E.value(1.2e8, 'simulated'))],
                          evidence=self.ev, next_experiment=self.next,
                          uncertainty_ref=self.unc)
        self.assertIn('would be invented', str(e.exception))

    def test_a_cart_hypothesis_reports_no_simulator_rather_than_borrowing_one(self):
        h = HY.hypothesis('HYP-C', 'More IL-7 may improve CAR-T expansion.',
                          project=self.cart, parameter_id='il7_ng_ml', direction='increase',
                          effects=[E.direction_only('viable_cells_total', 'cells', 'increase',
                                                    'derived',
                                                    reason='BioSense has no model of this process')],
                          evidence=self.ev, next_experiment=self.next, uncertainty_ref=self.unc)
        self.assertEqual('no_simulator', h['parameter']['simulator_coverage'])
        self.assertIn('no mechanistic model', h['parameter']['coverage_note'])

    def test_a_parameter_the_project_does_not_have_is_reported_as_such(self):
        h = HY.hypothesis('HYP-N', 'IL-7 is not a knob of this project.',
                          project=self.mac, parameter_id='il7_ng_ml', direction='increase',
                          effects=[E.direction_only('x', 'cells', 'unknown', 'derived',
                                                    reason='not applicable')],
                          evidence=self.ev, next_experiment=self.next, uncertainty_ref=self.unc)
        self.assertEqual('not_in_project', h['parameter']['simulator_coverage'])

    def test_a_hypothesis_still_requires_an_uncertainty(self):
        with self.assertRaises(K.ContractError):
            self.build(uncertainty_ref={'kind': 'evidence_gap'})


class NarrativeTests(unittest.TestCase):
    def setUp(self):
        self.e = pct('monocyte_gate_pct', 42, 68, higher_is_better=True)
        self.e['label'] = 'Desired macrophage population'
        self.facts = N.facts_from_estimate(self.e)

    def test_a_number_in_the_prose_must_come_from_a_fact(self):
        self.assertEqual([], N.validate(
            'The population rose from 42% to 68%, a gain of 26 percentage points.', self.facts))

    def test_an_invented_number_is_caught(self):
        problems = N.validate('That is roughly a 40% improvement in yield.', self.facts)
        self.assertTrue(problems)
        self.assertIn('40', problems[0])

    def test_an_unsupported_significance_claim_is_caught(self):
        """An effect size is not evidence of significance. A relative change
        among the facts must not license the word."""
        problems = N.validate('The difference was significant.', self.facts)
        self.assertTrue(any('significance' in p for p in problems), problems)

    def test_a_significance_claim_is_allowed_when_a_q_value_is_a_fact(self):
        row = {'readout': 'CD14_pos_pct', 'mean_a': 41.18, 'mean_b': 67.58, 'n_a': 3, 'n_b': 3,
               'q_value': 0.00176, 'p_value': 0.001, 'direction': 'increase',
               'test': "Welch's t-test", 'note': None}
        e = E.from_statistics_row(row, '%', source_ref='facs-1')
        facts = N.facts_from_estimate(e)
        self.assertTrue(any(f.fact_id.endswith('.q_value') for f in facts))
        self.assertEqual([], N.validate(
            'The increase was significant at BH-q = 0.00176.', facts))

    def test_a_rejected_rewrite_falls_back_to_the_deterministic_sentence(self):
        fallback = N.say_estimate(self.e)
        text, why = N.accept('A 40% jump in output.', self.facts, fallback)
        self.assertEqual(fallback, text)
        self.assertTrue(why)

    def test_an_acceptable_rewrite_is_used(self):
        good = 'Under the higher condition the population reached 68%, up 26 points from 42%.'
        text, why = N.accept(good, self.facts, 'fallback')
        self.assertEqual(good, text)
        self.assertEqual([], why)

    def test_the_deterministic_sentence_validates_against_its_own_facts(self):
        self.assertEqual([], N.validate(N.say_estimate(self.e), self.facts))

    def test_a_withheld_magnitude_is_said_in_words(self):
        d = E.direction_only('yield', 'cells', 'increase', 'derived',
                             reason='no dose-response data')
        said = N.say_estimate(d)
        self.assertIn('not yet estimated', said)
        self.assertEqual([], N.validate(said, N.facts_from_estimate(d)))

    def test_a_hypothesis_paragraph_invents_nothing(self):
        mac = PJ.load(MAC)
        h = HY.hypothesis(
            'HYP-001', 'Raising M-CSF may improve macrophage output.', project=mac,
            parameter_id='mcsf_ng_ml', direction='increase', current_value=50,
            candidate_value=90,
            effects=[self.e, pct('final_viability_pct', 94, 91, higher_is_better=True)],
            evidence=[HY.evidence_row('private_user_dataset', 'supportive', 'FACS',
                                      strength='strong', visibility='private')],
            next_experiment={'summary': 'Test a narrower range.'},
            uncertainty_ref={'kind': 'evidence_gap', 'ref': 'GAP',
                             'statement': 'The optimal dose is unknown.'})
        text = N.say_hypothesis(h)
        self.assertEqual([], N.validate(text, N.facts_from_hypothesis(h)))
        self.assertIn('does not change the protocol', text)


if __name__ == '__main__':
    unittest.main()
