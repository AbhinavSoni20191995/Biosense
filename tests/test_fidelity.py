"""How close the cells this protocol makes are expected to come to the real ones.

Hitting a yield and a purity target says nothing about whether the thing in the
vessel is the cell the person meant. This judges that, axis by axis, against
the in vivo trajectory — and the rules here are what keep an expectation from
reading as a result: the axes are fixed so none is quietly dropped, a high
expectation needs a citation, confidence is capped because the cells do not
exist yet, every axis names the assay that would settle it, and nothing may
call the product identical to a primary cell.
"""
import copy
import unittest

from biosense import contracts as K
from biosense import projects as PJ
from biosense.evidence import fidelity as FD
from biosense.production import discovery as DISC


def build(**over):
    d = copy.deepcopy(FD.TEMPLATE)
    d.update(over)
    return FD.build(d, project=PJ.load('ipsc_macrophage'), run_id='ai-x')


class AxisTests(unittest.TestCase):
    def test_every_axis_is_judged_or_comes_back_unknown(self):
        doc = build()
        K.require_valid('cell_fidelity', doc)
        self.assertEqual(list(FD.AXES), [c['axis'] for c in doc['criteria']],
                         'all ten axes, in order')
        by = {c['axis']: c for c in doc['criteria']}
        self.assertEqual('low', by['maturation_state']['expected_similarity'])
        # An axis the run never addressed is unknown with a reason, not missing.
        self.assertEqual('unknown', by['epigenome']['expected_similarity'])
        self.assertTrue(by['epigenome']['reasons'])
        self.assertEqual('not established by this run', by['epigenome']['measured_by'])

    def test_a_high_expectation_needs_a_source_that_measured_it(self):
        d = copy.deepcopy(FD.TEMPLATE)
        d['criteria'][1].update(expected_similarity='high', refs=[])
        with self.assertRaisesRegex(K.ContractError, 'High needs a source'):
            FD.build(d, project=PJ.load('ipsc_macrophage'))

    def test_confidence_cannot_exceed_moderate(self):
        d = copy.deepcopy(FD.TEMPLATE)
        d['criteria'][0]['confidence'] = 'high'
        with self.assertRaisesRegex(K.ContractError, 'use one of low, moderate'):
            FD.build(d, project=PJ.load('ipsc_macrophage'))

    def test_a_judgement_needs_a_reason_and_a_way_to_settle_it(self):
        d = copy.deepcopy(FD.TEMPLATE)
        d['criteria'][0]['reasons'] = []
        with self.assertRaisesRegex(K.ContractError, 'gives no reason'):
            FD.build(d, project=PJ.load('ipsc_macrophage'))
        d = copy.deepcopy(FD.TEMPLATE)
        d['criteria'][0]['measured_by'] = ''
        with self.assertRaisesRegex(K.ContractError, 'measured_by'):
            FD.build(d, project=PJ.load('ipsc_macrophage'))

    def test_calling_the_product_the_primary_cell_is_refused(self):
        for words in ('The product is indistinguishable from primary cells.',
                      'It is equivalent to the primary macrophage in every way.',
                      'The harvest is fully mature by the end of the run.'):
            d = copy.deepcopy(FD.TEMPLATE)
            d['overall']['summary'] = words
            with self.assertRaisesRegex(K.ContractError, 'have not been made yet'):
                FD.build(d, project=PJ.load('ipsc_macrophage'))

    def test_a_weak_axis_obliges_naming_the_one_that_limits_the_match(self):
        d = copy.deepcopy(FD.TEMPLATE)
        d['overall']['dominant_gap'] = None
        with self.assertRaisesRegex(K.ContractError, 'most limits the match'):
            FD.build(d, project=PJ.load('ipsc_macrophage'))
        d['overall']['dominant_gap'] = 'vibes'
        with self.assertRaisesRegex(K.ContractError, 'name one of the axes'):
            FD.build(d, project=PJ.load('ipsc_macrophage'))

    def test_which_real_cell_it_is_judged_against_is_stated_and_argued(self):
        doc = build()
        ref = doc['in_vivo_reference']
        self.assertIn('yolk-sac', ref['cell'])
        self.assertTrue(ref['why_this_one'], 'which counterpart is a claim, not a default')
        with self.assertRaisesRegex(K.ContractError, 'in_vivo_reference.why_this_one'):
            build(in_vivo_reference={'cell': 'a macrophage', 'why_this_one': ''})

    def test_the_round_plan_marks_which_expectations_become_measurements(self):
        plan = {'readouts': [{'name': 'phagocytosis assay', 'unit': '%', 'when': 'harvest'}]}
        doc = FD.build(copy.deepcopy(FD.TEMPLATE), project=PJ.load('ipsc_macrophage'),
                       round_plan=plan)
        by = {c['axis']: c for c in doc['criteria']}
        self.assertTrue(by['function']['in_round_plan'],
                        'the plan measures it, so it stops being an expectation')
        self.assertFalse(by['epigenome']['in_round_plan'])


class BriefAndPageTests(unittest.TestCase):
    def test_the_developmental_lens_asks_for_the_judgement(self):
        req = DISC.build(project_id='ipsc_macrophage', runtime_mode='synthetic_demo',
                         objective='more macrophages per iPSC cell')
        brief = DISC.render_brief(req, loop_dir='runs/ai-x')
        self.assertIn('cli fidelity --project', brief)
        self.assertIn('cell_fidelity.json', brief)
        self.assertIn('WHICH real cell you are judging against', brief)
        self.assertIn('dominant_gap', brief)

    def test_the_page_shows_every_axis_and_what_would_settle_it(self):
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertIn('id="fidSec"', html)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function renderFidelity', js)
        self.assertIn('snap.cell_fidelity', js)
        self.assertIn("'Settled by'", js)
        self.assertIn('have not been made yet', js)


if __name__ == '__main__':
    unittest.main()
