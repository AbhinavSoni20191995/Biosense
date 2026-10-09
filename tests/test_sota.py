"""The state-of-the-art add-on: the recommended protocol against the field.

The point is to answer "how does this compare to how the cell is already made?"
without overclaiming. These tests hold the honesty rules: a difference carries
an expected effect (a hypothesis, confidence capped) never a measured gain, the
builder refuses "better / best / outperforms" wording, and the add-on is on by
default and reaches the brief and the run page.
"""
import unittest

from biosense import contracts as K
from biosense import projects as PJ
from biosense.evidence import sota as SO
from biosense.production import discovery as DISC


def a_draft(**over):
    base = {
        'cell_type': 'macrophage',
        'references': [
            {'ref_id': 'S1', 'citation': 'Author 2013, Journal',
             'route': 'directed_differentiation',
             'summary': 'EB-based iPSC to macrophage via BMP4/VEGF then M-CSF.',
             'key_factors': ['BMP4', 'M-CSF'],
             'reported': {'yield': 'high', 'purity': '>90% CD14+', 'timeline': '~30 days'},
             'is_benchmark': True, 'refs': ['PMID:1']},
            {'ref_id': 'S2', 'citation': 'Author 2018, Journal', 'route': 'forward_programming',
             'summary': 'Transcription-factor forward programming to macrophages.',
             'refs': ['PMID:2']},
        ],
        'comparisons': [
            {'dimension': 'M-CSF dose', 'stage_id': 'myeloid', 'this_protocol': '50 ng/mL',
             'state_of_the_art': 'S1 uses 100 ng/mL', 'ref_ids': ['S1'], 'verdict': 'differs',
             'difference': 'half the benchmark',
             'expected_effect': {'readout': 'yield', 'direction': 'decrease', 'confidence': 'moderate',
                                 'basis': 'M-CSF is dose-limiting for CSF1R output here.',
                                 'refs': ['PMID:1']}},
        ],
        'standing': {'verdict': 'variant_of_sota', 'benchmark_ref_id': 'S1',
                     'summary': 'A variant of the standard directed route at a lower dose.'},
    }
    base.update(over)
    return base


def build(**over):
    return SO.build(a_draft(**over), project=PJ.load('ipsc_macrophage'), run_id='ai-x')


class RuleTests(unittest.TestCase):
    def test_overclaiming_a_gain_is_refused(self):
        for word in ('better than S1', 'the best route', 'outperforms S1', 'a superior protocol'):
            with self.assertRaisesRegex(K.ContractError, 'claims no measured gain'):
                build(standing={'verdict': 'variant_of_sota', 'summary': f'This is {word}.'})

    def test_an_expected_effect_is_capped_without_a_citation(self):
        # Moderate with a ref stays moderate; moderate with no ref is capped to low.
        self.assertEqual('moderate', build()['comparisons'][0]['expected_effect']['confidence'])
        c = a_draft()
        c['comparisons'][0]['expected_effect'] = {
            'readout': 'yield', 'direction': 'decrease', 'confidence': 'moderate',
            'basis': 'mechanistic reasoning only, no source cited here.', 'refs': []}
        got = SO.build(c, project=PJ.load('ipsc_macrophage'))
        self.assertEqual('low', got['comparisons'][0]['expected_effect']['confidence'])

    def test_a_difference_must_say_what_differs_and_a_ref_must_exist(self):
        with self.assertRaisesRegex(K.ContractError, 'states no difference'):
            build(comparisons=[{'dimension': 'format', 'this_protocol': 'suspension',
                                'state_of_the_art': 'S1 adherent', 'verdict': 'novel'}])
        with self.assertRaisesRegex(K.ContractError, 'not a reference'):
            build(comparisons=[{'dimension': 'format', 'this_protocol': 'x',
                                'state_of_the_art': 'y', 'verdict': 'same', 'ref_ids': ['S9']}])

    def test_a_reference_cites_its_source_and_at_least_one_is_needed(self):
        with self.assertRaisesRegex(K.ContractError, 'cites no source'):
            build(references=[dict(a_draft()['references'][0], refs=[])])
        with self.assertRaisesRegex(K.ContractError, 'at least one published protocol'):
            build(references=[])

    def test_the_built_comparison_validates_and_keeps_the_benchmark(self):
        doc = build()
        K.require_valid('sota_comparison', doc)
        self.assertEqual('S1', doc['standing']['benchmark_ref_id'])
        self.assertEqual('directed_differentiation', doc['references'][0]['route'])


class RequestAndBriefTests(unittest.TestCase):
    def _req(self, **kw):
        return DISC.build(project_id='ipsc_macrophage', runtime_mode='synthetic_demo',
                          objective='more macrophages per iPSC cell', **kw)

    def test_the_lens_is_on_by_default_and_can_be_turned_off(self):
        self.assertIn('state_of_the_art', self._req()['addons'])
        self.assertNotIn('state_of_the_art', self._req(addons=['developmental_biology'])['addons'])

    def test_the_lens_adds_a_task_and_a_section_to_the_brief(self):
        brief = DISC.render_brief(self._req(), loop_dir='runs/ai-x')
        self.assertIn('literature-sota', brief)
        self.assertIn('STATE OF THE ART:', brief)
        self.assertIn('state of the art comparison (on for this run)', brief)
        self.assertIn('cli sota --project', brief)
        self.assertIn('State of the art comparison', DISC.summarise(self._req()))
        off = DISC.render_brief(self._req(addons=['developmental_biology']), loop_dir='runs/ai-x')
        self.assertNotIn('STATE OF THE ART', off)


class AgentAndPageTests(unittest.TestCase):
    def test_the_literature_agent_knows_the_state_of_the_art_task(self):
        lit = (K.ROOT / 'discovery_loop' / 'agents' / 'literature' / 'config.yaml').read_text()
        self.assertIn('STATE OF THE ART:', lit)
        self.assertIn('never judge the run\'s protocol better', lit)

    def test_the_run_page_has_a_state_of_the_art_section(self):
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertIn('id="sotaSec"', html)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function renderSota', js)
        self.assertIn('snap.sota_comparison', js)


if __name__ == '__main__':
    unittest.main()
