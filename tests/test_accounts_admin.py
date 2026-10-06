"""Accounts, roles, and the line between a demo cap and a safety limit.

BioSense keeps accounts because a scientist's projects, datasets, runs and
reports belong to them. This file is the guard on what that ownership is worth,
and on the one privilege the deployment grants beyond it.

Three claims are tested here rather than reviewed by eye:

* **A role cannot be claimed.** Not by a request field, not by a header, not by a
  cookie, not by browser state, and not by signing in against an Omnigent server
  the caller runs themselves. Admin is a server-side configuration matched
  against a server-verified identity from one trusted issuer.
* **An operator is exempt from demo policy and from nothing else.** The caps on
  what strangers may spend do not apply to them; the run timeout, the
  one-at-a-time gate, the payload limits and every data boundary do.
* **Admin is not a key to other people's science.** A quota exemption grants no
  read of anybody else's project, run or report, and the ownership checks do not
  consult role at all.

Nothing here contacts an Omnigent server or calls a model: sign-in is injected.
"""
import json
import time
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense import workspace as WS
from biosense.production import authz as AZ
from biosense.production import budget as BU

ISSUER = 'https://accounts.example.org'


def signed_in(owner, *, server=ISSUER, provider_admin=False):
    return WS.WorkspaceIdentity(owner, authenticated=True, server=server, token='tok',
                                display=owner, provider_is_admin=provider_admin)


def policy(ids=('boss@lab.example',), server=ISSUER):
    return AZ.Policy(admin_ids=ids, auth_server=server)


class PolicyTests(unittest.TestCase):
    def test_the_three_roles_and_what_each_is_called(self):
        p = policy()
        self.assertEqual(AZ.ROLE_ADMIN, p.role_for(signed_in('boss@lab.example')))
        self.assertEqual(AZ.ROLE_USER, p.role_for(signed_in('student@lab.example')))
        self.assertEqual(AZ.ROLE_PUBLIC, p.role_for(WS.local_identity()))
        self.assertEqual(AZ.ROLE_PUBLIC, p.role_for(None))
        for r in AZ.ROLES:
            self.assertTrue(AZ.LABELS[r])

    def test_an_identity_from_another_issuer_is_never_admin(self):
        """The whole reason admin is bound to an issuer. `/api/auth/login` used to
        take the server from the request body, so anyone could stand up an
        Omnigent server, return the admin's id, and be an admin here."""
        p = policy()
        self.assertFalse(p.is_admin(signed_in('boss@lab.example',
                                              server='https://attacker.example')))
        self.assertFalse(p.is_admin(signed_in('boss@lab.example', server=None)))
        self.assertTrue(p.is_admin(signed_in('boss@lab.example')))

    def test_an_unauthenticated_identity_is_never_admin_however_it_is_named(self):
        p = policy()
        claim = WS.WorkspaceIdentity('boss@lab.example', authenticated=False, server=ISSUER)
        self.assertFalse(p.is_admin(claim))

    def test_the_providers_own_admin_flag_grants_nothing_here(self):
        """Omnigent's is_admin is about Omnigent's administration. A BioSense
        operator is a different role, decided by this deployment."""
        p = policy(ids=())
        who = signed_in('someone@lab.example', provider_admin=True)
        self.assertTrue(who.provider_is_admin)
        self.assertFalse(p.is_admin(who))
        self.assertEqual(AZ.ROLE_USER, p.role_for(who))

    def test_no_admins_at_all_unless_both_halves_are_configured(self):
        self.assertFalse(AZ.Policy().has_admins)
        self.assertFalse(AZ.Policy(admin_ids=('a@b.c',)).has_admins)
        self.assertFalse(AZ.Policy(auth_server=ISSUER).has_admins)
        self.assertTrue(policy().has_admins)

    def test_naming_admins_without_a_trusted_issuer_refuses_to_start(self):
        with self.assertRaises(K.ContractError) as e:
            AZ.from_env({AZ.ENV_ADMIN_IDS: 'boss@lab.example'})
        self.assertIn(AZ.ENV_AUTH_SERVER, str(e.exception))

    def test_ids_are_matched_the_way_the_account_system_stores_them(self):
        p = AZ.from_env({AZ.ENV_ADMIN_IDS: ' Boss@Lab.Example , second@lab.example ',
                         AZ.ENV_AUTH_SERVER: ISSUER + '/'})
        self.assertTrue(p.is_admin(signed_in('boss@lab.example')))
        self.assertTrue(p.is_admin(signed_in('second@lab.example')))
        self.assertFalse(p.is_admin(signed_in('third@lab.example')))

    def test_the_public_description_never_names_an_administrator(self):
        blob = json.dumps(policy(ids=('boss@lab.example', 'x@y.z')).describe())
        self.assertNotIn('boss@lab.example', blob)
        self.assertNotIn('x@y.z', blob)
        self.assertIn('admin_configured', blob)

    def test_require_admin_refuses_with_403_and_names_no_one(self):
        p = policy()
        with self.assertRaises(AZ.Forbidden) as e:
            p.require_admin(signed_in('student@lab.example'), 'that')
        self.assertEqual(403, e.exception.status)
        self.assertEqual(AZ.ROLE_ADMIN, e.exception.required_role)
        self.assertNotIn('boss@lab.example', str(e.exception))
        self.assertIsNone(p.require_admin.__doc__ and None)
        p.require_admin(signed_in('boss@lab.example'))

    def test_credential_modes_say_who_pays_and_reserve_the_future_one(self):
        p = policy()
        self.assertEqual('admin_platform', p.credential_mode(signed_in('boss@lab.example')))
        self.assertEqual('platform_demo', p.credential_mode(signed_in('student@lab.example')))
        self.assertEqual('platform_demo', p.credential_mode(WS.local_identity()))
        # Reserved, declared, and deliberately not implemented. Adding it later
        # should be a new branch, not a migration of every run already recorded.
        self.assertIn('user_byok', AZ.CREDENTIAL_MODES)
        self.assertNotIn('user_byok', AZ.IMPLEMENTED_CREDENTIAL_MODES)


class SignInTests(unittest.TestCase):
    """What a sign-in may and may not take from the caller."""

    def test_the_account_id_comes_from_the_server_and_never_from_the_form(self):
        """A server that authenticates somebody must say who they are. Falling
        back to the typed username would let a caller choose their own id — and
        on a deployment with administrators, choose to be one."""
        with self.assertRaises(WS.AuthError) as e:
            WS.sign_in(ISSUER, 'boss@lab.example', 'pw',
                       login_fn=lambda *a: {'token': 't', 'user': {}})
        self.assertEqual('no_account_id', e.exception.reason)

    def test_a_signed_in_identity_carries_its_issuer(self):
        who = WS.sign_in(ISSUER, 'bob', 'pw',
                         login_fn=lambda *a: {'token': 't', 'user': {'id': 'bob@lab.example'}})
        self.assertEqual(ISSUER, who.server)
        self.assertEqual('bob@lab.example', who.owner)
        self.assertTrue(who.authenticated)

    def test_the_token_never_reaches_a_public_payload(self):
        who = WS.sign_in(ISSUER, 'bob', 'pw',
                         login_fn=lambda *a: {'token': 'secret-session-jwt',
                                              'user': {'id': 'bob@lab.example'}})
        self.assertNotIn('secret-session-jwt', json.dumps(who.public(role='user')))


class AnonymousWorkspaceTests(unittest.TestCase):
    """A stranger on a shared URL still gets somewhere of their own."""

    def test_two_browsers_are_two_workspaces(self):
        a, b = WS.anon_identity(WS.new_anon_cookie()), WS.anon_identity(WS.new_anon_cookie())
        self.assertNotEqual(a.owner, b.owner)
        self.assertTrue(a.owner.startswith(WS.ANON_PREFIX))
        self.assertFalse(a.authenticated)
        self.assertTrue(a.anonymous)

    def test_the_cookie_value_is_never_the_workspace_id(self):
        """The thing that grants access must not be the thing written into a
        record, a directory name or a log line."""
        value = WS.new_anon_cookie()
        who = WS.anon_identity(value)
        self.assertNotIn(value, who.owner)
        self.assertNotIn(value, WS.owner_dir_name(who.owner))
        self.assertNotIn(value, json.dumps(who.public(role='public')))

    def test_the_same_cookie_is_the_same_workspace(self):
        value = WS.new_anon_cookie()
        self.assertEqual(WS.anon_identity(value).owner, WS.anon_identity(value).owner)


class DemoPolicyVersusSafetyTests(unittest.TestCase):
    """The distinction the whole admin feature turns on."""

    def test_an_operator_is_exempt_from_every_demo_cap(self):
        clock = [1_000_000.0]
        led = BU.Ledger(BU.Limits(public=True, per_client=2, daily=3, cooldown_s=60),
                        now=lambda: clock[0])
        for _ in range(25):
            led.authorise('boss-bucket', role=AZ.ROLE_ADMIN)
        self.assertEqual(25, led.state()['admin_real_runs_today'])

    def test_an_ordinary_account_is_not(self):
        clock = [1_000_000.0]
        led = BU.Ledger(BU.Limits(public=True, per_client=2, daily=9, cooldown_s=0),
                        now=lambda: clock[0])
        led.authorise('bucket', role=AZ.ROLE_USER)
        led.authorise('bucket', role=AZ.ROLE_USER)
        with self.assertRaises(BU.BudgetExceeded) as e:
            led.authorise('bucket', role=AZ.ROLE_USER)
        self.assertEqual('per_client', e.exception.limit)

    def test_the_cooldown_applies_to_a_user_and_not_to_an_operator(self):
        clock = [1_000_000.0]
        led = BU.Ledger(BU.Limits(public=True, cooldown_s=60), now=lambda: clock[0])
        led.authorise('u', role=AZ.ROLE_USER)
        with self.assertRaises(BU.BudgetExceeded) as e:
            led.authorise('u', role=AZ.ROLE_USER)
        self.assertEqual('cooldown', e.exception.limit)
        led.authorise('a', role=AZ.ROLE_ADMIN)
        led.authorise('a', role=AZ.ROLE_ADMIN)

    def test_the_deployment_cap_does_not_stop_an_operator(self):
        led = BU.Ledger(BU.Limits(public=True, daily=1, cooldown_s=0))
        led.authorise('someone', role=AZ.ROLE_USER)
        with self.assertRaises(BU.BudgetExceeded):
            led.authorise('other', role=AZ.ROLE_USER)
        led.authorise('boss', role=AZ.ROLE_ADMIN)

    def test_operator_runs_are_counted_rather_than_made_invisible(self):
        """They are not free. A deployment should be able to see how much of its
        own spend was its own development."""
        led = BU.Ledger(BU.Limits(public=True, cooldown_s=0))
        led.authorise('u', role=AZ.ROLE_USER)
        led.authorise('a', role=AZ.ROLE_ADMIN)
        st = led.state()
        self.assertEqual(1, st['public_real_runs_today'])
        self.assertEqual(1, st['admin_real_runs_today'])

    def test_an_operator_allowance_says_the_runs_still_cost_money(self):
        a = BU.Ledger(BU.Limits(public=True)).allowance('x', role=AZ.ROLE_ADMIN)
        self.assertFalse(a['capped'])
        self.assertIn('credits', a['note'])

    def test_a_synthetic_run_spends_no_allowance_for_anybody(self):
        led = BU.Ledger(BU.Limits(public=True, per_client=1, cooldown_s=60))
        for role in (AZ.ROLE_PUBLIC, AZ.ROLE_USER, AZ.ROLE_ADMIN):
            for _ in range(20):
                led.authorise('x', is_real=False, role=role)
        self.assertEqual(0, led.state()['real_runs_last_24h'])

    def test_the_safety_limits_are_not_in_the_budget_module_at_all(self):
        """The ones an operator must still obey live where no role can reach
        them: the concurrency gate, the prompt and body caps, the stage map."""
        import inspect
        from biosense.production import app as APP
        src = inspect.getsource(APP.DiscoveryRegistry.start)
        self.assertIn('MAX_CONCURRENT_REAL', src)
        # the gate is inside the lock, after the budget, and reads no role
        gate = src.split('MAX_CONCURRENT_REAL')[0].rsplit('with self.lock', 1)[-1]
        self.assertNotIn('role', gate)
        self.assertTrue(APP.MAX_PROMPT and APP.MAX_BODY_DISCOVERY)

    def test_the_deadline_applies_to_every_real_run_including_an_operators(self):
        """A run nobody can stop is a bill nobody can stop."""
        import inspect
        from biosense.production import app as APP
        src = inspect.getsource(APP.DiscoveryRegistry.start)
        deadline = src[src.index('deadline='):src.index('deadline=') + 220]
        self.assertIn('target.is_real', deadline)
        self.assertNotIn('role', deadline)


class HttpRoleTests(unittest.TestCase):
    """The HTTP surface: what a browser can and cannot talk itself into.

    A live server on a loopback port, a synthetic-only runtime, two signed-in
    accounts and one stranger. No Omnigent server is contacted: the sessions are
    created directly, which is what the real sign-in would have produced.
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        import tempfile
        import threading
        from http.server import ThreadingHTTPServer
        from biosense.production import app as APP
        from biosense.production import runtime as RT
        cls.APP = APP
        cls.shutil = shutil
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / 'runs').mkdir()
        cfg = RT.from_env(runs_dir=cls.tmp / 'runs', env={'BIOSENSE_HOSTED': '1'})
        limits = BU.from_env({'BIOSENSE_PUBLIC_DEMO': '1'})
        pol = policy()
        APP.Handler.runs_dir = cls.tmp / 'runs'
        APP.Handler.static_dir = K.ROOT / 'webapp'
        APP.Handler.registry = APP.Registry(cls.tmp / 'runs')
        APP.Handler.runtime_cfg = cfg
        APP.Handler.limits = limits
        APP.Handler.policy = pol
        APP.Handler.discovery = APP.DiscoveryRegistry(cls.tmp / 'runs', cfg, limits, pol)
        APP.Handler.sessions = WS.SessionStore()
        APP.Handler.default_identity = WS.local_identity()
        APP.Handler._ready = {'at': 0.0, 'body': None, 'code': 503}
        cls.admin_sid = APP.Handler.sessions.create(signed_in('boss@lab.example'))
        cls.user_sid = APP.Handler.sessions.create(signed_in('student@lab.example'))
        cls.other_sid = APP.Handler.sessions.create(signed_in('rival@lab.example'))
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), APP.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.shutil.rmtree(cls.tmp, ignore_errors=True)

    def _req(self, method, path, body=None, *, sid=None, headers=None):
        import urllib.error
        import urllib.request
        h = {'Content-Type': 'application/json', 'X-Forwarded-For': '203.0.113.7'}
        if sid:
            h['Cookie'] = f'{WS.SESSION_COOKIE}={sid}'
        h.update(headers or {})
        r = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}', method=method,
            data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, json.loads(resp.read() or b'{}'), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}'), e.headers

    def _run(self, sid, objective='Increase viable macrophage yield while keeping identity.'):
        code, d, _ = self._req('POST', '/api/discovery', {
            'project_id': 'ipsc_macrophage', 'objective': objective,
            'runtime_mode': 'synthetic_demo'}, sid=sid)
        self.assertEqual(202, code, d)
        rid = d['run_id']
        for _ in range(240):
            snap = self._req('GET', f'/api/discovery/{rid}', sid=sid)[1]
            if snap['status'] not in ('queued', 'running', 'finalizing'):
                break
            time.sleep(0.25)
        return rid

    # ── identity ────────────────────────────────────────────
    def test_identity_names_the_role_and_carries_no_secret(self):
        code, d, _ = self._req('GET', '/api/identity', sid=self.admin_sid)
        self.assertEqual(200, code)
        self.assertEqual('admin', d['role'])
        self.assertTrue(d['is_admin'])
        self.assertTrue(d['authenticated'])
        self.assertEqual('admin_platform', d['credential_mode'])
        blob = json.dumps(d)
        for secret in ('tok', 'ANTHROPIC', 'password', self.admin_sid):
            self.assertNotIn(secret, blob)

    def test_an_ordinary_account_is_a_user_and_a_stranger_is_public(self):
        self.assertEqual('user', self._req('GET', '/api/identity', sid=self.user_sid)[1]['role'])
        anon = self._req('GET', '/api/identity')[1]
        self.assertEqual('public', anon['role'])
        self.assertFalse(anon['is_admin'])

    def test_a_browser_cannot_declare_itself_an_operator(self):
        """Not with a header, not with a cookie, not with a body field. The role
        is computed from configuration the request cannot reach."""
        tries = [
            {'headers': {'X-Biosense-Role': 'admin'}},
            {'headers': {'X-Admin': 'true'}},
            {'headers': {'Cookie': 'biosense_role=admin; is_admin=1'}},
        ]
        for t in tries:
            code, d, _ = self._req('GET', '/api/identity', **t)
            self.assertEqual(200, code)
            self.assertFalse(d['is_admin'], t)
            self.assertEqual('public', d['role'], t)
        # and not by asking for it in the body of a run either
        code, d, _ = self._req('POST', '/api/discovery', {
            'project_id': 'ipsc_macrophage', 'objective': 'x' * 40,
            'runtime_mode': 'synthetic_demo', 'role': 'admin', 'is_admin': True,
            'credential_mode': 'admin_platform'}, sid=self.user_sid)
        self.assertEqual(202, code)
        self.assertFalse(d['started_by_admin'])
        self.assertEqual('platform_demo', d['credential_mode'])

    def test_signing_in_against_another_server_is_refused_where_an_issuer_is_set(self):
        code, d, _ = self._req('POST', '/api/auth/login', {
            'server': 'https://attacker.example', 'username': 'boss@lab.example',
            'password': 'x'})
        self.assertEqual(400, code)
        self.assertIn('one configured accounts server', d['error'])

    # ── ownership ───────────────────────────────────────────
    def test_a_run_belongs_to_the_account_that_started_it(self):
        rid = self._run(self.user_sid)
        mine = self._req('GET', f'/api/discovery/{rid}', sid=self.user_sid)
        self.assertEqual(200, mine[0])
        self.assertEqual('student@lab.example', mine[1]['owner'])

    def test_another_account_cannot_read_a_run_by_guessing_its_id(self):
        rid = self._run(self.user_sid)
        for sid in (self.other_sid, None):
            self.assertEqual(404, self._req('GET', f'/api/discovery/{rid}', sid=sid)[0])
            self.assertEqual(404, self._req('GET', f'/api/discovery/{rid}/protocol',
                                            sid=sid)[0])
            self.assertEqual(404, self._req('POST', f'/api/discovery/{rid}/cancel',
                                            sid=sid)[0])
            self.assertEqual(404, self._req('POST', f'/api/discovery/{rid}/benchmark',
                                            sid=sid)[0])

    def test_an_operator_is_not_given_other_peoples_science(self):
        """A quota exemption is not a key. If an administrative view over other
        accounts' data is ever wanted it should be added deliberately."""
        rid = self._run(self.user_sid)
        self.assertEqual(404, self._req('GET', f'/api/discovery/{rid}',
                                        sid=self.admin_sid)[0])

    def test_the_run_list_holds_only_the_callers_own_runs(self):
        rid = self._run(self.user_sid)
        for sid in (self.other_sid, self.admin_sid):
            listed = self._req('GET', '/api/discovery', sid=sid)[1]['runs']
            self.assertNotIn(rid, [r['run_id'] for r in listed])
        own = self._req('GET', '/api/discovery', sid=self.user_sid)[1]['runs']
        self.assertIn(rid, [r['run_id'] for r in own])

    def test_a_restored_run_keeps_its_owner(self):
        """The check must survive the process forgetting the run, or a reload
        would be a way around it."""
        rid = self._run(self.user_sid)
        self.APP.Handler.discovery.runs.clear()
        self.assertEqual(200, self._req('GET', f'/api/discovery/{rid}',
                                        sid=self.user_sid)[0])
        self.assertEqual(404, self._req('GET', f'/api/discovery/{rid}',
                                        sid=self.other_sid)[0])

    def test_two_browsers_with_no_account_do_not_share_a_run_list(self):
        code, d, headers = self._req('POST', '/api/discovery', {
            'project_id': 'ipsc_macrophage', 'objective': 'x' * 40,
            'runtime_mode': 'synthetic_demo'})
        self.assertEqual(202, code)
        cookie = headers.get('Set-Cookie') or ''
        self.assertIn(WS.ANON_COOKIE, cookie, 'a stranger got no workspace of their own')
        value = cookie.split(f'{WS.ANON_COOKIE}=')[1].split(';')[0]
        theirs = self._req('GET', '/api/discovery',
                           headers={'Cookie': f'{WS.ANON_COOKIE}={value}'})[1]['runs']
        self.assertIn(d['run_id'], [r['run_id'] for r in theirs])
        somebody_else = self._req('GET', '/api/discovery',
                                  headers={'Cookie': f'{WS.ANON_COOKIE}=something-else'})[1]['runs']
        self.assertNotIn(d['run_id'], [r['run_id'] for r in somebody_else])

    def test_a_project_id_cannot_walk_out_of_a_workspace(self):
        """A project id is a name, not a path. Before this it reached the
        template loader unchecked, which could read another account's project."""
        import os
        from biosense.production import project_builder as PB
        victim = signed_in('rival@lab.example')
        d = WS.projects_dir(victim, create=True)
        doc = K.read_json(K.ROOT / 'projects' / 'ipsc_macrophage.json')
        doc['project_id'], doc['name'] = 'victim_secret', 'VICTIM SECRET'
        K.write_json_atomic(d / 'victim_secret.json', doc)
        escape = os.path.relpath(str(d / 'victim_secret'), str(K.ROOT / 'projects'))
        with self.assertRaises(K.ContractError):
            PB.load_for(signed_in('student@lab.example'), escape)
        code, _, _ = self._req('POST', '/api/discovery/preview',
                               {'project_id': escape, 'objective': 'x' * 40},
                               sid=self.user_sid)
        self.assertEqual(400, code)


if __name__ == '__main__':
    unittest.main()
