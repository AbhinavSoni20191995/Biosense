"""Protein and analyte abundance between two conditions, from processed tables.

Every table here is SYNTHETIC, written by the test: the numbers are chosen so
the expected effects are exact, not measured from anything.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.bioinformatics import registry as TREG
from biosense.bioinformatics.toolkit.proteomics import abundance as AB
from biosense.data import tables as TB
from tests.test_geo_fetch import Sandbox

TOOL = 'proteomics.abundance_comparison'
CTRL, TRT = 'vehicle', 'IL-1b'


def long_table(rows):
    """SYNTHETIC long table: protein, sample_id, condition, log2_intensity."""
    lines = ['protein,sample_id,condition,log2_intensity']
    lines += [','.join(map(str, r)) for r in rows]
    return '\n'.join(lines) + '\n'


def synthetic_long():
    rows = []
    ctrl = ['c1', 'c2', 'c3']
    trt = ['t1', 't2', 't3']
    noise = [-0.1, 0.0, 0.1]
    for s, e in zip(ctrl, noise):
        rows += [('SYN_UP', s, CTRL, 20 + e), ('SYN_FLAT', s, CTRL, 25 + e),
                 ('SYN_DOWN', s, CTRL, 22 + e), ('SYN_ONLY_TRT', s, CTRL, 'NA'),
                 ('SYN_TWO_EACH', s, CTRL, 'NA' if s == 'c3' else 18 + e)]
    for s, e in zip(trt, noise):
        rows += [('SYN_UP', s, TRT, 22 + e), ('SYN_FLAT', s, TRT, 25 - e),
                 ('SYN_DOWN', s, TRT, 21 + e), ('SYN_ONLY_TRT', s, TRT, 19 + e),
                 ('SYN_TWO_EACH', s, TRT, 'NA' if s == 't3' else 19 + e)]
    # SYN_SPARSE: one value per group, testable under no setting.
    rows += [('SYN_SPARSE', 'c1', CTRL, 17), ('SYN_SPARSE', 't1', TRT, 18)]
    return long_table(rows)


def synthetic_cytokines(il6_c1='100'):
    """SYNTHETIC wide cytokine panel in pg/mL: treatment is exactly 4x control for IL6."""
    return ('sample,condition,donor,IL6_pg_ml,TNF_pg_ml,VEGFA_pg_ml\n'
            f'c1,{CTRL},d1,{il6_c1},50,1000\n'
            f'c2,{CTRL},d2,200,60,1100\n'
            f'c3,{CTRL},d3,400,55,<LOD\n'
            f't1,{TRT},d1,400,52,1050\n'
            f't2,{TRT},d2,800,58,1000\n'
            f't3,{TRT},d3,1600,57,1200\n')


class ProteomicsCase(Sandbox):
    def register(self, text, did, modality='proteomics', sample='sample_id', extra=()):
        f = self.tmp / f'{did}.csv'
        f.write_text(text)
        code, d = self.cli('datasets', 'register', '--file', str(f), '--dataset-id', did,
                           '--title', f'SYNTHETIC {did}', '--modality', modality,
                           '--condition-column', 'condition', '--control', CTRL,
                           '--treatment', TRT, '--sample-id-column', sample,
                           '--description', 'SYNTHETIC fixture written by tests/test_proteomics.py',
                           *extra)
        self.assertEqual(0, code, d)
        return did

    def plan(self, did, *extra):
        return self.cli(
            'analyse', 'plan', '--plan-id', 'plan-prot', '--question',
            'Which secreted or cellular proteins move under IL-1b?',
            '--evidence-gap', 'U-prot', '--uncertainty', 'whether IL-1b shifts the proteome',
            '--why', 'it decides whether the cytokine cue belongs in the protocol',
            '--dataset-ids', did, '--analysis-type', 'protein_abundance_comparison',
            '--tool', TOOL, '--decision-relevance', 'cytokine cue',
            '--group-column', 'condition', '--control', CTRL, '--treatment-level', TRT,
            *extra, '--out', str(self.tmp / 'plan.json'))

    def run_plan(self):
        out = self.tmp / 'result.json'
        code, res = self.cli('analyse', 'run', '--plan', str(self.tmp / 'plan.json'),
                             '--out', str(out))
        return code, res, (json.loads(out.read_text()) if code == 0 else None)

    def analyse(self, did, *extra):
        code, plan = self.plan(did, *extra)
        self.assertEqual(0, code, plan)
        return self.run_plan()


def qc(result):
    return {c['check']: c for c in result['quality_control']['checks']}


class RegistrationTests(unittest.TestCase):
    def test_the_tool_is_registered_for_its_analysis_type(self):
        spec = TREG.TOOLS[TOOL]
        self.assertIn('protein_abundance_comparison', spec.analysis_types)
        self.assertEqual(('proteomics', 'secretome', 'generic_table'), tuple(spec.modalities))


class LongTableTests(ProteomicsCase):
    def test_a_long_log2_table_runs_end_to_end(self):
        did = self.register(synthetic_long(), 'synthetic-prot-long')
        code, res, r = self.analyse(did)
        self.assertEqual(0, code, res)
        stats = {s['readout']: s for s in r['statistics']}
        self.assertEqual({'SYN_UP', 'SYN_FLAT', 'SYN_DOWN', 'SYN_TWO_EACH'}, set(stats))
        up = stats['SYN_UP']
        self.assertAlmostEqual(2.0, up['effect'], places=9)
        self.assertEqual('difference_in_log2_abundance', up['effect_type'])
        self.assertEqual('increase', up['direction'])
        self.assertEqual((3, 3), (up['n_a'], up['n_b']))
        self.assertIsNotNone(up['q_value'])
        self.assertAlmostEqual(-1.0, stats['SYN_DOWN']['effect'], places=9)
        self.assertEqual((2, 2), (stats['SYN_TWO_EACH']['n_a'], stats['SYN_TWO_EACH']['n_b']))
        self.assertIn('2 of 3 vehicle', stats['SYN_TWO_EACH']['note'])

        comp = r['comparison']
        self.assertEqual('long', comp['input_shape'])
        self.assertEqual('log2_intensity', comp['value_column'])
        self.assertEqual({'decided': 'log', 'transform': 'none'},
                         {k: comp['scale'][k] for k in ('decided', 'transform')})
        self.assertEqual({CTRL: 3, TRT: 3}, comp['samples'])
        only = comp['detected_in_one_condition_only']
        self.assertEqual(['SYN_ONLY_TRT'], [x['protein'] for x in only])
        self.assertEqual({CTRL: 0, TRT: 3}, only[0]['n_values'])
        self.assertEqual((TRT, CTRL), (only[0]['detected_in'], only[0]['absent_from']))
        self.assertNotIn('SYN_ONLY_TRT', stats, 'absent in one condition: never given a fold change')
        self.assertEqual(1, comp['proteins_not_testable'])     # SYN_SPARSE

        checks = qc(r)
        self.assertIn('long', checks['input shape']['detail'])
        self.assertIn('says log', checks['scale']['detail'])
        self.assertIn('4 of 6 tested', checks['proteins tested']['detail'])
        self.assertIn('SYN_SPARSE', checks['not testable']['detail'])
        self.assertEqual('WARN', checks['detected in one condition only']['status'])
        self.assertEqual('PASS', checks['group sizes']['status'])
        self.assertIn('does not normalise', checks['method']['detail'])
        self.assertEqual('derived_analysis', r['evidence_class'])

    def test_min_valid_is_respected_and_an_unknown_option_refused(self):
        did = self.register(synthetic_long(), 'synthetic-prot-minvalid')
        code, res, r = self.analyse(did, '--option', 'min_valid=3')
        self.assertEqual(0, code, res)
        tested = {s['readout'] for s in r['statistics']}
        self.assertNotIn('SYN_TWO_EACH', tested, 'two values per group is below min_valid=3')
        self.assertEqual(3, r['comparison']['min_valid'])
        self.assertEqual(2, r['comparison']['proteins_not_testable'])
        self.assertIn('min_valid 3', qc(r)['proteins tested']['detail'])

        code, res = self.plan(did, '--option', 'impute=true')
        self.assertEqual(0, code, res)
        code, res, _ = self.run_plan()
        self.assertNotEqual(0, code)
        self.assertIn('unknown option(s) impute', str(res))

    def test_duplicate_protein_sample_rows_are_refused(self):
        text = synthetic_long() + f'SYN_UP,c1,{CTRL},20.3\n'
        did = self.register(text, 'synthetic-prot-dup')
        code, res, _ = self.analyse(did)
        self.assertNotEqual(0, code)
        self.assertIn('SYN_UP/c1', str(res))
        self.assertIn('more than once', str(res))

    def test_small_positive_values_with_a_neutral_name_need_the_scale_named(self):
        text = synthetic_long().replace('log2_intensity', 'value')
        did = self.register(text, 'synthetic-prot-ambiguous')
        code, res, _ = self.analyse(did)
        self.assertNotEqual(0, code)
        self.assertIn('scale=log', str(res))
        code, res, r = self.analyse(did, '--option', 'scale=log')
        self.assertEqual(0, code, res)
        self.assertIn('named in the plan', r['comparison']['scale']['reason'])

    def test_negative_values_are_read_as_already_log_and_never_transformed_again(self):
        # SYNTHETIC NPX-like values: small, some negative.
        rows = [('SYN_A', s, g, v) for s, g, v in
                (('c1', CTRL, -0.5), ('c2', CTRL, -0.3), ('c3', CTRL, -0.4),
                 ('t1', TRT, 1.5), ('t2', TRT, 1.7), ('t3', TRT, 1.6))]
        text = long_table(rows).replace('log2_intensity', 'npx')
        did = self.register(text, 'synthetic-npx')
        code, res, r = self.analyse(did)
        self.assertEqual(0, code, res)
        self.assertEqual('log', r['comparison']['scale']['decided'])
        self.assertAlmostEqual(2.0, r['statistics'][0]['effect'], places=9)
        code, res, _ = self.analyse(did, '--option', 'scale=linear')
        self.assertNotEqual(0, code)
        self.assertIn('already logged', str(res))


class WideTableTests(ProteomicsCase):
    def test_a_cytokine_panel_in_concentrations_is_log2_transformed_once(self):
        did = self.register(synthetic_cytokines(), 'synthetic-cytokines', modality='secretome',
                            sample='sample', extra=('--donor-column', 'donor'))
        code, res, r = self.analyse(did)
        self.assertEqual(0, code, res)
        comp = r['comparison']
        self.assertEqual('wide', comp['input_shape'])
        self.assertEqual(['IL6_pg_ml', 'TNF_pg_ml', 'VEGFA_pg_ml'], comp['analytes'],
                         'the donor column is design, not an analyte')
        self.assertEqual(('linear', 'log2'), (comp['scale']['decided'],
                                              comp['scale']['transform']))
        stats = {s['readout']: s for s in r['statistics']}
        self.assertAlmostEqual(2.0, stats['IL6_pg_ml']['effect'], places=9,
                               msg='4x on the linear scale is +2 in log2')
        self.assertEqual(2, stats['VEGFA_pg_ml']['n_a'], '<LOD is missing, not zero')
        checks = qc(r)
        self.assertIn('wide', checks['input shape']['detail'])
        self.assertIn('linear', checks['scale']['detail'])
        self.assertIn('log2-transformed once', checks['scale']['detail'])
        self.assertEqual('WARN', checks['non-numeric values']['status'])
        self.assertIn('<LOD', checks['non-numeric values']['detail'])
        self.assertEqual('PASS', checks['independent units']['status'])

    def test_a_zero_concentration_is_missing_not_log2_of_zero(self):
        did = self.register(synthetic_cytokines(il6_c1='0'), 'synthetic-cytokines-zero',
                            modality='secretome', sample='sample')
        code, res, r = self.analyse(did)
        self.assertEqual(0, code, res)
        il6 = next(s for s in r['statistics'] if s['readout'] == 'IL6_pg_ml')
        self.assertEqual((2, 3), (il6['n_a'], il6['n_b']))
        self.assertEqual(1, r['comparison']['scale']['non_positive_read_as_missing'])
        self.assertIn('1 zero or negative value(s) were read as missing',
                      qc(r)['scale']['detail'])

    def test_readouts_restrict_the_analytes(self):
        did = self.register(synthetic_cytokines(), 'synthetic-cytokines-sub',
                            modality='secretome', sample='sample')
        code, res, r = self.analyse(did, '--readouts', 'IL6_pg_ml', 'TNF_pg_ml')
        self.assertEqual(0, code, res)
        self.assertEqual(['IL6_pg_ml', 'TNF_pg_ml'], [s['readout'] for s in r['statistics']])


class RefusalTests(unittest.TestCase):
    """The runner called directly on a SYNTHETIC table, for the refusals."""

    def table(self, text):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / 'synthetic.csv').write_text(text)
        return TB.read_table(d / 'synthetic.csv')

    def run_tool(self, text, design=None, comparison=None):
        design = {'condition_column': 'condition', 'control': CTRL, 'treatments': [TRT],
                  **(design or {})}
        return AB.run(self.table(text), {'experimental_design': design, 'files': []},
                      {'comparison': comparison or {}})

    def test_a_missing_control_level_is_refused_with_the_levels_present(self):
        with self.assertRaisesRegex(K.ContractError, "'placebo'.*Values present"):
            self.run_tool(synthetic_long(), design={'control': 'placebo'})

    def test_no_condition_column_named_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'not inferred'):
            self.run_tool(synthetic_long(), design={'condition_column': None})

    def test_a_wide_table_without_analytes_is_refused(self):
        text = f'sample_id,condition,donor\nc1,{CTRL},d1\nc2,{CTRL},d2\nt1,{TRT},d1\nt2,{TRT},d2\n'
        with self.assertRaisesRegex(K.ContractError, 'no analyte columns'):
            self.run_tool(text)

    def test_too_few_samples_are_refused(self):
        text = f'sample_id,condition,IL6\nc1,{CTRL},100\nt1,{TRT},400\nt2,{TRT},500\n'
        with self.assertRaisesRegex(K.ContractError, 'at least 2 samples'):
            self.run_tool(text)

    def test_a_bad_option_value_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'scale'):
            self.run_tool(synthetic_long(), comparison={'options': {'scale': 'log10'}})
        with self.assertRaisesRegex(K.ContractError, 'min_valid'):
            self.run_tool(synthetic_long(), comparison={'options': {'min_valid': 0}})

    def test_a_wide_table_with_a_repeated_sample_is_refused(self):
        text = synthetic_cytokines().replace('t3,', 't2,')
        with self.assertRaisesRegex(K.ContractError, 't2'):
            self.run_tool(text, design={'sample_id_column': 'sample'})

    def test_a_sample_under_two_conditions_is_refused(self):
        text = synthetic_long().replace(f'SYN_UP,t1,{TRT}', f'SYN_UP,t1,{CTRL}')
        with self.assertRaisesRegex(K.ContractError, 'both'):
            self.run_tool(text)


if __name__ == '__main__':
    unittest.main()
