"""A whole transcriptome through the screening comparison.

The tool used to refuse at 5,000 features. That number was never statistical —
BH-FDR already ran across whatever was tested — it was the point where a scipy
call per gene became a minute of wall clock. A real public series is 16,000
genes, so the refusal fired on the ordinary case and told the person to go and
find another tool for something this one computes correctly.

So the limit now says what it means: above a whole annotation the rows are
probably probes or transcripts rather than genes, which is worth stopping for.
Below it, the comparison runs, and these tests check it runs on a
transcriptome-shaped table, keeps the per-gene arithmetic, and still carries
the warnings that make it a screen rather than a DESeq2 result.
"""
import csv
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from biosense import contracts as K
from biosense.bioinformatics.toolkit.bulk import basic_de as DE
from biosense.data import tables as TB

MANIFEST = {'experimental_design': {'condition_column': 'condition', 'control': 'control',
                                    'treatments': ['mcsf_high'], 'sample_id_column': 'sample',
                                    'replicate_column': 'sample'}}
PLAN = {'comparison': {}}
SPIKED = 'spiked'


def write_long_table(path, *, n_genes, n_per_group=4, n_spiked=0, seed=0):
    """A long-format normalised-expression table, a few genes moved on purpose.

    The moved genes are named so a test can ask whether the comparison found
    them, rather than only that it produced numbers.
    """
    rng = np.random.default_rng(seed)
    base = rng.normal(6.0, 1.0, n_genes)
    with open(path, 'w', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(['gene', 'sample', 'condition', 'log2_expression'])
        for gi in range(n_genes):
            spiked = gi < n_spiked
            gene = f'{SPIKED}{gi}' if spiked else f'GENE{gi}'
            for cond in ('control', 'mcsf_high'):
                shift = 3.0 if (spiked and cond == 'mcsf_high') else 0.0
                for r in range(n_per_group):
                    v = base[gi] + shift + rng.normal(0, 0.35)
                    w.writerow([gene, f'{cond}_{r}', cond, f'{v:.4f}'])
    return path


class WideTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        p = write_long_table(cls.tmp / 'wide.tsv', n_genes=3000, n_spiked=120, seed=5)
        cls.table = TB.read_table(p)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_transcriptome_sized_table_is_compared_rather_than_refused(self):
        started = time.monotonic()
        res = DE.run(self.table, MANIFEST, PLAN)
        elapsed = time.monotonic() - started
        self.assertEqual(3000, res['comparison']['features_tested'])
        self.assertEqual(3000, len(res['statistics']))
        self.assertLess(elapsed, 20.0, f'3000 genes took {elapsed:.1f}s; the cost is the reason '
                                       f'the old limit existed, so it is held to here')

    def test_the_genes_that_were_moved_are_the_ones_it_finds(self):
        """Numbers on 3000 genes are easy; separating signal from noise is the
        claim, so it is checked rather than assumed."""
        rows = DE.run(self.table, MANIFEST, PLAN)['statistics']
        spiked = [r for r in rows if r['readout'].startswith(SPIKED)]
        rest = [r for r in rows if not r['readout'].startswith(SPIKED)]
        self.assertEqual(120, len(spiked))
        self.assertGreater(np.median([r['effect'] for r in spiked]), 2.0)
        self.assertLess(abs(np.median([r['effect'] for r in rest])), 0.5)
        called = [r for r in spiked if r['q_value'] is not None and r['q_value'] < 0.05]
        self.assertEqual(120, len(called), 'a 3 log2 shift on n=4 should be called')
        wrong = [r for r in rest if r['q_value'] is not None and r['q_value'] < 0.05]
        self.assertLess(len(wrong), len(rest) * 0.05,
                        f'{len(wrong)} of {len(rest)} unmoved genes called at q<0.05')

    def test_the_interval_says_which_method_produced_it(self):
        """The bootstrap is not affordable per gene across a transcriptome. The
        interval that replaces it is analytic and says so, so nobody reads a
        Welch interval as a resampled one."""
        rows = DE.run(self.table, MANIFEST, PLAN)['statistics']
        self.assertTrue(all('Welch t interval' in r['ci_method'] for r in rows))
        for r in rows[:50]:
            self.assertLess(r['ci_low'], r['effect'])
            self.assertGreater(r['ci_high'], r['effect'])

    def test_it_is_still_a_screen_and_still_says_so(self):
        """The reason the cap moved is performance. None of the honesty about
        what this tool is may move with it."""
        res = DE.run(self.table, MANIFEST, PLAN)
        detail = ' '.join(c['detail'] for c in res['quality_control']['checks'])
        self.assertIn('not a DESeq2/edgeR result', detail)
        self.assertIn('No dispersion modelling', detail)
        self.assertEqual('difference_in_log2_expression',
                         res['statistics'][0]['effect_type'])


class SmallTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_handful_of_genes_keeps_the_bootstrap_and_the_rank_test(self):
        """The fast path is for the size that needs it. A short table still gets
        the better interval and the second test beside the t-test."""
        p = write_long_table(self.tmp / 'small.tsv', n_genes=12, n_per_group=5, seed=2)
        rows = DE.run(TB.read_table(p), MANIFEST, PLAN)['statistics']
        self.assertEqual(12, len(rows))
        self.assertTrue(all('bootstrap' in r['ci_method'] for r in rows))
        self.assertTrue(all('Mann-Whitney' in r['test'] for r in rows))

    def test_the_two_paths_agree_on_the_effect_and_the_direction(self):
        """Which path ran is a cost decision. It may not change the answer."""
        p = write_long_table(self.tmp / 'both.tsv', n_genes=30, n_spiked=5, seed=7)
        table = TB.read_table(p)
        slow = DE.run(table, MANIFEST, PLAN)['statistics']
        with mock.patch.object(DE, 'BOOTSTRAP_UP_TO', 1):
            fast = DE.run(table, MANIFEST, PLAN)['statistics']
        self.assertTrue(all('Welch t interval' in r['ci_method'] for r in fast))
        for a, b in zip(slow, fast):
            self.assertEqual(a['readout'], b['readout'])
            self.assertAlmostEqual(a['effect'], b['effect'], places=12)
            self.assertAlmostEqual(a['p_value'], b['p_value'], places=12)
            self.assertAlmostEqual(a['q_value'], b['q_value'], places=12)
            self.assertEqual(a['direction'], b['direction'])


class LimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_above_a_whole_annotation_it_stops_and_says_what_to_check(self):
        """The refusal has to be actionable: more rows than there are genes
        means the feature column is probably the wrong column."""
        p = write_long_table(self.tmp / 'many.tsv', n_genes=8, seed=1)
        with mock.patch.object(DE, 'MAX_FEATURES', 4):
            with self.assertRaises(K.ContractError) as e:
                DE.run(TB.read_table(p), MANIFEST, PLAN)
        msg = str(e.exception)
        self.assertIn('8 features', msg)
        self.assertIn('probes or transcripts', msg)
        self.assertIn('external-tool adapter', msg)

    def test_the_limit_is_an_annotation_rather_than_a_performance_number(self):
        """If this drifts back down to a few thousand, an ordinary public series
        is refused again, which is the bug these tests exist for."""
        self.assertGreaterEqual(DE.MAX_FEATURES, 30_000)
        self.assertGreater(DE.MAX_FEATURES, DE.BOOTSTRAP_UP_TO)


if __name__ == '__main__':
    unittest.main()
