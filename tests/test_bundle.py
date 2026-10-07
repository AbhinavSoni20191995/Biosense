"""The operator's per-agent model choices, written into a copy of the bundle.

Omnigent's Claude harness honours executor.config.model and reasoning_effort
per agent. The specialists on a faster model is the biggest time lever a run
has; this is the one place it is written, and the committed bundle never is.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.production import bundle as BN


class BundleCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_specialists_get_the_model_and_the_orchestrator_keeps_its_own(self):
        r = BN.write_copy(K.ROOT / 'discovery_loop', self.tmp / 'b',
                          specialist_model='claude-sonnet-5-5', specialist_effort='medium')
        self.assertEqual({'model': 'claude-sonnet-5-5', 'reasoning_effort': 'medium'},
                         r['specialists'])
        for name in BN.SPECIALISTS:
            text = (self.tmp / 'b' / 'agents' / name / 'config.yaml').read_text()
            block = text[text.index('executor:'):text.index('os_env:')]
            self.assertIn('    harness: claude-sdk\n    model: claude-sonnet-5-5\n'
                          '    reasoning_effort: medium\n', block, name)
            self.assertEqual(1, text.count('model: claude-sonnet-5-5'), name)
        orch = (self.tmp / 'b' / 'config.yaml').read_text()
        self.assertNotIn('    model:', orch)
        # the committed bundle is untouched
        src = (K.ROOT / 'discovery_loop' / 'agents' / 'literature' / 'config.yaml').read_text()
        self.assertNotIn('model:', src)

    def test_everything_but_the_executor_config_survives_byte_for_byte(self):
        BN.write_copy(K.ROOT / 'discovery_loop', self.tmp / 'b', orchestrator_model='claude-opus-5')
        before = (K.ROOT / 'discovery_loop' / 'config.yaml').read_text().splitlines()
        after = (self.tmp / 'b' / 'config.yaml').read_text().splitlines()
        added = [l for l in after if l not in before]
        self.assertEqual(['    model: claude-opus-5'], added)
        self.assertEqual(len(before) + 1, len(after))
        lit_src = (K.ROOT / 'discovery_loop' / 'agents' / 'literature' / 'config.yaml').read_text()
        self.assertEqual(lit_src, (self.tmp / 'b' / 'agents' / 'literature' / 'config.yaml').read_text())

    def test_an_existing_key_is_replaced_in_place_and_never_duplicated(self):
        text = ('executor:\n  type: omnigent\n  config:\n    harness: claude-sdk\n'
                '    model: old\nasync: true\n')
        out = BN.set_executor_config(text, model='new', reasoning_effort='low')
        self.assertEqual('executor:\n  type: omnigent\n  config:\n    harness: claude-sdk\n'
                         '    reasoning_effort: low\n    model: new\nasync: true\n', out)
        self.assertEqual(1, out.count('model: '), 'the existing key was replaced, not re-added')
        self.assertNotIn('old', out)
        # Nothing after the executor block moves, and a second pass is a no-op.
        self.assertEqual(out, BN.set_executor_config(out, model='new', reasoning_effort='low'))

    def test_a_value_that_is_not_a_model_id_or_an_effort_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'not a model id'):
            BN.write_copy(K.ROOT / 'discovery_loop', self.tmp / 'b', specialist_model='x; rm -rf /')
        with self.assertRaisesRegex(ValueError, 'reasoning effort'):
            BN.write_copy(K.ROOT / 'discovery_loop', self.tmp / 'b', specialist_effort='max')
        with self.assertRaisesRegex(ValueError, 'no executor.config.harness'):
            BN.set_executor_config('executor:\n  type: omnigent\n', model='m')
        self.assertEqual(1, BN.main(['--src', str(self.tmp / 'nowhere'), '--dst', str(self.tmp / 'b')]))

    def test_every_agent_starts_in_the_workspace_not_a_scratch_directory(self):
        """A specialist with `cwd: .` woke in a per-session scratch directory and
        failed on '.venv/bin/python: No such file or directory', run after run."""
        BN.write_copy(K.ROOT / 'discovery_loop', self.tmp / 'b', cwd='/app')
        cfgs = [self.tmp / 'b' / 'config.yaml'] + sorted((self.tmp / 'b' / 'agents').glob('*/config.yaml'))
        self.assertEqual(7, len(cfgs))
        for cfg in cfgs:
            text = cfg.read_text()
            self.assertIn('\n  cwd: /app\n', text, cfg)
            self.assertNotIn('\n  cwd: .\n', text, cfg)
        # nothing else in os_env moved
        src = (K.ROOT / 'discovery_loop' / 'agents' / 'literature' / 'config.yaml').read_text()
        got = (self.tmp / 'b' / 'agents' / 'literature' / 'config.yaml').read_text()
        self.assertEqual(src.replace('\n  cwd: .\n', '\n  cwd: /app\n'), got)
        with self.assertRaisesRegex(ValueError, 'absolute'):
            BN.write_copy(K.ROOT / 'discovery_loop', self.tmp / 'c', cwd='relative/dir')

    def test_the_boot_scripts_always_register_a_pinned_copy(self):
        for script in ('deploy/start-ai.sh', 'scripts/start_local_ai.sh'):
            text = (K.ROOT / script).read_text()
            self.assertIn('biosense.production.bundle', text, script)
            self.assertIn('BIOSENSE_SPECIALIST_MODEL', text, script)
            self.assertIn('--agent "$AGENT_BUNDLE"', text, script)
        hosted = (K.ROOT / 'deploy' / 'start-ai.sh').read_text()
        self.assertIn('--cwd "$APP_ROOT"', hosted)
        # not behind a condition on the model variables: the cwd is always pinned
        block = hosted[hosted.index('The registered bundle is always a copy'):]
        self.assertNotIn('if [ -n "${BIOSENSE_SPECIALIST_MODEL', block.split('--cwd')[0])
        local = (K.ROOT / 'scripts' / 'start_local_ai.sh').read_text()
        self.assertIn('--cwd "$ROOT"', local)
        self.assertIn('AGENT_BUNDLE="$STATE/agents"', local)
        self.assertIn('BIOSENSE_SPECIALIST_MODEL', (K.ROOT / 'deploy' / 'README.md').read_text())
