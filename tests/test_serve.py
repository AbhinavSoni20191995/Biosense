"""Read-only artifact server: what it exposes, and what it must never expose."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense.production import serve as SV


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / 'runs'
        d = self.runs / 'loop-a'
        write(d / 'loop_state.json', {
            'loop_id': 'loop-a', 'request_id': 'req-1', 'created_at': '2026-01-02T00:00:00+00:00',
            'max_iterations': 4,
            'autonomy': {'mode': 'checkpoints', 'bioreactor_source': 'synthetic_standin'},
            'iterations': [{'iteration': 0, 'verdict': 'FAILED', 'advances_iteration': True},
                           {'iteration': 1, 'verdict': 'SUCCESS', 'advances_iteration': False}],
            'consults': [{'consult_id': 'consult-00', 'status': 'open'}]})
        write(d / 'decisions' / 'decision-00.json', {'decision_id': 'decision-00', 'type': 'revise_protocol'})
        write(d / 'decisions' / 'decision-01.json', {'decision_id': 'decision-01', 'type': 'protocol_succeeded'})
        write(d / 'it0' / 'analysis.json', {'analysis_id': 'a0', 'verdict': {'status': 'FAILED'}})
        write(d / 'it0' / 'protocol.approved.json', {'protocol_id': 'p0'})
        write(d / 'it1-release' / 'analysis.json', {'analysis_id': 'a1', 'verdict': {'status': 'SUCCESS'}})
        write(d / 'consults' / 'consult-00.json', {'consult_id': 'consult-00', 'question': 'q'})
        write(d / 'consults' / 'consult-00.answered.json',
              {'consult_id': 'consult-00', 'question': 'q', 'answer': {'text': 'a'}})

        older = self.runs / 'loop-b'
        write(older / 'loop_state.json', {'loop_id': 'loop-b', 'request_id': 'req-0',
                                          'created_at': '2026-01-01T00:00:00+00:00',
                                          'max_iterations': 2, 'iterations': [], 'consults': []})
        (self.runs / 'not-a-loop').mkdir()  # no loop_state.json

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lists_loops_newest_first_and_skips_non_loops(self):
        loops = SV.list_loops(self.runs)
        self.assertEqual([l['dir'] for l in loops], ['loop-a', 'loop-b'])
        a = loops[0]
        self.assertEqual(a['iterations_used'], 1)   # only the advancing iteration counts
        self.assertEqual(a['analyses'], 2)
        self.assertEqual(a['verdicts'], ['FAILED', 'SUCCESS'])
        self.assertEqual(a['open_consults'], 1)
        self.assertEqual(a['bioreactor_source'], 'synthetic_standin')

    def test_bundle_carries_state_decisions_analyses_protocols(self):
        b = SV.load_loop(self.runs, 'loop-a')
        self.assertEqual(b['state']['request_id'], 'req-1')
        self.assertEqual([d['decision_id'] for d in b['decisions']], ['decision-00', 'decision-01'])
        self.assertEqual([a['stage'] for a in b['analyses']], ['it0', 'it1-release'])
        self.assertEqual([p['protocol']['protocol_id'] for p in b['protocols']], ['p0'])

    def test_answered_consult_replaces_the_open_copy(self):
        consults = SV.load_loop(self.runs, 'loop-a')['consults']
        self.assertEqual(len(consults), 1)
        self.assertTrue(consults[0]['answer'])

    def test_missing_loop_and_malformed_state_are_absent_not_errors(self):
        self.assertIsNone(SV.load_loop(self.runs, 'no-such-loop'))
        bad = self.runs / 'loop-c'
        bad.mkdir()
        (bad / 'loop_state.json').write_text('{ not json')
        self.assertIsNone(SV.load_loop(self.runs, 'loop-c'))
        self.assertNotIn('loop-c', [l['dir'] for l in SV.list_loops(self.runs)])

    def test_truth_files_are_never_served(self):
        d = self.runs / 'loop-a'
        write(d / 'standin_truth.json', {'hidden': 'the answer'})
        write(d / 'it0' / 'analysis.json', {'analysis_id': 'a0', 'verdict': {'status': 'FAILED'}})
        bundle = SV.load_loop(self.runs, 'loop-a')
        self.assertNotIn('hidden', json.dumps(bundle))
        self.assertIsNone(SV._read_json(d / 'standin_truth.json'))

    def test_paths_outside_the_runs_directory_are_refused(self):
        for bad in ('../..', '..', 'loop-a/../../etc', '/etc', 'a/b', '', '.'):
            self.assertIsNone(SV.load_loop(self.runs, bad), bad)

    def test_oversized_file_is_skipped_rather_than_streamed(self):
        p = self.runs / 'loop-a' / 'decisions' / 'decision-02.json'
        p.write_text('[' + '0,' * (SV.MAX_BYTES // 2) + '0]')
        self.assertIsNone(SV._read_json(p))
        self.assertEqual(len(SV.load_loop(self.runs, 'loop-a')['decisions']), 2)


if __name__ == '__main__':
    unittest.main()
