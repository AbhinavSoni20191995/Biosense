"""The developmental-biology add-on: the process read against the embryo.

The point of the lens is to turn what development shows — signals and their
windows, the regulators, what knocking them out does — into levers to test,
honestly: an idea states the search behind its novelty, never "never tried",
and its confidence is capped to what mechanism and analogy can carry. These
tests hold those rules, and that the add-on is on by default and reaches the
brief.
"""
import unittest

from biosense import contracts as K
from biosense import projects as PJ
from biosense.evidence import devmap as DM
from biosense.production import discovery as DISC


def a_map(**over):
    base = {
        'cell_type': 'macrophage',
        'stages': [{
            'stage_id': 'myeloid', 'in_vivo_counterpart': 'yolk-sac primitive macrophage',
            'signals': [{'name': 'M-CSF', 'state': 'rising', 'refs': ['PMID:1']}],
            'regulators': [{'gene': 'PU.1', 'role': 'myeloid commitment', 'refs': ['PMID:2']}],
            'perturbations': [{'gene': 'CSF1R', 'type': 'knockout', 'model': 'mouse',
                               'phenotype': 'tissue macrophages lost', 'refs': ['PMID:3']}],
            'protocol_vs_development': ['Protocol holds M-CSF flat.'],
        }],
        'ideas': [{
            'idea_id': 'D1', 'lever': 'IL-34', 'stage_id': 'myeloid', 'action': 'add',
            'rationale': 'CSF1R has a second ligand in vivo that the protocol omits.',
            'developmental_refs': ['PMID:4'],
            'protocol_search': {'queries': ['iPSC macrophage IL-34 protocol'],
                                'found_in_protocols': False},
            'confidence': 'moderate',
        }],
    }
    base.update(over)
    return base


def build(**over):
    return DM.build(a_map(**over), project=PJ.load('ipsc_macrophage'), run_id='ai-x')


class RuleTests(unittest.TestCase):
    def test_a_developmental_statement_without_a_source_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'cites no source'):
            build(stages=[dict(a_map()['stages'][0],
                               signals=[{'name': 'BMP4', 'state': 'on', 'refs': []}])])

    def test_novelty_is_the_scope_of_a_search_never_never_tried(self):
        doc = build()
        self.assertEqual('not found in the 1 protocol search listed', doc['ideas'][0]['novelty'])
        with self.assertRaisesRegex(K.ContractError, 'No search can show that'):
            build(ideas=[dict(a_map()['ideas'][0], rationale='A factor never tried in any dish.')])

    def test_confidence_is_capped_to_what_the_idea_rests_on(self):
        # No protocol precedent: moderate is capped to low, with the reason kept.
        low = build()['ideas'][0]
        self.assertEqual('low', low['confidence'])
        self.assertIn('no direct test', low['confidence_note'])
        # A protocol that uses it lifts the cap to moderate and names it.
        used = build(ideas=[dict(a_map()['ideas'][0], confidence='moderate',
                                 protocol_search={'queries': ['q'], 'found_in_protocols': True,
                                                  'closest_protocol_ref': 'PMID:9'})])['ideas'][0]
        self.assertEqual(('moderate', 'used before: PMID:9'), (used['confidence'], used['novelty']))

    def test_an_idea_must_say_what_was_searched(self):
        with self.assertRaisesRegex(K.ContractError, 'states no protocol search'):
            build(ideas=[dict(a_map()['ideas'][0], protocol_search={'queries': [],
                                                                    'found_in_protocols': False})])

    def test_a_stage_must_be_one_the_project_has(self):
        with self.assertRaisesRegex(K.ContractError, 'names stage'):
            build(stages=[dict(a_map()['stages'][0], stage_id='gastrulation')])

    def test_an_empty_map_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'no stage and no idea'):
            build(stages=[], ideas=[])

    def test_the_built_map_validates_and_keeps_its_evidence(self):
        doc = build()
        K.require_valid('developmental_map', doc)
        self.assertEqual('CSF1R', doc['stages'][0]['perturbations'][0]['gene'])
        self.assertEqual(['PMID:3'], doc['stages'][0]['perturbations'][0]['refs'])


class RequestTests(unittest.TestCase):
    def test_the_lens_is_on_by_default_for_a_discovery_run_and_can_be_turned_off(self):
        req = DISC.build(project_id='ipsc_macrophage', objective='more macrophages per iPSC cell',
                         runtime_mode='synthetic_demo')
        self.assertIn('developmental_biology', req['addons'])
        off = DISC.build(project_id='ipsc_macrophage', objective='more macrophages per iPSC cell',
                         runtime_mode='synthetic_demo', addons=[])
        self.assertEqual([], off['addons'])
        with self.assertRaisesRegex(K.ContractError, 'unknown add-on'):
            DISC.build(project_id='ipsc_macrophage', objective='more macrophages per iPSC cell',
                       runtime_mode='synthetic_demo', addons=['teleology'])

    def test_a_reference_run_carries_no_lens(self):
        req = DISC.build(project_id='ipsc_macrophage', objective='build the process reference now',
                         runtime_mode='synthetic_demo', purpose='process_reference')
        self.assertEqual([], req['addons'])

    def test_the_lens_adds_a_task_and_a_section_to_the_brief(self):
        req = DISC.build(project_id='ipsc_macrophage', objective='more macrophages per iPSC cell',
                         runtime_mode='synthetic_demo')
        brief = DISC.render_brief(req, loop_dir='runs/ai-x')
        self.assertIn('literature-development', brief)
        self.assertIn('DEVELOPMENTAL BIOLOGY:', brief)
        self.assertIn('DEVELOPMENTAL LENS: on', brief)
        self.assertIn('developmental_map.json', brief)
        self.assertIn('Developmental biology lens', DISC.summarise(req))
        off = DISC.render_brief(DISC.build(project_id='ipsc_macrophage', addons=[],
                                           objective='more macrophages per iPSC cell',
                                           runtime_mode='synthetic_demo'), loop_dir='runs/ai-x')
        self.assertNotIn('DEVELOPMENTAL', off)


class AgentAndPageTests(unittest.TestCase):
    def test_every_agent_is_told_about_the_lens(self):
        lit = (K.ROOT / 'discovery_loop' / 'agents' / 'literature' / 'config.yaml').read_text()
        self.assertIn('DEVELOPMENTAL BIOLOGY:', lit)
        self.assertIn('never write that something was "never tried"', lit)
        bio = (K.ROOT / 'discovery_loop' / 'agents' / 'bioinformatics' / 'prompt.md').read_text()
        self.assertIn('DEVELOPMENTAL LENS', bio)
        self.assertIn('Receptor windows', bio)
        ana = (K.ROOT / 'discovery_loop' / 'agents' / 'analyst' / 'prompt.md').read_text()
        self.assertIn('Developmental data', ana)

    def test_the_run_page_has_a_developmental_section(self):
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertIn('id="devSec"', html)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function renderDevMap', js)
        self.assertIn('snap.developmental_map', js)


if __name__ == '__main__':
    unittest.main()
