"""single_cell.identity_purity: a stated marker rule, counted per cell, compared per sample.

Every matrix here is SYNTHETIC and written in a temp dir by the test itself. The
properties that matter: the fraction is exactly what the rule says on a fixture
small enough to count by hand; n counts samples, not cells; the options change the
rule in the way they claim; and what cannot be answered honestly is refused.
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from biosense import contracts as K
from biosense.data import roots as DR
from biosense.data.readers import h5ad as H5

HAVE_ANNDATA = H5.available()
TOOL = 'single_cell.identity_purity'
MARKERS = ['PPARG', 'MARCO', 'MRC1', 'SIGLEC1']

# Tiny fixture. Per sample, how many cells of each kind:
#   two  -> PPARG=2, MARCO=1                    (2 of 4 markers; MARCO only at 1)
#   one  -> PPARG=2                             (1 of 4)
#   neg  -> PPARG=2, MARCO=2, MRC1=2, CD3E=2    (3 of 4, plus the negative marker)
#   none -> ACTB only
TINY = {
    'c1': ('control', dict(two=2, one=4, neg=1, none=3)),
    'c2': ('control', dict(two=3, one=3, neg=1, none=3)),
    't1': ('treated', dict(two=6, one=1, neg=2, none=1)),
    't2': ('treated', dict(two=5, one=2, neg=2, none=1)),
}
# var_names carry an id for MRC1, so MRC1 is found only through var['feature_name'].
VAR_NAMES = ['PPARG', 'MARCO', 'ENSG00000260314', 'SIGLEC1', 'CD3E', 'ACTB']
SYMBOLS = ['PPARG', 'MARCO', 'MRC1', 'SIGLEC1', 'CD3E', 'ACTB']
KIND = {'two': {0: 2, 1: 1}, 'one': {0: 2}, 'neg': {0: 2, 1: 2, 2: 2, 4: 2}, 'none': {5: 3}}


def tiny_adata(fmt='csr', spans=False):
    import anndata as ad
    import pandas as pd
    from scipy import sparse
    rows, samples, conds = [], [], []
    for s, (cond, counts) in TINY.items():
        for kind, n in counts.items():
            for _ in range(n):
                r = np.zeros(len(VAR_NAMES))
                for j, v in KIND[kind].items():
                    r[j] = v
                rows.append(r)
                samples.append(s)
                conds.append(cond)
    if spans:
        conds[-1] = 'control'          # one cell of t2 recorded under control
    X = np.vstack(rows)
    X = X if fmt == 'dense' else sparse.csr_matrix(X).asformat(fmt)
    obs = pd.DataFrame({'sample_id': samples, 'condition': conds},
                       index=[f'cell{i}' for i in range(len(rows))])
    var = pd.DataFrame({'feature_name': SYMBOLS}, index=VAR_NAMES)
    return ad.AnnData(X=X, obs=obs, var=var)


def larger_adata(seed=7):
    """6 samples x 100 cells; treated samples hold more on-identity cells."""
    import anndata as ad
    import pandas as pd
    from scipy import sparse
    rng = np.random.default_rng(seed)
    genes = MARKERS + ['ABCG1', 'ACTB', 'GAPDH', 'CD3E']
    blocks, samples, conds = [], [], []
    for cond, p_on in (('control', 0.35), ('treated', 0.75)):
        for d in range(3):
            s = f'{cond}_D{d + 1}'
            on = rng.random(100) < p_on
            detect = np.where(on[:, None], 0.85, 0.08)
            hit = rng.random((100, len(genes))) < detect
            vals = (rng.poisson(3, (100, len(genes))) + 1) * hit
            vals[:, genes.index('ACTB')] = rng.poisson(20, 100) + 1
            vals[:, genes.index('CD3E')] = 0
            blocks.append(vals)
            samples += [s] * 100
            conds += [cond] * 100
    X = sparse.csr_matrix(np.vstack(blocks).astype(np.float32))
    obs = pd.DataFrame({'sample_id': samples, 'condition': conds},
                       index=[f'cell{i}' for i in range(len(samples))])
    return ad.AnnData(X=X, obs=obs, var=pd.DataFrame(index=genes))


@unittest.skipUnless(HAVE_ANNDATA, 'needs the optional single-cell environment '
                                   '(uv sync --extra singlecell)')
class IdentityPurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = {k: os.environ.get(k) for k in (DR.PRIVATE_ENV, DR.CACHE_ENV)}
        os.environ[DR.PRIVATE_ENV] = str(self.tmp / 'private')
        os.environ[DR.CACHE_ENV] = str(self.tmp / 'cache')
        DR.ensure_roots()
        self.n = 0

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ──
    def register(self, adata, dataset_id):
        from biosense.data import ingest as ING
        path = self.tmp / f'{dataset_id}.h5ad'
        adata.write_h5ad(path)
        ING.ingest_h5ad(path, dataset_id=dataset_id, title='SYNTHETIC identity fixture',
                        cell_type='iPSC-derived macrophage', perturbation='synthetic condition',
                        experimental_design={'condition_column': 'condition',
                                             'control': 'control', 'treatments': ['treated'],
                                             'sample_id_column': 'sample_id',
                                             'replicate_column': 'sample_id'})
        return dataset_id

    def cli(self, *argv):
        from biosense.bioinformatics import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, json.loads(buf.getvalue())

    def analyse(self, dataset_id, readouts, options=()):
        """Plan and run through the CLI. Returns (exit code, result or error doc)."""
        self.n += 1
        plan = self.tmp / f'plan{self.n}.json'
        out = self.tmp / f'result{self.n}.json'
        argv = ['analyse', 'plan', '--plan-id', f'ip-{self.n}',
                '--question', 'What fraction of each sample is on-identity, and does the '
                              'condition change it?',
                '--evidence-gap', 'GAP-identity',
                '--uncertainty', 'no per-sample identity measurement exists yet',
                '--why', 'the loop has no single-cell identity readout',
                '--dataset-ids', dataset_id, '--analysis-type', 'identity_purity',
                '--tool', TOOL, '--decision-relevance', 'whether the condition raises purity',
                '--group-column', 'condition', '--control', 'control',
                '--treatment-level', 'treated', '--readouts', *readouts, '--out', str(plan)]
        for o in options:
            argv += ['--option', o]
        code, doc = self.cli(*argv)
        self.assertEqual(0, code, doc)
        code, doc = self.cli('analyse', 'run', '--plan', str(plan), '--out', str(out))
        if code != 0:
            return code, doc
        return code, K.read_json(out)

    def ok(self, dataset_id, readouts, options=()):
        code, r = self.analyse(dataset_id, readouts, options)
        self.assertEqual(0, code, r)
        self.assertEqual([], K.schema_errors('analysis_result', r))
        return r

    def refused(self, dataset_id, readouts, options=()):
        code, doc = self.analyse(dataset_id, readouts, options)
        self.assertEqual(1, code, doc)
        self.assertEqual('ContractError', doc['error'])
        return doc['message']

    @staticmethod
    def fractions(r):
        return {s: v['fraction_on_identity'] for s, v in r['comparison']['per_sample'].items()}

    @staticmethod
    def qc(r):
        return {c['check']: c for c in r['quality_control']['checks']}

    # ── the tool is wired ──
    def test_the_tool_is_registered(self):
        from biosense.bioinformatics import registry as TREG
        self.assertIn(TOOL, TREG.TOOLS)

    # ── exact fractions on a fixture counted by hand ──
    def test_fractions_are_exactly_what_the_rule_says(self):
        ds = self.register(tiny_adata(), 'tiny')
        r = self.ok(ds, ['pparg', 'MARCO', 'MRC1', 'SIGLEC1'])   # case-insensitive
        rule = r['comparison']['rule']
        self.assertEqual(2, rule['min_markers'])                  # auto: ceil(4 / 2)
        self.assertEqual('auto', rule['min_markers_source'])
        self.assertFalse(rule['is_clustering'])
        self.assertFalse(rule['is_annotation'])
        # on = 'two' + 'neg' cells
        self.assertEqual({'c1': 0.3, 'c2': 0.4, 't1': 0.8, 't2': 0.7}, self.fractions(r))
        c1 = r['comparison']['per_sample']['c1']
        self.assertEqual((10, 3), (c1['n_cells'], c1['n_on_identity']))
        self.assertAlmostEqual(0.7, c1['marker_detection_rate']['pparg'])
        self.assertAlmostEqual(0.3, c1['marker_detection_rate']['MARCO'])
        self.assertAlmostEqual(0.1, c1['marker_detection_rate']['MRC1'])
        self.assertEqual(0.0, c1['marker_detection_rate']['SIGLEC1'])
        # MRC1 is only a symbol in var['feature_name']; the id in var_names is recorded.
        feats = {f['marker']: f for f in rule['marker_features']}
        self.assertEqual('ENSG00000260314', feats['MRC1']['var_name'])
        self.assertIn('feature_name', feats['MRC1']['matched_on'])

        main = r['statistics'][0]
        self.assertEqual('on-identity fraction (2 of 4 markers)', main['readout'])
        self.assertEqual('difference_in_fraction', main['effect_type'])
        self.assertAlmostEqual(0.35, main['mean_a'])
        self.assertAlmostEqual(0.75, main['mean_b'])
        self.assertAlmostEqual(0.40, main['effect'])
        self.assertEqual((2, 2, 4), (main['n_a'], main['n_b'], main['n']))
        self.assertIn('one fraction per sample', main['note'])
        markers = [s for s in r['statistics'][1:]]
        self.assertEqual(4, len(markers))
        self.assertTrue(all(s['readout'].startswith('marker detection rate: ') for s in markers))

        qc = self.qc(r)
        self.assertEqual('WARN', qc['cells per sample']['status'])     # 10 cells per sample
        self.assertIn('noisy', qc['cells per sample']['detail'])
        self.assertEqual('WARN', qc['group sizes']['status'])          # 2 samples per group
        self.assertEqual('WARN', qc['markers never detected']['status'])
        self.assertIn('SIGLEC1', qc['markers never detected']['detail'])
        self.assertIn('at least 2 of 4 markers', qc['identity rule']['detail'])
        self.assertIn('Not a clustering and not an annotation', qc['method']['detail'])

    def test_min_markers_changes_the_rule(self):
        ds = self.register(tiny_adata(), 'tiny-k3')
        r = self.ok(ds, MARKERS, ['min_markers=3'])
        self.assertEqual({'c1': 0.1, 'c2': 0.1, 't1': 0.2, 't2': 0.2}, self.fractions(r))
        self.assertEqual('on-identity fraction (3 of 4 markers)', r['statistics'][0]['readout'])
        self.assertEqual('plan', r['comparison']['rule']['min_markers_source'])

    def test_min_markers_beyond_the_markers_present_is_refused(self):
        ds = self.register(tiny_adata(), 'tiny-k9')
        msg = self.refused(ds, MARKERS, ['min_markers=9'])
        self.assertIn('between 1 and the 4 markers', msg)

    def test_negative_markers_put_a_cell_off_identity(self):
        ds = self.register(tiny_adata(), 'tiny-neg')
        r = self.ok(ds, MARKERS, ['negative_markers=CD3E,NOTAGENE'])
        self.assertEqual({'c1': 0.2, 'c2': 0.3, 't1': 0.6, 't2': 0.5}, self.fractions(r))
        rule = r['comparison']['rule']
        self.assertEqual(['CD3E'], rule['negative_markers'])
        self.assertEqual(['NOTAGENE'], rule['negative_markers_missing'])
        self.assertEqual(2, r['comparison']['per_sample']['t1']['n_excluded_by_negative_marker'])
        self.assertIn('none of CD3E', r['statistics'][0]['readout'])
        neg = self.qc(r)['negative markers']
        self.assertEqual('WARN', neg['status'])
        self.assertIn('NOTAGENE', neg['detail'])

    def test_detect_threshold_is_applied_strictly_above(self):
        ds = self.register(tiny_adata(), 'tiny-thr')
        r = self.ok(ds, MARKERS, ['detect_threshold=1'])
        # MARCO=1 in the 'two' cells is no longer detected: only 'neg' cells pass.
        self.assertEqual({'c1': 0.1, 'c2': 0.1, 't1': 0.2, 't2': 0.2}, self.fractions(r))

    def test_the_matrix_format_does_not_change_the_answer(self):
        """Dense, column- and row-major sparse; row chunks smaller than the matrix."""
        from unittest import mock
        from biosense.bioinformatics.toolkit.single_cell import identity_purity as IP
        ref = None
        for fmt, chunk in (('dense', IP.ROW_CHUNK), ('csc', IP.ROW_CHUNK),
                           ('csr', IP.ROW_CHUNK), ('csr', 7), ('dense', 7)):
            ds = self.register(tiny_adata(fmt), f'tiny-{fmt}-{chunk}')
            with mock.patch.object(IP, 'ROW_CHUNK', chunk):
                r = self.ok(ds, MARKERS)
            per = r['comparison']['per_sample']
            if ref is None:
                ref = per
            self.assertEqual(ref, per, fmt)

    def test_missing_markers_are_listed_not_substituted(self):
        ds = self.register(tiny_adata(), 'tiny-miss')
        r = self.ok(ds, MARKERS + ['ABCG1'])
        self.assertEqual(['ABCG1'], r['comparison']['rule']['markers_missing'])
        self.assertEqual('on-identity fraction (2 of 4 markers)', r['statistics'][0]['readout'])
        markers = self.qc(r)['markers']
        self.assertEqual('WARN', markers['status'])
        self.assertIn('4 of 5', markers['detail'])
        self.assertIn('ABCG1', markers['detail'])

    # ── refusals ──
    def test_too_few_markers_present_is_refused(self):
        ds = self.register(tiny_adata(), 'tiny-few')
        msg = self.refused(ds, ['PPARG', 'ABCG1', 'NOTAGENE'])
        self.assertIn('only 1 of the 3', msg)
        self.assertIn('ABCG1', msg)

    def test_no_markers_is_refused(self):
        ds = self.register(tiny_adata(), 'tiny-none')
        msg = self.refused(ds, [])
        self.assertIn('name the identity markers', msg)

    def test_one_marker_is_refused(self):
        ds = self.register(tiny_adata(), 'tiny-one')
        msg = self.refused(ds, ['PPARG'])
        self.assertIn('2 to 50 markers', msg)

    def test_a_sample_spanning_conditions_is_refused(self):
        ds = self.register(tiny_adata(spans=True), 'tiny-span')
        msg = self.refused(ds, MARKERS)
        self.assertIn("sample 't2' appears under more than one condition", msg)
        self.assertIn('spans groups', msg)

    def test_one_sample_per_group_is_refused(self):
        a = tiny_adata()
        a = a[a.obs['sample_id'].isin(['c1', 't1'])].copy()
        ds = self.register(a, 'tiny-thin')
        msg = self.refused(ds, MARKERS)
        self.assertIn('at least two samples per group', msg)
        self.assertIn('More cells do not help', msg)

    def test_an_unknown_option_is_refused(self):
        ds = self.register(tiny_adata(), 'tiny-opt')
        msg = self.refused(ds, MARKERS, ['cluster_resolution=0.8'])
        self.assertIn('unknown option(s) cluster_resolution', msg)
        self.assertIn('min_markers', msg)

    def test_a_marker_that_is_also_negative_is_refused(self):
        ds = self.register(tiny_adata(), 'tiny-both')
        msg = self.refused(ds, MARKERS, ['negative_markers=mrc1'])
        self.assertIn('both an identity marker and a negative marker', msg)

    # ── a realistic-sized synthetic design ──
    def test_the_treatment_raises_the_fraction_with_n_counting_samples(self):
        ds = self.register(larger_adata(), 'sc-larger')
        r = self.ok(ds, MARKERS + ['ABCG1'])
        main = r['statistics'][0]
        self.assertEqual('on-identity fraction (3 of 5 markers)', main['readout'])
        self.assertEqual('increase', main['direction'])
        self.assertEqual((3, 3, 6), (main['n_a'], main['n_b'], main['n']))
        self.assertGreater(main['effect'], 0.2)
        self.assertIsNotNone(main['p_value'])
        self.assertEqual(main['p_value'], main['q_value'])   # the primary readout stands alone
        for s in r['statistics']:
            self.assertEqual(6, s['n'])
        fr = self.fractions(r)
        self.assertTrue(all(fr[f'treated_D{i}'] > fr[f'control_D{j}']
                            for i in (1, 2, 3) for j in (1, 2, 3)))
        self.assertEqual(600, r['comparison']['cells_total'])
        qc = self.qc(r)
        self.assertEqual('PASS', qc['cells per sample']['status'])
        self.assertEqual('PASS', qc['group sizes']['status'])
        self.assertTrue(r['quality_control']['passed'])
        self.assertEqual('derived_analysis', r['evidence_class'])
        self.assertEqual(TOOL, r['method']['tool'])


if __name__ == '__main__':
    unittest.main()
