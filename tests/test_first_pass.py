"""The answer as a person acts on it: one recommendation, the plan stage by
stage with the inducers each stage gets, then every other hypothesis.

The plan is the protocol laid out differently, never a new protocol: values come
from the protocol, a lever a live hypothesis named that the project lacks is
NEW with that hypothesis's own dose or none, and a ruled-out one adds nothing.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.evidence import cli as EVCLI
from biosense.production import discovery as DISC
from biosense.production import discovery_runner as DR
from biosense.production import first_pass as FP

CNTF = 'CNTF concentration in maturation medium (ng/mL) - CANDIDATE PARAMETER, not registered'


def finished(tmp, *hypotheses):
    req = DISC.build(project_id='ipsc_macrophage', objective='More macrophages per input iPSC.',
                     runtime_mode='synthetic_demo')
    for h in hypotheses:
        draft = dict(EVCLI.TEMPLATE, effects=EVCLI.TEMPLATE['effects'][:1], **h)
        K.write_json_atomic(tmp / f'quantified_hypothesis_{draft["hypothesis_id"]}.json',
                            EVCLI.build_hypothesis(draft, project_id='ipsc_macrophage'))
    return DR.finish(req, tmp, runtime_mode='synthetic_demo')['protocol']


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_each_stage_lists_its_inducers_and_physical_settings_sit_apart(self):
        p = finished(self.tmp, {'candidate_value': 50},
                     {'hypothesis_id': 'H02', 'parameter_id': 'cntf_ng_ml',
                      'parameter_label': CNTF, 'stage': 'myeloid', 'candidate_value': 20})
        fp = p['first_pass']
        stages = {s['stage_id']: s for s in fp['stages']}
        myeloid = {i['parameter_id']: i for i in stages['myeloid']['inducers']}
        self.assertEqual(('raise', 50, 25), (myeloid['mcsf_ng_ml']['action'],
                                             myeloid['mcsf_ng_ml']['value'],
                                             myeloid['mcsf_ng_ml']['was']))
        cntf = myeloid['cntf_ng_ml']
        self.assertEqual(('add', 'NEW', False, 'H02'),
                         (cntf['action'], cntf['letter'], cntf['registered'], cntf['hypothesis_id']))
        self.assertEqual('CNTF concentration in maturation medium', cntf['label'])
        self.assertEqual('ng/mL', cntf['unit'], 'the unit the agent wrote into the label')
        self.assertEqual([], stages['expansion']['inducers'])
        self.assertIn('agitation_rpm', {i['parameter_id'] for i in fp['physical']})
        self.assertNotIn('agitation_rpm', {i['parameter_id'] for s in fp['stages']
                                           for i in s['inducers']})
        self.assertEqual('Add CNTF concentration in maturation medium at 20 ng/mL in Myeloid harvest',
                         p['actions']['H02'])
        self.assertEqual('Raise M-CSF to 50 ng/mL in Myeloid harvest', p['actions']['H01'])
        md = (self.tmp / 'protocol_summary.md').read_text()
        self.assertIn('## First-pass plan', md)
        self.assertIn('- Add CNTF concentration in maturation medium at 20 ng/mL [NEW, low, H02]', md)

    def test_no_dose_is_invented_and_a_ruled_out_lever_adds_nothing(self):
        p = finished(self.tmp, {'parameter_id': 'cntf_ng_ml', 'parameter_label': CNTF,
                                'stage': 'myeloid'},
                     {'hypothesis_id': 'H02', 'parameter_id': 'lif_ng_ml', 'unit': 'ng/mL',
                      'parameter_label': 'LIF', 'stage': 'myeloid', 'candidate_value': 10,
                      'status': 'contradicted', 'superseded_reason': 'refuted by H01'})
        items = {i['parameter_id']: i for s in p['first_pass']['stages'] for i in s['inducers']}
        self.assertIsNone(items['cntf_ng_ml']['value'])
        self.assertIn('dose not set by this run', items['cntf_ng_ml']['text'])
        self.assertNotIn('lif_ng_ml', items)

    def test_a_label_keeps_its_name_and_gives_up_its_notes(self):
        self.assertEqual(('CNTF concentration in maturation medium', 'ng/mL'), FP.split_label(CNTF))
        self.assertEqual(('M-CSF', None), FP.split_label('M-CSF'))
        self.assertEqual(('Wnt3a — pulse', None), FP.split_label('Wnt3a — pulse'))


class PageTests(unittest.TestCase):
    def test_the_page_reads_insights_then_recommendation_then_plan_then_the_rest(self):
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        order = [html.index(x) for x in ('id="litSec"', 'id="bioSec"', 'id="anaSec"',
                                         'id="resultPanel"', 'id="protocolPanel"',
                                         'id="otherPanel"')]
        self.assertEqual(sorted(order), order)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('host.append(recommendationCard(selected, protocol))', js)
        self.assertIn('host.append(renderPlan(p.first_pass))', js)
        self.assertIn("'Why — the full hypothesis, its effects and evidence'", js)
        proto = js[js.index('function renderProtocol'):js.index('function planItem')]
        self.assertNotIn("el('table', 'ledger')", proto, 'the ledger moved to Other hypotheses')


if __name__ == '__main__':
    unittest.main()
