"""Every condition a run surfaced, as options to design a round from.

A protocol can only carry a dose for a parameter the project has, which left a
person looking at an empty stage with no idea what the run had actually found
was available. The catalogue gathers all of it — what the protocol carries,
what hypotheses named, what the leading protocols use, what development
suggests, what the run proposed — and sorts each by what stands behind it. A
dose appears only where a source gives one.
"""
import copy
import unittest

from biosense import contracts as K
from biosense.evidence import options as OPT

JS = (K.ROOT / 'webapp' / 'discovery.js').read_text()

PROTO = {
    'stages': [{'stage_id': 'myeloid', 'label': 'Myeloid harvest', 'parameters': [
        {'parameter_id': 'mcsf_ng_ml', 'label': 'M-CSF', 'unit': 'ng/mL',
         'recommended_value': 50, 'provenance': 'reported', 'hypothesis_ref': 'H01'},
        {'parameter_id': 'il3_ng_ml', 'label': 'IL-3', 'unit': 'ng/mL',
         'recommended_value': 25, 'provenance': 'design_choice'},
        {'parameter_id': 'agitation_rpm', 'label': 'Agitation', 'unit': 'rpm',
         'recommended_value': 60, 'provenance': 'design_choice'},
    ]}],
    'timeline': {'stages': [{'stage_id': 'myeloid', 'label': 'Myeloid harvest'}],
                 'candidates': [{'parameter_id': 'cntf_ng_ml', 'label': 'CNTF (ng/mL)',
                                 'stage_id': 'myeloid', 'candidate_value': 20,
                                 'hypothesis_id': 'H02', 'statement': 'CNTF may help.'}]},
    'hypothesis_ledger': [{'hypothesis_id': 'H01', 'status': 'proposed'},
                          {'hypothesis_id': 'H02', 'status': 'proposed'}],
    'proposed_terms': [{'parameter_id': 'tgfb_ng_ml', 'label': 'TGF-beta1', 'unit': 'ng/mL',
                        'stage': 'myeloid',
                        'description': {'summary': 'a de novo response', 'references': ['PMID:9']}}],
}
SOTA = {'references': [
    {'ref_id': 'S1', 'citation': 'Author 2013', 'is_benchmark': True,
     'key_factors': ['M-CSF', 'SCF'], 'summary': 'the EB route', 'refs': ['PMID:1']},
    {'ref_id': 'S2', 'citation': 'Author 2018', 'is_benchmark': False,
     'key_factors': ['inducible TFs'], 'summary': 'forward programming', 'refs': ['PMID:2']}]}
DEVMAP = {'ideas': [
    {'idea_id': 'D1', 'lever': 'IL-34', 'stage_id': 'myeloid', 'rationale': 'second ligand',
     'novelty': 'not found in the 2 protocol searches listed',
     'protocol_search': {'found_in_protocols': False}, 'developmental_refs': ['PMID:7']},
    {'idea_id': 'D2', 'lever': 'BMP4 pulse', 'stage_id': 'myeloid', 'rationale': 'a pulse in vivo',
     'novelty': 'used before: PMID:99', 'protocol_search': {'found_in_protocols': True},
     'developmental_refs': ['PMID:8']}]}


def cat(**kw):
    return OPT.build(copy.deepcopy(PROTO), **kw)


def flat(c):
    return {o['label']: o for g in c['groups'] for o in g['options']}


class ClassTests(unittest.TestCase):
    def test_what_the_process_needs_is_separated_from_what_it_could_use(self):
        by = flat(cat(sota=SOTA, devmap=DEVMAP))
        # Carried on reported evidence, or used by the benchmark route.
        self.assertEqual('necessary', by['M-CSF']['class'])
        self.assertEqual('necessary', by['SCF']['class'])
        # A design choice is a starting value somebody picked, not a necessity.
        self.assertEqual('potential', by['IL-3']['class'])
        self.assertEqual('potential', by['CNTF']['class'])
        self.assertEqual('potential', by['inducible TFs']['class'])
        # Nothing searched was found using it.
        self.assertEqual('experimental', by['IL-34']['class'])
        self.assertEqual('experimental', by['TGF-beta1']['class'])
        # Development suggests it and a protocol uses it: a real option.
        self.assertEqual('potential', by['BMP4 pulse']['class'])

    def test_one_entry_per_real_thing_however_it_was_named(self):
        """The protocol knows M-CSF as mcsf_ng_ml and a paper calls it M-CSF."""
        by = flat(cat(sota=SOTA))
        self.assertEqual(1, sum(1 for k in by if k == 'M-CSF'))
        m = by['M-CSF']
        self.assertEqual(50, m['value'], 'the dose the protocol carries survives the merge')
        self.assertTrue(m['registered'])

    def test_a_dose_appears_only_where_a_source_gives_one(self):
        by = flat(cat(sota=SOTA, devmap=DEVMAP))
        self.assertEqual((50, 'reported'), (by['M-CSF']['value'], by['M-CSF']['value_from']))
        self.assertEqual((20, 'hypothesis'), (by['CNTF']['value'], by['CNTF']['value_from']))
        for name in ('SCF', 'IL-34', 'inducible TFs'):
            self.assertIsNone(by[name]['value'], name)

    def test_physical_setpoints_are_not_inducers(self):
        self.assertNotIn('Agitation', flat(cat()))

    def test_a_ruled_out_hypothesis_contributes_nothing(self):
        doc = copy.deepcopy(PROTO)
        doc['hypothesis_ledger'][1]['status'] = 'contradicted'
        self.assertNotIn('CNTF', flat(OPT.build(doc)))

    def test_every_option_says_what_it_stands_on(self):
        c = cat(sota=SOTA, devmap=DEVMAP)
        for g in c['groups']:
            self.assertIn(g['class'], OPT.CLASSES)
            self.assertTrue(g['why'])
            for o in g['options']:
                self.assertTrue(o['source'], o['label'])
        self.assertEqual(c['count'], sum(len(g['options']) for g in c['groups']))

    def test_the_lenses_are_optional(self):
        self.assertTrue(cat()['count'], 'a run without the add-ons still has options')


class PageTests(unittest.TestCase):
    def test_the_plan_offers_the_conditions_as_a_menu(self):
        self.assertIn('function conditionPicker', JS)
        self.assertIn('if (cat && cat.count) wrap.append(conditionPicker(cat));', JS)
        fn = JS[JS.index('function conditionPicker'):JS.index('function runTheRound')]
        self.assertIn("el('optgroup')", fn)
        self.assertIn('no dose established', fn)
        self.assertIn('cannot carry its dose yet', fn)

    def test_the_catalogue_rides_with_the_protocol(self):
        src = (K.ROOT / 'biosense' / 'production' / 'protocol_summary.py').read_text()
        self.assertIn("'options': OPT.build(doc, devmap=devmap, sota=sota),", src)


class BriefTests(unittest.TestCase):
    def test_round_one_is_a_complete_design(self):
        from biosense.production import discovery as DISC
        req = DISC.build(project_id='ipsc_macrophage', runtime_mode='synthetic_demo',
                         objective='make macrophages at suspension scale')
        brief = DISC.render_brief(req, loop_dir='runs/ai-x')
        self.assertIn('Round 1 is a complete design', brief)
        self.assertIn('never left blank and never invented silently', brief)
        self.assertIn('register it before the run', brief)


if __name__ == '__main__':
    unittest.main()
