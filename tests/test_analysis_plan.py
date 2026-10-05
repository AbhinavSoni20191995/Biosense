"""Planning and executing an analysis, and the rules that decide whether it may run.

The rule the whole design turns on is tested first: an analysis that answers no
stated question cannot be constructed. The rest check that a plan is matched
against the tools and the data before anything executes, so a refusal names the
missing field instead of producing a confident comparison of the wrong thing.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.bioinformatics import execute as EX
from biosense.bioinformatics import external as EXT
from biosense.bioinformatics import implications as IMP
from biosense.bioinformatics import plan as PLAN
from biosense.bioinformatics import registry as TREG
from biosense.data import ingest as ING
from biosense.data import roots as DR

FACS = K.ROOT / 'examples' / 'datasets' / 'facs_mcsf_synthetic.csv'
BULK = K.ROOT / 'examples' / 'datasets' / 'bulk_counts_synthetic.csv'
DESIGN = {'condition_column': 'condition', 'control': 'control', 'treatments': ['mcsf_high'],
          'replicate_column': 'replicate', 'donor_column': 'donor', 'sample_id_column': 'sample_id'}
GAP = 'GAP-mcsf-dose'


def a_gap():
    return PLAN.evidence_gap(GAP, 'Nothing in this loop says whether M-CSF dose limits the yield.')


class PlanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._env = os.environ.get(DR.PRIVATE_ENV)
        os.environ[DR.PRIVATE_ENV] = str(self.tmp / 'private')
        os.environ[DR.CACHE_ENV] = str(self.tmp / 'cache')
        DR.ensure_roots()
        self.manifest, _ = ING.ingest_local(
            FACS, dataset_id='facs-demo', title='M-CSF dose comparison',
            modality='cytometry_summary', cell_type='iPSC-derived monocyte',
            perturbation='M-CSF concentration', experimental_design=DESIGN)

    def tearDown(self):
        if self._env is None:
            os.environ.pop(DR.PRIVATE_ENV, None)
        else:
            os.environ[DR.PRIVATE_ENV] = self._env
        os.environ.pop(DR.CACHE_ENV, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def a_plan(self, **kw):
        kw.setdefault('uncertainty_ref', a_gap())
        kw.setdefault('recorded_gaps', [GAP])
        return PLAN.plan(
            plan_id=kw.pop('plan_id', 'plan-1'),
            question='Does the higher M-CSF condition raise CD14+ without costing viability?',
            why_requested='The loop holds no evidence on M-CSF dose.',
            dataset_ids=kw.pop('dataset_ids', ['facs-demo']),
            analysis_type=kw.pop('analysis_type', 'population_comparison'),
            tool=kw.pop('tool', 'cytometry.population_comparison'),
            decision_relevance='Whether to test a higher M-CSF dose next.',
            parameters_that_may_change=['mcsf_ng_ml'], **kw)


class UncertaintyTests(PlanCase):
    """The structural requirement, not a prompt instruction."""

    def test_a_plan_without_an_uncertainty_is_refused(self):
        with self.assertRaises(K.ContractError) as e:
            self.a_plan(uncertainty_ref=None)
        msg = str(e.exception)
        self.assertIn('uncertainty_ref', msg)
        self.assertIn('runs one because a decision is waiting on it', msg)

    def test_the_schema_itself_requires_an_uncertainty(self):
        p = self.a_plan()
        del p['uncertainty_ref']
        self.assertTrue(any('uncertainty_ref' in e for e in
                            K.schema_errors('analysis_plan', p)))

    def test_a_hypothesis_the_report_does_not_raise_is_refused(self):
        report = {'analysis_id': 'a1', 'diagnosis': [{'hypothesis_id': 'H01', 'statement': 'x'}]}
        with self.assertRaises(K.ContractError) as e:
            self.a_plan(uncertainty_ref={'kind': 'hypothesis', 'ref': 'H99',
                                         'statement': 'invented uncertainty'},
                        analysis_report=report)
        self.assertIn('H99', str(e.exception))
        self.assertIn('H01', str(e.exception))

    def test_a_hypothesis_the_report_does_raise_is_accepted(self):
        report = {'analysis_id': 'a1',
                  'diagnosis': [{'hypothesis_id': 'H01', 'statement': 'yield is limited'}]}
        p = self.a_plan(uncertainty_ref=PLAN.uncertainty_from_hypothesis(report['diagnosis'][0]),
                        analysis_report=report)
        self.assertEqual('H01', p['uncertainty_ref']['ref'])
        self.assertEqual('hypothesis', p['uncertainty_ref']['kind'])

    def test_an_unrecorded_evidence_gap_is_refused(self):
        with self.assertRaises(K.ContractError) as e:
            self.a_plan(uncertainty_ref=PLAN.evidence_gap('GAP-invented', 'something unstated'),
                        recorded_gaps=[GAP])
        self.assertIn('GAP-invented', str(e.exception))

    def test_a_plan_records_why_it_was_asked_and_what_it_could_change(self):
        p = self.a_plan()
        self.assertTrue(p['why_requested'])
        self.assertTrue(p['decision_relevance'])
        self.assertEqual(['mcsf_ng_ml'], p['parameters_that_may_change'])


class ToolMatchingTests(PlanCase):
    def test_an_unknown_tool_is_refused(self):
        with self.assertRaises(K.ContractError) as e:
            self.a_plan(tool='magic.do_science')
        self.assertIn('unknown tool', str(e.exception))

    def test_a_declared_but_unimplemented_tool_says_so(self):
        with self.assertRaises(K.ContractError) as e:
            TREG.get('external.deseq2')
        self.assertIn('declared but not implemented', str(e.exception))
        self.assertIn('Available now', str(e.exception))

    def test_a_tool_whose_optional_extra_is_absent_says_which_extra(self):
        """The two refusals read differently on purpose: one is work nobody has
        done, the other is an install line the reader can act on."""
        planned = {p['name']: p for p in TREG.PLANNED}
        name = 'single_cell.pseudobulk_comparison'
        if name in TREG.TOOLS:
            self.assertEqual('phase_2', planned[name]['status'])
            self.assertIn('singlecell', planned[name]['notes'])
            return
        with self.assertRaises(K.ContractError) as e:
            TREG.get(name)
        self.assertIn('needs an optional environment', str(e.exception))
        self.assertIn('uv sync --extra singlecell', str(e.exception))

    def test_a_tool_that_does_not_accept_this_modality_is_refused(self):
        with self.assertRaises(K.ContractError) as e:
            self.a_plan(tool='bulk.expression_comparison',
                        analysis_type='bulk_expression_comparison')
        self.assertIn('does not accept', str(e.exception))

    def test_a_tool_that_does_not_perform_this_analysis_is_refused(self):
        with self.assertRaises(K.ContractError) as e:
            self.a_plan(analysis_type='correlation')
        self.assertIn('does not perform', str(e.exception))

    def test_every_registered_tool_declares_what_it_needs(self):
        for spec in TREG.TOOLS.values():
            self.assertTrue(spec.modalities and spec.analysis_types)
            self.assertTrue(spec.required_metadata)
            self.assertTrue(spec.notes)
            self.assertTrue(callable(spec.runner))

    def test_planned_tools_are_listed_but_not_runnable(self):
        """Whatever a tool is waiting on, it is never in both lists: reporting one
        as planned while a plan using it runs would be the misleading direction."""
        d = TREG.describe()
        names = {t['name'] for t in d['implemented']}
        for p in d['planned']:
            self.assertNotIn(p['name'], names)
            self.assertNotIn(p['name'], TREG.TOOLS)
            self.assertIn(p['status'], ('planned',) + TREG.IMPLEMENTED_STATUSES)

    def test_a_tool_waiting_on_an_extra_is_not_confused_with_unwritten_work(self):
        """'planned' means nobody wrote it; the other statuses mean it is written
        and gated on an install the reader can perform. Only the first is a reason
        for a document to draw the capability as absent."""
        self.assertFalse(TREG.is_implemented('cytometry.gating'))
        self.assertFalse(TREG.is_implemented('external.deseq2'))
        self.assertTrue(TREG.is_implemented('single_cell.pseudobulk_comparison'))
        self.assertTrue(TREG.is_implemented('bulk.expression_comparison'))
        for name in TREG.TOOLS:
            self.assertTrue(TREG.is_implemented(name))


class MissingMetadataRefusalTests(PlanCase):
    def test_a_dataset_without_the_required_design_is_refused_and_names_the_field(self):
        ING.ingest_local(FACS, dataset_id='facs-bare', title='M-CSF comparison, no design',
                         modality='cytometry_summary', perturbation='M-CSF concentration',
                         experimental_design={})
        with self.assertRaises(K.ContractError) as e:
            self.a_plan(plan_id='plan-bare', dataset_ids=['facs-bare'])
        msg = str(e.exception)
        self.assertIn('condition_column', msg)
        self.assertIn('rather than letting the analysis assume it', msg)

    def test_an_unregistered_dataset_is_refused(self):
        with self.assertRaises(K.ContractError) as e:
            self.a_plan(dataset_ids=['not-registered'])
        self.assertIn('not-registered', str(e.exception))


class ExecutionTests(PlanCase):
    def test_a_result_validates_and_keeps_the_lineage_separate(self):
        r = EX.execute(self.a_plan())
        self.assertEqual([], K.schema_errors('analysis_result', r))
        self.assertEqual('derived_analysis', r['evidence_class'])
        self.assertEqual('private_user_dataset', r['source_evidence_class'])
        self.assertEqual('private', r['source_visibility'])
        self.assertEqual(['facs-demo'], r['parent_dataset_ids'])

    def test_the_result_carries_the_checksums_of_what_it_read(self):
        r = EX.execute(self.a_plan())
        self.assertIn(K.sha256_file(FACS), r['provenance']['input_checksums'])
        self.assertTrue(r['provenance']['code_sha256'])
        self.assertTrue(r['provenance']['plan_sha256'])

    def test_the_result_states_the_uncertainty_it_was_asked_about(self):
        r = EX.execute(self.a_plan())
        self.assertEqual(GAP, r['uncertainty_ref']['ref'])

    def test_the_same_plan_twice_gives_the_same_statistics(self):
        a = EX.execute(self.a_plan(plan_id='plan-a'))
        b = EX.execute(self.a_plan(plan_id='plan-b'))
        self.assertEqual([s['effect'] for s in a['statistics']],
                         [s['effect'] for s in b['statistics']])
        self.assertEqual([s['q_value'] for s in a['statistics']],
                         [s['q_value'] for s in b['statistics']])

    def test_repeated_wells_from_one_donor_count_once(self):
        """Six rows, three donors. Testing the wells would inflate n by the number
        of times each person was sampled."""
        r = EX.execute(self.a_plan())
        for s in r['statistics']:
            self.assertEqual(3, s['n_a'])
            self.assertEqual(3, s['n_b'])
        self.assertIn('donors', r['comparison']['independence_note'])

    def test_a_bulk_comparison_runs_through_the_same_contract(self):
        ING.ingest_local(BULK, dataset_id='bulk-demo', title='Bulk expression under M-CSF',
                         modality='bulk_rna', perturbation='M-CSF concentration',
                         experimental_design={'condition_column': 'condition',
                                              'control': 'control',
                                              'treatments': ['mcsf_high'],
                                              'replicate_column': 'replicate'})
        p = self.a_plan(plan_id='plan-bulk', dataset_ids=['bulk-demo'],
                        analysis_type='bulk_expression_comparison',
                        tool='bulk.expression_comparison')
        r = EX.execute(p)
        self.assertEqual([], K.schema_errors('analysis_result', r))
        self.assertTrue(any(s['readout'] == 'CD14' for s in r['statistics']))

    def test_combining_datasets_is_refused_rather_than_concatenated(self):
        ING.ingest_local(FACS, dataset_id='facs-two', title='A second M-CSF comparison',
                         modality='cytometry_summary', perturbation='M-CSF concentration',
                         experimental_design=DESIGN)
        p = self.a_plan(plan_id='plan-two', dataset_ids=['facs-demo', 'facs-two'])
        with self.assertRaises(K.ContractError) as e:
            EX.execute(p)
        self.assertIn('one dataset per plan', str(e.exception))


class ResultSemanticsTests(PlanCase):
    def test_a_result_is_never_citable(self):
        r = EX.execute(self.a_plan())
        self.assertIs(False, r['citable'])

    def test_a_private_source_is_recorded_as_uncitable_in_the_limitations(self):
        r = EX.execute(self.a_plan())
        joined = ' '.join(r['limitations']).lower()
        self.assertIn('never become a literature citation', joined)

    def test_candidate_parameters_use_the_existing_lever_vocabulary(self):
        r = EX.execute(self.a_plan())
        self.assertTrue(r['candidate_process_parameters'])
        for c in r['candidate_process_parameters']:
            self.assertEqual({'parameter', 'direction', 'arm_scope', 'basis', 'confidence',
                              'suggested_range'}, set(c))
            self.assertIn(c['direction'],
                          ('increase', 'decrease', 'shorten', 'extend', 'earlier', 'later',
                           'revisit'))
            self.assertIn(c['confidence'], ('low', 'moderate', 'high'))

    def test_a_candidate_is_worded_as_evidence_not_as_an_instruction(self):
        r = EX.execute(self.a_plan())
        basis = r['candidate_process_parameters'][0]['basis']
        self.assertIn('worth testing', basis)
        self.assertIn('not a conclusion that it must move', basis)

    def test_a_counterweight_readout_is_reported_beside_the_gain(self):
        """Viability falls in this comparison; a result that only reported the
        gain would read as a recommendation."""
        r = EX.execute(self.a_plan())
        self.assertIn('Counterweight', r['candidate_process_parameters'][0]['basis'])
        self.assertTrue(any('viability' in i['implication'] for i in r['process_implications']))

    def test_confidence_is_capped_at_moderate_from_one_dataset(self):
        r = EX.execute(self.a_plan())
        self.assertIn(r['confidence'], ('low', 'moderate'))
        self.assertTrue(any('capped at moderate' in x for x in r['limitations']))

    def test_a_perturbation_that_maps_to_no_parameter_suggests_nothing(self):
        param, why = IMP.parameter_for_perturbation('phase of the moon')
        self.assertIsNone(param)
        self.assertIn('inventing the link', why)

    def test_a_result_contains_no_protocol_and_cannot_revise_one(self):
        """An AnalysisResult is evidence. It has no field through which a
        protocol change could travel, which is what keeps the decision with the
        orchestrator."""
        r = EX.execute(self.a_plan())
        for forbidden in ('revision_brief', 'protocol', 'protocol_id', 'arm_adjustment',
                          'decision', 'route_to'):
            self.assertNotIn(forbidden, r)


class ExternalAdapterTests(unittest.TestCase):
    def test_a_record_without_a_tool_version_is_refused(self):
        with self.assertRaises(K.ContractError) as e:
            EXT.record(command='x', tool='deseq2', tool_version='', parameters={},
                       inputs=[], outputs=[], exit_status=0, duration_s=1, environment={})
        self.assertIn('cannot be reproduced', str(e.exception))

    def test_the_mock_is_structurally_complete_and_labelled(self):
        rec = EXT.run_mock('deseq2', inputs=[str(FACS)])
        for f in EXT.REQUIRED:
            self.assertIn(f, rec)
        self.assertIs(True, rec['mock'])
        self.assertEqual([K.sha256_file(FACS)], rec['input_checksums'])

    def test_an_unknown_adapter_is_refused(self):
        with self.assertRaises(K.ContractError):
            EXT.run_mock('nonsense', inputs=[])

    def test_no_adapter_is_required_for_the_base_install(self):
        """Ordinary CI must not need R, Bioconductor or a container runtime."""
        d = EXT.describe()
        self.assertTrue(d)
        for name, info in d.items():
            self.assertIn('available_here', info)


class MockEvidenceTests(PlanCase):
    def test_a_mock_external_run_cannot_produce_evidence(self):
        rec = EXT.run_mock('deseq2', inputs=[str(FACS)])
        with self.assertRaises(K.ContractError) as e:
            EX.execute(self.a_plan(), external_record=rec)
        self.assertIn('mock', str(e.exception))


if __name__ == '__main__':
    unittest.main()
