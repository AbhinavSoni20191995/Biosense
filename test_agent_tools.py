import copy
import unittest
from agent_tools import compile_handoff, convert_unit, effective_growth_rate

class LiteratureContractTests(unittest.TestCase):
    def setUp(self):
        self.request = {'request_id': 'synthetic-test', 'constraints': {'species': 'human'}, 'required_parameters': ['ipsc_expansion:duration']}
        self.sources = [{'id': 'TEST', 'article_type': 'research-article', 'paragraphs': [{'id': 'p0', 'text': 'Synthetic example: duration 48 h; a separate description gives 72 h.'}]}]
        self.claim = {'id': 'c1', 'protocol_id': 'test', 'parameter': 'duration', 'stage': 'ipsc_expansion', 'role': 'schedule', 'value': 48, 'unit': 'h', 'context': {'species': 'human', 'cell_origin': 'iPSC', 'target_cell': 'cardiomyocyte', 'cell_line': 'test', 'culture_format': 'test', 'medium': None, 'time_origin': 'stage_start', 'time_window': None}, 'condition_signature': 'same-condition', 'evidence': {'source_id': 'TEST', 'paragraph_id': 'p0', 'quote': 'duration 48 h'}, 'notes': 'Synthetic fixture, not biological evidence.'}

    def run_claims(self, claims):
        return compile_handoff(self.request, {'claims': claims}, self.sources)

    def test_missing_kinetics_stay_missing(self):
        self.request['required_parameters'].append('ipsc_expansion:mu_max')
        r = self.run_claims([self.claim])
        self.assertEqual(r['missing_required_parameters'], ['ipsc_expansion:mu_max'])
        self.assertFalse(r['ready_for_simulation'])

    def test_fabricated_quote_rejected(self):
        self.claim['evidence']['quote'] = 'growth was optimal'
        self.assertEqual(len(self.run_claims([self.claim])['rejected_claims']), 1)

    def test_different_values_conflict(self):
        other = copy.deepcopy(self.claim); other['id'] = 'c2'; other['value'] = 72; other['evidence']['quote'] = '72 h'
        r = self.run_claims([self.claim, other])
        self.assertEqual(len(r['conflicts']), 1)
        self.assertEqual(r['candidate_parameters'], [])

    def test_distinct_arms_are_not_a_conflict(self):
        other = copy.deepcopy(self.claim); other['id'] = 'c2'; other['value'] = 72; other['condition_signature'] = 'different-arm'; other['evidence']['quote'] = '72 h'
        self.assertEqual(self.run_claims([self.claim, other])['conflicts'], [])

    def test_unit_equivalent_values_agree(self):
        other = copy.deepcopy(self.claim); other['id'] = 'c2'; other['value'] = 2; other['unit'] = 'day'
        self.assertEqual(self.run_claims([self.claim, other])['conflicts'], [])

    def test_species_mismatch_excluded(self):
        self.claim['context']['species'] = 'mouse'
        self.assertEqual(len(self.run_claims([self.claim])['excluded_claims']), 1)

    def test_hard_limit_not_clipped(self):
        self.request['constraints']['parameter_limits'] = {'ipsc_expansion:duration': {'unit': 'day', 'max': 1}}
        r = self.run_claims([self.claim])
        self.assertEqual(r['claims'][0]['value'], 48)
        self.assertEqual(len(r['excluded_claims']), 1)

    def test_geometry_conversion_refused(self):
        with self.assertRaises(ValueError): convert_unit(1000, 'cells/cm2', 'cells/mL')

    def test_per_cell_rate_not_volumetric(self):
        with self.assertRaises(ValueError): convert_unit(.5, 'g/L/day', 'g/cell/day')

    def test_growth_derivation_is_not_mu_max(self):
        r = effective_growth_rate(2, 1)
        self.assertEqual(r['parameter'], 'effective_net_growth_rate')
        self.assertAlmostEqual(r['value'], .693147, places=5)

    def test_duplicate_ids_rejected(self):
        self.assertEqual(len(self.run_claims([self.claim, self.claim])['rejected_claims']), 1)

    def test_retracted_article_blocked(self):
        self.sources[0]['article_type'] = 'retraction'
        self.assertEqual(len(self.run_claims([self.claim])['rejected_claims']), 1)

if __name__ == '__main__': unittest.main()
