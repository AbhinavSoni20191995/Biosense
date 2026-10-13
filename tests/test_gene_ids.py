"""Gene identifiers to symbols, without asking a person — and without the network.

A run fetched three human series and could analyse none of them by gene: the
symbol table did not arrive, every symbol-based tool refused, and the run asked
a person for an identifier map NCBI and Ensembl publish for free. These replay
recorded response shapes through the lookup, so each source, the order they
are tried in, what is kept on this server and what is said are checked here.
Whether the services are up is checked by a live run.
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
from biosense.data.sources import gene_ids as GI
from biosense.data.sources import geo_fetch as GF
from biosense.data.sources import http as HTTP
from tests.test_geo_fetch import ACC, ANNOT, SERVED, Sandbox, fake_get

GENE_INFO = '\n'.join([
    '#tax_id\tGeneID\tSymbol\tLocusTag\tSynonyms\tdbXrefs\tchromosome',
    '9606\t5468\tPPARG\t-\tCIMT1|NR1C3\tMIM:601487|HGNC:HGNC:9236|Ensembl:ENSG00000132170\t3',
    '9606\t8685\tMARCO\t-\tSCARA2\tMIM:604870|HGNC:HGNC:6895|Ensembl:ENSG00000019169\t2',
    '9606\t60\tACTB\t-\t-\tHGNC:HGNC:132|Ensembl:ENSG00000075624.17\t7',
    '9606\t999\t-\t-\t-\t-\t-',
])


def transport(routes, calls=None):
    """A stand-in for the network: each URL fragment answers with a body, an
    exception, or — for a POST — a function of the posted JSON."""
    def get(url, timeout=60, max_bytes=None, data=None, headers=None):
        if calls is not None:
            calls.append((url, json.loads(data) if data else None))
        for key, body in routes.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                if callable(body):
                    body = body(json.loads(data))
                return gzip.compress((body if isinstance(body, str) else json.dumps(body)).encode())
        raise urllib.error.HTTPError(url, 404, 'not found', {}, None)
    return get


class ParsingTests(unittest.TestCase):
    def test_gene_info_gives_both_maps_from_one_file(self):
        """Its header begins with '#', which the annotation reader skips as a
        comment; the Ensembl id is one of the dbXrefs."""
        by_id, by_ens = GI.parse_gene_info(GENE_INFO)
        self.assertEqual({'5468': 'PPARG', '8685': 'MARCO', '60': 'ACTB'}, by_id,
                         'a gene with no symbol ("-") is left out, not named "-"')
        self.assertEqual('ACTB', by_ens['ENSG00000075624'], 'the version is not part of the id')
        self.assertEqual('PPARG', by_ens['ENSG00000132170'])
        clash = GENE_INFO + '\n9606\t7\tOTHER\t-\t-\tEnsembl:ENSG00000132170\t1'
        self.assertNotIn('ENSG00000132170', GI.parse_gene_info(clash)[1],
                         'an Ensembl id two genes claim is left out rather than guessed')
        self.assertEqual(({}, {}), GI.parse_gene_info('A\tB\n1\t2\n'))

    def test_the_cache_name_is_the_file_a_url_names(self):
        self.assertEqual('Human.GRCh38.p13.annot.tsv.gz', GI._name(GF.counts_urls(
            ACC, 'Homo sapiens')[1]))
        self.assertEqual('Homo_sapiens.gene_info.gz', GI._name(GI.GENE_INFO['Homo sapiens']))


class KeptOnThisServerTests(unittest.TestCase):
    """A burst of fetches is one download: the table is kept once it parses."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.url = GF.counts_urls(ACC, 'Homo sapiens')[1]

    def maps(self, get):
        return GI.ncbi_maps('Homo sapiens', annot_url=self.url, get=get, timeout=5,
                            cache_dir=self.d)

    def test_one_download_serves_every_later_series(self):
        calls = []
        first = self.maps(transport({'annot.tsv.gz': ANNOT}, calls))
        self.assertEqual('GATA6', first.by_geneid['2627'])
        self.assertIn('fetched now', first.geneid_note)
        # Now the service throttles — the failure a burst of fetches drew.
        throttled = transport({'annot.tsv.gz': urllib.error.HTTPError('u', 429, 'slow', {}, None)},
                              calls)
        again = self.maps(throttled)
        self.assertEqual('GATA6', again.by_geneid['2627'])
        self.assertIn('a copy kept on this server', again.geneid_note)
        self.assertEqual(1, sum('annot' in u for u, _ in calls), 'downloaded once')
        meta = json.loads((self.d / 'gene_ids' / 'Human.GRCh38.p13.annot.tsv.gz.json').read_text())
        self.assertEqual(self.url, meta['url'])
        self.assertEqual(64, len(meta['sha256']))

    def test_an_error_page_is_never_kept_as_a_table(self):
        page = '<html><body>Too many requests</body></html>'
        m = self.maps(transport({'annot.tsv.gz': page, 'gene_info': GENE_INFO}))
        self.assertTrue(any('no GeneID and symbol columns' in e and 'Too many requests' in e
                            for e in m.errors), m.errors)
        self.assertFalse((self.d / 'gene_ids' / 'Human.GRCh38.p13.annot.tsv.gz').exists())
        later = self.maps(transport({'annot.tsv.gz': ANNOT}))
        self.assertIn('fetched now', later.geneid_note, 'the next fetch tries the service again')

    def test_a_kept_copy_that_no_longer_parses_is_dropped_and_fetched_again(self):
        self.maps(transport({'annot.tsv.gz': ANNOT}))
        kept = self.d / 'gene_ids' / 'Human.GRCh38.p13.annot.tsv.gz'
        kept.write_bytes(b'\x1f\x8bnot really gzip')
        m = self.maps(transport({'annot.tsv.gz': ANNOT}))
        self.assertEqual('GATA6', m.by_geneid['2627'])
        self.assertIn('fetched now', m.geneid_note)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.url = GF.counts_urls(ACC, 'Homo sapiens')[1]

    def test_gene_info_answers_when_the_annotation_will_not(self):
        m = GI.ncbi_maps('Homo sapiens', annot_url=self.url, timeout=5, cache_dir=self.d,
                         get=transport({'annot.tsv.gz': urllib.error.HTTPError(
                             'u', 503, 'down', {}, None), 'gene_info': GENE_INFO}))
        self.assertEqual('PPARG', m.by_geneid['5468'])
        self.assertIn("NCBI's gene_info", m.geneid_note)
        self.assertTrue(any('annotation table could not be read' in e for e in m.errors),
                        'the source that failed is still said')

    def test_ensembl_ids_are_looked_up_where_ncbi_has_no_cross_reference(self):
        calls = []

        def lookup(body):
            known = {'ENSG00000019169': 'MARCO', 'ENSDARG00000017821': 'gata6'}
            return {i: ({'display_name': known[i]} if i in known else None) for i in body['ids']}
        emap, used, note = GI.ensembl_map(
            ['ENSG00000132170', 'ENSG00000019169.12', 'ENSDARG00000017821', 'ENSG00000000000'],
            'Homo sapiens', annot_url=self.url, timeout=5, cache_dir=self.d,
            get=transport({'annot.tsv.gz': ANNOT, 'rest.ensembl.org': lookup}, calls))
        self.assertEqual({'ENSG00000019169': 'MARCO', 'ENSDARG00000017821': 'gata6'}, emap)
        self.assertIn('rest.ensembl.org', used)
        self.assertIn('has no Ensembl column', note, 'what NCBI could not do is said')
        posted = next(b for u, b in calls if 'rest.ensembl.org' in u)
        self.assertNotIn('ENSG00000019169.12', posted['ids'], 'looked up without its version')

    def test_a_large_table_is_looked_up_in_the_endpoint_s_batches(self):
        calls = []
        ids = [f'ENSG{n:011d}' for n in range(2500)]
        emap, _, _ = GI.ensembl_map(
            ids, 'Danio rerio', annot_url=None, timeout=5, cache_dir=self.d,
            get=transport({'rest.ensembl.org': lambda b: {i: {'display_name': 'g' + i[-4:]}
                                                         for i in b['ids']}}, calls))
        self.assertEqual(2500, len(emap))
        self.assertEqual([1000, 1000, 500], [len(b['ids']) for _, b in calls])

    def test_a_lookup_that_fails_part_way_keeps_what_it_had_and_says_so(self):
        n = {'calls': 0}

        def flaky(body):
            n['calls'] += 1
            if n['calls'] == 2:
                raise urllib.error.HTTPError('u', 500, 'down', {}, None)
            return {i: {'display_name': 'g'} for i in body['ids']}

        def get(url, timeout=60, max_bytes=None, data=None, headers=None):
            return json.dumps(flaky(json.loads(data))).encode()
        got, err = GI.ensembl_symbols([f'ENSG{n:011d}' for n in range(1500)], get=get, timeout=5)
        self.assertEqual(1000, len(got))
        self.assertIn("Ensembl's lookup could not be reached", err)

    def test_nothing_mapped_is_none_so_the_ids_are_kept_and_it_is_said(self):
        emap, used, note = GI.ensembl_map(['ENSG00000132170'], 'Homo sapiens',
                                          annot_url=self.url, timeout=5, cache_dir=self.d,
                                          get=transport({}))
        self.assertIsNone(emap)
        self.assertIsNone(used)
        self.assertIn('could not be read', note)
        self.assertIn("Ensembl's lookup could not be reached", note)


class RunScenarioTests(Sandbox):
    """The run that asked for an identifier map: the annotation did not come."""

    def test_a_series_whose_annotation_will_not_come_is_still_read_by_symbol(self):
        served = dict(SERVED)
        served['Human.GRCh38.p13.annot.tsv.gz'] = urllib.error.HTTPError('u', 429, 'slow', {}, None)
        served['Homo_sapiens.gene_info.gz'] = GENE_INFO.replace('9606\t60\tACTB', '9606\t60\tACTB') \
            + '\n9606\t2627\tGATA6\t-\t-\t-\t18\n9606\t7037\tTFRC\t-\t-\t-\t3'
        with mock.patch.object(GF, '_get', fake_get(served)):
            d = GF.fetch(ACC, condition_key='treatment', control='vehicle',
                         treatments=['ATRA 100 nM'], genes=['GATA6', 'TFRC'],
                         i_have_network_permission=True)
        self.assertEqual(2, d['genes_in_table'], 'the panel matches by symbol')
        self.assertIn("3 of 4 NCBI GeneIDs given a symbol from NCBI's gene_info",
                      d['how_read']['symbols'])
        self.assertIn('annotation table could not be read', d['how_read']['symbols'])
        self.assertNotIn('symbols_warning', d['how_read'])
        self.assertIn('gene_info', d['sources']['annotation'])


class RetryTests(unittest.TestCase):
    """A 429 is the service saying "not now", not "not here"."""

    def setUp(self):
        self.slept = []
        p = mock.patch.object(HTTP, '_sleep', self.slept.append)
        p.start()
        self.addCleanup(p.stop)

    def failing(self, *errors, then=b'ok'):
        it = iter(errors)

        def fn():
            e = next(it, None)
            if e is None:
                return then
            raise e
        return fn

    def test_throttling_and_server_errors_are_retried_with_backoff(self):
        fn = self.failing(urllib.error.HTTPError('u', 429, 'slow', {}, None),
                          urllib.error.HTTPError('u', 503, 'down', {}, None))
        self.assertEqual(b'ok', HTTP.with_retry(fn))
        self.assertEqual([2, 4], self.slept)

    def test_a_retry_after_header_is_honoured_and_capped(self):
        fn = self.failing(urllib.error.HTTPError('u', 429, 'slow', {'Retry-After': '7'}, None),
                          urllib.error.HTTPError('u', 429, 'slow', {'Retry-After': '900'}, None))
        HTTP.with_retry(fn)
        self.assertEqual([7.0, float(HTTP.MAX_RETRY_AFTER_S)], self.slept)

    def test_an_answer_is_not_retried(self):
        with self.assertRaises(urllib.error.HTTPError):
            HTTP.with_retry(self.failing(urllib.error.HTTPError('u', 404, 'nf', {}, None)))
        self.assertEqual([], self.slept, 'a 404 is the answer')
        with self.assertRaises(urllib.error.URLError):
            HTTP.with_retry(self.failing(urllib.error.URLError(ConnectionRefusedError())))
        self.assertEqual([], self.slept, 'a refused connection will not change in seconds')

    def test_it_gives_up_after_its_tries(self):
        with self.assertRaises(urllib.error.HTTPError):
            HTTP.with_retry(self.failing(*[urllib.error.HTTPError('u', 429, 's', {}, None)] * 9))
        self.assertEqual(HTTP.TRIES - 1, len(self.slept))
        timeout = self.failing(urllib.error.URLError(TimeoutError('timed out')))
        self.assertEqual(b'ok', HTTP.with_retry(timeout))


if __name__ == '__main__':
    unittest.main()
