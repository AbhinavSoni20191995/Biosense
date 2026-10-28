"""Accounts this instance keeps itself: sign-up, sign-in, and five runs each.

A lab wants its people to open the page, make an account and try the system.
These tests hold the shape of that: a password is never written, an account
is its own workspace, a real AI run spends the account's allowance and nothing
else does, and the server — never the page — decides every one of those.

Nothing here contacts an Omnigent server or calls a model.
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from biosense import accounts as AC
from biosense import contracts as K
from biosense import workspace as WS
from biosense.production import authz as AZ
from biosense.production import budget as BU


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = AC.Store(self.tmp / 'accounts.json', AC.Settings(run_limit=5))

    def test_an_account_is_created_signed_in_and_signs_in_again(self):
        who = self.store.create('Alice@Lab.Example', 'correct horse battery')
        self.assertTrue(who.authenticated)
        self.assertEqual('alice@lab.example', who.owner, 'normalised like a workspace')
        self.assertEqual(AC.ISSUER, who.server, 'this instance is the issuer')
        again = self.store.sign_in('alice@lab.example', 'correct horse battery')
        self.assertEqual(who.owner, again.owner)
        self.assertTrue(again.authenticated)

    def test_the_password_is_never_written(self):
        self.store.create('alice@lab.example', 'correct horse battery')
        blob = (self.tmp / 'accounts.json').read_text()
        self.assertNotIn('correct horse battery', blob)
        self.assertIn('scrypt', blob)
        self.assertEqual(0o600, (self.tmp / 'accounts.json').stat().st_mode & 0o777)

    def test_a_wrong_password_and_an_unknown_account_read_the_same(self):
        self.store.create('alice@lab.example', 'correct horse battery')
        with self.assertRaises(WS.AuthError) as a:
            self.store.sign_in('alice@lab.example', 'wrong')
        with self.assertRaises(WS.AuthError) as b:
            self.store.sign_in('nobody@lab.example', 'wrong')
        self.assertEqual(str(a.exception), str(b.exception))
        self.assertEqual(401, a.exception.status)

    def test_what_sign_up_refuses(self):
        with self.assertRaises(WS.AuthError) as e:
            self.store.create('not-an-email', 'correct horse battery')
        self.assertEqual('bad_email', e.exception.reason)
        with self.assertRaises(WS.AuthError) as e:
            self.store.create('alice@lab.example', 'short')
        self.assertEqual('weak_password', e.exception.reason)
        self.store.create('alice@lab.example', 'correct horse battery')
        with self.assertRaises(WS.AuthError) as e:
            self.store.create('ALICE@lab.example', 'another password')
        self.assertEqual(409, e.exception.status)

    def test_sign_up_can_be_closed_or_put_behind_a_code(self):
        closed = AC.Store(self.tmp / 'a.json', AC.Settings(signup='closed'))
        with self.assertRaises(WS.AuthError) as e:
            closed.create('alice@lab.example', 'correct horse battery')
        self.assertEqual('signup_closed', e.exception.reason)
        coded = AC.Store(self.tmp / 'b.json', AC.Settings(signup_code='lab-2026'))
        with self.assertRaises(WS.AuthError) as e:
            coded.create('alice@lab.example', 'correct horse battery', signup_code='nope')
        self.assertEqual('signup_code', e.exception.reason)
        coded.create('alice@lab.example', 'correct horse battery', signup_code='lab-2026')
        self.assertNotIn('lab-2026', json.dumps(coded.settings.describe()))

    def test_five_real_runs_a_day_and_then_a_refusal_that_says_when(self):
        clock = {'t': 1_000_000.0}
        store = AC.Store(self.tmp / 'daily.json', AC.Settings(run_limit=5), now=lambda: clock['t'])
        store.create('alice@lab.example', 'correct horse battery')
        for n in range(5):
            self.assertEqual(5 - n, store.allowance('alice@lab.example')['runs_remaining_today'])
            store.authorise_run('alice@lab.example')
            clock['t'] += 600
        self.assertEqual(0, store.allowance('alice@lab.example')['runs_remaining_today'])
        with self.assertRaises(AC.RunsExhausted) as e:
            store.authorise_run('alice@lab.example')
        self.assertEqual((5, 5), (e.exception.used, e.exception.run_limit))
        self.assertIn('daily allowance is 5', str(e.exception))
        self.assertIn('ask for extra runs', str(e.exception))
        # The oldest run was 50 minutes ago, so the next opens in 23 h 10 min.
        self.assertAlmostEqual(AC.DAY_S - 3000, e.exception.retry_after_s, delta=2)
        self.assertIn('23 h 10 min', str(e.exception))
        # A day later the allowance is back, without anybody granting anything.
        clock['t'] += AC.DAY_S
        self.assertEqual(5, store.allowance('alice@lab.example')['runs_remaining_today'])
        store.authorise_run('alice@lab.example')
        # An operator raises the daily limit; a limit of 0 means none.
        store.set_run_limit('alice@lab.example', 7)
        self.assertEqual(6, store.allowance('alice@lab.example')['runs_remaining_today'])
        store.set_run_limit('alice@lab.example', 0)
        self.assertFalse(store.allowance('alice@lab.example')['capped'])

    def test_extra_runs_are_spent_only_once_the_days_are_gone(self):
        clock = {'t': 1_000_000.0}
        store = AC.Store(self.tmp / 'extra.json', AC.Settings(run_limit=2), now=lambda: clock['t'])
        store.create('alice@lab.example', 'correct horse battery')
        store.grant_runs('alice@lab.example', 3, granted_by='boss')
        self.assertEqual('day', store.authorise_run('alice@lab.example')['spent'])
        self.assertEqual('day', store.authorise_run('alice@lab.example')['spent'])
        a = store.allowance('alice@lab.example')
        self.assertEqual((0, 3), (a['runs_remaining_today'], a['extra_runs']))
        self.assertIn('plus 3 extra', a['note'])
        self.assertEqual('extra', store.authorise_run('alice@lab.example')['spent'])
        self.assertEqual(2, store.allowance('alice@lab.example')['extra_runs'])
        # Tomorrow the day's two come first again, and the bank waits.
        clock['t'] += AC.DAY_S
        self.assertEqual('day', store.authorise_run('alice@lab.example')['spent'])
        self.assertEqual(2, store.allowance('alice@lab.example')['extra_runs'])

    def test_a_request_for_more_runs_waits_for_an_operator_and_then_counts(self):
        self.store.create('alice@lab.example', 'correct horse battery')
        with self.assertRaisesRegex(K.ContractError, 'what the extra runs are for'):
            self.store.request_runs('alice@lab.example', 5, 'more')
        req = self.store.request_runs('alice@lab.example', 5,
                                      'Two more arms for the Friday round.')
        self.assertEqual('open', req['status'])
        with self.assertRaisesRegex(K.ContractError, 'already have a request open'):
            self.store.request_runs('alice@lab.example', 2, 'And one more for luck.')
        self.assertEqual(req['request_id'],
                         self.store.get('alice@lab.example')['open_request']['request_id'])
        self.assertEqual([req['request_id']],
                         [r['request_id'] for r in self.store.requests(open_only=True)])
        done = self.store.decide_request(req['request_id'], grant=True, decided_by='boss',
                                         runs=3, note='three is plenty')
        self.assertEqual(('granted', 3, 'boss'),
                         (done['status'], done['granted_runs'], done['decided_by']))
        self.assertEqual(3, self.store.get('alice@lab.example')['extra_runs'])
        self.assertIsNone(self.store.get('alice@lab.example')['open_request'])
        with self.assertRaisesRegex(K.ContractError, 'already granted'):
            self.store.decide_request(req['request_id'], grant=False, decided_by='boss')
        # Declining adds nothing.
        req2 = self.store.request_runs('alice@lab.example', 50, 'Everything, please, today.')
        self.store.decide_request(req2['request_id'], grant=False, decided_by='boss',
                                  note='ask again next week')
        self.assertEqual(3, self.store.get('alice@lab.example')['extra_runs'])

    def test_a_disabled_account_can_neither_sign_in_nor_run(self):
        self.store.create('alice@lab.example', 'correct horse battery')
        self.store.set_disabled('alice@lab.example', True)
        with self.assertRaises(WS.AuthError):
            self.store.sign_in('alice@lab.example', 'correct horse battery')
        with self.assertRaises(WS.AuthError):
            self.store.authorise_run('alice@lab.example')

    def test_the_listing_carries_counts_and_no_hash(self):
        self.store.create('alice@lab.example', 'correct horse battery')
        self.store.authorise_run('alice@lab.example')
        rows = self.store.listing()
        self.assertEqual([('alice@lab.example', 1, 5, 4)],
                         [(r['email'], r['runs_used_today'], r['run_limit'],
                           r['runs_remaining_today']) for r in rows])
        self.assertNotIn('hash', json.dumps(rows))
        self.assertNotIn('salt', json.dumps(rows))

    def test_the_cli_creates_lists_and_grants(self):
        import contextlib
        import io
        env = {'BIOSENSE_PRIVATE_DATA': str(self.tmp / 'private')}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                AC.main(['create', 'bob@lab.example', 'correct horse battery'])
                AC.main(['grant', 'bob@lab.example', '9'])
                AC.main(['limit', 'bob@lab.example', '7'])
                AC.main(['list'])
            self.assertIn('"extra_runs": 9', out.getvalue())
            self.assertIn('"run_limit": 7', out.getvalue())
            self.assertTrue((self.tmp / 'private' / 'accounts' / AC.FILE).is_file())
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_settings_come_from_the_environment(self):
        s = AC.settings_from_env({})
        self.assertTrue(s.enabled and s.signup_open)
        self.assertEqual(5, s.run_limit)
        s = AC.settings_from_env({'BIOSENSE_RUNS_PER_ACCOUNT': '12', 'BIOSENSE_SIGNUP': 'closed'})
        self.assertEqual((12, False), (s.run_limit, s.signup_open))
        self.assertFalse(AC.settings_from_env({'BIOSENSE_ACCOUNTS': 'off'}).enabled)
        with self.assertRaises(K.ContractError):
            AC.settings_from_env({'BIOSENSE_RUNS_PER_ACCOUNT': 'five'})


class LedgerTests(unittest.TestCase):
    def test_an_account_skips_the_per_address_bucket_but_not_the_deployment_cap(self):
        lim = BU.Limits(public=True, daily=2, per_client=1, cooldown_s=60)
        led = BU.Ledger(lim, now=lambda: 1000.0)
        led.authorise('c1', role='user', per_client_exempt=True)
        led.authorise('c1', role='user', per_client_exempt=True)   # no cooldown, no per-client
        with self.assertRaises(BU.BudgetExceeded) as e:
            led.authorise('c1', role='user', per_client_exempt=True)
        self.assertEqual('daily', e.exception.limit)
        self.assertEqual(0, led.state()['callers_last_24h'], 'nothing counted by address')


class HttpAccountTests(unittest.TestCase):
    """Through the server: a person signs up, is somebody, and has runs."""

    @classmethod
    def setUpClass(cls):
        from biosense.production import app as APP
        from biosense.production import runtime as RT
        cls.APP = APP
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / 'runs').mkdir()
        cls._private = os.environ.get('BIOSENSE_PRIVATE_DATA')
        os.environ['BIOSENSE_PRIVATE_DATA'] = str(cls.tmp / 'private')
        cfg = RT.from_env(runs_dir=cls.tmp / 'runs', env={'BIOSENSE_HOSTED': '1'})
        limits = BU.from_env({'BIOSENSE_PUBLIC_DEMO': '1'})
        pol = AZ.from_env({AZ.ENV_ADMIN_IDS: 'boss@lab.example'})
        store = AC.Store(cls.tmp / 'accounts.json', AC.Settings(run_limit=5))
        APP.Handler.runs_dir = cls.tmp / 'runs'
        APP.Handler.static_dir = K.ROOT / 'webapp'
        APP.Handler.registry = APP.Registry(cls.tmp / 'runs')
        APP.Handler.runtime_cfg = cfg
        APP.Handler.limits = limits
        APP.Handler.policy = pol
        APP.Handler.accounts = store
        APP.Handler.discovery = APP.DiscoveryRegistry(cls.tmp / 'runs', cfg, limits, pol,
                                                      accounts=store)
        APP.Handler.sessions = WS.SessionStore()
        APP.Handler.default_identity = WS.local_identity()
        APP.Handler._ready = {'at': 0.0, 'body': None, 'code': 503}
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), APP.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.APP.Handler.accounts = None
        if cls._private is None:
            os.environ.pop('BIOSENSE_PRIVATE_DATA', None)
        else:
            os.environ['BIOSENSE_PRIVATE_DATA'] = cls._private
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _req(self, method, path, body=None, *, cookie=None):
        import urllib.error
        import urllib.request
        h = {'Content-Type': 'application/json', 'X-Forwarded-For': '203.0.113.9'}
        if cookie:
            h['Cookie'] = cookie
        r = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}', method=method,
            data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, json.loads(resp.read() or b'{}'), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}'), e.headers

    @staticmethod
    def _session(headers):
        for part in headers.get_all('Set-Cookie') or []:
            if part.startswith(WS.SESSION_COOKIE + '='):
                return part.split(';')[0]
        return None

    def test_sign_up_is_sign_in_and_the_page_learns_the_allowance(self):
        code, d, h = self._req('POST', '/api/auth/signup',
                               {'email': 'Carol@Lab.Example', 'password': 'correct horse battery'})
        self.assertEqual(200, code, d)
        self.assertTrue(d['created'])
        cookie = self._session(h)
        self.assertTrue(cookie)
        self.assertIn('HttpOnly', ' '.join(h.get_all('Set-Cookie')))
        code, me, _ = self._req('GET', '/api/identity', cookie=cookie)
        self.assertEqual('user', me['role'])
        self.assertEqual('carol@lab.example', me['owner'])
        self.assertEqual({'kind': 'account', 'runs_limit': 5, 'runs_used_today': 0,
                          'runs_remaining_today': 5, 'extra_runs': 0, 'per': 'day'},
                         {k: me['allowance'][k] for k in ('kind', 'runs_limit', 'runs_used_today',
                                                          'runs_remaining_today', 'extra_runs',
                                                          'per')})
        self.assertTrue(me['accounts']['signup_open'])
        blob = json.dumps(me)
        for secret in ('correct horse battery', 'hash', 'salt'):
            self.assertNotIn(secret, blob)

    def test_sign_in_needs_the_right_password_and_ignores_any_server_field(self):
        self._req('POST', '/api/auth/signup', {'email': 'dave@lab.example',
                                              'password': 'correct horse battery'})
        code, d, _ = self._req('POST', '/api/auth/login', {'email': 'dave@lab.example',
                                                            'password': 'wrong'})
        self.assertEqual(401, code)
        self.assertEqual('invalid_credentials', d['reason'])
        code, d, h = self._req('POST', '/api/auth/login', {
            'server': 'https://attacker.example', 'username': 'dave@lab.example',
            'password': 'correct horse battery'})
        self.assertEqual(200, code, d)
        self.assertEqual(AC.ISSUER, d['identity']['server'])
        code, d, _ = self._req('POST', '/api/auth/logout', cookie=self._session(h))
        self.assertEqual('public', d['identity']['role'])

    def test_a_second_account_with_the_same_email_is_refused(self):
        self._req('POST', '/api/auth/signup', {'email': 'erin@lab.example',
                                              'password': 'correct horse battery'})
        code, d, _ = self._req('POST', '/api/auth/signup', {'email': 'erin@lab.example',
                                                             'password': 'another one'})
        self.assertEqual(409, code)
        self.assertEqual('exists', d['reason'])

    def test_a_listed_id_is_an_operator_once_it_signs_up_here(self):
        code, d, h = self._req('POST', '/api/auth/signup',
                               {'email': 'boss@lab.example', 'password': 'correct horse battery'})
        self.assertEqual(200, code, d)
        self.assertTrue(d['identity']['is_admin'])
        me = self._req('GET', '/api/identity', cookie=self._session(h))[1]
        self.assertEqual('admin', me['role'])
        self.assertFalse(me['allowance']['capped'])

    def test_the_demonstration_path_spends_no_allowance(self):
        code, d, h = self._req('POST', '/api/auth/signup',
                               {'email': 'frank@lab.example', 'password': 'correct horse battery'})
        cookie = self._session(h)
        code, d, _ = self._req('POST', '/api/discovery', {
            'project_id': 'ipsc_macrophage', 'runtime_mode': 'synthetic_demo',
            'objective': 'Increase viable macrophage yield while keeping identity.'},
            cookie=cookie)
        self.assertEqual(202, code, d)
        rid = d['run_id']
        for _ in range(240):
            snap = self._req('GET', f'/api/discovery/{rid}', cookie=cookie)[1]
            if snap['status'] not in ('queued', 'running', 'finalizing'):
                break
            time.sleep(0.25)
        me = self._req('GET', '/api/identity', cookie=cookie)[1]
        self.assertEqual(5, me['allowance']['runs_remaining_today'])

    def test_a_person_asks_for_more_runs_and_the_operator_grants_them_on_the_page(self):
        code, d, h = self._req('POST', '/api/auth/signup',
                               {'email': 'hana@lab.example', 'password': 'correct horse battery'})
        user = self._session(h)
        # The operator's account may already exist from another test; either way in.
        code, d, h = self._req('POST', '/api/auth/signup',
                               {'email': 'boss@lab.example', 'password': 'correct horse battery'})
        if code == 409:
            code, d, h = self._req('POST', '/api/auth/login',
                                   {'email': 'boss@lab.example', 'password': 'correct horse battery'})
        boss = self._session(h)
        # A stranger cannot ask, and an ordinary account cannot grant.
        self.assertEqual(401, self._req('POST', '/api/account/request-runs',
                                        {'runs': 3, 'reason': 'x' * 20})[0])
        self.assertEqual(403, self._req('GET', '/api/admin/accounts', cookie=user)[0])
        # The ask.
        code, d, _ = self._req('POST', '/api/account/request-runs',
                               {'runs': 3, 'reason': 'A second round before the group meeting.'},
                               cookie=user)
        self.assertEqual(201, code, d)
        rid = d['request']['request_id']
        self.assertEqual(rid, d['identity']['allowance']['open_request']['request_id'])
        self.assertEqual(1, self._req('GET', '/api/identity', cookie=boss)[1]['open_requests'])
        # What the operator sees: accounts and counts, never a hash, never a run.
        code, d, _ = self._req('GET', '/api/admin/accounts', cookie=boss)
        self.assertEqual(200, code)
        self.assertEqual([rid], [r['request_id'] for r in d['requests']])
        self.assertIn('hana@lab.example', [a['email'] for a in d['accounts']])
        blob = json.dumps(d)
        for secret in ('hash', 'salt', 'correct horse battery'):
            self.assertNotIn(secret, blob)
        # The grant, and what the person now has.
        code, d, _ = self._req('POST', f'/api/admin/requests/{rid}/grant',
                               {'runs': 2, 'note': 'two for now'}, cookie=boss)
        self.assertEqual(200, code, d)
        self.assertEqual('granted', d['request']['status'])
        self.assertEqual('boss@lab.example', d['request']['decided_by'])
        me = self._req('GET', '/api/identity', cookie=user)[1]
        self.assertEqual(2, me['allowance']['extra_runs'])
        self.assertIsNone(me['allowance']['open_request'])
        mine = self._req('GET', '/api/account/requests', cookie=user)[1]['requests']
        self.assertEqual(['granted'], [r['status'] for r in mine])
        # A direct grant and a new daily limit, with no request behind them.
        code, d, _ = self._req('POST', '/api/admin/accounts/hana@lab.example/grant',
                               {'runs': 1}, cookie=boss)
        self.assertEqual(3, d['account']['extra_runs'])
        code, d, _ = self._req('POST', '/api/admin/accounts/hana@lab.example/limit',
                               {'runs': 8}, cookie=boss)
        self.assertEqual(8, d['account']['run_limit'])
        self.assertEqual(8, self._req('GET', '/api/identity', cookie=user)[1]['allowance']['runs_limit'])

    def test_the_runtime_payload_says_how_accounts_work_here(self):
        code, d, _ = self._req('GET', '/api/runtime')
        self.assertEqual(200, code)
        self.assertEqual(5, d['accounts']['run_limit'])
        self.assertTrue(d['identity']['sign_in_available'])
        self.assertIn('Create an account', d['identity']['note'])


class RealRunGateTests(unittest.TestCase):
    """What a real AI run needs: an account on a hosted instance, and runs left."""

    def setUp(self):
        from biosense.production import app as APP
        from biosense.production import runtime as RT
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        cfg = RT.from_env(runs_dir=self.tmp, env={'BIOSENSE_HOSTED': '1',
                                                  'BIOSENSE_RUNTIME_MODE': 'local'})
        self.store = AC.Store(self.tmp / 'accounts.json', AC.Settings(run_limit=1))
        self.reg = APP.DiscoveryRegistry(self.tmp, cfg, BU.Limits(),
                                         AZ.Policy(admin_ids=('boss@lab.example',),
                                                   auth_server=AC.ISSUER),
                                         accounts=self.store)
        self.req = {'runtime_mode': 'local_real_ai', 'project_id': 'ipsc_macrophage',
                    'objective': 'x' * 40}

    def test_a_stranger_is_told_to_sign_in_before_the_runtime_is_even_probed(self):
        with self.assertRaises(WS.AuthError) as e:
            self.reg.start(self.req, identity=WS.anon_identity('c'), client='c')
        self.assertEqual('account_required', e.exception.reason)
        self.assertEqual(401, e.exception.status)
        self.assertIn('create an account', str(e.exception))

    def test_an_account_with_no_runs_left_is_refused_by_name_and_spends_nothing(self):
        who = self.store.create('gina@lab.example', 'correct horse battery')
        self.store.authorise_run('gina@lab.example')
        # The runtime is not running here, so the probe refuses first — that
        # proves the allowance is only spent once everything else has passed.
        from biosense.production import runtime as RT
        with self.assertRaises((RT.RuntimeUnavailable, K.ContractError)) as e:
            self.reg.start(self.req, identity=who, client='c')
        self.assertNotIsInstance(e.exception, AC.RunsExhausted)
        self.assertEqual(0, self.store.allowance('gina@lab.example')['runs_remaining_today'])
        self.assertEqual(1, self.store.get('gina@lab.example')['runs_used'],
                         'a refused start costs nothing')
