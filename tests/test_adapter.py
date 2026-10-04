import copy
import json
import unittest

import jsonschema

from biosense import contracts as K
from biosense import simulation as SIM
from biosense.adapters.literature import assemble_scenario
from tests.helpers import ROOT, handoff, load


def selection(mode='synthetic_demo', **kw):
    s = copy.deepcopy(load('selection.synthetic.json'))
    s['mode'] = mode
    s.update(kw)
    return s


def pmap(sc):
    return {p['id']: p for p in sc['parameters']}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.h = handoff()
        self.assumptions = load('assumptions.synthetic.json')

    def test_synthetic_scenario_is_complete_and_labelled(self):
        sc = assemble_scenario(self.h, selection(), self.assumptions)
        K.require_valid('scenario', sc)
        self.assertEqual(SIM.validate_scenario(sc)['status'], 'runnable')
        self.assertIn('SYNTHETIC', sc['label'])
        self.assertFalse(sc['validation']['biologically_calibrated'])

    def test_committed_example_matches_adapter_output(self):
        sc = assemble_scenario(self.h, selection(), self.assumptions)
        self.assertEqual(sc, load('scenario.synthetic.json'))

    def test_5_days_normalized_to_hours_with_original_kept(self):
        p = pmap(assemble_scenario(self.h, selection(), self.assumptions))['schedule.ipsc_expansion.duration']
        self.assertEqual((p['original_value'], p['original_unit'], p['value'], p['unit']), (5, 'day', 120.0, 'h'))
        self.assertEqual(p['provenance_type'], 'reported')
        self.assertEqual(p['source_claim_ids'], ['expansion_duration'])

    def _density_case(self, value, unit, with_volume=True, mode='synthetic_demo'):
        h = copy.deepcopy(self.h)
        dens = copy.deepcopy(h['claims'][0])
        dens.update(id='dens', parameter='initial_cell_density', role='initial_condition', value=value, unit=unit)
        h['claims'].append(dens)
        a = copy.deepcopy(self.assumptions)
        a['assumptions'] = [x for x in a['assumptions'] if x['model_input'] != 'initial.R']
        if with_volume:
            a['assumptions'].append({'id': 'A-vol', 'model_input': None, 'value': 100, 'unit': 'mL',
                                     'rationale': 'SYNTHETIC working volume operand'})
        sel = selection(mode, derived_inputs=[{'model_input': 'initial.R', 'formula': 'density_times_volume',
                                               'density': {'claim_id': 'dens'}, 'volume': {'assumption_id': 'A-vol'}}])
        return assemble_scenario(h, sel, a)

    def test_5_density_to_count_uses_declared_volume(self):
        sc = self._density_case(120000, 'cells/mL')
        p = pmap(sc)['initial.R']
        self.assertAlmostEqual(p['value'], 1.2e7)
        self.assertEqual(p['provenance_type'], 'derived')
        self.assertEqual((p['source_claim_ids'], p['assumption_ids']), (['dens'], ['A-vol']))
        self.assertTrue(p['depends_on_synthetic'])
        self.assertIn('working_volume', p['transformation']['formula'])
        self.assertAlmostEqual(pmap(self._density_case(1.2e8, 'cells/L'))['initial.R']['value'], 1.2e7)

    def test_5_density_without_volume_or_with_surface_units_is_refused(self):
        for sc, why in ((self._density_case(120000, 'cells/mL', with_volume=False), 'not found'),
                        (self._density_case(2e4, 'cells/cm2'), 'surface densities')):
            gap = [g for g in sc['gaps'] if g['model_input'] == 'initial.R'][0]
            self.assertEqual(gap['reason'], 'invalid_source')
            self.assertIn(why, gap['detail'])

    def test_5_synthetic_volume_operand_refused_in_evidence_mode(self):
        sc = self._density_case(120000, 'cells/mL', mode='evidence_based')
        gap = [g for g in sc['gaps'] if g['model_input'] == 'initial.R'][0]
        self.assertIn('evidence_based', gap['detail'])

    def test_6_evidence_mode_blocks_with_gaps(self):
        sc = assemble_scenario(self.h, load('selection.evidence.json'))
        self.assertEqual(sc['validation']['status'], 'blocked')
        self.assertEqual(SIM.validate_scenario(sc)['status'], 'blocked')
        ids = {g['literature_parameter_id'] for g in sc['gaps']}
        self.assertIn('ipsc_expansion:maximum_growth_rate', ids)
        self.assertIn('cardiac_differentiation:differentiation_transition_rate', ids)
        r, rows = SIM.run_simulation(sc, json.loads((ROOT / 'examples/baseline.evidence.json').read_text()))
        self.assertEqual(r['status'], 'blocked')
        self.assertIsNone(r['endpoint_state'])
        self.assertEqual(rows, [])

    def test_6_conflicting_claim_cannot_enter_silently(self):
        h = copy.deepcopy(self.h)
        h['conflicts'].append({'protocol_id': 'Laco2020_FR202_integrated', 'stage': 'ipsc_expansion',
                               'parameter': 'duration', 'claim_ids': ['expansion_duration'], 'resolution': 'unresolved'})
        sc = assemble_scenario(h, selection(), self.assumptions)
        gap = [g for g in sc['gaps'] if g['model_input'] == 'schedule.ipsc_expansion.duration'][0]
        self.assertEqual(gap['reason'], 'invalid_source')
        self.assertIn('unresolved conflict', gap['detail'])
        res = [{'claim_ids': ['expansion_duration'], 'chosen_claim_id': 'expansion_duration',
                'rationale': 'test resolution', 'resolved_by': 'test'}]
        sc2 = assemble_scenario(h, selection(conflict_resolutions=res), self.assumptions)
        p = pmap(sc2)['schedule.ipsc_expansion.duration']
        self.assertEqual(p['resolution']['rationale'], 'test resolution')

    def test_6_reagent_conflict_kept_as_unsupported_control(self):
        sc = assemble_scenario(self.h, selection(), self.assumptions)
        u = {x['claim_id']: x for x in sc['unsupported_controls']}
        self.assertEqual(set(u), {'CHIR_methods', 'CHIR_results'})
        self.assertEqual(u['CHIR_methods']['conflict_status'], 'unresolved')

    def test_6_rejected_excluded_and_cross_protocol_claims_refused(self):
        h = copy.deepcopy(self.h)
        h['excluded_claims'].append({'claim_id': 'expansion_duration', 'reasons': ['test']})
        sc = assemble_scenario(h, selection(), self.assumptions)
        self.assertIn('excluded', [g for g in sc['gaps'] if g['model_input'] == 'schedule.ipsc_expansion.duration'][0]['detail'])
        sc = assemble_scenario(self.h, selection(protocol_id='Other2024'), self.assumptions)
        self.assertIn('never mixed', [g for g in sc['gaps'] if g['model_input'] == 'schedule.ipsc_expansion.duration'][0]['detail'])

    def test_effective_growth_is_not_silently_mu_R(self):
        h = copy.deepcopy(self.h)
        g = copy.deepcopy(h['claims'][0])
        g.update(id='eff', parameter='effective_net_growth_rate', role='kinetic_parameter', value=0.5, unit='1/day')
        h['claims'].append(g)
        a = copy.deepcopy(self.assumptions)
        a['assumptions'] = [x for x in a['assumptions'] if x['model_input'] != 'ipsc_expansion.mu_R']
        sel = selection()
        sel['claim_selections'].append({'model_input': 'ipsc_expansion.mu_R', 'claim_id': 'eff'})
        sc = assemble_scenario(h, sel, a)
        gap = [x for x in sc['gaps'] if x['model_input'] == 'ipsc_expansion.mu_R'][0]
        self.assertIn('not an accepted meaning', gap['detail'])

    def test_7_synthetic_assumption_never_becomes_reported(self):
        sc = assemble_scenario(self.h, selection(), self.assumptions)
        for p in sc['parameters']:
            if p['assumption_ids']:
                self.assertEqual(p['provenance_type'], 'synthetic_assumption')
                self.assertEqual(p['source_claim_ids'], [])
        ev = assemble_scenario(self.h, load('selection.evidence.json'), self.assumptions)
        self.assertTrue(all(p['provenance_type'] == 'reported' for p in ev['parameters']))
        self.assertTrue(any('not permitted in evidence_based' in g.get('detail', '') for g in ev['gaps']))
        forged = copy.deepcopy(sc)
        forged['mode'] = 'evidence_based'
        self.assertEqual(SIM.validate_scenario(forged)['status'], 'invalid')
        forged = copy.deepcopy(sc)
        p = next(p for p in forged['parameters'] if p['provenance_type'] == 'synthetic_assumption')
        p['provenance_type'] = 'reported'
        self.assertEqual(SIM.validate_scenario(forged)['status'], 'invalid')

    def test_unknown_model_input_refused(self):
        a = copy.deepcopy(self.assumptions)
        a['assumptions'].append({'id': 'A-x', 'model_input': 'cardiac_differentiation.CHIR_response', 'value': 1, 'unit': '1/h'})
        sc = assemble_scenario(self.h, selection(), a)
        self.assertTrue(sc['errors'])
        self.assertEqual(SIM.validate_scenario(sc)['status'], 'invalid')

    def test_12_literature_handoff_unchanged_and_valid(self):
        before = copy.deepcopy(self.h)
        assemble_scenario(self.h, selection(), self.assumptions)
        self.assertEqual(self.h, before)
        schema = json.loads((ROOT / 'output.schema.json').read_text())
        jsonschema.validate(self.h, schema)
        self.assertFalse(self.h['ready_for_simulation'])
        self.assertEqual(self.h['selected_parameters'], [])


if __name__ == '__main__':
    unittest.main()
