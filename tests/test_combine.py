"""One readout pooled across series: standardised, random effects, disagreement shown.

The analysis results here are SYNTHETIC, written in the shape the tools emit.
"""
import contextlib
import io
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.bioinformatics import cli as BCLI
from biosense.bioinformatics import combine as CB


def result(aid, rows, confidence='moderate', control='vehicle', treatment='cue'):
    return {'analysis_id': aid, 'confidence': confidence,
            'source_evidence_class': 'public_dataset',
            'datasets': [{'dataset_id': f'synthetic_{aid}'}],
            'comparison': {'control': control, 'treatment': treatment}, 'statistics': rows}


def row(readout, ma, mb, sa=1.0, sb=1.0, na=4, nb=4):
    return {'readout': readout, 'mean_a': ma, 'mean_b': mb, 'sd_a': sa, 'sd_b': sb,
            'n_a': na, 'n_b': nb}


class CombineTests(unittest.TestCase):
    def test_hedges_g_matches_the_formula(self):
        g, v = CB.hedges_g(row('X', 5.0, 6.0))
        j = 1 - 3 / (4 * 8 - 9)
        self.assertAlmostEqual(j * 1.0, g)
        self.assertAlmostEqual(8 / 16 + g ** 2 / 16, v)
        self.assertIsNone(CB.hedges_g(row('X', 5, 6, na=1)))

    def test_agreeing_series_pool_to_a_tighter_estimate(self):
        res = [result('a', [row('PPARG', 5, 6)]), result('b', [row('PPARG', 5, 6.2)]),
               result('c', [row('PPARG', 5, 5.9)], confidence='low')]
        out = CB.combine(res, ['PPARG'])
        r = out['rows'][0]
        self.assertEqual(3, r['k'])
        self.assertEqual('increase', r['direction'])
        self.assertLess(r['se'], math.sqrt(CB.hedges_g(row('X', 5, 6))[1]))
        self.assertAlmostEqual(0.0, r['tau2'])
        self.assertAlmostEqual(1.0, sum(s['weight'] for s in r['studies']))
        self.assertEqual('moderate', r['confidence'], 'no higher than the best series')
        self.assertEqual(2, r['best_evidence_only']['k'])

    def test_disagreeing_series_widen_and_lower_confidence(self):
        res = [result('a', [row('PPARG', 5, 7, sa=.3, sb=.3)], confidence='high'),
               result('b', [row('PPARG', 5, 3, sa=.3, sb=.3)], confidence='high')]
        r = CB.combine(res, ['PPARG'])['rows'][0]
        self.assertGreater(r['i2'], 0.5)
        self.assertEqual('unknown', r['direction'])
        self.assertEqual('moderate', r['confidence'])
        self.assertIn('disagree', r['confidence_basis'])

    def test_a_set_score_pools_across_series_with_different_set_sizes(self):
        res = [result('a', [row('gene set score (5 genes)', 0, 0.8)]),
               result('b', [row('gene set score (4 genes)', 0, 0.6)], control='untreated')]
        out = CB.combine(res, ['gene set score'])
        self.assertEqual(2, out['rows'][0]['k'])
        self.assertIn('different contrasts', out['limitations'][0])

    def test_what_cannot_be_pooled_is_refused_with_the_reason(self):
        with self.assertRaisesRegex(K.ContractError, 'at least two'):
            CB.combine([result('a', [row('PPARG', 5, 6)])], ['PPARG'])
        with self.assertRaisesRegex(K.ContractError, 'not in this result'):
            CB.combine([result('a', [row('PPARG', 5, 6)]), result('b', [row('MARCO', 5, 6)])],
                       ['PPARG'])

    def test_the_cli_writes_the_pooled_file(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        files = []
        for aid, mb in (('a', 6), ('b', 6.3)):
            f = tmp / f'{aid}.json'
            f.write_text(json.dumps(result(aid, [row('PPARG', 5, mb)])))
            files.append(str(f))
        with contextlib.redirect_stdout(io.StringIO()):
            code = BCLI.main(['analyse', 'combine', '--results', *files, '--readouts', 'PPARG',
                              '--out', str(tmp / 'pooled.json')])
        self.assertEqual(0, code)
        self.assertEqual('combined_analysis',
                         json.loads((tmp / 'pooled.json').read_text())['kind'])


if __name__ == '__main__':
    unittest.main()
