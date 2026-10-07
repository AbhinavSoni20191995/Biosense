"""What a run could not settle becomes the first round at the bench.

The plan is checked against the project, keeps one control beside every change,
and fixes what the next run does with each outcome before any result exists.
"""
import copy
import unittest

from biosense import contracts as K
from biosense import parameters as PR
from biosense import projects as PJ
from biosense.evidence import cli as EC
from biosense.evidence import round_plan as RP
from biosense.production import project_builder as PB

LEVER = 'gmcsf_maturation_ng_ml'


class RoundPlanTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(PR.forget_local, LEVER)
        base = PJ.Project(PB.quick_build(project_id='my_macrophages', name='My iPSC macrophages',
                                         species='human', starting_cell='iPSC',
                                         target_cell='macrophage'))
        self.project, self.extra = PB.with_proposed_terms(
            base, copy.deepcopy(EC.TERM_TEMPLATE['terms']), run_id='abc123')
        self.draft = copy.deepcopy(RP.TEMPLATE)

    def build(self):
        return RP.build(self.draft, self.project, run_id='abc123', extra_levers=self.extra)

    def test_the_template_is_a_valid_plan_with_a_fixed_commitment(self):
        plan = self.build()
        self.assertEqual(('round_plan', 1, 3), (plan['kind'], plan['round'], len(plan['arms'])))
        self.assertEqual(['A0'], [a['arm_id'] for a in plan['arms'] if a['control']])
        self.assertTrue(all(a['provenance'] == 'design_choice' for a in plan['arms']))
        self.assertIn('nothing here actuates', plan['status'])
        self.assertEqual(plan['commitment_sha256'], self.build()['commitment_sha256'])
        self.draft['decision_rules'][0]['then'] = 'something else, decided later'
        self.assertNotEqual(plan['commitment_sha256'], self.build()['commitment_sha256'],
                            'a rewritten rule is visible')

    def test_every_change_is_read_against_exactly_one_control(self):
        self.draft['arms'][0]['control'] = False
        self.draft['arms'][0]['setpoints'] = {LEVER: 5}
        self.draft['arms'][0]['basis'] = 'a low dose'
        with self.assertRaisesRegex(K.ContractError, 'exactly one arm is the control'):
            self.build()
        self.draft = copy.deepcopy(RP.TEMPLATE)
        self.draft['arms'][1]['setpoints'] = {}
        with self.assertRaisesRegex(K.ContractError, 'changes nothing'):
            self.build()

    def test_values_stay_inside_the_project_and_on_known_levers(self):
        self.draft['arms'][2]['setpoints'] = {LEVER: 500}
        with self.assertRaisesRegex(K.ContractError, 'outside the project'):
            self.build()
        self.draft = copy.deepcopy(RP.TEMPLATE)
        self.draft['arms'][2]['setpoints'] = {'il99_ng_ml': 5}
        with self.assertRaisesRegex(K.ContractError, 'not a parameter of'):
            self.build()

    def test_each_unknown_says_what_the_next_run_does_and_something_is_measured(self):
        self.draft['decision_rules'] = []
        with self.assertRaisesRegex(K.ContractError, 'no decision rule for U1'):
            self.build()
        self.draft = copy.deepcopy(RP.TEMPLATE)
        self.draft['readouts'] = []
        with self.assertRaisesRegex(K.ContractError, 'what to measure'):
            self.build()

    def test_one_replicate_is_allowed_and_says_what_it_cannot_show(self):
        self.draft['replicates'] = 1
        self.assertIn('directional only', self.build()['limitations'][0])


if __name__ == '__main__':
    unittest.main()


class FallbackPlanTests(unittest.TestCase):
    """When the agents write no round plan, BioSense assembles one, labelled."""

    def setUp(self):
        self.addCleanup(PR.forget_local, 'csf2_ng_ml')

    def hyps(self):
        return [
            {'hypothesis_id': 'H01', 'status': 'proposed',
             'statement': 'Raising M-CSF may raise monocyte output.',
             'parameter': {'parameter_id': 'mcsf_ng_ml', 'candidate_value': 50,
                           'direction': 'increase', 'unit': 'ng/mL'}},
            {'hypothesis_id': 'H02', 'status': 'proposed',
             'statement': 'GM-CSF in maturation may give alveolar identity.',
             'parameter': {'parameter_id': 'csf2_ng_ml', 'candidate_value': 100,
                           'proposed_label': 'GM-CSF (maturation)', 'unit': 'ng/mL'}},
            {'hypothesis_id': 'H03', 'status': 'contradicted', 'statement': 'Ruled out here.',
             'parameter': {'parameter_id': 'il3_ng_ml', 'candidate_value': 5}},
            {'hypothesis_id': 'H04', 'status': 'proposed', 'statement': 'A direction only.',
             'parameter': {'parameter_id': 'il3_ng_ml', 'direction': 'increase'}},
        ]

    def test_one_arm_per_live_lever_against_the_current_process(self):
        plan = RP.from_hypotheses(PJ.load('ipsc_macrophage'), self.hyps(), run_id='ai-x')
        self.assertEqual('biosense', plan['assembled_by'])
        self.assertIn('ASSEMBLED BY BIOSENSE', plan['status'])
        self.assertEqual([{}, {'mcsf_ng_ml': 50}, {'csf2_ng_ml': 100}],
                         [a['setpoints'] for a in plan['arms']],
                         'contradicted and direction-only hypotheses get no arm')
        self.assertTrue(plan['arms'][0]['control'])
        self.assertEqual(4, len(plan['decision_rules']))

    def test_the_runner_writes_it_only_when_the_agents_did_not(self):
        import json
        import shutil
        import tempfile
        from pathlib import Path
        from biosense.production import discovery_runner as DRUN
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        project = PJ.load('ipsc_macrophage')
        self.assertIsNotNone(DRUN.ensure_round_plan(d, project, self.hyps()))
        self.assertEqual('biosense', json.loads((d / 'round_plan.json').read_text())['assembled_by'])
        (d / 'round_plan.json').write_text('{"kind": "round_plan", "round": 1}')
        self.assertIsNone(DRUN.ensure_round_plan(d, project, self.hyps()), 'never overwritten')
        self.assertIsNone(RP.from_hypotheses(project, self.hyps()[2:]), 'nothing to test')
