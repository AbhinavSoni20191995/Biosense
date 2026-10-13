"""Peak sets and single-cell series, brought in and analysed — without the network.

Three of a run's most decision-relevant series were lost to plumbing: one held
only `.narrowPeak.gz` files, one was a single-cell time course nothing could
register. These replay those shapes offline: a GEO ATAC series becomes a
peak-set dataset `chromatin.peak_overlap` runs on, and a Matrix Market count
matrix becomes a per-sample pseudobulk with its time axis.
"""
import gzip
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from biosense import contracts as K
from biosense.data.readers import peaks as PK
from biosense.data.sources import geo_fetch as GF
from biosense.data.sources import single_cell as SC
from tests.test_geo_fetch import Sandbox, fake_get

ACC = 'GSE243165'
GSMS = ['GSM1', 'GSM2', 'GSM3', 'GSM4']
FTP = 'ftp://ftp.ncbi.nlm.nih.gov/geo/samples/GSM1nnn'


def matrix_text(lines):
    return '\n'.join([
        '!Series_title\t"RXR knockout alveolar macrophages"',
        '!Sample_title\t"WT_1"\t"WT_2"\t"RXR_KO_1"\t"RXR_KO_2"',
        '!Sample_geo_accession\t' + '\t'.join(f'"{g}"' for g in GSMS),
        '!Sample_source_name_ch1\t' + '\t'.join(['"alveolar macrophage"'] * 4),
        '!Sample_organism_ch1\t' + '\t'.join(['"Mus musculus"'] * 4),
        '!Sample_library_strategy\t' + '\t'.join(['"ATAC-seq"'] * 4),
        *lines,
        '!series_matrix_table_begin', '!series_matrix_table_end'])


SUPP = '!Sample_supplementary_file_1\t' + '\t'.join(
    f'"{FTP}/{g}/suppl/{g}_peaks.narrowPeak.gz"' for g in GSMS)
NP = '{c}\t{s}\t{e}\tp\t10\t.\t5.0\t8.0\t{q}\t50'
WT = '\n'.join(NP.format(c='chr1', s=s, e=s + 300, q=4.0) for s in range(1000, 20000, 1000))
KO = '\n'.join(NP.format(c='chr1', s=s, e=s + 300, q=3.0) for s in range(1000, 10000, 1000))
SERVED = {'_series_matrix.txt.gz': matrix_text([SUPP]),
          'GSM1_peaks': WT, 'GSM2_peaks': WT, 'GSM3_peaks': KO, 'GSM4_peaks': KO}


class PeakReaderTests(unittest.TestCase):
    def test_a_peak_file_is_known_by_its_name_in_any_case_gzipped_or_not(self):
        for name, ft in (('x.narrowPeak.gz', 'narrowPeak'), ('X.NARROWPEAK', 'narrowPeak'),
                         ('y_peaks.broadPeak', 'broadPeak'), ('z.bed.gz', 'bed'),
                         ('w.bw', None)):
            self.assertEqual(ft, PK.peak_type(name), name)

    def test_consensus_keeps_what_half_the_replicates_share(self):
        a = [{'chrom': 'chr1', 'start': 0, 'end': 100}, {'chrom': 'chr1', 'start': 500, 'end': 600}]
        b = [{'chrom': 'chr1', 'start': 50, 'end': 150}]
        c = [{'chrom': 'chr2', 'start': 0, 'end': 10}]
        got = PK.consensus([a, b, c], 2)
        self.assertEqual([('chr1', 0, 150, 2.0)],
                         [(p['chrom'], p['start'], p['end'], p['score']) for p in got])
        self.assertEqual(3, len(PK.consensus([a, b, c], 1)), 'union')


class FetchPeaksTests(Sandbox):
    def fetch(self, *extra, served=SERVED):
        with mock.patch.object(GF, '_get', fake_get(served)):
            return self.cli('datasets', 'fetch-peaks', '--accession', ACC, '--condition-key',
                            'title~(RXR_KO|WT)', '--control', 'WT', '--treatment', 'RXR_KO',
                            '--i-have-network-permission', *extra)

    def test_replicates_are_combined_only_when_asked(self):
        code, d = self.fetch()
        self.assertEqual(1, code)
        self.assertIn('--replicates consensus', json.dumps(d))

    def test_a_series_of_narrowpeak_gz_files_becomes_a_peak_dataset_and_is_compared(self):
        code, d = self.fetch('--replicates', 'consensus')
        self.assertEqual(0, code, d)
        self.assertEqual('atac_seq', d['modality'], 'read from the library strategy')
        self.assertEqual({'WT': 2, 'RXR_KO': 2}, d['samples_per_group'])
        self.assertTrue(any('merged by BioSense (consensus)' in x for x in d['limitations_added']))
        from biosense.data import registry as REG
        m = REG.require(d['dataset_id'])
        self.assertEqual(('public', 'geo', ACC), (m['visibility'], m['source'], m['accession']))
        self.assertEqual({'WT': ['GSM1', 'GSM2'], 'RXR_KO': ['GSM3', 'GSM4']},
                         m['modality_detail']['replicates'])
        code, plan = self.cli(
            'analyse', 'plan', '--plan-id', 'plan-01', '--question', 'Do RXR-dependent sites close?',
            '--evidence-gap', 'U2', '--uncertainty', 'whether the lineage gates open',
            '--why', 'it decides whether RXR agonism belongs in maturation',
            '--dataset-ids', d['dataset_id'], '--analysis-type', 'peak_overlap_comparison',
            '--tool', 'chromatin.peak_overlap', '--decision-relevance', 'RXR agonist',
            '--control', 'WT', '--treatment-level', 'RXR_KO', '--out', str(self.tmp / 'p.json'))
        self.assertEqual(0, code, plan)
        code, res = self.cli('analyse', 'run', '--plan', str(self.tmp / 'p.json'))
        self.assertEqual(0, code, res)
        self.assertIn('peaks_shared', json.dumps(res['statistics']))

    def test_a_series_with_only_signal_tracks_says_peaks_would_have_to_be_called(self):
        served = dict(SERVED, **{'_series_matrix.txt.gz': matrix_text([
            '!Sample_supplementary_file_1\t' + '\t'.join(
                f'"{FTP}/{g}/suppl/{g}.bw"' for g in GSMS)])})
        code, d = self.fetch('--replicates', 'union', served=served)
        self.assertEqual(1, code)
        self.assertIn('peaks would have to be called', json.dumps(d))


def write_gz(path, text):
    with gzip.open(path, 'wt') as fh:
        fh.write(text)
    return path


class SCFiles(Sandbox):
    """Twenty-four cells, eight samples, two days, two annotated populations."""

    def setUp(self):
        super().setUp()
        d = self.tmp / 'sc'
        d.mkdir()
        genes = ['ENSG1\tCSF2RB', 'ENSG2\tACTB', 'ENSG3\tKDR']
        cells, entries = [], []
        k = 0
        for sample, cond, day in (('s1', 'ctrl', 'D2'), ('s2', 'ctrl', 'D2'),
                                  ('s3', 'BMP4', 'D2'), ('s4', 'BMP4', 'D2'),
                                  ('s5', 'ctrl', 'D4'), ('s6', 'ctrl', 'D4'),
                                  ('s7', 'BMP4', 'D4'), ('s8', 'BMP4', 'D4')):
            for t in ('HE', 'HE', 'endo'):
                k += 1
                cells.append(f'bc{k},{sample},{cond},{day},{t}')
                lift = 3 if (cond == 'BMP4' and day == 'D4' and t == 'HE') else 1
                entries += [(1, k, 10 * lift), (2, k, 100), (3, k, 5 if t == 'HE' else 50)]
        mtx = ['%%MatrixMarket matrix coordinate integer general', '% genes x cells',
               f'3 {k} {len(entries)}'] + [f'{i} {j} {v}' for i, j, v in entries]
        self.files = dict(
            matrix=write_gz(d / 'matrix.mtx.gz', '\n'.join(mtx) + '\n'),
            features=write_gz(d / 'features.tsv.gz', '\n'.join(genes) + '\n'),
            barcodes=write_gz(d / 'barcodes.tsv.gz', '\n'.join(f'bc{i}' for i in
                                                              range(1, k + 1)) + '\n'),
            cells=write_gz(d / 'cells.csv.gz', 'cell,sample,condition,day,celltype\n'
                           + '\n'.join(cells) + '\n'))
        self.kw = dict(cell_id_column='cell', sample_column='sample',
                       condition_column='condition', control='ctrl', treatments=['BMP4'])


class SingleCellTests(SCFiles):

    def test_one_population_summed_per_sample_with_its_time_axis(self):
        rows, stats = SC.build(**self.files, **self.kw, time_column='day',
                               cell_type_column='celltype', cell_types=['HE'], min_cells=1)
        self.assertEqual(['day 2', 'day 4'], stats['timepoints'])
        self.assertEqual({f's{i}': 2 for i in range(1, 9)}, stats['cells_per_sample'])
        self.assertEqual({'gene', 'sample_id', 'condition', 'logcpm', 'timepoint', 'n_cells'},
                         set(rows[0]))
        self.assertEqual({'CSF2RB', 'ACTB', 'KDR'}, {r['gene'] for r in rows})

    def test_normalised_values_and_unannotated_populations_are_refused(self):
        bad = self.tmp / 'sc' / 'norm.mtx'
        bad.write_text('%%MatrixMarket matrix coordinate real general\n3 24 1\n1 1 0.5\n')
        with self.assertRaisesRegex(K.ContractError, 'normalised'):
            SC.build(**dict(self.files, matrix=bad), **self.kw, min_cells=1)
        with self.assertRaisesRegex(K.ContractError, 'never inferred'):
            SC.build(**self.files, **self.kw, cell_types=['HE'], min_cells=1)

    def test_a_series_is_fetched_registered_and_compared_at_one_timepoint(self):
        listing = ''.join(f'<a href="{p.name}">{p.name}</a>' for p in self.files.values())
        copy = lambda url, dest, **_: Path(shutil.copy(self.files_by_name[url.rsplit('/', 1)[-1]],
                                                       dest))
        self.files_by_name = {p.name: p for p in self.files.values()}
        with mock.patch.object(GF, '_get', fake_get({'/suppl/': listing})), \
                mock.patch.object(SC, 'download', copy):
            code, d = self.cli('datasets', 'fetch-sc', '--accession', 'GSE274082',
                               '--matrix', 'matrix.mtx.gz', '--features', 'features.tsv.gz',
                               '--barcodes', 'barcodes.tsv.gz', '--cells', 'cells.csv.gz',
                               '--cell-id-column', 'cell', '--sample-column', 'sample',
                               '--condition-column', 'condition', '--control', 'ctrl',
                               '--treatment', 'BMP4', '--time-column', 'day',
                               '--cell-type-column', 'celltype', '--population', 'HE',
                               '--i-have-network-permission')
        self.assertEqual(1, code, 'two cells per sample is below the floor')
        self.assertIn('20 or more cells', json.dumps(d))
        with mock.patch.object(GF, '_get', fake_get({'/suppl/': listing})), \
                mock.patch.object(SC, 'download', copy):
            code, d = self.cli('datasets', 'fetch-sc', '--accession', 'GSE274082',
                               '--matrix', 'matrix.mtx.gz', '--features', 'features.tsv.gz',
                               '--barcodes', 'barcodes.tsv.gz', '--cells', 'cells.csv.gz',
                               '--cell-id-column', 'cell', '--sample-column', 'sample',
                               '--condition-column', 'condition', '--control', 'ctrl',
                               '--treatment', 'BMP4', '--time-column', 'day',
                               '--cell-type-column', 'celltype', '--population', 'HE',
                               '--min-cells', '2', '--i-have-network-permission')
        self.assertEqual(0, code, d)
        self.assertIn('--timepoint', d['next'])
        code, plan = self.cli(
            'analyse', 'plan', '--plan-id', 'plan-02', '--question', 'When does CSF2RB rise?',
            '--evidence-gap', 'U1', '--uncertainty', 'the receptor window',
            '--why', 'GM-CSF is a window', '--dataset-ids', d['dataset_id'],
            '--analysis-type', 'bulk_expression_comparison', '--tool',
            'bulk.expression_comparison', '--decision-relevance', 'GM-CSF start day',
            '--group-column', 'condition', '--control', 'ctrl', '--treatment-level', 'BMP4',
            '--timepoint', 'day 4', '--out', str(self.tmp / 'p.json'))
        self.assertEqual(0, code, plan)
        code, res = self.cli('analyse', 'run', '--plan', str(self.tmp / 'p.json'))
        self.assertEqual(0, code, res)
        row = next(r for r in res['statistics'] if 'CSF2RB' in json.dumps(r)[:120])
        self.assertGreater(row['effect'], 0)
        self.assertTrue(any('Compared at day 4 only' in x for x in res['limitations']))


class LocalRouteTests(SCFiles):
    def test_local_single_cell_files_become_a_private_pseudobulk(self):
        code, d = self.cli('datasets', 'pseudobulk', '--title', 'my EHT time course',
                           '--dataset-id', 'my_eht', '--matrix', str(self.files['matrix']),
                           '--features', str(self.files['features']),
                           '--barcodes', str(self.files['barcodes']),
                           '--cells', str(self.files['cells']), '--cell-id-column', 'cell',
                           '--sample-column', 'sample', '--condition-column', 'condition',
                           '--control', 'ctrl', '--treatment', 'BMP4', '--time-column', 'day',
                           '--min-cells', '1')
        self.assertEqual(0, code, d)
        from biosense.data import registry as REG
        m = REG.require('my_eht')
        self.assertEqual(('private', 'generic_table'), (m['visibility'], m['modality']))
        self.assertEqual('timepoint', m['experimental_design']['timepoint_column'])

    def test_local_peak_files_register_by_condition(self):
        a = write_gz(self.tmp / 'wt.narrowPeak.gz', WT + '\n')
        b = write_gz(self.tmp / 'ko.narrowPeak.gz', KO + '\n')
        code, d = self.cli('datasets', 'register-peaks', '--peaks', f'WT={a}', f'KO={b}',
                           '--control', 'WT', '--treatment', 'KO', '--dataset-id', 'my_atac',
                           '--title', 'my ATAC')
        self.assertEqual(0, code, d)
        from biosense.data import registry as REG
        m = REG.require('my_atac')
        self.assertEqual({'narrowPeak'}, {f['file_type'] for f in m['files']},
                         'a gzipped narrowPeak is a narrowPeak, not "other"')

    def test_a_gzipped_tab_table_registers_locally(self):
        t = write_gz(self.tmp / 'counts.tab.gz', 'gene\tsample_id\tcondition\tlogcpm\n'
                     + ''.join(f'G{g}\tS{i}\t{"a" if i < 3 else "b"}\t{g + i * 0.1}\n'
                               for g in range(3) for i in range(6)))
        code, d = self.cli('datasets', 'register', '--file', str(t), '--dataset-id', 'tabgz',
                           '--title', 'tab gz', '--modality', 'bulk_rna',
                           '--condition-column', 'condition', '--control', 'a',
                           '--treatment', 'b')
        self.assertEqual(0, code, d)


if __name__ == '__main__':
    unittest.main()
