"""What came back from a round, recorded against the plan it ran and compared by code."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense import parameters as PR
from biosense import projects as PJ
from biosense.evidence import cli as EC
from biosense.evidence import round_plan as RP
from biosense.evidence import round_results as RR
from biosense.production import app as APP
from biosense.production import project_builder as PB

LEVER = 'gmcsf_maturation_ng_ml'


def a_plan():
    base = PJ.Project(PB.quick_build(project_id='my_macrophages', name='My iPSC macrophages',
                                     species='human', starting_cell='iPSC',
                                     target_cell='macrophage'))
    project, extra = PB.with_proposed_terms(base, copy.deepcopy(EC.TERM_TEMPLATE['terms']))
    return RP.build(copy.deepcopy(RP.TEMPLATE), project, extra_levers=extra)


class RoundResultsTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(PR.forget_local, LEVER)
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.plan = a_plan()
        (self.dir / 'round_plan.json').write_text(json.dumps(self.plan))

    def enter(self, arm, rep, value, readout='CD206+ fraction'):
        return {'arm_id': arm, 'replicate': rep, 'readout': readout, 'value': value}

    def test_only_what_the_plan_asked_for_can_be_recorded(self):
        with self.assertRaisesRegex(K.ContractError, 'not in the round plan'):
            RR.add(self.dir, [self.enter('A9', 1, 30)])
        with self.assertRaisesRegex(K.ContractError, 'readout'):
            RR.add(self.dir, [self.enter('A0', 1, 30, readout='IL-6')])
        with self.assertRaisesRegex(K.ContractError, 'outside 1..3'):
            RR.add(self.dir, [self.enter('A0', 4, 30)])
        with self.assertRaisesRegex(K.ContractError, 'source'):
            RR.add(self.dir, [self.enter('A0', 1, 30)], source='guess')
        self.assertIsNone(RR.load(self.dir), 'a refused entry writes nothing')

    def test_a_correction_replaces_the_value_and_keeps_the_old_one(self):
        RR.add(self.dir, [self.enter('A0', 1, 30)], entered_by='Dr A')
        doc = RR.add(self.dir, [self.enter('A0', 1, 31)], entered_by='Dr A')
        self.assertEqual([31.0], [e['value'] for e in doc['entries']])
        self.assertEqual([30.0], [e['value'] for e in doc['replaced']])
        self.assertEqual('scientist', doc['entries'][0]['source'])
        self.assertEqual(self.plan['commitment_sha256'], doc['plan_commitment_sha256'])

    def test_each_arm_is_compared_with_the_control_by_code(self):
        RR.add(self.dir, [self.enter('A0', r, v) for r, v in ((1, 30), (2, 32), (3, 31))]
               + [self.enter('A2', r, v) for r, v in ((1, 50), (2, 53), (3, 52))])
        out = RR.compare(self.plan, RR.load(self.dir))
        self.assertEqual('A0', out['control'])
        a2 = next(r for r in out['rows'] if r['arm_id'] == 'A2' and r['readout'] == 'CD206+ fraction')
        self.assertAlmostEqual((50 + 53 + 52) / 3 - 31, a2['effect'], places=6)
        self.assertIsNotNone(a2['p_value'])
        a1 = next(r for r in out['rows'] if r['arm_id'] == 'A1')
        self.assertIn('not measured', a1['note'])
        self.assertEqual(self.plan['decision_rules'], out['decision_rules'])

    def test_results_stay_tied_to_the_plan_they_ran(self):
        RR.add(self.dir, [self.enter('A0', 1, 30)])
        other = dict(self.plan, commitment_sha256='0' * 64)
        with self.assertRaisesRegex(K.ContractError, 'different round plan'):
            RR.compare(other, RR.load(self.dir))
        (self.dir / 'round_plan.json').write_text(json.dumps(other))
        with self.assertRaisesRegex(K.ContractError, 'plan changed'):
            RR.add(self.dir, [self.enter('A0', 2, 30)])


class ConnectedSeedTests(unittest.TestCase):
    def test_a_follow_up_keeps_each_round_in_its_own_folder(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        r1 = tmp / 'ai-1'
        r1.mkdir()
        (r1 / 'round_plan.json').write_text('{"round": 1}')
        (r1 / 'app_run.json').write_text('{}')
        r2 = tmp / 'ai-2'
        APP.DiscoveryRegistry._seed(r2, r1, connected_round=1)
        (r2 / 'round_plan.json').write_text('{"round": 2}')
        r3 = tmp / 'ai-3'
        APP.DiscoveryRegistry._seed(r3, r2, connected_round=2)
        self.assertEqual('{"round": 1}',
                         (r3 / 'connected' / 'round-1-ai-1' / 'round_plan.json').read_text())
        self.assertEqual('{"round": 2}',
                         (r3 / 'connected' / 'round-2-ai-2' / 'round_plan.json').read_text())
        self.assertFalse((r3 / 'round_plan.json').exists(), 'the new round starts empty')
        self.assertFalse((r3 / 'connected' / 'round-1-ai-1' / 'app_run.json').exists())
        self.assertFalse((r3 / 'connected' / 'round-2-ai-2' / 'connected').exists(),
                         'earlier rounds are flattened, not nested')


if __name__ == '__main__':
    unittest.main()
