"""The experiment past the apparatus: what it may lift, and what it may never.

Round 1 is designed for the vessel the person has. The breakthrough design is
the one the orchestrator would run if the apparatus were not the limit. These
hold the rules that keep it honest: only the apparatus is lifted, quantities
keep their provenance, it has one control, its expected outcome is a
judgement, and it is never runnable or approved here.
"""
import copy
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.evidence import breakthrough as BT
from biosense.production import discovery as DISC
from biosense.production import pdf as PDF
from biosense.production import section_report as SR

REQUEST = {'project_id': 'ipsc_macrophage',
           'objective': 'More alveolar-like macrophages from iPSC in the stirred tank.',
           'culture_format': 'stirred_tank',
           'process_constraints': {'parameter_bounds': [
               {'parameter_id': 'mcsf_ng_ml', 'maximum': 150, 'source': 'safety_constraint'}]}}


def draft(**changes):
    d = copy.deepcopy(BT.TEMPLATE)
    d.update(changes)
    return d


class BuildTests(unittest.TestCase):
    def test_the_template_builds_as_a_concept_that_is_never_runnable_here(self):
        doc = BT.build(draft(), request=REQUEST)
        K.require_valid('breakthrough_design', doc)
        self.assertEqual('concept', doc['status'])
        self.assertFalse(doc['runnable_on_current_apparatus'])
        self.assertFalse(doc['ready_for_wet_lab'])
        self.assertEqual(REQUEST['objective'], doc['aim'], 'the aim is the request\'s')
        self.assertEqual('judgement', doc['expected']['estimate_type'])
        self.assertEqual([('Maturation on DLL4 carriers', 'DLL4 coating density')],
                         BT.gaps(doc))

    def test_the_aim_the_qc_limits_and_safety_bounds_are_always_kept(self):
        kept = [k['what'] for k in BT.build(draft(), request=REQUEST)['kept']]
        self.assertIn('the aim and its target', kept)
        self.assertIn('the QC limits', kept)
        self.assertIn('mcsf_ng_ml max 150', kept)

    def test_only_the_apparatus_is_lifted(self):
        for kind in ('target', 'qc', 'safety'):
            d = draft()
            d['constraints_lifted'][0]['kind'] = kind
            with self.assertRaisesRegex(K.ContractError, 'Only the apparatus is lifted'):
                BT.build(d, request=REQUEST)
        d = draft()
        d['constraints_lifted'][0]['refers_to'] = 'bound:mcsf_ng_ml'
        with self.assertRaisesRegex(K.ContractError, 'safety constraint. It stays'):
            BT.build(d, request=REQUEST)
        with self.assertRaisesRegex(K.ContractError, 'lifts nothing is round 1 again'):
            BT.build(draft(constraints_lifted=[]), request=REQUEST)

    def test_every_quantity_keeps_its_provenance(self):
        d = draft()
        d['stages'][1]['conditions'][1]['value'] = 2
        with self.assertRaisesRegex(K.ContractError, 'A gap carries no number'):
            BT.build(d, request=REQUEST)
        d = draft()
        d['stages'][0]['conditions'][0]['refs'] = []
        with self.assertRaisesRegex(K.ContractError, 'reported and cites nothing'):
            BT.build(d, request=REQUEST)

    def test_exactly_one_control(self):
        d = draft()
        d['arms'][1]['control'] = True
        with self.assertRaisesRegex(K.ContractError, 'exactly one arm is the control'):
            BT.build(d, request=REQUEST)

    def test_confidence_is_capped_and_a_design_nobody_ran_does_not_overclaim(self):
        with self.assertRaisesRegex(K.ContractError, 'capped at moderate'):
            BT.build(draft(confidence='high'), request=REQUEST)
        with self.assertRaisesRegex(K.ContractError, 'will raise'):
            BT.build(draft(closest_to_aim='This will raise the alveolar fraction a lot.'),
                     request=REQUEST)


class WiringTests(unittest.TestCase):
    def test_it_is_the_orchestrator_s_last_design_step(self):
        req = DISC.build(project_id='ipsc_macrophage', runtime_mode='synthetic_demo',
                         objective='increase viable macrophage yield from iPSC',
                         culture_format='stirred_tank')
        brief = ' '.join(DISC.render_brief(req, loop_dir='runs/ai-x').split())
        self.assertIn('Then design past the apparatus', brief)
        self.assertIn('cli breakthrough --project ipsc_macrophage', brief)
        self.assertIn('`culture_format` = culture format: stirred_tank', brief)

    def test_it_exports_from_the_run_s_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            K.write_json_atomic(d / 'discovery_request.json', REQUEST)
            K.write_json_atomic(d / BT.NAME, BT.build(draft(), request=REQUEST))
            t = ' '.join(PDF.text_of(SR.tobytes('breakthrough', d, 'pdf', run_id='x')))
            self.assertIn('CONCEPT — not runnable on the current apparatus', t)
            self.assertIn('The constraints it lifts', t)
            empty = Path(tmp) / 'none'
            empty.mkdir()
            self.assertIn('no breakthrough design yet',
                          ' '.join(PDF.text_of(SR.tobytes('breakthrough', empty, 'pdf'))))


if __name__ == '__main__':
    unittest.main()
