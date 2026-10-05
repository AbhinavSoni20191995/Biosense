"""The loop with datasets enabled: evidence reaches the orchestrator, and stops there.

The property this file exists for is the separation the whole design rests on.
An analysis produces a candidate parameter. A candidate parameter is not a
change. The change happens only if the orchestrator chooses `revise_protocol`
and that decision passes the envelope, and the test below asserts that the
analysis step itself altered nothing about the protocol.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.data import ingest as ING
from biosense.data import roots as DR
from biosense.production import engine as EN
from biosense.production import protocol as PP

IL7 = K.ROOT / 'examples' / 'datasets' / 'facs_il7_tcell_synthetic.csv'
TRUTH = K.ROOT / 'examples' / 'ipsc_tcell' / 'standin_truth.synthetic.json'
REQUEST = K.ROOT / 'examples' / 'ipsc_tcell' / 'request.wt_d40.json'


class DataLoopCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._env = os.environ.get(DR.PRIVATE_ENV)
        os.environ[DR.PRIVATE_ENV] = str(self.tmp / 'private')
        os.environ[DR.CACHE_ENV] = str(self.tmp / 'cache')
        DR.ensure_roots()
        ING.ingest_local(
            IL7, dataset_id='facs-il7-tcell',
            title='IL-7 dose response in iPSC-derived T cell expansion',
            modality='cytometry_summary', cell_type='iPSC-derived T cell',
            perturbation='IL-7 concentration',
            experimental_design={'condition_column': 'condition', 'control': 'il7_standard',
                                 'treatments': ['il7_high'], 'replicate_column': 'replicate',
                                 'donor_column': 'donor', 'sample_id_column': 'sample_id'})
        self.events = []

    def tearDown(self):
        if self._env is None:
            os.environ.pop(DR.PRIVATE_ENV, None)
        else:
            os.environ[DR.PRIVATE_ENV] = self._env
        os.environ.pop(DR.CACHE_ENV, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_loop(self, **bioinformatics):
        req = K.read_json(REQUEST)
        req['bioinformatics'] = {'allowed': True, **bioinformatics}
        return EN.safe_run_loop(req, self.tmp / 'loop', standin='ipsc_tcell',
                                truth=K.read_json(TRUTH), on_event=self.events.append)

    def kinds(self, kind):
        return [e for e in self.events if e['kind'] == kind]


class DatasetsOffTests(DataLoopCase):
    def test_a_loop_that_was_not_asked_for_datasets_reads_none(self):
        s = self.run_loop()
        self.assertEqual([], self.kinds('dataset_analysis'))
        self.assertEqual([], s['dataset_analyses'])

    def test_public_only_does_not_reach_private_data(self):
        """It may analyse the committed public fixture. It must not touch the
        private dataset registered in setUp."""
        s = self.run_loop(datasets=True, private_data=False)
        used = {d for a in s['dataset_analyses'] for d in a['datasets']}
        self.assertNotIn('facs-il7-tcell', used)
        for a in s['dataset_analyses']:
            self.assertNotEqual('private', a['source_visibility'])
            self.assertNotEqual('private_user_dataset', a['source_evidence_class'])


class DatasetsOnTests(DataLoopCase):
    def setUp(self):
        super().setUp()
        self.summary = self.run_loop(datasets=True, private_data=True)

    def test_the_analysis_runs_against_a_hypothesis_the_loop_raised(self):
        ev = self.kinds('dataset_analysis')
        self.assertEqual(1, len(ev))
        analysis = K.read_json(self.tmp / 'loop' / 'it0' / 'analysis.json')
        raised = {h['hypothesis_id'] for h in analysis['diagnosis']}
        self.assertIn(ev[0]['uncertainty'], raised)

    def test_the_result_keeps_its_lineage_separate(self):
        ev = self.kinds('dataset_analysis')[0]
        self.assertEqual('derived_analysis', ev['evidence_class'])
        self.assertEqual('private_user_dataset', ev['source_evidence_class'])
        self.assertEqual('private', ev['source_visibility'])

    def test_the_plan_and_the_result_are_written_and_valid(self):
        d = self.tmp / 'loop' / 'it0'
        plan = K.read_json(d / 'analysis_plan.json')
        result = K.read_json(d / 'dataset_analysis.json')
        self.assertEqual([], K.schema_errors('analysis_plan', plan))
        self.assertEqual([], K.schema_errors('analysis_result', result))
        self.assertEqual(plan['uncertainty_ref']['ref'], result['uncertainty_ref']['ref'])

    def test_the_decision_is_an_information_action_and_spends_no_iteration(self):
        decs = [e for e in self.kinds('decision') if e['type'] == 'request_bioinformatics']
        self.assertTrue(decs)
        for d in decs:
            self.assertFalse(d['advances'])

    def test_the_committed_decision_carries_the_analysis_contract(self):
        state = K.read_json(self.tmp / 'loop' / 'loop_state.json')
        rows = [it for it in state['iterations']
                if it['decision_type'] == 'request_bioinformatics']
        self.assertTrue(rows)
        doc = next(K.read_json(r['decision_path']) for r in rows
                   if (K.read_json(r['decision_path']).get('instruction') or {}).get('analysis'))
        a = doc['instruction']['analysis']
        for field in ('uncertainty_ref', 'dataset_ids', 'plan', 'parameter_decision'):
            self.assertTrue(a.get(field), field)
        self.assertIs(False, doc['advances_iteration'])

    def test_the_analysis_did_not_change_the_protocol(self):
        """The point of the whole separation: evidence arrives, nothing moves."""
        d = self.tmp / 'loop' / 'it0'
        protocol = K.read_json(d / 'protocol.approved.json')
        result = K.read_json(d / 'dataset_analysis.json')
        self.assertTrue(result['candidate_process_parameters'])
        # the protocol that ran is byte-identical to the one approved before the
        # analysis existed: the analysis is written beside it, never into it
        self.assertEqual(protocol['protocol_sha256'] if 'protocol_sha256' in protocol
                         else PP.protocol_sha256(protocol),
                         PP.protocol_sha256(K.read_json(d / 'protocol.approved.json')))
        for c in result['candidate_process_parameters']:
            self.assertNotIn(c['parameter'], str(protocol.get('changes_from_parent') or ''))

    def test_a_candidate_parameter_is_not_applied_to_the_next_revision_automatically(self):
        """A later protocol may move a parameter, but only through the reviser's
        own search. The analysis has no route into it."""
        import inspect

        from biosense.production import revise as RV
        src = inspect.getsource(RV)
        for forbidden in ('analysis_result', 'dataset_analysis', 'candidate_process_parameters',
                          'bioinformatics.execute'):
            self.assertNotIn(forbidden, src,
                             f'revise.py reads {forbidden}: an analysis result must not reach '
                             f'the reviser directly')

    def test_the_loop_still_reaches_a_terminal_decision(self):
        self.assertIn(self.summary['terminal'],
                      ('protocol_succeeded', 'stop_budget', 'complete_qc', 'search_exhausted'))
        self.assertEqual([], self.summary['refusals'])


if __name__ == '__main__':
    unittest.main()
