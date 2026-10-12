"""The published route, stage by stage, so no stage reads as unknown.

A protocol carries a dose only for a parameter the project registered. That is
the rule that keeps invented numbers out, and it is not what these tests
question. What they hold is the consequence: a project made from the quick form
has the vessel controls and no biology, so every stage renders "no inducer
set" however much the run found out, and the person reads a protocol that
appears to start at the last stage.

The baseline is where that knowledge lives. These check that it covers the
whole process, that a dose never appears without a source behind it, and that
it reaches the plan marked as published rather than recommended.
"""
import copy
import unittest

from biosense import contracts as K
from biosense import projects as PJ
from biosense.evidence import baseline as BL
from biosense.production import first_pass as FP


def draft(**kw):
    d = copy.deepcopy(BL.TEMPLATE)
    d.update(kw)
    return d


def one(stage='myeloid', **factor):
    f = {'name': 'M-CSF', 'role': 'essential', 'provenance': 'reported',
         'why': 'Drives myeloid commitment at this stage.',
         'sources': [{'ref': 'vanWilgenburg2013', 'quote': '100 ng/ml M-CSF'}]}
    f.update(factor)
    return {'cell_type': 'iPSC-derived macrophage',
            'stages': [{'stage_id': stage, 'purpose': 'Release myeloid progenitors.',
                        'factors': [f]}]}


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.project = PJ.load('ipsc_macrophage')

    def test_every_stage_of_the_project_appears_even_the_unresearched_ones(self):
        """The blank stage is the whole reason this document exists, so a stage
        the run skipped comes back stated, never missing."""
        doc = BL.build(one(), project=self.project)
        K.require_valid('baseline_protocol', doc)
        self.assertEqual([s['stage_id'] for s in self.project.stages],
                         [s['stage_id'] for s in doc['stages']], 'in the process\'s own order')
        skipped = [s for s in doc['stages'] if not s['addressed']]
        self.assertEqual(3, len(skipped))
        self.assertIn('established nothing', skipped[0]['purpose'])
        self.assertEqual([], skipped[0]['factors'])

    def test_a_stage_the_project_does_not_have_is_refused_with_the_ones_it_does(self):
        with self.assertRaises(K.ContractError) as e:
            BL.build(one(stage='maturation'), project=self.project)
        self.assertIn('expansion', str(e.exception))
        self.assertIn('myeloid', str(e.exception))

    def test_the_coverage_counts_what_a_reader_would_otherwise_have_to_count(self):
        doc = BL.build(BL.TEMPLATE, project=self.project)
        self.assertEqual({'stages_total': 4, 'stages_addressed': 2, 'factors': 3,
                          'reported': 2, 'gaps': 1, 'already_parameters': 1}, doc['coverage'])

    def test_a_factor_the_project_already_carries_is_recognised_however_it_is_spelled(self):
        """'M-CSF' and mcsf_ng_ml are the same thing. Named apart, the plan
        would offer to register a parameter that already exists."""
        doc = BL.build(one(), project=self.project)
        f = [x for s in doc['stages'] for x in s['factors']][0]
        self.assertEqual('mcsf_ng_ml', f['in_project'])
        doc = BL.build(one(name='M-CSF concentration'), project=self.project)
        self.assertEqual('mcsf_ng_ml',
                         [x for s in doc['stages'] for x in s['factors']][0]['in_project'])
        doc = BL.build(one(name='IL-34'), project=self.project)
        self.assertIsNone([x for s in doc['stages'] for x in s['factors']][0]['in_project'])


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.project = PJ.load('ipsc_macrophage')

    def build(self, **factor):
        return BL.build(one(**factor), project=self.project)

    def test_a_reported_factor_with_nothing_cited_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'reported with nothing cited'):
            self.build(sources=[])

    def test_a_reported_dose_needs_a_source_and_a_chosen_one_needs_a_reason(self):
        """The three ways a number may appear, and the one way it may not.

        Reported means a paper says it, so it cites one. A design choice is
        somebody's reasoned starting value and carries no citation by
        definition — it is marked D and a person approves it. What cannot
        happen is a number claiming to be reported with nothing behind it."""
        with self.assertRaisesRegex(K.ContractError, 'reported with nothing cited'):
            self.build(value=100, unit='ng/mL', sources=[])
        chosen = self.build(provenance='design_choice', value=100, unit='ng/mL',
                            why='Taken from the closest reported practice, a static flask.',
                            sources=[])
        f = [x for s in chosen['stages'] for x in s['factors']][0]
        self.assertEqual((100.0, 'design_choice', []), (f['value'], f['provenance'], f['sources']))
        self.assertEqual(100.0, [x for s in self.build(value=100, unit='ng/mL')['stages']
                                 for x in s['factors']][0]['value'])

    def test_a_value_with_no_unit_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'no unit'):
            self.build(value=100)

    def test_a_gap_carries_no_number_at_all(self):
        """A gap is what the run could not establish. A number in it would be
        exactly the invention the gap exists to avoid."""
        with self.assertRaisesRegex(K.ContractError, 'is a gap and carries a value'):
            self.build(provenance='gap', value=50, unit='ng/mL', sources=[],
                       what_to_search='GM-CSF dose stirred tank')
        with self.assertRaisesRegex(K.ContractError, 'gap with no search'):
            self.build(provenance='gap', sources=[])

    def test_an_adapted_dose_says_which_context_it_came_from(self):
        with self.assertRaisesRegex(K.ContractError, 'adapted_from'):
            self.build(provenance='adapted', value=100, unit='ng/mL')
        d = self.build(provenance='adapted', value=100, unit='ng/mL',
                       adapted_from='a static flask protocol for the same line')
        self.assertEqual('a static flask protocol for the same line',
                         [x for s in d['stages'] for x in s['factors']][0]['adapted_from'])

    def test_nothing_in_a_baseline_may_call_a_published_route_the_best_one(self):
        """The literature says what was done and what happened. A baseline that
        says a route is optimal turns a starting point into a recommendation
        nobody checked."""
        with self.assertRaisesRegex(K.ContractError, 'optimal'):
            self.build(why='The optimal dose for this stage, established widely.')
        with self.assertRaisesRegex(K.ContractError, 'gold-standard'):
            BL.build(one(), project=self.project) and BL.build(
                dict(one(), route='the gold-standard route'), project=self.project)

    def test_the_same_factor_twice_in_one_stage_is_refused(self):
        d = one()
        d['stages'][0]['factors'].append(dict(d['stages'][0]['factors'][0], name='M CSF'))
        with self.assertRaisesRegex(K.ContractError, 'the same factor in one stage'):
            BL.build(d, project=self.project)

    def test_a_factor_with_no_explanation_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'why'):
            self.build(why='')


class PlanMergeTests(unittest.TestCase):
    """What the person actually sees: the stages stop being blank."""

    def setUp(self):
        self.project = PJ.load('ipsc_macrophage')
        self.baseline = BL.build(BL.TEMPLATE, project=self.project)
        self.doc = {
            'timeline': {'stages': [{'stage_id': s['stage_id'], 'label': s['label'],
                                     'day_start': 0, 'days': 4}
                                    for s in self.project.stages],
                         'candidates': []},
            'stages': [{'stage_id': 'myeloid', 'parameters': [
                {'parameter_id': 'mcsf_ng_ml', 'label': 'M-CSF concentration', 'unit': 'ng/mL',
                 'recommended_value': 80, 'control_value': 50, 'changed': True,
                 'provenance': 'adapted', 'hypothesis_ref': 'H01'}]}],
            'hypothesis_ledger': [], 'gaps': [],
        }

    def test_a_stage_the_protocol_says_nothing_about_is_no_longer_empty(self):
        before = FP.plan(self.doc)
        self.assertEqual([], before['stages'][0]['inducers'], 'expansion, with no parameter')
        after = FP.plan(self.doc, baseline=self.baseline)
        rows = after['stages'][0]['inducers']
        self.assertEqual(1, len(rows))
        self.assertEqual('Y-27632 (ROCK inhibitor)', rows[0]['label'])
        self.assertTrue(rows[0]['from_baseline'])
        self.assertEqual('PUB', rows[0]['letter'])
        self.assertIn('Watanabe2007', rows[0]['refs'])

    def test_the_protocols_own_value_wins_over_the_published_one(self):
        """Two numbers for one factor is worse than one. The protocol decided;
        the baseline row for the same factor is dropped, not shown beside it."""
        after = FP.plan(self.doc, baseline=self.baseline)
        myeloid = [s for s in after['stages'] if s['stage_id'] == 'myeloid'][0]
        mcsf = [i for i in myeloid['inducers'] if i['label'].endswith('M-CSF concentration')
                or i['label'] == 'M-CSF']
        self.assertEqual(1, len(mcsf))
        self.assertEqual(80, mcsf[0]['value'], 'the protocol\'s value, not the paper\'s 100')
        self.assertFalse(mcsf[0].get('from_baseline'))

    def test_a_published_factor_with_no_dose_says_so_rather_than_looking_unset(self):
        after = FP.plan(self.doc, baseline=self.baseline)
        myeloid = [s for s in after['stages'] if s['stage_id'] == 'myeloid'][0]
        gm = [i for i in myeloid['inducers'] if i['label'] == 'GM-CSF'][0]
        self.assertEqual('gap', gm['provenance'])
        self.assertIsNone(gm['value'])
        self.assertIn('no source found gives a dose', gm['text'])
        self.assertTrue(gm['what_to_search'])

    def test_without_a_baseline_the_plan_is_exactly_what_it_was(self):
        self.assertEqual(FP.plan(self.doc), FP.plan(self.doc, baseline=None))

    def test_the_report_marks_published_rows_as_published(self):
        md = '\n'.join(FP.markdown(FP.plan(self.doc, baseline=self.baseline)))
        self.assertIn('PUB marks a factor the published route uses', md)
        self.assertIn('Watanabe2007', md)


class WiringTests(unittest.TestCase):
    def test_the_orchestrator_is_told_to_write_the_whole_route(self):
        from biosense.production import discovery as DISC
        req = DISC.build(project_id='ipsc_macrophage', runtime_mode='synthetic_demo',
                         objective='increase viable macrophage yield from iPSC')
        brief = DISC.render_brief(req, loop_dir='runs/ai-x')
        self.assertIn('cli baseline --project', brief)
        self.assertIn('not only the stage you changed', brief)
        self.assertIn('baseline_protocol.json', brief)

    def test_the_page_shows_it(self):
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertIn('id="baseSec"', html)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function renderBaseline', js)
        self.assertIn('snap.baseline_protocol', js)

    def test_the_run_reads_the_file_the_cli_wrote(self):
        """A document nothing reads is a document nobody sees."""
        import inspect
        from biosense.production import discovery_runner as DR
        src = inspect.getsource(DR._lenses)
        self.assertIn('baseline_protocol.json', src)
        self.assertIn('baseline', src)
        from biosense.production import app as APP
        self.assertIn('baseline_protocol.json', inspect.getsource(APP.DiscoveryRun))


if __name__ == '__main__':
    unittest.main()
