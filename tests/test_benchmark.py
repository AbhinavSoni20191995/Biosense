"""Benchmarks: a demonstration built from real artifacts, and the refusals that
keep it honest.

The two properties that matter most, tested directly:

* a benchmark PASSES when an unsupported parameter is labelled not_modelled, and
  FAILS if BioSense silently drops it or invents an effect for it;
* a public-safe export REFUSES when private lineage exists, rather than
  attempting to anonymise scientifically sensitive material.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.benchmark import cli as BCLI
from biosense.benchmark import figures as FG
from biosense.benchmark import privacy as PV
from biosense.benchmark import report as RP
from biosense.benchmark import runner as BR
from biosense.data import ingest as ING
from biosense.data import roots as DR
from biosense.evidence import expert as EK

CONFIG = K.ROOT / 'benchmarks' / 'configs' / 'macrophage_mcsf_demo.json'
BUNDLE = K.ROOT / 'benchmarks' / 'public' / 'macrophage_mcsf_demo'
FACS = K.ROOT / 'examples' / 'datasets' / 'facs_mcsf_synthetic.csv'


class ConfigTests(unittest.TestCase):
    def test_the_shipped_config_validates(self):
        c = BR.load_config(CONFIG)
        self.assertEqual([], K.schema_errors('benchmark_config', c))
        self.assertEqual('public_safe', c['export_policy'])

    def test_an_unknown_capability_is_refused(self):
        c = K.read_json(CONFIG)
        c['expected_capabilities'] = ['does_my_science_for_me']
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
            json.dump(c, fh)
        with self.assertRaises(K.ContractError) as e:
            BR.load_config(fh.name)
        self.assertIn('unknown capability', str(e.exception))
        os.unlink(fh.name)

    def test_a_benchmark_without_an_uncertainty_is_refused(self):
        c = K.read_json(CONFIG)
        c.pop('uncertainty')
        with self.assertRaises(K.ContractError) as e:
            BR.run(c)
        self.assertIn('a decision is waiting on it', str(e.exception))


class PublicRunTests(unittest.TestCase):
    """The committed public demonstration, re-run from its configuration."""

    @classmethod
    def setUpClass(cls):
        cls.result = BR.run(BR.load_config(CONFIG))

    def test_the_result_validates(self):
        self.assertEqual([], K.schema_errors('benchmark_result', self.result))

    def test_every_expected_capability_is_demonstrated(self):
        cfg = BR.load_config(CONFIG)
        passed = {r['capability'] for r in self.result['scorecard']['rows']
                  if r['status'] == 'PASS'}
        self.assertEqual(set(), set(cfg['expected_capabilities']) - passed)
        self.assertEqual(0, self.result['scorecard']['failed'])

    def test_the_scorecard_says_it_measures_capability_not_biology(self):
        note = self.result['scorecard']['note'].lower()
        self.assertIn('capability', note)
        self.assertIn('biological truth', note)
        self.assertIn('biologically wrong', note)

    def test_the_run_is_reproducible(self):
        again = BR.run(BR.load_config(CONFIG))
        self.assertEqual([r['status'] for r in self.result['scorecard']['rows']],
                         [r['status'] for r in again['scorecard']['rows']])
        a = [e['absolute_change'] for e in self.result['simulator']['effects']]
        b = [e['absolute_change'] for e in again['simulator']['effects']]
        self.assertEqual(a, b)

    def test_provenance_records_what_produced_it(self):
        p = self.result['provenance']
        self.assertTrue(p['biosense_version'])
        self.assertTrue(p['code_sha256'])
        self.assertTrue(p['project_version'])
        self.assertEqual('ipsc_monocyte_v1', p['simulator_model'])
        self.assertTrue(p['dataset_checksums'])
        self.assertTrue(p['tool_versions'])
        self.assertIn('simulator', p['seeds'])

    def test_the_project_profile_travels_with_the_result(self):
        self.assertEqual('ipsc_macrophage', self.result['project']['project_id'])
        self.assertEqual('1.0.0', self.result['project']['version'])


class SimulatorCoverageHonestyTests(unittest.TestCase):
    """The row the brief singles out: a benchmark passes when an unsupported
    parameter is honestly labelled, and fails if it is dropped or invented."""

    @classmethod
    def setUpClass(cls):
        cls.result = BR.run(BR.load_config(CONFIG))

    def test_every_declared_candidate_is_accounted_for(self):
        declared = {c['parameter'] for c in self.result['candidate_parameters']}
        sim = self.result['simulator']
        accounted = {r['parameter_id'] for r in sim['handoff']['applied']} | \
                    {r['parameter_id'] for r in sim['handoff']['skipped']}
        self.assertEqual(declared, accounted)

    def test_the_unsupported_parameter_is_labelled_not_dropped(self):
        skipped = self.result['simulator']['handoff']['skipped']
        temp = next(r for r in skipped if r['parameter_id'] == 'temperature_c')
        self.assertEqual('not_modelled', temp['simulator_coverage'])
        self.assertTrue(temp['reason'])
        self.assertTrue(any(r['capability'] == 'simulator_coverage_checked'
                            and r['status'] == 'PASS'
                            for r in self.result['scorecard']['rows']))

    def test_no_simulated_effect_exists_for_an_unsupported_parameter(self):
        metrics = {e['metric'] for e in self.result['simulator']['effects']}
        self.assertNotIn('temperature_c', metrics)

    def test_validation_fails_a_result_that_dropped_a_candidate(self):
        """If BioSense silently shrank the candidate set, validate must catch it."""
        bad = json.loads(json.dumps(self.result))
        bad['simulator']['handoff']['skipped'] = []
        problems = BCLI.validate(bad, BUNDLE)
        self.assertTrue(any('neither applied nor explained' in p for p in problems), problems)

    def test_validation_fails_an_unmodelled_parameter_with_no_reason(self):
        bad = json.loads(json.dumps(self.result))
        bad['simulator']['handoff']['skipped'][0]['reason'] = ''
        problems = BCLI.validate(bad, BUNDLE)
        self.assertTrue(any('no stated reason' in p for p in problems), problems)


class NumericProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = BR.run(BR.load_config(CONFIG))

    def test_every_simulator_number_is_labelled_simulated(self):
        for e in self.result['simulator']['effects']:
            self.assertEqual('simulated', e['estimate_type'])

    def test_measured_and_simulated_effects_are_distinguishable(self):
        h = self.result['hypotheses'][0]
        types = {e['estimate_type'] for e in h['expected_effects']}
        self.assertIn('derived', types)
        self.assertIn('simulated', types)

    def test_validation_catches_a_simulated_effect_mislabelled_as_measured(self):
        bad = json.loads(json.dumps(self.result))
        bad['simulator']['effects'][0]['estimate_type'] = 'measured'
        problems = BCLI.validate(bad, BUNDLE)
        self.assertTrue(any('not labelled simulated' in p for p in problems), problems)

    def test_validation_catches_a_magnitude_withheld_without_a_reason(self):
        bad = json.loads(json.dumps(self.result))
        e = bad['hypotheses'][0]['expected_effects'][0]
        e['magnitude_estimated'] = False
        e['withheld_reason'] = None
        problems = BCLI.validate(bad, BUNDLE)
        self.assertTrue(any('withholds a magnitude with no reason' in p for p in problems))

    def test_every_narrative_sentence_resolves_to_facts(self):
        for s in self.result['narrative']:
            self.assertTrue(s['ok'], f'step {s["step"]}: {s["problems"]}')

    def test_validation_catches_an_invented_number_in_the_narrative(self):
        bad = json.loads(json.dumps(self.result))
        bad['narrative'][0]['ok'] = False
        bad['narrative'][0]['problems'] = ["'20' appears in the text but in no structured fact"]
        problems = BCLI.validate(bad, BUNDLE)
        self.assertTrue(any('failed validation' in p for p in problems))


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = os.environ.get(DR.PRIVATE_ENV)
        os.environ[DR.PRIVATE_ENV] = str(self.tmp / 'private')
        os.environ[DR.CACHE_ENV] = str(self.tmp / 'cache')
        DR.ensure_roots()
        ING.ingest_local(FACS, dataset_id='private-facs-bm',
                         title='Somebody\'s own M-CSF titration',
                         modality='cytometry_summary', perturbation='M-CSF concentration',
                         cell_type='iPSC-derived monocyte',
                         experimental_design={'condition_column': 'condition',
                                              'control': 'control',
                                              'treatments': ['mcsf_high'],
                                              'replicate_column': 'replicate',
                                              'donor_column': 'donor'})
        self.cfg = K.read_json(CONFIG)
        self.cfg.update(benchmark_id='private_bm', datasets=['private-facs-bm'])

    def tearDown(self):
        if self._old is None:
            os.environ.pop(DR.PRIVATE_ENV, None)
        else:
            os.environ[DR.PRIVATE_ENV] = self._old
        os.environ.pop(DR.CACHE_ENV, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_public_safe_export_refuses_private_lineage(self):
        cfg = dict(self.cfg, export_policy='public_safe')
        r = BR.run(cfg)
        self.assertTrue(r['privacy']['has_private_lineage'])
        self.assertFalse(r['privacy']['safe_to_publish'])
        self.assertIn('refuses rather than attempting to anonymise',
                      r['privacy']['refusal_reason'])

    def test_the_cli_writes_nothing_when_it_refuses(self):
        cfg = dict(self.cfg, export_policy='public_safe')
        path = self.tmp / 'cfg.json'
        K.write_json_atomic(path, cfg)
        out = self.tmp / 'should_not_exist'
        code = BCLI.main(['run', '--config', str(path), '--out', str(out)])
        self.assertEqual(2, code)
        self.assertFalse(out.exists())

    def test_a_private_benchmark_is_written_outside_the_repository(self):
        cfg = dict(self.cfg, export_policy='private')
        r = BR.run(cfg)
        d = BCLI.bundle_dir(r)
        self.assertTrue(DR.is_private_path(d))
        self.assertFalse(str(d).startswith(str(K.ROOT / 'benchmarks')))

    def test_expert_knowledge_makes_a_run_private(self):
        k = EK.knowledge('EK-T1', 'Our line goes adherent above 100 ng/mL M-CSF.',
                         'unpublished_observation',
                         scope={'project_id': 'ipsc_macrophage', 'cell_type': None,
                                'stage': None, 'cell_line': None, 'donor': None,
                                'equipment': None},
                         parameter_claims=[{'parameter_id': 'mcsf_ng_ml',
                                            'claim': 'avoid_above', 'value': 100}])
        EK.save(k)
        cfg = dict(K.read_json(CONFIG), benchmark_id='ek_bm',
                   expert_knowledge=['EK-T1'], export_policy='public_safe')
        r = BR.run(cfg, private_root=DR.private_root())
        self.assertIn('expert_knowledge', r['privacy']['private_sources'])
        self.assertFalse(r['privacy']['safe_to_publish'])

    def test_expert_knowledge_narrows_a_range_and_is_attributed(self):
        k = EK.knowledge('EK-T2', 'Our line goes adherent above 100 ng/mL M-CSF.',
                         'unpublished_observation',
                         scope={'project_id': 'ipsc_macrophage', 'cell_type': None,
                                'stage': None, 'cell_line': None, 'donor': None,
                                'equipment': None},
                         parameter_claims=[{'parameter_id': 'mcsf_ng_ml',
                                            'claim': 'avoid_above', 'value': 100}])
        EK.save(k)
        cfg = dict(K.read_json(CONFIG), benchmark_id='ek_bm2',
                   expert_knowledge=['EK-T2'], export_policy='private')
        r = BR.run(cfg, private_root=DR.private_root())
        sr = r['hypotheses'][0]['parameter']['search_range']
        self.assertEqual(100.0, sr['upper'])
        self.assertIn('EK-T2', sr['narrowed_by'])

    def test_absolute_paths_are_caught_by_the_scanner(self):
        findings = PV.scan({'a': '/home/someone/private_data/x.csv'})
        self.assertTrue(findings)
        kinds = {f['what'] for f in findings}
        self.assertTrue(any('home' in k or 'private data root' in k for k in kinds))

    def test_a_private_dataset_id_is_caught_in_a_public_bundle(self):
        findings = PV.scan({'note': 'derived from private-facs-bm'},
                           private_ids=['private-facs-bm'])
        self.assertTrue(findings)

    def test_the_committed_public_bundle_leaks_nothing(self):
        result = K.read_json(BUNDLE / 'benchmark.json')
        self.assertTrue(result['privacy']['safe_to_publish'])
        self.assertEqual([], PV.validate_bundle(BUNDLE, result))


class BundleTests(unittest.TestCase):
    def test_the_committed_bundle_validates(self):
        result = K.read_json(BUNDLE / 'benchmark.json')
        self.assertEqual([], BCLI.validate(result, BUNDLE))

    def test_the_bundle_has_the_files_a_reader_needs(self):
        for rel in ('benchmark.json', 'summary.md', 'provenance/execution.json',
                    'provenance/datasets.json', 'provenance/tools.json',
                    'tables/scorecard.csv', 'detailed/hypotheses.json'):
            self.assertTrue((BUNDLE / rel).is_file(), rel)

    def test_figures_are_version_controlled_svg_in_both_themes(self):
        figs = sorted((BUNDLE / 'figures').glob('*.svg'))
        self.assertTrue(figs)
        names = {p.stem for p in figs}
        for base in ('workflow', 'evidence_summary', 'simulator_comparison'):
            self.assertIn(f'{base}-light', names)
            self.assertIn(f'{base}-dark', names)
        for p in figs:
            text = p.read_text()
            self.assertTrue(text.startswith('<svg'))
            self.assertNotIn('<image', text)
            self.assertIn('aria-label', text)

    def test_the_figures_use_this_projects_labels(self):
        svg = (BUNDLE / 'figures' / 'parameter_change-light.svg').read_text()
        self.assertIn('M-CSF', svg)
        self.assertIn('ipsc_macrophage', svg)
        self.assertNotIn('IL-15', svg)

    def test_a_figure_with_no_data_is_not_drawn(self):
        from biosense import projects as PJ
        from biosense.production import sim_candidate as SC
        cart = PJ.load('cart_expansion')
        empty = SC.compare_conditions(cart, candidates=[{'parameter': 'il7_ng_ml',
                                                         'direction': 'increase'}],
                                      candidate_values={'il7_ng_ml': 20})
        fake = {'simulator': empty, 'hypotheses': []}
        self.assertIsNone(FG.simulator_comparison(fake, FG.LIGHT))
        self.assertIsNone(FG.hypothesis_effect(fake, FG.LIGHT))

    def test_the_summary_labels_every_number_it_shows(self):
        md = (BUNDLE / 'summary.md').read_text()
        self.assertIn('SIMULATED', md)
        self.assertIn('DERIVED', md)
        self.assertIn('SYNTHETIC DEMONSTRATION', md)
        self.assertIn('NOT MODELLED', md)

    def test_the_summary_stays_concise(self):
        md = (BUNDLE / 'summary.md').read_text()
        self.assertLess(len(md.splitlines()), 200,
                        'the default report is meant to be 1-3 pages, not a technical dump')

    def test_the_detailed_export_holds_what_the_summary_omits(self):
        detail = K.read_json(BUNDLE / 'detailed' / 'analyses.json')
        self.assertTrue(detail)
        self.assertTrue(detail[0]['statistics'])
        self.assertTrue(detail[0]['provenance']['input_checksums'])


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = BR.run(BR.load_config(CONFIG))
        cls.md = RP.summary_markdown(cls.result)

    def test_the_report_answers_the_questions_it_should(self):
        for heading in ('What we were trying to improve', 'Research context',
                        'The question that was blocking a decision', 'Evidence considered',
                        'Analysis performed', 'Current hypothesis', 'Quantified effects',
                        'Candidate parameter', 'Simulator prediction',
                        'Recommended next experiment', 'Main limitations',
                        'Capability scorecard', 'Privacy'):
            self.assertIn(heading, self.md, heading)

    def test_percentage_changes_are_reported_in_percentage_points(self):
        self.assertIn('pp', self.md)

    def test_the_unmodelled_parameter_appears_in_the_report(self):
        self.assertIn('temperature_c', self.md)
        self.assertIn('NOT MODELLED', self.md)

    def test_tables_are_written_for_a_spreadsheet(self):
        with tempfile.TemporaryDirectory() as d:
            written = RP.write_tables(self.result, d)
            names = {p.name for p in written}
            self.assertIn('analysis_statistics.csv', names)
            self.assertIn('hypothesis_effects.csv', names)
            self.assertIn('candidate_parameters.csv', names)


if __name__ == '__main__':
    unittest.main()
