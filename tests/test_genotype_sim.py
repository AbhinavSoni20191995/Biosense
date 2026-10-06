"""Wild type against an engineered line, on the reactor, by hand and by the agents.

The Quick Loop's BACH2 comparison drew growth curves per arm; a person asked
for the same in the Simulator and in AI discovery. The reactor models an edit
as an assumed change in growth and differentiation — so that is what is run,
and what every caption says.
"""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.evidence import cli as EVCLI
from biosense.production import discovery as DISC
from biosense.production import sim_mode as SM


class ReactorTests(unittest.TestCase):
    def test_the_two_runs_differ_only_by_the_assumed_edit(self):
        r = SM.genotype_compare({'genotype': {'label': 'BACH2 KO', 'growth_ratio': 0.8,
                                              'diff_ratio': 1.2}})
        self.assertEqual('synthetic_demonstration', r['evidence_status'])
        days = r['curves']['days']
        self.assertGreater(len(days), 10)
        for arm in ('wild_type', 'edited'):
            self.assertEqual(len(days), len(r['curves'][arm]['vcd_e6_per_ml']))
        self.assertNotEqual(r['curves']['wild_type']['vcd_e6_per_ml'],
                            r['curves']['edited']['vcd_e6_per_ml'])
        self.assertIn('harvest_per_input_ipsc', r['deltas'])
        self.assertIn('not a prediction about the gene', r['verdict'])
        self.assertIn('(wild type)', r['verdict'])

    def test_no_effect_means_identical_curves(self):
        r = SM.genotype_compare({'genotype': {'label': 'silent', 'growth_ratio': 1,
                                              'diff_ratio': 1}})
        self.assertEqual(r['curves']['wild_type'], r['curves']['edited'])

    def test_an_edit_is_named_bounded_and_never_wild_type_by_accident(self):
        with self.assertRaisesRegex(K.ContractError, 'name the engineered line'):
            SM.genotype_compare({})
        g = SM.parse_genotype({'label': 'x', 'growth_ratio': 50, 'diff_ratio': -1})
        self.assertEqual((SM.GENOTYPE_RANGE[1], SM.GENOTYPE_RANGE[0]),
                         (g['growth_ratio'], g['diff_ratio']))
        with self.assertRaisesRegex(K.ContractError, 'number'):
            SM.parse_genotype({'growth_ratio': 'fast'})


class AgentCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_simulate_with_a_genotype_writes_both_arms(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = EVCLI.main(['simulate', '--project', 'ipsc_macrophage', '--set', 'mcsf_ng_ml=80',
                               '--genotype', 'BACH2_KO:growth=0.8,diff=1.2',
                               '--out', str(self.tmp / 'simulation.json')])
        self.assertEqual(0, code)
        g = K.read_json(self.tmp / 'simulation.json')['genotype_simulation']
        self.assertEqual('BACH2_KO', g['genotype']['label'])
        self.assertIn('labelled guess', g['assumption'])
        self.assertIn('mcsf_ng_ml', g['used'])
        self.assertIn('genotype', json.loads(out.getvalue()))

    def test_a_malformed_genotype_is_refused_with_the_shape(self):
        import argparse
        with self.assertRaises(argparse.ArgumentTypeError):
            EVCLI._genotype('BACH2 growth 0.8')
        with self.assertRaises(argparse.ArgumentTypeError):
            EVCLI._genotype('BACH2:speed=2')
        self.assertEqual({'label': 'KO', 'growth_ratio': 0.7, 'diff_ratio': 1.0},
                         EVCLI._genotype('KO:growth=0.7'))

    def test_the_run_page_reads_the_comparison_live(self):
        from biosense.production import app as APP
        out = self.tmp / 'ai-x'
        out.mkdir()
        run = APP.DiscoveryRun('g' * 16, {'request_id': 'r', 'project_id': 'p', 'objective': 'o'},
                               out, 'local_real_ai')
        run.scan_artifacts(force=True)
        self.assertIsNone(run.snapshot()['genotype_simulation'])
        doc = {'genotype_simulation': EVCLI.genotype_simulation(
            'ipsc_macrophage', {}, {'label': 'KO', 'growth_ratio': 0.9, 'diff_ratio': 1.1})}
        K.write_json_atomic(out / 'simulation.json', doc)
        run.scan_artifacts(force=True)
        g = run.snapshot()['genotype_simulation']
        self.assertEqual('KO', g['genotype']['label'])
        self.assertTrue(g['curves']['days'])

    def test_the_brief_tells_the_orchestrator_how_and_what_it_means(self):
        req = DISC.build(project_id='ipsc_macrophage',
                         objective='Compare wild type and a BACH2 knockout line for yield.',
                         runtime_mode='synthetic_demo')
        brief = DISC.render_brief(req, loop_dir='ai-x')
        self.assertIn('--genotype "<EDIT>:growth=<ratio>,diff=<ratio>"', brief)
        self.assertIn('never a prediction of the gene', brief)


class PageTests(unittest.TestCase):
    def test_both_pages_draw_the_curves_and_the_layout_holds(self):
        sim_html = (K.ROOT / 'webapp' / 'simulator.html').read_text()
        self.assertIn('id="genoCard"', sim_html)
        self.assertIn('curves.js', sim_html)
        self.assertIn("'/api/sim/genotype'", (K.ROOT / 'webapp' / 'simulator.js').read_text())
        console = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertIn('id="genoInsights"', console)
        self.assertIn('curves.js', console)
        # A long objective in the run list never widens its card under the live panel.
        self.assertIn('.cols > *, .stack > *{min-width:0}', console)
        css = (K.ROOT / 'webapp' / 'brand.css').read_text()
        self.assertIn('.prunlist .pobj{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}', css)
