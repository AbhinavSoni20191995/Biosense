"""Bioinformatics as one agent, one specialist per kind of data, or one per stage.

The same pattern as the literature search: the person chooses on the request,
the brief dispatches scoped tasks to the same agent in parallel, each writes in
its own folder, and the run page shows each specialist's notes under its name.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from biosense import contracts as K
from biosense import projects as PJ
from biosense.production import discovery as DISC
from tests.test_discovery import a_request


class RequestTests(unittest.TestCase):
    def test_one_agent_is_the_default(self):
        req = a_request()
        self.assertEqual('single', req['bioinformatics_mode'])
        self.assertEqual([], req['bioinformatics_specialists'])
        brief = DISC.render_brief(req, loop_dir='ai-x')
        self.assertIn('title: "bioinformatics-it1"', brief)
        self.assertNotIn('SPECIALIST:', brief)

    def test_by_modality_sends_the_four_common_specialists_unless_chosen(self):
        req = a_request(bioinformatics_mode='by_modality')
        self.assertEqual(list(DISC.DEFAULT_BIO_SPECIALISTS), req['bioinformatics_specialists'])
        brief = DISC.render_brief(req, loop_dir='ai-x')
        for k in DISC.DEFAULT_BIO_SPECIALISTS:
            self.assertIn(f'title: "bioinformatics-{k}"', brief)
            self.assertIn(f'`SPECIALIST: {k}`', brief)
            self.assertIn(f'ai-x/bioinformatics/{k}/', brief)
        self.assertNotIn('bioinformatics-epigenomics', brief)
        self.assertIn('reconcile them', brief)
        self.assertIn('Bioinformatics: one specialist per kind of data', DISC.summarise(req))

        chosen = a_request(bioinformatics_mode='by_modality',
                           bioinformatics_specialists=['proteomics', 'cytometry'])
        self.assertEqual(['cytometry', 'proteomics'], chosen['bioinformatics_specialists'])

    def test_a_named_dataset_brings_its_specialist(self):
        with mock.patch('biosense.data.registry.load',
                        return_value={'modality': 'atac_seq'}):
            req = a_request(bioinformatics_mode='by_modality', dataset_ids=['GSE155719'])
        self.assertIn('epigenomics', req['bioinformatics_specialists'])

    def test_by_stage_sends_one_specialist_per_stage(self):
        req = a_request(bioinformatics_mode='by_stage')
        brief = DISC.render_brief(req, loop_dir='ai-x')
        for st in list(PJ.load('ipsc_macrophage').stages)[:DISC.MAX_STAGE_SHARDS]:
            self.assertIn(f'title: "bioinformatics-{st["stage_id"]}"', brief)
            self.assertIn(f'`STAGE: {st["stage_id"]}`', brief)

    def test_bad_choices_are_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'bioinformatics_mode'):
            a_request(bioinformatics_mode='swarm')
        with self.assertRaisesRegex(K.ContractError, 'unknown bioinformatics specialist'):
            a_request(bioinformatics_mode='by_modality', bioinformatics_specialists=['metabolomics'])
        with self.assertRaisesRegex(K.ContractError, 'only when'):
            a_request(bioinformatics_specialists=['proteomics'])


class RunPageTests(unittest.TestCase):
    def test_each_specialist_s_notes_show_under_its_name(self):
        from biosense.production import app as APP
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        run = APP.DiscoveryRun('b' * 16, {'request_id': 'r', 'project_id': 'p',
                                          'objective': 'o'}, d, 'local_real_ai')
        (d / 'bioinformatics' / 'proteomics').mkdir(parents=True)
        (d / 'bioinformatics' / 'proteomics' / 'insights.md').write_text('- CSF1R: receptor')
        run.scan_artifacts(force=True)
        sp = run.bioinformatics['specialists']
        self.assertEqual(['proteomics'], [s['name'] for s in sp])
        self.assertEqual('Proteomics and secretome', sp[0]['label'])
        self.assertIn('CSF1R', sp[0]['text'])


class AgentAndFormTests(unittest.TestCase):
    def test_the_agent_knows_each_scope_and_the_form_offers_the_choice(self):
        prompt = (K.ROOT / 'discovery_loop' / 'agents' / 'bioinformatics' / 'prompt.md').read_text()
        self.assertIn('SPECIALIST: <name>', prompt)
        for k in DISC.BIO_SPECIALISTS:
            self.assertIn(f'**{k}**', prompt)
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertIn('id="bioMode"', html)
        for k in DISC.BIO_SPECIALISTS:
            self.assertIn(f'value="{k}"', html)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('bioinformatics_specialists', js)


if __name__ == '__main__':
    unittest.main()
