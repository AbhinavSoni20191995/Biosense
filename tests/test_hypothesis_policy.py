"""Hypothesis generation is not protocol adoption, and must not be gated like it.

BioSense exists to propose testable hypotheses and design the experiments that
settle them. During real-AI testing it repeatedly returned nothing at all for
reasonable questions — "improve neutrophil production" — because requirements
that belong to a much later claim were being applied to the first one.

Four claim levels, and the rule this file enforces:

    1 CANDIDATE   a plausible relationship worth testing. May rest on indirect
                  evidence and may carry no magnitude at all.
    2 QUANTIFIED  a magnitude from measurement or derivation.
    3 SIMULATED   an effect from a model that covers the parameter.
    4 ADOPTABLE   a parameter change eligible for a protocol. The strongest gates
                  live here, and only here.

**The requirements of level 4 must never suppress level 1.** Missing data,
refused analyses, absent simulator coverage and unestablished magnitudes lower
confidence and constrain wording. They do not make a hypothesis impossible.

The counterweight is equally explicit: none of this makes the system assert more
than it knows. A candidate hypothesis says *may*, carries `effect_estimate =
null` with a reason, and can never change a protocol.
"""
import unittest

from biosense import contracts as K
from biosense import projects as PJ
from biosense.evidence import estimates as E
from biosense.evidence import hypothesis as HY

UNC = {'kind': 'evidence_gap', 'ref': 'GAP-neutrophil-gcsf',
       'statement': 'Nothing in this project says whether G-CSF exposure during granulocytic '
                    'commitment limits mature neutrophil output.'}
NEXT = {'summary': 'Test several G-CSF levels while measuring viable cell yield, neutrophil '
                   'identity and maturation.',
        'test_points': [], 'measure': []}


def lit(summary='Published work supports the direction in a related differentiation system.'):
    return [HY.evidence_row('published_literature', 'supportive', summary,
                            ref='PMID:00000000', strength='moderate')]


class ClaimLevelTests(unittest.TestCase):
    """What kind of claim a set of effects adds up to — computed, never asserted."""

    def test_a_direction_with_no_magnitude_is_a_candidate(self):
        eff = [E.direction_only('neutrophil_yield', 'cells', 'increase', 'derived',
                                reason='no study gives a magnitude in this cell system')]
        level, why = HY.claim_level_of(eff, 'not_modelled')
        self.assertEqual('candidate', level)
        self.assertIn('magnitude', why)

    def test_a_measured_magnitude_is_quantified(self):
        eff = [E.estimate('yield', 'cells', E.value(100.0, 'measured'),
                          E.value(120.0, 'measured'), higher_is_better=True)]
        self.assertEqual('quantified', HY.claim_level_of(eff, 'not_modelled')[0])

    def test_a_modelled_simulated_effect_is_simulated(self):
        eff = [E.estimate('yield', 'cells', E.value(100.0, 'simulated'),
                          E.value(120.0, 'simulated'), higher_is_better=True)]
        self.assertEqual('simulated', HY.claim_level_of(eff, 'modelled')[0])

    def test_a_simulated_effect_without_coverage_is_not_a_simulated_claim(self):
        """Coverage is what makes a simulated number mean anything; without it the
        builder refuses the effect outright, and the level never claims it."""
        eff = [E.estimate('yield', 'cells', E.value(100.0, 'simulated'),
                          E.value(120.0, 'simulated'), higher_is_better=True)]
        self.assertNotEqual('simulated', HY.claim_level_of(eff, 'not_modelled')[0])


class CandidateHypothesisTests(unittest.TestCase):
    """The five cases the product asked for by name."""

    def setUp(self):
        self.project = PJ.load('ipsc_macrophage')

    def _build(self, effects, evidence, parameter_id='mcsf_ng_ml', **kw):
        return HY.hypothesis(
            'HYP-policy-01',
            'Increasing G-CSF during granulocytic commitment may increase mature neutrophil '
            'output.',
            project=self.project, uncertainty_ref=dict(UNC), parameter_id=parameter_id,
            direction='increase', effects=effects, evidence=evidence, next_experiment=NEXT,
            **kw)

    def test_1_literature_and_no_dataset_still_gives_a_candidate(self):
        h = self._build([E.direction_only('neutrophil_yield', 'cells', 'increase', 'derived',
                                          reason='no dataset in this system was executable')],
                        lit())
        self.assertEqual('candidate', h['claim_level'])
        self.assertFalse(h['expected_effects'][0]['magnitude_estimated'])
        self.assertIsNone(h['expected_effects'][0]['relative_change_pct'])
        self.assertEqual('low', h['confidence'])
        self.assertFalse(h['may_change_protocol'])
        self.assertTrue(h['next_experiment']['summary'])

    def test_2_a_failed_analysis_does_not_remove_the_hypothesis(self):
        h = self._build(
            [E.direction_only('neutrophil_yield', 'cells', 'increase', 'derived',
                              reason='the analysis refused: metadata join unsupported')],
            lit(), limitations=['ANALYSIS LIMITATION: metadata join unsupported for GSE155719'])
        self.assertEqual('candidate', h['claim_level'])
        self.assertTrue(any('metadata join' in l for l in h['limitations']))

    def test_3_an_analysis_result_with_no_simulator_is_quantified_and_says_not_modelled(self):
        pid = next(p for p in sorted(self.project.parameter_ids)
                   if self.project.coverage(p) == 'not_modelled')
        h = self._build([E.estimate('yield', 'cells', E.value(100.0, 'measured'),
                                    E.value(118.0, 'measured'), higher_is_better=True)],
                        lit(), parameter_id=pid)
        self.assertEqual('quantified', h['claim_level'])
        self.assertEqual('not_modelled', h['parameter']['simulator_coverage'])
        self.assertIn('no term for it', h['parameter']['coverage_note'])

    def test_4_coverage_plus_a_model_effect_is_a_simulated_claim(self):
        pid = next(iter(sorted(self.project.modelled_ids())))
        h = self._build([E.estimate('yield', 'cells', E.value(100.0, 'simulated'),
                                    E.value(120.0, 'simulated'), higher_is_better=True)],
                        lit(), parameter_id=pid)
        self.assertEqual('simulated', h['claim_level'])
        self.assertEqual('modelled', h['parameter']['simulator_coverage'])

    def test_5_with_no_effect_at_all_the_hypothesis_is_refused_and_says_why(self):
        """The one case where silence is right — and it is still explained."""
        with self.assertRaises(K.ContractError) as e:
            self._build([], lit())
        self.assertIn('direction_only', str(e.exception))

    def test_an_unregistered_lever_is_labelled_rather_than_discarded(self):
        """A vocabulary gap is not a scientific one. The hypothesis stands; the
        protocol door stays shut until the parameter is mapped."""
        h = self._build([E.direction_only('neutrophil_yield', 'cells', 'increase', 'derived',
                                          reason='magnitude not established')],
                        lit(), parameter_id='gcsf_ng_ml',
                        parameter_label='G-CSF concentration')
        self.assertFalse(h['parameter']['registered'])
        self.assertEqual('G-CSF concentration', h['parameter']['proposed_label'])
        self.assertIn('CANDIDATE PARAMETER — NOT YET REGISTERED',
                      h['parameter']['coverage_note'])
        self.assertFalse(h['may_change_protocol'])
        K.require_valid('quantified_hypothesis', h)

    def test_the_language_of_a_candidate_stays_hedged(self):
        h = self._build([E.direction_only('neutrophil_yield', 'cells', 'increase', 'derived',
                                          reason='magnitude not established')], lit())
        self.assertIn('may', h['statement'])
        for word in (' will ', 'proven', 'validated', 'optimal'):
            self.assertNotIn(word, h['statement'].lower())


class NeutrophilRegressionTests(unittest.TestCase):
    """The question that used to come back empty.

    It asserts no cytokine and no biological answer: only that when relevant
    evidence exists, no dataset is executable, no neutrophil simulator exists and
    no magnitude is available, BioSense still returns something a scientist can
    act on.
    """

    def test_a_neutrophil_objective_can_still_produce_something_testable(self):
        project = PJ.load('ipsc_macrophage')
        effects = [E.direction_only(
            'mature_neutrophil_yield', 'cells', 'increase', 'derived',
            reason='relevant evidence comes from a related differentiation system and gives no '
                   'magnitude for this process')]
        evidence = lit('Granulocytic commitment responds to the cytokine in related human '
                       'differentiation systems.')
        h = HY.hypothesis(
            'HYP-neutrophil-01',
            'Increasing the granulocytic commitment cytokine may increase mature neutrophil '
            'output; the magnitude is not established.',
            project=project, uncertainty_ref=dict(UNC), parameter_id='gcsf_ng_ml',
            parameter_label='G-CSF concentration', direction='increase',
            effects=effects, evidence=evidence, next_experiment=NEXT,
            limitations=['No neutrophil simulator exists in this project, so no prediction is '
                         'produced.',
                         'No executable dataset was found for this exact cell system.'])
        K.require_valid('quantified_hypothesis', h)
        # 1 main uncertainty, 2 evidence, 3 a candidate hypothesis, 4 the lever,
        # 5 a direction, 6 a magnitude or "not established", 7 confidence,
        # 8 limitations, 9 the next experiment.
        self.assertTrue(h['uncertainty_ref']['statement'])
        self.assertTrue(h['evidence'])
        self.assertEqual('candidate', h['claim_level'])
        self.assertTrue(h['parameter']['proposed_label'])
        self.assertEqual('increase', h['parameter']['direction'])
        self.assertFalse(h['expected_effects'][0]['magnitude_estimated'])
        self.assertTrue(h['expected_effects'][0]['withheld_reason'])
        self.assertIn(h['confidence'], ('low', 'moderate'))
        self.assertGreaterEqual(len(h['limitations']), 2)
        self.assertIn('Test several', h['next_experiment']['summary'])
        self.assertFalse(h['may_change_protocol'])


class WithholdingTests(unittest.TestCase):
    """When nothing is proposed, that is a decision and it is explained."""

    def test_the_deterministic_path_says_why_it_formed_no_hypothesis(self):
        from biosense.benchmark import runner as BR
        from biosense.production import discovery as DISC
        from biosense.production import discovery_runner as DRUN
        req = DISC.build(project_id='cart_expansion',
                         objective='Improve something with no evidence and no named lever at '
                                   'all in this request.',
                         runtime_mode='synthetic_demo')
        cfg = DRUN.synthetic_config(req)
        result = BR.run(cfg)
        if result['hypotheses']:
            self.skipTest('this configuration produced a hypothesis, which is also fine')
        self.assertTrue(result['hypothesis_withheld_reason'])
        self.assertIn('lever', result['hypothesis_withheld_reason'])
        K.require_valid('benchmark_result', result)

    def test_a_named_value_is_enough_to_make_a_lever(self):
        """Somebody saying "try this knob" is a lever, even with no analysis."""
        from biosense.benchmark import runner as BR
        from biosense.production import discovery as DISC
        from biosense.production import discovery_runner as DRUN
        req = DISC.build(project_id='ipsc_macrophage',
                         objective='Increase viable macrophage production while maintaining '
                                   'identity and viability.',
                         runtime_mode='synthetic_demo',
                         candidate_values={'mcsf_ng_ml': 50})
        result = BR.run(DRUN.synthetic_config(req))
        self.assertTrue(result['hypotheses'], result.get('hypothesis_withheld_reason'))
        self.assertIsNone(result['hypothesis_withheld_reason'])
        self.assertIn(result['hypotheses'][0]['claim_level'],
                      ('candidate', 'quantified', 'simulated'))


if __name__ == '__main__':
    unittest.main()


class BestGuessTests(unittest.TestCase):
    """A labelled best guess: a starting point when the evidence points at a size
    without measuring it. It must stay unmistakably a guess."""

    def guess(self, **kw):
        kw.setdefault('low', 2); kw.setdefault('high', 10)
        kw.setdefault('confidence', 'low')
        kw.setdefault('rationale', 'Two related-cell studies report a rise; none in this format.')
        return E.best_guess('cd14_fraction', '%', higher_is_better=True, **kw)

    def test_a_best_guess_is_a_range_with_a_confidence_and_stays_a_candidate(self):
        e = self.guess()
        K.require_valid('estimate', e)
        self.assertEqual('judgement', e['estimate_type'])
        self.assertEqual((2.0, 10.0, 'best_guess_range'),
                         (e['interval']['lower'], e['interval']['upper'], e['interval']['type']))
        self.assertEqual('increase', e['direction'])
        self.assertIsNone(e['absolute_change'], 'no central value is invented by averaging')
        self.assertEqual('candidate', HY.claim_level_of([e], 'not_modelled')[0])
        self.assertIn('best guess +2 to +10 percentage_points [JUDGEMENT · low confidence]'
                      .replace('percentage_points', 'percentage points'), E.render(e))

    def test_confidence_needs_the_references_to_carry_it(self):
        with self.assertRaisesRegex(K.ContractError, 'at least 2'):
            self.guess(confidence='high', evidence_refs=['PMID:1'])
        with self.assertRaisesRegex(K.ContractError, 'rationale'):
            self.guess(rationale=' ')
        with self.assertRaisesRegex(K.ContractError, 'outside its own range'):
            self.guess(central=20)
        self.assertEqual('high', self.guess(confidence='high',
                                            evidence_refs=['PMID:1', 'PMID:2'])['judgement']['confidence'])

    def test_a_guess_is_never_surer_than_the_hypothesis_evidence(self):
        e = self.guess(confidence='high', evidence_refs=['PMID:1', 'PMID:2'])
        h = HY.hypothesis('H01', 'Raising M-CSF may raise the CD14+ fraction.',
                          project=PJ.load('ipsc_macrophage'), uncertainty_ref=UNC,
                          parameter_id='mcsf_ng_ml', direction='increase', effects=[e],
                          evidence=lit(), next_experiment=NEXT)
        self.assertEqual('low', h['confidence'])
        self.assertEqual('low', h['expected_effects'][0]['judgement']['confidence'])
        self.assertEqual('candidate', h['claim_level'])

    def test_a_guess_weakens_anything_it_is_combined_with(self):
        self.assertEqual('judgement', E.combine_type('measured', 'judgement'))
