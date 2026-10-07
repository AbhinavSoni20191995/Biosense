"""Which programmes moved: enrichment against a named, versioned, openly licensed library.

Run on SYNTHETIC fixtures only: an invented 60-gene table in which one invented
pathway's genes all rise modestly, and a GMT library in both dialects in use.
"""
import contextlib
import csv
import gzip
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from biosense import contracts as K
from biosense.bioinformatics import cli as BCLI
from biosense.bioinformatics import genesets as GS
from biosense.data import ingest as ING
from biosense.data import roots as DR

UP = [f'UPG{i}' for i in range(8)]
FLAT = [f'GEN{i}' for i in range(52)]
REACTOME = ('Synthetic up programme\tR-HSA-0001\t' + '\t'.join(UP) + '\n'
            'Synthetic flat programme\tR-HSA-0002\t' + '\t'.join(FLAT[:10]) + '\n'
            'Too small\tR-HSA-0003\tGEN1\n')
ENRICHR = ('synthetic up (GO:0000001)\t\t' + '\t'.join(f'{g},1.0' for g in UP) + '\n')


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {DR.CACHE_ENV: str(self.tmp / 'cache'),
                                           DR.PRIVATE_ENV: str(self.tmp / 'private')})
        env.start(); self.addCleanup(env.stop)

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = BCLI.main(list(argv))
        return code, json.loads(out.getvalue())

    def zipped(self, text):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('ReactomePathways.gmt', text)
        return buf.getvalue()


class LibraryTests(Sandbox):
    def test_both_gmt_dialects_are_read(self):
        sets = GS.parse_gmt(REACTOME)
        self.assertEqual('R-HSA-0001', sets['Synthetic up programme']['id'])
        self.assertNotIn('Too small', sets, 'a one-gene set is dropped')
        self.assertEqual(UP, GS.parse_gmt(ENRICHR)['synthetic up (GO:0000001)']['genes'],
                         'Enrichr weights are stripped')

    def test_a_public_library_is_fetched_with_permission_and_recorded(self):
        with self.assertRaisesRegex(K.ContractError, 'network'):
            GS.fetch('reactome')
        with mock.patch.object(GS, '_get', return_value=self.zipped(REACTOME)):
            meta = GS.fetch('reactome', i_have_network_permission=True)
        self.assertEqual((2, 'CC0 1.0 (Reactome)'), (meta['sets'], meta['license']))
        self.assertEqual(64, len(meta['sha256']))
        self.assertEqual(['reactome'], [m['library'] for m in GS.available()['stored']])
        with self.assertRaisesRegex(K.ContractError, 'not on this machine'):
            GS.load('go_bp')

    def test_a_persons_library_says_whose_and_on_what_terms(self):
        f = self.tmp / 'panel.gmt'
        f.write_text(REACTOME)
        with self.assertRaisesRegex(K.ContractError, 'terms'):
            GS.add_local(f, name='my_panel', license=' ')
        with self.assertRaisesRegex(K.ContractError, 'not one of the public'):
            GS.add_local(f, name='reactome', license='internal')
        meta = GS.add_local(f, name='my_panel', license='internal', added_by='Dr A')
        self.assertEqual(('local', 'internal'), (meta['source'], meta['license']))


class EnrichmentTests(Sandbox):
    def dataset(self):
        rnd = random.Random(7)
        path = self.tmp / 'expr.tsv'
        with open(path, 'w', newline='') as f:
            w = csv.writer(f, delimiter='\t')
            w.writerow(['gene', 'sample_id', 'condition', 'logcpm'])
            for g in UP + FLAT:
                for i in range(4):
                    for cond, shift in (('vehicle', 0.0), ('cue', 0.8 if g in UP else 0.0)):
                        w.writerow([g, f'{cond}_{i}', cond,
                                    f'{5 + shift + rnd.gauss(0, 0.4):.4f}'])
        m, _ = ING.ingest_public_table(
            path, dataset_id='synthetic_pathway_demo', accession='SYNTHETIC-GSE1',
            source='geo', title='SYNTHETIC: one programme rises', modality='bulk_rna',
            experimental_design={'condition_column': 'condition', 'control': 'vehicle',
                                 'treatments': ['cue'], 'sample_id_column': 'sample_id'},
            registered_by='test')
        return m['dataset_id']

    def run_plan(self, did, *options):
        code, plan = self.cli(
            'analyse', 'plan', '--plan-id', 'plan-path', '--question', 'Which programmes moved?',
            '--evidence-gap', 'U1', '--uncertainty', 'which programme the cue engages',
            '--why', 'it decides which lever to test next',
            '--dataset-ids', did, '--analysis-type', 'pathway_enrichment',
            '--tool', 'bulk.pathway_enrichment', '--decision-relevance', 'maturation cue',
            '--group-column', 'condition', '--control', 'vehicle', '--treatment-level', 'cue',
            *[x for o in options for x in ('--option', o)],
            '--out', str(self.tmp / 'plan.json'))
        self.assertEqual(0, code, plan)
        code, res = self.cli('analyse', 'run', '--plan', str(self.tmp / 'plan.json'),
                             '--out', str(self.tmp / 'result.json'))
        return code, res

    def test_the_programme_that_moved_is_found_and_named_with_its_library(self):
        f = self.tmp / 'panel.gmt'
        f.write_text(REACTOME)
        GS.add_local(f, name='synthetic_lib', license='synthetic fixture')
        code, res = self.run_plan(self.dataset(), 'library=synthetic_lib', 'min_size=5')
        self.assertEqual(0, code, res)
        res = json.loads((self.tmp / 'result.json').read_text())
        top = res['statistics'][0]
        self.assertEqual('Synthetic up programme', top['readout'])
        self.assertEqual('increase', top['direction'])
        self.assertLess(top['q_value'], 0.05)
        self.assertEqual(set(UP), set(top['leading_genes']))
        flat = next(r for r in res['statistics'] if r['readout'] == 'Synthetic flat programme')
        self.assertGreater(flat['p_value'], top['p_value'])
        checks = {c['check']: c for c in res['quality_control']['checks']}
        self.assertIn('synthetic_lib', checks['library']['detail'])
        self.assertEqual('WARN', checks['background']['status'], '60 genes is not a genome')

    def test_an_unknown_option_or_missing_library_is_refused(self):
        did = self.dataset()
        code, res = self.run_plan(did, 'library=reactome')
        self.assertNotEqual(0, code)
        self.assertIn('not on this machine', json.dumps(res))
        code, res = self.run_plan(did, 'libary=reactome')
        self.assertNotEqual(0, code)
        self.assertIn('unknown option', json.dumps(res))


if __name__ == '__main__':
    unittest.main()
