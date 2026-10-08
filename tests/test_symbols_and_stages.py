"""Two failures a live run showed, fixed and pinned.

1. NCBI's counts are keyed by GeneID and get symbols from NCBI's annotation
   table. A header the parser did not expect made it return nothing, silently,
   so every gene kept its number and every symbol request failed. The parser
   now reads by meaning, the counts survive a missing table, and how many ids
   got a symbol is said loudly.
2. A finished run showed "Testing it in the simulator" and "Writing the
   report" unticked although both had happened: the commands a discovery run
   uses were not recognised. Stages are now ticked by command and by artifact,
   and a step a finished run truly skipped says so.

Fixtures are SYNTHETIC (see test_geo_fetch).
"""
import gzip
import json
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from biosense import contracts as K
from biosense.data.sources import geo_fetch as GF
from biosense.production import stages as ST
from tests.test_geo_fetch import ACC, ANNOT, SERVED, Sandbox, fake_get


class AnnotationTests(unittest.TestCase):
    def test_the_symbol_table_is_read_by_meaning_not_exact_header(self):
        variants = [ANNOT, '﻿' + ANNOT, '# NCBI annotation\n' + ANNOT,
                    ANNOT.replace('GeneID\tSymbol', 'geneid\tsymbol'),
                    ANNOT.replace('GeneID\tSymbol', '"GeneID"\t"Symbol"')]
        for text in variants:
            self.assertEqual('GATA6', GF.parse_annotation(text).get('2627'), text[:30])
        self.assertEqual({}, GF.parse_annotation('A\tB\n1\t2\n'), 'no id or symbol column')

    def test_a_doubly_gzipped_file_is_unwrapped(self):
        data = gzip.compress(gzip.compress(ANNOT.encode()))
        self.assertTrue(GF._text(data).startswith('GeneID'))


class SymbolCoverageTests(Sandbox):
    def fetch(self, served, *genes):
        with mock.patch.object(GF, '_get', fake_get(served)):
            return GF.fetch(ACC, condition_key='treatment', control='vehicle',
                            treatments=['ATRA 100 nM'], genes=list(genes) or None,
                            i_have_network_permission=True)

    def test_coverage_is_recorded_when_the_table_reads(self):
        d = self.fetch(SERVED, 'GATA6')
        self.assertEqual('ncbi_processed', d['route'])
        self.assertIn('3 of 4 NCBI GeneIDs given a symbol', d['how_read']['symbols'])
        self.assertNotIn('symbols_warning', d['how_read'])

    def test_counts_survive_a_missing_symbol_table_and_say_so(self):
        served = dict(SERVED)
        served['Human.GRCh38.p13.annot.tsv.gz'] = urllib.error.HTTPError('u', 404, 'nf', {}, None)
        d = self.fetch(served, '2627')
        self.assertEqual('ncbi_processed', d['route'], 'the counts are kept')
        self.assertIn('could not be read', d['how_read']['symbols'])
        self.assertIn('mostly NCBI GeneIDs', d['how_read']['symbols_warning'])
        self.assertEqual(1, d['genes_in_table'], 'a gene can be asked for by its id')
        from biosense.data import registry as REG
        self.assertTrue(any('NCBI GeneIDs' in x
                            for x in REG.require(d['dataset_id'])['limitations']))

    def test_a_symbol_that_cannot_match_explains_why(self):
        served = dict(SERVED)
        served['Human.GRCh38.p13.annot.tsv.gz'] = 'unexpected\tcolumns\n1\t2\n'
        with self.assertRaisesRegex(K.ContractError, 'rows are its own ids'):
            self.fetch(served, 'MAFB', 'MAF')


class DepositorTableTests(unittest.TestCase):
    """GSE327431 in a live run: dotted headers naming the donor where GEO titles name
    the group, a donor confounded with the group, and a sample count of 50000."""

    def samples(self):
        rows, i = [], 0
        for grp, donor in (('Control', 'PEN8E'), ('LCA1', 'PEN1B')):
            for tr in ('DMSO', 'Halo'):
                for oid in ('PA.2b', 'PA.3a', 'PB.1c'):
                    i += 1
                    rows.append({'gsm': f'GSM{i}', 'title': f'{grp}.{tr}.{oid}',
                                 'source': 'retinal organoid',
                                 'characteristics': {'donor': donor, 'group': f'{grp}.{tr}',
                                                     'treatment': tr}})
        return rows

    def test_dotted_headers_naming_the_donor_map_on_their_own(self):
        rows = self.samples()
        donor = {'Control': 'PEN8E', 'LCA1': 'PEN1B'}
        headers = [donor[r['title'].split('.')[0]] + r['title'][r['title'].index('.'):]
                   for r in rows]
        got = GF.suggest_column_map(headers, rows)
        self.assertEqual({}, got['unresolved'])
        self.assertEqual([r['gsm'] for r in rows],
                         [got['suggested'][h]['gsm'] for h in headers])
        self.assertIn('0.5', [t for t, _ in GF._units('ATRA.0.5.uM')], 'a decimal stays whole')

    def test_a_donor_that_splits_along_the_contrast_is_flagged(self):
        rows = self.samples()
        gsms = [r['gsm'] for r in rows]
        matrix = [[10.0 + j for j in range(len(gsms))] for _ in range(3)]
        _, stats = GF.build_long_table(rows, ['A', 'B', 'C'], gsms, matrix, {},
                                       condition_key='group', control='Control.DMSO',
                                       treatments=['LCA1.DMSO'], keep={'treatment': 'DMSO'})
        self.assertEqual(['donor'], [c['field'] for c in stats['confounded']])
        self.assertIn('perfectly confounded', stats['confounded'][0]['statement'])

    def test_a_long_table_counts_its_samples_not_its_rows(self):
        from biosense.data import tables as T
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        f = d / 't.tsv'
        f.write_text('gene\tsample_id\tcondition\tlogcpm\n' + ''.join(
            f'G{g}\tGSM{s}\t{"a" if s < 3 else "b"}\t1.0\n' for g in range(50) for s in range(6)))
        meta = T.describe(T.read_table(f))
        self.assertEqual(6, meta['sample_count'])
        self.assertEqual(300, meta['row_count'])


class StageTests(unittest.TestCase):
    def shell(self, command):
        return ST.stage_for_tool('sys_os_shell', {'command': command})

    def test_the_commands_a_discovery_run_uses_are_recognised(self):
        self.assertEqual('testing_simulator', self.shell(
            '.venv/bin/python -m biosense.evidence.cli simulate --project p --set x=1 '
            '--out runs/ai-1/simulation.json'))
        self.assertEqual('testing_simulator', self.shell(
            '.venv/bin/python -m biosense.evidence.cli term --project p --draft d --out o'))
        self.assertEqual('generating_report', self.shell(
            '.venv/bin/python -m biosense.evidence.cli round-plan --project p --draft d'))
        self.assertEqual('generating_report', self.shell('cat > runs/ai-1/RUN_SUMMARY.md'))
        self.assertEqual('building_hypothesis', self.shell(
            '.venv/bin/python -m biosense.evidence.cli hypothesis --project p --draft H01.json'))

    def test_a_run_ticks_stages_from_the_files_it_left(self):
        from biosense.production import app as APP
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        run = APP.DiscoveryRun('a' * 16, {'request_id': 'r', 'project_id': 'p',
                                          'objective': 'o'}, d, 'local_real_ai')
        (d / 'simulation.json').write_text(json.dumps({'genotype_simulation': None}))
        (d / 'RUN_SUMMARY.md').write_text('summary')
        run.scan_artifacts(force=True)
        self.assertTrue({'testing_simulator', 'generating_report'} <= run.stages_reached)

    def test_a_finished_run_marks_what_it_never_reached(self):
        rows = ST.progress({'building_hypothesis'}, terminal='complete')
        status = {r['stage']: r['status'] for r in rows}
        self.assertEqual('done', status['building_hypothesis'])
        self.assertEqual('skipped', status['testing_simulator'])


if __name__ == '__main__':
    unittest.main()
