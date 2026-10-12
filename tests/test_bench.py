"""What round 1 needs at the bench, and who the catalogue number came from.

An arms table is not a protocol. "A2: GM-CSF 50 ng/mL replacing M-CSF" does
not tell anybody which medium, made up how, which reagent at what stock
concentration, seeded how, fed how often, or what ruins the run — and those
are the questions between a recommendation and an experiment.

The rule worth testing hardest is the sourcing one. A supplier and a catalogue
number look like facts, are rarely checked before ordering, and a wrong one
costs a week, so they may only be recorded where a source states them. These
check that the refusal is real rather than a warning in a docstring.
"""
import copy
import unittest

from biosense import contracts as K
from biosense import projects as PJ
from biosense.evidence import bench as BN


def draft(**kw):
    d = copy.deepcopy(BN.TEMPLATE)
    d.update(kw)
    return d


def reagent(**kw):
    r = {'name': 'M-CSF', 'role': 'drives myeloid commitment', 'sourcing': 'your_lab',
         'provenance': 'design_choice'}
    r.update(kw)
    return draft(reagents=[r])


class SourcingTests(unittest.TestCase):
    def setUp(self):
        self.project = PJ.load('ipsc_macrophage')

    def build(self, d):
        return BN.build(d, project=self.project, run_id='ai-x', round_number=1)

    def test_a_catalogue_number_with_nothing_behind_it_is_refused(self):
        """The most expensive invention this system could make: it looks like a
        fact, nobody checks it before ordering, and a wrong one costs a week."""
        with self.assertRaises(K.ContractError) as e:
            self.build(reagent(sourcing='from_source', supplier='PeproTech',
                               catalogue='300-25'))
        self.assertIn('nothing cited', str(e.exception))
        self.assertIn('costs a week', str(e.exception))

    def test_a_supplier_cannot_be_smuggled_in_under_your_lab(self):
        with self.assertRaisesRegex(K.ContractError, 'sourcing "your_lab"'):
            self.build(reagent(supplier='PeproTech'))

    def test_a_sourced_reagent_keeps_the_supplier_and_the_sentence(self):
        d = self.build(reagent(
            sourcing='from_source', supplier='PeproTech', catalogue='300-25',
            provenance='reported',
            sources=[{'ref': 'vanWilgenburg2013',
                      'quote': 'M-CSF (PeproTech, 300-25) at 100 ng/ml', 'locator': 'Methods'}]))
        r = d['reagents'][0]
        self.assertEqual(('PeproTech', '300-25', 'from_source'),
                         (r['supplier'], r['catalogue'], r['sourcing']))
        self.assertEqual('vanWilgenburg2013', r['sources'][0]['ref'])
        self.assertEqual(1, d['coverage']['sourced'])

    def test_claiming_a_source_without_naming_one_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'neither supplier nor catalogue'):
            self.build(reagent(sourcing='from_source', provenance='reported',
                               sources=[{'ref': 'vanWilgenburg2013',
                                         'quote': 'M-CSF at 100 ng/ml'}]))

    def test_an_unsourced_reagent_is_normal_and_says_what_to_use(self):
        d = self.build(reagent(note='Use your own qualified stock and record the lot.'))
        r = d['reagents'][0]
        self.assertEqual(('your_lab', None, None), (r['sourcing'], r['supplier'], r['catalogue']))
        self.assertEqual(0, d['coverage']['sourced'])

    def test_a_reported_reagent_or_medium_names_its_source(self):
        with self.assertRaisesRegex(K.ContractError, 'reported reagent names the source'):
            self.build(reagent(provenance='reported'))
        d = draft()
        d['media'][0]['sources'] = []
        with self.assertRaisesRegex(K.ContractError, 'reported medium names the source'):
            self.build(d)


class CompletenessTests(unittest.TestCase):
    def setUp(self):
        self.project = PJ.load('ipsc_macrophage')

    def test_the_template_builds_and_counts_what_it_covers(self):
        d = BN.build(BN.TEMPLATE, project=self.project, round_number=1,
                     culture_format='six_well')
        K.require_valid('bench_setup', d)
        self.assertEqual({'stages_total': 4, 'stages_with_medium': 1, 'reagents': 1,
                          'sourced': 0, 'gaps': 0}, d['coverage'])
        self.assertEqual('six_well', d['culture_format'])

    def test_a_setup_with_no_handling_is_an_arms_table_again(self):
        with self.assertRaisesRegex(K.ContractError, 'arms table again'):
            BN.build(draft(handling=[]), project=self.project)

    def test_a_rule_with_no_reason_is_folklore_and_is_refused(self):
        d = draft(dont=[{'rule': 'Do not re-freeze a thawed cytokine aliquot.'}])
        with self.assertRaisesRegex(K.ContractError, 'why'):
            BN.build(d, project=self.project)

    def test_at_least_one_thing_to_do_or_avoid_is_required(self):
        with self.assertRaisesRegex(K.ContractError, 'what goes wrong if'):
            BN.build(draft(do=[], dont=[]), project=self.project)

    def test_a_stage_the_project_does_not_have_is_refused(self):
        d = draft()
        d['media'][0]['stage_id'] = 'maturation'
        with self.assertRaisesRegex(K.ContractError, 'expansion'):
            BN.build(d, project=self.project)

    def test_two_media_for_one_stage_are_refused(self):
        d = draft()
        d['media'].append(dict(d['media'][0]))
        with self.assertRaisesRegex(K.ContractError, 'two media'):
            BN.build(d, project=self.project)

    def test_the_written_section_carries_the_sourcing_and_the_handling(self):
        md = '\n'.join(BN.markdown(BN.build(BN.TEMPLATE, project=self.project)))
        self.assertIn('### Media', md)
        self.assertIn('your own qualified stock (no source names one)', md)
        self.assertIn('### How it is run', md)
        self.assertIn('Do not re-freeze', md)
        self.assertIn('Storage:', md)


class WiringTests(unittest.TestCase):
    def test_the_orchestrator_is_told_to_write_it_and_not_to_invent_a_catalogue(self):
        from biosense.production import discovery as DISC
        req = DISC.build(project_id='ipsc_macrophage', runtime_mode='synthetic_demo',
                         objective='increase viable macrophage yield from iPSC')
        brief = DISC.render_brief(req, loop_dir='runs/ai-x')
        self.assertIn('An arms table is not a protocol', brief)
        self.assertIn('cli bench --project', brief)
        self.assertIn('may only be recorded where a source states it', brief)
        self.assertIn('a rule with no reason is folklore', brief)

    def test_the_run_reads_it_and_the_page_shows_it(self):
        import inspect
        from biosense.production import app as APP
        from biosense.production import discovery_runner as DR
        self.assertIn('bench_setup.json', inspect.getsource(DR._lenses))
        self.assertIn('bench_setup.json', inspect.getsource(APP.DiscoveryRun))
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function renderBench', js)
        self.assertIn('snap.bench_setup', js)

    def test_the_bench_copy_carries_the_reagents(self):
        from biosense.production import lab_protocol as LP
        from tests.test_lab_protocol import PROTOCOL, text_of
        b = BN.build(BN.TEMPLATE, project=PJ.load('ipsc_macrophage'))
        text = '\n'.join(text_of(LP.tobytes(dict(PROTOCOL, bench_setup=b))))
        self.assertIn('At the bench', text)
        self.assertIn('X-VIVO 15', text)
        self.assertIn('your own qualified stock — no source read names one', text)
        self.assertIn('Do not re-freeze a thawed cytokine aliquot.', text)


class StageDaysTests(unittest.TestCase):
    """A stage length is a fact about somebody's process, not about this
    software, so every plan drawing the same fixed day ranges was wrong for
    most people reading it."""

    def setUp(self):
        import os
        import shutil
        import tempfile
        from biosense import workspace as WS
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get('BIOSENSE_PRIVATE_DATA')
        os.environ['BIOSENSE_PRIVATE_DATA'] = self.tmp
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(lambda: os.environ.pop('BIOSENSE_PRIVATE_DATA', None)
                        if self._prev is None
                        else os.environ.__setitem__('BIOSENSE_PRIVATE_DATA', self._prev))
        self.identity = WS.local_identity()

    def set(self, days, stage='expansion'):
        from biosense.production import project_builder as PB
        return PB.set_stage_days(self.identity, 'ipsc_macrophage', stage, days)

    def test_a_person_can_say_how_long_their_expansion_runs(self):
        doc, stage = self.set(7)
        self.assertEqual(7.0, stage['default_days'])
        self.assertNotEqual('0.1.0', doc['version'], 'the change bumps the project')
        from biosense.production import project_builder as PB
        again = PB.load_for(self.identity, 'ipsc_macrophage')
        self.assertEqual(7.0, [s for s in again.stages
                               if s['stage_id'] == 'expansion'][0]['default_days'],
                         'and it survives being reloaded')

    def test_the_project_s_own_bounds_still_hold(self):
        with self.assertRaisesRegex(K.ContractError, 'below this stage'):
            self.set(0.5)
        with self.assertRaisesRegex(K.ContractError, 'above this stage'):
            self.set(60)

    def test_a_stage_or_a_length_that_is_not_one_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'no stage'):
            self.set(5, stage='maturation')
        with self.assertRaisesRegex(K.ContractError, 'number of days'):
            self.set('a fortnight')
        with self.assertRaisesRegex(K.ContractError, 'not a stage length'):
            self.set(0)

    def test_the_committed_template_is_never_edited(self):
        import json
        before = json.loads((K.ROOT / 'projects' / 'ipsc_macrophage.json').read_text())
        self.set(7)
        after = json.loads((K.ROOT / 'projects' / 'ipsc_macrophage.json').read_text())
        self.assertEqual(before, after, 'the change goes to the workspace copy')

    def test_the_page_offers_it(self):
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function stageDays', js)
        self.assertIn('/stages/', js)
        self.assertIn('not a recommendation from the', js)


if __name__ == '__main__':
    unittest.main()
