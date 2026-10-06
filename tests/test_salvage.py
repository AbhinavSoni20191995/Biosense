"""A run in flight when the server restarted keeps what its agents wrote.

A redeploy replaced the container under a twenty-minute real run, and the run
then showed only "interrupted" — while its literature digest, its running
notes and a validated hypothesis sat in the run directory on the volume.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.evidence import cli as EVCLI
from biosense.production import discovery as DISC
from biosense.production import run_store as RS
from biosense.production import salvage as SV


class SalvageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.req = DISC.build(project_id='ipsc_macrophage',
                              objective='Increase viable macrophage production while keeping identity.',
                              runtime_mode='synthetic_demo')

    def interrupted_run(self, rid='a' * 16, with_hypothesis=True, status='running'):
        out = self.tmp / f'ai-x-{rid[:6]}'
        out.mkdir()
        K.write_json_atomic(out / 'discovery_request.json', self.req)
        if with_hypothesis:
            draft = dict(EVCLI.TEMPLATE, effects=EVCLI.TEMPLATE['effects'][:1])
            K.write_json_atomic(out / 'quantified_hypothesis.json',
                                EVCLI.build_hypothesis(draft, project_id='ipsc_macrophage'))
        RS.write_state(out, {'run_id': rid, 'status': status, 'runtime_mode': 'synthetic_demo',
                             'project_id': 'ipsc_macrophage', 'objective': self.req['objective'],
                             'started_at': 1.0, 'owner': None})
        return out

    def test_what_the_agents_wrote_is_finished_and_kept_labelled_partial(self):
        out = self.interrupted_run()
        self.assertEqual(['a' * 16], SV.salvage(self.tmp))
        snap = RS.restore(self.tmp, 'a' * 16)
        self.assertEqual('interrupted', snap['status'])
        self.assertIn('partial result', snap['error'])
        self.assertTrue(snap['result']['bundle']['hypotheses'], 'the hypothesis was kept')
        self.assertTrue((out / 'protocol_summary.json').exists())
        self.assertTrue(snap['salvaged'])

    def test_a_run_is_salvaged_once_and_a_live_or_finished_one_never(self):
        self.interrupted_run('a' * 16)
        self.interrupted_run('b' * 16, status='done')
        self.interrupted_run('c' * 16)
        self.assertEqual(['a' * 16], SV.salvage(self.tmp, live_ids={'c' * 16}))
        self.assertEqual(['c' * 16], SV.salvage(self.tmp), 'only the one not yet salvaged')
        self.assertEqual([], SV.salvage(self.tmp))
        self.assertEqual('done', RS.restore(self.tmp, 'b' * 16)['status'])

    def test_a_run_with_nothing_written_is_kept_honest(self):
        self.interrupted_run(with_hypothesis=False)
        SV.salvage(self.tmp)
        snap = RS.restore(self.tmp, 'a' * 16)
        self.assertEqual('interrupted', snap['status'])
        self.assertFalse((snap['result'] or {}).get('bundle', {}).get('hypotheses'))

    def test_the_app_salvages_at_startup_and_the_page_shows_partial_results(self):
        src = (K.ROOT / 'biosense' / 'production' / 'app.py').read_text()
        self.assertIn('SV.salvage_in_background(runs', src)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn("'Partial result — '", js)
        self.assertIn('function hasPartial', js)


class WorkingDirectoryTests(unittest.TestCase):
    """A specialist's shell can start in a scratch directory; one run lost its
    bioinformatics agent to '.venv/bin/python: No such file or directory'."""

    def test_the_brief_names_the_root_and_uses_an_absolute_interpreter(self):
        req = DISC.build(project_id='ipsc_macrophage',
                         objective='Increase viable macrophage production while keeping identity.',
                         runtime_mode='synthetic_demo')
        brief = DISC.render_brief(req, loop_dir='data/runs/ai-x', workspace='/app',
                                  python='/app/.venv/bin/python')
        self.assertIn('## Where you are', brief)
        self.assertIn('`cd /app && `', brief)
        self.assertIn('/app/.venv/bin/python -m biosense.bioinformatics.cli analyse run', brief)
        self.assertNotIn('## Where you are', DISC.render_brief(req, loop_dir='x'))

    def test_every_specialist_is_told_to_start_from_the_root(self):
        for name in ('analysis', 'bioinformatics', 'biosimulator', 'outcome'):
            self.assertIn('Working directory', (K.ROOT / 'discovery_loop' / 'agents' / name /
                                                'prompt.md').read_text(), name)
        self.assertIn('Working directory', (K.ROOT / 'discovery_loop' / 'agents' / 'literature' /
                                            'config.yaml').read_text())

    def test_the_runner_passes_the_absolute_root(self):
        src = (K.ROOT / 'biosense' / 'production' / 'discovery_runner.py').read_text()
        self.assertIn("workspace=str(ws) if ws else None", src)
