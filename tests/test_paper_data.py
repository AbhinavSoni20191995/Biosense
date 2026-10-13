"""The datasets a paper deposited, found from the paper — without the network.

A run searched GEO nine times by gene, found nothing, and asked a person for
the accessions of studies it had already read. These replay recorded response
shapes from NCBI's ELink and ESummary and from Europe PMC's annotations API.
"""
import contextlib
import io
import json
import unittest
import urllib.error
import urllib.parse

from biosense import contracts as K
from biosense.bioinformatics import cli as BCLI
from biosense.data.sources import paper_data as PD

ELINK = {'linksets': [
    {'dbfrom': 'pubmed', 'ids': ['24166003'],
     'linksetdbs': [{'dbto': 'gds', 'linkname': 'pubmed_gds', 'links': ['200046080']}]},
    {'dbfrom': 'pubmed', 'ids': ['11111111']}]}
ESUMMARY = {'result': {'uids': ['200046080'], '200046080': {
    'accession': 'GSE46080', 'title': 'Pparg-deficient alveolar macrophages',
    'taxon': 'Mus musculus', 'gdstype': 'Expression profiling by array', 'n_samples': '8'}}}
ANNOTATIONS = [
    {'source': 'MED', 'extId': '24166003', 'pmcid': 'PMC3930045', 'annotations': [
        {'exact': 'GSE46080', 'type': 'Accession Numbers', 'section': 'Methods',
         'tags': [{'name': 'GSE46080', 'uri': 'http://identifiers.org/geo/GSE46080'}]},
        {'exact': 'E-GEOD-49999', 'type': 'Accession Numbers', 'section': 'Data availability',
         'tags': [{'name': 'E-GEOD-49999', 'uri': 'http://identifiers.org/arrayexpress/E-GEOD-49999'}]}]},
    {'source': 'PMC', 'extId': 'PMC9999999', 'annotations': [
        {'exact': 'E-MTAB-1234', 'type': 'Accession Numbers', 'section': 'Methods', 'tags': []}]}]


def transport(routes, calls=None):
    def get(url, timeout=60, max_bytes=None, data=None, headers=None):
        if calls is not None:
            calls.append(url)
        for key, body in routes.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return json.dumps(body).encode()
        raise urllib.error.HTTPError(url, 404, 'nf', {}, None)
    return get


SERVED = {'elink.fcgi': ELINK, 'esummary.fcgi': ESUMMARY, 'annotationsByArticleIds': ANNOTATIONS}


class PaperDataTests(unittest.TestCase):
    def find(self, papers, routes=SERVED, calls=None):
        return PD.for_papers(papers, i_have_network_permission=True,
                             get=transport(routes, calls))

    def test_a_paper_s_series_is_found_by_geo_s_link_and_its_text(self):
        doc = self.find(['PMID:24166003'])
        ds = {d['accession']: d for d in doc['papers'][0]['datasets']}
        self.assertEqual({'GSE46080', 'GSE49999'}, set(ds))
        gse = ds['GSE46080']
        self.assertEqual(('Mus musculus', 8), (gse['organism'], gse['samples']))
        self.assertEqual(['GEO links this series to the paper', "named in the paper's text "
                          '(Methods)'], gse['via'], 'both routes, said')
        self.assertIn('as E-GEOD-49999', ds['GSE49999']['via'][0],
                      'an ArrayExpress mirror of a GEO series is the series')
        self.assertEqual([], doc['errors'])

    def test_a_paper_with_no_deposit_says_so_rather_than_inventing_one(self):
        doc = self.find(['11111111'])
        self.assertEqual([], doc['papers'][0]['datasets'])
        self.assertIn('No deposit found', doc['papers'][0]['note'])

    def test_an_arrayexpress_only_accession_is_kept_and_its_limit_said(self):
        doc = self.find(['PMC9999999'])
        self.assertEqual(['E-MTAB-1234'], [d['accession'] for d in doc['papers'][0]['datasets']])
        self.assertIn('not read by fetch-geo', doc['next'])

    def test_one_source_failing_is_said_and_the_other_still_answers(self):
        routes = dict(SERVED, **{'elink.fcgi': urllib.error.HTTPError('u', 503, 'down', {}, None)})
        doc = self.find(['PMID:24166003'], routes)
        self.assertIn('GSE46080', [d['accession'] for d in doc['papers'][0]['datasets']])
        self.assertTrue(any("publication links could not be read" in e for e in doc['errors']))

    def test_papers_are_sent_to_europe_pmc_in_its_batches(self):
        calls = []
        self.find([f'PMID:{n}' for n in range(1, 18)], calls=calls)
        batches = [c for c in calls if 'annotationsByArticleIds' in c]
        self.assertEqual(3, len(batches), '17 papers in batches of 8')
        ids = urllib.parse.parse_qs(urllib.parse.urlparse(batches[0]).query)['articleIds'][0]
        self.assertEqual(8, len(ids.split(',')))

    def test_what_is_not_a_paper_id_is_refused_by_name(self):
        self.assertEqual(('MED', '123'), PD.normalise('PMID: 123'))
        self.assertEqual(('PMC', 'PMC456'), PD.normalise('pmc456'))
        with self.assertRaisesRegex(K.ContractError, 'a DOI has to be resolved'):
            PD.normalise('10.1038/nature12345')
        with self.assertRaisesRegex(K.ContractError, 'network'):
            PD.for_papers(['PMID:1'])

    def test_the_command_needs_the_permission_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = BCLI.main(['datasets', 'from-papers', '--papers', 'PMID:24166003'])
        self.assertEqual(1, code)
        self.assertIn('network', json.loads(out.getvalue())['message'])


if __name__ == '__main__':
    unittest.main()
