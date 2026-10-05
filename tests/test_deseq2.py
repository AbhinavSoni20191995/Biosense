"""The DESeq2 adapter: the handoff into R, and the parsing back out.

R is not installed in ordinary CI and must never be needed by it. That is the
reason this file exists in the shape it does: the two steps where a count-level
analysis goes silently wrong are building the counts matrix and reading the
results back, and both are pure functions over files, so both are tested on
every machine with no R anywhere. What is left for R is the model itself.
"""
import csv
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.bioinformatics import external as EXT
from biosense.bioinformatics import registry as TREG
from biosense.bioinformatics.toolkit.bulk import deseq2 as D
from biosense.data import tables as TB

COUNTS = K.ROOT / 'examples' / 'datasets' / 'bulk_counts_mcsf_synthetic.tsv'

MANIFEST = {'experimental_design': {'condition_column': 'condition', 'control': 'control',
                                    'treatments': ['mcsf_high'], 'sample_id_column': 'sample'}}
PLAN = {'comparison': {}}


def table(path=COUNTS):
    return TB.read_table(path)


class RegistrationTests(unittest.TestCase):
    def test_without_r_the_tool_is_not_registered(self):
        """Not 'registered but fails later': the alternative to DESeq2 is a Welch
        test over counts, which is the wrong answer rather than a cheaper one."""
        if EXT.available('deseq2'):
            self.assertIn(D.NAME, TREG.TOOLS)
            return
        self.assertNotIn(D.NAME, TREG.TOOLS)
        with self.assertRaises(K.ContractError) as e:
            TREG.get(D.NAME)
        self.assertIn('needs an external program', str(e.exception))
        self.assertIn('DESeq2', str(e.exception))

    def test_it_counts_as_implemented_even_where_it_cannot_run(self):
        """'needs R' and 'nobody wrote it' are different claims, and only the
        second is a reason for a document to show the capability as absent."""
        self.assertTrue(TREG.is_implemented(D.NAME))
        self.assertFalse(TREG.is_implemented('cytometry.gating'))

    def test_the_r_script_is_committed_and_sets_the_reference_level(self):
        """Without relevel() the sign of every fold change would depend on how
        the conditions happen to be spelled."""
        src = D.SCRIPT.read_text()
        self.assertIn('relevel', src)
        self.assertIn('DESeqDataSetFromMatrix', src)
        self.assertIn('identical(colnames(counts), rownames(coldata))', src)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, **kw):
        return D.write_inputs(kw.pop('table', None) or table(), kw.pop('manifest', MANIFEST),
                              kw.pop('plan', PLAN), self.tmp)

    def read_tsv(self, name):
        with (self.tmp / name).open(newline='') as fh:
            return list(csv.reader(fh, delimiter='\t'))

    def test_the_two_files_agree_on_sample_order(self):
        """The one mistake the R script cannot catch for us if we get it wrong
        here consistently: every count column labelled with another sample's
        condition still produces a plausible table."""
        io = self.write()
        counts, coldata = self.read_tsv('counts.tsv'), self.read_tsv('coldata.tsv')
        self.assertEqual(io['samples'], counts[0][1:])
        self.assertEqual(io['samples'], [r[0] for r in coldata[1:]])

    def test_each_count_lands_under_its_own_sample(self):
        io = self.write()
        counts = self.read_tsv('counts.tsv')
        header, by_gene = counts[0], {r[0]: r for r in counts[1:]}
        src = table()
        for r in src.rows[:40]:
            col = header.index(r['sample'])
            self.assertEqual(int(r['count']), int(by_gene[r['gene']][col]),
                             f'{r["gene"]} / {r["sample"]}')
        self.assertEqual(io['n_control'], 3)
        self.assertEqual(io['n_treatment'], 3)

    def test_the_coldata_condition_matches_the_sample(self):
        self.write()
        for sample, cond in self.read_tsv('coldata.tsv')[1:]:
            self.assertTrue(sample.startswith(cond), f'{sample} labelled {cond}')

    def test_normalised_values_are_refused(self):
        """DESeq2 fits dispersion to counts; normalised values break that
        silently rather than loudly."""
        p = self.tmp / 'norm.tsv'
        with p.open('w', newline='') as fh:
            w = csv.writer(fh, delimiter='\t', lineterminator='\n')
            w.writerow(['gene', 'sample', 'condition', 'count'])
            for i in range(6):
                w.writerow(['CD14', f's{i}', 'control' if i < 3 else 'mcsf_high', 3.25 + i])
        with self.assertRaises(K.ContractError) as e:
            self.write(table=TB.read_table(p))
        self.assertIn('not raw integer counts', str(e.exception))
        self.assertIn('bulk.expression_comparison', str(e.exception))

    def test_one_replicate_per_group_is_refused_with_the_reason(self):
        """The tempting fix is to sequence deeper, and it is the wrong one."""
        p = self.tmp / 'thin.tsv'
        with p.open('w', newline='') as fh:
            w = csv.writer(fh, delimiter='\t', lineterminator='\n')
            w.writerow(['gene', 'sample', 'condition', 'count'])
            for s, c in (('a', 'control'), ('b', 'mcsf_high')):
                w.writerow(['CD14', s, c, 500])
        with self.assertRaises(K.ContractError) as e:
            self.write(table=TB.read_table(p))
        self.assertIn('at least two samples per group', str(e.exception))
        self.assertIn('Sequencing deeper does not help', str(e.exception))

    def test_a_sample_in_both_arms_is_refused(self):
        p = self.tmp / 'both.tsv'
        with p.open('w', newline='') as fh:
            w = csv.writer(fh, delimiter='\t', lineterminator='\n')
            w.writerow(['gene', 'sample', 'condition', 'count'])
            for i, c in enumerate(['control', 'mcsf_high'] * 2):
                w.writerow([f'G{i}', 'shared', c, 100])
        with self.assertRaises(K.ContractError) as e:
            self.write(table=TB.read_table(p))
        self.assertIn('more than one condition', str(e.exception))

    def test_a_duplicated_feature_is_refused_rather_than_summed(self):
        p = self.tmp / 'dup.tsv'
        with p.open('w', newline='') as fh:
            w = csv.writer(fh, delimiter='\t', lineterminator='\n')
            w.writerow(['gene', 'sample', 'condition', 'count'])
            for s, c in (('a1', 'control'), ('a2', 'control'),
                         ('b1', 'mcsf_high'), ('b2', 'mcsf_high')):
                w.writerow(['CD14', s, c, 500])
            w.writerow(['CD14', 'a1', 'control', 700])
        with self.assertRaises(K.ContractError) as e:
            self.write(table=TB.read_table(p))
        self.assertIn('appears twice', str(e.exception))
        self.assertIn('invent a library', str(e.exception))

    def test_a_missing_sample_column_is_refused_and_says_why_it_matters(self):
        m = {'experimental_design': dict(MANIFEST['experimental_design'])}
        m['experimental_design'].pop('sample_id_column')
        with self.assertRaises(K.ContractError) as e:
            self.write(manifest=m)
        self.assertIn('sample_id_column', str(e.exception))


class ResultParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def results(self, rows):
        p = self.tmp / 'res.tsv'
        cols = ['feature', 'base_mean', 'log2_fold_change', 'lfc_se', 'stat',
                'p_value', 'q_value', 'shrinkage']
        with p.open('w', newline='') as fh:
            w = csv.writer(fh, delimiter='\t', lineterminator='\n')
            w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c, 'NA') for c in cols])
        return p

    def test_it_reads_the_table_into_statistics_rows(self):
        p = self.results([{'feature': 'CD14', 'base_mean': 900, 'log2_fold_change': 1.2,
                           'lfc_se': 0.3, 'stat': 4.0, 'p_value': 1e-5, 'q_value': 2e-4,
                           'shrinkage': 'apeglm'}])
        rows, filtered = D.parse_results(p, control='control', treatment='mcsf_high')
        self.assertEqual(0, filtered)
        r = rows[0]
        self.assertEqual('CD14', r['readout'])
        self.assertEqual('log2_fold_change', r['effect_type'])
        self.assertAlmostEqual(1.2, r['effect'])
        self.assertAlmostEqual(1.2 - 1.96 * 0.3, r['effect_ci_low'])
        self.assertEqual('control', r['group_a'])
        self.assertEqual('mcsf_high', r['group_b'])
        self.assertIn('DESeq2', r['test'])

    def test_a_filtered_feature_is_dropped_not_called_not_significant(self):
        """Independent filtering removed it before the correction. Giving it q=1
        would both overstate how many features were tested and understate every
        other q in the table."""
        p = self.results([
            {'feature': 'CD14', 'base_mean': 900, 'log2_fold_change': 1.2, 'lfc_se': 0.3,
             'p_value': 1e-5, 'q_value': 2e-4, 'shrinkage': 'apeglm'},
            {'feature': 'RARE', 'base_mean': 0.2, 'log2_fold_change': 0.1, 'lfc_se': 4.0,
             'p_value': 0.9, 'q_value': 'NA', 'shrinkage': 'apeglm'}])
        rows, filtered = D.parse_results(p, control='control', treatment='mcsf_high')
        self.assertEqual(1, filtered)
        self.assertEqual(['CD14'], [r['readout'] for r in rows])

    def test_a_truncated_output_is_refused_naming_what_is_missing(self):
        p = self.tmp / 'bad.tsv'
        p.write_text('feature\tlog2_fold_change\nCD14\t1.2\n')
        with self.assertRaises(K.ContractError) as e:
            D.parse_results(p, control='control', treatment='mcsf_high')
        self.assertIn('p_value', str(e.exception))
        self.assertIn('q_value', str(e.exception))

    def test_an_all_filtered_table_is_refused_rather_than_returned_empty(self):
        p = self.results([{'feature': 'RARE', 'log2_fold_change': 0.1, 'p_value': 0.9,
                           'q_value': 'NA'}])
        with self.assertRaises(K.ContractError) as e:
            D.parse_results(p, control='control', treatment='mcsf_high')
        self.assertIn('no feature with an adjusted p-value', str(e.exception))


class MockIsNotEvidenceTests(unittest.TestCase):
    def test_a_mock_record_is_structurally_complete_and_marked(self):
        rec = EXT.run_mock('deseq2', inputs=[COUNTS])
        self.assertTrue(rec['mock'])
        self.assertEqual('mock-0', rec['tool_version'])

    def test_the_tool_refuses_to_build_a_result_from_a_mock(self):
        """Exercising the contract is the mock's whole purpose; a table of fold
        changes that no model produced is not a by-product it may have."""
        src = Path(D.__file__).read_text()
        self.assertIn("if rec.get('mock'):", src)
        self.assertIn('came from no model', src)


if __name__ == '__main__':
    unittest.main()


class WiringTests(unittest.TestCase):
    """Does run() hold the pieces together: argv, the execution record, parsing?

    Run against a stub on PATH that answers like Rscript and writes a fixed
    table. It says nothing whatever about DESeq2 — the numbers it returns were
    typed into the stub — and it is not a substitute for running the real thing,
    which has not happened here. What it does cover is the wiring between the
    three parts, which is otherwise only exercised on a machine with R.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        self.stub = self.bin / 'Rscript'
        self._path = os.environ['PATH']
        os.environ['PATH'] = f'{self.bin}:{self._path}'

    def tearDown(self):
        os.environ['PATH'] = self._path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_stub(self, body):
        self.stub.write_text('#!/usr/bin/env python3\n' + body)
        self.stub.chmod(0o755)

    STUB = '''
import sys
a = sys.argv[1:]
if a[0] == '-e':
    print('1.44.0', end='')
    sys.exit(0)
out = a[6]
with open(out, 'w') as fh:
    fh.write('feature\\tbase_mean\\tlog2_fold_change\\tlfc_se\\tstat\\tp_value\\tq_value\\tshrinkage\\n')
    fh.write('CD14\\t900\\t1.4\\t0.3\\t4.6\\t1e-6\\t3e-5\\tapeglm\\n')
    fh.write('NANOG\\t300\\t-1.6\\t0.4\\t-4.0\\t2e-5\\t2e-4\\tapeglm\\n')
    fh.write('RARE\\t0.2\\t0.1\\t4.0\\t0.02\\t0.9\\tNA\\tapeglm\\n')
print('deseq2_version=1.44.0')
'''

    def test_run_produces_a_result_carrying_its_execution_record(self):
        self.write_stub(self.STUB)
        self.assertTrue(EXT.available('deseq2'))
        r = D.run(table(), MANIFEST, PLAN, work_dir=self.tmp / 'work')
        self.assertEqual(['CD14', 'NANOG'], [x['readout'] for x in r['statistics']])
        rec = r['external_execution']
        self.assertEqual(0, rec['exit_status'])
        self.assertFalse(rec['mock'])
        self.assertEqual('1.44.0', rec['tool_version'])
        self.assertIn('deseq2.R', rec['command'])
        # the model that produced the numbers is pinned, not just named
        self.assertEqual(K.sha256_file(D.SCRIPT), rec['parameters']['script_sha256'])
        self.assertEqual(2, len(rec['input_checksums']))
        self.assertEqual('~ condition', rec['parameters']['design'])
        qc = {c['check']: c['detail'] for c in r['quality_control']['checks']}
        self.assertIn('1 feature(s)', qc['independent filtering'])
        self.assertEqual(1, r['comparison']['features_filtered'])
        self.assertIn('DESeq2 1.44.0', r['software'])

    def test_a_failing_r_run_is_refused_with_its_stderr(self):
        self.write_stub('''
import sys
if sys.argv[1] == '-e':
    print('1.44.0', end=''); sys.exit(0)
sys.stderr.write('Error in DESeq(dds): every gene contains at least one zero\\n')
sys.exit(1)
''')
        with self.assertRaises(K.ContractError) as e:
            D.run(table(), MANIFEST, PLAN, work_dir=self.tmp / 'work')
        self.assertIn('exited 1', str(e.exception))
        self.assertIn('at least one zero', str(e.exception))
