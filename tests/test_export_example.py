"""A real run becomes a committable example, and nothing that must not be committed comes along."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.production import discovery as DISC
from biosense.production import export_example as EX


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run = self.tmp / 'runs' / 'ai-20261007-abcdef'
        (self.run / 'literature' / 'sources').mkdir(parents=True)
        (self.run / 'analyst').mkdir()
        req = DISC.build(project_id='ipsc_macrophage',
                         objective='Increase viable macrophage production while keeping identity.',
                         runtime_mode='local')
        K.write_json_atomic(self.run / 'discovery_request.json', req)
        (self.run / 'literature' / 'insights.md').write_text('PMC1: shows a dose.')
        (self.run / 'literature' / 'sources' / 'PMC1.json').write_text('{"full": "text"}')
        (self.run / 'events.jsonl').write_text('{}\n')
        (self.run / 'H01.draft.json').write_text('{}')
        K.write_json_atomic(self.run / 'analyst' / 'analysis_result_pub.json',
                            {'source_visibility': 'public', 'datasets': []})
        K.write_json_atomic(self.run / 'analyst' / 'analysis_result_mine.json',
                            {'source_visibility': 'private', 'datasets': []})

    def test_only_what_may_be_committed_is_copied(self):
        r = EX.export(self.run, name='macrophage-test', out_root=self.tmp / 'ex')
        self.assertIn('literature/insights.md', r['copied'])
        self.assertIn('analyst/analysis_result_pub.json', r['copied'])
        joined = ' '.join(r['copied'])
        for banned in ('sources', 'events.jsonl', 'draft', 'mine'):
            self.assertNotIn(banned, joined)
        self.assertTrue(any('private' in w for w in r['withheld']))
        readme = (self.tmp / 'ex' / 'macrophage-test' / 'README.md').read_text()
        self.assertIn('Increase viable macrophage production', readme)
        self.assertIn('## Withheld', readme)

    def test_a_synthetic_run_or_a_bad_name_is_refused(self):
        req = json.loads((self.run / 'discovery_request.json').read_text())
        with self.assertRaisesRegex(K.ContractError, 'name the example'):
            EX.export(self.run, name='Bad Name', out_root=self.tmp / 'ex')
        req['runtime_mode'] = 'synthetic_demo'
        (self.run / 'discovery_request.json').write_text(json.dumps(req))
        with self.assertRaisesRegex(K.ContractError, 'synthetic demo'):
            EX.export(self.run, name='demo-run', out_root=self.tmp / 'ex')


if __name__ == '__main__':
    unittest.main()
