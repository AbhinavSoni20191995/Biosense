"""Real data readers: single-cell, chromatin, and the optional-environment rule.

The property that matters most: a tool that cannot run is NOT registered, so an
unavailable analysis is refused at planning rather than failing at execution —
which is the wrong end of the process to discover it.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.bioinformatics import registry as TREG
from biosense.data import ingest as ING
from biosense.data import roots as DR
from biosense.data.readers import h5ad as H5
from biosense.data.readers import peaks as PK

FIX = K.ROOT / 'examples' / 'datasets'
H5AD = FIX / 'sc_mcsf_synthetic.h5ad'
PEAK_A = FIX / 'atac_control_synthetic.narrowPeak'
PEAK_B = FIX / 'atac_mcsf_high_synthetic.narrowPeak'
ANCHORS = FIX / 'gene_anchors_synthetic.tsv'
needs_sc = unittest.skipUnless(H5.available(),
                               'needs the optional single-cell environment '
                               '(uv sync --extra singlecell)')


class OptionalEnvironmentTests(unittest.TestCase):
    def test_the_reader_reports_whether_its_extra_is_present(self):
        self.assertIsInstance(H5.available(), bool)

    def test_a_missing_extra_refuses_with_the_install_line(self):
        if H5.available():
            self.skipTest('the extra is installed here; the refusal path is covered below')
        with self.assertRaises(K.ContractError) as e:
            H5.require()
        self.assertIn('uv sync --extra singlecell', str(e.exception))
        self.assertIn('refuses rather than analysing something cheaper', str(e.exception))

    def test_a_tool_that_cannot_run_is_not_registered(self):
        """Refused at planning, not at execution."""
        if H5.available():
            self.assertIn('single_cell.pseudobulk_comparison', TREG.TOOLS)
        else:
            self.assertNotIn('single_cell.pseudobulk_comparison', TREG.TOOLS)
            with self.assertRaises(K.ContractError) as e:
                TREG.get('single_cell.pseudobulk_comparison')
            self.assertIn('optional environment', str(e.exception))

    def test_the_base_install_still_has_the_lightweight_tools(self):
        for name in ('cytometry.population_comparison', 'bulk.expression_comparison',
                     'chromatin.peak_overlap'):
            self.assertIn(name, TREG.TOOLS)

    def test_every_tool_declares_the_file_types_it_reads(self):
        for spec in TREG.TOOLS.values():
            self.assertTrue(spec.file_types, spec.name)

    def test_raw_fcs_is_refused_with_the_reason_it_is_not_supported(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'sample.fcs'
            p.write_bytes(b'FCS3.1 not really')
            with self.assertRaises(K.ContractError) as e:
                ING.ingest_local(p, dataset_id='fcs-x', title='t', modality='flow_cytometry')
            self.assertIn('fcsparser', str(e.exception))


class PeakReaderTests(unittest.TestCase):
    def test_a_narrowpeak_file_reads_with_its_extra_columns(self):
        peaks = PK.read_peaks(PEAK_A, 'narrowPeak')
        self.assertGreater(len(peaks), 100)
        self.assertIsNotNone(peaks[0]['q_value'])
        self.assertIn('chrom', peaks[0])

    def test_a_malformed_interval_is_refused_not_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'bad.bed'
            p.write_text('chr1\t500\t100\n')
            with self.assertRaises(K.ContractError) as e:
                PK.read_peaks(p)
            self.assertIn('ends before it starts', str(e.exception))

    def test_calling_peaks_is_explicitly_out_of_scope(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'signal.bigwig'
            p.write_bytes(b'x')
            with self.assertRaises(K.ContractError) as e:
                PK.read_peaks(p)
            self.assertIn('run MACS or an established pipeline', str(e.exception))

    def test_overlap_reports_both_directions(self):
        a, b = PK.read_peaks(PEAK_A, 'narrowPeak'), PK.read_peaks(PEAK_B, 'narrowPeak')
        ov = PK.overlap(a, b)
        self.assertNotEqual(ov['frac_a_overlapping'], ov['frac_b_overlapping'])
        self.assertIn('different questions', ov['note'])

    def test_a_distal_peak_is_not_called_an_enhancer(self):
        from biosense.data import tables as TB
        t = TB.read_table(ANCHORS, 'tsv')
        anchors = [{'gene': r['gene'], 'chrom': r['chrom'], 'tss': int(r['tss']),
                    'strand': r['strand']} for r in t.rows]
        ann = PK.annotate_to_genes(PK.read_peaks(PEAK_A, 'narrowPeak')[:40], anchors)
        kinds = {x['class'] for x in ann['annotations']}
        self.assertTrue(kinds <= {'promoter_proximal', 'distal', 'unassigned'})
        self.assertIn('not called enhancers', ann['note'])

    def test_describe_says_biosense_did_not_call_the_peaks(self):
        d = PK.describe(PEAK_A, 'narrowPeak')
        self.assertIn('did not call these peaks', d['note'])
        self.assertGreater(d['n_peaks'], 0)


class PrivateRootCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = os.environ.get(DR.PRIVATE_ENV)
        os.environ[DR.PRIVATE_ENV] = str(self.tmp / 'private')
        os.environ[DR.CACHE_ENV] = str(self.tmp / 'cache')
        DR.ensure_roots()

    def tearDown(self):
        if self._old is None:
            os.environ.pop(DR.PRIVATE_ENV, None)
        else:
            os.environ[DR.PRIVATE_ENV] = self._old
        os.environ.pop(DR.CACHE_ENV, None)
        shutil.rmtree(self.tmp, ignore_errors=True)


class ChromatinIngestTests(PrivateRootCase):
    def ingest(self, **kw):
        kw.setdefault('peak_files', {'control': str(PEAK_A), 'mcsf_high': str(PEAK_B)})
        return ING.ingest_peaks(
            PEAK_A, extra_files=[PEAK_B], dataset_id=kw.pop('dataset_id', 'atac-1'),
            title='ATAC peaks under two conditions', modality='atac_seq',
            perturbation='M-CSF concentration',
            experimental_design={'condition_column': 'condition', 'control': 'control',
                                 'treatments': ['mcsf_high'], 'replicate_column': 'condition'},
            gene_anchors=str(ANCHORS), **kw)

    def test_a_peak_dataset_validates_and_is_private(self):
        m, path = self.ingest()
        self.assertEqual([], K.schema_errors('dataset_manifest', m))
        self.assertEqual('atac_seq', m['modality'])
        self.assertEqual('private', m['visibility'])
        self.assertTrue(DR.is_private_path(path))

    def test_the_condition_to_file_mapping_is_recorded_not_guessed(self):
        m, _ = self.ingest()
        self.assertEqual({'control', 'mcsf_high'}, set(m['modality_detail']['peak_files']))

    def test_a_comparison_without_that_mapping_is_refused(self):
        from biosense.bioinformatics import execute as EX
        from biosense.bioinformatics import plan as PLAN
        m, _ = self.ingest(dataset_id='atac-nomap', peak_files={})
        p = PLAN.plan(plan_id='atac-nomap-1',
                      question='Do the accessible regions differ between conditions here?',
                      uncertainty_ref=PLAN.evidence_gap('G', 'no chromatin evidence yet exists'),
                      recorded_gaps=['G'], why_requested='the loop holds no chromatin data',
                      dataset_ids=['atac-nomap'], analysis_type='peak_overlap_comparison',
                      tool='chromatin.peak_overlap',
                      decision_relevance='whether M-CSF changes accessibility',
                      parameters_that_may_change=['mcsf_ng_ml'])
        with self.assertRaises(K.ContractError) as e:
            EX.execute(p)
        self.assertIn('guessing from filenames', str(e.exception))

    def test_the_comparison_runs_and_warns_about_what_it_cannot_establish(self):
        from biosense.bioinformatics import execute as EX
        from biosense.bioinformatics import plan as PLAN
        self.ingest(dataset_id='atac-ok')
        p = PLAN.plan(plan_id='atac-ok-1',
                      question='Do the accessible regions differ between conditions here?',
                      uncertainty_ref=PLAN.evidence_gap('G', 'no chromatin evidence yet exists'),
                      recorded_gaps=['G'], why_requested='the loop holds no chromatin data',
                      dataset_ids=['atac-ok'], analysis_type='peak_overlap_comparison',
                      tool='chromatin.peak_overlap',
                      decision_relevance='whether M-CSF changes accessibility',
                      parameters_that_may_change=['mcsf_ng_ml'])
        r = EX.execute(p)
        self.assertEqual([], K.schema_errors('analysis_result', r))
        warns = ' '.join(c['detail'] for c in r['quality_control']['checks'])
        self.assertIn('not replicated samples', warns)
        self.assertIn('did not call these peaks', warns)
        self.assertIsNotNone(r['comparison']['annotation'])


@needs_sc
class SingleCellTests(PrivateRootCase):
    def ingest(self, dataset_id='sc-1', **design):
        d = {'condition_column': 'condition', 'control': 'control',
             'treatments': ['mcsf_high'], 'sample_id_column': 'sample_id',
             'donor_column': 'donor', 'replicate_column': 'sample_id'}
        d.update(design)
        return ING.ingest_h5ad(H5AD, dataset_id=dataset_id,
                               title='Single-cell under two M-CSF conditions',
                               cell_type='iPSC-derived monocyte',
                               perturbation='M-CSF concentration', experimental_design=d)

    def plan(self, dataset_id='sc-1', plan_id='sc-plan'):
        from biosense.bioinformatics import plan as PLAN
        return PLAN.plan(plan_id=plan_id,
                         question='Does the higher M-CSF condition shift myeloid identity?',
                         uncertainty_ref=PLAN.evidence_gap('GAP-sc',
                                                           'no single-cell evidence exists yet'),
                         recorded_gaps=['GAP-sc'],
                         why_requested='the loop holds no single-cell measurement',
                         dataset_ids=[dataset_id], analysis_type='pseudobulk_comparison',
                         tool='single_cell.pseudobulk_comparison',
                         decision_relevance='whether M-CSF is worth testing higher',
                         parameters_that_may_change=['mcsf_ng_ml'])

    def test_the_manifest_records_what_is_there_and_what_is_missing(self):
        m, _ = self.ingest()
        self.assertEqual([], K.schema_errors('dataset_manifest', m))
        d = m['modality_detail']
        self.assertEqual(720, d['n_cells'])
        self.assertEqual(20, d['n_genes'])
        self.assertIn('condition', d['obs_keys'])
        self.assertIn('normalization_status', d)
        self.assertIn('condition', d['design_candidates'])

    def test_design_columns_are_listed_as_candidates_not_chosen(self):
        from biosense.data.readers import h5ad as H
        desc = H.describe(H5AD)
        self.assertIn('does not pick which column', desc['note'])

    def test_a_design_column_that_is_not_in_obs_is_refused(self):
        with self.assertRaises(K.ContractError) as e:
            self.ingest(dataset_id='sc-bad', condition_column='treatment_group')
        self.assertIn('treatment_group', str(e.exception))

    def test_the_replication_unit_is_the_sample_not_the_cell(self):
        """720 cells, 6 samples. n must be 3 per group."""
        from biosense.bioinformatics import execute as EX
        self.ingest()
        r = EX.execute(self.plan())
        for s in r['statistics']:
            self.assertEqual(3, s['n_a'])
            self.assertEqual(3, s['n_b'])
        self.assertEqual(720, r['comparison']['cells_total'])
        self.assertIn('replication unit is the sample', r['comparison']['independence_note'])

    def test_a_sample_column_that_spans_both_groups_is_refused(self):
        """Each donor gave both a control and a treated sample, so the donor is
        not the unit of replication for this contrast: it is paired across it."""
        from biosense.bioinformatics import execute as EX
        self.ingest(dataset_id='sc-donor', sample_id_column='donor')
        with self.assertRaises(K.ContractError) as e:
            EX.execute(self.plan(dataset_id='sc-donor', plan_id='sc-donor-1'))
        self.assertIn('more than one condition', str(e.exception))
        self.assertIn('spans groups', str(e.exception))

    def test_it_refuses_a_design_with_too_few_samples(self):
        """One sample per group. The refusal has to say that more cells are not
        the answer, or a reader will reach for a bigger dissociation."""
        import anndata as ad
        from biosense.bioinformatics import execute as EX
        full = ad.read_h5ad(H5AD)
        keep = full.obs['sample_id'].astype(str).isin(['control_D1', 'mcsf_high_D1'])
        thin = self.tmp / 'one_per_group.h5ad'
        full[keep].copy().write_h5ad(thin)
        ING.ingest_h5ad(thin, dataset_id='sc-thin',
                        title='One sample per group',
                        cell_type='iPSC-derived monocyte',
                        perturbation='M-CSF concentration',
                        experimental_design={'condition_column': 'condition',
                                             'control': 'control',
                                             'treatments': ['mcsf_high'],
                                             'sample_id_column': 'sample_id',
                                             'donor_column': 'donor',
                                             'replicate_column': 'sample_id'})
        with self.assertRaises(K.ContractError) as e:
            EX.execute(self.plan(dataset_id='sc-thin', plan_id='sc-thin-1'))
        self.assertIn('at least two samples per group', str(e.exception))
        self.assertIn('More cells do not help', str(e.exception))

    def test_signature_scores_are_reported_beside_the_genes(self):
        from biosense.bioinformatics import execute as EX
        self.ingest()
        r = EX.execute(self.plan())
        readouts = {s['readout'] for s in r['statistics']}
        self.assertIn('CD14', readouts)
        self.assertTrue(any(x.startswith('score:') for x in readouts))

    def test_it_records_that_it_did_not_recluster(self):
        from biosense.bioinformatics import execute as EX
        self.ingest()
        r = EX.execute(self.plan())
        qc = ' '.join(c['detail'] for c in r['quality_control']['checks'])
        self.assertIn('no reclustering, re-annotation or UMAP', qc)

    def test_the_result_keeps_its_lineage(self):
        from biosense.bioinformatics import execute as EX
        self.ingest()
        r = EX.execute(self.plan())
        self.assertEqual('derived_analysis', r['evidence_class'])
        self.assertEqual('private_user_dataset', r['source_evidence_class'])
        self.assertEqual('private', r['source_visibility'])


if __name__ == '__main__':
    unittest.main()
