"""The cell production library: the landscape every run starts from.

A run reported "no paper gives a progenitor density" while the protocol it was
reading stated ten embryoid bodies per well. These tests hold the library's
rules — every value quotes its paper, a proxy says what it informs and how,
one bad paper does not lose the rest — and that a run's brief starts from it.
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from biosense import contracts as K
from biosense import projects as PJ
from biosense.data import roots as DR
from biosense.evidence import library as LIB
from biosense.production import discovery as DISC
from biosense.production import discovery_runner as DRUN

QUOTE = 'Ten EBs were transferred to each well of a 6-well plate in 3 mL of medium.'


def paper(**kw):
    p = {'paper_id': 'PMC3741356', 'title': 'Efficient, long term production of monocyte-derived macrophages',
         'year': 2013, 'system': {'cells': 'hPSC', 'format': 'static EB', 'vessel': '6-well plate'},
         'stages': ['aggregation', 'differentiation'], 'summary': 'EB factory.',
         'values': [{'parameter': 'M-CSF concentration', 'parameter_id': 'mcsf_ng_ml',
                     'stage': 'differentiation', 'value': 100, 'unit': 'ng/mL',
                     'quote': 'EBs were plated in X-VIVO 15 with 100 ng/mL M-CSF and 25 ng/mL IL-3.'},
                    {'parameter': 'EBs per well', 'stage': 'differentiation', 'value': 10,
                     'unit': 'EBs/well', 'proxy_for': 'seed_density',
                     'conversion': '10 EBs in 3 mL; cells per EB not stated', 'quote': QUOTE}]}
    p.update(kw)
    return p


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {DR.PRIVATE_ENV: str(self.tmp / 'private')})
        env.start(); self.addCleanup(env.stop)

    def test_a_value_needs_its_quote_and_a_proxy_its_conversion(self):
        bad_quote = paper(values=[dict(paper()['values'][0], quote='')])
        with self.assertRaisesRegex(K.ContractError, 'quote'):
            LIB.check_paper(bad_quote)
        no_conv = paper(values=[dict(paper()['values'][1], conversion=None)])
        with self.assertRaisesRegex(K.ContractError, 'how it relates'):
            LIB.check_paper(no_conv)
        with self.assertRaisesRegex(K.ContractError, 'PMID'):
            LIB.check_paper(paper(paper_id='a paper'))

    def test_one_bad_paper_is_refused_and_the_rest_are_kept(self):
        r = LIB.merge({'papers': [paper(), paper(paper_id='PMID:1', title='')]}, from_run='r1')
        self.assertEqual((['PMC3741356'], 1), (r['added'], len(r['refused'])))
        r = LIB.merge({'papers': [paper(summary='Read again.')]}, from_run='r2')
        self.assertEqual(['PMC3741356'], r['updated'])
        self.assertEqual('Read again.', LIB.load()['papers'][0]['summary'])
        self.assertEqual({'papers': 1, 'values': 2}, {k: LIB.stats()[k] for k in ('papers', 'values')})

    def test_search_finds_the_proxy_for_a_density_question(self):
        LIB.merge({'papers': [paper()]})
        hits = LIB.search(['embryoid', 'EBs', 'density'], stage='differentiation')
        self.assertEqual('EBs per well', hits[0]['parameter'])
        self.assertEqual('seed_density', hits[0]['proxy_for'])

    def test_every_run_s_brief_starts_from_the_library(self):
        req = DISC.build(project_id='ipsc_macrophage',
                         objective='Increase viable macrophage production while keeping identity.',
                         runtime_mode='synthetic_demo')
        self.assertIn('library is empty on this server', DISC.render_brief(req, loop_dir='ai-x'))
        LIB.merge({'papers': [paper()]})
        brief = DISC.render_brief(req, loop_dir='ai-x')
        self.assertIn('The cell production library — start here', brief)
        self.assertIn('EBs per well = 10 EBs/well', brief)
        self.assertIn('proxy for seed_density', brief)
        self.assertIn('never a bare "not found"', brief)

    def test_a_landscape_run_dispatches_areas_and_its_drafts_join_the_library(self):
        req = DISC.build(project_id='ipsc_macrophage',
                         objective='Build the cell production library across stages.',
                         runtime_mode='local', purpose='landscape')
        brief = DISC.render_brief(req, loop_dir='ai-x')
        self.assertIn('This run builds the cell production library', brief)
        for area, _ in DISC.LANDSCAPE_AREAS:
            self.assertIn(f'literature-{area}', brief)
        self.assertNotIn('The cell production library — start here', brief)
        out = self.tmp / 'run'
        (out / 'literature' / 'aggregation').mkdir(parents=True)
        K.write_json_atomic(out / 'discovery_request.json', req)
        K.write_json_atomic(out / 'literature' / 'aggregation' / 'library.draft.json',
                            {'papers': [paper()]})
        final = DRUN.finish(req, out, runtime_mode=req['runtime_mode'])
        self.assertEqual(['PMC3741356'], final['library']['added'])
        self.assertEqual(1, final['library']['stats']['papers'])

    def test_the_cli_checks_a_draft(self):
        f = self.tmp / 'd.json'
        f.write_text(json.dumps({'papers': [paper(), paper(paper_id='PMID:2', values=[{}])]}))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = LIB.main(['check', '--draft', str(f)])
        self.assertEqual(1, code)
        self.assertEqual(['PMC3741356'], json.loads(out.getvalue())['ok'])


if __name__ == '__main__':
    unittest.main()
