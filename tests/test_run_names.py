"""A run you can name, in a list you can fold.

Six runs on one objective read identically in a list — and the row showed the
hypothesis the run produced, so none of them read as what was actually typed.
A run now carries a name the person gives it, at the start or afterwards. The
name is a label on the run, never a rewrite of the request: what the agents
were asked stays what they were asked.
"""
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

from biosense import contracts as K
from biosense import workspace as WS
from biosense.production import authz as AZ
from biosense.production import budget as BU
from biosense.production import discovery as DISC
from biosense.production import run_store as RS
from biosense.production import runtime as RT


class RequestTests(unittest.TestCase):
    def test_a_name_rides_on_the_request_and_is_optional(self):
        req = DISC.build(project_id='ipsc_macrophage', runtime_mode='synthetic_demo',
                         objective='increase viable macrophage yield from iPSC',
                         title='GM-CSF swap, round 1')
        self.assertEqual('GM-CSF swap, round 1', req['title'])
        bare = DISC.build(project_id='ipsc_macrophage', runtime_mode='synthetic_demo',
                          objective='increase viable macrophage yield from iPSC')
        self.assertIsNone(bare['title'])


class RowTests(unittest.TestCase):
    def test_a_listed_run_carries_its_name(self):
        row = RS.summarise({'run_id': 'a' * 16, 'status': 'done', 'title': 'Round 2, higher DO',
                            'objective': 'increase viable macrophage yield'})
        self.assertEqual('Round 2, higher DO', row['title'])
        self.assertEqual('increase viable macrophage yield', row['objective'])
        # A run from before names existed simply has none.
        self.assertIsNone(RS.summarise({'run_id': 'b' * 16, 'status': 'done'})['title'])


class RenameTests(unittest.TestCase):
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
        RS.write_state(d, {'run_id': rid, 'status': 'done', 'is_real': True,
                           'runtime_mode': 'local_real_ai', 'owner': None,
                           'project_id': 'ipsc_macrophage', 'objective': req['objective'],
                           'run_dir': d.name, 'started_at': time.time() - 60,
                           'finished_at': time.time()})
        return d

    def test_an_ended_run_can_be_named_and_the_name_is_what_lists_show(self):
        rid = 'aaaa111122223333'
        d = self._ended(rid)
        code, body = self._req('POST', f'/api/discovery/{rid}/rename',
                               {'title': 'GM-CSF swap, round 1'})
        self.assertEqual(200, code, body)
        code, snap = self._req('GET', f'/api/discovery/{rid}')
        self.assertEqual('GM-CSF swap, round 1', snap['title'])
        code, listed = self._req('GET', '/api/discovery')
        row = next(r for r in listed['runs'] if r['run_id'] == rid)
        self.assertEqual('GM-CSF swap, round 1', row['title'])
        # The request is untouched: a name never changes what was asked.
        asked = json.loads((d / 'discovery_request.json').read_text())
        self.assertEqual('increase viable macrophage yield from iPSC', asked['objective'])
        self.assertIsNone(asked['title'])

    def test_a_name_can_be_cleared_and_is_bounded(self):
        rid = 'bbbb111122223333'
        self._ended(rid)
        self._req('POST', f'/api/discovery/{rid}/rename', {'title': 'temporary'})
        code, body = self._req('POST', f'/api/discovery/{rid}/rename', {'title': '   '})
        self.assertEqual(200, code, body)
        self.assertIsNone(body['title'])
        code, body = self._req('POST', f'/api/discovery/{rid}/rename', {'title': 'x' * 500})
        self.assertEqual(400, code, body)

    def test_renaming_a_run_that_does_not_exist_is_a_404(self):
        code, _ = self._req('POST', '/api/discovery/cccc111122223333/rename', {'title': 'n'})
        self.assertEqual(404, code)


class PageTests(unittest.TestCase):
    def test_the_form_offers_a_name_and_sends_it(self):
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertIn('id="runName"', html)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn("title: (($('#runName') && $('#runName').value)", js)

    def test_the_list_shows_the_name_not_the_hypothesis_and_folds(self):
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        start = js.index('function renderProjectRunList')
        fn = js[start:js.index('\nasync function ', start)]
        self.assertIn("r.title || r.objective || r.run_id", fn)
        self.assertNotIn('r.hypothesis || r.objective', fn,
                         'the row showed what the run found, not what was asked')
        self.assertIn("el('details', 'prun-list')", fn)
        self.assertIn('function renameRun', js)
        self.assertIn('/rename`', js)
        runs = (K.ROOT / 'webapp' / 'runs.js').read_text()
        self.assertIn('r.title || r.objective', runs)

    def test_the_row_grid_has_a_column_for_every_control(self):
        css = (K.ROOT / 'webapp' / 'brand.css').read_text()
        rule = css[css.index('.prunlist .prun{'):][:220]
        cols = rule.split('grid-template-columns:')[1].split(';')[0].strip()
        # date, status, objective, count, rename, open, delete
        self.assertEqual(7, len(cols.split()), cols)


if __name__ == '__main__':
    unittest.main()
