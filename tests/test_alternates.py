"""The other leads, worked up as experiments the person asked for.

A run surfaces more good ideas than one round can hold: a Notch ligand from
the pulmonary endothelium, a surfactant lipid load, GM-CSF as a timed surge
rather than a constant. One becomes the hypothesis. The rest become prose in a
report, and prose is not an experiment — nobody ever puts a vessel on it.

So a person can pick from that list, in the same conversation they ask
questions in, and get plans back. These tests hold the two rules that make
that safe: round 1 is never touched, and an alternate that restates the main
hypothesis costs a vessel to learn nothing.
"""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense import projects as PJ
from biosense.evidence import alternates as AL


def draft(**kw):
    d = copy.deepcopy(AL.TEMPLATE)
    d.update(kw)
    return d


def item(**kw):
    it = copy.deepcopy(AL.TEMPLATE['items'][0])
    it.update(kw)
    return draft(items=[it])


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.project = PJ.load('ipsc_macrophage')

    def build(self, d, **kw):
        return AL.build(d, project=self.project, run_id='ai-x', requested_by='Ana', **kw)

    def test_a_lead_becomes_a_claim_with_arms_and_a_price(self):
        doc = self.build(AL.TEMPLATE)
        K.require_valid('alternate_plans', doc)
        it = doc['items'][0]
        self.assertEqual(('ALT1', 'extension', 'proposed'),
                         (it['alt_id'], it['relation'], it['status']))
        self.assertEqual(2, len(it['arms']))
        self.assertTrue(it['cost'], 'a vessel somebody pays for says what it costs')
        self.assertTrue(it['decision_rule']['if'] and it['decision_rule']['then'])

    def test_an_alternate_with_no_arms_or_no_readout_is_not_an_experiment(self):
        with self.assertRaisesRegex(K.ContractError, 'what would be run'):
            self.build(item(arms=[]))
        with self.assertRaisesRegex(K.ContractError, 'what would settle it'):
            self.build(item(readouts=[]))

    def test_confidence_is_capped_by_what_it_rests_on(self):
        """A mechanism nobody has tested in this system is not strong evidence
        that changing it works."""
        with self.assertRaisesRegex(K.ContractError, 'capped at moderate'):
            self.build(item(evidence_basis='mechanistic', confidence='high'))
        with self.assertRaisesRegex(K.ContractError, 'capped at low'):
            self.build(item(evidence_basis='judgement', confidence='moderate'))
        d = self.build(item(evidence_basis='judgement', confidence='low', refs=[]))
        self.assertEqual('low', d['items'][0]['confidence'])

    def test_calling_it_cited_without_citing_anything_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'cited and cites nothing'):
            self.build(item(evidence_basis='cited', confidence='moderate', refs=[]))

    def test_an_alternate_that_restates_the_main_plan_is_refused(self):
        """A vessel that tests the main plan again teaches nothing new."""
        same = item(levers=[{'label': 'M-CSF', 'direction': 'increase'}],
                    differs_from_main='it is a bit different')
        with self.assertRaisesRegex(K.ContractError, 'teaches nothing new'):
            self.build(same, main_levers=[('M-CSF', 'increase')])
        # Said properly, it is allowed: two runs can move one lever for
        # different reasons, and the reason is what distinguishes them.
        ok = item(levers=[{'label': 'M-CSF', 'direction': 'increase'}],
                  differs_from_main='The main plan raises M-CSF through the whole harvest; '
                                    'this raises it only for the first 48 h and then withdraws '
                                    'it, which tests the window rather than the level.')
        self.assertTrue(self.build(ok, main_levers=[('M-CSF', 'increase')])['items'])

    def test_nothing_here_may_claim_it_works(self):
        with self.assertRaisesRegex(K.ContractError, 'proves'):
            self.build(item(claim='Adding DLL4 proves that Notch is required for alveolar '
                                  'identity in this system.'))
        with self.assertRaisesRegex(K.ContractError, 'will raise'):
            self.build(item(would_change='It will raise the alveolar fraction by 20 points.'))

    def test_a_lever_the_project_has_is_marked_as_one(self):
        d = self.build(item(levers=[{'label': 'M-CSF', 'parameter_id': 'mcsf_ng_ml',
                                     'direction': 'decrease', 'stage': 'myeloid'}],
                            differs_from_main='The main plan adds a factor; this withdraws the '
                                              'one already there, which is a different claim.'))
        self.assertTrue(d['items'][0]['levers'][0]['registered'])
        d2 = self.build(item(levers=[{'label': 'immobilised DLL4', 'direction': 'add'}]))
        self.assertFalse(d2['items'][0]['levers'][0]['registered'])

    def test_a_stage_the_project_does_not_have_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, "this project's stages"):
            self.build(item(levers=[{'label': 'DLL4', 'stage': 'maturation'}]))

    def test_asked_for_and_not_worked_up_is_a_real_answer(self):
        doc = self.build(AL.TEMPLATE)
        self.assertEqual(1, len(doc['not_pursued']))
        self.assertIn('surrogate', doc['not_pursued'][0]['why'])

    def test_it_says_round_one_is_untouched(self):
        doc = self.build(AL.TEMPLATE)
        self.assertIn('do not change the main hypothesis', doc['does_not_change'])
        self.assertIn('commitment digest', doc['does_not_change'])

    def test_more_alternates_than_a_round_can_hold_are_refused(self):
        many = draft(items=[dict(copy.deepcopy(AL.TEMPLATE['items'][0]), alt_id=f'ALT{i}')
                            for i in range(1, 9)])
        with self.assertRaisesRegex(K.ContractError, 'the limit is 6'):
            self.build(many)


class TurnTests(unittest.TestCase):
    """The conversation: a question changes nothing, this writes one file."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_the_two_kinds_share_one_thread(self):
        from biosense.production import ask as ASK
        q = ASK.start(self.d, 'Why CNTF and not LIF?')
        ASK.finish(self.d, q['id'], status='answered', answer='Because …')
        a = ASK.start(self.d, 'Work up the Notch lead too', kind='alternate')
        self.assertEqual(['question', 'alternate'],
                         [i['kind'] for i in ASK.load(self.d)['items']])
        self.assertEqual('alternate', a['kind'])
        with self.assertRaisesRegex(K.ContractError, 'kind must be one of'):
            ASK.start(self.d, 'x y z', kind='rewrite_everything')

    def test_the_orchestrator_is_told_what_it_may_not_change(self):
        text = AL.framed('work up DLL4 and the GM-CSF surge', loop_dir='data/runs/ai-1',
                         project_id='ipsc_macrophage', main=[('mcsf_ng_ml', 'increase')],
                         existing=[('ALT1', 'Notch')])
        for rule in ('Do NOT change the main hypothesis', 'round_plan.json',
                     'commitment digest', 'caps confidence at moderate',
                     'Do not dispatch a specialist'):
            self.assertIn(rule, text)
        self.assertIn('<<<work up DLL4 and the GM-CSF surge>>>', text,
                      'their words travel as data')
        self.assertIn('ALT1', text, 'it is told what it already wrote')
        self.assertIn('mcsf_ng_ml', text, 'and what the main plan moves')

    def test_the_main_levers_come_from_the_run_s_own_hypotheses(self):
        (self.d / 'quantified_hypothesis.json').write_text(json.dumps(
            {'parameter': {'parameter_id': 'mcsf_ng_ml', 'direction': 'increase'}}))
        (self.d / 'quantified_hypothesis_2.json').write_text(json.dumps(
            {'parameter': {'label': 'TGF-beta1', 'direction': 'add'}}))
        self.assertEqual([('mcsf_ng_ml', 'increase'), ('TGF-beta1', 'add')],
                         AL.main_levers(self.d))
        self.assertEqual([], AL.main_levers(self.d / 'nope'))

    def test_a_run_with_no_alternates_reads_as_none(self):
        self.assertIsNone(AL.load(self.d))
        (self.d / AL.NAME).write_text('{"kind": "something_else"}')
        self.assertIsNone(AL.load(self.d))


class WiringTests(unittest.TestCase):
    def test_the_page_offers_it_and_shows_what_came_back(self):
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertIn('id="altBtn"', html)
        self.assertIn('id="altPanel"', html)
        self.assertIn('it never changes', html)
        self.assertIn('the protocol or round 1', html)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function renderAlternates', js)
        self.assertIn("kind: kind || 'question'", js)
        self.assertIn('snap.alternate_plans', js)

    def test_the_run_reads_them_live_because_they_arrive_after_it_ended(self):
        import inspect
        from biosense.production import app as APP
        from biosense.production import run_store as RS
        self.assertIn('alternate_plans', inspect.getsource(RS.restore))
        self.assertIn('_read_alternates', inspect.getsource(APP.DiscoveryRun))

    def test_the_bench_copy_carries_them_as_an_appendix(self):
        from biosense.production import lab_protocol as LP
        from tests.test_lab_protocol import PROTOCOL, text_of
        doc = AL.build(AL.TEMPLATE, project=PJ.load('ipsc_macrophage'))
        text = '\n'.join(text_of(LP.tobytes(dict(PROTOCOL, alternate_plans=doc))))
        self.assertIn('Alternate and extended plans', text)
        self.assertIn('ALT1 — DLL4 / Notch as the missing juxtacrine signal', text)
        self.assertIn('What it costs', text)
        self.assertIn('do not change the main hypothesis', text)
        # After round 1, not inside it.
        self.assertLess(text.index('The protocol, stage by stage'),
                        text.index('Alternate and extended plans'))

    def test_the_written_report_keeps_the_price_and_the_rule(self):
        md = '\n'.join(AL.markdown(AL.build(AL.TEMPLATE, project=PJ.load('ipsc_macrophage'))))
        self.assertIn('**What it costs**', md)
        self.assertIn('**If**', md)
        self.assertIn('Asked for, not worked up', md)


if __name__ == '__main__':
    unittest.main()
