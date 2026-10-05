"""The runtime abstraction, and the substitution it must never make.

The single most dangerous thing this product could do is answer a question with a
synthetic run and let somebody believe a real one happened. Most of this file
exists to make that failure impossible to introduce by accident: there is no
fallback path, the modes are distinct objects, and a real runtime that cannot
start says which of the named reasons applies.

Nothing here contacts a server. The Omnigent client is mocked, because no test
may require live AI.
"""
import json
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense import glossary as GL
from biosense.production import runtime as RT


class ModeTests(unittest.TestCase):
    def test_the_three_modes_are_distinct_and_labelled(self):
        self.assertEqual(('synthetic_demo', 'local_real_ai', 'remote_real_ai'), RT.MODES)
        labels = {RT.LABELS[m] for m in RT.MODES}
        self.assertEqual(3, len(labels), 'two runtimes share a badge')
        self.assertIn('SYNTHETIC', RT.LABELS['synthetic_demo'])
        for m in RT.REAL_MODES:
            self.assertIn('REAL AI', RT.LABELS[m])

    def test_spellings_resolve_and_nonsense_is_refused(self):
        for raw, want in (('local', 'local_real_ai'), ('REMOTE', 'remote_real_ai'),
                          ('synthetic', 'synthetic_demo'), ('real_local', 'local_real_ai')):
            self.assertEqual(want, RT.normalise_mode(raw))
        with self.assertRaises(K.ContractError):
            RT.normalise_mode('probably_real')

    def test_a_synthetic_config_is_never_real(self):
        cfg = RT.from_env(env={})
        self.assertEqual('synthetic_demo', cfg.mode)
        self.assertFalse(cfg.is_real)
        self.assertIsNone(cfg.server)


class ConfigTests(unittest.TestCase):
    def test_local_defaults_to_loopback_and_remote_refuses_to_guess(self):
        local = RT.from_env(env={'BIOSENSE_RUNTIME_MODE': 'local'})
        self.assertEqual(RT.DEFAULT_LOCAL_SERVER, local.server)
        self.assertTrue(local.is_real)
        with self.assertRaises(K.ContractError) as e:
            RT.from_env(env={'BIOSENSE_RUNTIME_MODE': 'remote'})
        self.assertIn('does not guess', str(e.exception))

    def test_remote_configuration_is_read_whole(self):
        cfg = RT.from_env(env={
            'BIOSENSE_RUNTIME_MODE': 'remote',
            'BIOSENSE_OMNIGENT_SERVER': 'https://omnigent.example.org/',
            'BIOSENSE_OMNIGENT_AGENT': 'biosense_discovery_loop',
            'BIOSENSE_OMNIGENT_TOKEN': 'tok-abc',
        })
        self.assertEqual('https://omnigent.example.org', cfg.server)
        self.assertEqual('tok-abc', cfg.token)
        self.assertTrue(cfg.is_real)

    def test_a_token_file_is_read_and_a_missing_one_refuses(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'tok'
            p.write_text('  file-token\n')
            cfg = RT.from_env(env={'BIOSENSE_RUNTIME_MODE': 'remote',
                                   'BIOSENSE_OMNIGENT_SERVER': 'https://x.example',
                                   'BIOSENSE_OMNIGENT_TOKEN_FILE': str(p)})
            self.assertEqual('file-token', cfg.token)
        with self.assertRaises(K.ContractError):
            RT.from_env(env={'BIOSENSE_RUNTIME_MODE': 'remote',
                             'BIOSENSE_OMNIGENT_SERVER': 'https://x.example',
                             'BIOSENSE_OMNIGENT_TOKEN_FILE': '/no/such/file'})

    def test_a_plaintext_remote_server_is_refused_unless_declared_safe(self):
        """A bearer token over plaintext to a third party is a deployment mistake,
        and refusing to start with it is the right outcome."""
        with self.assertRaises(K.ContractError) as e:
            RT.from_env(env={'BIOSENSE_RUNTIME_MODE': 'remote',
                             'BIOSENSE_OMNIGENT_SERVER': 'http://omnigent.example.org'})
        self.assertIn('plaintext', str(e.exception))
        ok = RT.from_env(env={'BIOSENSE_RUNTIME_MODE': 'remote',
                              'BIOSENSE_OMNIGENT_SERVER': 'http://omnigent.example.org',
                              'BIOSENSE_OMNIGENT_ALLOW_INSECURE': '1'})
        self.assertTrue(ok.is_real)

    def test_local_mode_refuses_a_server_that_is_not_this_machine(self):
        with self.assertRaises(K.ContractError) as e:
            RT.from_env(env={'BIOSENSE_RUNTIME_MODE': 'local',
                             'BIOSENSE_OMNIGENT_SERVER': 'https://somewhere.example'})
        self.assertIn('not this machine', str(e.exception))

    def test_credentials_in_the_url_are_refused(self):
        with self.assertRaises(K.ContractError) as e:
            RT.validate_server('https://user:pw@omnigent.example', 'remote_real_ai')
        self.assertIn('BIOSENSE_OMNIGENT_TOKEN', str(e.exception))

    def test_a_deployment_offers_only_what_it_was_configured_for(self):
        """Railway as configured today serves one mode, and the interface must
        show that rather than a button that cannot work."""
        cfg = RT.from_env(env={})
        self.assertEqual(('synthetic_demo',), cfg.allowed)
        both = RT.from_env(env={'BIOSENSE_ALLOWED_RUNTIMES': 'synthetic, local'})
        self.assertEqual(('synthetic_demo', 'local_real_ai'), both.allowed)


class SecretTests(unittest.TestCase):
    def test_the_public_view_carries_no_token_and_no_path(self):
        cfg = RT.from_env(env={'BIOSENSE_RUNTIME_MODE': 'remote',
                               'BIOSENSE_OMNIGENT_SERVER': 'https://omnigent.example.org/x/y',
                               'BIOSENSE_OMNIGENT_TOKEN': 'super-secret-token'})
        blob = json.dumps(cfg.public())
        self.assertNotIn('super-secret-token', blob)
        self.assertNotIn(cfg.workspace, blob)
        # scheme and host only: a path or a query could itself carry a credential
        self.assertEqual('https://omnigent.example.org', cfg.public()['server'])
        self.assertIs(True, cfg.public()['token_configured'])

    def test_an_unavailable_runtime_never_mentions_the_token(self):
        e = RT.RuntimeUnavailable('auth_invalid', 'rejected by the server')
        self.assertNotIn('token=', str(e))
        self.assertTrue(e.next_step)


class AvailabilityTests(unittest.TestCase):
    def test_every_reason_code_has_a_next_step(self):
        """A failure with no stated remedy is a dead end for whoever hit it."""
        for code, (headline, step) in RT.REASONS.items():
            self.assertTrue(headline, code)
            if code != 'ok':
                self.assertTrue(step, f'{code} has no next step')

    def test_a_mode_this_deployment_does_not_offer_is_refused_by_name(self):
        cfg = RT.from_env(env={})
        found = RT.available(cfg, 'local_real_ai', probe_fn=lambda c: RT.reason('ok'))
        self.assertFalse(found['ok'])
        self.assertEqual('not_configured', found['reason'])

    def test_the_synthetic_runtime_needs_no_probe(self):
        cfg = RT.from_env(env={})

        def explode(_c):
            raise AssertionError('the synthetic path must not contact a server')

        self.assertTrue(RT.available(cfg, 'synthetic_demo', probe_fn=explode)['ok'])

    def test_a_probe_failure_becomes_a_named_reason(self):
        cfg = RT.from_env(env={'BIOSENSE_RUNTIME_MODE': 'local',
                               'BIOSENSE_ALLOWED_RUNTIMES': 'local'})
        found = RT.available(cfg, 'local_real_ai',
                             probe_fn=lambda c: RT.reason('no_runner_available'))
        self.assertFalse(found['ok'])
        self.assertIn('omnigent host', found['next_step'])

    def test_a_runs_directory_outside_the_workspace_is_refused_before_anything_starts(self):
        """The agents write under ./runs relative to the runner's workspace. If
        BioSense reads elsewhere the run succeeds and nobody ever sees it."""
        cfg = RT.from_env(runs_dir='/tmp', env={
            'BIOSENSE_RUNTIME_MODE': 'local',
            'BIOSENSE_ALLOWED_RUNTIMES': 'local',
            'BIOSENSE_OMNIGENT_WORKSPACE': str(K.ROOT)})
        with self.assertRaises(RT.RuntimeUnavailable) as e:
            RT.check_workspace(cfg)
        self.assertEqual('workspace_mismatch', e.exception.reason)
        inside = RT.from_env(runs_dir=K.ROOT / 'runs', env={
            'BIOSENSE_RUNTIME_MODE': 'local',
            'BIOSENSE_OMNIGENT_WORKSPACE': str(K.ROOT)})
        self.assertEqual('runs', RT.check_workspace(inside))


class NoFallbackTests(unittest.TestCase):
    """The rule the whole design turns on, asserted rather than assumed."""

    def test_no_module_maps_a_real_failure_onto_the_synthetic_path(self):
        import inspect
        from biosense.production import discovery_runner as DR
        from biosense.production import omnigent_runtime as OMNI
        for mod in (RT, OMNI, DR):
            src = inspect.getsource(mod)
            for marker in ('fall back to synthetic', 'fallback to synthetic',
                           "mode = 'synthetic_demo'"):
                self.assertNotIn(marker, src.replace('does not fall back', ''),
                                 f'{mod.__name__} may substitute a synthetic run')

    def test_run_real_refuses_a_synthetic_configuration(self):
        from biosense.production import discovery_runner as DR
        cfg = RT.from_env(env={})
        with self.assertRaises(K.ContractError):
            DR.run_real(cfg, {'objective': 'x', 'request_id': 'r', 'project_id': 'p'}, '/tmp/x')


class GlossaryTests(unittest.TestCase):
    def test_every_word_the_code_uses_can_be_explained(self):
        """A badge the interface can render but cannot define is the gap the
        glossary exists to close, and it would otherwise appear silently the next
        time somebody adds a class."""
        self.assertEqual([], GL.missing_terms())

    def test_each_definition_says_something(self):
        for name, group in GL.describe()['groups'].items():
            for key, d in group['terms'].items():
                self.assertTrue(d.get('label'), f'{name}/{key} has no label')
                self.assertGreater(len(d.get('short', '')), 10, f'{name}/{key} is not explained')

    def test_the_dangerous_words_say_what_they_are_not(self):
        self.assertIn('not a validated digital twin',
                      GL.lookup('estimate_type', 'simulated')['long'])
        self.assertIn('never evidence', GL.lookup('estimate_type', 'target')['long'])
        self.assertIn('fitted to nothing', GL.lookup('model_basis', 'ai_proposed')['long'])
        self.assertIn('never supply a cited protocol value',
                      GL.lookup('evidence_class', 'expert_knowledge')['long'])
        self.assertIn('blocks the wet lab', GL.lookup('provenance', 'gap')['short'])


if __name__ == '__main__':
    unittest.main()
