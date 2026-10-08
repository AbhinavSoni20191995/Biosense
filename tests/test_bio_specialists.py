"""Bioinformatics as one agent, or one specialist per fixed category.

The same pattern as the literature search: the person chooses the mode on the
request — one agent, by analysis or by process — never the specialists. The
brief dispatches one scoped task per fixed category to the same agent in
parallel, each writes in its own folder, and the run page shows each
specialist's notes under its name.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense import projects as PJ
from biosense.production import discovery as DISC
from tests.test_discovery import a_request


class RequestTests(unittest.TestCase):
    def test_one_agent_is_the_default(self):
        req = a_request()
        self.assertEqual('single', req['bioinformatics_mode'])
        brief = DISC.render_brief(req, loop_dir='ai-x')
        self.assertIn('title: "bioinformatics-it1"', brief)
        self.assertNotIn('ANALYSIS:', brief)
        self.assertNotIn('PROCESS:', brief)

    def test_by_analysis_sends_every_fixed_area(self):
        req = a_request(bioinformatics_mode='by_analysis')
        self.assertNotIn('bioinformatics_specialists', req, 'the person picks the mode only')
        brief = DISC.render_brief(req, loop_dir='ai-x')
        for k in ('expression', 'phenotype', 'proteome', 'regulation'):
            self.assertIn(f'title: "bioinformatics-{k}"', brief)
            self.assertIn(f'`ANALYSIS: {k}`', brief)
            self.assertIn(f'ai-x/bioinformatics/{k}/', brief)
        self.assertIn('reconcile them', brief)
        self.assertIn('one specialist per kind of analysis', DISC.summarise(req))

    def test_by_process_uses_fixed_areas_not_the_project_s_stages(self):
        req = a_request(bioinformatics_mode='by_process')
        brief = DISC.render_brief(req, loop_dir='ai-x')
        for k in ('expansion', 'commitment', 'differentiation', 'product'):
            self.assertIn(f'title: "bioinformatics-{k}"', brief)
            self.assertIn(f'`PROCESS: {k}`', brief)
        for st in PJ.load('ipsc_macrophage').stages:
            if st['stage_id'] not in DISC.BIO_PROCESS_AREAS:
                self.assertNotIn(f'bioinformatics-{st["stage_id"]}"', brief)

    def test_bad_choices_are_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'bioinformatics_mode'):
            a_request(bioinformatics_mode='by_modality')
        with self.assertRaises(TypeError):
            a_request(bioinformatics_specialists=['proteome'])


class RunPageTests(unittest.TestCase):
    def test_each_specialist_s_notes_show_under_its_name(self):
        from biosense.production import app as APP
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        run = APP.DiscoveryRun('b' * 16, {'request_id': 'r', 'project_id': 'p',
                                          'objective': 'o'}, d, 'local_real_ai')
        (d / 'bioinformatics' / 'proteome').mkdir(parents=True)
        (d / 'bioinformatics' / 'proteome' / 'insights.md').write_text('- CSF1R: receptor')
        run.scan_artifacts(force=True)
        sp = run.bioinformatics['specialists']
        self.assertEqual(['proteome'], [s['name'] for s in sp])
        self.assertEqual('Proteins and secretome', sp[0]['label'])
        self.assertIn('CSF1R', sp[0]['text'])


class AgentAndFormTests(unittest.TestCase):
    def test_the_agent_knows_each_area_and_the_form_offers_only_the_mode(self):
        prompt = (K.ROOT / 'discovery_loop' / 'agents' / 'bioinformatics' / 'prompt.md').read_text()
        self.assertIn('ANALYSIS: <area>', prompt)
        for k in list(DISC.BIO_ANALYSIS_AREAS) + list(DISC.BIO_PROCESS_AREAS):
            self.assertIn(f'**{k}**', prompt)
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertIn('id="bioMode"', html)
        for m in DISC.BIOINFORMATICS_MODES:
            self.assertIn(f'value="{m}"', html)
        self.assertNotIn('bioSpecialists', html)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('bioinformatics_mode:', js)



class FoldTableTests(unittest.TestCase):
    def test_analyses_and_the_round_plan_are_tables_whose_rows_open(self):
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function foldTable', js)
        self.assertIn('function analysisTable', js)
        self.assertIn("host.append(analysisTable(bio))", js)
        rp = js[js.index('function renderRoundPlan'):js.index('function runTheRound')]
        self.assertIn("'What this round settles'", rp)
        self.assertIn("'Then the next run'", rp)
        self.assertIn("'Differs from control in'", rp)
        self.assertNotIn('What the next run does with each outcome', rp,
                         'the outcome rules sit inside their unknown, not in a long list')
        self.assertNotIn('innerHTML', js[js.index('function foldTable'):js.index('function renderBioInsights')])
        css = (K.ROOT / 'webapp' / 'brand.css').read_text()
        self.assertIn('.ft tr.ft-detail', css)
        self.assertIn('#roundInsights,#bioInsights{font-size:13px', css)

if __name__ == '__main__':
    unittest.main()
