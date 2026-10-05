"""Privacy: a dataset somebody ingested does not leave the machine.

These are regression tests for a guarantee, so they are written against the
failure rather than the feature: each one asserts that a specific route *out* is
closed. The routes are the HTTP servers, the git working tree, the literature
claim path, and the listing endpoints.

The guard under test is path containment, not a filename rule. The older
`*truth*` convention protects files this project named itself; a person's
upload is not going to be named to suit it.
"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from biosense import contracts as K
from biosense.data import ingest as ING
from biosense.data import registry as DREG
from biosense.data import roots as DR
from biosense.data import sources as DSRC
from biosense.production import app as APP
from biosense.production import serve as SRV

FACS = K.ROOT / 'examples' / 'datasets' / 'facs_mcsf_synthetic.csv'
DESIGN = {'condition_column': 'condition', 'control': 'control', 'treatments': ['mcsf_high'],
          'replicate_column': 'replicate', 'donor_column': 'donor'}


class PrivacyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._env = os.environ.get(DR.PRIVATE_ENV)
        os.environ[DR.PRIVATE_ENV] = str(self.tmp / 'private')
        os.environ[DR.CACHE_ENV] = str(self.tmp / 'cache')
        DR.ensure_roots()
        self.manifest, self.manifest_path = ING.ingest_local(
            FACS, dataset_id='private-facs', title='Somebody\'s own FACS run',
            modality='cytometry_summary', perturbation='M-CSF concentration',
            experimental_design=DESIGN)

    def tearDown(self):
        if self._env is None:
            os.environ.pop(DR.PRIVATE_ENV, None)
        else:
            os.environ[DR.PRIVATE_ENV] = self._env
        os.environ.pop(DR.CACHE_ENV, None)
        shutil.rmtree(self.tmp, ignore_errors=True)


class PathGuardTests(PrivacyCase):
    def test_a_private_path_is_recognised_by_where_it_is(self):
        self.assertTrue(DR.is_private_path(self.manifest_path))
        self.assertFalse(DR.is_private_path(self.tmp / 'runs' / 'loop' / 'analysis.json'))

    def test_the_guard_does_not_depend_on_the_filename(self):
        """The file is called manifest.json. Nothing in its name marks it."""
        self.assertNotIn('truth', self.manifest_path.name.lower())
        self.assertNotIn('private', self.manifest_path.name.lower())
        self.assertTrue(DR.is_private_path(self.manifest_path))

    def test_a_symlink_cannot_present_private_data_as_public(self):
        link = self.tmp / 'public_link.json'
        try:
            link.symlink_to(self.manifest_path)
        except OSError:
            self.skipTest('symlinks unavailable here')
        self.assertTrue(DR.is_private_path(link.resolve()))

    def test_a_traversal_out_of_the_private_root_is_not_private(self):
        self.assertFalse(DR.is_private_path(DR.private_root() / '..' / 'elsewhere.csv'))

    def test_both_servers_refuse_a_private_file(self):
        self.assertTrue(SRV._is_forbidden(self.manifest_path))
        self.assertTrue(APP._is_forbidden(self.manifest_path))

    def test_a_server_rooted_inside_the_private_root_is_refused(self):
        with self.assertRaises(K.ContractError) as e:
            DR.assert_disjoint(DR.private_root() / 'runs')
        self.assertIn('overlaps the private data root', str(e.exception))

    def test_a_server_whose_root_contains_the_private_root_is_refused(self):
        with self.assertRaises(K.ContractError):
            DR.assert_disjoint(self.tmp)

    def test_assert_servable_refuses_a_private_file(self):
        with self.assertRaises(K.ContractError) as e:
            DR.assert_servable(self.manifest_path)
        self.assertIn('never served', str(e.exception))


class ListingTests(PrivacyCase):
    def test_a_public_listing_excludes_private_datasets(self):
        public = {m['dataset_id'] for m in DREG.list_datasets(include_private=False)}
        self.assertNotIn('private-facs', public)
        everything = {m['dataset_id'] for m in DREG.list_datasets(include_private=True)}
        self.assertIn('private-facs', everything)

    def test_the_local_search_adapter_honours_the_private_flag(self):
        hit = DSRC.get('local').search('M-CSF monocyte', include_private=True)
        self.assertIn('private-facs', [c.accession for c in hit.candidates])
        miss = DSRC.get('local').search('M-CSF monocyte', include_private=False)
        self.assertNotIn('private-facs', [c.accession for c in miss.candidates])


class HttpTests(PrivacyCase):
    """The real failure mode: a running server that will hand the file over."""

    def setUp(self):
        super().setUp()
        self.runs = self.tmp / 'runs'
        self.runs.mkdir()
        APP.Handler.runs_dir = self.runs
        APP.Handler.static_dir = K.ROOT / 'webapp'
        APP.Handler.registry = APP.Registry(self.runs)
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), APP.Handler)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def _get(self, path):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_the_dataset_endpoint_never_lists_a_private_dataset(self):
        code, body = self._get('/api/datasets')
        self.assertEqual(200, code)
        d = json.loads(body)
        self.assertNotIn('private-facs', [r['dataset_id'] for r in d['datasets']])
        self.assertIs(False, d['private_listed'])
        self.assertNotIn(b'private-facs', body)

    def test_no_url_shape_reaches_a_private_manifest(self):
        rel = os.path.relpath(self.manifest_path, K.ROOT / 'webapp')
        for path in ('/private_data/private-facs/manifest.json',
                     '/../private_data/private-facs/manifest.json',
                     f'/{rel}',
                     '/api/loops/../../private_data/private-facs/manifest.json'):
            code, body = self._get(path)
            self.assertEqual(404, code, path)
            self.assertNotIn(b'CD14', body, path)

    def test_the_payload_itself_is_not_reachable(self):
        code, body = self._get('/examples/datasets/facs_mcsf_synthetic.csv')
        self.assertEqual(404, code)


class GitTests(PrivacyCase):
    def test_the_private_root_is_ignored_by_git(self):
        import subprocess
        probe = DR.DEFAULT_PRIVATE / 'someones-data' / 'manifest.json'
        r = subprocess.run(['git', 'check-ignore', '-v', str(probe)],
                           cwd=K.ROOT, capture_output=True, text=True)
        self.assertEqual(0, r.returncode,
                         f'{probe} is NOT git-ignored; private data could be committed')

    def test_the_cache_root_is_ignored_by_git(self):
        import subprocess
        r = subprocess.run(['git', 'check-ignore', '-v', str(DR.DEFAULT_CACHE / 'x.csv')],
                           cwd=K.ROOT, capture_output=True, text=True)
        self.assertEqual(0, r.returncode)


class PromptPathTests(PrivacyCase):
    """A prompt typed into a web console is not consent to read private data."""

    def test_a_prompt_driven_run_may_use_public_data_and_not_private(self):
        from biosense.production import autonomy as AU
        from biosense.production import prompt as PR
        req, prov = PR.parse('Optimise conditions for growing wild-type T cells from iPSC.')
        g = AU.resolve(req)
        self.assertTrue(g['datasets_allowed'])
        self.assertFalse(g['private_data_allowed'])
        self.assertFalse(g['dataset_search_live'])

    def test_the_assumption_is_reported_to_whoever_typed_the_prompt(self):
        from biosense.production import prompt as PR
        _, prov = PR.parse('Optimise conditions for growing wild-type T cells from iPSC.')
        row = next(p for p in prov['fields'] if p['field'] == 'bioinformatics.private_data')
        self.assertIs(False, row['value'])
        self.assertIn('not consent', row['basis'])


class CitationTests(PrivacyCase):
    """A dataset cannot become a literature citation, by construction.

    A literature claim requires a source, a paragraph id and a verbatim quote.
    A measurement has none of those, so the firewall is the claim schema itself
    rather than a rule somebody has to remember.
    """

    def test_a_manifest_declares_itself_uncitable(self):
        self.assertIs(False, self.manifest['citable'])

    def test_an_analysis_result_declares_itself_uncitable(self):
        from biosense.bioinformatics import execute as EX
        from biosense.bioinformatics import plan as PLAN
        p = PLAN.plan(plan_id='plan-priv',
                      question='Does the M-CSF condition raise the CD14+ population?',
                      uncertainty_ref=PLAN.evidence_gap('GAP-1', 'No evidence on M-CSF dose.'),
                      recorded_gaps=['GAP-1'], why_requested='The loop holds no such evidence.',
                      dataset_ids=['private-facs'], analysis_type='population_comparison',
                      tool='cytometry.population_comparison',
                      decision_relevance='Whether to test a higher dose.',
                      parameters_that_may_change=['mcsf_ng_ml'])
        r = EX.execute(p)
        self.assertIs(False, r['citable'])

    def test_a_dataset_derived_value_cannot_satisfy_the_claim_schema(self):
        claim = {'id': 'C1', 'protocol_id': 'P1', 'parameter': 'mcsf_ng_ml', 'stage': 'myeloid',
                 'role': 'operating_condition', 'value': 100, 'unit': 'ng/mL',
                 'context': {}, 'condition_signature': 'x', 'notes': '',
                 'evidence': {'source_id': 'dataset:private-facs'}}
        handoff = {'schema_version': '2.0', 'request_id': 'r', 'ready_for_simulation': False,
                   'claims': [claim], 'candidate_parameters': [], 'conflicts': [],
                   'missing_required_parameters': [], 'selected_parameters': []}
        errors = K.schema_errors('literature_handoff', handoff)
        self.assertTrue(errors, 'a dataset-derived claim must not validate as literature evidence')
        self.assertTrue(any('paragraph_id' in e or 'quote' in e for e in errors), errors)

    def test_the_handoff_the_loop_builds_carries_no_dataset_claim(self):
        from biosense.production import design as DS
        req = K.read_json(K.ROOT / 'examples' / 'ipsc_tcell' / 'request.wt_d40.json')
        handoff = DS.build_handoff(req)
        for c in handoff['claims']:
            self.assertNotIn('dataset:', c['evidence']['source_id'])
            self.assertTrue(c['evidence']['quote'])


if __name__ == '__main__':
    unittest.main()
