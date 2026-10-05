"""The live GEO path, exercised without a network.

NCBI is unreachable from this repository's CI, and it must stay that way: an
ordinary test run has to work offline. But "untested because unreachable" left
the whole response-parsing path unexercised, and that is the half of a live
adapter most likely to be wrong — a field renamed, a count that arrives as a
string, a record of the wrong assay type counted as a hit.

So the transport is replaced and the recorded response shapes are fed through
the real parser. This says nothing about whether NCBI is up or whether its
schema still looks like this; it says that given these responses, the adapter
produces these candidates, and given a failure it refuses rather than reporting
an empty search as an absence of data.
"""
import io
import json
import unittest
import urllib.error
from unittest import mock

from biosense import contracts as K
from biosense.data.sources import geo

# shapes recorded from NCBI eutils: esearch returns ids, esummary returns a map
# keyed by those ids, with gdstype naming the assay
ESEARCH = {'esearchresult': {'count': '3', 'idlist': ['200012345', '200067890', '200099999']}}
ESUMMARY = {'result': {
    'uids': ['200012345', '200067890', '200099999'],
    '200012345': {'accession': 'GSE12345', 'title': 'M-CSF dose response in human macrophages',
                  'gdstype': 'Expression profiling by high throughput sequencing',
                  'taxon': 'Homo sapiens', 'n_samples': '12'},
    '200067890': {'accession': 'GSE67890', 'title': 'scRNA-seq of iPSC-derived myeloid cells',
                  'gdstype': 'Expression profiling by high throughput sequencing',
                  'taxon': 'Homo sapiens', 'n_samples': 6},
    # an assay the adapter has no modality for: it must be dropped, not guessed at
    '200099999': {'accession': 'GSE99999', 'title': 'Methylation profiling of something else',
                  'gdstype': 'Methylation profiling by genome tiling array',
                  'taxon': 'Homo sapiens', 'n_samples': '4'},
}}


def responses(*payloads):
    """A urlopen stand-in that answers each call with the next payload."""
    it = iter(payloads)

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(url, timeout=None):
        p = next(it)
        if isinstance(p, Exception):
            raise p
        return R(json.dumps(p).encode())
    return fake


class PermissionTests(unittest.TestCase):
    def test_live_needs_both_flags(self):
        """Two flags because a network call from inside an analysis loop should
        be something somebody did on purpose."""
        with self.assertRaises(K.ContractError) as e:
            geo.search('macrophage', live=True)
        self.assertIn('i_have_network_permission=True', str(e.exception))

    def test_the_offline_default_never_opens_a_socket(self):
        with mock.patch('urllib.request.urlopen',
                        side_effect=AssertionError('the offline path must not reach the network')):
            r = geo.search('macrophage M-CSF')
        self.assertFalse(r.live)
        self.assertIn('never that GEO has none', r.note)


class LiveParsingTests(unittest.TestCase):
    def live(self, *payloads, **kw):
        with mock.patch('urllib.request.urlopen', responses(*payloads)):
            return geo.search(kw.pop('query', 'macrophage M-CSF'), live=True,
                              i_have_network_permission=True, **kw)

    def test_it_parses_records_into_candidates(self):
        r = self.live(ESEARCH, ESUMMARY)
        self.assertTrue(r.live)
        by = {c.accession: c for c in r.candidates}
        self.assertIn('GSE12345', by)
        self.assertEqual('Homo sapiens', by['GSE12345'].organism)
        self.assertEqual(12, by['GSE12345'].sample_count)
        self.assertIn('acc=GSE12345', by['GSE12345'].url)

    def test_a_sample_count_that_arrives_as_a_string_is_still_a_number(self):
        """eutils returns these inconsistently, and a string here would compare
        wrongly everywhere downstream."""
        r = self.live(ESEARCH, ESUMMARY)
        for c in r.candidates:
            self.assertIsInstance(c.sample_count, int)

    def test_an_assay_it_has_no_modality_for_is_dropped_not_guessed(self):
        """Calling a methylation array something else would put it in front of a
        tool that cannot read it."""
        r = self.live(ESEARCH, ESUMMARY)
        self.assertNotIn('GSE99999', [c.accession for c in r.candidates])
        self.assertEqual(2, len(r.candidates))

    def test_nothing_is_downloaded_by_a_search(self):
        r = self.live(ESEARCH, ESUMMARY)
        self.assertIn('Nothing has been downloaded', r.note)

    def test_an_empty_search_is_not_reported_as_an_absence_of_data(self):
        r = self.live({'esearchresult': {'idlist': []}})
        self.assertEqual([], list(r.candidates))
        self.assertTrue(r.query_plan, 'the query that was run must still be visible')

    def test_a_network_failure_says_so_rather_than_returning_nothing(self):
        """The distinction the whole adapter is built around: no result is not
        the same as no such data."""
        r = self.live(urllib.error.URLError('connection refused'))
        self.assertEqual([], list(r.candidates))
        self.assertIn('No result is not the same as no such data', r.note)

    def test_a_malformed_response_is_handled_the_same_way(self):
        with mock.patch('urllib.request.urlopen', responses(ValueError('not json'))):
            r = geo.search('macrophage', live=True, i_have_network_permission=True)
        self.assertIn('Live GEO search failed', r.note)
        self.assertEqual([], list(r.candidates))

    def test_the_query_plan_is_reported_whether_or_not_it_matched(self):
        for payloads in ((ESEARCH, ESUMMARY), ({'esearchresult': {'idlist': []}},)):
            r = self.live(*payloads)
            self.assertTrue(r.query_plan[0]['url'].startswith('https://'))
            self.assertIn('eutils', r.query_plan[0]['url'])


if __name__ == '__main__':
    unittest.main()
