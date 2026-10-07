"""The AI analyst's tools: plan against the data, run what the tools cannot, interpret honestly.

A deterministic tool refuses a plan that names a column or level the data does
not have — correctly — and a run used to end there. The analyst inspects the
data first, names the columns the tool cannot guess, writes a script when no
tool fits (labelled AGENT-WRITTEN, capped at low), and interprets each result
with a confidence capped by the evidence. These tests hold those rules.
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
from biosense.bioinformatics import analyst as AN
from biosense.bioinformatics import cli as BCLI
from biosense.data import ingest as ING
from biosense.data import roots as DR

ROWS = [('GATA6', 'S1', 'ctrl', 1.0), ('GATA6', 'S2', 'ctrl', 1.2), ('GATA6', 'S3', 'ctrl', 0.9),
        ('GATA6', 'S4', 'RA', 3.1), ('GATA6', 'S5', 'RA', 3.4), ('GATA6', 'S6', 'RA', 2.9),
        ('ACTB', 'S1', 'ctrl', 9.0), ('ACTB', 'S2', 'ctrl', 9.1), ('ACTB', 'S3', 'ctrl', 8.9),
        ('ACTB', 'S4', 'RA', 9.0), ('ACTB', 'S5', 'RA', 9.2), ('ACTB', 'S6', 'RA', 8.8)]
SCRIPT = '''
import argparse, json, os, statistics as st
ap = argparse.ArgumentParser(); ap.add_argument("--data", action="append"); ap.add_argument("--out")
a = ap.parse_args()
rows = [l.rstrip("\\n").split("\\t") for l in open(a.data[0])][1:]
g = [float(r[3]) for r in rows if r[0] == "GATA6"]
json.dump({"method": "mean GATA6 across samples", "findings": ["GATA6 mean %.2f" % st.mean(g)],
           "statistics": [{"name": "gata6_mean", "value": st.mean(g)}]},
          open(os.path.join(a.out, "result.json"), "w"))
'''


class AnalystTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {DR.CACHE_ENV: str(self.tmp / 'cache'),
                                           DR.PRIVATE_ENV: str(self.tmp / 'private')})
        env.start(); self.addCleanup(env.stop)
        table = self.tmp / 'zf.tsv'
        table.write_text('symbol\tsample\tgroup\tlog2_tpm\n'
                         + '\n'.join('\t'.join(map(str, r)) for r in ROWS) + '\n')
        ING.ingest_public_table(
            table, dataset_id='gse1_zebrafish', accession='GSE1', source='geo',
            title='zebrafish macrophages, RA vs control', organism='Danio rerio',
            experimental_design={'condition_column': 'group', 'control': 'ctrl',
                                 'treatments': ['RA']},
            notes='route: depositor_supplementary; confidence ceiling for this species and '
                  'route: low', overwrite=True)

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = BCLI.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_inspect_shows_groups_tools_and_the_evidence_ceiling(self):
        d = AN.inspect('gse1_zebrafish')
        self.assertEqual({'ctrl': 6, 'RA': 6}, d['categorical_columns']['group'])
        self.assertIn('bulk.expression_comparison',
                      [t['tool'] for t in d['tools_that_accept_this_modality']])
        self.assertIn('--group-column "group"', d['suggested_plan_arguments'])
        self.assertEqual('low', d['evidence_ceiling'])

    def test_a_refused_plan_is_repaired_by_naming_the_columns(self):
        base = ['analyse', 'plan', '--plan-id', 'plan-rA', '--question', 'Does RA raise GATA6?',
                '--evidence-gap', 'U1', '--uncertainty', 'whether RA induces GATA6',
                '--why', 'decides the retinoid', '--dataset-ids', 'gse1_zebrafish',
                '--analysis-type', 'bulk_expression_comparison',
                '--tool', 'bulk.expression_comparison', '--decision-relevance', 'whether a retinoid belongs in maturation',
                '--group-column', 'group', '--control', 'ctrl', '--treatment-level', 'RA']
        code, d = self.cli(*base, '--out', str(self.tmp / 'p1.json'))
        self.assertEqual(0, code, d)
        code, d = self.cli('analyse', 'run', '--plan', str(self.tmp / 'p1.json'))
        self.assertEqual(1, code)
        self.assertIn('no expression value column', d['message'])
        self.cli(*base, '--value-column', 'log2_tpm', '--feature-column', 'symbol',
                 '--out', str(self.tmp / 'p2.json'))
        code, d = self.cli('analyse', 'run', '--plan', str(self.tmp / 'p2.json'),
                           '--out', str(self.tmp / 'analysis_result_ra.json'))
        self.assertEqual(0, code, d)
        self.assertIn('GATA6', json.dumps(d['statistics']))

    def test_an_agent_written_script_runs_is_kept_and_capped_low(self):
        s = self.tmp / 'gata6_mean.py'
        s.write_text(SCRIPT)
        doc = AN.run_script(s, dataset_ids=['gse1_zebrafish'], question='mean GATA6',
                            out_dir=self.tmp / 'analyst' / 'mean')
        self.assertTrue(doc['succeeded'], doc['problem'])
        self.assertEqual(('agent_written', 'low', False),
                         (doc['method_kind'], doc['confidence'], doc['citable']))
        self.assertAlmostEqual(2.0833, doc['statistics'][0]['value'], 3)
        self.assertEqual(64, len(doc['script']['sha256']))
        bad = self.tmp / 'bad.py'
        bad.write_text('raise SystemExit(3)')
        self.assertFalse(AN.run_script(bad, dataset_ids=['gse1_zebrafish'], question='x q',
                                       out_dir=self.tmp / 'analyst' / 'bad')['succeeded'])

    def test_an_interpretation_is_capped_by_result_species_and_closeness(self):
        result = {'confidence': 'high', 'datasets': [{'dataset_id': 'gse1_zebrafish'}]}
        rpath = self.tmp / 'r.json'
        rpath.write_text(json.dumps(result))
        draft = dict(AN.TEMPLATE, analysis_ref=str(rpath), confidence='high',
                     transfer={'species': 'Danio rerio', 'cells': 'same', 'stage': 'same',
                               'treatment': 'same'})
        d = self.tmp / 'draft.json'
        d.write_text(json.dumps(draft))
        doc = AN.interpret_file(d, self.tmp / 'interpretation_ra.json')
        self.assertEqual(('low', 'high'), (doc['confidence'], doc['confidence_claimed']))
        self.assertTrue(any('evidence ceiling' in c for c in doc['confidence_capped_by']))
        human = AN.interpret(dict(draft, transfer=dict(draft['transfer'], cells='related')),
                             result={'confidence': 'high'})
        self.assertEqual('moderate', human['confidence'])
        with self.assertRaisesRegex(K.ContractError, 'transfer.stage'):
            AN.interpret(dict(draft, transfer={'cells': 'same', 'stage': 'close',
                                               'treatment': 'same'}))

    def test_the_brief_routes_analysis_to_the_analyst(self):
        from biosense.production import discovery as DISC
        from biosense.production import activity as AC
        req = DISC.build(project_id='ipsc_macrophage',
                         objective='Increase viable macrophage production while keeping identity.',
                         runtime_mode='local')
        brief = DISC.render_brief(req, loop_dir='ai-x')
        self.assertIn('Analysing a dataset goes to `analyst`', brief)
        self.assertEqual('analyst', AC.agent_named(
            '{"agent": "analyst", "args": "analysis of GSE1 for the bioinformatics gap"}'))


if __name__ == '__main__':
    unittest.main()
