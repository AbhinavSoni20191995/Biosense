"""Established setpoints fill a protocol's physical gaps, labelled as what they are.

A protocol from a real run left agitation, temperature and dissolved oxygen as
GAPs because no paper about that exact cell stated them. Most of those values
are shared by suspension culture of iPSC and their derivatives whatever the
cells become. These tests hold the rules that keep a shared value honest: it
enters as a design choice with its basis, never as a reported value; an
unreviewed or vessel-less agitation entry carries low confidence; and only a
named reviewer adds entries.
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
from unittest import mock

from biosense import contracts as K
from biosense import projects as PJ
from biosense.data import roots as DR
from biosense.evidence import cli as EVCLI
from biosense.evidence import process_reference as PREF
from biosense.production import protocol_summary as PS


def entry(**kw):
    e = {'entry_id': 'agitation.ipsc-spinner', 'parameter_id': 'agitation_rpm', 'unit': 'rpm',
         'typical': 60.0, 'range': {'lower': 40.0, 'upper': 80.0},
         'context': {'cells': 'hiPSC aggregates', 'format': 'stirred suspension',
                     'vessel': 'spinner flask 125 mL', 'stage': 'expansion'},
         'basis': 'cited',
         'sources': [{'ref': 'PMID:1', 'quote': 'Spinner flasks were stirred at 60 rpm.',
                      'locator': 'Methods'}],
         'confidence': 'moderate', 'status': 'reviewed', 'reviewed_by': 'A. Reviewer',
         'reviewed_at': None, 'from_run': None, 'notes': None}
    e.update(kw)
    return e


class _PrivateRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {DR.PRIVATE_ENV: str(self.tmp / 'private')})
        patcher.start()
        self.addCleanup(patcher.stop)

    def curate(self, *entries):
        PREF.private_path().parent.mkdir(parents=True, exist_ok=True)
        K.write_json_atomic(PREF.private_path(), {
            'schema_version': K.PRODUCTION_VERSION, 'updated_at': None, 'note': 't',
            'entries': list(entries)})


class EntryRuleTests(_PrivateRoot):
    def test_the_committed_reference_is_valid_and_holds_conventions_only(self):
        doc = K.read_json(PREF.COMMITTED)
        K.require_valid('process_reference', doc)
        for e in doc['entries']:
            PREF.check_entry(e)
            self.assertEqual('convention', e['basis'], 'citations live on the server, reviewed')

    def test_a_cited_entry_without_a_source_and_a_convention_with_one_are_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'needs at least one source'):
            PREF.check_entry(entry(sources=[]))
        with self.assertRaisesRegex(K.ContractError, 'carries no source'):
            PREF.check_entry(entry(basis='convention'))
        with self.assertRaisesRegex(K.ContractError, 'outside its own range'):
            PREF.check_entry(entry(typical=120.0))

    def test_an_unreviewed_entry_fills_only_at_low_confidence_and_says_so(self):
        self.curate(entry(status='unreviewed', reviewed_by=None, confidence='high'))
        e, conf, caveats = PREF.entry_for('agitation_rpm')
        self.assertEqual('low', conf)
        self.assertIn('unreviewed', caveats[0])

    def test_agitation_without_a_vessel_or_in_another_vessel_is_low(self):
        self.curate(entry(context={'cells': 'hiPSC', 'format': 'stirred', 'vessel': None,
                                   'stage': None}))
        self.assertEqual('low', PREF.entry_for('agitation_rpm')[1])
        self.curate(entry())

        class P:
            doc = {'biological_system': {'vessel': 'PBS-3 vertical wheel'}}
        e, conf, caveats = PREF.entry_for('agitation_rpm', P())
        self.assertEqual('low', conf)
        self.assertIn('not this project', caveats[0])

        class Same:
            doc = {'biological_system': {'vessel': 'Spinner flask 125 mL'}}
        self.assertEqual('moderate', PREF.entry_for('agitation_rpm', Same())[1])

    def test_a_curated_entry_replaces_the_committed_one_with_the_same_id(self):
        self.curate(entry(entry_id='temperature.mammalian-convention', parameter_id='temperature_c',
                          unit='degC', typical=36.5, range=None))
        e, _, _ = PREF.entry_for('temperature_c')
        self.assertEqual(36.5, e['typical'])


class AutoFillTests(_PrivateRoot):
    def test_an_unset_temperature_is_a_design_choice_from_the_reference_not_a_gap(self):
        project = PJ.load('ipsc_macrophage')
        hyp = EVCLI.build_hypothesis(EVCLI.TEMPLATE | {'effects': EVCLI.TEMPLATE['effects'][:1]},
                                     project_id='ipsc_macrophage')
        real = PS._current_value
        with mock.patch.object(PS, '_current_value',
                               lambda pr, p, q, c: None if p == 'temperature_c' else real(pr, p, q, c)):
            doc = PS.build(project=project, objective='o', hypotheses=[hyp],
                           runtime_mode='synthetic_demo')
        row = next(p for s in doc['stages'] for p in s['parameters']
                   if p['parameter_id'] == 'temperature_c')
        self.assertEqual('design_choice', row['provenance'], 'never "reported"')
        self.assertEqual(37.0, row['recommended_value'])
        self.assertEqual('temperature.mammalian-convention',
                         row['design_choice']['reference_entry'])
        self.assertNotIn('temperature_c', [g['parameter_id'] for g in doc['gaps']])
        self.assertTrue(any('filled from the process reference' in x for x in doc['limitations']))
        K.require_valid('protocol_summary', doc)

    def test_a_run_s_own_design_choice_wins_over_the_reference(self):
        project = PJ.load('ipsc_macrophage')
        q = project.parameter('temperature_c')
        mine = {'temperature_c': {'parameter_id': 'temperature_c', 'value': q.maximum or 37.0,
                                  'confidence': 'low', 'rationale': 'mine',
                                  'derived_from': ['x'], 'unit': q.unit}}
        real = PS._current_value
        with mock.patch.object(PS, '_current_value',
                               lambda pr, p, q, c: None if p == 'temperature_c' else real(pr, p, q, c)):
            doc = PS.build(project=project, objective='o', hypotheses=[], design_choices=mine,
                           runtime_mode='synthetic_demo')
        row = next(p for s in doc['stages'] for p in s['parameters']
                   if p['parameter_id'] == 'temperature_c')
        self.assertIsNone(row['design_choice'].get('reference_entry'))


class PromoteTests(_PrivateRoot):
    def test_promotion_needs_a_named_reviewer_and_stamps_every_entry(self):
        draft = {'entries': [entry(status='unreviewed', reviewed_by=None)]}
        with self.assertRaisesRegex(K.ContractError, 'name of the person'):
            PREF.promote(draft, reviewed_by='  ')
        ids = PREF.promote(draft, reviewed_by='Dr Reviewer', from_run='abcdef12')
        self.assertEqual(['agitation.ipsc-spinner'], ids)
        stored = K.read_json(PREF.private_path())['entries'][0]
        self.assertEqual(('reviewed', 'Dr Reviewer', 'abcdef12'),
                         (stored['status'], stored['reviewed_by'], stored['from_run']))

    def test_one_bad_entry_refuses_the_draft_and_writes_nothing(self):
        draft = {'entries': [entry(), entry(entry_id='agitation.bad', sources=[])]}
        with self.assertRaises(K.ContractError):
            PREF.promote(draft, reviewed_by='Dr Reviewer')
        self.assertFalse(PREF.private_path().exists())


class BriefTests(unittest.TestCase):
    def test_a_reference_run_is_briefed_for_it_and_purpose_is_checked(self):
        from biosense.production import discovery as DISC
        req = DISC.build(project_id='ipsc_macrophage',
                         objective='Collect established suspension-culture setpoints.',
                         runtime_mode='synthetic_demo', purpose='process_reference')
        K.require_valid('discovery_request', req)
        brief = DISC.render_brief(req, loop_dir='ai-x')
        self.assertIn('This run builds the process reference', brief)
        self.assertIn('process_reference.draft.json', brief)
        plain = DISC.render_brief(DISC.build(project_id='ipsc_macrophage',
                                             objective='Increase viable macrophage production.',
                                             runtime_mode='synthetic_demo'), loop_dir='ai-x')
        self.assertNotIn('This run builds the process reference', plain)
        with self.assertRaisesRegex(K.ContractError, 'purpose'):
            DISC.build(project_id='ipsc_macrophage', objective='x' * 20,
                       runtime_mode='synthetic_demo', purpose='anything')


class ApiTests(_PrivateRoot):
    """The draft is shown on the run page and promoted by an operator."""

    def setUp(self):
        super().setUp()
        from biosense.production import authz as AZ
        from biosense.production import runtime as RT
        from biosense import workspace as WS
        from biosense.production import app as APP
        from biosense.production import budget as BU
        (self.tmp / 'runs').mkdir()
        cfg = RT.from_env(runs_dir=self.tmp / 'runs', env={})
        H = APP.Handler
        saved = {k: getattr(H, k, None) for k in (
            'runs_dir', 'static_dir', 'registry', 'runtime_cfg', 'limits', 'policy',
            'discovery', 'sessions', 'default_identity')}
        self.addCleanup(lambda: [setattr(H, k, v) for k, v in saved.items()])
        H.runs_dir = self.tmp / 'runs'
        H.static_dir = K.ROOT / 'webapp'
        H.registry = APP.Registry(self.tmp / 'runs')
        H.runtime_cfg = cfg
        H.limits = BU.Limits()
        H.policy = AZ.Policy()
        H.discovery = APP.DiscoveryRegistry(self.tmp / 'runs', cfg)
        H.sessions = WS.SessionStore()
        H.default_identity = WS.local_identity()
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.rid = 'abcdef1234567890'
        self.run_dir = self.tmp / 'runs' / f'ai-20260101-{self.rid[:6]}'
        self.run_dir.mkdir()
        from biosense.production import run_store as RS
        RS.write_state(self.run_dir, {'run_id': self.rid, 'status': 'succeeded',
                                      'owner': None, 'runtime_mode': 'synthetic_demo'})

    def _req(self, method, path, body=None):
        r = urllib.request.Request(
            f'http://127.0.0.1:{self.srv.server_address[1]}{path}', method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b'{}')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def test_a_draft_is_promoted_under_the_reviewer_s_name(self):
        path = f'/api/discovery/{self.rid}/promote-reference'
        self.assertEqual(404, self._req('POST', path, {'reviewed_by': 'R'})[0], 'no draft yet')
        K.write_json_atomic(self.run_dir / PREF.DRAFT_NAME,
                            {'entries': [entry(status='unreviewed', reviewed_by=None)]})
        code, d = self._req('POST', path, {'reviewed_by': ''})
        self.assertEqual(400, code)
        self.assertIn('name of the person', d['error'])
        code, d = self._req('POST', path, {'reviewed_by': 'Dr Reviewer'})
        self.assertEqual(200, code, d)
        self.assertEqual(['agitation.ipsc-spinner'], d['promoted'])
        self.assertEqual('Dr Reviewer', PREF.entry_for('agitation_rpm')[0]['reviewed_by'])
        self.assertEqual(404, self._req('POST', '/api/discovery/..%2a/promote-reference',
                                        {'reviewed_by': 'R'})[0])

    def test_the_run_page_reads_the_draft_and_its_problems(self):
        from biosense.production import app as APP
        run = APP.DiscoveryRun.__new__(APP.DiscoveryRun)
        run.out_dir, run._live_mtimes, run.reference_draft = self.run_dir, {}, None
        K.write_json_atomic(self.run_dir / PREF.DRAFT_NAME,
                            {'entries': [entry(), entry(entry_id='agitation.bad', sources=[])]})
        run._read_reference_draft()
        self.assertEqual(2, len(run.reference_draft['entries']))
        self.assertEqual(1, len(run.reference_draft['errors']))
        self.assertIn('needs at least one source', run.reference_draft['errors'][0])


if __name__ == '__main__':
    unittest.main()
