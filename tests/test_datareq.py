"""What the run could not get, asked of the person who can supply it.

An agent that cannot fetch a table writes a limitation, and that is usually
where it ends — the run goes thin and nobody is told what would have fixed it.
Often the person who started it has exactly the missing thing. These hold the
handover: an ask that says what failed and what it would unblock, an answer
recorded with its provenance, and a next round that carries it.
"""
import copy
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from biosense import contracts as K
from biosense import workspace as WS
from biosense.evidence import datareq as DRQ
from biosense.production import authz as AZ
from biosense.production import budget as BU
from biosense.production import discovery as DISC
from biosense.production import run_store as RS
from biosense.production import runtime as RT


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_an_ask_says_what_failed_and_what_it_would_unblock(self):
        doc = DRQ.build(DRQ.TEMPLATE, run_id='ai-x')
        K.require_valid('data_requests', doc)
        first = doc['items'][0]
        self.assertEqual('processed_table', first['needs'])
        self.assertIn('404', first['why'])
        self.assertTrue(first['unblocks'])
        self.assertEqual('open', first['status'])
        self.assertIsNone(first['answer'])

    def test_an_ask_with_no_reason_or_no_purpose_is_refused(self):
        for field in ('why', 'unblocks'):
            d = copy.deepcopy(DRQ.TEMPLATE)
            d['items'][0][field] = ''
            with self.assertRaisesRegex(K.ContractError, field):
                DRQ.build(d, run_id='ai-x')
        with self.assertRaisesRegex(K.ContractError, 'at least one thing'):
            DRQ.build({'items': []}, run_id='ai-x')

    def test_an_answer_carries_who_gave_it_and_when(self):
        K.write_json_atomic(self.d / DRQ.NAME, DRQ.build(DRQ.TEMPLATE, run_id='ai-x'))
        item = DRQ.answer(self.d, 'R1', dataset_id='my-counts', provided_by='Ana')
        self.assertEqual('answered', item['status'])
        self.assertEqual(('my-counts', 'Ana'),
                         (item['answer']['dataset_id'], item['answer']['provided_by']))
        self.assertTrue(item['answer']['at'])
        self.assertEqual('answered', DRQ.load(self.d)['items'][0]['status'], 'written to disk')

    def test_an_empty_answer_is_not_an_answer_and_declining_is_its_own_state(self):
        K.write_json_atomic(self.d / DRQ.NAME, DRQ.build(DRQ.TEMPLATE, run_id='ai-x'))
        with self.assertRaisesRegex(K.ContractError, 'give a dataset id, a link, or what you know'):
            DRQ.answer(self.d, 'R1')
        item = DRQ.decline(self.d, 'R2', note='the platform record is gone', provided_by='Ana')
        self.assertEqual('declined', item['status'])

    def test_the_handover_separates_what_can_be_analysed_from_what_was_said(self):
        K.write_json_atomic(self.d / DRQ.NAME, DRQ.build(DRQ.TEMPLATE, run_id='ai-x'))
        DRQ.answer(self.d, 'R1', dataset_id='my-counts', provided_by='Ana')
        DRQ.answer(self.d, 'R2', note='we see CD14 at about 80% by flow', provided_by='Ana')
        h = DRQ.handover(DRQ.load(self.d))
        self.assertEqual(['my-counts'], h['dataset_ids'], 'only a dataset can be analysed')
        self.assertIn('CD14 at about 80%', h['results'])
        self.assertEqual((0, 2), (h['open'], h['answered']))

    def test_a_declined_ask_tells_the_next_round_to_stop_asking(self):
        K.write_json_atomic(self.d / DRQ.NAME, DRQ.build(DRQ.TEMPLATE, run_id='ai-x'))
        DRQ.decline(self.d, 'R1', note='never published', provided_by='Ana')
        h = DRQ.handover(DRQ.load(self.d))
        self.assertIn('cannot be supplied', h['results'])
        self.assertIn('do not ask for it again', h['results'])
        self.assertEqual([], h['dataset_ids'])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        from biosense.production import app as APP
        cls.APP = APP
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / 'runs').mkdir()
        cls._private = os.environ.get('BIOSENSE_PRIVATE_DATA')
        os.environ['BIOSENSE_PRIVATE_DATA'] = str(cls.tmp / 'private')
        cfg = RT.from_env(runs_dir=cls.tmp / 'runs', env={})
        APP.Handler.runs_dir = cls.tmp / 'runs'
        APP.Handler.static_dir = K.ROOT / 'webapp'
        APP.Handler.registry = APP.Registry(cls.tmp / 'runs')
        APP.Handler.runtime_cfg = cfg
        APP.Handler.limits = BU.Limits()
        APP.Handler.policy = AZ.Policy()
        APP.Handler.discovery = APP.DiscoveryRegistry(cls.tmp / 'runs', cfg)
        APP.Handler.sessions = WS.SessionStore()
        APP.Handler.default_identity = WS.local_identity()
        APP.Handler._ready = {'at': 0.0, 'body': None, 'code': 503}
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), APP.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        import os
        cls.srv.shutdown()
        cls.srv.server_close()
        if cls._private is None:
            os.environ.pop('BIOSENSE_PRIVATE_DATA', None)
        else:
            os.environ['BIOSENSE_PRIVATE_DATA'] = cls._private
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _req(self, method, path, body=None):
        r = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}', method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b'{}')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def _ended(self, rid):
        d = self.tmp / 'runs' / f'ai-20260101-{rid[:6]}'
        d.mkdir()
        req = DISC.build(project_id='ipsc_macrophage', runtime_mode='local_real_ai',
                         objective='increase viable macrophage yield from iPSC')
        (d / 'discovery_request.json').write_text(json.dumps(req))
        K.write_json_atomic(d / DRQ.NAME, DRQ.build(DRQ.TEMPLATE, run_id=d.name))
        RS.write_state(d, {'run_id': rid, 'status': 'done', 'is_real': True,
                           'runtime_mode': 'local_real_ai', 'owner': None,
                           'project_id': 'ipsc_macrophage', 'objective': req['objective'],
                           'run_dir': d.name, 'started_at': time.time() - 60,
                           'finished_at': time.time()})
        return d

    def test_the_run_shows_what_it_asked_for_and_takes_an_answer(self):
        rid = 'aaaa111122223333'
        self._ended(rid)
        code, snap = self._req('GET', f'/api/discovery/{rid}')
        self.assertEqual(200, code)
        self.assertEqual(2, len(snap['data_requests']['items']))
        self.assertEqual(2, snap['data_requests']['handover']['open'])
        code, body = self._req('POST', f'/api/discovery/{rid}/data-request',
                               {'request_id': 'R1', 'dataset_id': 'my-counts'})
        self.assertEqual(200, code, body)
        self.assertEqual('answered', body['item']['status'])
        self.assertEqual(['my-counts'], body['handover']['dataset_ids'])
        # Read back live, not from the state written when the run ended.
        code, snap = self._req('GET', f'/api/discovery/{rid}')
        self.assertEqual(1, snap['data_requests']['handover']['answered'])

    def test_an_answer_starts_the_next_round_carrying_what_was_given(self):
        rid = 'bbbb111122223333'
        self._ended(rid)
        self._req('POST', f'/api/discovery/{rid}/data-request',
                  {'request_id': 'R1', 'dataset_id': 'my-counts'})
        self._req('POST', f'/api/discovery/{rid}/data-request',
                  {'request_id': 'R2', 'note': 'the platform record gives the mapping'})
        code, snap = self._req('GET', f'/api/discovery/{rid}')
        h = snap['data_requests']['handover']
        seen = {}

        class Started:
            def snapshot(self):
                return {'run_id': 'cccccccccccccccc'}
        with mock.patch.object(self.APP.Handler.discovery, 'start',
                               side_effect=lambda req, **kw: seen.update(req=req) or Started()):
            code, body = self._req('POST', f'/api/discovery/{rid}/follow-up',
                                   {'results': h['results'], 'dataset_ids': h['dataset_ids']})
        self.assertEqual(202, code, body)
        self.assertIn('my-counts', seen['req']['dataset_ids'])
        self.assertIn('platform record', seen['req']['continued_from']['results'])
        self.assertEqual('follow_up', seen['req']['continued_from']['kind'])

    def test_an_unknown_request_is_refused(self):
        rid = 'dddd111122223333'
        self._ended(rid)
        code, body = self._req('POST', f'/api/discovery/{rid}/data-request',
                               {'request_id': 'R9', 'note': 'here'})
        self.assertEqual(400, code, body)


class BriefAndPageTests(unittest.TestCase):
    def test_the_orchestrator_is_told_to_ask_rather_than_only_record(self):
        req = DISC.build(project_id='ipsc_macrophage', runtime_mode='synthetic_demo',
                         objective='increase viable macrophage yield from iPSC')
        brief = DISC.render_brief(req, loop_dir='runs/ai-x')
        self.assertIn('cli data-request --draft', brief)
        self.assertIn('a limitation nobody can act on', brief)
        self.assertIn('this run does not wait', brief)

    def test_the_page_asks_and_hands_back(self):
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertIn('id="askDataPanel"', html)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function renderDataRequests', js)
        self.assertIn('snap.data_requests', js)
        self.assertIn('Start round 2 with what I provided', js)
        self.assertIn('/data-request`', js)
        self.assertIn('never as a cited one', js)


if __name__ == '__main__':
    unittest.main()
