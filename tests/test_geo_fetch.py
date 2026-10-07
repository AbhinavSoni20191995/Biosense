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


class FetchTests(unittest.TestCase):
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
            with self.assertRaisesRegex(K.ContractError, 'no processed RNA-seq counts.*suppl'):
                GF.fetch(ACC, condition_key='treatment', control='vehicle',
                         treatments=['ATRA 100 nM'], i_have_network_permission=True)


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
