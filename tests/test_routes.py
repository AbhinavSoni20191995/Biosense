"""The specialist mode: learn the system, choose the route, then the parameters.

A run that searches for setpoints before deciding what to make the cell from
researches the wrong thing well. The staged mode asks the two questions that
come first — how does this cell arise, and how is it made in any system — has
the analyst score each route against the stated need, and records the choice in
a document a reader can check. These tests hold the rules that make it
checkable: a criterion names where it came from, every candidate is scored
against every criterion, a route failing a hard criterion cannot be chosen,
every candidate not chosen carries a reason, and the wording is "chosen
because", never "best".
"""
import copy
import unittest

from biosense import contracts as K
from biosense import projects as PJ
from biosense.evidence import routes as RS
from biosense.production import discovery as DISC


def build(**over):
    d = copy.deepcopy(RS.TEMPLATE)
    d.update(over)
    return RS.build(d, project=PJ.load('ipsc_macrophage'), run_id='ai-x')


class ChoiceTests(unittest.TestCase):
    def test_the_template_builds_and_keeps_the_reasons(self):
        doc = build()
        K.require_valid('route_selection', doc)
        self.assertEqual('R1', doc['chosen']['route_id'])
        self.assertEqual({'R2', 'R3'}, {a['route_id'] for a in doc['alternatives']})
        self.assertTrue(all(a['why_not'] for a in doc['alternatives']))

    def test_a_route_that_fails_a_hard_criterion_cannot_be_chosen(self):
        d = copy.deepcopy(RS.TEMPLATE)
        d['chosen'] = {'route_id': 'R2', 'why': 'it is the fastest route by far',
                       'fits_project': True}
        d['alternatives'] = [{'route_id': 'R1', 'why_not': 'slower to first harvest'},
                             {'route_id': 'R3', 'why_not': 'not a renewable source'}]
        with self.assertRaisesRegex(K.ContractError, 'fails hard criteria'):
            RS.build(d, project=PJ.load('ipsc_macrophage'))

    def test_every_candidate_is_scored_against_every_criterion(self):
        d = copy.deepcopy(RS.TEMPLATE)
        d['candidates'][0]['fit'] = [{'criterion_id': 'C1', 'verdict': 'meets'}]
        doc = RS.build(d, project=PJ.load('ipsc_macrophage'))
        scores = {f['criterion_id']: f['verdict'] for f in doc['candidates'][0]['fit']}
        self.assertEqual({'C1', 'C2', 'C3', 'C4'}, set(scores))
        self.assertEqual('unknown', scores['C4'], 'a criterion left out is unknown, not dropped')

    def test_a_candidate_dropped_without_a_reason_is_refused(self):
        d = copy.deepcopy(RS.TEMPLATE)
        d['alternatives'] = d['alternatives'][:1]
        with self.assertRaisesRegex(K.ContractError, 'every candidate but the chosen one'):
            RS.build(d, project=PJ.load('ipsc_macrophage'))

    def test_claiming_one_route_beats_another_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'cannot prove one route beats another'):
            build(chosen={'route_id': 'R1', 'why': 'it is the best route available',
                          'fits_project': True})

    def test_a_route_the_project_cannot_run_says_what_it_would_need(self):
        d = copy.deepcopy(RS.TEMPLATE)
        d['chosen']['fits_project'] = False
        with self.assertRaisesRegex(K.ContractError, 'what the project would need'):
            RS.build(d, project=PJ.load('ipsc_macrophage'))
        d['chosen']['project_note'] = 'register a vessel parameter for the aggregate format'
        self.assertFalse(RS.build(d, project=PJ.load('ipsc_macrophage'))['chosen']['fits_project'])

    def test_every_route_cites_its_sources_and_the_need_has_criteria(self):
        with self.assertRaisesRegex(K.ContractError, 'cites no source'):
            d = copy.deepcopy(RS.TEMPLATE)
            d['candidates'][0]['refs'] = []
            RS.build(d, project=PJ.load('ipsc_macrophage'))
        with self.assertRaisesRegex(K.ContractError, 'no criteria'):
            build(need={'criteria': []})


class ModeTests(unittest.TestCase):
    def _req(self, **kw):
        return DISC.build(project_id='ipsc_macrophage', runtime_mode='synthetic_demo',
                          objective='make macrophages at suspension scale', **kw)

    def test_specialist_is_a_mode_and_not_the_default(self):
        self.assertEqual('single', self._req()['literature_mode'])
        self.assertEqual('specialist', self._req(literature_mode='specialist')['literature_mode'])
        with self.assertRaisesRegex(K.ContractError, 'literature_mode'):
            self._req(literature_mode='swarm')

    def test_the_brief_asks_the_three_phases_in_order(self):
        brief = DISC.render_brief(self._req(literature_mode='specialist'), loop_dir='runs/ai-x')
        for part in ('literature-development', 'literature-landscape', 'PRODUCTION LANDSCAPE:',
                     'ROUTE REVIEW:', 'analyst-routes', 'route-select --project',
                     'Phase 3', 'worth_a_parallel_arm'):
            self.assertIn(part, brief)
        self.assertLess(brief.index('PRODUCTION LANDSCAPE:'), brief.index('ROUTE REVIEW:'))
        self.assertLess(brief.index('ROUTE REVIEW:'), brief.index('Phase 3'))
        self.assertIn('specialist (staged)', DISC.summarise(self._req(literature_mode='specialist')))

    def test_the_ordinary_modes_do_not_carry_the_staged_brief(self):
        for mode in ('single', 'by_stage'):
            brief = DISC.render_brief(self._req(literature_mode=mode), loop_dir='runs/ai-x')
            self.assertNotIn('PRODUCTION LANDSCAPE:', brief, mode)
            self.assertNotIn('ROUTE REVIEW:', brief, mode)


class AgentAndPageTests(unittest.TestCase):
    def test_each_agent_knows_its_part(self):
        lit = (K.ROOT / 'discovery_loop' / 'agents' / 'literature' / 'config.yaml').read_text()
        self.assertIn('PRODUCTION LANDSCAPE:', lit)
        self.assertIn('never say which route is best', lit)
        ana = (K.ROOT / 'discovery_loop' / 'agents' / 'analyst' / 'prompt.md').read_text()
        self.assertIn('ROUTE REVIEW', ana)
        self.assertIn('You **review**; you do not choose.', ana)
        bio = (K.ROOT / 'discovery_loop' / 'agents' / 'bioinformatics' / 'prompt.md').read_text()
        self.assertIn('LANDSCAPE: on', bio)
        self.assertIn('Do not rank the routes.', bio)

    def test_the_page_offers_the_mode_and_shows_the_route(self):
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertIn('value="specialist"', html)
        self.assertIn('id="routeSec"', html)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function renderRoute', js)
        self.assertIn('snap.route_selection', js)


if __name__ == '__main__':
    unittest.main()
