"""A project built on the base reactor gets terms for its own biology, one at a time.

A run proposes a response term for a lever the reactor has no equation for — a
shape and constants read from cited claims — and plays it, labelled DE NOVO. It
joins the project only when a person adds it, and it stays what it is: proposed
by an agent, uncalibrated.
"""
import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from biosense import contracts as K
from biosense import parameters as PR
from biosense import projects as PJ
from biosense import workspace as WS
from biosense.data import roots as DR
from biosense.evidence import cli as EC
from biosense.production import project_builder as PB

LEVER = 'gmcsf_maturation_ng_ml'


def macrophage_project():
    return PB.quick_build(project_id='my_macrophages', name='My iPSC macrophages',
                          species='human', starting_cell='iPSC', target_cell='macrophage')


class ProposedTermTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {DR.PRIVATE_ENV: str(self.tmp / 'private')})
        patcher.start(); self.addCleanup(patcher.stop)
        self.addCleanup(PR.forget_local, LEVER)
        self.projects = self.tmp / 'projects'
        self.projects.mkdir()
        self.doc = macrophage_project()
        (self.projects / 'my_macrophages.json').write_text(json.dumps(self.doc))
        self.terms = copy.deepcopy(EC.TERM_TEMPLATE['terms'])

    def test_a_proposed_term_is_applied_in_memory_and_the_project_is_untouched(self):
        project = PJ.load('my_macrophages', str(self.projects))
        before = json.dumps(project.doc, sort_keys=True)
        with_terms, pids = PB.with_proposed_terms(project, self.terms, run_id='abc123')
        self.assertEqual([LEVER], pids)
        q = with_terms.parameter(LEVER)
        self.assertEqual('de_novo_ai', q.simulator_coverage)
        self.assertEqual('ai_proposed', q.response_model['origin'])
        self.assertIn('abc123', q.response_model['proposed_by'])
        self.assertEqual(before, json.dumps(project.doc, sort_keys=True))
        self.assertNotIn(LEVER, json.loads((self.projects / 'my_macrophages.json').read_text())
                         ['parameters'][-1]['parameter_id'])

    def test_a_term_with_no_citation_is_a_gap_not_a_term(self):
        project = PJ.load('my_macrophages', str(self.projects))
        self.terms[0]['response_model']['references'] = []
        with self.assertRaisesRegex(K.ContractError, 'cites none'):
            PB.with_proposed_terms(project, self.terms)

    def test_a_calibrated_parameter_keeps_its_fitted_term(self):
        project = PJ.load('ipsc_macrophage')
        term = dict(self.terms[0], parameter_id='mcsf_ng_ml')
        with self.assertRaisesRegex(K.ContractError, 'calibrated term'):
            PB.with_proposed_terms(project, [term])

    def test_the_cli_proposes_then_plays_the_term_on_the_base_reactor(self):
        draft = self.tmp / 'terms.draft.json'
        draft.write_text(json.dumps({'terms': self.terms}))
        out = self.tmp / 'proposed_terms.json'
        with mock.patch('sys.stdout'):
            self.assertEqual(0, EC.main(['term', '--project', 'my_macrophages',
                                         '--projects-dir', str(self.projects),
                                         '--draft', str(draft), '--run-id', 'abc123',
                                         '--out', str(out)]))
        doc = json.loads(out.read_text())
        self.assertEqual('proposed_terms', doc['kind'])
        self.assertEqual('DE NOVO · UNCALIBRATED', doc['terms'][0]['description']['badge'])
        sim = self.tmp / 'simulation.json'
        with mock.patch('sys.stdout'):
            self.assertEqual(0, EC.main(['simulate', '--project', 'my_macrophages',
                                         '--projects-dir', str(self.projects),
                                         '--terms', str(out), '--set', f'{LEVER}=50',
                                         '--out', str(sim)]))
        g = json.loads(sim.read_text())['genotype_simulation']
        self.assertTrue(g['stand_in'])
        self.assertTrue(g['stand_in_note'].startswith('BUILT ON THE BASE REACTOR'))
        # The project's maturation stage is played in the base reactor's myeloid
        # stage, and the text says so; the term's origin travels with it.
        self.assertIn("maturation (played in the base reactor's myeloid stage)", g['assumption'])
        self.assertIn('DE NOVO', g['assumption'])
        self.assertEqual('myeloid', g['genotype']['effects'][0]['stage'])
        self.assertGreater(g['genotype']['effects'][0]['diff_ratio'], 1.0)

    def test_a_person_adds_the_term_and_it_stays_ai_proposed(self):
        me = WS.WorkspaceIdentity('scientist@example.org', authenticated=True)
        PB.save(me, self.doc)
        doc, pid = PB.adopt_term(me, 'my_macrophages', self.terms[0], accepted_by='Dr A',
                                 from_run='abc123')
        self.assertEqual(LEVER, pid)
        mine = PB.load_for(me, 'my_macrophages')
        rm = mine.parameter(LEVER).response_model
        self.assertEqual(('de_novo_ai', 'ai_proposed', 'Dr A'),
                         (mine.coverage(LEVER), rm['origin'], rm['accepted_by']))
        self.assertIn('abc123', rm['proposed_by'])
        self.assertNotEqual(self.doc['version'], doc['version'], 'a change bumps the version')


if __name__ == '__main__':
    unittest.main()
