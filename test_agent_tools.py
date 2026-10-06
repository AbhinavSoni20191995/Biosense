import copy
import unittest
from agent_tools import compile_handoff, convert_unit, effective_growth_rate

class LiteratureContractTests(unittest.TestCase):
    def setUp(self):
        self.request = {'request_id': 'synthetic-test', 'constraints': {'species': 'human'}, 'required_parameters': ['ipsc_expansion:duration']}
        self.sources = [{'id': 'TEST', 'article_type': 'research-article', 'paragraphs': [{'id': 'p0', 'text': 'Synthetic example: duration 48 h; a separate description gives 72 h.'}]}]
        self.claim = {'id': 'c1', 'protocol_id': 'test', 'parameter': 'duration', 'stage': 'ipsc_expansion', 'role': 'schedule', 'value': 48, 'unit': 'h', 'context': {'species': 'human', 'cell_origin': 'iPSC', 'target_cell': 'cardiomyocyte', 'cell_line': 'test', 'culture_format': 'test', 'medium': None, 'time_origin': 'stage_start', 'time_window': None}, 'condition_signature': 'same-condition', 'evidence': {'source_id': 'TEST', 'paragraph_id': 'p0', 'quote': 'duration 48 h'}, 'notes': 'Synthetic fixture, not biological evidence.'}

    def run_claims(self, claims):
        return compile_handoff(self.request, {'claims': claims}, self.sources)

    def test_missing_kinetics_stay_missing(self):
        self.request['required_parameters'].append('ipsc_expansion:mu_max')
        r = self.run_claims([self.claim])
        self.assertEqual(r['missing_required_parameters'], ['ipsc_expansion:mu_max'])
        self.assertFalse(r['ready_for_simulation'])

    def test_fabricated_quote_rejected(self):
        self.claim['evidence']['quote'] = 'growth was optimal'
        self.assertEqual(len(self.run_claims([self.claim])['rejected_claims']), 1)

    def test_different_values_conflict(self):
        other = copy.deepcopy(self.claim); other['id'] = 'c2'; other['value'] = 72; other['evidence']['quote'] = '72 h'
        r = self.run_claims([self.claim, other])
        self.assertEqual(len(r['conflicts']), 1)
        self.assertEqual(r['candidate_parameters'], [])

    def test_distinct_arms_are_not_a_conflict(self):
        other = copy.deepcopy(self.claim); other['id'] = 'c2'; other['value'] = 72; other['condition_signature'] = 'different-arm'; other['evidence']['quote'] = '72 h'
        self.assertEqual(self.run_claims([self.claim, other])['conflicts'], [])

    def test_unit_equivalent_values_agree(self):
        other = copy.deepcopy(self.claim); other['id'] = 'c2'; other['value'] = 2; other['unit'] = 'day'
        self.assertEqual(self.run_claims([self.claim, other])['conflicts'], [])

    def test_species_mismatch_excluded(self):
        self.claim['context']['species'] = 'mouse'
        self.assertEqual(len(self.run_claims([self.claim])['excluded_claims']), 1)

    def test_hard_limit_not_clipped(self):
        self.request['constraints']['parameter_limits'] = {'ipsc_expansion:duration': {'unit': 'day', 'max': 1}}
        r = self.run_claims([self.claim])
        self.assertEqual(r['claims'][0]['value'], 48)
        self.assertEqual(len(r['excluded_claims']), 1)

    def test_geometry_conversion_refused(self):
        with self.assertRaises(ValueError): convert_unit(1000, 'cells/cm2', 'cells/mL')

    def test_per_cell_rate_not_volumetric(self):
        with self.assertRaises(ValueError): convert_unit(.5, 'g/L/day', 'g/cell/day')

    def test_growth_derivation_is_not_mu_max(self):
        r = effective_growth_rate(2, 1)
        self.assertEqual(r['parameter'], 'effective_net_growth_rate')
        self.assertAlmostEqual(r['value'], .693147, places=5)

    def test_duplicate_ids_rejected(self):
        self.assertEqual(len(self.run_claims([self.claim, self.claim])['rejected_claims']), 1)

    def test_retracted_article_blocked(self):
        self.sources[0]['article_type'] = 'retraction'
        self.assertEqual(len(self.run_claims([self.claim])['rejected_claims']), 1)

if __name__ == '__main__': unittest.main()


import contextlib
import io
import json
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

import agent_tools as AT

PUBMED_XML = b'''<?xml version="1.0"?><PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>111</PMID>
<Article><Journal><Title>Synthetic Journal</Title><JournalIssue><PubDate><Year>2021</Year></PubDate></JournalIssue></Journal>
<ArticleTitle>Synthetic fixture: iPSC macrophages</ArticleTitle><Abstract><AbstractText Label="METHODS">Cells received 100 ng/mL M-CSF.</AbstractText></Abstract>
<AuthorList><Author><LastName>Doe</LastName><Initials>J</Initials></Author></AuthorList>
<PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList></Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">111</ArticleId><ArticleId IdType="pmc">PMC999</ArticleId><ArticleId IdType="doi">10.0/x</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>'''


class _Resp:
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, n=-1): return self.data


class RetrievalTests(unittest.TestCase):
    """Synthetic responses only: no network in ordinary CI."""

    def setUp(self):
        self.sleeps = []
        patcher = mock.patch.object(AT, '_sleep', self.sleeps.append)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_a_dropped_connection_is_retried_not_reported_as_no_evidence(self):
        answers = [urllib.error.URLError('reset'), _Resp(b'{"hitCount": 0, "resultList": {"result": []}}')]
        def fake(req, timeout):
            a = answers.pop(0)
            if isinstance(a, Exception): raise a
            return a
        with mock.patch('urllib.request.urlopen', fake):
            r = AT.search_literature('macrophage')
        self.assertEqual(0, r['hit_count'])
        self.assertEqual([1], self.sleeps)

    def test_a_not_found_is_an_answer_and_is_not_retried(self):
        """Each archive is asked once; a 404 moves on to the other, never
        back to the same one."""
        calls = []
        def fake(req, timeout):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 404, 'Not Found', {}, None)
        with mock.patch('urllib.request.urlopen', fake), self.assertRaisesRegex(ValueError, 'no archive'):
            AT.fetch_full_text('PMC1')
        self.assertEqual(2, len(calls))
        self.assertIn('europepmc', calls[0]); self.assertIn('efetch', calls[1])
        self.assertEqual([], self.sleeps)

    def test_filters_are_added_to_the_query_actually_sent(self):
        seen = []
        def fake(req, timeout):
            seen.append(req.full_url)
            return _Resp(b'{"hitCount": 3, "resultList": {"result": []}}')
        with mock.patch('urllib.request.urlopen', fake):
            r = AT.search_literature('iPSC AND macrophage', open_access=True, since=2015, sort='cited')
        self.assertEqual('(iPSC AND macrophage) AND OPEN_ACCESS:y AND PUB_YEAR:[2015 TO 3000]', r['query_sent'])
        self.assertIn('sort=CITED+desc', seen[0])

    def test_pubmed_returns_papers_in_the_europe_pmc_shape(self):
        def fake(req, timeout):
            if 'esearch' in req.full_url:
                return _Resp(b'{"esearchresult": {"count": "1", "idlist": ["111"]}}')
            return _Resp(PUBMED_XML)
        with mock.patch('urllib.request.urlopen', fake):
            r = AT.search_pubmed('iPSC macrophage M-CSF', since=2015)
        p = r['papers'][0]
        self.assertEqual(('111', 'PMC999', '2021', 'pubmed'), (p['pmid'], p['pmcid'], p['pubYear'], r['index']))
        self.assertIn('100 ng/mL M-CSF', p['abstractText'])

    def test_an_abstract_can_carry_a_checked_quote_and_says_it_is_an_abstract(self):
        paper = AT.parse_pubmed(PUBMED_XML)[0]
        src = AT.abstract_source(paper)
        self.assertEqual('abstract', src['article_type'])
        self.assertEqual(['p0000'], [p['id'] for p in src['paragraphs']])
        self.assertIn('100 ng/mL M-CSF', src['paragraphs'][0]['text'])

    def test_find_shows_only_the_paragraphs_that_matter(self):
        src = {'paragraphs': [{'id': 'p0', 'section': 'body / Methods', 'text': 'M-CSF at 100 ng/mL'},
                              {'id': 'p1', 'section': 'body / Intro', 'text': 'Macrophages are cells.'}]}
        self.assertEqual(['p0'], [p['id'] for p in AT.find_paragraphs(src, ['m-csf'])])

    def test_an_unreachable_index_is_one_readable_refusal_with_a_way_on(self):
        def fake(req, timeout): raise urllib.error.URLError('Name or service not known')
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, mock.patch('urllib.request.urlopen', fake), contextlib.redirect_stdout(out):
            code = AT.main(['search', 'macrophage', '--out', str(Path(d) / 's.json')])
        doc = json.loads(out.getvalue())
        self.assertEqual(1, code)
        self.assertTrue(doc['refused'])
        self.assertIn('pubmed', doc['next'])
        self.assertEqual(3, len(self.sleeps) + 1, 'retried before refusing')


PMC_SET_XML = b'''<?xml version="1.0"?><pmc-articleset><article article-type="research-article">
<front><article-meta><title-group><article-title>Synthetic: mirrored article</article-title></title-group></article-meta></front>
<body><sec><title>Methods</title><p>Cells received 50 ng/mL M-CSF from day 7.</p></sec></body></article></pmc-articleset>'''


class FullTextFallbackTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(AT, '_sleep', lambda s: None)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_an_archive_answering_500_is_not_the_end_of_the_paper(self):
        seen = []
        def fake(req, timeout):
            seen.append(req.full_url)
            if 'europepmc' in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 500, 'Server Error', {}, None)
            return _Resp(PMC_SET_XML)
        with mock.patch('urllib.request.urlopen', fake):
            src = AT.fetch_full_text('PMC999')
        self.assertEqual('ncbi_pmc', src['archive'])
        self.assertEqual(3, sum('europepmc' in u for u in seen), 'the first archive was retried')
        self.assertIn('50 ng/mL M-CSF', src['paragraphs'][0]['text'])
        self.assertEqual('body / Methods', src['paragraphs'][0]['section'])

    def test_both_archives_failing_is_one_refusal_naming_both(self):
        def fake(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 500, 'Server Error', {}, None)
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, mock.patch('urllib.request.urlopen', fake), contextlib.redirect_stdout(out):
            code = AT.main(['fetch', 'PMC999', '--out', str(Path(d) / 'f.json')])
        doc = json.loads(out.getvalue())
        self.assertEqual(1, code)
        self.assertIn('Europe PMC', doc['reason'])
        self.assertIn('NCBI PMC', doc['reason'])
        self.assertIn('abstract', doc['next'])
