"""A real public series, fetched, normalised and analysed — without the network.

A run's bioinformatics section read "9/9 genes not found; live lookups not
permitted; only synthetic fixtures reachable; no analysis planned" every time,
because nothing could bring a real dataset in. These tests replay recorded
response shapes from NCBI (series matrix, NCBI-generated counts, the gene
annotation table) through the fetch, so the parsing, the normalisation and
every refusal are checked here; whether NCBI is up is checked by a live run.
"""
import contextlib
import gzip
import io
import json
import math
import os
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from biosense import contracts as K
from biosense.bioinformatics import cli as BCLI
from biosense.bioinformatics import tools as BT
from biosense.data import roots as DR
from biosense.data.sources import geo_fetch as GF
from biosense.production import discovery as DISC

ACC = 'GSE123456'
GSMS = ['GSM1', 'GSM2', 'GSM3', 'GSM4', 'GSM5', 'GSM6']
MATRIX = '\n'.join([
    '!Series_title\t"Retinoic acid and peritoneal macrophage identity"',
    '!Sample_title\t"veh_1"\t"veh_2"\t"veh_3"\t"ra_1"\t"ra_2"\t"ra_3"',
    '!Sample_geo_accession\t' + '\t'.join(f'"{g}"' for g in GSMS),
    '!Sample_source_name_ch1\t' + '\t'.join(['"iPSC-macrophage"'] * 6),
    '!Sample_organism_ch1\t' + '\t'.join(['"Homo sapiens"'] * 6),
    '!Sample_characteristics_ch1\t' + '\t'.join(['"cell type: macrophage"'] * 6),
    '!Sample_characteristics_ch1\t"treatment: vehicle"\t"treatment: vehicle"\t'
    '"treatment: vehicle"\t"treatment: ATRA 100 nM"\t"treatment: ATRA 100 nM"\t'
    '"treatment: ATRA 100 nM"',
    '!Sample_library_strategy\t' + '\t'.join(['"RNA-Seq"'] * 6),
    '!series_matrix_table_begin',
    '"ID_REF"\t' + '\t'.join(f'"{g}"' for g in GSMS),
    '!series_matrix_table_end',
])
# GeneID x GSM. GATA6 (2627) rises with ATRA; ACTB (60) flat; one gene absent from
# the annotation keeps its GeneID.
COUNTS = '\n'.join([
    'GeneID\t' + '\t'.join(GSMS),
    '2627\t100\t110\t90\t400\t420\t380',
    '60\t5000\t5100\t4900\t5000\t5050\t4950',
    '999999\t10\t12\t9\t11\t10\t10',
    '7037\t900\t880\t910\t300\t320\t280',
])
ANNOT = 'GeneID\tSymbol\tDescription\n2627\tGATA6\tGATA binding protein 6\n60\tACTB\tactin beta\n' \
        '7037\tTFRC\ttransferrin receptor\n'


def fake_get(responses):
    def get(url, timeout=60, max_bytes=GF.MAX_DOWNLOAD):
        for key, body in responses.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return gzip.compress(body.encode())
        raise urllib.error.HTTPError(url, 404, 'not found', {}, None)
    return get


SERVED = {'_series_matrix.txt.gz': MATRIX, '_raw_counts_GRCh38.p13_NCBI.tsv.gz': COUNTS,
          'Human.GRCh38.p13.annot.tsv.gz': ANNOT}


class ParsingTests(unittest.TestCase):
    def test_the_series_matrix_gives_samples_and_groupable_fields(self):
        rows, meta = GF.parse_series_matrix(MATRIX)
        self.assertEqual(GSMS, [r['gsm'] for r in rows])
        self.assertEqual('ATRA 100 nM', rows[3]['characteristics']['treatment'])
        self.assertIn('Retinoic acid', meta['title'])

    def test_the_url_follows_geo_s_directory_layout(self):
        self.assertEqual('https://ftp.ncbi.nlm.nih.gov/geo/series/GSE123nnn/GSE123456',
                         GF.series_url('GSE123456'))
        self.assertTrue(GF.series_url('GSE1234').endswith('/GSE1nnn/GSE1234'))
        with self.assertRaises(K.ContractError):
            GF.check_accession('GSE12; rm -rf')

    def test_log2_cpm_uses_every_gene_for_library_size(self):
        rows, _ = GF.parse_series_matrix(MATRIX)
        genes, gsms, m = GF.parse_counts(COUNTS)
        table, stats = GF.build_long_table(
            rows, genes, gsms, m, GF.parse_annotation(ANNOT), condition_key='treatment',
            control='vehicle', treatments=['ATRA 100 nM'], genes=['gata6', 'NOPE'])
        self.assertEqual({'vehicle': 3, 'ATRA 100 nM': 3}, stats['samples_per_group'])
        self.assertEqual(['NOPE'], stats['genes_missing'])
        first = table[0]
        lib = 100 + 5000 + 10 + 900
        self.assertEqual(('GATA6', 'GSM1', 'vehicle'),
                         (first['gene'], first['sample_id'], first['condition']))
        self.assertAlmostEqual(math.log2(100 / lib * 1e6 + 1), float(first['logcpm']), 4)

    def test_what_the_samples_cannot_support_is_refused(self):
        rows, _ = GF.parse_series_matrix(MATRIX)
        genes, gsms, m = GF.parse_counts(COUNTS)
        kw = dict(condition_key='treatment', control='vehicle', treatments=['ATRA 100 nM'])
        with self.assertRaisesRegex(K.ContractError, 'Values of'):
            GF.build_long_table(rows, genes, gsms, m, {}, **dict(kw, treatments=['ATRA']))
        with self.assertRaisesRegex(K.ContractError, 'no sample has a field'):
            GF.build_long_table(rows, genes, gsms, m, {}, **dict(kw, condition_key='dose'))
        with self.assertRaisesRegex(K.ContractError, 'at least 2'):
            GF.build_long_table(rows, genes, gsms, m, {}, keep={'cell type': 'monocyte'}, **kw)


class Sandbox(unittest.TestCase):
    """A private cache and registry per test, and the CLI's JSON output."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {DR.CACHE_ENV: str(self.tmp / 'cache'),
                                           DR.PRIVATE_ENV: str(self.tmp / 'private')})
        env.start(); self.addCleanup(env.stop)

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = BCLI.main(list(argv))
        return code, json.loads(out.getvalue())


class FetchTests(Sandbox):
    def test_without_permission_nothing_is_fetched(self):
        with self.assertRaisesRegex(K.ContractError, 'network'):
            GF.samples(ACC)
        code, d = self.cli('gene-info', '--genes', 'GATA6')
        self.assertEqual(1, code)
        self.assertIn('network', d['refused'])

    def test_a_series_becomes_a_public_dataset_and_is_analysed(self):
        with mock.patch.object(GF, '_get', fake_get(SERVED)):
            code, s = self.cli('datasets', 'geo-samples', '--accession', ACC,
                               '--i-have-network-permission')
            self.assertEqual(0, code)
            self.assertEqual({'vehicle': 3, 'ATRA 100 nM': 3}, s['groupable_fields']['treatment'])
            self.assertNotIn('title', s['groupable_fields'], 'every title differs: groups nothing')
            code, d = self.cli('datasets', 'fetch-geo', '--accession', ACC, '--condition-key',
                               'treatment', '--control', 'vehicle', '--treatment', 'ATRA 100 nM',
                               '--genes', 'GATA6', 'ACTB', 'TFRC', '--i-have-network-permission')
        self.assertEqual(0, code, d)
        self.assertTrue(d['registered'])
        m = json.loads(Path(DR.cache_root(), d['dataset_id'], 'manifest.json').read_text()) \
            if Path(DR.cache_root(), d['dataset_id'], 'manifest.json').exists() else None
        from biosense.data import registry as REG
        m = REG.require(d['dataset_id'])
        self.assertEqual(('public', 'geo', ACC), (m['visibility'], m['source'], m['accession']))
        self.assertEqual('public_dataset', m['evidence_class'])
        code, plan = self.cli(
            'analyse', 'plan', '--plan-id', 'plan-01', '--question', 'Does ATRA raise GATA6?',
            '--evidence-gap', 'U1', '--uncertainty', 'whether RA signalling induces GATA6',
            '--why', 'it decides whether a retinoid belongs in maturation',
            '--dataset-ids', d['dataset_id'], '--analysis-type', 'bulk_expression_comparison',
            '--tool', 'bulk.expression_comparison', '--decision-relevance', 'retinoid dose',
            '--group-column', 'condition', '--control', 'vehicle',
            '--treatment-level', 'ATRA 100 nM', '--out', str(self.tmp / 'plan.json'))
        self.assertEqual(0, code, plan)
        code, res = self.cli('analyse', 'run', '--plan', str(self.tmp / 'plan.json'),
                             '--out', str(self.tmp / 'result.json'))
        self.assertEqual(0, code, res)
        self.assertEqual('public_dataset', res['source_evidence_class'])
        gata6 = next(r for r in res['statistics'] if r.get('feature') == 'GATA6'
                     or r.get('name') == 'GATA6' or 'GATA6' in json.dumps(r)[:80])
        self.assertIn('GATA6', json.dumps(gata6))

    def test_a_series_without_processed_counts_says_where_to_look(self):
        served = dict(SERVED)
        served.pop('_raw_counts_GRCh38.p13_NCBI.tsv.gz')
        with mock.patch.object(GF, '_get', fake_get(served)):
            with self.assertRaisesRegex(K.ContractError, 'no processed counts.*supplementary'):
                GF.fetch(ACC, condition_key='treatment', control='vehicle',
                         treatments=['ATRA 100 nM'], i_have_network_permission=True)


ZEBRAFISH = MATRIX.replace('Homo sapiens', 'Danio rerio')
LISTING = ('<a href="GSE123456_RAW.tar">GSE123456_RAW.tar</a> '
           '<a href="GSE123456_filelist.txt">filelist</a> '
           '<a href="GSE123456_normalized_tpm.txt.gz">GSE123456_normalized_tpm.txt.gz</a>')
# Headers are the sample titles, not GSM ids; a description column sits between.
TPM = '\n'.join([
    'gene_name\tdescription\tveh_1\tveh_2\tveh_3\tra_1\tra_2\tra_3',
    'gata6\tGATA binding\t10.5\t11.0\t9.5\t40.1\t42.3\t38.0',
    'actb\tactin\t900\t910\t890\t905\t899\t901',
    'tfrc\ttransferrin\t60\t61\t59\t20\t22\t19',
])


class AnySpeciesTests(FetchTests):
    """Other species, and human series NCBI has not processed: the depositors' table."""

    def test_a_zebrafish_series_is_read_from_its_own_table_at_low_confidence(self):
        served = {'_series_matrix.txt.gz': ZEBRAFISH, '_normalized_tpm.txt.gz': TPM,
                  '/suppl/': LISTING}
        with mock.patch.object(GF, '_get', fake_get(served)):
            d = GF.fetch(ACC, condition_key='treatment', control='vehicle',
                         treatments=['ATRA 100 nM'], i_have_network_permission=True)
        self.assertEqual(('depositor_supplementary', 'Danio rerio'), (d['route'], d['organism']))
        self.assertEqual('low', d['evidence_weight']['confidence_ceiling'])
        self.assertEqual('linear', d['how_read']['transform'])
        self.assertEqual('gene_name', d['how_read']['feature_column'])
        self.assertIn('description', d['how_read']['unmatched_columns'])
        self.assertIn('NCBI processes human and mouse', d['why_not_ncbi'])
        from biosense.data import registry as REG
        self.assertTrue(any('not a mammal' in x for x in REG.require(d['dataset_id'])['limitations']))

    def test_a_human_series_without_ncbi_counts_falls_back_to_its_table(self):
        served = {'_series_matrix.txt.gz': MATRIX, '_normalized_tpm.txt.gz': TPM,
                  '/suppl/': LISTING}
        with mock.patch.object(GF, '_get', fake_get(served)):
            d = GF.fetch(ACC, condition_key='treatment', control='vehicle',
                         treatments=['ATRA 100 nM'], genes=['GATA6'],
                         i_have_network_permission=True)
        self.assertEqual(('depositor_supplementary', 'moderate'),
                         (d['route'], d['evidence_weight']['confidence_ceiling']))
        self.assertEqual(1, d['genes_in_table'])

    def test_unmatched_headers_are_listed_and_a_column_map_resolves_them(self):
        rows, _ = GF.parse_series_matrix(MATRIX)
        odd = TPM.replace('veh_1', 'S1').replace('veh_2', 'S2').replace('veh_3', 'S3') \
            .replace('ra_1', 'S4').replace('ra_2', 'S5').replace('ra_3', 'S6')
        with self.assertRaisesRegex(K.ContractError, 'Columns: gene_name, description, S1'):
            GF.parse_supplementary_table(odd, rows)
        cmap = {f'S{i + 1}': g for i, g in enumerate(GSMS)}
        genes, gsms, m, how = GF.parse_supplementary_table(odd, rows, column_map=cmap)
        self.assertEqual(GSMS, gsms)

    def test_species_weights(self):
        self.assertEqual('high', GF.species_weight('Homo sapiens', 'ncbi_processed')['confidence_ceiling'])
        self.assertEqual('moderate', GF.species_weight('Mus musculus', 'ncbi_processed')['confidence_ceiling'])
        self.assertEqual('low', GF.species_weight('Sus scrofa', 'depositor_supplementary')['confidence_ceiling'])


# GSE309039's shape: a semicolon table, unnamed first column, composite feature
# ids, headers in the depositors' shorthand (MoCul = monocyte culture, CoCul =
# co-culture), in an order unlike the GSM order.
MOCUL_ACC = 'GSE309039'
MOCUL_CONDS = [('Monocyte culture, untreated', 'none'),
               ('Monocyte culture, GM-CSF', 'GM-CSF 50 ng/ml'),
               ('Monocyte co-culture with LN229', 'co-culture with LN229')]
MOCUL_GSMS = [f'GSM{101 + i}' for i in range(9)]
MOCUL_TITLES = [f'{t}, replicate {r}' for t, _ in MOCUL_CONDS for r in (1, 2, 3)]
MOCUL_TREAT = [tr for _, tr in MOCUL_CONDS for _ in (1, 2, 3)]
MOCUL_MATRIX = '\n'.join([
    '!Series_title\t"Monocytes conditioned by glioblastoma cells"',
    '!Series_overall_design\t"MoCul: monocyte culture; CoCul: co-culture with LN229"',
    '!Series_pubmed_id\t"41000001"',
    '!Series_pubmed_id\t"41000002"',
    '!Sample_title\t' + '\t'.join(f'"{t}"' for t in MOCUL_TITLES),
    '!Sample_geo_accession\t' + '\t'.join(f'"{g}"' for g in MOCUL_GSMS),
    '!Sample_source_name_ch1\t' + '\t'.join(['"CD14+ monocytes"'] * 9),
    '!Sample_organism_ch1\t' + '\t'.join(['"Homo sapiens"'] * 9),
    '!Sample_characteristics_ch1\t' + '\t'.join(['"cell type: monocyte"'] * 9),
    '!Sample_characteristics_ch1\t' + '\t'.join(f'"treatment: {t}"' for t in MOCUL_TREAT),
    '!series_matrix_table_begin',
    '!series_matrix_table_end',
])
MOCUL_HEADERS = ['CoCul_LN229_n1', 'MoCul_n1', 'MoCul_GMCSF_n1', 'CoCul_LN229_n2', 'MoCul_n2',
                 'MoCul_GMCSF_n2', 'CoCul_LN229_n3', 'MoCul_n3', 'MoCul_GMCSF_n3']
MOCUL_TRUTH = {'MoCul_n1': 'GSM101', 'MoCul_n2': 'GSM102', 'MoCul_n3': 'GSM103',
               'MoCul_GMCSF_n1': 'GSM104', 'MoCul_GMCSF_n2': 'GSM105', 'MoCul_GMCSF_n3': 'GSM106',
               'CoCul_LN229_n1': 'GSM107', 'CoCul_LN229_n2': 'GSM108', 'CoCul_LN229_n3': 'GSM109'}
# PPARG: column index * 10 + 50, so each column is recognisable after mapping.
SEMI = '\n'.join([
    ';' + ';'.join(MOCUL_HEADERS),
    '"ENSG00000132170.21|PPARG|protein_coding";' + ';'.join(str(50 + 10 * i) for i in range(9)),
    'ENSG00000025434.19|NR1H3|protein_coding;' + ';'.join(['300'] * 9),
    'ENSG00000075624.17|ACTB|protein_coding;' + ';'.join(['20000'] * 9),
    'ENSG00000125730.16|C3|protein_coding;' + ';'.join(['7'] * 9),
])
MOCUL_LISTING = ('<a href="GSE309039_RAW.tar">GSE309039_RAW.tar</a> '
                 '<a href="GSE309039_data2_raw_counts.csv.gz">counts</a>')
MOCUL_SERVED = {'_series_matrix.txt.gz': MOCUL_MATRIX, '_data2_raw_counts.csv.gz': SEMI,
                '/suppl/': MOCUL_LISTING}
MOCUL_KW = dict(condition_key='treatment', control='none', treatments=['GM-CSF 50 ng/ml'])


def mocul_rows():
    return GF.parse_series_matrix(MOCUL_MATRIX)[0]


class TableShapeTests(unittest.TestCase):
    """Delimiters, decimal commas, R row names and composite feature ids. Pure."""

    def test_the_delimiter_is_the_one_that_splits_consistently(self):
        self.assertEqual(';', GF.sniff_delimiter(SEMI.splitlines()))
        self.assertEqual('\t', GF.sniff_delimiter(TPM.splitlines()))
        self.assertEqual(',', GF.sniff_delimiter(['gene,a,b', 'X,1,2', 'Y,3,4']))
        # Decimal commas in a semicolon table do not make it a comma table.
        self.assertEqual(';', GF.sniff_delimiter(['gene;a;b', 'X;1,5;2,5', 'Y;3;4,25']))

    def test_a_semicolon_table_with_decimal_commas(self):
        rows, _ = GF.parse_series_matrix(MATRIX)
        text = '\n'.join(['gene_name;veh_1;veh_2;veh_3;ra_1;ra_2;ra_3',
                          'GATA6;1,5;1,25;2;4,5;5;4',
                          'ACTB;9,75;10;10,5;10;9,5;10'])
        genes, gsms, m, how = GF.parse_supplementary_table(text, rows)
        self.assertEqual(['GATA6', 'ACTB'], genes)
        self.assertEqual([1.5, 1.25, 2.0, 4.5, 5.0, 4.0], m[0])
        self.assertEqual(('semicolon', True), (how['delimiter'], how['decimal_comma']))

    def test_an_r_header_one_field_short_reads_row_names_as_features(self):
        rows, _ = GF.parse_series_matrix(MATRIX)
        text = '\n'.join(['veh_1\tveh_2\tveh_3\tra_1\tra_2\tra_3',
                          'GATA6\t10\t11\t9\t40\t42\t38',
                          'ACTB\t900\t910\t890\t905\t899\t901'])
        genes, gsms, m, how = GF.parse_supplementary_table(text, rows)
        self.assertEqual((['GATA6', 'ACTB'], GSMS), (genes, gsms))
        self.assertEqual([10.0, 11.0, 9.0, 40.0, 42.0, 38.0], m[0])
        self.assertEqual('(row names)', how['feature_column'])
        self.assertIn('row names', how['row_names'])

    def test_feature_ids_are_reduced_to_symbols_without_guessing(self):
        fs = GF.feature_symbol
        self.assertEqual('C3', fs('ENSG00000125730.16|C3|protein_coding'))
        self.assertEqual('ENSG00000125730', fs('ENSG00000125730.16'))
        self.assertEqual('gata6', fs('ENSDARG00000017821|gata6|protein_coding'), 'zebrafish case')
        self.assertEqual('Pparg', fs('Pparg|ENSMUSG00000000440.13|lncRNA'))
        self.assertEqual('MALAT1', fs('ENSG00000251562|MALAT1|lncRNA'))
        self.assertEqual('FOO', fs('12345|FOO'))
        self.assertEqual('GATA6', fs('GATA6'))
        self.assertEqual('ENSG00000000001', fs('ENSG00000000001.4|processed_pseudogene'))
        m, how = GF.supplementary_symbols(['ENSG1000001.2|C3|protein_coding',
                                           'ENSG2000002.1|C3|protein_coding', 'ACTB'])
        self.assertEqual({'ENSG1000001.2|C3|protein_coding': 'C3',
                          'ENSG2000002.1|C3|protein_coding': 'ENSG2000002'}, m,
                         'a second row with the same symbol keeps its own id')
        self.assertIn('composite', how)
        self.assertIn('share a symbol', how)

    def test_genes_are_found_by_symbol_inside_composite_ids(self):
        rows = mocul_rows()
        cmap = {h: g for h, g in MOCUL_TRUTH.items()}
        genes, gsms, m, how = GF.parse_supplementary_table(SEMI, rows, column_map=cmap)
        self.assertEqual('(unnamed first column)', how['feature_column'])
        with self.assertRaisesRegex(K.ContractError, 'none of PPARG'):
            GF.build_long_table(rows, genes, gsms, m, {}, genes=['PPARG'], **MOCUL_KW)
        symbols, _ = GF.supplementary_symbols(genes)
        table, stats = GF.build_long_table(rows, genes, gsms, m, symbols,
                                           genes=['PPARG', 'NR1H3', 'NOPE'], **MOCUL_KW)
        self.assertEqual({'PPARG', 'NR1H3'}, {r['gene'] for r in table})
        self.assertEqual(['NOPE'], stats['genes_missing'])


class ColumnSuggestionTests(unittest.TestCase):
    """Headers in the depositors' shorthand: suggested conservatively, never guessed."""

    def test_the_mocul_headers_are_suggested_correctly(self):
        sug = GF.suggest_column_map(MOCUL_HEADERS, mocul_rows())
        self.assertEqual({}, sug['unresolved'])
        self.assertEqual(MOCUL_TRUTH, {h: v['gsm'] for h, v in sug['suggested'].items()})
        basis = sug['suggested']['MoCul_n1']['basis']
        self.assertIn('replicate 1 agrees', basis)
        self.assertIn('gmcsf', basis, 'says why the untreated sample beat the GM-CSF one')
        self.assertEqual(1.0, sug['suggested']['MoCul_GMCSF_n2']['score'])

    def test_replicate_numbers_are_split_only_where_marked(self):
        sr = GF.split_replicate
        self.assertEqual(('MoCul', 1), sr('MoCul_n1'))
        self.assertEqual(('CoCul_LN229', None), sr('CoCul_LN229'), 'LN229 is a cell line')
        self.assertEqual(('x', 2), sr('x_rep2'))
        self.assertEqual(('x', 3), sr('x-r3'))
        self.assertEqual(('Monocyte culture, untreated', 2),
                         sr('Monocyte culture, untreated, replicate 2'))
        self.assertEqual(('veh', 1), sr('veh_1'))
        self.assertEqual(('IL-4', None), sr('IL-4'))
        self.assertEqual(('Mono day 3', None), sr('Mono day 3'))

    def test_ambiguous_headers_stay_unresolved_with_their_candidates(self):
        rows = mocul_rows() + [
            {'gsm': f'GSM2{r}', 'title': f'Monocyte co-culture with U87, replicate {r}',
             'source': 'CD14+ monocytes',
             'characteristics': {'cell type': 'monocyte', 'treatment': 'co-culture with U87'}}
            for r in (1, 2, 3)]
        sug = GF.suggest_column_map(['CoCul_n1', 'Mono_IL4_n1', 'S7', 'MoCul_n1'], rows)
        self.assertEqual({'MoCul_n1'}, set(sug['suggested']))
        self.assertEqual(['GSM107', 'GSM21'], sug['unresolved']['CoCul_n1']['candidates'])
        self.assertIn('different conditions', sug['unresolved']['CoCul_n1']['reason'])
        self.assertIn('il4', sug['unresolved']['Mono_IL4_n1']['reason'])
        self.assertEqual([], sug['unresolved']['S7']['candidates'])

    def test_two_headers_for_one_sample_are_both_left_open(self):
        sug = GF.suggest_column_map(['MoCul_GMCSF_n1', 'Mo_GMCSF_n1'], mocul_rows())
        self.assertEqual({}, sug['suggested'])
        self.assertIn('competes', sug['unresolved']['Mo_GMCSF_n1']['reason'])

    def test_replicates_of_one_condition_are_assigned_in_order(self):
        def s(gsm, title, treat, donor):
            return {'gsm': gsm, 'title': title, 'source': 'monocytes',
                    'characteristics': {'treatment': treat, 'donor': donor}}
        rows = [s('GSM1', 'Monocyte culture, untreated', 'none', 'D1'),
                s('GSM2', 'Monocyte culture, untreated', 'none', 'D2'),
                s('GSM3', 'Monocyte culture, GM-CSF', 'GM-CSF', 'D1'),
                s('GSM4', 'Monocyte culture, GM-CSF', 'GM-CSF', 'D2')]
        sug = GF.suggest_column_map(['MoCul_n2', 'MoCul_n1', 'MoCul_GMCSF_n1',
                                     'MoCul_GMCSF_n2'], rows)
        self.assertEqual({'MoCul_n1': 'GSM1', 'MoCul_n2': 'GSM2', 'MoCul_GMCSF_n1': 'GSM3',
                          'MoCul_GMCSF_n2': 'GSM4'},
                         {h: v['gsm'] for h, v in sug['suggested'].items()})
        self.assertIn('replicate order within an identical condition',
                      sug['suggested']['MoCul_n1']['basis'])
        # Different conditions are never put in order.
        rows[1]['characteristics']['treatment'] = 'none, 2 h'
        sug = GF.suggest_column_map(['MoCul_n1', 'MoCul_n2'], rows)
        self.assertEqual({}, sug['suggested'])

    def test_the_series_papers_are_read_from_the_matrix(self):
        _, meta = GF.parse_series_matrix(MOCUL_MATRIX)
        self.assertEqual(['41000001', '41000002'], meta['pubmed_ids'])
        with mock.patch.object(GF, '_get', fake_get(MOCUL_SERVED)):
            d = GF.samples(MOCUL_ACC, i_have_network_permission=True)
        self.assertEqual(['41000001', '41000002'], d['pubmed_ids'])
        self.assertIn('pubmed_ids', d['note'])
        self.assertIn('overall_design', d['note'])


class SemicolonSeriesTests(Sandbox):
    """GSE309039 end to end through fetch, with the network replayed."""

    def test_the_refusal_offers_a_ready_to_paste_column_map(self):
        with mock.patch.object(GF, '_get', fake_get(MOCUL_SERVED)):
            with self.assertRaises(K.ContractError) as cm:
                GF.fetch(MOCUL_ACC, i_have_network_permission=True, **MOCUL_KW)
        msg = str(cm.exception)
        self.assertIn('--column-map ', msg)
        for h, g in MOCUL_TRUTH.items():
            self.assertIn(f'{h}={g}', msg)
        self.assertIn('overall design', msg)
        self.assertIn('Columns: , CoCul_LN229_n1, MoCul_n1', msg, 'split on semicolons')
        # The agent checks it, then pastes it with its reason.
        pasted = msg.split('--column-map ', 1)[1].split('.', 1)[0].split()
        cmap = dict(x.rsplit('=', 1) for x in pasted)
        with mock.patch.object(GF, '_get', fake_get(MOCUL_SERVED)):
            d = GF.fetch(MOCUL_ACC, i_have_network_permission=True, column_map=cmap,
                         column_map_basis='overall design: MoCul = monocyte culture',
                         genes=['PPARG'], **MOCUL_KW)
        self.assertEqual(MOCUL_TRUTH, d['how_read']['mapped_columns'])
        from biosense.data import registry as REG
        lims = REG.require(d['dataset_id'])['limitations']
        self.assertTrue(any('column map the caller supplied' in x and 'MoCul = monocyte' in x
                            for x in lims))

    def test_infer_columns_reads_the_table_and_records_how(self):
        with mock.patch.object(GF, '_get', fake_get(MOCUL_SERVED)):
            code, d = self.cli('datasets', 'fetch-geo', '--accession', MOCUL_ACC,
                               '--condition-key', 'treatment', '--control', 'none',
                               '--treatment', 'GM-CSF 50 ng/ml', '--genes', 'PPARG', 'NR1H3',
                               '--infer-columns', '--column-map-basis',
                               'series overall design names MoCul and CoCul',
                               '--i-have-network-permission')
        self.assertEqual(0, code, d)
        how = d['how_read']
        self.assertEqual(('semicolon', 'counts'), (how['delimiter'], how['transform']))
        self.assertEqual(MOCUL_TRUTH, {h: v['gsm'] for h, v in how['inferred_columns'].items()})
        self.assertIn('name similarity', how['inferred_columns']['MoCul_n1']['basis'])
        self.assertEqual('series overall design names MoCul and CoCul', how['column_map_basis'])
        self.assertIn('composite', how['symbols'])
        self.assertEqual(2, d['genes_in_table'])
        self.assertEqual('moderate', d['evidence_weight']['confidence_ceiling'])
        # GSM104 is the MoCul_GMCSF_n1 column (index 2), not the third column read.
        lib = 50 + 10 * 2 + 300 + 20000 + 7
        rows = list(csv_rows(d['table']))
        v = next(r for r in rows if (r['gene'], r['sample_id']) == ('PPARG', 'GSM104'))
        self.assertAlmostEqual(math.log2(70 / lib * 1e6 + 1), float(v['logcpm']), 4)
        from biosense.data import registry as REG
        lims = REG.require(d['dataset_id'])['limitations']
        self.assertTrue(any('inferred by BioSense' in x and 'MoCul_GMCSF_n1 -> GSM104' in x
                            for x in lims))


def csv_rows(path):
    import csv
    with open(path, newline='') as f:
        yield from csv.DictReader(f, delimiter='\t')


# Bare versioned Ensembl ids in a human series NCBI has not processed.
ENS_TABLE = '\n'.join([
    'gene_id\tveh_1\tveh_2\tveh_3\tra_1\tra_2\tra_3',
    'ENSG00000141448.10\t10.5\t11.0\t9.5\t40.1\t42.3\t38.0',
    'ENSG00000075624.17\t900\t910\t890\t905\t899\t901',
    'ENSG00000072274.13\t60\t61\t59\t20\t22\t19',
    'ENSG00000999999.1\t5\t5\t5\t5\t5\t5',
])
ENS_LISTING = '<a href="GSE123456_tpm_matrix.txt.gz">tpm</a>'
ANNOT_ENS = ('GeneID\tSymbol\tDescription\tEnsemblGeneID\n'
             '2627\tGATA6\tGATA binding protein 6\tENSG00000141448\n'
             '60\tACTB\tactin beta\tENSG00000075624\n'
             '7037\tTFRC\ttransferrin receptor\tENSG00000072274\n')


class EnsemblSymbolTests(Sandbox):
    def served(self, annot):
        return {'_series_matrix.txt.gz': MATRIX, '_tpm_matrix.txt.gz': ENS_TABLE,
                '/suppl/': ENS_LISTING, 'Human.GRCh38.p13.annot.tsv.gz': annot}

    def test_bare_ensembl_ids_are_mapped_with_ncbi_s_annotation(self):
        self.assertEqual({'ENSG00000141448': 'GATA6', 'ENSG00000075624': 'ACTB',
                          'ENSG00000072274': 'TFRC'}, GF.parse_annotation_ensembl(ANNOT_ENS))
        with mock.patch.object(GF, '_get', fake_get(self.served(ANNOT_ENS))):
            d = GF.fetch(ACC, condition_key='treatment', control='vehicle',
                         treatments=['ATRA 100 nM'], genes=['GATA6', 'TFRC'],
                         i_have_network_permission=True)
        self.assertEqual(2, d['genes_in_table'])
        self.assertIn('3 of 4 bare Ensembl ids mapped', d['how_read']['symbols'])
        self.assertIn('EnsemblGeneID', d['how_read']['symbols'])
        self.assertIn('annot.tsv.gz', d['sources']['annotation'])

    def test_without_an_ensembl_column_the_ids_are_kept_and_it_is_said(self):
        self.assertIsNone(GF.parse_annotation_ensembl(ANNOT))
        with mock.patch.object(GF, '_get', fake_get(self.served(ANNOT))):
            d = GF.fetch(ACC, condition_key='treatment', control='vehicle',
                         treatments=['ATRA 100 nM'], i_have_network_permission=True)
        self.assertIn('kept as Ensembl ids', d['how_read']['symbols'])
        self.assertIn('no Ensembl column', d['how_read']['symbols'])
        self.assertNotIn('annotation', d['sources'])
        genes = {r['gene'] for r in csv_rows(d['table'])}
        self.assertIn('ENSG00000141448', genes, 'version stripped, not guessed')
        served = self.served(urllib.error.HTTPError('u', 500, 'down', {}, None))
        with mock.patch.object(GF, '_get', fake_get(served)):
            d = GF.fetch(ACC, condition_key='treatment', control='vehicle',
                         treatments=['ATRA 100 nM'], i_have_network_permission=True)
        self.assertIn('could not be read', d['how_read']['symbols'])


class GeneInfoTests(unittest.TestCase):
    def test_public_records_are_read_into_fields_without_an_effect(self):
        def lookup(sym, sources, timeout):
            return {'symbol': sym, 'retrieved_at': 't', 'errors': [], 'results': [
                {'source': 'Ensembl', 'url': 'u1', 'payload': {
                    'id': 'ENSG00000141448', 'description': 'GATA binding protein 6',
                    'biotype': 'protein_coding'}},
                {'source': 'UniProt', 'url': 'u2', 'payload': {'results': [{
                    'primaryAccession': 'Q92908',
                    'proteinDescription': {'recommendedName': {'fullName': {
                        'value': 'Transcription factor GATA-6'}}},
                    'comments': [{'commentType': 'FUNCTION', 'texts': [{'value': 'Transcriptional activator.'}]}],
                    'uniProtKBCrossReferences': [
                        {'database': 'GO', 'properties': [{'key': 'GoTerm', 'value': 'P:cell differentiation'}]},
                        {'database': 'GO', 'properties': [{'key': 'GoTerm', 'value': 'C:nucleus'}]}],
                    'keywords': [{'name': 'Activator'}]}]}},
                {'source': 'STRING', 'url': 'u3', 'payload': [
                    {'preferredName_A': 'GATA6', 'preferredName_B': 'NKX2-5', 'score': 0.9},
                    {'preferredName_A': 'TBX5', 'preferredName_B': 'GATA6', 'score': 0.8}]}]}
        d = BT.gene_info(['GATA6'], lookup=lookup)
        g = d['genes'][0]
        self.assertTrue(g['found'])
        self.assertEqual(['cell differentiation'], g['go_biological_process'])
        self.assertEqual(['NKX2-5', 'TBX5'], g['interaction_partners'])
        self.assertNotIn('effects', g)
        self.assertIn('not a direction of effect', d['note'])


class RequestTests(unittest.TestCase):
    def test_public_data_is_on_for_a_real_run_and_never_for_a_demo(self):
        obj = 'Increase viable macrophage production while keeping identity.'
        real = DISC.build(project_id='ipsc_macrophage', objective=obj, runtime_mode='local')
        demo = DISC.build(project_id='ipsc_macrophage', objective=obj,
                          runtime_mode='synthetic_demo', public_data=True)
        off = DISC.build(project_id='ipsc_macrophage', objective=obj, runtime_mode='local',
                         public_data=False)
        self.assertEqual((True, False, False),
                         (real['public_data'], demo['public_data'], off['public_data']))
        brief = DISC.render_brief(real, loop_dir='ai-x')
        self.assertIn('Public databases — permitted', brief)
        self.assertIn('datasets fetch-geo', brief)
        self.assertNotIn('Public databases — permitted', DISC.render_brief(off, loop_dir='ai-x'))


if __name__ == '__main__':
    unittest.main()
